"""
Status Quo Estimation engine.

Replicates the 'Status Quo Estimation' sheet: computes the full 10-year
on-premises cost profile (CAPEX + OPEX) for all workloads combined.

All values are in USD. Local currency conversion is applied at the output
layer, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import BenchmarkConfig, BusinessCaseInputs, PriceLevel, WorkloadInventory, YesNo


YEARS = 10  # number of projection years (Y1–Y10); Y0 = baseline


@dataclass
class StatusQuoCosts:
    """
    Annual on-premises cost breakdown for each year Y0–Y10.
    Index 0 = Y0 (baseline/current state), indices 1–10 = projection years.
    All in USD.
    """
    # CAPEX — acquisition costs (one-time, recognized at asset replacement)
    server_acquisition: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    storage_acquisition: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    network_fitout_acquisition: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # OPEX — hardware maintenance
    server_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    storage_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    network_maintenance: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # OPEX — backup & DR storage (on-prem)
    backup_storage_cost: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    dr_storage_cost: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # OPEX — DC facilities
    dc_lease_space: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    dc_power: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # OPEX — bandwidth
    bandwidth: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # OPEX — licenses
    virtualization_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    windows_server_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sql_server_licenses: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    windows_esu: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sql_esu: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    backup_software: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    dr_software: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # OPEX — IT admin (system administrators)
    system_admin_staff: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    def total_capex(self) -> list[float]:
        return [
            self.server_acquisition[i] + self.storage_acquisition[i] + self.network_fitout_acquisition[i]
            for i in range(YEARS + 1)
        ]

    def total_opex(self) -> list[float]:
        categories = [
            self.server_maintenance, self.storage_maintenance, self.network_maintenance,
            self.backup_storage_cost, self.dr_storage_cost,
            self.dc_lease_space, self.dc_power, self.bandwidth,
            self.virtualization_licenses, self.windows_server_licenses, self.sql_server_licenses,
            self.windows_esu, self.sql_esu, self.backup_software, self.dr_software,
            self.system_admin_staff,
        ]
        return [sum(cat[i] for cat in categories) for i in range(YEARS + 1)]

    def total(self) -> list[float]:
        capex = self.total_capex()
        opex = self.total_opex()
        return [capex[i] + opex[i] for i in range(YEARS + 1)]


def _server_acquisition_cost(wl: WorkloadInventory, bm: BenchmarkConfig) -> float:
    """Total server hardware acquisition cost for one workload (USD)."""
    pcores = wl.est_allocated_pcores_incl_hosts
    pmem_gb = wl.allocated_vmemory_gb * bm.vmem_to_pmem_ratio
    return pcores * bm.server_cost_per_core + pmem_gb * bm.server_cost_per_gb_memory


def _storage_acquisition_cost(wl: WorkloadInventory, bm: BenchmarkConfig) -> float:
    """Extra storage acquisition cost (above what's bundled with servers)."""
    servers = wl.est_physical_servers_incl_hosts
    bundled_gb = servers * bm.storage_gb_included_in_server
    extra_gb = max(0.0, wl.allocated_storage_gb - bundled_gb)
    return extra_gb * bm.storage_cost_per_gb


def _network_fitout_cost(wl: WorkloadInventory, bm: BenchmarkConfig) -> float:
    """Network hardware and fitout acquisition cost for one workload."""
    servers = wl.est_physical_servers_incl_hosts
    num_cabinets = servers / bm.servers_per_cabinet
    num_core_routers = bm.core_routers_per_dc  # per datacentre — simplified: 1 DC per workload
    num_agg_routers = num_core_routers * bm.aggregate_routers_per_core
    num_access_sw = num_core_routers * bm.access_switches_per_core
    num_lb = num_core_routers * bm.load_balancers_per_core

    return (
        num_cabinets * bm.cabinet_cost
        + num_core_routers * bm.core_router_cost
        + num_agg_routers * bm.aggregate_router_cost
        + num_access_sw * bm.access_switch_cost
        + num_lb * bm.load_balancer_cost
    )


def _dc_power_kw(wl: WorkloadInventory, bm: BenchmarkConfig) -> float:
    """Estimated annual DC power draw in kW for one workload."""
    pcores = wl.est_allocated_pcores_incl_hosts
    storage_tb = wl.allocated_storage_gb * bm.gb_to_tb
    # Server power: thermal design power × load factor × watt-to-kWh
    server_kwh_yr = pcores * bm.thermal_design_power_watt_yr_per_core * bm.on_prem_load_factor * bm.watt_to_kwh * bm.hours_per_year
    storage_kwh_yr = storage_tb * bm.storage_power_kwh_yr_per_tb
    total_it_kwh_yr = server_kwh_yr + storage_kwh_yr
    # PUE scales total IT power; divide by hours to get average kW
    total_kw = (total_it_kwh_yr * bm.on_prem_pue * (1 + bm.unused_power_overhead_pct)) / bm.hours_per_year
    return total_kw


def compute(inputs: BusinessCaseInputs, benchmarks: BenchmarkConfig) -> StatusQuoCosts:
    """
    Compute 10-year status quo on-premises costs for all workloads.

    Returns a StatusQuoCosts object where index 0 = Y0 baseline
    and indices 1–10 are forward projections with growth applied.
    """
    costs = StatusQuoCosts()
    g = inputs.hardware.expected_future_growth_rate
    win_level = inputs.pricing.windows_server_price_level
    sql_level = inputs.pricing.sql_server_price_level

    # Aggregate baseline values across all workloads
    total_vms = sum(wl.total_vms_and_physical for wl in inputs.workloads)
    total_pcores = sum(wl.est_allocated_pcores_incl_hosts for wl in inputs.workloads)
    total_pcores_win = sum(wl.pcores_with_windows_server for wl in inputs.workloads)
    total_pcores_win_esu = sum(wl.pcores_with_windows_esu for wl in inputs.workloads)
    total_pcores_sql = sum(wl.pcores_with_sql_server or 0 for wl in inputs.workloads)
    total_pcores_sql_esu = sum(wl.pcores_with_sql_esu or 0 for wl in inputs.workloads)
    total_pcores_virt = sum(wl.pcores_with_virtualization for wl in inputs.workloads)
    total_backup_vms = sum(
        wl.total_vms_and_physical for wl, cp in zip(inputs.workloads, inputs.consumption_plans)
        if cp.backup_software_in_consumption == YesNo.NO and cp.backup_activated == YesNo.YES
    )
    total_dr_vms = sum(
        wl.total_vms_and_physical for wl, cp in zip(inputs.workloads, inputs.consumption_plans)
        if cp.dr_software_in_consumption == YesNo.NO and cp.dr_activated == YesNo.YES
    )
    total_backup_gb = sum(
        (wl.backup_size_gb or 0) for wl, cp in zip(inputs.workloads, inputs.consumption_plans)
        if cp.backup_storage_in_consumption == YesNo.NO and cp.backup_activated == YesNo.YES
    )
    total_dr_gb = sum(
        (wl.dr_size_gb or 0) for wl, cp in zip(inputs.workloads, inputs.consumption_plans)
        if cp.dr_storage_in_consumption == YesNo.NO and cp.dr_activated == YesNo.YES
    )

    # Baseline acquisition costs (Y0)
    base_server_acq = sum(_server_acquisition_cost(wl, benchmarks) for wl in inputs.workloads)
    base_storage_acq = sum(_storage_acquisition_cost(wl, benchmarks) for wl in inputs.workloads)
    base_nw_acq = sum(_network_fitout_cost(wl, benchmarks) for wl in inputs.workloads)
    base_dc_kw = sum(_dc_power_kw(wl, benchmarks) for wl in inputs.workloads)

    depr_life = inputs.hardware.depreciation_life_years
    actual_life = inputs.hardware.actual_usage_life_years

    for yr in range(YEARS + 1):
        growth = (1 + g) ** yr

        # --- CAPEX: hardware refreshes (based on depreciation schedule) ---
        acq_factor = growth * (depr_life / actual_life)
        costs.server_acquisition[yr] = base_server_acq * acq_factor / depr_life
        costs.storage_acquisition[yr] = base_storage_acq * acq_factor / depr_life
        costs.network_fitout_acquisition[yr] = base_nw_acq * acq_factor / depr_life

        # --- OPEX: maintenance ---
        costs.server_maintenance[yr] = base_server_acq * growth * benchmarks.server_hw_maintenance_pct
        costs.storage_maintenance[yr] = base_storage_acq * growth * benchmarks.storage_hw_maintenance_pct
        costs.network_maintenance[yr] = base_nw_acq * growth * benchmarks.network_hw_maintenance_pct

        # --- OPEX: backup & DR storage ---
        costs.backup_storage_cost[yr] = total_backup_gb * benchmarks.backup_storage_cost_per_gb_yr * growth
        costs.dr_storage_cost[yr] = total_dr_gb * benchmarks.dr_storage_cost_per_gb_yr * growth

        # --- OPEX: DC facilities ---
        kw = base_dc_kw * growth
        costs.dc_lease_space[yr] = kw * benchmarks.space_cost_per_kw_month * 12
        costs.dc_power[yr] = kw * benchmarks.power_cost_per_kw_month * 12

        # --- OPEX: bandwidth ---
        costs.bandwidth[yr] = inputs.datacenter.num_interconnects_to_terminate * benchmarks.interconnect_cost_per_yr * growth

        # --- OPEX: licenses ---
        costs.virtualization_licenses[yr] = total_pcores_virt * growth * benchmarks.virtualization_license_per_core_yr
        costs.windows_server_licenses[yr] = total_pcores_win * growth * benchmarks.windows_license_per_core(win_level)
        costs.sql_server_licenses[yr] = total_pcores_sql * growth * benchmarks.sql_license_per_core(sql_level)
        costs.windows_esu[yr] = total_pcores_win_esu * growth * benchmarks.windows_esu_per_core(win_level)
        costs.sql_esu[yr] = total_pcores_sql_esu * growth * benchmarks.sql_esu_per_core(sql_level)
        costs.backup_software[yr] = total_backup_vms * benchmarks.backup_software_per_vm_yr * growth
        costs.dr_software[yr] = total_dr_vms * benchmarks.dr_software_per_vm_yr * growth

        # --- OPEX: IT admin ---
        num_admins = total_vms * growth / benchmarks.vms_per_sysadmin
        costs.system_admin_staff[yr] = num_admins * benchmarks.sysadmin_fully_loaded_cost_yr

    return costs
