"""
v1.6 Test Scaffolding — Configurable Terminal Value
====================================================

Pre-built parameterized parity tests for the v1.6 ``tv_method`` enum and
``tv_floor_at_zero`` flag. **All tests in this file are SKIPPED today**
because the underlying ``BenchmarkConfig`` fields do not yet exist.

Why pre-build the scaffold:

* The acceptance criteria for v1.6 are already known from the
  May 2026 risk analysis (see ``version-history.md`` Roadmap section).
* When the engine PR lands, these tests light up automatically — no
  scrambling to design tests under merge pressure.
* The skip messages are PR-grep-able: a contributor implementing v1.6
  can run ``pytest tests/test_v16_tv_method_scaffold.py -v`` and see
  the exact contract the new fields must satisfy.

Acceptance criteria captured here:

  AC-1  ``tv_method`` enum exists on ``BenchmarkConfig`` with values
        ``"gordon" | "exit_multiple" | "none"``.
  AC-2  Default ``tv_method == "gordon"`` — back-compat invariant.
  AC-3  ``tv_method == "gordon"`` produces *exactly* the current
        ``_terminal_value`` output.
  AC-4  ``tv_method == "none"`` returns 0 (no perpetuity contribution).
  AC-5  ``tv_method == "exit_multiple"`` returns
        ``cf_last × benchmarks.tv_exit_multiple`` (no discounting in
        the helper itself; outputs.compute() handles PV).
  AC-6  ``tv_floor_at_zero=True`` clips negative TV to 0.
  AC-7  Defaults preserve Layer 3 parity (zero drift) on Customer A.

When v1.6 ships:
  1. Remove the ``pytest.importorskip`` / ``hasattr`` skips below.
  2. Run ``pytest tests/test_v16_tv_method_scaffold.py -v`` — all 6
     tests must pass.
  3. Run the full Layer 3 ratchet to confirm AC-7
     (``pytest tests/test_layer3_parity.py``).
"""

from __future__ import annotations

import pytest


def _has_tv_method() -> bool:
    """Detect whether v1.6 fields have landed on BenchmarkConfig."""
    from engine.models import BenchmarkConfig
    return hasattr(BenchmarkConfig, "tv_method") or "tv_method" in (
        BenchmarkConfig.model_fields if hasattr(BenchmarkConfig, "model_fields") else {}
    )


def _has_tv_floor() -> bool:
    from engine.models import BenchmarkConfig
    return "tv_floor_at_zero" in (
        BenchmarkConfig.model_fields if hasattr(BenchmarkConfig, "model_fields") else {}
    )


# ---------------------------------------------------------------------------
# AC-1 / AC-2 — schema invariants
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_tv_method(), reason="v1.6 not landed: BenchmarkConfig.tv_method missing"
)
def test_ac1_tv_method_enum_present():
    """AC-1: ``tv_method`` field exists with the documented enum values."""
    from engine.models import BenchmarkConfig

    bm = BenchmarkConfig()
    assert hasattr(bm, "tv_method")
    # Should accept all three documented values without raising
    for method in ("gordon", "exit_multiple", "none"):
        BenchmarkConfig(tv_method=method)


@pytest.mark.skipif(
    not _has_tv_method(), reason="v1.6 not landed: BenchmarkConfig.tv_method missing"
)
def test_ac2_default_is_gordon():
    """AC-2: default ``tv_method`` MUST be ``"gordon"`` for back-compat."""
    from engine.models import BenchmarkConfig

    assert BenchmarkConfig().tv_method == "gordon", (
        "Changing the default tv_method silently breaks Layer 3 parity for "
        "every existing customer. Default MUST stay 'gordon'."
    )


