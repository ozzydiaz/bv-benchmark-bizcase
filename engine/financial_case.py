"""
Detailed Financial Case engine.

Replicates the 'Detailed Financial Case' sheet: assembles the full
34-row × 11-column (Y0–Y10) P&L matrix for both the Status Quo scenario
and the Azure scenario, side-by-side.

All values are in USD.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import BenchmarkConfig, BusinessCaseInputs, YesNo
from .status_quo import StatusQuoCosts, YEARS
from .retained_costs import RetainedCosts
from .depreciation import AllDepreciationSchedules, LOOKBACK


@dataclass
class FinancialCase:
    """
    Full P&L for both scenarios, Y0–Y10 (11 values per list).

    Status Quo = what the customer pays if they stay on-premises.
    Azure Case = what the customer pays after migrating.
    Savings = Status Quo - Azure Case (positive = Azure is cheaper).
    """

    # ---------- Status Quo P&L ----------
    # Hardware (OPEX view — depreciation)
    sq_server_depreciation: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_server_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_storage_depreciation: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_storage_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_storage_backup_cost: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_storage_dr_cost: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_nw_depreciation: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_nw_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    # DC
    sq_bandwidth: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_dc_space: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_dc_power: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    # Licenses
    sq_virtualization_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_windows_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_sql_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_windows_esu: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_sql_esu: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_backup_software: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_dr_software: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    # IT admin
    sq_system_admin: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # ---------- Azure Case P&L ----------
    # Retained on-prem costs (declining)
    az_server_depreciation: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_server_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_storage_depreciation: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_storage_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_storage_backup_cost: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_storage_dr_cost: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_nw_depreciation: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_nw_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_bandwidth: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_dc_space: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_dc_power: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_virtualization_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_windows_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_sql_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_windows_esu: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_sql_esu: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_backup_software: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_dr_software: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_system_admin: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    # Azure-only costs
    az_azure_consumption: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_migration_costs: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_microsoft_funding: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_existing_azure_run_rate: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # ---------- Cash Flow view: hardware acquisition (CAPEX basis) ----------
    # Populated by compute() from depreciation.forward_acquisition
    sq_server_acquisition: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_storage_acquisition: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_nw_acquisition: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    # Azure case: retained (un-migrated) fraction of hardware acquisition
    az_server_acquisition: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_storage_acquisition: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_nw_acquisition: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # ---------- Computed summaries ----------
    def sq_total_hardware(self) -> list[float]:
        return [
            self.sq_server_depreciation[i] + self.sq_server_maintenance[i]
            + self.sq_storage_depreciation[i] + self.sq_storage_maintenance[i]
            + self.sq_storage_backup_cost[i] + self.sq_storage_dr_cost[i]
            + self.sq_nw_depreciation[i] + self.sq_nw_maintenance[i]
            for i in range(YEARS + 1)
        ]

    def sq_total_datacenter(self) -> list[float]:
        return [
            self.sq_bandwidth[i] + self.sq_dc_space[i] + self.sq_dc_power[i]
            for i in range(YEARS + 1)
        ]

    def sq_total_licenses(self) -> list[float]:
        return [
            self.sq_virtualization_licenses[i] + self.sq_windows_licenses[i]
            + self.sq_sql_licenses[i] + self.sq_windows_esu[i] + self.sq_sql_esu[i]
            + self.sq_backup_software[i] + self.sq_dr_software[i]
            for i in range(YEARS + 1)
        ]

    def sq_total(self) -> list[float]:
        return [
            self.sq_total_hardware()[i] + self.sq_total_datacenter()[i]
            + self.sq_total_licenses()[i] + self.sq_system_admin[i]
            for i in range(YEARS + 1)
        ]

    def az_total_retained_onprem(self) -> list[float]:
        return [
            self.az_server_depreciation[i] + self.az_server_maintenance[i]
            + self.az_storage_depreciation[i] + self.az_storage_maintenance[i]
            + self.az_storage_backup_cost[i] + self.az_storage_dr_cost[i]
            + self.az_nw_depreciation[i] + self.az_nw_maintenance[i]
            + self.az_bandwidth[i] + self.az_dc_space[i] + self.az_dc_power[i]
            + self.az_virtualization_licenses[i] + self.az_windows_licenses[i]
            + self.az_sql_licenses[i] + self.az_windows_esu[i] + self.az_sql_esu[i]
            + self.az_backup_software[i] + self.az_dr_software[i]
            + self.az_system_admin[i]
            for i in range(YEARS + 1)
        ]

    def az_total(self) -> list[float]:
        return [
            self.az_total_retained_onprem()[i]
            + self.az_azure_consumption[i]
            + self.az_migration_costs[i]
            + self.az_microsoft_funding[i]
            + self.az_existing_azure_run_rate[i]
            for i in range(YEARS + 1)
        ]

    def savings(self) -> list[float]:
        """Status Quo - Azure Case (P&L). Positive = Azure is cheaper."""
        sq = self.sq_total()
        az = self.az_total()
        return [sq[i] - az[i] for i in range(YEARS + 1)]

    # ------------------------------------------------------------------
    # Cash Flow view (acquisition-based CAPEX instead of depreciation)
    # ------------------------------------------------------------------

    def sq_capex(self) -> list[float]:
        """SQ annual CAPEX: hardware acquisition (server + storage + NW)."""
        return [
            self.sq_server_acquisition[i] + self.sq_storage_acquisition[i] + self.sq_nw_acquisition[i]
            for i in range(YEARS + 1)
        ]

    def sq_opex_cf(self) -> list[float]:
        """SQ OPEX: all non-CAPEX costs (maintenance, DC facilities, licenses, IT admin)."""
        return [
            self.sq_server_maintenance[i] + self.sq_storage_maintenance[i]
            + self.sq_storage_backup_cost[i] + self.sq_storage_dr_cost[i]
            + self.sq_nw_maintenance[i] + self.sq_bandwidth[i]
            + self.sq_dc_space[i] + self.sq_dc_power[i]
            + self.sq_total_licenses()[i] + self.sq_system_admin[i]
            for i in range(YEARS + 1)
        ]

    def sq_total_cf(self) -> list[float]:
        """SQ total cashflow = CAPEX + OPEX."""
        cap = self.sq_capex()
        ops = self.sq_opex_cf()
        return [cap[i] + ops[i] for i in range(YEARS + 1)]

    def az_capex_cf(self) -> list[float]:
        """Azure case CAPEX: retained (un-migrated) hardware acquisition."""
        return [
            self.az_server_acquisition[i] + self.az_storage_acquisition[i] + self.az_nw_acquisition[i]
            for i in range(YEARS + 1)
        ]

    def az_opex_cf(self) -> list[float]:
        """Azure case OPEX: retained on-prem ops (maintenance, DC, licenses, IT admin)."""
        return [
            self.az_server_maintenance[i] + self.az_storage_maintenance[i]
            + self.az_storage_backup_cost[i] + self.az_storage_dr_cost[i]
            + self.az_nw_maintenance[i] + self.az_bandwidth[i]
            + self.az_dc_space[i] + self.az_dc_power[i]
            + self.az_virtualization_licenses[i] + self.az_windows_licenses[i]
            + self.az_sql_licenses[i] + self.az_windows_esu[i] + self.az_sql_esu[i]
            + self.az_backup_software[i] + self.az_dr_software[i]
            + self.az_system_admin[i]
            for i in range(YEARS + 1)
        ]

    def az_azure_costs_cf(self) -> list[float]:
        """Azure cloud consumption costs (new Azure + existing run rate)."""
        return [
            self.az_azure_consumption[i] + self.az_existing_azure_run_rate[i]
            for i in range(YEARS + 1)
        ]

    def az_migration_cf(self) -> list[float]:
        """Net migration one-time costs (gross + Microsoft funding)."""
        return [
            self.az_migration_costs[i] + self.az_microsoft_funding[i]
            for i in range(YEARS + 1)
        ]

    def az_total_cf(self) -> list[float]:
        """Azure case total cashflow = retained CAPEX + retained OPEX + Azure costs + migration."""
        cap = self.az_capex_cf()
        ops = self.az_opex_cf()
        azur = self.az_azure_costs_cf()
        mig = self.az_migration_cf()
        return [cap[i] + ops[i] + azur[i] + mig[i] for i in range(YEARS + 1)]

    def cf_savings(self) -> list[float]:
        """Cashflow savings: SQ total CF - Azure total CF. Positive = Azure saves cash."""
        sq = self.sq_total_cf()
        az = self.az_total_cf()
        return [sq[i] - az[i] for i in range(YEARS + 1)]


def _azure_consumption_by_year(
    inputs: BusinessCaseInputs,
    benchmarks: BenchmarkConfig,
) -> list[float]:
    """
    Sum Azure consumption across all workloads for each year.

    Formula (matching Excel 'Detailed Financial Case' sheet):
        consumption_y = avg_ramp_y × full_run_rate × (1 + g) × (1 − ACD)

    Where:
      avg_ramp_y  = (ramp_y + ramp_{y-1}) / 2  (half-year convention)
      full_run_rate = compute + storage + other  (Y10 anchor, pre-growth)
      g           = hardware.expected_future_growth_rate  (flat 1-period uplift)
      ACD         = consumption_plan.azure_consumption_discount

    Note: the Excel applies (1 + g) as a flat single-period cost-growth
    adjustment to all years, not as a compound annual rate.  This reflects
    the assumption that Azure pricing will grow by g relative to today.
    """
    g = inputs.hardware.expected_future_growth_rate
    result = [0.0] * (YEARS + 1)
    for cp in inputs.consumption_plans:
        full_run = (
            cp.annual_compute_consumption_lc_y10
            + cp.annual_storage_consumption_lc_y10
            + cp.annual_other_consumption_lc_y10
        )
        acd = cp.azure_consumption_discount
        effective_run = full_run * (1.0 + g) * (1.0 - acd)
        for yr in range(1, YEARS + 1):
            ramp_this = cp.migration_ramp_pct[yr - 1]
            ramp_prev = cp.migration_ramp_pct[yr - 2] if yr > 1 else 0.0
            avg_ramp = (ramp_this + ramp_prev) / 2
            result[yr] += avg_ramp * effective_run
    return result


def _migration_costs_by_year(inputs: BusinessCaseInputs) -> list[float]:
    """Net migration costs (gross cost + Microsoft funding) by year."""
    gross = [0.0] * (YEARS + 1)
    funding = [0.0] * (YEARS + 1)
    for cp in inputs.consumption_plans:
        total_vms = sum(wl.total_vms_and_physical for wl in inputs.workloads)
        ramp_prev = 0.0
        for yr in range(1, YEARS + 1):
            ramp_this = cp.migration_ramp_pct[yr - 1]
            newly_migrated_frac = ramp_this - ramp_prev
            vms_migrated = newly_migrated_frac * total_vms
            gross[yr] += vms_migrated * cp.migration_cost_per_vm_lc
            ramp_prev = ramp_this
        for yr in range(1, YEARS + 1):
            funding[yr] += cp.aco_by_year[yr - 1] + cp.ecif_by_year[yr - 1]
    return [gross[i] + funding[i] for i in range(YEARS + 1)]


def compute(
    inputs: BusinessCaseInputs,
    benchmarks: BenchmarkConfig,
    status_quo: StatusQuoCosts,
    retained: RetainedCosts,
    depreciation: AllDepreciationSchedules,
) -> FinancialCase:
    """Assemble the full Detailed Financial Case matrix."""
    fc = FinancialCase()

    az_consumption = _azure_consumption_by_year(inputs, benchmarks)
    az_migration = _migration_costs_by_year(inputs)

    # Existing Azure run rate
    existing_az = [0.0] * (YEARS + 1)
    if inputs.azure_run_rate.include_in_business_case == YesNo.YES:
        monthly = inputs.azure_run_rate.monthly_spend_usd
        for yr in range(1, YEARS + 1):
            existing_az[yr] = monthly * 12

    # Hardware renewal factor (M12): fraction of due hardware actually purchased on the
    # Azure migration track.  Template cell '1-Client Variables'!D27; default 10%.
    # On the migration path, customers defer hardware refreshes, so only M12% of the
    # normally-due acquisitions are made; retained CAPEX and depreciation scale accordingly.
    m12 = inputs.hardware.hardware_renewal_during_migration_pct

    # Forward depreciation AND acquisition: slice from LOOKBACK position
    depr_servers = depreciation.servers.forward_depreciation
    depr_storage = depreciation.storage.forward_depreciation
    depr_nw = depreciation.network_fitout.forward_depreciation
    acq_servers = depreciation.servers.forward_acquisition
    acq_storage = depreciation.storage.forward_acquisition
    acq_nw = depreciation.network_fitout.forward_acquisition

    for yr in range(YEARS + 1):
        retained_frac = max(0.0, 1.0 - _avg_ramp(inputs, yr))

        # --- Status Quo: P&L (depreciation-based) ---
        fc.sq_server_depreciation[yr] = depr_servers[yr]
        fc.sq_server_maintenance[yr] = status_quo.server_maintenance[yr]
        fc.sq_storage_depreciation[yr] = depr_storage[yr]
        fc.sq_storage_maintenance[yr] = status_quo.storage_maintenance[yr]
        fc.sq_storage_backup_cost[yr] = status_quo.backup_storage_cost[yr]
        fc.sq_storage_dr_cost[yr] = status_quo.dr_storage_cost[yr]
        fc.sq_nw_depreciation[yr] = depr_nw[yr]
        fc.sq_nw_maintenance[yr] = status_quo.network_maintenance[yr]
        fc.sq_bandwidth[yr] = status_quo.bandwidth[yr]
        fc.sq_dc_space[yr] = status_quo.dc_lease_space[yr]
        fc.sq_dc_power[yr] = status_quo.dc_power[yr]
        fc.sq_virtualization_licenses[yr] = status_quo.virtualization_licenses[yr]
        fc.sq_windows_licenses[yr] = status_quo.windows_server_licenses[yr]
        fc.sq_sql_licenses[yr] = status_quo.sql_server_licenses[yr]
        fc.sq_windows_esu[yr] = status_quo.windows_esu[yr]
        fc.sq_sql_esu[yr] = status_quo.sql_esu[yr]
        fc.sq_backup_software[yr] = status_quo.backup_software[yr]
        fc.sq_dr_software[yr] = status_quo.dr_software[yr]
        fc.sq_system_admin[yr] = status_quo.system_admin_staff[yr]

        # --- Status Quo: cashflow CAPEX (hardware acquisition) ---
        fc.sq_server_acquisition[yr] = acq_servers[yr]
        fc.sq_storage_acquisition[yr] = acq_storage[yr]
        fc.sq_nw_acquisition[yr] = acq_nw[yr]

        # --- Azure Case: retained on-prem (P&L) ---
        # Depreciation scaled by M12: fewer assets are being acquired on the Azure track.
        fc.az_server_depreciation[yr] = depr_servers[yr] * retained_frac * m12
        fc.az_server_maintenance[yr] = retained.server_maintenance[yr]
        fc.az_storage_depreciation[yr] = depr_storage[yr] * retained_frac * m12
        fc.az_storage_maintenance[yr] = retained.storage_maintenance[yr]
        fc.az_storage_backup_cost[yr] = retained.backup_storage_cost[yr]
        fc.az_storage_dr_cost[yr] = retained.dr_storage_cost[yr]
        fc.az_nw_depreciation[yr] = depr_nw[yr] * retained_frac * m12
        fc.az_nw_maintenance[yr] = retained.network_maintenance[yr]
        fc.az_bandwidth[yr] = retained.bandwidth[yr]
        fc.az_dc_space[yr] = retained.dc_lease_space[yr]
        fc.az_dc_power[yr] = retained.dc_power[yr]
        fc.az_virtualization_licenses[yr] = retained.virtualization_licenses[yr]
        fc.az_windows_licenses[yr] = retained.windows_server_licenses[yr]
        fc.az_sql_licenses[yr] = retained.sql_server_licenses[yr]
        fc.az_windows_esu[yr] = retained.windows_esu[yr]
        fc.az_sql_esu[yr] = retained.sql_esu[yr]
        fc.az_backup_software[yr] = retained.backup_software[yr]
        fc.az_dr_software[yr] = retained.dr_software[yr]
        fc.az_system_admin[yr] = retained.system_admin_staff[yr]

        # --- Azure Case: cashflow CAPEX (retained hardware acquisition) ---
        # Mirrors the BA workbook's Detailed Financial Case row 21-22 ("Retained
        # CAPEX"):
        #   Y0     = full Y0 baseline acquisition (pre-migration; nothing has
        #            migrated yet so the customer still books the full annual
        #            sustaining hardware spend).
        #   Y1..Y10 = baseline_y0 × hw_renewal_pct × (1 - eoy_ramp[t])
        #            (defer refreshes during migration; only the residual
        #             non-migrated portion needs new hardware).
        #
        # Note: BA holds the baseline static (no yearly growth, no
        # depr/actual_life refresh factor) on this line — only the migration
        # ramp shapes the year-by-year value.
        if yr == 0:
            fc.az_server_acquisition[yr] = acq_servers[0]
            fc.az_storage_acquisition[yr] = acq_storage[0]
            fc.az_nw_acquisition[yr] = acq_nw[0]
        else:
            fc.az_server_acquisition[yr] = acq_servers[0] * m12 * retained_frac
            fc.az_storage_acquisition[yr] = acq_storage[0] * m12 * retained_frac
            fc.az_nw_acquisition[yr] = acq_nw[0] * m12 * retained_frac

        # --- Azure Case: Azure-only costs ---
        fc.az_azure_consumption[yr] = az_consumption[yr]
        fc.az_migration_costs[yr] = az_migration[yr]
        fc.az_existing_azure_run_rate[yr] = existing_az[yr]

    return fc


def _avg_ramp(inputs: BusinessCaseInputs, year: int) -> float:
    if year == 0 or not inputs.consumption_plans:
        return 0.0
    return sum(cp.migration_ramp_pct[year - 1] for cp in inputs.consumption_plans) / len(inputs.consumption_plans)
