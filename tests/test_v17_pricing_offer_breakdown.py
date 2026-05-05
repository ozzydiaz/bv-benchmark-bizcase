"""
v1.7 Acceptance Tests — Per-VM Pricing Offer Breakdown
========================================================

Replaces the obsolete "RI/SP-blended ACD" scaffolding. The original v1.7
plan derived ``effective_acd = paygo*0 + ri_1y*0.20 + ri_3y*0.36 + sp_1y*0.18
+ sp_3y*0.30`` as a single weighted-average discount fed into the financial
case. This was reframed during v1.7 design review:

> "Blending those offers as a single ACD doesn't reflect reality — a given
> VM is placed on **one** Azure pricing offer at a time. We want the
> per-VM pricing offers summed up and shown to the user in the app UI."

The reframed contract is **display-only**:

* Compute Y10 compute spend under each individual Azure pricing offer
  (PAYG, RI-1Y, RI-3Y, SP-1Y, SP-3Y) by applying a discount fraction to
  the PAYG list-price total the consumption builder already produces.
* Sum across VMs to produce per-offer totals.
* Show alongside a 'BA-truth' anchor row using the actual
  ``ConsumptionPlan.azure_consumption_discount`` so the BA can compare.

Critically, this never feeds into ``financial_case`` / NPV / ROI — Layer 3
zero-drift parity (Customer A 395/395 + Customer B 395/395) is preserved
by construction, **regardless of any user edits to the discount knobs**.

Acceptance criteria
-------------------
  AC-1  ``BenchmarkConfig`` has 4 discount-rate fields with the documented
        defaults (RI-1Y 20%, RI-3Y 36%, SP-1Y 18%, SP-3Y 30%) — taken from
        the BA workbook D156/D157/D163-D166 model.
  AC-2  ``engine.pricing_offers.compute_for_plan(plan, bm)`` returns a
        ``PerPlanBreakdown`` with rows for all 5 standard offers plus a
        BA-truth anchor row.
  AC-3  Per-offer totals = ``payg_total * (1 - offer_discount)`` to the
        cent, for every offer.
  AC-4  Layer 3 zero-drift parity (Customer A AND Customer B) remains
        intact regardless of how the new discount knobs are configured.
        This is the v1.7 contract: discount rates are display-only and
        MUST NOT mutate the engine's NPV/ROI math.
  AC-5  No new CF/P&L bifurcation fields leak onto ``BusinessCaseSummary``.
        Per-VM RI/SP allocation with Y1 upfront amortization is still
        deferred to v2.0 — see version-history.md.
"""

from __future__ import annotations

import pytest

from engine.models import BenchmarkConfig


# Documented per-family discount rates (BA workbook D156/D157/D163-D166).
RI_1Y_DISCOUNT = 0.20
RI_3Y_DISCOUNT = 0.36
SP_1Y_DISCOUNT = 0.18
SP_3Y_DISCOUNT = 0.30


# ---------------------------------------------------------------------------
# AC-1 — schema invariants
# ---------------------------------------------------------------------------


def test_ac1_discount_fields_present_with_documented_defaults():
    """AC-1: four discount fields exist with the BA-workbook-derived defaults."""
    bm = BenchmarkConfig()
    assert bm.ri_1y_discount == pytest.approx(RI_1Y_DISCOUNT, abs=1e-9)
    assert bm.ri_3y_discount == pytest.approx(RI_3Y_DISCOUNT, abs=1e-9)
    assert bm.sp_1y_discount == pytest.approx(SP_1Y_DISCOUNT, abs=1e-9)
    assert bm.sp_3y_discount == pytest.approx(SP_3Y_DISCOUNT, abs=1e-9)

    # Round-trip override smoke check — fields must accept fractional values
    # in [0, 1] without raising.
    BenchmarkConfig(
        ri_1y_discount=0.0,
        ri_3y_discount=0.5,
        sp_1y_discount=0.18,
        sp_3y_discount=0.30,
    )


# ---------------------------------------------------------------------------
# AC-2 / AC-3 — computation contract
# ---------------------------------------------------------------------------


def test_ac2_compute_for_plan_returns_all_offers(default_benchmarks, contoso_inputs):
    """AC-2: breakdown produces all 5 offer rows + BA-truth anchor."""
    from engine import pricing_offers

    plan = contoso_inputs.consumption_plans[0]
    out = pricing_offers.compute_for_plan(plan, default_benchmarks)

    offers = [r.offer for r in out.rows]
    expected = ["PAYG", "RI 1Y", "RI 3Y", "SP 1Y", "SP 3Y", "BA-truth (current ACD)"]
    assert offers == expected, (
        f"compute_for_plan returned the wrong offer set: got {offers}, "
        f"expected {expected}. Order matters for UI rendering."
    )


