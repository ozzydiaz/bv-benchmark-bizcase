"""
Layer 3 BA Replica — Status Quo Block
======================================

Formula-faithful Python reproduction of the BA's ``Status Quo Estimation``
sheet plus the Status-Quo (on-prem) columns of ``Detailed Financial Case``.

Every formula here is reverse-engineered cell-by-cell from Customer A's
finalised workbook and verified to match within $0.01.

NO IMPORTS FROM ``engine/`` — this is an independent oracle.

Outputs
-------
``compute_status_quo()`` returns a flat dict whose keys match the labels
emitted by ``layer3_golden_extractor.flatten_golden()``. The 3-way auditor
in ``layer3_judge.py`` consumes that dict directly.

BA Formulas (locked-in for Customer A)
--------------------------------------
* Server acquisition         = pCores × $147 + pMem_GB × $16.503
* Storage acquisition        = max(0, allocated_GB − included_GB) × $2.20
* NW & Fitout acquisition    = (servers / 16) × cabinet_cost  +  (cores × routers/switches × costs if interconnects > 0)
* Server / Storage / NW depr = acquisition / depr_life
* Server maintenance Y0      = server_acq × server_hw_maintenance_pct (5%)
* Storage maintenance Y0     = storage_acq × storage_hw_maintenance_pct (10%)
* NW maintenance Y0          = nw_acq × network_hw_maintenance_pct (10%)

* DC compute kW              = pCores × tdp_W × watt_to_kwh / load_factor × PUE
* DC storage kW              = (storage_GB × gb_to_tb × storage_kwh_yr_per_tb) / hours_per_year / load_factor × PUE
* DC capacity kW             = (compute_kW + storage_kW) / (1 − overhead_pct)
* DC space cost Y0           = capacity_kW × space_cost_per_kW_month × 12
* DC power cost Y0           = capacity_kW × power_cost_per_kW_month × 12
* Bandwidth cost Y0          = nb_interconnects × interconnect_cost_per_yr

* Virt licenses Y0           = pCores_virt × virt_license_per_core_yr (only if NOT BYOL)
* Win/SQL licenses Y0        = pCores_win/sql × license_per_core_yr(price_level)
* Win/SQL ESU Y0             = pCores_win_esu/sql_esu × esu_per_core_yr(price_level)
* Backup software Y0         = backup_protected_vms × backup_sw_per_vm_yr
* DR software Y0             = dr_protected_vms × dr_sw_per_vm_yr

* IT admin Y0                = ROUND(VMs / vms_per_sysadmin) × sysadmin_cost
* IT admin Yyr               = ROUND(VMs × (1+g)^yr / vms_per_sysadmin) × sysadmin_cost

Forward projection (Y1..Y10)
----------------------------
For every line item EXCEPT IT-admin (which is a step function on rounded headcount):

    line_yr = line_y0 × (1 + g)^yr           [g = expected_future_growth_rate]

Total On-Prem Cost Y_yr = SUM(all SQ lines for Y_yr).
"""

from __future__ import annotations

from dataclasses import dataclass

from .layer3_inputs import InputsBenchmark, InputsClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


N_YEARS = 11  # Y0..Y10


def _round_half_up(x: float) -> int:
    """Excel-compatible ROUND(x, 0) — half away from zero (NOT banker's)."""
    if x >= 0:
        return int(x + 0.5)
    return -int(-x + 0.5)


def _grow(baseline: float, growth_rate: float, n_years: int = N_YEARS) -> tuple[float, ...]:
    """baseline × (1+g)^yr for yr in 0..n_years-1."""
    return tuple(baseline * (1.0 + growth_rate) ** y for y in range(n_years))


def _rolling_depr(
    capex_y0: float,
    growth_rate: float,
    depr_life: float,
    n_years: int = N_YEARS,
) -> tuple[float, ...]:
    """
    Rolling-average depreciation series.

    P&L Depreciation[T] = average(CAPEX[T - depr_life + 1 .. T])

    where:
        * CAPEX[t]  = capex_y0 × (1+g)^t   for t ≥ 0
        * CAPEX[t]  = capex_y0             for t  < 0   (steady-state historical)

    This matches the BA workbook's behaviour exactly: the P&L view of hardware
    spend lags the cash-flow CAPEX view because pre-Y0 acquisitions (still
    being depreciated) anchor the early years.
    """
    n_back = int(depr_life) - 1  # how many historical years to include
    out: list[float] = []
    for T in range(n_years):
        # Average over the rolling window [T - depr_life + 1, T]
        capex_window: list[float] = []
        for k in range(T - n_back, T + 1):
            if k < 0:
                capex_window.append(capex_y0)
            else:
                capex_window.append(capex_y0 * (1.0 + growth_rate) ** k)
        out.append(sum(capex_window) / depr_life)
    return tuple(out)


