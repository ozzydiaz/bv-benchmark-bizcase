"""
Layer 3 Engine Bridge
=====================

Wires the production ``engine/*.py`` pipeline into the same flat-dict shape
as the BA-replica modules so the 3-way auditor in ``layer3_judge.py`` can
compare the engine's output directly against the BA workbook (oracle) AND
against the locked replica.

Conceptual flow::

    Customer A workbook (BA inputs)
            │
            ▼
    layer3_inputs.{Client,Benchmark,Consumption}
            │
            ├─► training/replicas/...        ─► replica dict   ─┐
            │                                                    ├─► layer3_judge.audit()
            └─► engine_bridge_l3 (this file) ─► engine dict    ─┘                │
                                                                                  ▼
                                                                          3-way scorecard
                                                                          (BA / Replica / Engine)

Only this file contains the conversion logic. The replica modules and
the engine modules remain untouched.

Public API:

* ``replica_inputs_to_engine_inputs(client, bm, cons) -> (BusinessCaseInputs, BenchmarkConfig)``
* ``compute_engine_layer3_dict(client, bm, cons) -> dict[str, float]``

The returned dict's keys MUST match the auditor's expectations
(``status_quo.<label>.Y<n>``, ``cash_flow.<label>.Y<n>``, ``headline.*``,
``five_payback.*``, ``detailed_npv.*``, ``sq_estimation.*``).
"""

from __future__ import annotations

from typing import Tuple

from engine import depreciation as engine_depreciation
from engine import financial_case as engine_financial_case
from engine import outputs as engine_outputs
from engine import retained_costs as engine_retained_costs
from engine import status_quo as engine_status_quo
from engine.models import (
    AzureRunRate,
    BenchmarkConfig,
    BusinessCaseInputs,
    ConsumptionPlan,
    DatacenterConfig,
    DCExitType,
    EngagementInfo,
    HardwareLifecycle,
    PriceLevel,
    PricingConfig,
    WorkloadInventory,
    YesNo,
)

from training.replicas.layer3_inputs import (
    InputsBenchmark,
    InputsClient,
    InputsConsumption,
)


# ---------------------------------------------------------------------------
# Conversion: replica InputsClient/Benchmark/Consumption -> engine models
# ---------------------------------------------------------------------------


def _yesno(b: bool) -> YesNo:
    return YesNo.YES if b else YesNo.NO


def _price_level(s: str) -> PriceLevel:
    return PriceLevel.B if (s or "").strip().upper() == "B" else PriceLevel.D


def _dc_exit_type(s: str) -> DCExitType:
    return DCExitType.STATIC if (s or "").strip().lower() == "static" else DCExitType.PROPORTIONAL


