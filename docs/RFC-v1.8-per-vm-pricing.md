# RFC — v1.8: Per-VM Azure Retail Price API in the Engine

**Status:** Draft v4.1 — patches v4 with judge's 3 blocking corrections
(Linux-filter strictness, error-spec gaps, missing tests) and 4
non-blocking refinements. **Awaiting user approval.**
**Date:** 2026-05-05
**Supersedes:** v1 (deleted, three wrong assumptions), v2 (deleted, four
blocking issues), v3 (deleted, five real issues), v4 (in-place patched
to v4.1).

---

## The principle (in plain English, one more time)

1. The Azure Retail Price API gives **5 prices for every VM SKU**: PAYG,
   RI-1Y, RI-3Y, SP-1Y, SP-3Y. The Excel **Xa2** add-in already uses it.
   The BA computes per-VM offers in a separate spreadsheet, then pastes
   the summed totals into `2a-Consumption Plan Wk1!N28/N29/N30`.
2. **Per-VM is the source of truth.** Fleet sums are FYI labels, never
   inputs to financial math.
3. **ACD is a separate, manually-entered discount** the customer
   negotiated with Microsoft. It applies **only to PAYG-assigned VMs**.
   It does NOT stack with RI/SP (those offers already include their
   discount in the API price).
4. **Zero drift on both customers** is non-negotiable.

## What v1.8 actually does

The Layer 2 replica already fetches per-VM 5-offer prices from the same
Azure Retail Price API the Xa2 add-in uses. v1.8 wires that path into
the engine so the engine can build a `ConsumptionPlan` with real per-VM
data when the source is RVTools (no workbook). For workbook-based
engagements, **nothing in the engine path changes** — Layer 3 still
reads `N28/N29/N30/D8` directly.

---

## Five architectural changes (the real RFC v4 deltas vs v3)

### Change 1 — New shared module `engine/azure_per_vm_pricing.py`

