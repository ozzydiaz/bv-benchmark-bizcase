# RFC — v1.8: Per-VM Azure Pricing Offers from Retail Price API

**Status:** Draft — awaiting user review (NOT YET APPROVED FOR BUILD).
**Date drafted:** 2026-05-05
**Author:** GitHub Copilot (autonomous draft, user offline)
**Authoritative principle (verbatim from user, 2026-05-05):**

> "NO FLEETWIDE is used for the financials nor ACR!! Fleetwide averages are
> only for FYI to the user/BA. ALL calculations for ACR and the pricing
> offers are per-VM, then aggregated to the various sums to be shown AFTER
> the azure retail price API provides the PAYG, RI1, RI3, SP1, SP3 pricing
> offers per-VM!!!"

Adversarial-judge verdict against this principle on the v1.7 codebase:
**NON-COMPLIANT, HIGH confidence, 5 blocking gaps.** This RFC scopes the
remediation. It is intentionally a planning artifact, not an implementation
plan, because the core tension below requires user adjudication.

---

## 1. Executive summary

v1.7 ships a flat-% sensitivity panel that does NOT match the per-VM-from-API
contract above. v1.7.1 (already shipped alongside this RFC) relabels that
panel as "FYI-only / interim" so no BA mistakes it for the real per-VM
breakdown.

v1.8 must:

1. Fetch per-VM RI-1Y / RI-3Y / SP-1Y / SP-3Y rates from the Azure Retail
   Price API in lockstep with the PAYG fetch already done in
   [engine/consumption_builder.py](../engine/consumption_builder.py).
2. Persist a per-VM pricing record on every consumption plan (5-tuple
   per VM: PAYG, RI-1Y, RI-3Y, SP-1Y, SP-3Y annual costs in local currency).
3. Drive the pricing-offer breakdown UI from those summed per-VM records,
   not from `aggregate × benchmark_%`.
4. Decide (open question §6) whether per-VM offer selection drives ACR/NPV,
   or whether ACR/NPV remains on the BA-truth fleet ACD with per-VM only
   for display.

---

## 2. Current state — verbatim from adversarial audit

### 2.1 Fleetwide-only data flow today

