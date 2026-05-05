# Step 15 — Customer B Onboarding (ECIF)

**Status:** ✅ Complete
**Tag candidate:** `v1.5.1-customer-b-ecif`

## Scope
Onboard Customer B (`customer_b_BV_Benchmark_Business_Case_v6.xlsm`) which exercises **Microsoft funding (ECIF)** subsidies that Customer A had at zero. Customer B has $-1,050,000 ECIF in each of Y1, Y2, and Y3 (entered as per-year cells `2a-Consumption Plan Wk1!E22:G22`).

## BA workbook canonical structure (verified by formula inspection)

### Per-year cells are authoritative

| Sheet | Cell(s) | Meaning |
|---|---|---|
| `2a-Consumption Plan Wk1` | `E21:N21` | Per-year ACO (Y1..Y10) |
| `2a-Consumption Plan Wk1` | `E22:N22` | Per-year ECIF (Y1..Y10) |
| `2a-Consumption Plan Wk1` | `D21` | `=SUM(E21:N21)` — **derived from per-year** |
| `2a-Consumption Plan Wk1` | `D22` | `=SUM(E22:N22)` — **derived from per-year** |
| `2a-Consumption Plan Wk1` | `E23:N23` | `=SUM(E21:E22)` etc — per-year MS funding total |

### Detailed Financial Case (the canonical financial computation)

| Row | Label | Formula | Meaning |
|---|---|---|---|
| 46 | Azure Migration Costs | `=SUM('2a'!E20, …) + Q47` | **NET migration** (gross + funding) |
| 47 | Microsoft Investments | `=SUM('2a'!E23, …)` | Funding alone (ACO + ECIF) |
| 71 | Azure Migration Costs (tagged `Migration Costs`) | `=Q46` | NET (gross + funding) |
| 72 | Microsoft Investments (tagged `Microsoft Investments`) | `=Q47` | Funding alone |
| 73 | Total Operating Cash Flows | `=SUM(Q60:Q72)` | **Sums BOTH row 71 (NET) AND row 72 (funding)** |

**⚠ Critical BA template peculiarity:** Row 73 sums both Q71 (which already includes funding via Q47) AND Q72 (which is funding alone). This means **funding is counted TWICE** in the BA's Total Operating Cash Flow. For Customer A funding=0 this is harmless. For Customer B this reduces total cash flow by `2 × funding`.

Per user directive, the BA workbook is the canonical truth. The engine and replica must mirror this double-count.

## Bug enumeration and fixes

| # | Layer | File | Fix | Status |
|---|---|---|---|---|
| 1 | Schema | `training/replicas/layer3_inputs.py` | Added `aco_by_year`, `ecif_by_year` tuples (length 10) to `InputsConsumption` | ✅ |
| 2 | Loader | `load_consumption_inputs` | Reads `E21:N21` and `E22:N22` per-year cells (None → 0.0) | ✅ |
| 3 | Replica | `_az_migration_series` | Returns NET migration `gross + funding` per year (matches BA row 71) | ✅ |
| 4 | Replica | `_az_ms_funding_series` | Returns per-year funding (matches BA row 72) — was hard-coded zeros | ✅ |
| 5 | Bridge | `engine_bridge_l3.py` | Passes per-year arrays to engine ConsumptionPlan (was lump-sum to Y1) | ✅ |
| 6 | Engine | `_migration_costs_by_year` | Returns `(net_list, funding_list)` tuple split | ✅ |
| 7 | Engine | `compute()` | Populates BOTH `fc.az_migration_costs` and `fc.az_microsoft_funding` separately | ✅ |
| 8 | Replica | `layer3_project_npv.py` | `migration_npv = -SUM(NET + funding)` (mirrors BA double-count) | ✅ |
| 9 | Bridge | `engine_bridge_l3.py:five_payback` | Same double-count for `migration_npv` and `total_costs_npv` | ✅ |

## Customer B audit results

### Pre-Step 15 (from initial empirical run on commit `a828866`)
- Replica: 30 fails / 395
- Engine: 105 fails / 395

