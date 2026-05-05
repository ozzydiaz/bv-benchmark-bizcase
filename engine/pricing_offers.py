"""
FYI-only flat-% pricing-offer sensitivity (v1.7 — INTERIM).

**Honest scope.** This module is a *fleet-aggregate × static-benchmark-discount*
sensitivity panel. It multiplies the per-plan PAYG aggregate
(``ConsumptionPlan.annual_compute_consumption_lc_y10``) by static fractions
on ``BenchmarkConfig`` (RI-1Y 20%, RI-3Y 36%, SP-1Y 18%, SP-3Y 30%) to show
"what if all VMs in this plan were on offer X at the benchmark rate?".

It is **NOT** per-VM-from-API pricing. The Azure Retail Price API path in
``engine/consumption_builder.py`` only fetches PAYG rates today; per-VM
RI / SP rates are not retrieved or persisted anywhere. True per-VM offer
pricing is planned for v1.8 — see ``docs/RFC-v1.8-per-vm-pricing.md``.

The user's authoritative principle (2026-05-05) is:

  "NO FLEETWIDE is used for the financials nor ACR. Fleetwide averages are
  only for FYI to the user/BA. ALL calculations for ACR and the pricing
  offers are per-VM, then aggregated to the various sums to be shown AFTER
  the azure retail price API provides the PAYG, RI1, RI3, SP1, SP3 pricing
  offers per-VM."

This v1.7 module satisfies the *FYI* clause only. v1.8 will replace the
flat-% formula with per-VM API rates summed up.

Engine-math invariant
---------------------
This module is **strictly display-only**. It does not mutate the financial
case, retained-cost, or NPV pipeline, so Layer 3 parity (Customer A 395/395
+ Customer B 395/395 zero drift) is preserved by construction. Any change
that flows its output back into ``financial_case`` or ``outputs`` re-opens
layer 3 parity on **both** customers and is forbidden until v1.8 lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.models import BenchmarkConfig, BusinessCaseInputs, ConsumptionPlan


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OfferRow:
    """One row in the offer breakdown table.

    All values are in the same currency unit as the consumption plan
    (typically local currency, ``ConsumptionPlan.*_lc_y10``).
    """
    offer: str                 # "PAYG" | "RI 1Y" | "RI 3Y" | "SP 1Y" | "SP 3Y" | "BA-truth (current ACD)"
    discount_pct: float        # fractional discount off PAYG (0.0 = none)
    annual_total: float        # Y10 compute spend under this offer
    savings_vs_payg: float     # PAYG_total − this offer's total (≥0 except for negative ACD)
    savings_pct_vs_payg: float # savings_vs_payg / PAYG_total (0.0 if PAYG_total == 0)


@dataclass(frozen=True)
class PerPlanBreakdown:
    """Pricing-offer breakdown for a single consumption plan."""
    workload_name: str
    payg_compute_y10: float        # full PAYG list price (Y10)
    storage_y10: float             # PAYG-only: storage is not RI/SP-eligible
    other_y10: float               # PAYG-only: 'other' (DB, network, etc.) is not RI/SP-eligible
    current_acd: float             # the ACD currently used in NPV (BA-truth anchor)
    rows: list[OfferRow] = field(default_factory=list)

    @property
    def total_payg_y10(self) -> float:
        """Compute + storage + other under PAYG list price (no offer discount)."""
        return self.payg_compute_y10 + self.storage_y10 + self.other_y10


@dataclass(frozen=True)
class PricingOfferBreakdown:
    """Full pricing-offer breakdown across all consumption plans."""
    plans: list[PerPlanBreakdown] = field(default_factory=list)

    @property
    def fleet_payg_compute_y10(self) -> float:
        return sum(p.payg_compute_y10 for p in self.plans)

    def fleet_total_for(self, offer_label: str) -> float:
        """Sum the named offer's compute total across every plan."""
        out = 0.0
        for plan in self.plans:
            for row in plan.rows:
                if row.offer == offer_label:
                    out += row.annual_total
                    break
        return out


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

# Order matters — UI renders rows in this sequence.
_OFFER_ORDER = ("PAYG", "RI 1Y", "RI 3Y", "SP 1Y", "SP 3Y")


def _offer_discount(offer: str, bm: BenchmarkConfig) -> float:
    if offer == "PAYG":
        return 0.0
    if offer == "RI 1Y":
        return bm.ri_1y_discount
    if offer == "RI 3Y":
        return bm.ri_3y_discount
    if offer == "SP 1Y":
        return bm.sp_1y_discount
    if offer == "SP 3Y":
        return bm.sp_3y_discount
    raise ValueError(f"Unknown offer: {offer!r}")


def compute_for_plan(
    plan: ConsumptionPlan,
    bm: BenchmarkConfig,
) -> PerPlanBreakdown:
    """Compute the offer breakdown for a single ``ConsumptionPlan``."""
    payg_compute = plan.annual_compute_consumption_lc_y10
    rows: list[OfferRow] = []

    # 5 standard offers (ordered for UI display).
    for offer in _OFFER_ORDER:
        d = _offer_discount(offer, bm)
        total = payg_compute * (1.0 - d)
        savings = payg_compute - total
        savings_pct = (savings / payg_compute) if payg_compute > 0 else 0.0
        rows.append(OfferRow(
            offer=offer,
            discount_pct=d,
            annual_total=total,
            savings_vs_payg=savings,
            savings_pct_vs_payg=savings_pct,
        ))

    # BA-truth anchor — the ACD actually fed into the financial case / NPV.
    # Shown last so users can compare their negotiated discount to the
    # individual-offer alternatives.
    acd = plan.azure_consumption_discount
    ba_total = payg_compute * (1.0 - acd)
    ba_savings = payg_compute - ba_total
    ba_pct = (ba_savings / payg_compute) if payg_compute > 0 else 0.0
    rows.append(OfferRow(
        offer="BA-truth (current ACD)",
        discount_pct=acd,
        annual_total=ba_total,
        savings_vs_payg=ba_savings,
        savings_pct_vs_payg=ba_pct,
    ))

    return PerPlanBreakdown(
        workload_name=plan.workload_name or "",
        payg_compute_y10=payg_compute,
        storage_y10=plan.annual_storage_consumption_lc_y10,
        other_y10=plan.annual_other_consumption_lc_y10,
        current_acd=acd,
        rows=rows,
    )


def compute(
    inputs: BusinessCaseInputs,
    benchmarks: BenchmarkConfig,
) -> PricingOfferBreakdown:
    """Build a per-consumption-plan offer breakdown for the whole case."""
    return PricingOfferBreakdown(
        plans=[compute_for_plan(p, benchmarks) for p in inputs.consumption_plans]
    )
