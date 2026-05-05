# Step 15 — Customer B Onboarding (ECIF) — REVISED (Step 15.1)

**Status:** ✅ Complete — both customers at 395/395 on replica AND engine
**Tag candidate:** `v1.5.1-customer-b-zero-drift`

## Scope
Onboard Customer B (`customer_b_BV_Benchmark_Business_Case_v6.xlsm`) which exercises **Microsoft funding (ECIF)** subsidies that Customer A had at zero. Customer B has $-1,050,000 ECIF in each of Y1, Y2, and Y3 (entered as per-year cells `2a-Consumption Plan Wk1!E22:G22`).

## BA workbook canonical structure (verified by formula inspection)

### Per-year cells are authoritative

| Sheet | Cell(s) | Meaning |
|---|---|---|
| `2a-Consumption Plan Wk1` | `E21:N21` | Per-year ACO (Y1..Y10) |
| `2a-Consumption Plan Wk1` | `E22:N22` | Per-year ECIF (Y1..Y10) |
| `2a-Consumption Plan Wk1` | `D21` | `=SUM(E21:N21)` — derived from per-year |
| `2a-Consumption Plan Wk1` | `D22` | `=SUM(E22:N22)` — derived from per-year |
| `2a-Consumption Plan Wk1` | `E23:N23` | `=SUM(E21:E22)` etc — per-year MS funding total |

### Detailed Financial Case — SUMIF tag buckets are mutually exclusive

The `Detailed Financial Case` sheet rows 54..75 carry an internal tag column (column AH) that classifies each P&L line item into one of five mutually-exclusive categories:

| Tag | Rows | Meaning |
|---|---|---|
| `CAPEX` | 54..56 | Capitalised costs (depreciation tracks elsewhere) |
| `OPEX` | 60..69 | Recurring operating expenses (DC, licenses, IT admin, …) |
| `Azure Costs` | 70 | Azure consumption (PAYG + Reservations) |
| `Migration Costs` | 71 | NET migration (gross migration spend offset by Microsoft Investments) |
| `Microsoft Investments` | 72 | Funding alone (ACO + ECIF), reported as a separate line item |

Each row is tagged exactly once, so the `SUMIF` aggregations on `Summary Financial Case` rows 21..26 sum **disjoint** values:

```
Summary!Q21 = SUMIF(DFC!AH:AH, "CAPEX",                 DFC!Q:Q)
Summary!Q22 = SUMIF(DFC!AH:AH, "OPEX",                  DFC!Q:Q)
Summary!Q23 = SUMIF(DFC!AH:AH, "Azure Costs",           DFC!Q:Q)
Summary!Q24 = SUMIF(DFC!AH:AH, "Migration Costs",       DFC!Q:Q)
Summary!Q25 = SUMIF(DFC!AH:AH, "Microsoft Investments", DFC!Q:Q)
Summary!Q26 = Q21 + Q22 + Q23 + Q24 + Q25
```

**Migration Costs and Microsoft Investments are independent line items.** The "Migration Costs" bucket (row 71) holds the *net* outflow for migration services; the "Microsoft Investments" bucket (row 72) holds the *funding* line. They are tagged differently and SUM'd to row 26 once each. **There is no double-counting in the BA template** — every value flows through a unique row and a unique tag.

The earlier Step 15 doc framed row 73's `=SUM(Q60:Q72)` as a "double-count of funding". That framing was wrong. Row 73 sums OPEX + Azure + Migration + Microsoft Investments where each underlying value is unique and tagged exclusively. The replica and engine match these unique values verbatim.

## Bug enumeration and fixes

### Step 15 (initial commit `cf2d734`)

| # | Layer | File | Fix |
|---|---|---|---|
| 1 | Schema | `training/replicas/layer3_inputs.py` | Added `aco_by_year`, `ecif_by_year` tuples (length 10) to `InputsConsumption` |
| 2 | Loader | `load_consumption_inputs` | Reads `E21:N21` and `E22:N22` per-year cells (None → 0.0) |
| 3 | Replica | `_az_migration_series` | Returns NET migration `gross + funding` per year (matches BA row 71) |
| 4 | Replica | `_az_ms_funding_series` | Returns per-year funding (matches BA row 72) — was hard-coded zeros |
| 5 | Bridge | `engine_bridge_l3.py` | Passes per-year arrays to engine ConsumptionPlan (was lump-sum to Y1) |
| 6 | Engine | `_migration_costs_by_year` | Returns `(net_list, funding_list)` tuple split |
| 7 | Engine | `compute()` | Populates BOTH `fc.az_migration_costs` and `fc.az_microsoft_funding` separately |

### Step 15.1 (this commit — drives both paths to 395/395)

After re-engaging the adversarial judge, three additional bugs were uncovered. Each was masked by Customer A's specific input pattern.

