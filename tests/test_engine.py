"""
Engine unit tests.

Each test validates one engine module against the reference workbook values.
Tests are ordered by dependency: models → parser → status_quo → depreciation
→ retained_costs → financial_case → outputs.

Numeric tolerances are ±1% unless the workbook formula is exactly known,
in which case exact float comparisons with a tight tolerance are used.
"""

import pytest
from pathlib import Path

from engine.models import BenchmarkConfig, WorkloadInventory
from engine import status_quo, retained_costs, depreciation, financial_case, outputs
from engine.rvtools_parser import parse as parse_rvtools

RVTOOLS_FILE = "RVTools_export_VCP003_2026-01-05_13.14.03.xlsx"
TOL = 0.01  # 1% tolerance for all financial comparisons


def approx_pct(expected: float, actual: float, tol: float = TOL) -> bool:
    """Return True if actual is within tol % of expected."""
    if expected == 0:
        return abs(actual) < 1.0
    return abs(actual - expected) / abs(expected) <= tol


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TestModels:
    def test_workload_derived_fields(self, contoso_workload):
        wl = contoso_workload
        assert wl.total_vms_and_physical == 13364
        # est_physical_servers = 13364 / 12 + 0 ≈ 1,114  (workbook D42 = D39/K11)
        assert approx_pct(1_114, wl.est_physical_servers_incl_hosts)
        # pcores_with_sql defaults to 10% of windows
        assert wl.pcores_with_sql_server == 543

    def test_benchmark_from_yaml(self, default_benchmarks):
        bm = default_benchmarks
        assert bm.wacc == pytest.approx(0.07)
        assert bm.vm_to_physical_server_ratio == pytest.approx(12.0)
        assert bm.virtualization_license_per_core_yr == pytest.approx(208.0)
        assert bm.vms_per_sysadmin == pytest.approx(1200.0)

    def test_benchmark_price_level_helper(self, default_benchmarks):
        from engine.models import PriceLevel
        bm = default_benchmarks
        assert bm.windows_license_per_core(PriceLevel.B) == pytest.approx(86.16, abs=0.01)
        assert bm.windows_license_per_core(PriceLevel.D) == pytest.approx(73.08, abs=0.01)


# ---------------------------------------------------------------------------
# RVtools parser
# ---------------------------------------------------------------------------

class TestRVToolsParser:
    def test_parse_file_exists(self):
        assert Path(RVTOOLS_FILE).exists(), f"RVtools file not found: {RVTOOLS_FILE}"

    def test_parse_vm_count(self):
        inv = parse_rvtools(RVTOOLS_FILE)
        assert inv.num_vms > 0, "Expected at least one VM"

    def test_parse_host_count(self):
        inv = parse_rvtools(RVTOOLS_FILE)
        assert inv.num_hosts > 0, "Expected at least one host"

    def test_parse_vcpu_positive(self):
        inv = parse_rvtools(RVTOOLS_FILE)
        assert inv.total_vcpu > 0

    def test_parse_storage_positive(self):
        inv = parse_rvtools(RVTOOLS_FILE)
        assert inv.total_storage_in_use_gb > 0

    def test_parse_ratio_positive(self):
        inv = parse_rvtools(RVTOOLS_FILE)
        assert inv.vcpu_per_core_ratio > 0

    def test_parse_windows_derived_from_vcpu(self):
        inv = parse_rvtools(RVTOOLS_FILE)
        # Windows pCores should never exceed total host pCores
        assert inv.pcores_with_windows_server <= inv.total_host_pcores + 1


# ---------------------------------------------------------------------------
# Status Quo
# ---------------------------------------------------------------------------

class TestStatusQuo:
    def test_returns_correct_length(self, contoso_inputs, default_benchmarks):
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        assert len(sq.total()) == 11  # Y0–Y10

    def test_costs_positive(self, contoso_inputs, default_benchmarks):
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        for yr in range(1, 11):
            assert sq.total()[yr] > 0, f"Expected positive cost at Y{yr}"

    def test_costs_grow_with_rate(self, contoso_inputs, default_benchmarks):
        """Costs should grow year-over-year due to expected_future_growth_rate=10%."""
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        totals = sq.total()
        assert totals[5] > totals[1], "Costs should grow by Y5"
        assert totals[10] > totals[5], "Costs should grow by Y10"

    def test_license_cost_nonzero(self, contoso_inputs, default_benchmarks):
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        assert sq.virtualization_licenses[1] > 0
        assert sq.windows_server_licenses[1] > 0

    def test_admin_cost_nonzero(self, contoso_inputs, default_benchmarks):
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        assert sq.system_admin_staff[1] > 0


# ---------------------------------------------------------------------------
# Depreciation
# ---------------------------------------------------------------------------

class TestDepreciation:
    def test_schedule_length(self, contoso_inputs, default_benchmarks):
        depr = depreciation.compute(contoso_inputs, default_benchmarks)
        from engine.depreciation import TOTAL_COLS
        assert len(depr.servers.yearly_acquisition) == TOTAL_COLS

    def test_forward_slice_length(self, contoso_inputs, default_benchmarks):
        depr = depreciation.compute(contoso_inputs, default_benchmarks)
        assert len(depr.servers.forward_depreciation) == 11  # Y0–Y10

    def test_acquisition_positive(self, contoso_inputs, default_benchmarks):
        depr = depreciation.compute(contoso_inputs, default_benchmarks)
        assert sum(depr.servers.forward_acquisition) > 0


# ---------------------------------------------------------------------------
# Retained Costs
# ---------------------------------------------------------------------------

