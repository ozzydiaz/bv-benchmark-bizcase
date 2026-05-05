# RFC — v1.8: Per-VM Azure Retail Price API in the Engine

**Status:** Draft v3 — short, plain English. **Awaiting user approval.**
**Date:** 2026-05-05
**Supersedes:** Draft v1 (deleted, three wrong assumptions) and Draft v2
(deleted, four blocking issues from adversarial judge — most blocking
issues stemmed from inventing UI behaviour and mis-modelling ACD).

---

## The principle (in plain English)

1. The Azure Retail Price API gives **5 prices for every VM SKU**: PAYG,
   RI-1Y, RI-3Y, SP-1Y, SP-3Y. The Excel **Xa2** add-in already uses this
   API; the BA does this per-VM in another spreadsheet, then pastes the
   summed totals into `2a-Consumption Plan Wk1!N28/N29/N30`.
2. **Per-VM is the source of truth.** Fleet sums are FYI labels, never
   inputs to financial math.
3. **ACD is a separate, manually-entered discount** the customer
   negotiated with Microsoft. It applies **only to VMs assigned to PAYG**.
   It does **not** stack on RI/SP — those offers already include their
   discount in the API price.
4. **Zero drift on both customers** is non-negotiable.

## What v1.8 actually does

The Layer 2 replica already fetches per-VM 5-offer prices from the same
Azure API the Xa2 add-in uses. v1.8 wires that path **into the engine**
so the engine can build a `ConsumptionPlan` with real per-VM data when
the source is RVTools (no workbook). For workbook-based engagements,
**nothing in the engine path changes** — Layer 3 still reads
`N28/N29/N30/D8` directly.

Three concrete changes, no UI work, no CSV upload, no dropdowns:

1. **`engine/models.py`** — add `per_vm_pricing: list[PerVmPricing]` to
   `ConsumptionPlan` (default `[]`). Each `PerVmPricing` carries
   `vm_name, sku, region, payg_usd_yr, ri1y_usd_yr, ri3y_usd_yr,
   sp1y_usd_yr, sp3y_usd_yr`. Remove the v1.7 flat-% benchmark fields
   `ri_1y_discount`, `ri_3y_discount`, `sp_1y_discount`, `sp_3y_discount`
   from `BenchmarkConfig` — they are gone.

2. **`engine/consumption_builder.py`** — in the RVTools build path, call
   the L2 replica's `match_with_retry` / `replicate_layer2` to get the
   per-VM 5-tuple. Persist the list on `ConsumptionPlan.per_vm_pricing`.
   The engine's existing PAYG-summed `compute_consumption_y10`
   computation stays — `Σ vm.payg_usd_yr × hours × y10_uplift`. **ACD
   stays a manual user input** and is unaffected.