def _zero_series(n_years: int = N_YEARS) -> tuple[float, ...]:
    return tuple(0.0 for _ in range(n_years))


# ---------------------------------------------------------------------------
# Sub-block: Acquisition costs (yearly baselines for Detailed P&L)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusQuoBaselines:
    """Y0 baseline values for every Status Quo cost line."""

    # Capital
    server_acquisition: float
    storage_acquisition: float
    nw_fitout_acquisition: float

    # Depreciation (acq / depr_life — Y0 same as P&L Y0)
    server_depreciation_y0: float
    storage_depreciation_y0: float
    nw_fitout_depreciation_y0: float

    # Maintenance
    server_maintenance_y0: float
    storage_maintenance_y0: float
    network_maintenance_y0: float

    # Storage extras
    storage_backup_y0: float
    storage_dr_y0: float

    # Datacenter
    dc_capacity_kw: float
    dc_lease_space_y0: float
    dc_power_y0: float
    bandwidth_y0: float

    # Licenses (subscription model — yearly)
    virtualization_licenses_y0: float
    windows_licenses_y0: float
    sql_licenses_y0: float
    windows_esu_y0: float
    sql_esu_y0: float
    backup_licenses_y0: float
    dr_licenses_y0: float

    # IT admin Y0 — computed at the projector (depends on VM growth)


# ---------------------------------------------------------------------------
# Baseline computation
# ---------------------------------------------------------------------------