**Why**: RFC v3 proposed `engine/consumption_builder.py` calling into
`training/replicas/layer2_ba_replica.py`. That violates the replica's
"INDEPENDENT ORACLE — no engine imports" rule (cited at
[training/replicas/azure_pricing.py:11](../training/replicas/azure_pricing.py#L11))
**in reverse** — it would make production engine code depend on
training-tier code, promoting `training/` into a production-critical
path without the standard QA gate.

**What**: Promote the per-VM SKU matching + 5-offer fetch into a new
`engine/azure_per_vm_pricing.py` module. Both `engine/consumption_builder.py`
**and** `training/replicas/layer2_ba_replica.py` import from there.

**Honest framing of the rule change** (per v4 judge feedback): the
replica's docstring says "no engine imports." v1.8 narrows that rule:
the replica's **parity-validation logic remains independent** (no engine
business math), but **API-fetching primitives are now shared** so parity
testing validates SKU-matching algorithms alone, not API-client
differences. We are explicitly amending the replica's docstring to:
*"INDEPENDENT ORACLE for parity logic — no engine business-math imports.
Shared API-fetching primitives live in `engine/azure_per_vm_pricing.py`."*

**Concretely**:
- New file `engine/azure_per_vm_pricing.py` exporting:
  - `PRICING_OFFERS = ("payg", "ri1y", "ri3y", "sp1y", "sp3y")`
  - `PricedSku`, `PricedDisk` frozen dataclasses
  - `match_with_retry(...)` function
  - `fetch_priced_vm_catalog(region: str) -> list[PricedSku]`
  - `is_linux_sku(product_name: str) -> bool` (single Linux filter, see Change 5)
- `training/replicas/azure_pricing.py` and
  `training/replicas/layer2_ba_replica.py` re-export from
  `engine.azure_per_vm_pricing` (thin re-exports, no behaviour change).
  L2 parity tolerances `-0.71 % PAYG / -1.35 % RI-3Y` must hold after
  the move.

### Change 2 — `ConsumptionPlan.per_vm_pricing` + ACD guardrail

**Why**: RFC v3 deferred per-VM offer assignment to v2.0 with a promise
that "today's formula `compute × (1 − acd)` is correct because both
customers are 100 % PAYG today." That's true today but **silently
wrong** the moment a future user mixes offers.

**What**:
- Add to `engine/models.py`:
  ```python
  @dataclass(frozen=True)
  class PerVmPricing:
      vm_name: str
      sku: str
      region: str
      offer_assigned: Literal["payg","ri1y","ri3y","sp1y","sp3y"] = "payg"
      payg_usd_yr:  float
      ri1y_usd_yr:  float | None
      ri3y_usd_yr:  float | None
      sp1y_usd_yr:  float | None
      sp3y_usd_yr:  float | None
  ```
  And on `ConsumptionPlan`:
  ```python
  per_vm_pricing: list[PerVmPricing] = Field(default_factory=list)
  ```
  The `offer_assigned` defaults to `"payg"` for v1.8 (every RVTools VM
  starts on PAYG; heterogeneous assignment is v2.0's UI work).

- Add ACD guard helper `_assert_acd_safe_to_apply(plan)`:
  ```python
  def _assert_acd_safe_to_apply(plan: ConsumptionPlan) -> None:
      """ACD applies to PAYG-assigned VMs only. If any VM is on RI/SP,
      uniform `compute × (1 − acd)` would silently double-discount.
      Until v2.0 ships heterogeneous-offer math, refuse to apply ACD
      to a mixed-offer plan."""
      if not plan.per_vm_pricing:
          return  # workbook path; ACD comes from D8, already baked
      offers = {v.offer_assigned for v in plan.per_vm_pricing}
      if offers - {"payg"}:
          raise ValueError(
              "ACD can only be applied to PAYG-assigned VMs. "
              f"Found non-PAYG offers: {offers - {'payg'}}. "
              "v2.0 will support heterogeneous offer math; until then, "
              "either set offer_assigned=payg for all VMs or set acd=0."
          )
  ```
  Called once at the top of [engine/financial_case.py](../engine/financial_case.py#L261)'s
  azure-scenario branch.

- Engine financial formula at line 261 stays untouched.
  **Algebraic proof** that v1.8 default (`offer_assigned="payg"` for all
  VMs) is identical to today's behaviour:
  - Today: `effective_run = (Σ vm.payg_usd_yr) × (1 − acd)`.
  - v1.8 default: every VM is `offer_assigned="payg"`, so the guard
    passes; `compute_consumption_y10` is still `Σ vm.payg_usd_yr` × y10
    factors; the formula yields the same number.
  - Customers A and B reach Layer 3 via the workbook path
    (`per_vm_pricing == []`), so the guard short-circuits and zero drift
    holds by construction.

### Change 3 — Error-handling specification

**Why**: RFC v3 didn't say what happens when the API can't match a SKU,
when a region is unrecognized, or when a region doesn't sell all 5
offers. Silent failures are unacceptable with money on the line.

**What** (single rule per failure mode, v4.1 tightened per judge):

| Failure mode | Behaviour |
| --- | --- |
| `match_with_retry` returns `None` for a VM (no SKU fits) | Skip the VM from per-VM 5-tuple; emit a `pricing_warnings` list entry on `ConsumptionPlan` (`{vm_name, reason: "no_sku_match"}`); UI shows count + names. **PAYG fallback**: the dropped VM still contributes to `compute_consumption_y10` via the existing per-vCPU/per-GiB benchmark-rate path in `consumption_builder` (`vcpu_usd_hr=0.048` etc., already used for L2 reference math at [training/replicas/layer2_ba_replica.py:188](../training/replicas/layer2_ba_replica.py#L188)) — no silent under-counting |
| Region string unknown / misspelled | Pre-flight validation: derive accepted-region set from the **first successful API call's `armRegionName` values** cached at `.cache/azure_per_vm/known_regions.json`; if cache absent, use a hard-coded fallback list of major Azure regions in `engine/azure_per_vm_pricing.py:KNOWN_REGIONS` (uksouth, eastus, eastus2, westus2, westeurope, northeurope, etc. — sourced from `data/region_map.yaml` and `data/azure_vm_catalog.json`). Unknown region → `ValueError` loudly at builder time; **no silent zero** |
| 5-offer fetch returns null for one offer (e.g., region doesn't sell SP) | Store `None` on that field of `PerVmPricing`. **`compute_for_plan` MUST filter `None` before summing**: `total = sum(getattr(v, attr) or 0.0 for v in plan.per_vm_pricing if getattr(v, attr) is not None)`. Footer disclosure: `"<offer> unavailable in N regions: r1, r2, …"`. Never coerced silently to 0 inside the sum |
| API down + cache present (within staleness budget) | Use cache; banner notes age (`"pricing data is N hours old"`). **Staleness budget: 7 days.** Cache files older than 7 days are treated as missing (next failure mode triggers) |
| API down + cache missing or stale | If RVTools-built plan: empty `per_vm_pricing`, fall back to per-vCPU/per-GiB benchmark-rate path (same fallback as no-SKU-match), banner says "Azure pricing unavailable — using benchmark rates; per-VM detail disabled." If workbook plan: untouched (Layer 3 reads `N28/N29/N30/D8` directly anyway) |

### Change 4 — Eleven tests in `tests/test_v18_per_vm_offers.py`

**Why**: RFC v3 had 3 tests; v4 added 2; v4.1 adds the **6 error-path
tests the judge flagged as missing**.

**Happy path**:
1. `ConsumptionPlan` carries non-empty `per_vm_pricing` when built from RVTools.
2. `compute_for_plan` rows equal `Σ filter(None, vm.<offer>_usd_yr)` to the cent.
3. Zero-drift constants preserved on both customers (`MAX_ENGINE_DRIFT == 0` and `MAX_ENGINE_DRIFT_CUSTOMER_B == 0`).
4. **ACD guardrail — fail path**: synthetic plan with 2 PAYG + 2 RI-3Y
   VMs, `acd=0.10`, expect `_assert_acd_safe_to_apply` to raise.
5. **Per-VM API regression**: for Customer A and B, `Σ vm.payg_usd_yr`
   within **±0.75 %** of `N28` (matches existing L2 PAYG tolerance of
   -0.71 %; tightened from v4's lax ±2 %).

**Error-path (NEW in v4.1)**:
6. **ACD guardrail — happy path**: synthetic plan with all PAYG VMs,
   `acd=0.10`, expect `_assert_acd_safe_to_apply` to pass and formula
   yields `Σ vm.payg × (1 − 0.10)`.
7. **No-SKU-match warning**: synthetic RVTools input with one
   unmatchable VM, expect `pricing_warnings` populated and that VM's
   compute still appears in `compute_consumption_y10` via
   benchmark-rate fallback.
8. **Unknown region**: build with `region="faketowneast"`, expect
   `ValueError` from pre-flight validation.
9. **Null offer in region**: synthetic VM in a region with no SP-1Y,
   expect `vm.sp1y_usd_yr is None` and `compute_for_plan` SP-1Y row
   excludes it without crashing.
10. **API outage + warm cache**: monkeypatch API to raise; cache
    populated; expect plan built successfully + banner age string.
11. **`is_linux_sku` filter**: assert `is_linux_sku("Windows Server")` is
    False; `is_linux_sku("Virtual Machines D-Series Linux")` is True;
    `is_linux_sku("Virtual Machines")` (neither marker) follows the
    consolidation rule (see Change 5).

Plus the existing **L2 parity tolerance check** (`-0.71 % PAYG /
-1.35 % RI-3Y vs Customer A Xa2-fixed`) re-runs after the Change-1
shared-module move; tolerances unchanged.

### Change 5 — Single Linux-only filter (strict positive check)

**Why**: Two filters today —
[training/replicas/azure_pricing.py:508-511](../training/replicas/azure_pricing.py#L508-L511)
(negative: skip if `"Windows" in productName` — permissive) and
[engine/azure_sku_matcher.py:507](../engine/azure_sku_matcher.py#L503)
(positive: `contains(productName, 'Linux')` — strict). Both work today,
but they could drift if Azure changes `productName` format. v4 proposed
consolidating to the permissive rule; the judge correctly flagged that
any Azure SKU that happens to contain neither marker would silently
leak through under permissive semantics.

**What** (v4.1 corrected per judge): New
`engine/azure_per_vm_pricing.py:is_linux_sku(product_name)` with the
**strict positive rule** — accept only items where `"Linux" in product_name`.
This preserves `engine/azure_sku_matcher.py`'s current behaviour
exactly (no semantic change there) and tightens the L2 replica's path
(today permissive → strict). If a future Azure SKU has a non-Linux
non-Windows marker that the BA needs included, that's a one-line
amendment to the helper, gated by an explicit test.

---

## What stays the same (zero-drift safety, restated)

| Path | Input source | v1.8 impact |
| --- | --- | --- |
| Layer 3 parity, Customer A + B (395/395 each) | reads `N28/N29/N30/D8` directly via [training/replicas/layer3_inputs.py:340-349](../training/replicas/layer3_inputs.py#L340-L349) | **NONE** — workbook→engine path untouched |
| `engine/financial_case.py` `effective_run` formula | unchanged at [line 261](../engine/financial_case.py#L261) | **NONE**; ACD guard short-circuits on workbook plans |
| ACD as a manual user input | unchanged at [app/pages/consumption.py:99](../app/pages/consumption.py#L99) | **NONE** |
| L2 parity tolerances | -0.71 % PAYG / -1.35 % RI-3Y vs Customer A Xa2-fixed | re-validated after Change 1 module move |

Because Layer 3 parity reads workbook cells directly and never traverses
`consumption_builder`, **v1.8 cannot regress L3 zero drift on either
customer.**

---

## Three engine changes (post-Change-1 module relocation)

1. **`engine/models.py`** — add `PerVmPricing` frozen dataclass; add
   `per_vm_pricing: list[PerVmPricing]` to `ConsumptionPlan`; add
   `pricing_warnings: list[dict]` for skipped VMs.
   **Remove** v1.7 flat-% fields `ri_1y_discount`, `ri_3y_discount`,
   `sp_1y_discount`, `sp_3y_discount` from `BenchmarkConfig`.

2. **`engine/consumption_builder.py`** — in the RVTools build path,
   call `engine.azure_per_vm_pricing.match_with_retry` to get the
   per-VM 5-tuple. Persist the list (and any warnings) on
   `ConsumptionPlan`. PAYG-summed `compute_consumption_y10`
   computation stays.

3. **`engine/pricing_offers.py`** — rewrite `compute_for_plan` to read
   `plan.per_vm_pricing` and sum per offer. Remove the v1.7.1
   "FYI-only flat-%" warning banner from
   [app/pages/consumption.py:207](../app/pages/consumption.py#L207).
   Add the failure-state banner per Change 3.

---

## API resilience (consolidating Change 3)

- Existing 24-hour disk cache at `.cache/azure_prices_l2/`
  ([training/replicas/azure_pricing.py:24-29](../training/replicas/azure_pricing.py#L24-L29)),
  moves to `.cache/azure_per_vm/` after Change 1.
- **Cache migration on first run**: a one-shot copy from
  `.cache/azure_prices_l2/*.json` → `.cache/azure_per_vm/*.json` runs
  during the v1.8 startup if the new directory is empty and the old one
  has files. No cold-cache penalty for upgrading customers.
- **Staleness budget**: 7 days. Cache age ≤ 7d → use it (banner notes
  age). Cache age > 7d → treat as missing.
- Cache-miss / stale + API outage:
  * RVTools-built plan → benchmark-rate fallback (per-vCPU/per-GiB),
    banner says "Azure pricing unavailable — using benchmark rates."
  * Workbook plan → untouched (Layer 3 reads workbook directly).

---

## What is NOT in v1.8 (deferred to v2.0)

- Per-VM heterogeneous offer assignment driving ACR/NPV (each VM on a
  different offer). v1.8 exposes the 5-tuple and locks the ACD
  guardrail; the math for mixed-offer ACR/NPV stays in v2.0.
- Per-VM offer-selection UI (dropdowns, CSVs).
- Reservation upfront-amortization for cashflow (v2.0 CF/P&L
  bifurcation).

---

## Risk register

| Risk | Severity | Mitigation |
| --- | --- | --- |
| L3 parity regresses on either customer | BLOCKING | Layer 3 reads workbook directly — invariant by design |
| L2 PAYG / RI-3Y aggregates regress beyond existing tolerance | HIGH | Re-run L2 parity suite after Change 1 module move; tolerances unchanged |
| ACD silently mis-applied on heterogeneous offer mix | HIGH | Change 2 guardrail raises `ValueError` until v2.0 |
| Engine→replica circular import | HIGH | Change 1 promotes shared code into `engine/`; replica only re-exports API primitives, not business math |
| Replica imports from `engine/` (replica docstring rule narrowing) | MEDIUM | v1.8 explicitly amends replica docstring: "INDEPENDENT for parity logic; shared API primitives live in `engine/azure_per_vm_pricing.py`" |
| Removing flat-% fields breaks any consumer outside `pricing_offers.py` | MEDIUM | Grep-verified: only `pricing_offers.py` reads them; `data/benchmarks_default.yaml` does NOT list them |
| Saved v1.7 user-input JSON / YAML carries `ri_*_discount` keys | LOW-MEDIUM | Pydantic `model_config = ConfigDict(extra="ignore")` on `BenchmarkConfig`; v1.7 keys silently dropped on load |
| API outage during build | MEDIUM | 24h cache + 7-day staleness budget; benchmark-rate fallback for RVTools; banner for any non-fresh state |
| Cache directory rename (`azure_prices_l2` → `azure_per_vm`) cold-cache hit | LOW-MEDIUM | One-shot copy migration on first v1.8 run (Change 3) |
| `match_with_retry` returns None for a VM | MEDIUM | Skip from 5-tuple + warning row + benchmark-rate fallback; no silent under-count |
| Region string unknown / misspelled | MEDIUM | Pre-flight against `KNOWN_REGIONS` set + cached `armRegionName` values; loud `ValueError` |
| 5-tuple has nulls (offer not sold in region) | LOW | Stored as `None`; `compute_for_plan` filters None before sum; footnote |
| `ConsumptionPlan` schema bump breaks deserialization of old saved plans | LOW | New fields default to `[]`; pydantic accepts missing |
| Windows-licensed prices contaminate per-VM totals | LOW | Strict `is_linux_sku` helper (`'Linux' in productName`); one rule both paths share |
| Linux filter divergence under future Azure API changes | LOW | Single helper (Change 5) ends dual-filter risk |

---

## Decision

User: tick one.

- [ ] **Approve** — proceed with Changes 1-5 + the three engine deltas + 5 tests above.
- [ ] **Amend** — point me to the misunderstanding; I rewrite v5.
- [ ] **Hold** — stay parked at `v1.7.1-fyi-relabel`.

Code state today: `v1.7.1-fyi-relabel`. No v1.8 work begins until you
approve.