### Post-Step 15
- **Replica: 10 fails / 395 (97.5% pass)**
  - All 10 are bucket-(a) `AZ OPEX.Y3` cascade — single root-cause bug in retained-OPEX modeling that Customer A's "fully migrated by Y2" pattern masked.
  - Affected cells: AZ OPEX.Y3, AZ Total CF.Y3, Savings.Y3, CF Delta.Y3, CF Rate.Y3, project_npv_excl_tv_5y/10y, net_benefits_npv, roi_5y_cf.
  - **NOT ECIF-related. Out of scope for Step 15. Tracked as bucket-(a) for subsequent steps.**

- **Engine: 89 fails / 395 (77.5% pass)**
  - 75 bucket-(a) `status_quo.*` cells (Server Depreciation, Network HW Maintenance, NW+Fitout Depreciation, DC Power, Server HW Maintenance) — pre-existing engine drift surfaced by Customer B's different sizing.
  - 4 bucket-(a) `sq_estimation.*` Y0 baselines.
  - 10 derived from cash_flow.Savings + CF Delta cascade.
  - **NOT ECIF-related.**

### ECIF contract: ZERO drift on these cells

| Cell | BA value | Engine | Replica |
|---|---:|:---:|:---:|
| `cash_flow.AZ MS Funding.Y1` | -$1,050,000 | ✅ | ✅ |
| `cash_flow.AZ MS Funding.Y2` | -$1,050,000 | ✅ | ✅ |
| `cash_flow.AZ MS Funding.Y3` | -$1,050,000 | ✅ | ✅ |
| `cash_flow.AZ Migration.Y1` | $1,194,330 | ✅ | ✅ |
| `cash_flow.AZ Migration.Y2` | $1,194,330 | ✅ | ✅ |
| `cash_flow.AZ Migration.Y3` | $1,262,340 | ✅ | ✅ |
| `five_payback.migration_npv` | -$501,000 | ✅ | ✅ |
| `five_payback.total_costs_npv` | -$14,554,212 | ✅ | ✅ |

## Customer A regression check

- ✅ All 29 pre-existing Layer-3 parity tests still pass (zero regression).
- ✅ MAX_ENGINE_DRIFT for Customer A remains 0.
- ✅ Replica clean.
- For Customer A, `aco_by_year` and `ecif_by_year` are all zeros (per-year cells blank in workbook), so all changes are no-ops. Bit-for-bit identical results.

## Test coverage added (6 new tests)

| Test | Purpose |
|---|---|
| `test_customer_b_extractor_pulls_395_cells` | Same shape as Customer A. |
| `test_customer_b_ecif_replica_cells_pass` | Step 15 contract: 8 ECIF cells must pass on replica. |
| `test_customer_b_ecif_engine_cells_pass` | Step 15 contract: 8 ECIF cells must pass on engine bridge. |
| `test_customer_b_engine_bridge_covers_all_oracle_cells` | All 395 keys populated. |
| `test_customer_b_replica_drift_under_ratchet` | One-way ratchet `MAX_REPLICA_DRIFT_CUSTOMER_B = 10`. |
| `test_customer_b_engine_drift_under_ratchet` | One-way ratchet `MAX_ENGINE_DRIFT_CUSTOMER_B = 89`. |

## Adversarial audit summary

Per user directive, Explore subagent dispatched before any code change. Verdict: GREEN to proceed with all 7 fixes, with two caveats:
1. (YELLOW) Existing assertion `tests/test_engine.py:255` on `fc.az_migration_costs[4]` — verified safe (Contoso fully migrates by Y3, no funding, value unchanged).
2. (YELLOW) Recommend separate test class for Customer B with its own ratchet — implemented.

No RED items. Audit findings persisted in `/memories/session/step-15-customer-b-ecif-diagnosis.md`.

## Out of scope (next steps)

| Bucket | Description | Cell count | Owner |
|---|---|---:|---|
| (a) | `AZ OPEX.Y3` retained-OPEX cascade — single root-cause masked by Customer A | 10 replica | Step 16 |
| (a) | Status quo depreciation/maintenance/DC-power drift surfaced by Customer B sizing | 75 engine | Step 16 |
| (a) | Status quo Y0 baseline drift | 4 engine | Step 16 |

These are tracked under their respective ratchets (`MAX_REPLICA_DRIFT_CUSTOMER_B = 10`, `MAX_ENGINE_DRIFT_CUSTOMER_B = 89`) which can only DECREASE over time.