| # | Layer | File | Bug | Fix |
|---|---|---|---|---|
| 8 | Replica | `training/replicas/layer3_azure_case.py` | `_az_dc_or_bandwidth` used single-factor `(1 - eoy_ramp[t-1])` decay. BA's `Retained Costs Estimation` rows 287/293/295 actually use a CHAINED product `Π_{k=1..t-1} (1 - eoy_ramp[k])`. Customer A's `[0.5, 1.0, ...]` ramp collapses the chain to one factor (hiding the bug); Customer B's `[0.33, 0.66, 1.0, ...]` exposes it. | Replaced single-factor with multiplicative chain. |
| 9 | Engine | `engine/retained_costs.py` | Same single-factor vs. chained-product mismatch in the Proportional DC-exit branch. Affected `dc_lease_space`, `dc_power`, `bandwidth` retained costs in Y3 for multi-step ramps. | Replaced `dc_fraction = lagged_fraction` with cumulative product `Π_{k=1..yr-1} (1 - _combined_ramp(plans, k))`. Static-exit branch unchanged. |
| 10 | Engine model | `engine/models.py` + `training/replicas/engine_bridge_l3.py` | `est_physical_servers_incl_hosts` and `est_allocated_pcores_incl_hosts` derived from `num_vms / ratio + excl_hosts_residual`. The bridge clamped the residual to ≥0, which lost BA's hand-typed D42/D47 totals when those values were SMALLER than what the engine derived (Customer B: D42=65 vs derived 4534/12=377.83; D47=4004 vs derived 19816/4.9=4044). | Added `est_physical_servers_incl_hosts_override` and `est_allocated_pcores_incl_hosts_override` optional fields on `WorkloadInventory`. When set, the corresponding `@property` returns the override verbatim. Bridge populates both from `client.nb_physical_servers` (D42) and `client.allocated_pcores` (D47). |
| 11 | Engine | `engine/outputs.py` | `compute_cf_roi_and_payback` reported a fractional payback in `[0, 1)` when Y1 cumulative discounted savings already covered the investment. BA's `5Y CF with Payback!I32 = SUM(C47:G47)` only fills a payback value when cumulative crosses the investment threshold *between* observed years (C46→D46, D46→E46, …). Y1-already-covers means BA's I32 stays at 0 (sentinel for "less than one year"). | Require `prev_cum < investment_npv` AND `cum_yr ≥ investment_npv` AND `yr ≥ 2` for the engine to report a non-zero payback. |

## Customer B audit results

| Stage | Commit | Replica fails | Engine fails |
|---|---|---:|---:|
| Pre-Step 15 | `a828866` | 30 / 395 | 105 / 395 |
| Step 15 mid-state | `cf2d734` | 10 / 395 (97.5%) | 89 / 395 (77.5%) |
| **Step 15.1 (this commit)** | — | **0 / 395 (100%)** ✅ | **0 / 395 (100%)** ✅ |

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

- ✅ All 35 Layer-3 parity tests pass (zero regression).
- ✅ Customer A still at 395/395 on both replica and engine paths.
- ✅ `MAX_ENGINE_DRIFT = 0` invariant (locked at `v1.5.0-layer3-zero-drift`) preserved.

For Customer A, the new override fields receive D42=280 and D47=11040. D42=280 exactly matches what the additive residual path produces (`2831/12 + 44.0833 = 280`), so `est_physical_servers_incl_hosts` is unchanged. D47=11040 differs by 1.27 cores (0.012%) from what the additive path produces (`15330/1.97 + 3257 = 11038.73`); the override aligns the engine *more closely* with BA's hand-typed value (well within audit tolerance — Customer A still passes 395/395). The chained-product retained-cost decay collapses to the single-factor formula on Customer A's `[0.5, 1.0, ...]` ramp pattern. The payback logic still finds Y1 coverage at the 0 sentinel for Customer A's project.

## Test coverage

`tests/test_layer3_parity.py` — 35 tests, all passing. Ratchets:
- `MAX_REPLICA_DRIFT_CUSTOMER_B = 0` (was 10 in Step 15)
- `MAX_ENGINE_DRIFT_CUSTOMER_B = 0` (was 89 in Step 15)
- `MAX_ENGINE_DRIFT = 0` (Customer A — unchanged from `v1.5.0`)

The `ECIF_REPLICA_REQUIRED_CELLS` 8-cell zero-tolerance contract continues to hold.

## Adversarial audit summary

User-as-judge rejected the Step 15 framing of "double-counting funding" and the partial 97.5%/77.5% accuracy. Re-investigation confirmed:

1. **The "double-counting" framing was wrong.** Row 73 sums mutually-exclusive SUMIF tag buckets — every value is unique and tagged once.
2. **The replica's 10 residual fails were not "out of scope" — they were a single-root-cause bug.** Customer A's ramp pattern coincidentally satisfied a single-factor formula; Customer B's multi-step ramp exposed the chained-product requirement.
3. **The engine's 89 residual fails were three distinct bugs** (chained decay, D42/D47 override clamp, payback semantics). All three were Customer-A-pattern-bias artifacts.

After all four fixes (replica retained-decay, engine retained-decay, override fields, payback semantics) both customers reach full 395/395 parity. No "out of scope" remainder.

## Out of scope

None. Both customers reach full 395/395 parity on both paths. The only remaining test-suite failures are 10 unrelated tests in `tests/test_engine.py` (RVTools file fixtures and storage-builder edge cases) that pre-exist on `main` and do not touch Layer-3 financial computations.