| Stage                                              | Per-VM today? | File                                                                                                         |
| -------------------------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------ |
| Azure Retail Price API call (PAYG)                 | ✅ per-VM     | [engine/azure_sku_matcher.py](../engine/azure_sku_matcher.py) (only PAYG)                                    |
| Azure Retail Price API call (RI-1Y/3Y, SP-1Y/3Y)   | ❌ none       | not implemented anywhere                                                                                     |
| Per-VM PAYG rate stored                            | ❌ transient  | [engine/consumption_builder.py:290](../engine/consumption_builder.py#L290) — summed and discarded            |
| Per-VM RI/SP rate stored                           | ❌ none       | n/a                                                                                                          |
| Pricing-offer breakdown                            | ❌ flat-%     | [engine/pricing_offers.py:117](../engine/pricing_offers.py#L117) — `aggregate × (1 − static_benchmark_%)`    |
| ACR drives NPV                                     | ❌ fleet-ACD  | [engine/financial_case.py:261](../engine/financial_case.py#L261) — `full_run × (1 − cp.azure_consumption_discount)` |

### 2.2 Why this matters

- The user is paying for an Azure cost model whose RI/SP numbers are static
  benchmarks, not customer-specific API rates. Different SKUs (D-series,
  E-series, M-series, GPU) have *very* different RI/SP discount curves; a
  fleetwide 36% RI-3Y assumption can be off by ±10 percentage points for an
  individual VM.
- A BA cannot defend the v1.7 panel to a customer who asks "what's my
  actual RI-3Y price for an E64s_v5 in East US?". Today the answer is "we
  multiplied your fleet PAYG total by 0.64."

---

## 3. Proposed architecture (v1.8)

### 3.1 Data model changes — [engine/models.py](../engine/models.py)

New frozen dataclass (or pydantic model) capturing per-VM, per-offer cost:

```python
@dataclass(frozen=True)
class PerVmOfferCost:
    """Per-VM Y10 annual cost under each Azure pricing offer (local currency)."""
    vm_name: str           # RVTools VM name
    sku: str               # matched Azure SKU, e.g. "Standard_D4s_v5"
    region: str            # billing region, e.g. "eastus"
    payg_annual_lc:  float
    ri_1y_annual_lc: float
    ri_3y_annual_lc: float
    sp_1y_annual_lc: float
    sp_3y_annual_lc: float
```

Add to `ConsumptionPlan`:

```python
per_vm_offers: list[PerVmOfferCost] = Field(default_factory=list)
# NOTE: annual_compute_consumption_lc_y10 remains the canonical PAYG
# aggregate (sum of per_vm_offers[*].payg_annual_lc) so layer 3 parity
# is preserved by construction.
```

### 3.2 SKU matcher changes — [engine/azure_sku_matcher.py](../engine/azure_sku_matcher.py)

Extend `VMSku` (currently only carries `price_per_hour_usd` for PAYG):

```python
@dataclass(frozen=True)
class VMSku:
    sku: str
    vcpu: int
    memory_gib: float
    price_per_hour_payg_usd:  float
    price_per_hour_ri_1y_usd: float | None  # may be missing for some SKUs
    price_per_hour_ri_3y_usd: float | None
    price_per_hour_sp_1y_usd: float | None
    price_per_hour_sp_3y_usd: float | None
```

Extend the cache schema and `data/azure_vm_catalog.json` builder
(`scripts/validate_pricing_cache.py`) to include all 5 offers per
SKU × region.

### 3.3 Consumption builder changes — [engine/consumption_builder.py](../engine/consumption_builder.py)

The per-VM loop at lines ~275–310 currently captures only `vm_price_per_hr`
for PAYG. Extend to capture the full 5-tuple and append a `PerVmOfferCost`
to a list that becomes `ConsumptionPlan.per_vm_offers`.

**Critical invariant:** `sum(per_vm_offers[*].payg_annual_lc) ==
annual_compute_consumption_lc_y10` to the cent. Layer 3 parity tests must
continue to read `annual_compute_consumption_lc_y10` and see the same
value as today.

### 3.4 Pricing-offer breakdown changes — [engine/pricing_offers.py](../engine/pricing_offers.py)

Replace flat-% formula with per-VM sum:

```python
def compute_for_plan(plan, bm):
    payg = sum(v.payg_annual_lc for v in plan.per_vm_offers)
    rows = [
        OfferRow("PAYG",  0.0,   payg, 0.0, 0.0),
        OfferRow("RI 1Y", _avg_eff_disc(plan, "ri_1y"), sum(v.ri_1y_annual_lc for v in plan.per_vm_offers), ...),
        OfferRow("RI 3Y", _avg_eff_disc(plan, "ri_3y"), sum(v.ri_3y_annual_lc for v in plan.per_vm_offers), ...),
        OfferRow("SP 1Y", _avg_eff_disc(plan, "sp_1y"), sum(v.sp_1y_annual_lc for v in plan.per_vm_offers), ...),
        OfferRow("SP 3Y", _avg_eff_disc(plan, "sp_3y"), sum(v.sp_3y_annual_lc for v in plan.per_vm_offers), ...),
        # BA-truth row continues to anchor against plan.azure_consumption_discount
    ]
```

Where `_avg_eff_disc` reports the **fleetwide effective discount** as
`1 − (sum_offer / sum_payg)` — labeled clearly as FYI, not used as input.

`BenchmarkConfig.{ri,sp}_*_discount` fields become **fallback defaults** for
SKUs where the API does not return offer pricing (some custom SKUs, older
generations, partner SKUs). They MUST be flagged in the UI when any VM
falls back to them.

### 3.5 UI changes — [app/pages/consumption.py](../app/pages/consumption.py)

- Remove the v1.7 FYI warning banner (no longer needed once numbers are
  honest).
- Add a "details" expander per workload showing per-VM 5-tuple table
  (downloadable as CSV).
- Keep the fleet roll-up but label fleetwide effective discounts
  explicitly as "fleet-effective % (FYI; computed as Σoffer / Σpayg)".

### 3.6 Test changes — [tests/test_v17_pricing_offer_breakdown.py](../tests/test_v17_pricing_offer_breakdown.py)

- **AC-3 must be replaced.** Today it asserts
  `offer_total == payg × (1 − static_benchmark_%)` to the cent. Under v1.8
  this is wrong: the offer total is the per-VM sum, which only happens to
  equal the static-% formula when every VM's actual API discount equals
  the benchmark.
- New AC-3: `offer_total == sum(per_vm_offers[*].{offer}_annual_lc)` to
  the cent.
- New AC-3c: `sum(per_vm_offers[*].payg_annual_lc) ==
  annual_compute_consumption_lc_y10` (parity contract with consumption
  builder).
- AC-1 (BenchmarkConfig defaults) stays — fields become fallbacks.
- AC-4 (zero-drift constants) stays — see §6 for whether zero drift can
  remain at 0.

---

## 4. Phasing options

### 4.1 Option A — Per-VM display only, ACR/NPV unchanged (LOW RISK)

- Implement §3.1–§3.6 above.
- `engine/financial_case.py` still uses `cp.azure_consumption_discount`
  (BA-truth fleet ACD) for NPV.
- Layer 3 zero-drift parity preserved by construction.
- Honest with user's principle for **display**; partial on **ACR drives
  NPV** (still fleetwide there).
- Status of fleetwide in financials: **still present in NPV path** —
  technically violates "NO FLEETWIDE is used for the financials nor ACR."

### 4.2 Option B — Per-VM display AND per-VM offer selection drives NPV (HIGH RISK)

- Implement §3.1–§3.6 above.
- Replace [engine/financial_case.py:261](../engine/financial_case.py#L261)
  `effective_run = full_run × (1 + g) × (1 − acd)` with per-VM offer
  allocation. Each VM is assigned an offer (PAYG / RI-1Y / RI-3Y / SP-1Y /
  SP-3Y) with a Y1-upfront component for RIs.
- Requires:
  - User-chosen offer-allocation strategy (greedy savings? cost-of-capital
    aware? customer-specified per-VM?). Currently no input field captures
    this.
  - Replica/oracle re-baseline (the BA workbook itself uses fleet ACD —
    the engine output will diverge from the workbook unless the workbook
    is also restructured).
  - Layer 3 `MAX_ENGINE_DRIFT` and `MAX_ENGINE_DRIFT_CUSTOMER_B` will
    almost certainly need to budge from 0. **This is the v2.0 deferral
    we already documented.**

---

## 5. Layer 3 parity risk (NEVER REGRESS)

| Risk                                                    | Severity   | Mitigation                                                              |
| ------------------------------------------------------- | ---------- | ----------------------------------------------------------------------- |
| `annual_compute_consumption_lc_y10` aggregate drifts    | BLOCKING   | Invariant test: `sum(per_vm_offers[*].payg_annual_lc)` == aggregate     |
| `financial_case._azure_consumption_by_year` formula change | BLOCKING (Opt B only) | Option A doesn't change it; Option B re-baselines drift constants       |
| Replica oracle out of sync                              | BLOCKING   | Replica must populate `per_vm_offers` from BA workbook per-VM rows      |
| `BenchmarkConfig.ri_*_discount` semantics change        | MEDIUM     | Rename to `*_discount_fallback` to make the fallback role explicit      |
| AC-3 test relaxed too loosely                           | MEDIUM     | New AC-3 must assert *exact* per-VM sum, not a tolerance                |

---

## 6. Open questions (REQUIRES USER DECISION BEFORE BUILD)

### Q1 — Option A or Option B?

The user's principle as stated demands Option B ("ALL calculations for
ACR and the pricing offers are per-VM"). But Option B breaks layer 3
zero-drift against the BA workbook because the workbook itself uses
fleet ACD for ACR. So either:

- **(B1)** We accept temporary layer 3 drift (`MAX_ENGINE_DRIFT > 0`)
  while we restructure the BA workbook in lockstep.
- **(B2)** We keep Option A for v1.8 and explicitly defer the
  ACR-drives-NPV piece to v2.0 (already in our roadmap), labeling the
  v1.7 fleetwide FYI as "interim until v2.0."
- **(B3)** Some hybrid: per-VM offer selection drives a new
  `azure_consumption_per_vm_npv` output column shown alongside the
  BA-truth NPV, with the customer choosing which to commit to.

**Recommendation:** B2 (Option A for v1.8 + v2.0 deferral) — preserves
zero-drift, executes the FYI clause cleanly, and surfaces per-VM API
rates everywhere they currently belong. v2.0 then handles the workbook
restructure.

### Q2 — Where does the offer-allocation strategy live (Option B only)?

If we go to per-VM offer selection driving NPV, we need a policy:

- Greedy: allocate the largest VMs to the deepest discount.
- All-RI-3Y by default with PAYG override per workload.
- User-uploaded per-VM allocation (CSV).

This is a product decision, not an engineering one.

### Q3 — How do we treat upfront RI commitment in CF vs P&L?

Today the engine assumes all Azure consumption is OPEX in the year it
occurs. RI 1Y / 3Y upfront purchases are CF in Y1 but amortized P&L
expense over the term. This is the deferred v2.0 work; if we do Option B,
we must address it now.

### Q4 — Cache strategy for retail prices?

Current cache (`data/azure_vm_catalog.json`) is PAYG-only. With 5 offers
× ~50 SKUs × ~30 regions, the cache balloons ~5×. Acceptable, but the
cache validator in `scripts/validate_pricing_cache.py` must be extended.
What's the acceptable cache age? (Today: 30 days for PAYG; RI/SP rates
move less but should probably stay matched.)

### Q5 — How do we handle SKUs without RI/SP availability?

Some SKUs (preview, partner, custom) have no RI/SP API rate. Today the
fallback is the BA workbook's static benchmark. Should the UI flag VMs
that fell back to fleetwide-flat-% so the BA knows which numbers are
honest per-VM and which are not?

---

## 7. Concrete implementation plan (Option A only — low risk)

If user approves Option A, the build sequence is:

1. **Cache schema bump** — `data/azure_vm_catalog.json` v2 with all 5
   offers per SKU × region. Migration script in `scripts/`.
2. **`VMSku` extension + API client** — fetch RI/SP rates from Azure
   Retail Price API in `engine/azure_sku_matcher.py`. Filter parameters
   require `priceType eq 'Reservation'` and term filters.
3. **`PerVmOfferCost` dataclass** — `engine/models.py`.
4. **`ConsumptionPlan.per_vm_offers`** — populate in
   `engine/consumption_builder.py` per-VM loop.
5. **Invariant test** — `tests/test_v18_per_vm_offers_parity.py`:
   - sum of per-VM PAYG == `annual_compute_consumption_lc_y10`
   - all 35 layer 3 parity tests still pass at zero drift on both customers
   - Customer A 395/395 + Customer B 395/395 invariant holds.
6. **Replace `engine/pricing_offers.py:compute_for_plan`** — sum per-VM,
   relabel `BenchmarkConfig` discounts as fallback only.
7. **Rewrite `tests/test_v17_pricing_offer_breakdown.py`** AC-3 to per-VM
   sum (or move file to `test_v18_*` and retire v1.7 file).
8. **UI** — replace v1.7 warning banner with per-VM detail expander; add
   per-VM CSV download.
9. **`docs/version-history.md`** — v1.8 release entry; retire v1.7 FYI
   panel.

**Out of scope for v1.8:** any change to `engine/financial_case.py` ACR
formula. That work is v2.0 (per-VM offer selection drives NPV).

---

## 8. Decision needed

User: please confirm one of:

- [ ] Build **Option A** as scoped (per-VM display, ACR/NPV unchanged) → I proceed with §7.
- [ ] Build **Option B** (per-VM display + per-VM ACR drives NPV) → I scope v2.0 in a follow-up RFC and address Q2 + Q3 + Q5 first.
- [ ] **Hold** — neither yet; revise this RFC.

Until one of these is checked, the codebase remains at v1.7.1 (FYI-only
relabel) with no per-VM API work in flight.