3. **`engine/pricing_offers.py`** — rewrite `compute_for_plan` so each
   row sums per-VM offer prices from `plan.per_vm_pricing`. The %-off
   shown on the row is back-derived for FYI display; the real number is
   the dollars. Remove the v1.7.1 "FYI-only flat-%" warning banner from
   [app/pages/consumption.py](../app/pages/consumption.py#L207).

That is the entire RFC.

## The math (one line each)

- Per-VM PAYG total today: `Σ vm.payg_usd_yr` (already the basis for
  `compute_consumption_y10`, unchanged).
- Per-VM offer total in v1.8: `Σ vm.<offer>_usd_yr` for any of the 5 offers.
- Engine compute spend with ACD applied correctly (PAYG-only):
  `Σ_PAYG (vm.payg × (1 − acd)) + Σ_RI/SP vm.<offer_price>`.
- Today's engine formula `compute × (1 − acd)` is **mathematically
  correct only when every VM is on PAYG**. It happens to be correct on
  both Customer A and Customer B today because `D8` (ACD) is set to a
  value that already encodes whatever offer-mix the BA computed in Xa2,
  and `N28` is the PAYG-summed total. v1.8 does not change Layer 3 input
  reads, so this stays correct for those two customers.

## What stays the same (zero-drift safety)

| Path | Input source | v1.8 impact |
| --- | --- | --- |
| Layer 3 parity (Customer A + B, 395/395 each) | reads `N28/N29/N30/D8` directly from the workbook via [training/replicas/layer3_inputs.py:340-349](../training/replicas/layer3_inputs.py#L340-L349) | **NONE** — workbook→engine path untouched |
| `engine/financial_case.py` `effective_run` formula | unchanged at [line 261](../engine/financial_case.py#L261) | **NONE** |
| ACD as a manual input | unchanged in [app/pages/consumption.py:99](../app/pages/consumption.py#L99) | **NONE** — still user-typed, applies only to the PAYG portion of the per-VM mix (today: 100% PAYG, so equivalent to `total × (1 − acd)`) |

Because Layer 3 parity reads workbook cells directly and never traverses
`consumption_builder`, **v1.8 cannot regress L3 zero drift on either
customer.**

## Linux-only / KP.LINUX_AHUB_ASSUMPTION

Confirmed already enforced at
[training/replicas/azure_pricing.py:508-511](../training/replicas/azure_pricing.py#L508-L511):
```python
# Linux-only: productName must NOT contain 'Windows'.
product_name = item.get("productName") or ""
if "Windows" in product_name:
    continue
```
v1.8 inherits this filter unchanged.

## API resilience

- Existing 24-hour disk cache at `.cache/azure_prices_l2/` (per
  [training/replicas/azure_pricing.py:24-29](../training/replicas/azure_pricing.py#L24-L29)).
- If API is down **and** cache is missing → empty SKU catalog returned;
  v1.8 must surface a clear UI banner ("Azure pricing unavailable —
  per-VM panel disabled, falling back to PAYG-only basis from RVTools")
  and refuse silent computation.
- If API is down **but** cache is present → use cache, banner notes
  "pricing data is up to N hours old". Never silently use stale data
  without disclosure.

## Tests

- **Delete** `tests/test_v17_pricing_offer_breakdown.py` (asserted the
  v1.7 flat-% formula, which is being removed).
- **Add** `tests/test_v18_per_vm_offers.py` with three tests, plain English:
  1. `ConsumptionPlan` carries a non-empty `per_vm_pricing` list when
     built from RVTools.
  2. `pricing_offers.compute_for_plan` returns rows whose dollar totals
     equal `Σ vm.<offer>_usd_yr` to the cent.
  3. The two existing zero-drift constants still hold:
     `MAX_ENGINE_DRIFT == 0` AND `MAX_ENGINE_DRIFT_CUSTOMER_B == 0` in
     [tests/test_layer3_parity.py](../tests/test_layer3_parity.py).
- **Keep** the L2 PAYG and RI-3Y aggregate tolerance checks
  (-0.71 % / -1.35 % already validated).

## What is NOT in v1.8 (deferred to v2.0)

- Per-VM heterogeneous offer assignment driving ACR/NPV (each VM on a
  different offer). v1.8 just exposes the 5-tuple; the engine continues
  to treat all RVTools-built VMs as PAYG. The per-customer reality where
  the BA assigns offers per VM lives in Xa2 outside the engine.
- Per-VM offer-selection UI (dropdowns, CSVs).
- Reservation upfront-amortization for cashflow (v2.0 CF/P&L
  bifurcation).

## Risk register

| Risk | Severity | Mitigation |
| --- | --- | --- |
| L3 parity regresses on either customer | BLOCKING | Layer 3 reads workbook directly — invariant by design |
| L2 PAYG / RI-3Y aggregates regress beyond existing tolerance | HIGH | Run L2 parity suite before commit; tolerances unchanged |
| Removing flat-% fields breaks any consumer outside `pricing_offers.py` | MEDIUM | Already grep-verified: only `pricing_offers.py` reads them; `data/benchmarks_default.yaml` does NOT list them |
| API outage during build | MEDIUM | 24h cache absorbs short outages; explicit fallback banner for cache-miss + outage |
| `ConsumptionPlan` schema bump breaks deserialization of old saved plans | LOW | New field defaults to `[]`; pydantic accepts missing |
| Windows-licensed prices contaminate per-VM totals | LOW | Already filtered at azure_pricing.py:508-511 |

## Decision

User: tick one.

- [ ] **Approve** — proceed with the three engine changes + test swap above.
- [ ] **Amend** — point me to the misunderstanding; I rewrite v4.
- [ ] **Hold** — stay parked at `v1.7.1-fyi-relabel`.

Code state today: `v1.7.1-fyi-relabel`. No v1.8 work begins until you
approve.
