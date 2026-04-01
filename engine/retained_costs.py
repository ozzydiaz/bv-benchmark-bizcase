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
from .productivity import compute as _compute_productivity


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

    Billing-model conventions (validated against workbook 'Cash Flow Output - Detailed'):

      Hardware maintenance  — current-year ramp; terminates the moment servers leave.
      DC facilities         — lagged ramp (current sq value); physical space can't be
                             vacated the same day VMs migrate.
      Virtualization        — lagged ramp + lagged sq (yr-1); annual subscription,
                             cancel takes effect next renewal.
      Windows/SQL licenses  — BYOL/AHB: SA obligations persist regardless of ramp;
                             cost = prior-year sq value (renewal priced at prior year).
      Windows/SQL ESU       — lagged ramp + lagged sq; drops to $0 once in Azure
                             (AHB provides free ESU coverage).
      Backup/DR software    — lagged ramp + lagged sq; annual subscription renewal.
      IT admin              — lagged ramp + lagged sq + productivity floor from D31.
      Backup/DR storage     — lagged ramp + lagged sq; on-prem infrastructure persists
                             through transition year before Azure Backup takes over.
    """
    retained = RetainedCosts()
    plans = inputs.consumption_plans
    dc_exit = inputs.datacenter.dc_exit_type.value  # 'Static' or 'Proportional'

    # Pre-compute productivity benefit for IT admin floor (D31 toggle)
    pb = _compute_productivity(inputs, benchmarks)
    # Azure IT admin floor: baseline headcount minus headcount saved = what remains in cloud
    azure_it_floor = max(0.0, status_quo.system_admin_staff[0] - pb.annual_benefit_full)

    for yr in range(YEARS + 1):
        # ── current-year ramp (hardware: terminates immediately on migration) ──
        avg_ramp = _combined_ramp(plans, yr)
        hw_fraction = 1.0 - avg_ramp

        # ── lagged ramp (everything else: 1-year billing lag) ──
        prev_yr = max(0, yr - 1)
        lagged_ramp = _combined_ramp(plans, prev_yr)
        lagged_fraction = 1.0 - lagged_ramp

        # DC fraction: Proportional or Static exit type (both use lagged ramp)
        if dc_exit == "Proportional":
            dc_fraction = lagged_fraction
        else:  # Static
            dc_fraction = 0.0 if lagged_ramp >= 1.0 else 1.0

        # ── Hardware maintenance — terminates when migrated ──
        retained.server_maintenance[yr]  = status_quo.server_maintenance[yr]  * hw_fraction
        retained.storage_maintenance[yr] = status_quo.storage_maintenance[yr] * hw_fraction
        retained.network_maintenance[yr] = status_quo.network_maintenance[yr] * hw_fraction

        # ── DC facilities — 1-year lag (physical space persists through migration year) ──
        retained.dc_lease_space[yr] = status_quo.dc_lease_space[yr] * dc_fraction
        retained.dc_power[yr]       = status_quo.dc_power[yr]       * dc_fraction
        retained.bandwidth[yr]      = status_quo.bandwidth[yr]      * dc_fraction

        # ── Virtualization — annual subscription, 1-year lag ──
        retained.virtualization_licenses[yr] = (
            status_quo.virtualization_licenses[prev_yr] * lagged_fraction
        )

        # ── Windows / SQL Server licenses — BYOL/AHB: SA obligation persists in Azure ──
        # License renewals are priced at the prior-year pCore count; the obligation
        # does not disappear when VMs move to Azure (you still pay SA to use AHB).
        retained.windows_server_licenses[yr] = status_quo.windows_server_licenses[prev_yr]
        retained.sql_server_licenses[yr]     = status_quo.sql_server_licenses[prev_yr]

        # ── Windows / SQL ESU — 1-year lag; covered free by AHB once in Azure ──
        retained.windows_esu[yr] = status_quo.windows_esu[prev_yr] * lagged_fraction
        retained.sql_esu[yr]     = status_quo.sql_esu[prev_yr]     * lagged_fraction

        # ── Backup / DR software — annual subscription, 1-year lag ──
        retained.backup_software[yr] = status_quo.backup_software[prev_yr] * lagged_fraction
        retained.dr_software[yr]     = status_quo.dr_software[prev_yr]     * lagged_fraction

        # ── IT admin — 1-year lag + D31 productivity floor ──
        sq_it_prev = status_quo.system_admin_staff[prev_yr]
        if inputs.incorporate_productivity_benefit == YesNo.YES:
            retained.system_admin_staff[yr] = max(
                sq_it_prev * lagged_fraction, azure_it_floor
            )
        else:
            retained.system_admin_staff[yr] = sq_it_prev * lagged_fraction

        # ── Backup / DR storage — 1-year lag ──
        # When in Azure Consumption: Y0/Y1 = full on-prem cost (transition period),
        # then drops to 0 once migration is complete (billed via Azure Consumption).
        # When not in Azure Consumption: declines proportionally with ramp.
        retained.backup_storage_cost[yr] = (
            status_quo.backup_storage_cost[prev_yr] * lagged_fraction
        )
        retained.dr_storage_cost[yr] = (
            status_quo.dr_storage_cost[prev_yr] * lagged_fraction
        )

    return retained
