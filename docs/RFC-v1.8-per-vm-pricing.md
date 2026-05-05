# RFC — v1.8: Wire Existing Per-VM 5-Offer Pricing to the Engine

**Status:** Draft v2 — short, corrected. **Awaiting user approval.**
**Date:** 2026-05-05
**Supersedes:** previous RFC v1 (deleted; based on three wrong assumptions).

---

## The user's three corrections (acknowledged)

1. **The Azure Retail Price API DOES provide RI / SP per-VM.** The Excel Xa2
   add-in already uses it. My prior RFC implied this was a future dependency
   — wrong.
2. **The BA workbook drives Azure NPV from per-VM sums** (not a fleetwide
   ACD). The "fleet ACD" in the workbook is a back-derived FYI label, not
   an input.
3. **Zero drift is non-negotiable.** Any plan that introduces drift against
   either Customer A or Customer B is rejected.

## What I missed in RFC v1

The per-VM 5-offer pricing infrastructure **is already built and validated**
in the Layer 2 replica:

- [training/replicas/azure_pricing.py:74](../training/replicas/azure_pricing.py#L74) — `PRICING_OFFERS = ("payg", "ri1y", "ri3y", "sp1y", "sp3y")`.
- The replica fetches all 5 offers per SKU × region from the live Azure
  Retail Prices API (the same one Xa2 uses, per repo memory transcript
  00:07:33).
- Per-VM `pricing` dict carries all 5 offers (memory line 233).
- [training/baselines/customer_a_2024_10/ba_expected.yaml](../training/baselines/customer_a_2024_10/ba_expected.yaml) encodes
  the rule **`L2.PRICING.001` — "per-VM Σ matched_SKU PAYG_hr × 8760"** —
  exactly the user's principle, locked since Phase 2b.
- L2 parity vs Customer A's Xa2-fixed authoritative tab today:
  - PAYG: −0.71 % (BA $6,775,859 → replica $6,727,968)
  - RI-3Y: −1.35 % (BA $2,569,291 → replica $2,534,639)

**Therefore v1.8 is *plumbing*, not *building*.** Fetch / matcher / 5-tuple
already work. The work is wiring this up to (a) the engine inputs path,
(b) the v1.7 pricing-offer panel, and (c) the financial case so per-VM
sums replace the flat-% benchmarks I added in v1.7.

## Architecture today vs target

```
                    TODAY                              v1.8 TARGET
                    -----                              -----------
RVTools → consumption_builder                  RVTools → consumption_builder
       ↓ (per-VM PAYG only)                          ↓ (per-VM 5-tuple from L2)
       ↓                                             ↓
  ConsumptionPlan                              ConsumptionPlan
  ─ annual_compute_..._y10  (Σ vm PAYG)        ─ annual_compute_..._y10 (Σ vm PAYG, unchanged)
  ─ azure_consumption_discount = user-typed    ─ azure_consumption_discount = back-derived (1 − Σ_after / Σ_payg)
                                               ─ per_vm_pricing: list[PerVmPricing]  ← NEW
       ↓                                             ↓
  financial_case._azure_consumption_by_year()  ← UNCHANGED. Same `compute × (1 − ACD)` formula.
       ↓                                             ↓
  pricing_offers.compute_for_plan()            pricing_offers.compute_for_plan()
  → flat-%: aggregate × static benchmark       → per-VM: Σ vm.{payg,ri1y,ri3y,sp1y,sp3y}_usd_yr
                                                    + back-derived effective rate (FYI label)
```

## The math (and why zero drift holds)

For any consumption plan:

$$
\text{compute\_consumption\_y10} = \sum_i v_i^{\text{PAYG}}, \quad
\text{ACD} = 1 - \frac{\sum_i v_i^{\text{after-offer}}}{\sum_i v_i^{\text{PAYG}}}
$$

Engine computes:

$$
\text{effective\_run} = \text{compute\_consumption\_y10} \times (1 - \text{ACD}) = \sum_i v_i^{\text{after-offer}}
$$

This is **algebraically identical** to per-VM offer summation. So the
engine's existing `full_run × (1 − ACD)` formula in
[engine/financial_case.py:261](../engine/financial_case.py#L261) **stays
untouched** while the principle "per-VM sums drive financials" is
satisfied at the input layer.

### Layer 3 parity (zero-drift contract)

| Path                        | Inputs source                                        | v1.8 impact         |
| --------------------------- | ---------------------------------------------------- | ------------------- |
| L3 parity tests             | [training/replicas/layer3_inputs.py](../training/replicas/layer3_inputs.py) reads workbook `2a-Consumption Plan Wk1!N28` (PAYG total) and `D8` (ACD) directly | **NONE** — workbook→engine path untouched |
| App "build from RVTools"    | [engine/consumption_builder.py](../engine/consumption_builder.py) — currently per-VM PAYG, fleet ACD | **WIRED to L2** — per-VM 5-tuple from existing replica |
| L2 parity tests (PAYG / RI-3Y aggregate) | [training/replicas/azure_pricing.py](../training/replicas/azure_pricing.py) | Continues to validate per-VM Σ within tolerance |

Because L3 parity reads workbook cells directly and never traverses the
consumption builder, **v1.8 cannot regress L3 zero drift on either customer.**
This is the same reason v1.7 didn't regress it.

## Concrete change list

### Engine

1. **`engine/models.py`** — add to `ConsumptionPlan`:
   ```python
   per_vm_pricing: list["PerVmPricing"] = Field(default_factory=list)
   ```
   New `PerVmPricing` dataclass (frozen):
   ```python
   vm_name: str
   sku: str
   region: str
   payg_usd_yr:  float
   ri1y_usd_yr:  float
   ri3y_usd_yr:  float
   sp1y_usd_yr:  float
   sp3y_usd_yr:  float
   ```
   **Remove** the v1.7 flat-% fields:
   `ri_1y_discount`, `ri_3y_discount`, `sp_1y_discount`, `sp_3y_discount`.
   They were a mistake; per-VM API data replaces them.

2. **`engine/consumption_builder.py`** — replace the inline PAYG-only matcher
   with a call into the L2 replica's `match_with_retry` /
   `replicate_layer2` path that already returns the 5-tuple per VM. Persist
   each VM's 5-tuple on `ConsumptionPlan.per_vm_pricing`. Compute:
   ```
   compute_consumption_y10 = Σ vm.payg_usd_yr × hours_per_year × y10_uplift
   azure_consumption_discount = 1 − (Σ assigned-offer_usd_yr) / (Σ payg_usd_yr)
   ```
   Default offer assignment: PAYG (= ACD 0). User can pick "all RI-3Y",
   "all SP-1Y", or upload per-VM allocation in the UI (UI scope below).

3. **`engine/financial_case.py`** — **unchanged.** Continues to use
   `compute_consumption_y10 × (1 + g) × (1 − ACD)`. By the algebraic
   identity above, this equals `Σ vm × (1 − vm_offer_disc)` exactly.

4. **`engine/pricing_offers.py`** — rewrite `compute_for_plan` to read
   `plan.per_vm_pricing` directly:
   ```python
   payg = sum(v.payg_usd_yr for v in plan.per_vm_pricing)
   for offer, attr in [("PAYG","payg_usd_yr"), ("RI 1Y","ri1y_usd_yr"), ...]:
       total = sum(getattr(v, attr) for v in plan.per_vm_pricing)
       eff_disc = 1 − total / payg if payg > 0 else 0.0
       rows.append(OfferRow(offer, eff_disc, total, payg − total, ...))
   ```
   Plus the existing BA-truth anchor row.
   The displayed "% off PAYG" is now the **back-derived effective rate**,
   which is FYI. Per-VM totals are real.

### UI

5. **`app/pages/consumption.py`** —
   - Remove the v1.7 "FYI-only / interim flat-%" warning banner (no longer needed).
   - Add an "🔍 Per-VM detail" expander under each workload showing the
     full per-VM 5-tuple table with download-as-CSV.
   - Add an offer-assignment control: dropdown "Default offer for this
     plan" with options PAYG / RI-1Y / RI-3Y / SP-1Y / SP-3Y, plus
     "Upload per-VM allocation (CSV)".

### Tests

6. **`tests/test_v17_pricing_offer_breakdown.py`** — rename to
   `test_v18_per_vm_offers.py` (or just keep the file and rewrite). New
   acceptance tests:
   - **AC-1** `ConsumptionPlan` has `per_vm_pricing: list[PerVmPricing]`.
   - **AC-2** `pricing_offers.compute_for_plan` reads from `per_vm_pricing`,
     not from `BenchmarkConfig`.
   - **AC-3** Each offer total == `Σ vm.{offer}_usd_yr` to the cent.
   - **AC-3b** `Σ payg_usd_yr == annual_compute_consumption_lc_y10` (input
     parity invariant).
   - **AC-4** Layer 3 drift constants preserved: `MAX_ENGINE_DRIFT == 0`
     AND `MAX_ENGINE_DRIFT_CUSTOMER_B == 0`.
   - **AC-5** No CF/P&L bifurcation fields leak (v2.0 deferral guardrail).
   - **AC-6 (NEW)** L2 PAYG and RI-3Y replica aggregates remain within
     existing tolerance vs Customer A's Xa2-fixed numbers.

### Cleanup

7. **`engine/outputs.py`** docstring at line ~448 — drop the reference to
   `azure_consumption_discount` as the BA-truth single number; clarify it's
   now back-derived from per-VM offer assignment.
8. **`version-history.md`** — v1.8 release entry; v1.7 / v1.7.1 retired
   (the flat-% benchmarks and FYI banner go away with v1.8).
9. **`docs/RFC-v1.8-per-vm-pricing.md`** — mark "implemented" once shipped.

## Risk register

| Risk                                                | Severity | Mitigation                                                  |
| --------------------------------------------------- | -------- | ----------------------------------------------------------- |
| L3 parity regresses                                 | BLOCKING | L3 path doesn't traverse consumption builder — invariant by design |
| L2 PAYG / RI-3Y aggregates regress beyond tolerance | HIGH     | Run L2 parity suite before commit; ratchet existing thresholds |
| `ConsumptionPlan` schema bump breaks replicas       | MEDIUM   | New field defaults to `[]`; replicas continue reading workbook directly |
| Per-VM allocation UI exceeds scope                  | LOW      | Phase as: v1.8.0 default-offer dropdown; v1.8.1 CSV upload  |
| API rate-limiting on full 5-tuple fetch             | LOW      | Existing 24h cache in `.cache/azure_prices_l2/` already absorbs this |

## What this RFC does NOT propose (deferred to v2.0)

- Per-VM **offer selection drives ACR / NPV** with each VM on a *different*
  offer (greedy, cost-of-capital aware). Today and in v1.8, the user picks
  one offer per plan (or uploads per-VM allocation) and the engine treats
  the result as a single back-derived ACD. v2.0 (per repo memory) is
  per-VM Y1 RI upfront amortization — that's CF/P&L bifurcation and stays
  deferred.

## Decision needed

User: confirm one of:

- [ ] **Approve** — proceed with §"Concrete change list" steps 1-9.
- [ ] **Amend** — point me to the misunderstanding and I'll redraft.
- [ ] **Hold** — neither yet.

Until checked, code state stays at `v1.7.1-fyi-relabel`.
