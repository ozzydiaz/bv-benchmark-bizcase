"""
Layer 3 Parity Audit — CI test.
=================================

Validates the 3-way audit framework in
``training/replicas/layer3_judge.py`` and the golden extractor in
``training/replicas/layer3_golden_extractor.py``.

This file establishes the trust harness. Replica + engine implementations
are wired in subsequent commits; this test currently locks in:

* The golden extractor pulls every BA-authoritative Customer A value
  to within $0.01 of the spreadsheet (deterministic).
* The auditor's tiered tolerance bands behave correctly under
  adversarial drift profiles (PERFECT, DRIFT, BIG_BREAK).
* The verdict logic returns the expected severity for each profile.

When the Layer 3 BA replica and the engine bridge are wired in, this
file gains the actual end-to-end parity assertion (replica vs BA, engine
vs BA) — the harness is already in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from training.replicas.layer3_golden_extractor import (
    extract_layer3_golden,
    flatten_golden,
)
from training.replicas.layer3_judge import (
    absolute_tolerance,
    audit,
    tier_label,
    verdict,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CUSTOMER_A_WORKBOOK = REPO_ROOT / "customer_a_BV_Benchmark_Business_Case_v6.xlsm"
CUSTOMER_B_WORKBOOK = REPO_ROOT / "customer_b_BV_Benchmark_Business_Case_v6.xlsm"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden():
    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip(f"Customer A workbook not found at {CUSTOMER_A_WORKBOOK}")
    return extract_layer3_golden(str(CUSTOMER_A_WORKBOOK))


@pytest.fixture(scope="module")
def flat_oracle(golden):
    return flatten_golden(golden)


# ---------------------------------------------------------------------------
# Tolerance band tests (no workbook required)
# ---------------------------------------------------------------------------


def test_tolerance_zero_is_exact():
    assert absolute_tolerance(0.0) == 0.005
    assert tier_label(0.0) == "EXACT"


def test_tolerance_rates_are_exact():
    """ROI = -0.47, WACC = 0.07 must demand exact match."""
    assert absolute_tolerance(0.07) == 0.005
    assert absolute_tolerance(-0.4703) == 0.005
    assert tier_label(0.07) == "EXACT"


def test_tolerance_small_dollars():
    """$10 — penny tolerance. $5,000 — dollar tolerance."""
    assert absolute_tolerance(10.0) == 0.01
    assert absolute_tolerance(5_000.0) == 1.00
    assert tier_label(10.0) == "<$100"
    assert tier_label(5_000.0) == "<$10K"


def test_tolerance_mid_dollars():
    """$100K — 0.1% tolerance ⇒ $100."""
    assert absolute_tolerance(100_000.0) == pytest.approx(100.0)
    assert tier_label(100_000.0) == "<$1M"


def test_tolerance_large_dollars():
    """$10M — 1% tolerance ⇒ $100K."""
    assert absolute_tolerance(10_000_000.0) == pytest.approx(100_000.0)
    assert tier_label(10_000_000.0) == "≥$1M"


def test_tolerance_strictly_monotonic():
    """Sanity: tolerance never decreases as |value| grows."""
    samples = [0.0, 0.5, 50.0, 5_000.0, 500_000.0, 50_000_000.0]
    tols = [absolute_tolerance(s) for s in samples]
    assert tols == sorted(tols)


# ---------------------------------------------------------------------------
# Golden extractor tests (require Customer A workbook)
# ---------------------------------------------------------------------------


def test_extractor_pulls_395_cells(flat_oracle):
    """Locks in the cardinality of the oracle so we notice if cells drift."""
    assert len(flat_oracle) == 395


def test_extractor_known_anchors(golden):
    """Every cell in this list was visually inspected in the BA workbook."""
    anchors = {
        ("status_quo", "server_depreciation", 0): 995_995.4525,
        ("status_quo", "server_depreciation", 10): 1_167_425.0856,
        ("status_quo", "total_on_prem_cost", 10): 15_002_479.4179,
        ("cash_flow", "savings", 10): 1_995_190.3773,
        ("cash_flow", "az_total", 10): 13_123_084.3307,
    }
    for (block_name, field_name, year), expected in anchors.items():
        block = getattr(golden, block_name)
        series = getattr(block, field_name)
        actual = series.values[year]
        assert actual == pytest.approx(expected, abs=0.01), (
            f"{block_name}.{field_name} Y{year}: expected {expected:,.4f}, got {actual:,.4f}"
        )


def test_extractor_headlines(golden):
    h = golden.headline
    assert h.npv_sq_10y.value == pytest.approx(96_257_591.60, abs=0.01)
    assert h.project_npv_excl_tv_10y.value == pytest.approx(2_569_869.66, abs=0.01)
    assert h.project_npv_excl_tv_5y.value == pytest.approx(-1_793_194.08, abs=0.01)
    assert h.roi_5y_cf.value == pytest.approx(-0.4703, abs=0.0001)
    assert h.payback_years.value == pytest.approx(0.0, abs=0.01)


def test_extractor_provenance(flat_oracle):
    """Every extracted value must carry sheet+address provenance."""
    for label, ref, _val in flat_oracle:
        assert ref.sheet, f"{label}: missing sheet"
        assert ref.address, f"{label}: missing cell address"
        assert ref.label, f"{label}: missing human label"


# ---------------------------------------------------------------------------
# Adversarial auditor tests
# ---------------------------------------------------------------------------


def test_audit_perfect_replica_and_engine_pass(golden, flat_oracle):
    perfect = {label: val for label, _ref, val in flat_oracle}
    report = audit(golden, replica_values=perfect, engine_values=dict(perfect))
    v = verdict(report)
    assert v.overall_clean
    assert report.replica_pass_count == report.total_cells
    assert report.engine_pass_count == report.total_cells


def test_audit_half_percent_drift_fails_small_values(golden, flat_oracle):
    """0.5% drift ⇒ small values fail; large values pass."""
    drifted = {label: val * 1.005 for label, _ref, val in flat_oracle}
    report = audit(golden, replica_values=drifted, engine_values=drifted)
    v = verdict(report)
    assert not v.replica_clean, "Drift must fail somewhere"

    # Specifically: any |value| < $10K under 0.5% drift creates Δ > 0.5*tol.
    failing = [a for a in report.audits if a.replica_passes is False]
    assert len(failing) > 0
    # And there exists at least one large-value cell that passed.
    passing_big = [a for a in report.audits if a.tier == "≥$1M" and a.replica_passes]
    assert passing_big


def test_audit_isolates_single_engine_break(golden, flat_oracle):
    """Breaking one cell by $1M must surface exactly that cell."""
    perfect = {label: val for label, _ref, val in flat_oracle}
    broken = dict(perfect)
    target = "headline.project_npv_excl_tv_10y"
    broken[target] = perfect[target] + 1_000_000.0

    report = audit(golden, replica_values=perfect, engine_values=broken)
    v = verdict(report)
    assert v.replica_clean, "Replica unchanged ⇒ must remain clean"
    assert not v.engine_clean
    assert report.engine_fail_count == 1

    offender = next(a for a in report.audits if not a.engine_passes)
    assert offender.label == target
    assert offender.delta_ac == pytest.approx(1_000_000.0)


def test_audit_zero_must_be_exact(golden, flat_oracle):
    """For BA cells with value 0, even a small replica delta must FAIL."""
    perfect = {label: val for label, _ref, val in flat_oracle}
    target = next(label for label, _r, val in flat_oracle if val == 0.0)
    broken = dict(perfect)
    broken[target] = 0.10  # ten cents — far above the $0.005 tolerance

    report = audit(golden, replica_values=broken, engine_values=perfect)
    offender = next(a for a in report.audits if a.label == target)
    assert offender.replica_passes is False, (
        f"BA cell {target} = $0; replica = $0.10 must FAIL exact-tier audit"
    )


def test_audit_unknown_replica_does_not_false_pass(golden):
    """Empty replica dict must produce 'unknown' status — not pass."""
    report = audit(golden, replica_values={}, engine_values={})
    v = verdict(report)
    assert not v.replica_clean
    assert not v.engine_clean
    assert report.replica_unknown_count == report.total_cells
    assert report.engine_unknown_count == report.total_cells
    assert report.replica_pass_count == 0
    assert report.engine_pass_count == 0


# ---------------------------------------------------------------------------
# Render smoke
# ---------------------------------------------------------------------------


def test_render_full_report_is_markdown(golden, flat_oracle):
    from training.replicas.layer3_judge import render_full_report

    perfect = {label: val for label, _ref, val in flat_oracle}
    report = audit(golden, replica_values=perfect, engine_values=perfect)
    md = render_full_report(report)
    assert "# Layer 3 Parity Audit" in md
    assert "Customer" in md or "Contoso" in md or report.customer_name in md
    assert "Replica vs BA" in md
    assert "Engine vs BA" in md


# ---------------------------------------------------------------------------
# Status Quo replica end-to-end parity (Customer A workbook)
# ---------------------------------------------------------------------------


def test_status_quo_replica_perfect_parity_customer_a(golden):
    """
    The Status Quo replica MUST match Customer A's workbook EXACTLY for all
    231 Status Quo cells (19 P&L lines × 11 years + 11 sq_estimation scalars).

    This is the trust-building anchor: if a single Status Quo cell drifts,
    the entire downstream financial case is contaminated.
    """
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
    )
    from training.replicas.layer3_status_quo import compute_status_quo

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required for end-to-end parity")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    replica = compute_status_quo(client, bm)

    report = audit(golden, replica_values=replica, engine_values=None)

    # Every Status Quo cell the replica covers MUST pass.
    sq_cells_covered = [a for a in report.audits if a.label.startswith(("status_quo.", "sq_estimation."))
                        and a.replica_passes is not None]
    failing = [a for a in sq_cells_covered if a.replica_passes is False]

    assert not failing, (
        f"{len(failing)} Status Quo cells failed parity:\n"
        + "\n".join(
            f"  {a.label} @ {a.cell_ref}: BA={a.ba_value:,.2f} replica={a.replica_value:,.2f} "
            f"Δ={a.delta_ab:,.4f} (tol={a.tolerance:,.4f})"
            for a in failing[:10]
        )
    )

    # Replica must cover all 19 line items × 11 years + sq_estimation scalars
    assert len(sq_cells_covered) >= 220, (
        f"Status Quo replica covers only {len(sq_cells_covered)} cells, expected ≥220"
    )


def test_status_quo_replica_keys_match_extractor_labels(golden):
    """
    Defensive: the replica's output dict keys must align EXACTLY with the
    auditor's expected keys. Drift here would cause silent NOT-IMPLEMENTED.
    """
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
    )
    from training.replicas.layer3_status_quo import compute_status_quo

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    replica = compute_status_quo(client, bm)

    expected_labels = {
        f"status_quo.{line}.Y{yr}"
        for line in [
            "Server Depreciation", "Server HW Maintenance",
            "Storage Depreciation", "Storage Maintenance",
            "Storage Backup", "Storage DR",
            "NW+Fitout Depreciation", "Network HW Maintenance",
            "Bandwidth Costs", "DC Lease (Space)", "DC Power",
            "Virtualization Licenses", "Windows Server Licenses",
            "SQL Server Licenses", "Windows Server ESU", "SQL Server ESU",
            "Backup Licenses", "Disaster Recovery Licenses",
            "IT Admin Staff", "Total On-Prem Cost",
        ]
        for yr in range(11)
    }
    missing = expected_labels - set(replica.keys())
    assert not missing, f"Replica missing labels: {sorted(missing)[:5]}..."


# ---------------------------------------------------------------------------
# SQ Cash Flow + NPV replica end-to-end parity (Customer A workbook)
# ---------------------------------------------------------------------------


def test_sq_cash_flow_replica_perfect_parity_customer_a(golden):
    """
    The SQ cash-flow view + SQ NPV scalars MUST match Customer A's workbook
    exactly (42 additional cells beyond the Status Quo P&L block).
    """
    from training.replicas.layer3_cash_flow import compute_status_quo_cash_flow_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
    )
    from training.replicas.layer3_status_quo import compute_status_quo

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required for end-to-end parity")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    replica: dict = {}
    replica.update(compute_status_quo(client, bm))
    replica.update(compute_status_quo_cash_flow_dict(client, bm))

    report = audit(golden, replica_values=replica, engine_values=None)

    # Every cell the replica covers MUST pass.
    relevant_prefixes = (
        "status_quo.",
        "sq_estimation.",
        "cash_flow.SQ ",
        "headline.npv_sq_",
        "detailed_npv.",
    )
    cells_covered = [
        a for a in report.audits
        if a.label.startswith(relevant_prefixes) and a.replica_passes is not None
    ]
    failing = [a for a in cells_covered if a.replica_passes is False]

    assert not failing, (
        f"{len(failing)} cells failed parity:\n"
        + "\n".join(
            f"  {a.label} @ {a.cell_ref}: BA={a.ba_value:,.4f} replica={a.replica_value:,.4f} "
            f"Δ={a.delta_ab:,.4f} (tol={a.tolerance:,.4f})"
            for a in failing[:10]
        )
    )

    # Replica must cover Status Quo (~231) + Cash Flow (33) + headlines (2) + NPV (7) = 273
    assert len(cells_covered) >= 270, (
        f"Replica covers only {len(cells_covered)} cells, expected ≥270"
    )


def test_sq_npv_matches_workbook_anchors(golden):
    """
    Defensive: hard-coded NPV anchors for Customer A must match exactly.

    These values were independently verified by hand from the Customer A
    workbook to prevent silent drift in the NPV math.
    """
    from training.replicas.layer3_cash_flow import compute_status_quo_cash_flow_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
    )

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    replica = compute_status_quo_cash_flow_dict(client, bm)

    # Customer A SQ NPV anchors (frozen from finalised workbook)
    assert replica["headline.npv_sq_10y"] == pytest.approx(96_257_591.60, abs=1.0)
    assert replica["headline.npv_sq_5y"] == pytest.approx(53_803_461.30, abs=1.0)
    assert replica["detailed_npv.terminal_value_10y_raw"] == pytest.approx(
        389_295_573.73, abs=10.0
    )
    assert replica["detailed_npv.wacc"] == pytest.approx(0.07, abs=1e-9)
    assert replica["detailed_npv.perpetual_growth_rate"] == pytest.approx(0.03, abs=1e-9)


# ---------------------------------------------------------------------------
# Azure Case + retained costs replica (Customer A workbook)
# ---------------------------------------------------------------------------


def test_az_case_replica_perfect_parity_customer_a(golden):
    """
    The Azure Case cash-flow + retained-costs replica MUST match Customer A's
    workbook exactly across all AZ-side cells (CAPEX/OPEX/Consumption/Migration/
    MS Funding/Total + Savings/Delta/Rate + AZ NPV scalars).

    101 cells total; any drift breaks the trust chain feeding into Project NPV
    and the displayed ROI/Payback in Step 10.
    """
    from training.replicas.layer3_azure_case import compute_azure_case_dict
    from training.replicas.layer3_cash_flow import compute_status_quo_cash_flow_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )
    from training.replicas.layer3_status_quo import compute_status_quo

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required for end-to-end parity")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_A_WORKBOOK))

    replica: dict = {}
    replica.update(compute_status_quo(client, bm))
    replica.update(compute_status_quo_cash_flow_dict(client, bm))
    replica.update(compute_azure_case_dict(client, bm, cons))

    report = audit(golden, replica_values=replica, engine_values=None)

    # Every AZ-side cell the replica covers MUST pass.
    az_prefixes = (
        "cash_flow.AZ ",
        "cash_flow.Savings",
        "cash_flow.CF Delta",
        "cash_flow.CF Rate",
        "headline.npv_az_",
    )
    az_cells = [
        a for a in report.audits
        if a.label.startswith(az_prefixes) and a.replica_passes is not None
    ]
    failing = [a for a in az_cells if a.replica_passes is False]

    assert not failing, (
        f"{len(failing)} Azure-case cells failed parity:\n"
        + "\n".join(
            f"  {a.label} @ {a.cell_ref}: BA={a.ba_value:,.4f} replica={a.replica_value:,.4f} "
            f"Δ={a.delta_ab:,.4f} (tol={a.tolerance:,.4f})"
            for a in failing[:10]
        )
    )

    # 9 series × 11 years + 2 NPV scalars = 101 AZ cells expected
    assert len(az_cells) >= 99, (
        f"Replica covers only {len(az_cells)} AZ cells, expected ≥99"
    )


def test_az_case_replica_keys_match_extractor_labels():
    """
    Defensive: replica AZ keys must align EXACTLY with the auditor's
    expected labels. Drift here would cause silent NOT-IMPLEMENTED.
    """
    from training.replicas.layer3_azure_case import compute_azure_case_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_A_WORKBOOK))
    replica = compute_azure_case_dict(client, bm, cons)

    expected_labels = {
        f"cash_flow.{line}.Y{yr}"
        for line in [
            "AZ CAPEX", "AZ OPEX", "AZ Consumption", "AZ Migration",
            "AZ MS Funding", "AZ Total CF", "Savings (SQ-AZ)",
            "CF Delta (AZ-SQ)", "CF Rate",
        ]
        for yr in range(11)
    } | {"headline.npv_az_10y", "headline.npv_az_5y"}

    missing = expected_labels - set(replica.keys())
    assert not missing, f"Replica missing labels: {sorted(missing)[:5]}..."


def test_az_npv_matches_workbook_anchors(golden):
    """Defensive: hard-coded NPV AZ anchors for Customer A must match."""
    from training.replicas.layer3_azure_case import compute_azure_case_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_A_WORKBOOK))
    replica = compute_azure_case_dict(client, bm, cons)

    assert replica["headline.npv_az_10y"] == pytest.approx(
        golden.headline.npv_az_10y.value, abs=1.0
    )
    assert replica["headline.npv_az_5y"] == pytest.approx(
        golden.headline.npv_az_5y.value, abs=1.0
    )
    # Customer A: AZ Total CF Y0 = SQ Total CF Y0 = 12,311,615.42
    assert replica["cash_flow.AZ Total CF.Y0"] == pytest.approx(12_311_615.42, abs=0.01)
    assert replica["cash_flow.AZ Total CF.Y10"] == pytest.approx(13_123_084.33, abs=0.01)
    # Savings, delta, rate at Y10
    assert replica["cash_flow.Savings (SQ-AZ).Y10"] == pytest.approx(1_995_190.38, abs=0.01)
    assert replica["cash_flow.CF Rate.Y10"] == pytest.approx(-0.13, abs=0.01)


# ---------------------------------------------------------------------------
# Project NPV / 5Y CF Payback replica (Customer A workbook) — closes the oracle
# ---------------------------------------------------------------------------


def test_project_npv_replica_perfect_parity_customer_a(golden):
    """
    Project NPV + 5Y CF Payback replica MUST match Customer A's workbook
    exactly across all remaining 21 cells (12 headline scalars + 9 five_payback
    scalars).

    Together with Steps 7-9 this lifts replica coverage to 395/395 = 100% of
    the Layer 3 oracle.
    """
    from training.replicas.layer3_azure_case import compute_azure_case_dict
    from training.replicas.layer3_cash_flow import compute_status_quo_cash_flow_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )
    from training.replicas.layer3_project_npv import compute_project_npv_dict
    from training.replicas.layer3_status_quo import compute_status_quo

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required for end-to-end parity")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_A_WORKBOOK))

    replica: dict = {}
    replica.update(compute_status_quo(client, bm))
    replica.update(compute_status_quo_cash_flow_dict(client, bm))
    replica.update(compute_azure_case_dict(client, bm, cons))
    replica.update(compute_project_npv_dict(client, bm, cons))

    report = audit(golden, replica_values=replica, engine_values=None)
    v = verdict(report)

    failing = [a for a in report.audits if a.replica_passes is False]
    assert v.replica_clean and not failing, (
        f"{len(failing)} cells failed parity:\n"
        + "\n".join(
            f"  {a.label} @ {a.cell_ref}: BA={a.ba_value:,.4f} replica={a.replica_value:,.4f} "
            f"Δ={a.delta_ab:,.4f} (tol={a.tolerance:,.4f})"
            for a in failing[:10]
        )
    )

    # 100% coverage — replica should match all 395 oracle cells.
    assert report.total_cells == 395
    assert report.replica_unknown_count == 0, (
        f"Replica should cover all 395 cells, but {report.replica_unknown_count} are uncovered"
    )
    assert report.replica_pass_count == 395


def test_project_npv_replica_keys_match_extractor_labels():
    """
    Defensive: replica project-NPV / five-payback keys must align exactly with
    the auditor's expected labels.
    """
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )
    from training.replicas.layer3_project_npv import compute_project_npv_dict

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_A_WORKBOOK))
    replica = compute_project_npv_dict(client, bm, cons)

    expected_labels = {
        "headline.terminal_value_10y", "headline.terminal_value_5y",
        "headline.project_npv_with_tv_10y", "headline.project_npv_with_tv_5y",
        "headline.project_npv_excl_tv_10y", "headline.project_npv_excl_tv_5y",
        "headline.roi_5y_cf", "headline.payback_years",
        "headline.y10_savings_10y_cf", "headline.y10_savings_5y_cf",
        "headline.y10_savings_rate_10y", "headline.y10_savings_rate_5y",
        "five_payback.infra_cost_reduction_npv",
        "five_payback.infra_admin_reduction_npv",
        "five_payback.total_benefits_npv",
        "five_payback.incremental_azure_npv",
        "five_payback.migration_npv",
        "five_payback.total_costs_npv",
        "five_payback.net_benefits_npv",
        "five_payback.roi_5y_cf",
        "five_payback.payback_years",
    }
    missing = expected_labels - set(replica.keys())
    assert not missing, f"Replica missing labels: {sorted(missing)}"


def test_project_npv_anchors_customer_a(golden):
    """Hard-coded anchors verified by hand from the finalised Customer A workbook."""
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )
    from training.replicas.layer3_project_npv import compute_project_npv_dict

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_A_WORKBOOK))
    replica = compute_project_npv_dict(client, bm, cons)

    # Customer A frozen anchors (Summary Financial Case + 5Y CF Payback)
    assert replica["headline.terminal_value_10y"] == pytest.approx(26_117_030.61, abs=1.0)
    assert replica["headline.project_npv_with_tv_10y"] == pytest.approx(28_686_900.27, abs=1.0)
    assert replica["headline.project_npv_excl_tv_10y"] == pytest.approx(2_569_869.66, abs=1.0)
    assert replica["headline.project_npv_excl_tv_5y"] == pytest.approx(-1_793_194.08, abs=1.0)
    assert replica["headline.roi_5y_cf"] == pytest.approx(-0.4703, abs=0.0001)
    assert replica["headline.payback_years"] == pytest.approx(0.0, abs=0.001)
    assert replica["five_payback.net_benefits_npv"] == pytest.approx(-1_793_194.08, abs=1.0)
    assert replica["five_payback.migration_npv"] == pytest.approx(-4_246_500.0, abs=0.01)
    assert replica["five_payback.total_benefits_npv"] == pytest.approx(43_294_028.26, abs=1.0)


def test_full_layer3_oracle_replica_clean_customer_a(golden):
    """
    Top-level smoke: end-to-end Customer A replica must be CLEAN against the
    full 395-cell oracle. This is the trust-anchor for downstream engine work
    (Steps 11-14).
    """
    from training.replicas.layer3_azure_case import compute_azure_case_dict
    from training.replicas.layer3_cash_flow import compute_status_quo_cash_flow_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )
    from training.replicas.layer3_project_npv import compute_project_npv_dict
    from training.replicas.layer3_status_quo import compute_status_quo

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required for end-to-end parity")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_A_WORKBOOK))

    replica: dict = {}
    replica.update(compute_status_quo(client, bm))
    replica.update(compute_status_quo_cash_flow_dict(client, bm))
    replica.update(compute_azure_case_dict(client, bm, cons))
    replica.update(compute_project_npv_dict(client, bm, cons))

    report = audit(golden, replica_values=replica, engine_values=None)
    v = verdict(report)

    assert v.replica_clean, (
        f"Replica is not clean: {report.replica_fail_count} fail, "
        f"{report.replica_unknown_count} unknown of {report.total_cells}"
    )
    assert report.replica_pass_count == 395


# ---------------------------------------------------------------------------
# Step 11 — Engine bridge: 3-way audit (BA / Replica / Engine)
# ---------------------------------------------------------------------------
#
# This is the trust harness for Steps 12-14. It runs the engine bridge against
# the SAME oracle the replica is pinned to, and tracks the engine drift count
# as a monotonically-non-increasing counter.
#
# Replica must remain CLEAN (any regression blocks merge).
# Engine drift must be <= MAX_ENGINE_DRIFT (ratchet — only improves over time).
#
# When Step 12 fixes engine bugs, lower MAX_ENGINE_DRIFT and re-commit.
# ---------------------------------------------------------------------------

# Initial baseline measured 1 May 2026 against Customer A workbook with the
# engine pipeline as-shipped. Step 12 work must DECREASE this number.
# Step 12.3h achieved drift = 0 (engine matches BA exactly across all 395
# oracle keys). Any regression is a hard fail.
MAX_ENGINE_DRIFT = 0


def test_engine_bridge_covers_all_oracle_cells_customer_a(golden):
    """
    The engine bridge must populate ALL 395 oracle keys. Missing keys would be
    a bridge bug, not an engine bug, so they must be zero.
    """
    from training.replicas.engine_bridge_l3 import compute_engine_layer3_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_A_WORKBOOK))

    engine = compute_engine_layer3_dict(client, bm, cons)

    report = audit(golden, replica_values=None, engine_values=engine)
    assert report.engine_unknown_count == 0, (
        f"Engine bridge missing {report.engine_unknown_count} keys; "
        "every oracle key must be populated."
    )
    assert report.total_cells == 395


def test_engine_drift_does_not_exceed_baseline_customer_a(golden):
    """
    Three-way audit (BA / Replica / Engine) — the Step 11 trust scorecard.

    Asserts:
      1. Replica MUST remain CLEAN (zero failing cells, full coverage).
         Any regression there is a contract violation and blocks merge.
      2. Engine drift MUST NOT exceed ``MAX_ENGINE_DRIFT``. As Step 12 fixes
         engine bugs, lower this constant and commit. The test is a one-way
         ratchet — engine drift can only go DOWN.
    """
    from training.replicas.engine_bridge_l3 import compute_engine_layer3_dict
    from training.replicas.layer3_azure_case import compute_azure_case_dict
    from training.replicas.layer3_cash_flow import compute_status_quo_cash_flow_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )
    from training.replicas.layer3_project_npv import compute_project_npv_dict
    from training.replicas.layer3_status_quo import compute_status_quo

    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip("Customer A workbook required")

    client = load_client_inputs(str(CUSTOMER_A_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_A_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_A_WORKBOOK))

    # Build the locked replica dict (must be CLEAN against the oracle).
    replica: dict = {}
    replica.update(compute_status_quo(client, bm))
    replica.update(compute_status_quo_cash_flow_dict(client, bm))
    replica.update(compute_azure_case_dict(client, bm, cons))
    replica.update(compute_project_npv_dict(client, bm, cons))

    # Build the engine bridge dict (drift here is what Step 12 must reduce).
    engine = compute_engine_layer3_dict(client, bm, cons)

    report = audit(golden, replica_values=replica, engine_values=engine)
    v = verdict(report)

    # Trust contract: replica must remain clean.
    assert v.replica_clean, (
        f"Replica regression — {report.replica_fail_count} failing, "
        f"{report.replica_unknown_count} unknown of {report.total_cells}"
    )

    # Engine drift ratchet — only allowed to decrease over time.
    assert report.engine_fail_count <= MAX_ENGINE_DRIFT, (
        f"Engine drift increased: {report.engine_fail_count} > "
        f"MAX_ENGINE_DRIFT ({MAX_ENGINE_DRIFT}). Fix the engine OR raise the "
        "ratchet only if the increase is intentional."
    )
    pass


# ---------------------------------------------------------------------------
# Customer B parity (Step 15 — ECIF onboarding)
# ---------------------------------------------------------------------------
#
# Customer B exercises the Microsoft funding (ECIF) feature that Customer A
# left at zero. The BA workbook canonical structure for ECIF is:
#
#   * '2a-Consumption Plan Wk1'!E22:N22 — per-year ECIF (negative numbers).
#     Customer B has -1,050,000 in Y1, Y2, Y3 (E22, F22, G22).
#   * 'Detailed Financial Case'!Q46 = gross migration + Q47 (NET).
#   * 'Detailed Financial Case'!Q47 = ACO + ECIF (funding alone).
#   * 'Summary Financial Case' rows 21..26 use SUMIF on the AH tag column of
#     'Detailed Financial Case' rows 54..75. The tags ("CAPEX", "OPEX",
#     "Azure Costs", "Migration Costs", "Microsoft Investments") are
#     mutually exclusive — each tag matches a unique row, so row 26 sums
#     these as independent buckets with no double-counting.
#
# Step 15.1 hardens the contract: BOTH replica and engine paths must reach
# 395/395 on Customer B. The drifts present in Step 15 traced to two
# distinct bugs that Customer A's coarse ramp pattern accidentally hid:
#   1. `_az_dc_or_bandwidth` (replica) and `engine/retained_costs.py`
#      (engine) used a single-factor `(1 - eoy_ramp[t-1])` decay where the
#      BA workbook actually uses a chained product
#      `Π_{k=1..t-1} (1 - eoy_ramp[k])`. For ramps that jump straight to
#      1.0 (Customer A: [0.5, 1.0, ...]) the chain collapses to one factor,
#      hiding the bug. Customer B's [0.33, 0.66, 1.0, ...] exposes it.
#   2. The engine bridge clamped `num_physical_servers_excl_hosts` and
#      `allocated_pcores_excl_hosts` residuals to ≥0, losing the BA's
#      hand-typed D42/D47 totals when those values were SMALLER than what
#      the engine derived from VM count / vCPU divided by ratios. New
#      override fields in `WorkloadInventory` carry D42/D47 verbatim into
#      `est_physical_servers_incl_hosts` / `est_allocated_pcores_incl_hosts`.
#   3. The 5Y CF payback computation diverged from BA's
#      `5Y CF with Payback!I32 = SUM(C47:G47)` semantics, which only fills
#      a payback value when cumulative crosses the investment threshold
#      *between* observed years. If Y1 already covers the investment,
#      payback < 1 year is reported as 0 (sentinel for "less than one year").

# Replica drift ratchet for Customer B (one-way: only decreases).
MAX_REPLICA_DRIFT_CUSTOMER_B = 0

# Engine drift ratchet for Customer B (one-way: only decreases).
MAX_ENGINE_DRIFT_CUSTOMER_B = 0

# ECIF cells that MUST pass after Step 15 (zero tolerance — these are the
# Step 15 contract).
ECIF_REPLICA_REQUIRED_CELLS = (
    "cash_flow.AZ MS Funding.Y1",
    "cash_flow.AZ MS Funding.Y2",
    "cash_flow.AZ MS Funding.Y3",
    "cash_flow.AZ Migration.Y1",
    "cash_flow.AZ Migration.Y2",
    "cash_flow.AZ Migration.Y3",
    "five_payback.migration_npv",
    "five_payback.total_costs_npv",
)


@pytest.fixture(scope="module")
def golden_customer_b():
    if not CUSTOMER_B_WORKBOOK.exists():
        pytest.skip(f"Customer B workbook not found at {CUSTOMER_B_WORKBOOK}")
    return extract_layer3_golden(str(CUSTOMER_B_WORKBOOK))


def test_customer_b_extractor_pulls_395_cells(golden_customer_b):
    """The golden extractor must yield 395 cells from Customer B (same shape as A)."""
    flat = flatten_golden(golden_customer_b)
    # flat is a list of (key, ref, value) tuples
    assert len(flat) == 395, f"Expected 395 cells, got {len(flat)}"


def test_customer_b_ecif_replica_cells_pass(golden_customer_b):
    """All ECIF-mandated replica cells (Step 15 contract) must pass with zero drift."""
    from training.replicas.layer3_azure_case import compute_azure_case_dict
    from training.replicas.layer3_cash_flow import (
        compute_status_quo_cash_flow_dict,
    )
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )
    from training.replicas.layer3_project_npv import compute_project_npv_dict
    from training.replicas.layer3_status_quo import compute_status_quo

    if not CUSTOMER_B_WORKBOOK.exists():
        pytest.skip("Customer B workbook required")

    client = load_client_inputs(str(CUSTOMER_B_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_B_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_B_WORKBOOK))

    replica: dict = {}
    replica.update(compute_status_quo(client, bm))
    replica.update(compute_status_quo_cash_flow_dict(client, bm))
    replica.update(compute_azure_case_dict(client, bm, cons))
    replica.update(compute_project_npv_dict(client, bm, cons))

    report = audit(golden_customer_b, replica_values=replica, engine_values=None)
    failed = {a.label for a in report.audits if a.replica_passes is False}
    missing = [c for c in ECIF_REPLICA_REQUIRED_CELLS if c in failed]
    assert not missing, (
        f"ECIF replica regression — these cells must pass per Step 15 contract: {missing}"
    )


def test_customer_b_ecif_engine_cells_pass(golden_customer_b):
    """All ECIF-mandated engine cells (Step 15 contract) must pass with zero drift."""
    from training.replicas.engine_bridge_l3 import compute_engine_layer3_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )

    if not CUSTOMER_B_WORKBOOK.exists():
        pytest.skip("Customer B workbook required")

    client = load_client_inputs(str(CUSTOMER_B_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_B_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_B_WORKBOOK))

    engine = compute_engine_layer3_dict(client, bm, cons)
    report = audit(golden_customer_b, replica_values=None, engine_values=engine)
    failed = {a.label for a in report.audits if a.engine_passes is False}
    missing = [c for c in ECIF_REPLICA_REQUIRED_CELLS if c in failed]
    assert not missing, (
        f"ECIF engine regression — these cells must pass per Step 15 contract: {missing}"
    )


def test_customer_b_engine_bridge_covers_all_oracle_cells(golden_customer_b):
    """The engine bridge must populate ALL 395 oracle keys for Customer B too."""
    from training.replicas.engine_bridge_l3 import compute_engine_layer3_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )

    if not CUSTOMER_B_WORKBOOK.exists():
        pytest.skip("Customer B workbook required")

    client = load_client_inputs(str(CUSTOMER_B_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_B_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_B_WORKBOOK))

    engine = compute_engine_layer3_dict(client, bm, cons)
    report = audit(golden_customer_b, replica_values=None, engine_values=engine)
    assert report.engine_unknown_count == 0, (
        f"Engine bridge missing {report.engine_unknown_count} keys for Customer B"
    )
    assert report.total_cells == 395


def test_customer_b_replica_drift_under_ratchet(golden_customer_b):
    """Replica drift ratchet for Customer B (one-way: only decreases)."""
    from training.replicas.layer3_azure_case import compute_azure_case_dict
    from training.replicas.layer3_cash_flow import (
        compute_status_quo_cash_flow_dict,
    )
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )
    from training.replicas.layer3_project_npv import compute_project_npv_dict
    from training.replicas.layer3_status_quo import compute_status_quo

    if not CUSTOMER_B_WORKBOOK.exists():
        pytest.skip("Customer B workbook required")

    client = load_client_inputs(str(CUSTOMER_B_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_B_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_B_WORKBOOK))

    replica: dict = {}
    replica.update(compute_status_quo(client, bm))
    replica.update(compute_status_quo_cash_flow_dict(client, bm))
    replica.update(compute_azure_case_dict(client, bm, cons))
    replica.update(compute_project_npv_dict(client, bm, cons))

    report = audit(golden_customer_b, replica_values=replica, engine_values=None)
    assert report.replica_fail_count <= MAX_REPLICA_DRIFT_CUSTOMER_B, (
        f"Customer B replica drift increased: {report.replica_fail_count} > "
        f"MAX_REPLICA_DRIFT_CUSTOMER_B ({MAX_REPLICA_DRIFT_CUSTOMER_B}). "
        "Fix the regression OR lower the ratchet (only allowed to decrease)."
    )


def test_customer_b_engine_drift_under_ratchet(golden_customer_b):
    """Engine drift ratchet for Customer B (one-way: only decreases)."""
    from training.replicas.engine_bridge_l3 import compute_engine_layer3_dict
    from training.replicas.layer3_inputs import (
        load_benchmark_inputs,
        load_client_inputs,
        load_consumption_inputs,
    )

    if not CUSTOMER_B_WORKBOOK.exists():
        pytest.skip("Customer B workbook required")

    client = load_client_inputs(str(CUSTOMER_B_WORKBOOK))
    bm = load_benchmark_inputs(str(CUSTOMER_B_WORKBOOK))
    cons = load_consumption_inputs(str(CUSTOMER_B_WORKBOOK))

    engine = compute_engine_layer3_dict(client, bm, cons)
    report = audit(golden_customer_b, replica_values=None, engine_values=engine)
    assert report.engine_fail_count <= MAX_ENGINE_DRIFT_CUSTOMER_B, (
        f"Customer B engine drift increased: {report.engine_fail_count} > "
        f"MAX_ENGINE_DRIFT_CUSTOMER_B ({MAX_ENGINE_DRIFT_CUSTOMER_B}). "
        "Fix the regression OR lower the ratchet (only allowed to decrease)."
    )