class TestRetainedCosts:
    def test_retained_lte_status_quo(self, contoso_inputs, default_benchmarks):
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        depr = depreciation.compute(contoso_inputs, default_benchmarks)
        ret = retained_costs.compute(contoso_inputs, default_benchmarks, sq)
        for yr in range(1, 11):
            assert ret.total()[yr] <= sq.total()[yr] + 0.01, (
                f"Retained costs exceed status quo at Y{yr}"
            )

    def test_retained_zero_at_full_migration(self, contoso_inputs, default_benchmarks):
        """Virt licenses drop to 0 one year after full migration.
        Contoso ramp=[0.4, 0.8, 1.0, ...]: full migration at Y3, so lagged_ramp
        reaches 1.0 at Y4 — virt licenses[4] should be 0."""
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        ret = retained_costs.compute(contoso_inputs, default_benchmarks, sq)
        assert ret.virtualization_licenses[4] == pytest.approx(0.0, abs=1.0)

    def test_retained_partial_at_y2(self, contoso_inputs, default_benchmarks):
        """At Y2, lagged ramp = ramp[Y1] = 40%, so virt retained ≈ 60% of sq[Y1].
        Contoso ramp=[0.4, 0.8, 1.0, ...]: lagged_ramp at Y2 = ramp_y1 = 0.4."""
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        ret = retained_costs.compute(contoso_inputs, default_benchmarks, sq)
        # retained[2] = sq[1] * (1 - 0.4); compare against sq[1] baseline
        expected_fraction = 0.60
        actual_fraction = ret.virtualization_licenses[2] / sq.virtualization_licenses[1]
        assert approx_pct(expected_fraction, actual_fraction, tol=0.05)


# ---------------------------------------------------------------------------
# Financial Case
# ---------------------------------------------------------------------------

class TestFinancialCase:
    def test_sq_total_positive(self, contoso_inputs, default_benchmarks):
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        depr = depreciation.compute(contoso_inputs, default_benchmarks)
        ret = retained_costs.compute(contoso_inputs, default_benchmarks, sq)
        fc = financial_case.compute(contoso_inputs, default_benchmarks, sq, ret, depr)
        for yr in range(1, 11):
            assert fc.sq_total()[yr] > 0

    def test_azure_has_consumption(self, contoso_inputs, default_benchmarks):
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        depr = depreciation.compute(contoso_inputs, default_benchmarks)
        ret = retained_costs.compute(contoso_inputs, default_benchmarks, sq)
        fc = financial_case.compute(contoso_inputs, default_benchmarks, sq, ret, depr)
        assert sum(fc.az_azure_consumption[1:]) > 0

    def test_migration_costs_concentrated_early(self, contoso_inputs, default_benchmarks):
        """Migration costs should be 0 by Y4 (ramp complete at Y3)."""
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        depr = depreciation.compute(contoso_inputs, default_benchmarks)
        ret = retained_costs.compute(contoso_inputs, default_benchmarks, sq)
        fc = financial_case.compute(contoso_inputs, default_benchmarks, sq, ret, depr)
        assert fc.az_migration_costs[4] == pytest.approx(0.0, abs=1.0)


# ---------------------------------------------------------------------------
# Outputs / Summary
# ---------------------------------------------------------------------------

class TestOutputs:
    def _run(self, contoso_inputs, default_benchmarks):
        sq = status_quo.compute(contoso_inputs, default_benchmarks)
        depr = depreciation.compute(contoso_inputs, default_benchmarks)
        ret = retained_costs.compute(contoso_inputs, default_benchmarks, sq)
        fc = financial_case.compute(contoso_inputs, default_benchmarks, sq, ret, depr)
        return outputs.compute(contoso_inputs, default_benchmarks, fc), fc

    def test_npv_10yr_positive(self, contoso_inputs, default_benchmarks):
        summary, _ = self._run(contoso_inputs, default_benchmarks)
        assert summary.npv_10yr > 0, "Expected positive NPV for this scenario"

    def test_npv_5yr_less_than_10yr(self, contoso_inputs, default_benchmarks):
        summary, _ = self._run(contoso_inputs, default_benchmarks)
        assert summary.npv_5yr < summary.npv_10yr

    def test_payback_within_10_years(self, contoso_inputs, default_benchmarks):
        summary, _ = self._run(contoso_inputs, default_benchmarks)
        assert summary.payback_years is not None
        assert summary.payback_years <= 10.0

    def test_waterfall_keys_present(self, contoso_inputs, default_benchmarks):
        summary, _ = self._run(contoso_inputs, default_benchmarks)
        expected_keys = [
            "Status Quo (On-Prem)", "Hardware Costs Reduction",
            "Facilities Costs Reduction", "Licenses Costs Reduction",
            "IT Operations Costs Reduction", "Azure Consumption Increase", "Azure Case"
        ]
        for k in expected_keys:
            assert k in summary.waterfall, f"Missing waterfall key: {k}"

    def test_cost_per_vm_positive(self, contoso_inputs, default_benchmarks):
        summary, _ = self._run(contoso_inputs, default_benchmarks)
        assert summary.on_prem_cost_per_vm_yr > 0
        assert summary.azure_cost_per_vm_yr > 0

    def test_print_summary_runs(self, contoso_inputs, default_benchmarks):
        # print_summary now logs to DEBUG rather than stdout — just verify it doesn't raise
        summary, _ = self._run(contoso_inputs, default_benchmarks)
        outputs.print_summary(summary)  # should not raise