def replica_inputs_to_engine_inputs(
    client: InputsClient,
    bm: InputsBenchmark,
    cons: InputsConsumption,
) -> Tuple[BusinessCaseInputs, BenchmarkConfig]:
    """
    Build engine-shaped ``BusinessCaseInputs`` + ``BenchmarkConfig`` from the
    replica's input dataclasses (which were loaded from the BA workbook).

    This deliberately only carries Workload #1 — Customer A only fills out
    a single workload, matching the replica's scope.
    """
    # ---- Engagement / pricing / DC / hardware ----
    engagement = EngagementInfo(client_name=client.client_name, local_currency_name=client.currency)
    pricing = PricingConfig(
        windows_server_price_level=_price_level(client.win_price_level),
        sql_server_price_level=_price_level(client.sql_price_level),
    )
    datacenter = DatacenterConfig(
        num_datacenters_to_exit=int(client.nb_dc),
        dc_exit_type=_dc_exit_type(client.dc_exit_type),
        num_interconnects_to_terminate=int(client.nb_interconnects),
    )
    hardware = HardwareLifecycle(
        depreciation_life_years=int(client.hw_depr_life_yrs or 5),
        actual_usage_life_years=int(client.hw_actual_life_yrs or 5),
        expected_future_growth_rate=client.expected_future_growth_rate,
        hardware_renewal_during_migration_pct=client.hw_renewal_during_migration_pct,
    )

    # ---- Workload #1 inventory ----
    # The replica reads `nb_physical_servers` from D42 (incl. VM hosts);
    # the engine derives the "incl. hosts" total via vm_to_server_ratio
    # plus `num_physical_servers_excl_hosts`. So we feed the engine
    # nb_physical_servers EXCLUDING VM hosts (best estimate = D42 - D39/K11).
    incl_hosts = float(client.nb_physical_servers)
    derived_hosts = float(client.nb_vms) / max(bm.vm_to_physical_server_ratio, 0.01)
    excl_hosts = max(0.0, incl_hosts - derived_hosts)

    # CPU/memory totals — match the BA's hand-typed D47 and D52 cells.
    #
    # BA's `Status Quo Estimation!J11` reads `'1-Client Variables'!D47` and
    # `D52` directly (the user types the GRAND TOTAL pcores and pmemory). The
    # engine prefers RVtools-style derivation: `allocated_vcpu /
    # vcpu_per_core_ratio + allocated_pcores_excl_hosts` for pcores, and
    # `allocated_vmemory_gb * vmem_to_pmem_ratio + allocated_pmemory_gb_excl_hosts`
    # for pmem. To make the engine's output match BA's hand-typed totals, we
    # use the additive `*_excl_hosts` channel as a residual:
    #     residual = TOTAL - derived  (clamped to >= 0)
    # This preserves the field's documented semantics (additive non-VM source)
    # and stays compatible with the RVtools path (which fills the same field
    # with hypervisor host pcores/memory).
    vcpu_ratio = client.vcpu_per_pcore_ratio or 1.97
    derived_pcores = float(client.allocated_vcpu) / max(vcpu_ratio, 0.01)
    pcores_residual = max(0, round(client.allocated_pcores - derived_pcores))
    derived_pmem = client.allocated_vmem_gb * bm.vmem_to_pmem_ratio
    pmem_residual = max(0.0, client.allocated_pmem_gb - derived_pmem)

    workload = WorkloadInventory(
        workload_name=client.workload_name or "Workload #1",
        num_vms=int(client.nb_vms),
        num_physical_servers_excl_hosts=int(round(excl_hosts)),
        allocated_vcpu=int(client.allocated_vcpu),
        # Additive residual so engine's est_allocated_pcores_incl_hosts == D47.
        allocated_pcores_excl_hosts=pcores_residual,
        allocated_vmemory_gb=client.allocated_vmem_gb,
        # Additive residual so engine's pmem_gb in _server_acquisition_cost == D52.
        # NOTE: requires engine fix in `_server_acquisition_cost` to actually
        # consume this field (was previously ignored — RVtools path bug).
        allocated_pmemory_gb_excl_hosts=pmem_residual,
        allocated_storage_gb=client.allocated_storage_gb,
        backup_size_gb=client.backup_size_gb or None,
        backup_num_protected_vms=int(client.backup_protected_vms) if client.backup_protected_vms else None,
        dr_size_gb=client.dr_size_gb or None,
        dr_num_protected_vms=int(client.dr_protected_vms) if client.dr_protected_vms else None,
        vm_to_server_ratio=bm.vm_to_physical_server_ratio,
        byol_virtualization_for_avs=_yesno(client.byol_virtualization),
        vcpu_per_core_ratio=client.vcpu_per_pcore_ratio or 1.97,
        # Pass fractional pCore counts verbatim — BA workbook hand-types
        # fractional values (e.g., =12405/1.48 = 8381.756...) and the per-core
        # licensing rate applies to the fractional value, not a rounded int.
        pcores_with_virtualization=float(client.pcores_with_virtualization),
        pcores_with_windows_server=float(client.pcores_with_windows_server),
        pcores_with_windows_esu=float(client.pcores_with_windows_server_esu),
        pcores_with_sql_server=float(client.pcores_with_sql_server),
        pcores_with_sql_esu=float(client.pcores_with_sql_server_esu),
    )

    # ---- Consumption plan ----
    consumption = ConsumptionPlan(
        workload_name=client.workload_name or "Workload #1",
        azure_vcpu=int(client.allocated_vcpu),  # placeholder; not used by L3 engine math
        azure_memory_gb=client.allocated_vmem_gb,
        azure_storage_gb=client.allocated_storage_gb,
        migration_cost_per_vm_lc=cons.migration_cost_per_vm,
        migration_ramp_pct=list(cons.migration_ramp_eoy),
        annual_compute_consumption_lc_y10=cons.compute_consumption_y10,
        annual_storage_consumption_lc_y10=cons.storage_consumption_y10,
        annual_other_consumption_lc_y10=cons.other_consumption_y10,
        azure_consumption_discount=cons.azure_consumption_discount,
        # ACO/ECIF: replica only stores totals — feed Y1 as a single-shot, rest zero
        aco_by_year=[cons.aco_total] + [0.0] * 9,
        ecif_by_year=[cons.ecif_total] + [0.0] * 9,
        backup_activated=_yesno(cons.backup_activated),
        backup_storage_in_consumption=_yesno(cons.backup_storage_in_azure_run_rate),
        backup_software_in_consumption=_yesno(cons.backup_software_in_azure_run_rate),
        dr_activated=_yesno(cons.dr_activated),
        dr_storage_in_consumption=_yesno(cons.dr_storage_in_azure_run_rate),
        dr_software_in_consumption=_yesno(cons.dr_software_in_azure_run_rate),
    )

    # ---- Top-level inputs ----
    inputs = BusinessCaseInputs(
        engagement=engagement,
        pricing=pricing,
        datacenter=datacenter,
        hardware=hardware,
        incorporate_productivity_benefit=_yesno(client.incorporate_productivity),
        workloads=[workload],
        consumption_plans=[consumption],
        azure_run_rate=AzureRunRate(),  # Customer A: not used (default = NO)
    )

    # ---- Benchmarks ----
    benchmarks = BenchmarkConfig(
        wacc=bm.wacc,
        hours_per_year=bm.hours_per_year,
        watt_to_kwh=bm.watt_to_kwh,
        gb_to_tb=bm.gb_to_tb,
        vm_to_physical_server_ratio=bm.vm_to_physical_server_ratio,
        vmem_to_pmem_ratio=bm.vmem_to_pmem_ratio,
        server_cost_per_core=bm.server_cost_per_core,
        server_cost_per_gb_memory=bm.server_cost_per_gb_memory,
        storage_cost_per_gb=bm.storage_cost_per_gb,
        storage_gb_included_in_server=bm.gb_storage_already_in_servers,
        backup_storage_cost_per_gb_yr=bm.backup_storage_cost_per_gb_yr,
        dr_storage_cost_per_gb_yr=bm.dr_storage_cost_per_gb_yr,
        server_hw_maintenance_pct=bm.server_hw_maintenance_pct,
        storage_hw_maintenance_pct=bm.storage_hw_maintenance_pct,
        servers_per_cabinet=bm.servers_per_cabinet,
        core_routers_per_dc=bm.core_routers_per_dc,
        aggregate_routers_per_core=bm.aggregate_routers_per_core,
        access_switches_per_core=bm.access_switches_per_core,
        load_balancers_per_core=bm.load_balancers_per_core,
        cabinet_cost=bm.cabinet_cost,
        core_router_cost=bm.core_router_cost,
        aggregate_router_cost=bm.aggregate_router_cost,
        access_switch_cost=bm.access_switch_cost,
        load_balancer_cost=bm.load_balancer_cost,
        network_hw_maintenance_pct=bm.network_hw_maintenance_pct,
        windows_server_license_per_core_yr_b=bm.windows_server_license_per_core_yr_b,
        sql_server_license_per_core_yr_b=bm.sql_server_license_per_core_yr_b,
        windows_esu_per_core_yr_b=bm.windows_esu_per_core_yr_b,
        sql_esu_per_core_yr_b=bm.sql_esu_per_core_yr_b,
        windows_server_license_per_core_yr_d=bm.windows_server_license_per_core_yr_d,
        sql_server_license_per_core_yr_d=bm.sql_server_license_per_core_yr_d,
        windows_esu_per_core_yr_d=bm.windows_esu_per_core_yr_d,
        sql_esu_per_core_yr_d=bm.sql_esu_per_core_yr_d,
        virtualization_license_per_core_yr=bm.virtualization_license_per_core_yr,
        backup_software_per_vm_yr=bm.backup_software_per_vm_yr,
        dr_software_per_vm_yr=bm.dr_software_per_vm_yr,
        unused_power_overhead_pct=bm.unused_power_overhead_pct,
        space_cost_per_kw_month=bm.space_cost_per_kw_month,
        power_cost_per_kw_month=bm.power_cost_per_kw_month,
        on_prem_pue=bm.on_prem_pue,
        thermal_design_power_watt_yr_per_core=bm.tdp_watt_per_core,
        storage_power_kwh_yr_per_tb=bm.storage_power_kwh_yr_per_tb,
        on_prem_load_factor=bm.on_prem_load_factor,
        interconnect_cost_per_yr=bm.interconnect_cost_per_yr,
        vms_per_sysadmin=bm.vms_per_sysadmin,
        sysadmin_fully_loaded_cost_yr=bm.sysadmin_fully_loaded_cost_yr,
        sysadmin_working_hours_yr=bm.sysadmin_working_hours_yr,
        sysadmin_contractor_pct=bm.sysadmin_contractor_pct,
        productivity_reduction_after_migration=bm.productivity_reduction_after_migration,
        productivity_recapture_rate=bm.productivity_recapture_rate,
        nii_interest_rate=bm.nii_interest_rate,
        perpetual_growth_rate=bm.perpetual_growth_rate,
    )

    return inputs, benchmarks


