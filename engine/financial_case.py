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
        """Status Quo - Azure Case. Positive = Azure is cheaper."""
        sq = self.sq_total()
        az = self.az_total()
        return [sq[i] - az[i] for i in range(YEARS + 1)]


def _azure_consumption_by_year(inputs: BusinessCaseInputs) -> list[float]:
    """
    Sum Azure consumption across all workloads for each year.
    Each workload's consumption ramps linearly from 0 to the Y10 anchor,
    weighted by the migration ramp-up schedule (average of current and
    prior year ramp — matching the Excel formula pattern).
    """
    result = [0.0] * (YEARS + 1)
    for cp in inputs.consumption_plans:
        full_run = (
            cp.annual_compute_consumption_lc_y10
            + cp.annual_storage_consumption_lc_y10
            + cp.annual_other_consumption_lc_y10
        )
        for yr in range(1, YEARS + 1):
            ramp_this = cp.migration_ramp_pct[yr - 1]
            ramp_prev = cp.migration_ramp_pct[yr - 2] if yr > 1 else 0.0
            avg_ramp = (ramp_this + ramp_prev) / 2
            result[yr] += avg_ramp * full_run
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

    az_consumption = _azure_consumption_by_year(inputs)
    az_migration = _migration_costs_by_year(inputs)

    # Existing Azure run rate
    existing_az = [0.0] * (YEARS + 1)
    if inputs.azure_run_rate.include_in_business_case == YesNo.YES:
        monthly = inputs.azure_run_rate.monthly_spend_usd
        for yr in range(1, YEARS + 1):
            existing_az[yr] = monthly * 12

    # Forward depreciation: slice from LOOKBACK position
    depr_servers = depreciation.servers.forward_depreciation
    depr_storage = depreciation.storage.forward_depreciation
    depr_nw = depreciation.network_fitout.forward_depreciation

    for yr in range(YEARS + 1):
        # --- Status Quo ---
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

        # --- Azure Case: retained on-prem ---
        fc.az_server_depreciation[yr] = depr_servers[yr] * (1 - _avg_ramp(inputs, yr))
        fc.az_server_maintenance[yr] = retained.server_maintenance[yr]
        fc.az_storage_depreciation[yr] = depr_storage[yr] * (1 - _avg_ramp(inputs, yr))
        fc.az_storage_maintenance[yr] = retained.storage_maintenance[yr]
        fc.az_storage_backup_cost[yr] = retained.backup_storage_cost[yr]
        fc.az_storage_dr_cost[yr] = retained.dr_storage_cost[yr]
        fc.az_nw_depreciation[yr] = depr_nw[yr] * (1 - _avg_ramp(inputs, yr))
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

        # --- Azure Case: Azure-only costs ---
        fc.az_azure_consumption[yr] = az_consumption[yr]
        fc.az_migration_costs[yr] = az_migration[yr]
        fc.az_existing_azure_run_rate[yr] = existing_az[yr]

    return fc


def _avg_ramp(inputs: BusinessCaseInputs, year: int) -> float:
    if year == 0 or not inputs.consumption_plans:
        return 0.0
    return sum(cp.migration_ramp_pct[year - 1] for cp in inputs.consumption_plans) / len(inputs.consumption_plans)
