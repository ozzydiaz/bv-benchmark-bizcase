"""
v1.7 Test Scaffolding — RI/SP-Blended Effective ACD
=====================================================

Pre-built parity tests for the v1.7 ``use_ri_sp_blending`` opt-in flag and
the family-blended ``effective_acd`` derivation. **All tests in this file
are SKIPPED today** because the underlying ``BenchmarkConfig`` field does
not yet exist.

Why pre-build the scaffold:

* The May 2026 risk analysis (see ``version-history.md`` Roadmap section)
  enumerated 8 risks for the RI/SP refactor; the algebra is well-defined
  and the contract surface is small.
* When the engine PR lands, these tests light up automatically — and any
  drift versus the BA workbook's pre-computed ``new_acd`` cell becomes a
  hard CI failure rather than a tribal-knowledge worry.

Acceptance criteria captured here:

  AC-1  ``use_ri_sp_blending`` flag exists on ``BenchmarkConfig`` and
        defaults to ``False`` (opt-in until two-customer baseline
        confirms).
  AC-2  When the flag is OFF, ``financial_case.az_total()`` is byte-
        identical to today's PAYG-only behaviour. (Back-compat proof.)
  AC-3  When the flag is ON, the engine uses
        ``effective_acd = paygo*0 + ri_1y*0.20 + ri_3y*0.36
                         + sp_1y*0.18 + sp_3y*0.30``
        as documented in the BA workbook D156/D157/D163-D166 model.
  AC-4  Layer 3 parity (Customer A) MUST stay at zero engine drift
        regardless of flag value, because Customer A's BA workbook
        already pre-computes the blended ACD into ``cp.azure_consumption_discount``.
  AC-5  Per-VM RI/SP allocation with Y1 upfront bifurcation is
        explicitly DEFERRED to v2.0. v1.7 is a display/derivation
        shift only; it MUST NOT split CF vs P&L.

When v1.7 ships:
  1. Remove the ``hasattr`` skips below.
  2. Run ``pytest tests/test_v17_ri_sp_blending_scaffold.py -v`` —
     all 5 tests must pass.
  3. Run the full Layer 3 ratchet for AC-4 verification
     (``pytest tests/test_layer3_parity.py``).
"""

from __future__ import annotations

import pytest


# Documented per-family discount rates from the May 2026 risk analysis.
# These are placeholder anchors — v1.7 implementation may source them from
# benchmarks_default.yaml. Update both this file AND the YAML in lock-step.
RI_1Y_DISCOUNT = 0.20
RI_3Y_DISCOUNT = 0.36
SP_1Y_DISCOUNT = 0.18
SP_3Y_DISCOUNT = 0.30


def _has_blending_flag() -> bool:
    from engine.models import BenchmarkConfig
    return "use_ri_sp_blending" in (
        BenchmarkConfig.model_fields if hasattr(BenchmarkConfig, "model_fields") else {}
    )


# ---------------------------------------------------------------------------
# AC-1 / AC-2 — opt-in default and back-compat invariant
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_blending_flag(),
    reason="v1.7 not landed: BenchmarkConfig.use_ri_sp_blending missing",
)
def test_ac1_blending_flag_default_off():
    """AC-1: ``use_ri_sp_blending`` exists and defaults to ``False``.

    Defaulting to ON would silently shift every existing customer's
    Azure cost forecast — same risk pattern as the v1.6 TV default.
    """
    from engine.models import BenchmarkConfig

    bm = BenchmarkConfig()
    assert hasattr(bm, "use_ri_sp_blending")
    assert bm.use_ri_sp_blending is False, (
        "Default MUST be False — opt-in only until two-customer baseline."
    )