# ---------------------------------------------------------------------------
# Engine output -> auditor-shaped flat dict
# ---------------------------------------------------------------------------


# Map auditor labels (used by GoldenSeries.label) → FinancialCase attribute names.
_SQ_LINE_MAP: dict[str, str] = {
    "Server Depreciation": "sq_server_depreciation",
    "Server HW Maintenance": "sq_server_maintenance",
    "Storage Depreciation": "sq_storage_depreciation",
    "Storage Maintenance": "sq_storage_maintenance",
    "Storage Backup": "sq_storage_backup_cost",
    "Storage DR": "sq_storage_dr_cost",
    "NW+Fitout Depreciation": "sq_nw_depreciation",
    "Network HW Maintenance": "sq_nw_maintenance",
    "Bandwidth Costs": "sq_bandwidth",
    "DC Lease (Space)": "sq_dc_space",
    "DC Power": "sq_dc_power",
    "Virtualization Licenses": "sq_virtualization_licenses",
    "Windows Server Licenses": "sq_windows_licenses",
    "SQL Server Licenses": "sq_sql_licenses",
    "Windows Server ESU": "sq_windows_esu",
    "SQL Server ESU": "sq_sql_esu",
    "Backup Licenses": "sq_backup_software",
    "Disaster Recovery Licenses": "sq_dr_software",
    "IT Admin Staff": "sq_system_admin",
}


