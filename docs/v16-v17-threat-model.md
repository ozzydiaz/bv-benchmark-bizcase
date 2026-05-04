# ADVERSARIAL THREAT MODEL: ENGINE OUTPUTS & FINANCIAL CASE
## Two Functions Slated for v1.6/v1.7 Refactor

**Scope**: v1.5.0-layer3-zero-drift (locked, read-only review)  
**Date**: May 4, 2026  
**Analyst**: Adversarial Code Review

---

## TARGET 1: `engine/outputs.py::_terminal_value(cf_last, wacc, growth_rate)`

### A. CURRENT IMPLEMENTATION

**File**: [engine/outputs.py](engine/outputs.py#L175-L179)

```python
def _terminal_value(cf_last: float, wacc: float, growth_rate: float) -> float:
    """Gordon Growth Model terminal value."""
    if wacc <= growth_rate:
        return 0.0
    return cf_last * (1 + growth_rate) / (wacc - growth_rate)
```

**Call Sites** (from grep search):
1. [engine/outputs.py:223](engine/outputs.py#L223) — `tv = _terminal_value(savings[YEARS], wacc, g)` → 10-year terminal value
2. [engine/outputs.py:225](engine/outputs.py#L225) — `tv_5yr_discounted = _terminal_value(savings[5], wacc, g) / (1 + wacc) ** 5` → 5-year terminal value

**Downstream Output Fields Dependent on This Value**:
- `summary.terminal_value` (line 229) — discounted TV, stored as PV
- `summary.npv_10yr_with_terminal_value` (line 230) — 10Y NPV + TV
- `summary.npv_5yr_with_terminal_value` (line 231) — 5Y NPV + TV
- `summary.roi_10yr` (line 237) — ROI computed as `npv_10yr_with_terminal_value / npv_az_10yr`
- `summary.roi_5yr` (line 238) — ROI computed as `npv_5yr_with_terminal_value / npv_az_5yr`
- [engine/fact_checker.py:471](engine/fact_checker.py#L471) — `"project_npv_10yr": summary.npv_10yr_with_terminal_value` (external audit cell)

**Total downstream impact**: 5 summary fields + 1 external audit binding = 6 direct dependencies; cascades to all ROI/NPV reporting and executive summary.

---

### B. TRUST BOUNDARY INPUTS

**Where do `cf_last`, `wacc`, and `growth_rate` originate?**

#### `cf_last` (Year 10 or Year 5 cash flow savings)
- **Source**: [outputs.py:207](engine/outputs.py#L207) — `savings = fc.savings()`
- **Chain**: 
  - `fc.savings()` = `sq_total() - az_total()` [financial_case.py:171]
  - Both derived from P&L (depreciation + opex) populated in `financial_case.compute()`
  - Ultimate inputs: RVTools inventory + BA workbook (consumption plans, hardware config)
- **Editable in UI?** **NO** — savings are *derived* from two cost scenarios, not directly entered
- **RVTools sourced?** **YES** — VM/host inventory, storage, CPU counts feed both scenarios
- **BA workbook sourced?** **YES** — Hardware lifecycle assumptions, discount rates, migration ramps
- **Untrusted file risk**: YES — a malicious RVTools export could populate zero VMs → zero savings → zero TV

#### `wacc` (Weighted Average Cost of Capital)
- **Source**: [outputs.py:206](engine/outputs.py#L206) — `wacc = benchmarks.wacc`
- **Default**: [models.py:442](engine/models.py#L442) — `wacc: float = 0.07` (7%)
- **Editable in UI?** **YES** — appears on benchmarks page (Step 3)
- **RVTools sourced?** **NO** — pure financial parameter
- **BA workbook sourced?** **YES** — can be overridden per engagement
- **Untrusted file risk**: YES — malicious input could set `wacc = 0.02` → denominator very small → TV inflates

#### `growth_rate` (Perpetual growth rate / `perpetual_growth_rate`)
- **Source**: [outputs.py:207](engine/outputs.py#L207) — `g = benchmarks.perpetual_growth_rate`
- **Default**: [models.py:477](engine/models.py#L477) — `perpetual_growth_rate: float = 0.03` (3%)
- **Editable in UI?** **YES** — benchmarks page (Step 3)
- **RVTools sourced?** **NO**
- **BA workbook sourced?** **YES** — can be customer-specific
- **Untrusted file risk**: YES — critical guard condition depends on this value

---

### C. NUMERICAL EDGE CASES

#### **1. Division by Zero** 
- **Trigger**: `wacc == growth_rate`
- **Impact**: Line 177 guard returns `0.0` — **graceful handling**
- **Risk**: When `wacc == growth_rate` (e.g., both 0.05), TV silently becomes zero even if `cf_last` is large
- **Example**: $10M Y10 savings × 0.05/(0.05-0.05) should be infinite perpetuity → engine returns $0
- **Adversarial case**: Set `growth_rate = 0.069` and `wacc = 0.07` → TV = $10M × 1.069/(0.001) = $10.69B (fragile numerics near boundary)

#### **2. Negative Output (Wrong Sign)**
- **Trigger**: `cf_last < 0` (negative savings = Azure costs more than on-prem) AND `wacc > growth_rate`
- **Impact**: `(-X) * (1+g) / (Y) = negative TV` — mathematically correct, but semantically odd
- **Example**: Customer A has `savings[10] = -$1,995,190` (negative). TV = $-1.995M × 1.03 / 0.04 = **$-51.4M**
- **Downstream risk**: `npv_10yr_with_terminal_value = 2.57M + (-51.4M) = -48.8M` (flips sign of business case result)
- **Layer 3 reality check**: Customer A's actual workbook output: `headline.terminal_value_10y = $26,117,030.61` (POSITIVE despite negative Y10 savings)
  - **Discrepancy**: Replica oracle computes TV on *Status Quo* cash flow alone, not savings. Engine computes on relative savings. Different semantics.

#### **3. NaN / Inf Cases**
- **Trigger 1**: `cf_last = NaN` (propagated from broken savings computation) → returns NaN
- **Trigger 2**: `wacc - growth_rate = 0` (caught by guard) → returns 0.0 ✓
- **Trigger 3**: `cf_last = ±inf` (unconstrained upstream error) → returns ±inf
- **No trap**: Python allows inf/inf = NaN; no exception raised

#### **4. Silent Zero Risk**
- **Scenario 1**: `wacc <= growth_rate` → guard returns 0.0 regardless of `cf_last`
  - A $100M Y10 savings with perpetual growth ≥ discount rate becomes *zero* TV
  - **Silent**: No warning logged, no exception, just returns 0
- **Scenario 2**: `cf_last = 0` (all Y10 savings consumed by Azure costs) → TV = 0 ✓ correct
- **Scenario 3**: `cf_last = epsilon` (microscopically positive due to floating-point rounding) → TV = epsilon × (1+g)/(wacc-g) ≈ epsilon (tiny but not zero)

#### **5. Sign Flip Pattern**
- **Pattern**: Negative cash flow × positive (1+growth_rate) / positive (wacc - growth_rate) = **negative TV**
- **Customer A case**: `savings[10] < 0` → TV < 0
- **Business interpretation**: "Perpetual annual loss of $X discounted to today yields $-Y million loss"
- **UI risk**: CFO sees ROI = -0.47, Terminal Value = -$51M → assumes model is broken or forecasting disaster

#### **6. Ordering Surprises (Negative Growth)**
- **Scenario**: `growth_rate < 0` (e.g., -0.05 for assumed declining savings)
- **Formula becomes**: `cf_last * (1 - 0.05) / (0.07 - (-0.05)) = cf_last × 0.95 / 0.12` ✓ works fine
- **No ordering problem** unless both are negative:
  - If `growth_rate = -0.10` and `wacc = 0.07`: denominator = 0.17 ✓ OK
  - If `growth_rate = -0.08` and `wacc = -0.02`: denominator = 0.06 ✓ OK (unlikely pair)
  - If `growth_rate = -0.10` and `wacc = -0.15`: denominator = -0.05 → sign flip (numerator changes)

#### **7. Input Magnitude Edge Cases**
- **Very small `wacc`**: `wacc = 0.001` (0.1%) → perpetuity is 100× larger
  - `cf_last = $1M`, `g = 0.001` → TV = $1M × 1.001 / 0.000 ≠ infinity (denominator tiny)
- **Exceeds safe float**: `cf_last = 1e20` → TV = 1e20 × 1.03 / 0.04 = 2.575e21 (near float64 limit ~1.8e308) ✓ representable but risky
- **Very large growth rate**: `growth_rate = 0.5` (50% perpetual growth) → guard prevents (`0.5 < 0.07` false) → returns 0.0 ✓ safe

---

### D. REFACTOR RISKS FOR v1.6/v1.7

**Planned Changes** (from test scaffold [test_v16_tv_method_scaffold.py](tests/test_v16_tv_method_scaffold.py)):
- v1.6 will add `tv_method: Literal["gordon", "exit_multiple", "none"]`
- v1.6 will add `tv_floor_at_zero: bool`
- v1.7 will swap flat `acd` for family-blended weighted average (separate threat, TARGET 2)

**Five Highest-Likelihood Refactor Breaks on Layer 3 Parity**:

| Risk # | Break Scenario | Root Cause | Affected Output | Regression Test |
|--------|---|---|---|---|
| **R1** | Default `tv_method != "gordon"` | PR author forgets AC-2 back-compat invariant; ships with `tv_method="none"` default | `terminal_value`, `npv_*_with_terminal_value`, `roi_*` all become zero | `test_ac2_default_is_gordon()` — assert `BenchmarkConfig().tv_method == "gordon"` (scaffold has this) |
| **R2** | `tv_floor_at_zero=True` inadvertently enabled | Feature PR adds floor without opt-in; negative TVs clip to 0 | Customer A's TV flips from -$51M to $0 (downstream: ROI sign changes) | `test_ac6_floor_negative_tv()` — run Customer A with floor=True; TV must stay negative if default is False |
| **R3** | Wrong `cf_anchor` year in switch logic | Refactor confuses `savings[5]` vs `savings[10]` in new method branches; 5Y TV uses wrong cash flow | `npv_5yr_with_terminal_value` drifts; 5Y ROI computed on 10Y perpetuity | `test_ac5_exit_multiple_uses_correct_year()` — for each method, assert TV(5) uses `savings[5]`, TV(10) uses `savings[10]` |
| **R4** | `exit_multiple` multiplier not defined | Field added but default value missing or wired to wrong benchmark cell | 5Y TV = `savings[5] × tv_exit_multiple` but multiplier undefined → NaN | `test_ac5_exit_multiple_requires_field()` — assert `BenchmarkConfig` has `tv_exit_multiple: float` field |
| **R5** | Discounting logic moved into helper | v1.6 refactor moves PV discount into `_terminal_value()` instead of `outputs.compute()` | TV gets discounted twice (once in helper, once in caller at line 224/226) | `test_ac3_gordon_matches_replica()` — re-run Customer A; `terminal_value` must match oracle exactly |

**Acceptance Criteria** (from scaffold):
- AC-1: `tv_method` enum exists ✓
- AC-2: Default is "gordon" ← **CRITICAL for Layer 3 parity**
- AC-3: "gordon" produces *exactly* current output
- AC-4: "none" returns 0
- AC-5: "exit_multiple" returns `cf_last × benchmarks.tv_exit_multiple`
- AC-6: `tv_floor_at_zero=True` clips negative TV to 0
- AC-7: **Defaults preserve Layer 3 parity (MAX_ENGINE_DRIFT = 0)** ← hard CI fail if violated

---

### E. ADVERSARIAL PROMPT-INJECTION VECTORS

**Worst-case inputs a malicious BA workbook or RVTools file could inject:**

1. **Set `growth_rate > wacc`** 
   - Input: `perpetual_growth_rate = 0.08`, `wacc = 0.07` (both editable in UI + BA workbook)
   - Guard fires: returns `0.0` instead of correct TV
   - **Business impact**: Multi-million-dollar perpetuity erased silently
   - **Attacker motive**: Make Azure case look worse (negative ROI case) by hiding positive TV offset
   - **Mitigation**: UI should warn when `growth_rate >= wacc - 0.01` (2% margin)

2. **Set `growth_rate = wacc exactly`**
   - Input: Both 0.07 (can hand-enter in benchmarks page)
   - Guard fires on `<=` condition: returns 0.0
   - Same as above; perpetuity collapses

3. **Negative `wacc` with negative `growth_rate` both provided**
   - Input: `wacc = -0.05`, `growth_rate = -0.10` (malicious/broken benchmark inputs)
   - Formula: `cf_last × (1 - 0.10) / (-0.05 - (-0.10)) = cf_last × 0.9 / 0.05` → huge positive
   - **No guard catches this** (guard only checks `<=`)
   - Finance nonsense (negative discount rate) but computes without error

4. **Extreme `cf_last` from zero-VM inventory**
   - Input: RVTools export with no powered-on VMs (all are templates or disabled)
   - Result: `savings[YEARS] = 0 - (Azure admin costs) = small negative` (on-prem shutdown, Azure stub remains)
   - TV = tiny negative × (1.03) / 0.04 ≈ small negative (harmless, but reveals zero-VM case)

5. **Set `cf_last = inf` via upstream computation error**
   - Propagated from broken depreciation or consumption builder (e.g., division by zero)
   - TV = inf × 1.03 / 0.04 = inf
   - No exception; `npv_10yr_with_terminal_value = 2.57M + inf = inf` → downstream comparisons fail

---

### F. HIDDEN COUPLING & CONSISTENCY RISKS

**Other modules that implement related perpetuity/NPV math:**

#### 1. **[engine/net_interest_income.py:55](engine/net_interest_income.py#L55)** — `compute()`
- **NII math**: `nii_yr = max(0, beginning_cash) * rate; disc = nii_yr / (1 + wacc) ** yr`
- **Divergence**: Uses simple interest (not perpetuity); only covers positive cash positions
- **Risk if refactored**: If v1.6 changes how `wacc` flows (e.g., moves to `BenchmarkConfig` field), NII discount must update in lock-step
- **Inconsistency**: NII uses `wacc` as discount; TV uses `wacc - growth_rate` as denominator. Different semantics (not a bug, but coupling point).

#### 2. **[engine/outputs.py:166](engine/outputs.py#L166) — `_npv()` helper**
- **Formula**: `total += cash_flows[yr] / (1 + wacc) ** yr`
- **Divergence**: Assumes finite time horizon (YEARS=10); no perpetuity
- **Consistency**: Both use `wacc`; both exclude Y0 (index 0); both sum discounted flows
- **Risk**: If v1.6 refactors `_npv` and `_terminal_value` together, they must keep the same `(1+wacc)^yr` discount convention

#### 3. **[training/replicas/layer3_cash_flow.py](training/replicas/layer3_cash_flow.py)** — Replica `terminal_value()`
- **Equivalent formula** (from Layer 3 memory line 352): 
  ```
  TV[N] = (SQ[YN] - AZ[YN]) × (1+perp)/(WACC-perp) / (1+WACC)^N
  ```
  i.e., `savings[N] × (1+g)/(wacc-g) / (1+wacc)^N`
- **Exact match**: Engine's two calls at lines 223–225 compute this correctly
- **Risk if refactored**: Must remain byte-identical for Layer 3 parity (AC-7 is locked at MAX_ENGINE_DRIFT=0)

#### 4. **[engine/financial_case.py](engine/financial_case.py)** — Depreciation growth rate
- **Formula** (from depreciation.py line 103): `acq = annual_baseline_acq * (1 + growth_rate) ** year_offset`
- **Divergence**: `growth_rate` here = `inputs.hardware.expected_future_growth_rate` (different from `benchmarks.perpetual_growth_rate`)
- **Semantics**: Depreciation uses growth for CAPEX forecasting; terminal value uses perpetual growth for perpetuity
- **Consistency risk**: Two different `growth_rate` sources could be refactored to a single field. If merged naively, one path breaks.

#### 5. **[engine/fact_checker.py:471](engine/fact_checker.py#L471)** — External oracle binding
- **Cell**: `"project_npv_10yr": summary.npv_10yr_with_terminal_value`
- **Dependency**: If TV changes, oracle cell fails
- **Risk**: v1.6 refactor must ensure `fact_checker` audit still works

---

## TARGET 2: `engine/financial_case.py::effective_run = full_run * (1.0 + g) * (1.0 - acd)`

### A. CURRENT IMPLEMENTATION

**File**: [engine/financial_case.py](engine/financial_case.py#L240-L270)

**Function Context**: `_azure_consumption_by_year(inputs, benchmarks) -> list[float]`

```python
def _azure_consumption_by_year(
    inputs: BusinessCaseInputs,
    benchmarks: BenchmarkConfig,
) -> list[float]:
    """
    Sum Azure consumption across all workloads for each year.

    Formula (matching Excel 'Detailed Financial Case' sheet):
        consumption_y = avg_ramp_y × full_run_rate × (1 + g) × (1 − ACD)
    """
    g = inputs.hardware.expected_future_growth_rate
    result = [0.0] * (YEARS + 1)
    for cp in inputs.consumption_plans:
        full_run = (
            cp.annual_compute_consumption_lc_y10
            + cp.annual_storage_consumption_lc_y10
            + cp.annual_other_consumption_lc_y10
        )
        acd = cp.azure_consumption_discount
        effective_run = full_run * (1.0 + g) * (1.0 - acd)  # LINE 260
        for yr in range(1, YEARS + 1):
            ramp_this = cp.migration_ramp_pct[yr - 1]
            ramp_prev = cp.migration_ramp_pct[yr - 2] if yr > 1 else 0.0
            avg_ramp = (ramp_this + ramp_prev) / 2
            result[yr] += avg_ramp * effective_run
    return result
```

**Exact Line**: [financial_case.py:260](engine/financial_case.py#L260)

**Call Sites**:
1. [financial_case.py:304](engine/financial_case.py#L304) — `az_consumption = _azure_consumption_by_year(inputs, benchmarks)`
2. [financial_case.py:370](engine/financial_case.py#L370) — `fc.az_azure_consumption[yr] = az_consumption[yr]`

**Downstream Output Fields Dependent**:
- `fc.az_azure_consumption[yr]` — annual Azure cloud costs (11 values, Y0–Y10)
- `fc.az_total()[yr]` (line 172) — `az_total_retained_onprem[yr] + az_azure_consumption[yr] + ...` 
- `summary.total_az_10yr` — sum of annual Azure costs (T-statistic for case ROI)
- `summary.annual_savings[yr]` — `sq_total[yr] - az_total[yr]`
- `summary.npv_10yr`, `summary.npv_5yr` — all NPVs depend on savings
- `summary.roi_10yr`, `summary.roi_5yr` — all ROIs derived from NPVs
- `summary.terminal_value` — perpetuity computed from Y10 savings (which includes `az_consumption[10]`)
- `fc.cf_savings()` — cash flow savings used for 5Y CF ROI/payback (lines 310–311)
- **External audit**: `fact_checker.py` maps dozens of annual consumption cells to oracle

**Total impact**: Controls ~15–20% of total Azure case cost (rest is on-prem hardware, migration, retained costs).

---

### B. TRUST BOUNDARY INPUTS

**Where do `full_run`, `g`, and `acd` originate?**

#### `full_run` = sum of three annual consumption anchors
- **Components**:
  - `cp.annual_compute_consumption_lc_y10` — Azure Compute Y10 anchor cost
  - `cp.annual_storage_consumption_lc_y10` — Azure Storage Y10 anchor cost
  - `cp.annual_other_consumption_lc_y10` — Azure Other (bandwidth, etc.) Y10 anchor cost
- **Source path**:
  - **From RVTools path**: [rvtools_to_inputs.py](engine/rvtools_to_inputs.py) → `consumption_builder.build_with_validation()` → computes per-VM Azure SKU cost → sums to workload anchors
  - **From BA workbook path**: User hand-enters Y10 consumption figures (yellow cells in Consumption Plan sheets)
- **Editable in UI?** **YES** (Step 2: Consumption Plan page allows manual adjustment of Y10 anchors)
- **RVTools sourced?** **YES** (automatic path computes from inventory + Azure pricing API)
- **BA workbook sourced?** **YES** (manual override path allows BA to pre-compute or adjust)
- **Untrusted file risk**: **CRITICAL** — RVTools could report zero VMs or 100× memory (malformed export) → consumption balloons or shrinks

#### `g` = `inputs.hardware.expected_future_growth_rate`
- **Default**: [models.py:258](engine/models.py#L258) — `expected_future_growth_rate: float = 0.10` (10% annual growth)
- **Source**: Hardware config section (1-Client Variables sheet, column D)
- **Editable in UI?** **YES** (Step 1: Client Intake page allows override)
- **RVTools sourced?** **NO** (pure financial assumption)
- **BA workbook sourced?** **YES** (customer-specific; typical 5–15%)
- **Untrusted file risk**: YES — can be set arbitrarily high (e.g., 1.0 = 100% annual growth) or negative (deflation assumption)

#### `acd` = `cp.azure_consumption_discount`
- **Full name**: Azure Consumption Discount (ACD)
- **Range**: [models.py:309](engine/models.py#L309) — `0.0 ≤ acd ≤ 1.0` (0% discount = PAYG; 1.0 = free)
- **Semantics**: Flat discount off PAYG list prices (e.g., 0.15 = 15% off via CSP/EA/MCA agreement)
- **Default**: [models.py:310](engine/models.py#L310) — `0.0` (no discount, PAYG list price)
- **Source path**:
  - **From RVTools**: [rvtools_to_inputs.py](engine/rvtools_to_inputs.py) hardcodes `acd = 0.0` (PAYG assumption; no per-VM RI/SP tracking)
  - **From BA workbook**: User enters `acd` in Consumption Plan sheet (cell 2a!D11 or similar, per fact_checker.py)
- **Editable in UI?** **YES** (Step 2: Consumption Plan page, ACD input field)
- **BA workbook sourced?** **YES** (critical financial lever; changes consumption by %)
- **Untrusted file risk**: **EXTREME** — acd=1.0 makes Azure free; acd>1.0 (clamped by Pydantic) produces negative costs (rare but possible if validator broken)

**v1.7 planned change** (from [test_v17_ri_sp_blending_scaffold.py](tests/test_v17_ri_sp_blending_scaffold.py)):
- Current: Flat `acd` per consumption plan (one global discount)
- v1.7: Family-blended `effective_acd = paygo×0 + ri_1y×0.20 + ri_3y×0.36 + sp_1y×0.18 + sp_3y×0.30`
- **AC-3 documented** (line 33): exact weighted-average formula for v1.7
- **AC-4 critical** (line 37): Customer A already pre-computes blended ACD into `cp.azure_consumption_discount` → v1.7 must NOT regress Layer 3 parity regardless of flag value

---

### C. NUMERICAL EDGE CASES

#### **1. Negative effective_run (acd > 1.0)**
- **Trigger**: `acd = 1.1` (exceeds max; possible if Pydantic validator bypassed or data file hand-edited)
- **Formula**: `full_run × (1.0 + 0.10) × (1.0 - 1.1) = full_run × 1.1 × (-0.1) = full_run × (-0.11)`
- **Impact**: `az_azure_consumption[yr] = avg_ramp[yr] × (negative)` → negative Azure cost (subsidy!)
- **Downstream**: `az_total[yr]` shrinks; savings grows; ROI/NPV inflate artificially
- **Pydantic guard**: [models.py:309](engine/models.py#L309) has `le=1.0` validator, should reject `acd > 1.0` at input
- **Risk**: If validator disabled or data smuggled around it (e.g., direct dict construction), negative costs propagate silently

#### **2. Division-by-zero analog (acd = 1.0 exactly)**
- **Trigger**: `acd = 1.0` (100% discount = free Azure)
- **Formula**: `effective_run = full_run × (1.0 + g) × (1.0 - 1.0) = full_run × (1.0 + g) × 0 = 0`
- **Impact**: **Azure consumption becomes zero for all years**
- **Downstream**: `az_total[yr]` = only retained on-prem costs (no cloud spend); savings = very large
- **Business logic**: 100% discounted Azure is free tier (valid scenario, e.g., pilot/POC programs)
- **Silent risk**: UI should prominently warn when `acd = 1.0` (not a silent zero, but extreme case)

#### **3. Order-of-operations: (1+g) × (1-acd) vs (1+g-acd)**
- **Current formula**: `full_run × (1.0 + g) × (1.0 - acd)` → applies growth THEN discount
- **Alternative semantics**: `full_run × (1.0 + g - acd)` → would apply growth and discount additively (wrong!)
- **Example**: `full_run = $1M`, `g = 0.10`, `acd = 0.15`:
  - **Correct (current)**: $1M × 1.10 × 0.85 = $935K
  - **Wrong**: $1M × (1.10 - 0.15) = $950K (discount erased by growth, not good)
- **No ordering risk** in current code; formula is explicit with parentheses

#### **4. Silent zero from multiplicative chain**
- **Scenario 1**: `full_run = 0` (no consumption anchors entered) → effective_run = 0 ✓ correct
- **Scenario 2**: `g = -1.0` (negative growth, extreme deflation assumption)
  - `effective_run = full_run × (1.0 + (-1.0)) × (1.0 - acd) = full_run × 0 × (1.0 - acd) = 0`
  - **Silent zero**: Consumption flattened despite positive `acd`
- **Scenario 3**: `acd = 0`, `g = 0` → effective_run = full_run × 1.0 × 1.0 = full_run ✓ baseline

#### **5. Extreme growth rate**
- **Trigger**: `g = 2.0` (200% annual growth — unrealistic but unconstrained)
- **Formula**: `effective_run = full_run × 3.0 × (1.0 - acd)`
- **Impact**: Consumption anchors 3× larger → Azure costs 3× larger → savings/ROI shrink dramatically
- **No guard**: Field is `float` with default 0.10; no `le=` constraint on `HardwareLifecycle.expected_future_growth_rate`
- **Risk**: Adversarial input could be 10.0 (1000% growth) → effective_run = full_run × 11 × (1-acd) — plausible but nonsensical

#### **6. Negative growth (deflation assumption)**
- **Trigger**: `g = -0.05` (prices decline 5% annually)
- **Formula**: `effective_run = full_run × 0.95 × (1.0 - acd)` ✓ works fine
- **No special handling**: Formula treats negative `g` same as positive (correct)
- **No risk**: Deflation is a valid financial assumption

#### **7. Floating-point precision (ramp × effective_run)**
- **Chain**: `avg_ramp × effective_run`, where:
  - `avg_ramp` = (ramp_this + ramp_prev) / 2 → can be 0.5, 0.25, etc. (fractional)
  - `effective_run` = large number (e.g., $10M × 1.1 × 0.85 = $9.35M)
- **Result**: `0.5 × $9.35M = $4.675M` ← no precision loss at typical scales
- **Risk**: If `full_run = 1e15` (petabyte pricing), `effective_run = 3.3e15` → near float64 limits

#### **8. Migration ramp boundary (avg_ramp at Y1)**
- **Trigger**: Y1 has `ramp_pct[0]` = 0.4 (40% migrated by EOY1), `ramp_pct[-1]` = 0.0
- **Average**: `(0.4 + 0) / 2 = 0.2` (20% average through Y1)
- **Result**: Y1 consumption = 0.2 × effective_run (quarter of full rate)
- **No issue**: Half-year convention is correct per BA semantics

#### **9. Sign flip via negative g and acd interaction**
- **Scenario**: `g = -2.0` (prices fall 200% — nonsense), `acd = 0.5` (50% discount)
- **Formula**: `effective_run = full_run × (1 - 2.0) × (1 - 0.5) = full_run × (-1.0) × 0.5 = full_run × (-0.5)`
- **Result**: Negative consumption (subsidy)
- **Root cause**: Unconstrained `g` in financial model (no lower bound)

---

### D. REFACTOR RISKS FOR v1.7

**Planned Change** (from [test_v17_ri_sp_blending_scaffold.py](tests/test_v17_ri_sp_blending_scaffold.py#L23-L37)):
- v1.7 will add `use_ri_sp_blending: bool` flag (default False)
- v1.7 will compute family-blended `effective_acd` from per-family discount mix (paygo/ri_1y/ri_3y/sp_1y/sp_3y %)
- **AC-4 locked**: Layer 3 parity must stay at zero drift (Customer A pre-computes blended ACD into flat field)

**Five Highest-Likelihood Refactor Breaks**:

| Risk # | Break Scenario | Root Cause | Affected Output | Regression Test |
|--------|---|---|---|---|
| **R6** | `use_ri_sp_blending=True` shipped as default | PR author sets `default=True` for new opt-in flag | Every customer's Azure costs recalculated with blended ACD instead of flat | `test_ac1_blending_flag_default_off()` — assert `BenchmarkConfig().use_ri_sp_blending is False` |
| **R7** | Blended ACD multiplier wrong formula | Refactors weighted-average but mixes up weights (e.g., `ri_1y×0.36` instead of `ri_3y×0.36`) | Consumption drifts 1–5% depending on customer's RI/SP mix; Customer A stays zero (pre-computed) but other customers break | `test_ac3_blended_acd_matches_documented_weighted_average()` — hard-code the 5 discount rates, verify formula |
| **R8** | Per-VM RI/SP allocation Y1 upfront bifurcation added | PR accidentally includes CF vs P&L split (deferred to v2.0) | Creates two new fields (`azure_ri_upfront_y1`, `azure_ri_amortization_by_year`) violating 'pure OPEX' invariant | `test_ac5_no_cf_pl_split_in_v17()` — assert no such fields exist on `BusinessCaseSummary` or `FinancialCase` |
| **R9** | Blended ACD applied in wrong place | Moves calculation from `_azure_consumption_by_year()` to `consumption_builder.py` or `models.py`; breaks consumption plan contract | Flat `cp.azure_consumption_discount` field is bypassed; old BA workbooks with pre-computed blending silently compute wrong | `test_ac2_flag_off_preserves_legacy_payg()` — run Customer A with flag=False; `az_total()` must be byte-identical to HEAD |
| **R10** | Growth rate `g` applied twice (before + after blending) | Blended ACD computation mixes in `g` again; formula becomes `full_run × (1+g)^2 × (1 - effective_acd)` | Y10 consumption inflates; all downstream ROI/NPV underestimate Azure costs | `test_ac4_layer3_drift_unchanged()` — Layer 3 ratchet test; MAX_ENGINE_DRIFT must stay 0 for Customer A |

**Acceptance Criteria** (from scaffold):
- AC-1: Flag exists, defaults to False ✓
- AC-2: Flag OFF → byte-identical `az_total()` to pre-v1.7 ← **CRITICAL**
- AC-3: Flag ON → uses documented blended formula (5 rates from BA D163-D166)
- AC-4: **Layer 3 parity stays zero** regardless of flag (Customer A locks the guarantee)
- AC-5: NO Y1 upfront CF split in v1.7 (deferred to v2.0)

---

### E. ADVERSARIAL PROMPT-INJECTION VECTORS

**Worst-case inputs a malicious BA workbook or RVTools file could inject:**

1. **Set `acd = 1.0 + epsilon` (e.g., 1.001)**
   - **Input path**: Hand-edit BA Consumption Plan cell to 1.001 (Pydantic validator might allow rounding errors)
   - **Formula**: `effective_run = full_run × (1+g) × (1.0 - 1.001) = full_run × (1+g) × (-0.001)`
   - **Impact**: Azure costs become micro-subsidies; savings inflate; ROI shoots to +500%
   - **Attacker motive**: Fraudulently boost ROI for regulatory sign-off or investor pitch
   - **Mitigation**: UI should enforce strict `acd ≤ 1.0` with margin (e.g., `≤ 0.99`)

2. **Set `g = 10.0` (1000% annual growth)**
   - **Input path**: Hand-enter in Client Intake (no upper bound on `expected_future_growth_rate`)
   - **Formula**: `effective_run = full_run × 11.0 × (1.0 - acd)` → 11× larger than baseline
   - **Impact**: Azure consumption forecast wildly inflates; customer sees $100M/yr cost → kills deal
   - **Attacker motive**: Sink a competitor's Azure bid by making their costs look terrible
   - **Mitigation**: Add `le=0.50` constraint to field (50% max growth, typical for tech assumptions)

3. **Set `g = -1.0` (100% annual deflation)**
   - **Input path**: Hand-enter negative value (no lower bound)
   - **Formula**: `effective_run = full_run × 0.0 × (1.0 - acd) = 0` → **zero consumption for all years**
   - **Impact**: Azure costs vanish; Azure appears free; on-prem TCO becomes 100% (ROI = -100%)
   - **Attacker motive**: Hide true Azure costs (inverse of attack #2)
   - **Mitigation**: Validate `0.0 ≤ g ≤ 0.50` (no deflation, max 50% growth)

4. **Set `full_run = 0` (no consumption anchors)**
   - **Input path**: RVTools export with zero VMs, or user hand-enters $0 in Consumption Plan
   - **Formula**: `effective_run = 0.0` → all Azure annual costs = 0
   - **Impact**: Azure case appears costless; on-prem baseline absorbs all opex; ROI = -∞ (migration loses money despite free cloud)
   - **Adversarial**: Less intentional, more of a validation oversight
   - **Mitigation**: UI should warn if `full_run = 0` for non-pilot scenarios

5. **Set `ramp_pct` to all-zeros** (migration ramp completely flat at 0)
   - **Input path**: Hand-edit Consumption Plan migration ramp to [0, 0, ..., 0]
   - **Formula**: `avg_ramp[yr] = (0 + 0) / 2 = 0` → `result[yr] = 0 × effective_run = 0`
   - **Impact**: Azure consumption never happens (infinite pilot period); on-prem costs continue forever
   - **Attacker motive**: Show customer won't migrate; Azure bid stays on shelf
   - **Mitigation**: Validate ramp reaches >= 0.95 by Y10 (full migration)

6. **Set `acd = 0` but configure blended-RI mix to 100% RI-3y in v1.7**
   - **Input path**: Two fields conflict (flat ACD = 0; blended mix = [0, 0, 1.0, 0, 0])
   - **Bug**: If v1.7 refactor uses blended when flag=ON but checks flat ACD first
   - **Formula**: Could compute two different effective_acd values → inconsistent costs
   - **Impact**: Consumption calculated twice; wrong value used; downstream drift
   - **Mitigation**: v1.7 must have clear precedence: if `use_ri_sp_blending=True`, compute blended; else use flat `acd`

---

### F. HIDDEN COUPLING & CONSISTENCY RISKS

**Other modules that depend on or re-implement similar discount/growth logic:**

#### 1. **[engine/consumption_builder.py](engine/consumption_builder.py)** — Per-VM Azure consumption build
- **Equivalent math**: Builds per-VM consumption plans, sums to `annual_compute_consumption_lc_y10` etc.
- **Divergence**: Returns PAYG list price (line 24: "ACD applied in Step 2 of financial model, not here")
- **Growth rate**: NOT applied in consumption_builder; pure RVTools-to-SKU pricing
- **Risk if refactored**: If v1.6/v1.7 moves growth/discount into consumption_builder, it double-applies
- **Consistency**: financial_case.py must be sole place where `(1+g) × (1-acd)` applies to consumption anchors

#### 2. **[engine/retained_costs.py:78](engine/retained_costs.py#L78)** — Retained on-prem costs post-migration
- **Similar pattern**: `baseline × (1 - eoy_ramp[t])` (no growth in retained OPEX; hardware maintenance only)
- **Divergence**: Retained costs DON'T grow; only migrated costs do
- **Risk**: If v1.7 accidentally applies growth to *retained* consumption too, those should stay flat
- **Consistency check**: `az_system_admin[yr] = sq_system_admin[yr] × (1 - eoy_ramp[t])` (line 191 comment: "with NO growth")

#### 3. **[engine/depreciation.py:103](engine/depreciation.py#L103)** — Hardware depreciation schedule
- **Similar logic**: Forward acquisition grows with `(1 + growth_rate) ** year_offset`
- **Growth rate source**: Same as consumption (`inputs.hardware.expected_future_growth_rate`)
- **Consistency**: Both `effective_run` and depreciation forward schedule use the same `g`
- **Risk**: If v1.6/v1.7 refactors `g` field location, both must be updated in lock-step or hardware/consumption drift apart

#### 4. **[engine/outputs.py:166](engine/outputs.py#L166)** — NPV discount factor
- **Formula**: `cash_flows[yr] / (1 + wacc) ** yr`
- **Relationship**: Uses `wacc` (different from `g`)
- **Consistency**: If v1.7 introduces family-blended `effective_acd`, the ACD factor is applied BEFORE NPV (in consumption); NPV discounting unchanged
- **Risk**: If someone mistakenly tries to blend `effective_acd` into NPV discount rate, formula breaks

#### 5. **[engine/fact_checker.py](engine/fact_checker.py)** — External workbook audit
- **Audit cells**: Maps >100 cells including annual consumption Y1..Y10
- **Risk if refactored**: 
  - If v1.7 renames or restructures how `az_azure_consumption[yr]` is computed, fact_checker cell addresses may not match BA workbook layout
  - BA pre-computes blended ACD into consumption cells; v1.7 must not change that for Customer A (AC-4)
- **Consistency**: fact_checker must validate that blended vs flat ACD produce identical results for locked customers

#### 6. **[training/replicas/layer3_azure_case.py](training/replicas/layer3_azure_case.py)** — Layer 3 replica
- **Equivalent formula** (from repo memory line 337): `_az_consumption_series() → Y10_anchor × avg(eoy[t-1], eoy[t]) × (1+g)`
- **Memory note**: "Y10 anchor INCLUDES `(1 + Client Variables!D26)` uplift baked in"
- **Discrepancy**: Replica includes uplift in anchor; engine treats uplift via `g` applied to base anchor
- **Risk if refactored**: If v1.7 changes how anchors are sourced, replica formula must stay in sync or layer 3 audit fails

#### 7. **[scripts/_probe_layer3_cf.py](scripts/_probe_layer3_cf.py)** (gitignored, debug only)
- **Ad-hoc testing**: Probes consumption formula; not in CI
- **Risk**: v1.7 refactor should update this for continuity of debugging

---

## SUMMARY TABLE: REFACTOR DANGER ZONES

| Category | Function 1: `_terminal_value` | Function 2: `effective_run` |
|---|---|---|
| **Guard condition** | `if wacc <= growth_rate: return 0.0` | Pydantic validator `0.0 ≤ acd ≤ 1.0` |
| **Silent failure mode** | Returns 0 when perpetuity undefined (g ≥ w) | Returns 0 when acd=1.0 (free Azure); returns negative when acd>1.0 |
| **Negative output risk** | HIGH: savings[10] < 0 → TV < 0 (sign flip) | HIGH: acd > 1.0 → effective_run < 0 (subsidy) |
| **Coupling points** | Uses `wacc` (also in NII); uses `growth_rate` (also in depreciation) | Uses `g` (also in depreciation); uses `acd` (v1.7 will replace with blended) |
| **Layer 3 audit exposure** | CRITICAL: terminal_value field locked in oracle (AC-7 zero drift) | CRITICAL: every az_azure_consumption[yr] cell audited; 11 cells × 3 workloads = 33 audit bindings |
| **v1.6/v1.7 breaking changes** | tv_method enum (default="gordon") + tv_floor_at_zero flag | use_ri_sp_blending flag (default=False) + effective_acd formula |
| **Highest refactor risk** | R1: Default tv_method != "gordon" (back-compat break) | R6: use_ri_sp_blending=True shipped as default (silent cost shift) |
| **Mitigation priority** | Scaffold tests (AC-1 through AC-7) already pre-built; scaffold has all guards | Scaffold tests (AC-1 through AC-5) pre-built; Layer 3 ratchet test (AC-4) is hard CI blocker |

---

## RECOMMENDATIONS

### Immediate (Pre-v1.6 PR):
1. **Field validation**: Add `le=0.50` constraint to `HardwareLifecycle.expected_future_growth_rate` (cap at 50% annual growth)
2. **Enum guard**: Ensure scaffold tests AC-1 through AC-7 are in CI; any PR touching `_terminal_value` must pass all 7
3. **Documentation**: Add docstring to `_terminal_value` explaining guard condition and negative-output risk for unprofitable cases

### Pre-v1.7 PR:
1. **Default safety**: `use_ri_sp_blending` MUST default to False; scaffold test AC-1 in CI
2. **Formula lock**: Hard-code blended ACD rates (0.20/0.36/0.18/0.30) in test (AC-3); any change requires BA approval
3. **Customer A guarantee**: Layer 3 ratchet test must pass before merge (AC-4 = zero drift with Customer A)
4. **No CF split**: Test AC-5 explicitly forbids new fields (`azure_ri_upfront_y1`, etc.); v2.0 only

### General:
1. **Adversarial input testing**: Add tests for acd > 1.0 (even if Pydantic rejects it), g < 0, g > 1.0, wacc = growth_rate
2. **Layer 3 audit**: Maintain full audit coverage for every output field that depends on these functions; any cell shift requires workbook re-baseline
3. **Coupling inventory**: Document the two-layer dependency (this function → financial_case → outputs → fact_checker) for any future refactors

---

## CONCLUSION

Both functions are critical financial levers with high adversarial surface:
- **`_terminal_value`**: Silent-zero risk when perpetuity undefined (wacc ≤ growth_rate); negative TV on unprofitable cases (Customer A example); refactor safety depends on scaffold tests locking default to "gordon" method.
- **`effective_run`**: Multi-stage multiplication chain (growth × discount) creates sign-flip and magnitude risks; v1.7 blending refactor must maintain Layer 3 byte-parity for Customer A or trust breaks.

Both are **locked at zero drift** for Customer A (AC-7 and AC-4 respectively). Any PR touching these functions **must run Layer 3 ratchet and pass all scaffold tests**, or the business case trust contract is violated.