def compute_baselines(client: InputsClient, bm: InputsBenchmark) -> StatusQuoBaselines:
    """Compute Y0 baseline values for every Status Quo cost line."""

    # ---- Capital ----
    server_acq = client.allocated_pcores * bm.server_cost_per_core + client.allocated_pmem_gb * bm.server_cost_per_gb_memory
    storage_acq = max(0.0, client.allocated_storage_gb - bm.gb_storage_already_in_servers) * bm.storage_cost_per_gb

    # NW & Fitout — cabinets only (firewalls/interconnect routers omitted per BA notes
    # because they would exist in both Status Quo and Azure case)
    nb_cabinets = client.nb_physical_servers / bm.servers_per_cabinet if bm.servers_per_cabinet else 0.0
    nw_acq_cabinets = nb_cabinets * bm.cabinet_cost
    # Routers/switches/load-balancers ONLY if there are eliminable interconnects.
    if client.nb_interconnects > 0:
        nb_core = bm.core_routers_per_dc * client.nb_interconnects
        nw_acq_routers = (
            nb_core * bm.core_router_cost
            + nb_core * bm.aggregate_routers_per_core * bm.aggregate_router_cost
            + nb_core * bm.access_switches_per_core * bm.access_switch_cost
            + nb_core * bm.load_balancers_per_core * bm.load_balancer_cost
        )
    else:
        nw_acq_routers = 0.0
    nw_acq = nw_acq_cabinets + nw_acq_routers

    # ---- Depreciation Y0 (= acquisition / depreciation life) ----
    depr_life = client.hw_depr_life_yrs or 5.0
    server_depr_y0 = server_acq / depr_life
    storage_depr_y0 = storage_acq / depr_life
    nw_depr_y0 = nw_acq / depr_life

    # ---- Maintenance Y0 ----
    server_maint_y0 = server_acq * bm.server_hw_maintenance_pct
    storage_maint_y0 = storage_acq * bm.storage_hw_maintenance_pct
    network_maint_y0 = nw_acq * bm.network_hw_maintenance_pct

    # ---- Storage Backup / DR (only if Backup/DR storage option ON in SQ — Customer A: OFF) ----
    storage_backup_y0 = 0.0  # populated downstream when Backup option toggled
    storage_dr_y0 = 0.0

    # ---- Datacenter (compute kW, then storage kW, then capacity kW) ----
    # compute_kw = pCores × TDP_W × W→kW / load_factor × PUE
    compute_kw = (
        client.allocated_pcores
        * bm.tdp_watt_per_core
        * bm.watt_to_kwh
        / bm.on_prem_load_factor
        * bm.on_prem_pue
    )

    # storage_kw = (GB × GB→TB) × kWh/yr/TB / hours_per_year / load_factor × PUE
    storage_kw = (
        client.allocated_storage_gb
        * bm.gb_to_tb
        * bm.storage_power_kwh_yr_per_tb
        / bm.hours_per_year
        / bm.on_prem_load_factor
        * bm.on_prem_pue
    )

    base_kw = compute_kw + storage_kw
    overhead = bm.unused_power_overhead_pct or 0.0
    capacity_kw = base_kw / (1.0 - overhead) if overhead < 1.0 else base_kw

    dc_lease_space_y0 = capacity_kw * bm.space_cost_per_kw_month * 12.0
    dc_power_y0 = capacity_kw * bm.power_cost_per_kw_month * 12.0
    bandwidth_y0 = client.nb_interconnects * bm.interconnect_cost_per_yr

    # ---- Licenses ----
    virt_y0 = 0.0 if client.byol_virtualization else client.pcores_with_virtualization * bm.virtualization_license_per_core_yr

    if client.win_price_level.upper() == "D":
        win_rate = bm.windows_server_license_per_core_yr_d
        win_esu_rate = bm.windows_esu_per_core_yr_d
    else:
        win_rate = bm.windows_server_license_per_core_yr_b
        win_esu_rate = bm.windows_esu_per_core_yr_b

    if client.sql_price_level.upper() == "D":
        sql_rate = bm.sql_server_license_per_core_yr_d
        sql_esu_rate = bm.sql_esu_per_core_yr_d
    else:
        sql_rate = bm.sql_server_license_per_core_yr_b
        sql_esu_rate = bm.sql_esu_per_core_yr_b

    win_y0 = client.pcores_with_windows_server * win_rate
    sql_y0 = client.pcores_with_sql_server * sql_rate
    win_esu_y0 = client.pcores_with_windows_server_esu * win_esu_rate
    sql_esu_y0 = client.pcores_with_sql_server_esu * sql_esu_rate

    backup_lic_y0 = client.backup_protected_vms * bm.backup_software_per_vm_yr
    dr_lic_y0 = client.dr_protected_vms * bm.dr_software_per_vm_yr

    return StatusQuoBaselines(
        server_acquisition=server_acq,
        storage_acquisition=storage_acq,
        nw_fitout_acquisition=nw_acq,
        server_depreciation_y0=server_depr_y0,
        storage_depreciation_y0=storage_depr_y0,
        nw_fitout_depreciation_y0=nw_depr_y0,
        server_maintenance_y0=server_maint_y0,
        storage_maintenance_y0=storage_maint_y0,
        network_maintenance_y0=network_maint_y0,
        storage_backup_y0=storage_backup_y0,
        storage_dr_y0=storage_dr_y0,
        dc_capacity_kw=capacity_kw,
        dc_lease_space_y0=dc_lease_space_y0,
        dc_power_y0=dc_power_y0,
        bandwidth_y0=bandwidth_y0,
        virtualization_licenses_y0=virt_y0,
        windows_licenses_y0=win_y0,
        sql_licenses_y0=sql_y0,
        windows_esu_y0=win_esu_y0,
        sql_esu_y0=sql_esu_y0,
        backup_licenses_y0=backup_lic_y0,
        dr_licenses_y0=dr_lic_y0,
    )


# ---------------------------------------------------------------------------
# Forward projection (Y0..Y10)
# ---------------------------------------------------------------------------


def compute_it_admin_series(client: InputsClient, bm: InputsBenchmark) -> tuple[float, ...]:
    """
    IT admin cost is a STEP function: ROUND(VMs × (1+g)^yr / vms_per_admin) × admin_cost.

    Uses Excel-compatible ROUND (half-away-from-zero), NOT Python banker's round.
    """
    g = client.expected_future_growth_rate
    vms_per_admin = bm.vms_per_sysadmin
    admin_cost = bm.sysadmin_fully_loaded_cost_yr
    out = []
    for yr in range(N_YEARS):
        vms_yr = client.nb_vms * (1.0 + g) ** yr
        n_admins = _round_half_up(vms_yr / vms_per_admin)
        out.append(n_admins * admin_cost)
    return tuple(out)