@pytest.mark.skipif(
    not _has_blending_flag(), reason="v1.7 not landed"
)
def test_ac2_flag_off_preserves_legacy_payg(contoso_inputs, default_benchmarks):
    """AC-2: With flag OFF, ``az_total`` matches pre-v1.7 behaviour.

    We compute ``az_total`` twice — once at HEAD and once with the flag
    explicitly forced OFF — and assert byte-identity. Drift here means
    a v1.7 PR accidentally changed a default code path.
    """
    from engine import status_quo, retained_costs, depreciation, financial_case

    bm_off = default_benchmarks.model_copy(update={"use_ri_sp_blending": False})

    sq = status_quo.compute(contoso_inputs, bm_off)
    rc = retained_costs.compute(contoso_inputs, bm_off, sq)
    dp = depreciation.compute(contoso_inputs, bm_off)
    fc_off = financial_case.compute(contoso_inputs, bm_off, sq, rc, dp)

    sq2 = status_quo.compute(contoso_inputs, default_benchmarks)
    rc2 = retained_costs.compute(contoso_inputs, default_benchmarks, sq2)
    dp2 = depreciation.compute(contoso_inputs, default_benchmarks)
    fc_legacy = financial_case.compute(contoso_inputs, default_benchmarks, sq2, rc2, dp2)

    for yr in range(11):
        assert abs(fc_off.az_total()[yr] - fc_legacy.az_total()[yr]) < 0.01, (
            f"Y{yr} drift between flag=False and HEAD: "
            f"{fc_off.az_total()[yr]:,.2f} vs {fc_legacy.az_total()[yr]:,.2f}"
        )


# ---------------------------------------------------------------------------
# AC-3 — blended ACD formula
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_blending_flag(), reason="v1.7 not landed"
)
def test_ac3_blended_acd_matches_documented_weighted_average():
    """AC-3: blended ACD = weighted sum of per-family discount rates.

    The exact mix taken from the BA workbook ``Customer Information``
    cells D163-D166 (paygo/ri_1y/ri_3y/sp_1y/sp_3y respectively).
    """
    # Once v1.7 lands, replace this import with the actual derivation helper.
    from engine.models import AzureRunRate  # noqa: F401  (sanity import)

    mix = {"paygo": 0.10, "ri_1y": 0.20, "ri_3y": 0.40, "sp_1y": 0.10, "sp_3y": 0.20}
    expected = (
        mix["paygo"] * 0.0
        + mix["ri_1y"] * RI_1Y_DISCOUNT
        + mix["ri_3y"] * RI_3Y_DISCOUNT
        + mix["sp_1y"] * SP_1Y_DISCOUNT
        + mix["sp_3y"] * SP_3Y_DISCOUNT
    )
    # 0*0.10 + 0.20*0.20 + 0.40*0.36 + 0.10*0.18 + 0.20*0.30 = 0.262
    assert abs(expected - 0.262) < 1e-9

    # When v1.7 ships:
    #   from engine.consumption_builder import compute_blended_acd
    #   actual = compute_blended_acd(mix, benchmarks)
    #   assert abs(actual - expected) < 1e-6
    pytest.skip("Awaiting compute_blended_acd() helper from v1.7 implementation.")


# ---------------------------------------------------------------------------
# AC-4 — Layer 3 parity preservation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_blending_flag(), reason="v1.7 not landed"
)
def test_ac4_layer3_drift_unchanged():
    """AC-4: Layer 3 ratchet remains at zero engine drift.

    Customer A's BA workbook already rolls per-family RI/SP discounts up
    into ``cp.azure_consumption_discount`` (the flat ACD). v1.7 must
    therefore produce the same numbers as today regardless of flag value
    — anything else is a regression.
    """
    from tests.test_layer3_parity import MAX_ENGINE_DRIFT

    assert MAX_ENGINE_DRIFT == 0, (
        f"MAX_ENGINE_DRIFT raised to {MAX_ENGINE_DRIFT}. "
        f"v1.7 MUST NOT loosen the ratchet — Customer A is the contract."
    )


# ---------------------------------------------------------------------------
# AC-5 — v2.0 deferral guardrail
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_blending_flag(), reason="v1.7 not landed"
)
def test_ac5_no_cf_pl_split_in_v17():
    """AC-5: v1.7 MUST NOT introduce per-VM Y1-upfront CF/P&L split.

    That bifurcation is deferred to v2.0 because it breaks the
    'Azure consumption is pure OPEX' invariant the engine relies on for
    ``az_total_cf()`` vs ``az_total()``. If a v1.7 PR adds these fields
    accidentally, this test catches it.
    """
    from engine.models import BusinessCaseSummary

    fields = (
        BusinessCaseSummary.model_fields if hasattr(BusinessCaseSummary, "model_fields") else {}
    )
    forbidden = {"azure_ri_upfront_y1", "azure_ri_amortization_by_year"}
    leaked = forbidden & set(fields)
    assert not leaked, (
        f"v1.7 leaked v2.0-only fields: {leaked}. Per-VM RI/SP allocation "
        f"with Y1 upfront bifurcation is deferred to v2.0 — see version-history.md."
    )