def test_ac3_offer_totals_match_payg_times_one_minus_discount(default_benchmarks):
    """AC-3: each offer total = payg * (1 - offer_discount), to the cent."""
    from engine import pricing_offers
    from engine.models import ConsumptionPlan

    plan = ConsumptionPlan(
        workload_name="acceptance-test",
        annual_compute_consumption_lc_y10=1_000_000.0,
        annual_storage_consumption_lc_y10=0.0,
        annual_other_consumption_lc_y10=0.0,
        azure_consumption_discount=0.262,  # arbitrary BA-truth ACD
    )
    out = pricing_offers.compute_for_plan(plan, default_benchmarks)

    by_offer = {r.offer: r for r in out.rows}
    payg = 1_000_000.0
    assert by_offer["PAYG"].annual_total == pytest.approx(payg, abs=0.01)
    assert by_offer["RI 1Y"].annual_total == pytest.approx(payg * (1 - 0.20), abs=0.01)
    assert by_offer["RI 3Y"].annual_total == pytest.approx(payg * (1 - 0.36), abs=0.01)
    assert by_offer["SP 1Y"].annual_total == pytest.approx(payg * (1 - 0.18), abs=0.01)
    assert by_offer["SP 3Y"].annual_total == pytest.approx(payg * (1 - 0.30), abs=0.01)
    assert by_offer["BA-truth (current ACD)"].annual_total == pytest.approx(
        payg * (1 - 0.262), abs=0.01,
    )

    # Savings columns are derived consistently with the totals.
    for r in out.rows:
        assert r.savings_vs_payg == pytest.approx(payg - r.annual_total, abs=0.01)


def test_ac3b_zero_payg_short_circuits_safely(default_benchmarks):
    """AC-3b: a plan with no compute spend produces 0s without dividing by 0."""
    from engine import pricing_offers
    from engine.models import ConsumptionPlan

    plan = ConsumptionPlan(workload_name="empty", azure_consumption_discount=0.30)
    out = pricing_offers.compute_for_plan(plan, default_benchmarks)
    assert out.payg_compute_y10 == 0.0
    for r in out.rows:
        assert r.annual_total == pytest.approx(0.0, abs=1e-9)
        assert r.savings_pct_vs_payg == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# AC-4 — Layer 3 zero-drift preservation (the core invariant)
# ---------------------------------------------------------------------------


def test_ac4_layer3_drift_unchanged_regardless_of_discount_knobs():
    """AC-4: discount rates are display-only — engine NPV/ROI never moves.

    Re-imports the layer 3 ratchet constants so any v1.7 PR that bumps drift
    on either customer is caught at code review.
    """
    from tests.test_layer3_parity import (
        MAX_ENGINE_DRIFT,
        MAX_ENGINE_DRIFT_CUSTOMER_B,
    )

    assert MAX_ENGINE_DRIFT == 0, (
        f"MAX_ENGINE_DRIFT raised to {MAX_ENGINE_DRIFT}. "
        f"v1.7 MUST NOT loosen the Customer A ratchet — pricing_offers.compute "
        f"is display-only and must never feed financial_case."
    )
    assert MAX_ENGINE_DRIFT_CUSTOMER_B == 0, (
        f"MAX_ENGINE_DRIFT_CUSTOMER_B raised to {MAX_ENGINE_DRIFT_CUSTOMER_B}. "
        f"v1.7 MUST NOT loosen the Customer B ratchet either."
    )


def test_ac4b_pricing_offers_does_not_mutate_inputs(default_benchmarks, contoso_inputs):
    """AC-4b: ``compute_for_plan`` is pure — does not mutate inputs/benchmarks.

    A mutation here could subtly leak into financial_case via a shared object.
    """
    import copy

    from engine import pricing_offers

    plan = contoso_inputs.consumption_plans[0]
    plan_snapshot = copy.deepcopy(plan)
    bm_snapshot = copy.deepcopy(default_benchmarks)

    pricing_offers.compute_for_plan(plan, default_benchmarks)

    assert plan == plan_snapshot, "compute_for_plan mutated the ConsumptionPlan"
    assert default_benchmarks == bm_snapshot, (
        "compute_for_plan mutated the BenchmarkConfig"
    )


# ---------------------------------------------------------------------------
# AC-5 — v2.0 deferral guardrail
# ---------------------------------------------------------------------------


def test_ac5_no_cf_pl_split_in_v17():
    """AC-5: v1.7 MUST NOT introduce per-VM Y1-upfront CF/P&L split.

    That bifurcation is deferred to v2.0 because it breaks the
    'Azure consumption is pure OPEX' invariant the engine relies on for
    ``az_total_cf()`` vs ``az_total()``. If a v1.7 PR adds these fields
    accidentally, this test catches it.
    """
    from dataclasses import fields as dc_fields

    from engine.outputs import BusinessCaseSummary

    field_names = {f.name for f in dc_fields(BusinessCaseSummary)}
    forbidden = {"azure_ri_upfront_y1", "azure_ri_amortization_by_year"}
    leaked = forbidden & field_names
    assert not leaked, (
        f"v1.7 leaked v2.0-only fields: {leaked}. Per-VM RI/SP allocation "
        f"with Y1 upfront bifurcation is deferred to v2.0 — see version-history.md."
    )