# ---------------------------------------------------------------------------
# AC-3 / AC-4 / AC-5 — terminal-value semantics
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_tv_method(), reason="v1.6 not landed"
)
def test_ac3_gordon_matches_legacy_formula(default_benchmarks):
    """AC-3: 'gordon' must reproduce ``_terminal_value`` to the cent."""
    from engine.outputs import _terminal_value

    bm = default_benchmarks.model_copy(update={"tv_method": "gordon"})
    cf_last, wacc, g = 1_000_000.0, bm.wacc, bm.perpetual_growth_rate

    legacy = cf_last * (1 + g) / (wacc - g)
    actual = _terminal_value(cf_last, wacc, g)  # signature may grow a `bm=` param
    assert abs(actual - legacy) < 0.01, (
        f"'gordon' regressed the legacy Gordon Growth formula: "
        f"expected {legacy:,.2f} got {actual:,.2f}"
    )


@pytest.mark.skipif(
    not _has_tv_method(), reason="v1.6 not landed"
)
def test_ac4_none_returns_zero(default_benchmarks):
    """AC-4: ``tv_method='none'`` MUST contribute 0 to NPV."""
    from engine.outputs import _terminal_value

    bm = default_benchmarks.model_copy(update={"tv_method": "none"})
    # Signature is expected to grow a benchmarks/method-aware path.
    # Until then this test is skipped above.
    tv = _terminal_value(1_000_000.0, bm.wacc, bm.perpetual_growth_rate)  # call site TBD
    assert tv == 0.0, "tv_method='none' must short-circuit to 0"


@pytest.mark.skipif(
    not _has_tv_method(), reason="v1.6 not landed"
)
def test_ac5_exit_multiple_uses_configured_multiple(default_benchmarks):
    """AC-5: ``tv_method='exit_multiple'`` returns cf_last × multiple."""
    from engine.outputs import _terminal_value

    bm = default_benchmarks.model_copy(
        update={"tv_method": "exit_multiple", "tv_exit_multiple": 8.0}
    )
    cf_last = 1_000_000.0
    expected = cf_last * 8.0
    actual = _terminal_value(cf_last, bm.wacc, bm.perpetual_growth_rate)
    assert abs(actual - expected) < 0.01, (
        f"exit_multiple TV broken: expected {expected:,.2f} got {actual:,.2f}"
    )


# ---------------------------------------------------------------------------
# AC-6 — negative-TV floor
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_tv_floor(),
    reason="v1.6 not landed: BenchmarkConfig.tv_floor_at_zero missing",
)
def test_ac6_floor_clips_negative_tv(default_benchmarks):
    """AC-6: ``tv_floor_at_zero=True`` MUST clip a negative perpetuity to 0.

    Customer scenarios where Y10 savings dip negative (e.g. ECIF runoff
    plus ramp-down) currently produce a NEGATIVE Gordon TV, dragging NPV
    further down via a perpetual-loss assumption. The floor flag protects
    BAs from accidentally embedding that assumption.
    """
    from engine.outputs import _terminal_value

    bm = default_benchmarks.model_copy(update={"tv_floor_at_zero": True})
    cf_last = -500_000.0  # negative final-year cash flow
    tv = _terminal_value(cf_last, bm.wacc, bm.perpetual_growth_rate)
    assert tv >= 0.0, (
        f"tv_floor_at_zero=True did not clip negative TV (got {tv:,.2f}). "
        f"Negative perpetuity is almost never the right model — see risk analysis."
    )


# ---------------------------------------------------------------------------
# AC-7 — Layer 3 parity preservation under defaults
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_tv_method(), reason="v1.6 not landed"
)
def test_ac7_defaults_preserve_layer3_parity():
    """AC-7: Customer A 3-way audit MUST stay at zero engine drift when
    v1.6 is enabled with defaults (``tv_method='gordon'``, floor off).

    This test is a *thin marker* — the real ratchet lives in
    ``tests/test_layer3_parity.py``. We re-import its drift constant here
    so a v1.6 PR that bumps ``MAX_ENGINE_DRIFT`` is caught at code review.
    """
    from tests.test_layer3_parity import MAX_ENGINE_DRIFT

    assert MAX_ENGINE_DRIFT == 0, (
        f"MAX_ENGINE_DRIFT was raised to {MAX_ENGINE_DRIFT}. "
        f"v1.6 must not loosen the Layer 3 ratchet — Customer A parity is "
        f"the contract that lets us ship engine refactors safely."
    )