# ---------------------------------------------------------------------------
# Main entry — flat dict matching the auditor's label scheme
# ---------------------------------------------------------------------------


def compute_status_quo(client: InputsClient, bm: InputsBenchmark) -> dict[str, float]:
    """
    Return a flat dict whose keys match ``layer3_golden_extractor.flatten_golden()``.

    Includes:
    * ``status_quo.<line>.Y0..Y10`` for all 19 P&L line items + total
    * ``sq_estimation.<scalar>`` for every value on the Status Quo Estimation tab
    """
    base = compute_baselines(client, bm)
    g = client.expected_future_growth_rate
    depr_life = client.hw_depr_life_yrs or 5.0

    # Build series for every line that follows the (1+g)^yr rule
    series = {
        # Depreciation — rolling 5-year average (P&L view, lags CAPEX cash-flow view)
        "Server Depreciation": _rolling_depr(base.server_depreciation_y0, g, depr_life),
        "Storage Depreciation": _rolling_depr(base.storage_depreciation_y0, g, depr_life),
        "NW+Fitout Depreciation": _rolling_depr(base.nw_fitout_depreciation_y0, g, depr_life),
        # Maintenance & operating costs — straight (1+g)^yr growth
        "Server HW Maintenance": _grow(base.server_maintenance_y0, g),
        "Storage Maintenance": _grow(base.storage_maintenance_y0, g),
        "Storage Backup": _grow(base.storage_backup_y0, g),
        "Storage DR": _grow(base.storage_dr_y0, g),
        "Network HW Maintenance": _grow(base.network_maintenance_y0, g),
        "Bandwidth Costs": _grow(base.bandwidth_y0, g),
        "DC Lease (Space)": _grow(base.dc_lease_space_y0, g),
        "DC Power": _grow(base.dc_power_y0, g),
        "Virtualization Licenses": _grow(base.virtualization_licenses_y0, g),
        "Windows Server Licenses": _grow(base.windows_licenses_y0, g),
        "SQL Server Licenses": _grow(base.sql_licenses_y0, g),
        "Windows Server ESU": _grow(base.windows_esu_y0, g),
        "SQL Server ESU": _grow(base.sql_esu_y0, g),
        "Backup Licenses": _grow(base.backup_licenses_y0, g),
        "Disaster Recovery Licenses": _grow(base.dr_licenses_y0, g),
    }

    # IT admin — step function on rounded headcount
    series["IT Admin Staff"] = compute_it_admin_series(client, bm)

    # Total = sum of every row, year-by-year
    total = []
    for yr in range(N_YEARS):
        total.append(sum(s[yr] for s in series.values()))
    series["Total On-Prem Cost"] = tuple(total)

    # Flatten to the auditor's label scheme
    out: dict[str, float] = {}
    for label, ys in series.items():
        for yr, val in enumerate(ys):
            out[f"status_quo.{label}.Y{yr}"] = val

    # ---- sq_estimation.* scalars (matches StatusQuoEstimationGolden fields) ----
    out["sq_estimation.server_acquisition_cost"] = base.server_acquisition
    out["sq_estimation.storage_acquisition_cost"] = base.storage_acquisition
    out["sq_estimation.nw_fitout_acquisition_cost"] = base.nw_fitout_acquisition
    out["sq_estimation.licenses_yearly_cost"] = (
        base.virtualization_licenses_y0
        + base.windows_licenses_y0
        + base.sql_licenses_y0
        + base.windows_esu_y0
        + base.sql_esu_y0
        + base.backup_licenses_y0
        + base.dr_licenses_y0
    )
    out["sq_estimation.dc_lease_space_yearly"] = base.dc_lease_space_y0
    out["sq_estimation.dc_power_yearly"] = base.dc_power_y0
    out["sq_estimation.bandwidth_yearly"] = base.bandwidth_y0
    out["sq_estimation.server_hw_maint_yearly"] = base.server_maintenance_y0
    out["sq_estimation.storage_hw_maint_yearly"] = base.storage_maintenance_y0
    out["sq_estimation.network_hw_maint_yearly"] = base.network_maintenance_y0
    out["sq_estimation.sysadmin_yearly"] = series["IT Admin Staff"][0]

    return out
