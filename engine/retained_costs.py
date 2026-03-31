"""
Retained Costs Estimation engine.

Replicates the 'Retained Costs Estimation' sheet: computes the subset of
on-premises costs that remain during and after the migration (i.e., the
Azure scenario's on-prem cost tail), declining in proportion to the
workload migration ramp-up schedule.

All values are in USD.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import BenchmarkConfig, BusinessCaseInputs, YesNo
from .status_quo import StatusQuoCosts, YEARS


@dataclass
class RetainedCosts:
    """
    Per-year on-premises costs that are retained in the Azure scenario.
    Mirrors the structure of StatusQuoCosts but values decay as migration progresses.
    """
    virtualization_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    windows_server_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sql_server_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    windows_esu: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sql_esu: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    backup_software: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    dr_software: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    system_admin_staff: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    dc_lease_space: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    dc_power: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    bandwidth: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    server_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    storage_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    network_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    backup_storage_cost: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    dr_storage_cost: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    def total(self) -> list[float]:
        categories = [
            self.virtualization_licenses, self.windows_server_licenses,
            self.sql_server_licenses, self.windows_esu, self.sql_esu,
            self.backup_software, self.dr_software, self.system_admin_staff,
            self.dc_lease_space, self.dc_power, self.bandwidth,
            self.server_maintenance, self.storage_maintenance, self.network_maintenance,
            self.backup_storage_cost, self.dr_storage_cost,
        ]
        return [sum(cat[i] for cat in categories) for i in range(YEARS + 1)]


def _combined_ramp(consumption_plans: list, year: int) -> float:
    """
    Returns the weighted-average migration ramp-up % across all workloads
    at a given year index (1-based). Year 0 = 0% migrated.
    """
    if year == 0:
        return 0.0
    if not consumption_plans:
        return 0.0
    ramp_sum = sum(cp.migration_ramp_pct[year - 1] for cp in consumption_plans)
    return ramp_sum / len(consumption_plans)


def _per_workload_ramp(consumption_plans: list, workload_idx: int, year: int) -> float:
    """Migration ramp % for a specific workload at a given year (1-based)."""
    if year == 0:
        return 0.0
    if workload_idx >= len(consumption_plans):
        return 0.0
    return consumption_plans[workload_idx].migration_ramp_pct[year - 1]


def compute(
    inputs: BusinessCaseInputs,
    benchmarks: BenchmarkConfig,
    status_quo: StatusQuoCosts,
) -> RetainedCosts:
    """
    Compute the on-prem costs that persist during the Azure scenario's migration ramp.

    For each cost category, retained cost = status_quo_cost × (1 - migration_ramp_pct).
    DC facilities use DC-exit-type logic ('Proportional' vs 'Static').
    """
    retained = RetainedCosts()
    g = inputs.hardware.expected_future_growth_rate
    plans = inputs.consumption_plans
    dc_exit = inputs.datacenter.dc_exit_type.value  # 'Static' or 'Proportional'

    for yr in range(YEARS + 1):
        avg_ramp = _combined_ramp(plans, yr)
        on_prem_fraction = 1.0 - avg_ramp

        # Proportional: DC costs reduce in proportion to migration progress
        # Static: DC costs stay flat until fully migrated (then drop to zero)
        if dc_exit == "Proportional":
            dc_fraction = on_prem_fraction
        else:  # Static
            dc_fraction = 0.0 if avg_ramp >= 1.0 else 1.0

        # --- License costs: decline per-workload proportionally ---
        retained.virtualization_licenses[yr] = status_quo.virtualization_licenses[yr] * on_prem_fraction
        retained.windows_server_licenses[yr] = status_quo.windows_server_licenses[yr] * on_prem_fraction
        retained.sql_server_licenses[yr] = status_quo.sql_server_licenses[yr] * on_prem_fraction
        retained.windows_esu[yr] = status_quo.windows_esu[yr] * on_prem_fraction
        retained.sql_esu[yr] = status_quo.sql_esu[yr] * on_prem_fraction
        retained.backup_software[yr] = status_quo.backup_software[yr] * on_prem_fraction
        retained.dr_software[yr] = status_quo.dr_software[yr] * on_prem_fraction

        # --- IT admin: declines with migration ---
        retained.system_admin_staff[yr] = status_quo.system_admin_staff[yr] * on_prem_fraction

        # --- DC facilities: depend on exit type ---
        retained.dc_lease_space[yr] = status_quo.dc_lease_space[yr] * dc_fraction
        retained.dc_power[yr] = status_quo.dc_power[yr] * dc_fraction

        # --- Bandwidth: eliminated when DC exited ---
        retained.bandwidth[yr] = status_quo.bandwidth[yr] * dc_fraction

        # --- Hardware maintenance: declines as servers leave ---
        retained.server_maintenance[yr] = status_quo.server_maintenance[yr] * on_prem_fraction
        retained.storage_maintenance[yr] = status_quo.storage_maintenance[yr] * on_prem_fraction
        retained.network_maintenance[yr] = status_quo.network_maintenance[yr] * on_prem_fraction

        # --- Backup / DR storage (on-prem): declines with migration ---
        retained.backup_storage_cost[yr] = status_quo.backup_storage_cost[yr] * on_prem_fraction
        retained.dr_storage_cost[yr] = status_quo.dr_storage_cost[yr] * on_prem_fraction

    return retained