def compute_engine_layer3_dict(
    client: InputsClient,
    bm: InputsBenchmark,
    cons: InputsConsumption,
) -> dict[str, float]:
    """
    Drive the production engine pipeline from BA-workbook inputs and return
    a flat dict keyed by the same labels the BA oracle uses.

    Output keyspace covers: ``status_quo.*``, ``cash_flow.*``, ``headline.*``,
    ``five_payback.*``, ``sq_estimation.*``, plus a partial ``detailed_npv.*``.

    Mismatches against the oracle / replica are exactly the engine bugs to
    surface in Step 12.
    """
    inputs, benchmarks = replica_inputs_to_engine_inputs(client, bm, cons)

    # Run the engine pipeline in canonical order.
    sq = engine_status_quo.compute(inputs, benchmarks)
    depr = engine_depreciation.compute(inputs, benchmarks)
    rc = engine_retained_costs.compute(inputs, benchmarks, sq)
    fc = engine_financial_case.compute(inputs, benchmarks, sq, rc, depr)
    summary = engine_outputs.compute(inputs, benchmarks, fc)

    out: dict[str, float] = {}

    # ----------------------------------------------------------------
    # status_quo.* — 19 P&L line items + Total On-Prem Cost (Y0..Y10)
    # ----------------------------------------------------------------
    for label, attr in _SQ_LINE_MAP.items():
        series = getattr(fc, attr)
        for yr, v in enumerate(series):
            out[f"status_quo.{label}.Y{yr}"] = float(v)

    sq_total = fc.sq_total()
    for yr, v in enumerate(sq_total):
        out[f"status_quo.Total On-Prem Cost.Y{yr}"] = float(v)

    # ----------------------------------------------------------------
    # cash_flow.* — Cash Flow view (CAPEX/OPEX, AZ retained + cloud, Savings)
    # ----------------------------------------------------------------
    sq_capex = fc.sq_capex()
    sq_opex = fc.sq_opex_cf()
    sq_total_cf = fc.sq_total_cf()
    az_capex = fc.az_capex_cf()
    az_opex = fc.az_opex_cf()
    az_consumption = list(fc.az_azure_consumption)
    az_existing = list(fc.az_existing_azure_run_rate)
    az_consumption_total = [az_consumption[i] + az_existing[i] for i in range(len(az_consumption))]
    az_migration = list(fc.az_migration_costs)  # gross (before MS funding)
    az_funding = list(fc.az_microsoft_funding)
    az_total_cf = fc.az_total_cf()
    savings_cf = fc.cf_savings()  # SQ - AZ

    n = len(sq_total_cf)
    for yr in range(n):
        out[f"cash_flow.SQ CAPEX.Y{yr}"] = float(sq_capex[yr])
        out[f"cash_flow.SQ OPEX.Y{yr}"] = float(sq_opex[yr])
        out[f"cash_flow.SQ Total CF.Y{yr}"] = float(sq_total_cf[yr])
        out[f"cash_flow.AZ CAPEX.Y{yr}"] = float(az_capex[yr])
        out[f"cash_flow.AZ OPEX.Y{yr}"] = float(az_opex[yr])
        out[f"cash_flow.AZ Consumption.Y{yr}"] = float(az_consumption_total[yr])
        out[f"cash_flow.AZ Migration.Y{yr}"] = float(az_migration[yr])
        out[f"cash_flow.AZ MS Funding.Y{yr}"] = float(az_funding[yr])
        out[f"cash_flow.AZ Total CF.Y{yr}"] = float(az_total_cf[yr])
        # Savings (SQ-AZ): BA's display floors at zero (no "negative savings")
        out[f"cash_flow.Savings (SQ-AZ).Y{yr}"] = float(max(0.0, savings_cf[yr]))
        # CF Delta = AZ - SQ (negative when Azure is cheaper)
        out[f"cash_flow.CF Delta (AZ-SQ).Y{yr}"] = float(az_total_cf[yr] - sq_total_cf[yr])
        # CF Rate = (AZ - SQ) / SQ -- BA's sign convention: positive when Azure
        # costs more than status quo, negative when Azure saves.
        rate = (az_total_cf[yr] - sq_total_cf[yr]) / sq_total_cf[yr] if sq_total_cf[yr] else 0.0
        out[f"cash_flow.CF Rate.Y{yr}"] = float(rate)

    # ----------------------------------------------------------------
    # headline.* — Summary Financial Case rows 6-12
    # ----------------------------------------------------------------
    wacc = benchmarks.wacc
    perp = benchmarks.perpetual_growth_rate

    def _npv(series: list[float], periods: int) -> float:
        return sum(series[yr] / (1 + wacc) ** yr for yr in range(1, periods + 1) if yr < len(series))

    npv_sq_10y = _npv(sq_total_cf, 10)
    npv_sq_5y = _npv(sq_total_cf, 5)
    npv_az_10y = _npv(az_total_cf, 10)
    npv_az_5y = _npv(az_total_cf, 5)

    def _tv(sq_yn: float, az_yn: float, n: int) -> float:
        if wacc <= perp:
            return 0.0
        return (sq_yn - az_yn) * (1 + perp) / (wacc - perp) / (1 + wacc) ** n

    tv_10y = _tv(sq_total_cf[10], az_total_cf[10], 10) if len(sq_total_cf) > 10 else 0.0
    tv_5y = _tv(sq_total_cf[5], az_total_cf[5], 5) if len(sq_total_cf) > 5 else 0.0

    out["headline.npv_sq_10y"] = npv_sq_10y
    out["headline.npv_sq_5y"] = npv_sq_5y
    out["headline.npv_az_10y"] = npv_az_10y
    out["headline.npv_az_5y"] = npv_az_5y
    out["headline.terminal_value_10y"] = tv_10y
    out["headline.terminal_value_5y"] = tv_5y
    out["headline.project_npv_excl_tv_10y"] = npv_sq_10y - npv_az_10y
    out["headline.project_npv_excl_tv_5y"] = npv_sq_5y - npv_az_5y
    out["headline.project_npv_with_tv_10y"] = (npv_sq_10y - npv_az_10y) + tv_10y
    out["headline.project_npv_with_tv_5y"] = (npv_sq_5y - npv_az_5y) + tv_5y

    # ROI / Payback come from the engine's 5Y CF computation (matches workbook E6/E11)
    out["headline.roi_5y_cf"] = float(summary.roi_cf)
    out["headline.payback_years"] = float(summary.payback_cf or 0.0)

    # Y10 / Y5 savings + savings rate
    # NOTE: BA's headline labels are labeled "savings" but use the (AZ - SQ) sign
    # convention -- negative means Azure saves vs status quo. Match the workbook.
    y10_savings = az_total_cf[10] - sq_total_cf[10] if len(sq_total_cf) > 10 else 0.0
    y5_savings = az_total_cf[5] - sq_total_cf[5] if len(sq_total_cf) > 5 else 0.0
    out["headline.y10_savings_10y_cf"] = float(y10_savings)
    out["headline.y10_savings_5y_cf"] = float(y5_savings)
    out["headline.y10_savings_rate_10y"] = float(y10_savings / sq_total_cf[10]) if sq_total_cf[10] else 0.0
    out["headline.y10_savings_rate_5y"] = float(y5_savings / sq_total_cf[5]) if sq_total_cf[5] else 0.0

    # ----------------------------------------------------------------
    # five_payback.* — 5Y CF with Payback sheet (engine's compute_cf_roi_and_payback)
    # ----------------------------------------------------------------
    # Reproduce the breakdown the BA sheet displays.
    sq_admin = fc.sq_system_admin
    az_admin = fc.az_system_admin

    def _sum_y1_y5(series: list[float]) -> float:
        # BA's "5Y CF with Payback" breakdown column H is the UNDISCOUNTED Y1..Y5
        # raw sum (e.g. migration_npv = -1,698,600 + -2,547,900 = -4,246,500).
        # Replica `layer3_project_npv.py` computes these as `sum(series_y1_y5)`.
        return sum(series[yr] for yr in range(1, 6) if yr < len(series))

    # Infra savings (excl IT admin) NPV
    sq_infra = [sq_capex[yr] + sq_opex[yr] - sq_admin[yr] for yr in range(n)]
    az_infra = [az_capex[yr] + az_opex[yr] - az_admin[yr] for yr in range(n)]
    infra_savings = [sq_infra[yr] - az_infra[yr] for yr in range(n)]
    infra_admin_savings = [sq_admin[yr] - az_admin[yr] for yr in range(n)]
    total_benefits = [infra_savings[yr] + infra_admin_savings[yr] for yr in range(n)]

    incremental_azure = [-az_consumption_total[yr] for yr in range(n)]
    migration = [-az_migration[yr] for yr in range(n)]
    total_costs = [incremental_azure[yr] + migration[yr] for yr in range(n)]
    net_benefits = [total_benefits[yr] + total_costs[yr] for yr in range(n)]

    out["five_payback.infra_cost_reduction_npv"] = _sum_y1_y5(infra_savings)
    out["five_payback.infra_admin_reduction_npv"] = _sum_y1_y5(infra_admin_savings)
    out["five_payback.total_benefits_npv"] = _sum_y1_y5(total_benefits)
    out["five_payback.incremental_azure_npv"] = _sum_y1_y5(incremental_azure)
    out["five_payback.migration_npv"] = _sum_y1_y5(migration)
    out["five_payback.total_costs_npv"] = _sum_y1_y5(total_costs)
    # net_benefits_npv is the only label in this block that is DISCOUNTED -- it
    # equals headline.project_npv_excl_tv_5y = NPV_SQ[5] - NPV_AZ[5].
    out["five_payback.net_benefits_npv"] = npv_sq_5y - npv_az_5y
    out["five_payback.roi_5y_cf"] = float(summary.roi_cf)
    out["five_payback.payback_years"] = float(summary.payback_cf or 0.0)

    # ----------------------------------------------------------------
    # sq_estimation.* — Status Quo Estimation tab scalars (Y0 baseline values)
    # ----------------------------------------------------------------
    # The BA's `Status Quo Estimation!C7/C19/C32` cells contain the RAW one-time
    # acquisition cost (the price you pay to buy the kit), not the annual
    # depreciation refresh.  The engine's `sq.server_acquisition[0]` is the
    # amortized refresh = `raw / depreciation_life_years` (at Y0 with
    # depr_life == actual_usage_life).  Read the engine's private helper
    # directly so the oracle key reflects the same RAW number BA writes.
    num_dcs = inputs.datacenter.num_datacenters_to_exit
    base_server_acq = sum(
        engine_status_quo._server_acquisition_cost(wl, benchmarks)
        for wl in inputs.workloads
    )
    base_storage_acq = sum(
        engine_status_quo._storage_acquisition_cost(wl, benchmarks)
        for wl in inputs.workloads
    )
    base_nw_acq = sum(
        engine_status_quo._network_fitout_cost(wl, benchmarks, num_dcs)
        for wl in inputs.workloads
    )
    out["sq_estimation.server_acquisition_cost"] = float(base_server_acq)
    out["sq_estimation.storage_acquisition_cost"] = float(base_storage_acq)
    out["sq_estimation.nw_fitout_acquisition_cost"] = float(base_nw_acq)
    out["sq_estimation.licenses_yearly_cost"] = float(
        sq.virtualization_licenses[0]
        + sq.windows_server_licenses[0]
        + sq.sql_server_licenses[0]
        + sq.windows_esu[0]
        + sq.sql_esu[0]
        + sq.backup_software[0]
        + sq.dr_software[0]
    )
    out["sq_estimation.dc_lease_space_yearly"] = float(sq.dc_lease_space[0])
    out["sq_estimation.dc_power_yearly"] = float(sq.dc_power[0])
    out["sq_estimation.bandwidth_yearly"] = float(sq.bandwidth[0])
    out["sq_estimation.server_hw_maint_yearly"] = float(sq.server_maintenance[0])
    out["sq_estimation.storage_hw_maint_yearly"] = float(sq.storage_maintenance[0])
    out["sq_estimation.network_hw_maint_yearly"] = float(sq.network_maintenance[0])
    out["sq_estimation.sysadmin_yearly"] = float(sq.system_admin_staff[0])

    # ----------------------------------------------------------------
    # detailed_npv.* — Detailed Financial Case rows 91-101
    # ----------------------------------------------------------------
    # The BA's M91-M98 block is the **Status-Quo perpetual continuation**
    # display:
    #   M92 = SQ Total CF for that year      (= row 75)
    #   M93 = Gordon TV on SQ Y10            (= M92 * (1+g_perp)/(wacc-g_perp))
    #   M94 = SQ CF discounted to today      (= M92 / (1+wacc)^year)
    #   M97 = sum of M94 across years        (= NPV of SQ-only stream excl. TV)
    #   M98 = M94 + M95                       (NPV including TV present-value)
    # i.e. these scalars use the SQ cash-flow stream, NOT the relative-savings
    # P&L stream that drives ROI.  (Headline TV / project-NPV are the
    # relative-savings versions and are mapped above via headline.*.)
    if len(sq_total_cf) > 10 and wacc > perp:
        annual_sq_pv = [sq_total_cf[t] / (1 + wacc) ** t for t in range(11)]
        out["detailed_npv.annual_npv_y1"] = annual_sq_pv[1]
        out["detailed_npv.annual_npv_y10"] = annual_sq_pv[10]
        out["detailed_npv.npv_10y_excl_tv"] = sum(annual_sq_pv[t] for t in range(1, 11))
        # Gordon TV on SQ-only Y10 cash flow (undiscounted)
        tv_sq_y10_raw = sq_total_cf[10] * (1 + perp) / (wacc - perp)
        out["detailed_npv.terminal_value_10y_raw"] = tv_sq_y10_raw
        # NPV with TV: SQ NPV + present-value of TV (discounted from Y10)
        tv_sq_y10_pv = tv_sq_y10_raw / (1 + wacc) ** 10
        out["detailed_npv.npv_with_tv_10y_raw"] = (
            out["detailed_npv.npv_10y_excl_tv"] + tv_sq_y10_pv
        )
    else:
        out["detailed_npv.annual_npv_y1"] = 0.0
        out["detailed_npv.annual_npv_y10"] = 0.0
        out["detailed_npv.npv_10y_excl_tv"] = 0.0
        out["detailed_npv.terminal_value_10y_raw"] = 0.0
        out["detailed_npv.npv_with_tv_10y_raw"] = 0.0
    out["detailed_npv.wacc"] = float(wacc)
    out["detailed_npv.perpetual_growth_rate"] = float(perp)

    return out
