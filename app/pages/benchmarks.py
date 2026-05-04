"""
Page 3 — Benchmark Overrides

Allows overriding any of the 57+ benchmark parameters from their survey-backed defaults.
Changes are stored in session state and flow into every subsequent calculation.

Sections mirror the Excel 'Benchmark Assumptions' sheet groupings.
"""

import streamlit as st
from engine.models import BenchmarkConfig


def _pct(label: str, val: float, key: str, step: float = 0.01, help: str = "") -> float:
    """Render a percentage input (stored as decimal, displayed as %)."""
    display = st.number_input(
        label,
        value=round(val * 100, 4),
        step=round(step * 100, 4),
        format="%.2f",
        help=help + (" " if help else "") + "Enter as a percentage (e.g. 7 for 7%).",
        key=key,
    )
    return display / 100.0


def render():
    st.title("Step 3 · Benchmark Assumptions")
    st.caption(
        "All values are pre-loaded from survey-backed defaults. "
        "Override only where you have better client-specific data. "
        "Changes here apply immediately to the Step 4 results."
    )

    if "benchmarks" not in st.session_state:
        st.session_state["benchmarks"] = BenchmarkConfig.from_yaml()

    bm: BenchmarkConfig = st.session_state["benchmarks"]

    # ----------------------------------------------------------------
    # Financial
    # ----------------------------------------------------------------
    with st.expander("💰 Financial", expanded=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            bm.wacc = _pct("WACC (cost of capital)", bm.wacc, "bm_wacc",
                           help="Discount rate for NPV. Default 7%.")
        with c2:
            bm.perpetual_growth_rate = _pct(
                "Perpetual Growth Rate (Gordon TV)",
                bm.perpetual_growth_rate,
                "bm_pgr",
                help=(
                    "Terminal value in the 10-year P&L NPV uses Gordon Growth: "
                    "TV = savings[10] × (1 + g) / (WACC − g), discounted to PV. "
                    "Default 3%. WACC must be > g or TV is forced to 0."
                ),
            )
        with c3:
            bm.nii_interest_rate = _pct("NII Interest Rate", bm.nii_interest_rate, "bm_nii",
                                         help="Short-term interest rate earned on retained cash. Default 3%.")

    # ----------------------------------------------------------------
    # Hardware Lifecycle
    # ----------------------------------------------------------------
    with st.expander("🔄 Hardware Lifecycle & Sizing"):
        c1, c2, c3 = st.columns(3)
        with c1:
            bm.vm_to_physical_server_ratio = st.number_input(
                "VM-to-Physical Server Ratio", value=bm.vm_to_physical_server_ratio, step=1.0, key="bm_vm2svr",
                help="Used to estimate pCore count from VM count. Default 12.")
            bm.vcpu_to_pcores_ratio = st.number_input(
                "vCPU-to-pCore Ratio", value=bm.vcpu_to_pcores_ratio, step=0.5, key="bm_v2p",
                help="Benchmark vCPU overcommit ratio per physical core. Default 7.")
        with c2:
            bm.vmem_to_pmem_ratio = st.number_input(
                "vMemory-to-pMemory Ratio", value=bm.vmem_to_pmem_ratio, step=0.1, key="bm_vm2pm",
                help="Provisioned vRAM vs installed physical RAM. Default 1.0.")
            bm.storage_gb_included_in_server = st.number_input(
                "Storage GB included in Server Cost", value=bm.storage_gb_included_in_server, step=10.0, key="bm_stor_incl",
                help="GB of storage bundled into server unit cost (avoids double counting). Default 0.")
        with c3:
            bm.thermal_design_power_watt_yr_per_core = st.number_input(
                "TDP Watts per Core", value=bm.thermal_design_power_watt_yr_per_core, step=0.5, key="bm_tdp",
                help="Thermal design power per physical core. Default 10.056 W/core.")
            bm.storage_power_kwh_yr_per_tb = st.number_input(
                "Storage Power kWh/yr per TB", value=bm.storage_power_kwh_yr_per_tb, step=1.0, key="bm_stor_pwr",
                help="Annual power consumption per TB of storage. Default 10 kWh/yr/TB.")

    # ----------------------------------------------------------------
    # Server & Storage Costs
    # ----------------------------------------------------------------
    with st.expander("🖥️ Server & Storage Costs"):
        c1, c2, c3 = st.columns(3)
        with c1:
            bm.server_cost_per_core = st.number_input(
                "Server Cost per Core ($)", value=bm.server_cost_per_core, step=5.0, key="bm_svc",
                help="Blended acquisition cost per physical core. Default $147.")
            bm.server_cost_per_gb_memory = st.number_input(
                "Server Cost per GB Memory ($)", value=bm.server_cost_per_gb_memory, step=0.5, key="bm_mem_cost",
                help="Blended memory acquisition cost per GB. Default $16.50.")
        with c2:
            bm.storage_cost_per_gb = st.number_input(
                "Storage Cost per GB ($)", value=bm.storage_cost_per_gb, step=0.1, key="bm_stor_cost",
                help="Blended on-prem storage acquisition cost per GB. Default $2.20.")
            bm.backup_storage_cost_per_gb_yr = st.number_input(
                "Backup Storage Cost per GB/yr ($)", value=bm.backup_storage_cost_per_gb_yr, step=0.01, key="bm_bk_stor",
                help="Annual cost per GB of backup storage. Default $0.15.")
        with c3:
            bm.dr_storage_cost_per_gb_yr = st.number_input(
                "DR Storage Cost per GB/yr ($)", value=bm.dr_storage_cost_per_gb_yr, step=0.01, key="bm_dr_stor",
                help="Annual cost per GB of DR storage. Default $0.15.")
            bm.server_hw_maintenance_pct = _pct(
                "Server Maintenance (% of acq)", bm.server_hw_maintenance_pct, "bm_srv_maint",
                help="Annual maintenance as % of acquisition cost. Default 5%.")

        c1, c2 = st.columns(2)
        with c1:
            bm.storage_hw_maintenance_pct = _pct(
                "Storage Maintenance (% of acq)", bm.storage_hw_maintenance_pct, "bm_stor_maint",
                help="Annual storage maintenance as % of acq cost. Default 10%.")
        with c2:
            bm.network_hw_maintenance_pct = _pct(
                "Network Maintenance (% of acq)", bm.network_hw_maintenance_pct, "bm_nw_maint",
                help="Annual network hardware maintenance as % of acq cost. Default 10%.")

    # ----------------------------------------------------------------
    # Datacenter / Power
    # ----------------------------------------------------------------
    with st.expander("🏢 Datacenter & Power"):
        c1, c2, c3 = st.columns(3)
        with c1:
            bm.on_prem_pue = st.number_input(
                "On-Prem PUE", value=bm.on_prem_pue, step=0.01, format="%.2f", key="bm_pue",
                help="Power Usage Effectiveness. 1.0 = perfect. Default 1.56.")
            bm.on_prem_load_factor = _pct(
                "On-Prem Load Factor", bm.on_prem_load_factor, "bm_load",
                help="Average server utilisation as a fraction of TDP. Default 30%.")
        with c2:
            bm.space_cost_per_kw_month = st.number_input(
                "DC Space Cost per kW/month ($)", value=bm.space_cost_per_kw_month, step=5.0, key="bm_dc_space",
                help="Colocated or owned DC space rate per kW/month. Default $338.44.")
            bm.power_cost_per_kw_month = st.number_input(
                "DC Power Cost per kW/month ($)", value=bm.power_cost_per_kw_month, step=1.0, key="bm_dc_pwr",
                help="Electricity cost per kW/month. Default $52.28.")
        with c3:
            bm.unused_power_overhead_pct = _pct(
                "Unused Power Overhead", bm.unused_power_overhead_pct, "bm_pwr_oh",
                help="% of power capacity allocated but unused. Default 25%.")
            bm.interconnect_cost_per_yr = st.number_input(
                "Interconnect Cost per yr ($)", value=bm.interconnect_cost_per_yr, step=5000.0, key="bm_interconnect",
                help="Annual WAN/interconnect cost per circuit. Default $100,000.")

    # ----------------------------------------------------------------
    # Network Hardware
    # ----------------------------------------------------------------
    with st.expander("🔌 Network Hardware (per DC)"):
        c1, c2, c3 = st.columns(3)
        with c1:
            bm.servers_per_cabinet = st.number_input("Servers per Cabinet", value=bm.servers_per_cabinet, step=1.0, key="bm_spc")
            bm.core_routers_per_dc = st.number_input("Core Routers per DC", value=bm.core_routers_per_dc, step=1.0, key="bm_core_r")
            bm.aggregate_routers_per_core = st.number_input("Aggregate Routers per Core Router", value=bm.aggregate_routers_per_core, step=1.0, key="bm_agg_r")
        with c2:
            bm.access_switches_per_core = st.number_input("Access Switches per Core Router", value=bm.access_switches_per_core, step=1.0, key="bm_acc_sw")
            bm.load_balancers_per_core = st.number_input("Load Balancers per Core Router", value=bm.load_balancers_per_core, step=1.0, key="bm_lb")
        with c3:
            bm.cabinet_cost = st.number_input("Cabinet Cost ($)", value=bm.cabinet_cost, step=50.0, key="bm_cab")
            bm.core_router_cost = st.number_input("Core Router Cost ($)", value=bm.core_router_cost, step=1000.0, key="bm_core_r_cost")
            bm.aggregate_router_cost = st.number_input("Aggregate Router Cost ($)", value=bm.aggregate_router_cost, step=500.0, key="bm_agg_r_cost")
            bm.access_switch_cost = st.number_input("Access Switch Cost ($)", value=bm.access_switch_cost, step=100.0, key="bm_acc_sw_cost")
            bm.load_balancer_cost = st.number_input("Load Balancer Cost ($)", value=bm.load_balancer_cost, step=1000.0, key="bm_lb_cost")

    # ----------------------------------------------------------------
    # Licenses
    # ----------------------------------------------------------------
    with st.expander("📋 License Costs (per core/yr)"):
        st.caption("Level B = list price. Level D = EA/MCA discounted (~15% below B). The price level applied is set in Step 1.")
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**Virtualization**")
            bm.virtualization_license_per_core_yr = st.number_input(
                "Virtualization ($/core/yr)", value=bm.virtualization_license_per_core_yr, step=1.0, key="bm_virt_lic")
        with c2:
            st.markdown("**Windows Server**")
            bm.windows_server_license_per_core_yr_b = st.number_input(
                "Windows Server Level B ($/core/yr)", value=bm.windows_server_license_per_core_yr_b, step=1.0, key="bm_ws_b")
            bm.windows_server_license_per_core_yr_d = st.number_input(
                "Windows Server Level D ($/core/yr)", value=bm.windows_server_license_per_core_yr_d, step=1.0, key="bm_ws_d")
            bm.windows_esu_per_core_yr_b = st.number_input(
                "Windows ESU Level B ($/core/yr)", value=bm.windows_esu_per_core_yr_b, step=5.0, key="bm_wesu_b")
            bm.windows_esu_per_core_yr_d = st.number_input(
                "Windows ESU Level D ($/core/yr)", value=bm.windows_esu_per_core_yr_d, step=5.0, key="bm_wesu_d")
        with c3:
            st.markdown("**SQL Server**")
            bm.sql_server_license_per_core_yr_b = st.number_input(
                "SQL Server Level B ($/core/yr)", value=bm.sql_server_license_per_core_yr_b, step=10.0, key="bm_sql_b")
            bm.sql_server_license_per_core_yr_d = st.number_input(
                "SQL Server Level D ($/core/yr)", value=bm.sql_server_license_per_core_yr_d, step=10.0, key="bm_sql_d")
            bm.sql_esu_per_core_yr_b = st.number_input(
                "SQL ESU Level B ($/core/yr)", value=bm.sql_esu_per_core_yr_b, step=50.0, key="bm_sesu_b")
            bm.sql_esu_per_core_yr_d = st.number_input(
                "SQL ESU Level D ($/core/yr)", value=bm.sql_esu_per_core_yr_d, step=50.0, key="bm_sesu_d")

        st.markdown("**Backup & DR Software**")
        c1, c2 = st.columns(2)
        with c1:
            bm.backup_software_per_vm_yr = st.number_input(
                "Backup Software per VM/yr ($)", value=bm.backup_software_per_vm_yr, step=5.0, key="bm_bk_sw")
        with c2:
            bm.dr_software_per_vm_yr = st.number_input(
                "DR Software per VM/yr ($)", value=bm.dr_software_per_vm_yr, step=5.0, key="bm_dr_sw")

    # ----------------------------------------------------------------
    # IT Admin & Productivity
    # ----------------------------------------------------------------
    with st.expander("👥 IT Admin & Productivity"):
        c1, c2, c3 = st.columns(3)
        with c1:
            bm.vms_per_sysadmin = st.number_input(
                "VMs per Sysadmin", value=bm.vms_per_sysadmin, step=50.0, key="bm_vpa",
                help="Benchmark span of control. Default 1,200.")
            bm.sysadmin_fully_loaded_cost_yr = st.number_input(
                "Sysadmin Fully-Loaded Cost/yr ($)", value=bm.sysadmin_fully_loaded_cost_yr, step=1000.0, key="bm_sal",
                help="Including employer taxes, benefits, tools. Default $196,587.")
        with c2:
            bm.sysadmin_working_hours_yr = st.number_input(
                "Sysadmin Working Hours/yr", value=bm.sysadmin_working_hours_yr, step=40.0, key="bm_hrs",
                help="Productive hours per year. Default 2,040.")
            bm.sysadmin_contractor_pct = _pct(
                "Contractor Mix", bm.sysadmin_contractor_pct, "bm_ctr",
                help="% of IT headcount that are contractors (excluded from productivity savings). Default 32%.")
        with c3:
            bm.productivity_reduction_after_migration = _pct(
                "Productivity Reduction after Migration", bm.productivity_reduction_after_migration, "bm_prod_red",
                help="% of IT time freed up post-migration (cloud ops vs data centre ops). Default 42%.")
            bm.productivity_recapture_rate = _pct(
                "Productivity Recapture Rate", bm.productivity_recapture_rate, "bm_prod_cap",
                help="% of freed-up time actually redeployed or realised as savings. Default 95%.")

    # ----------------------------------------------------------------
    # Azure Pricing Fallbacks
    # ----------------------------------------------------------------
    with st.expander("☁️ Azure Pricing Fallbacks"):
        st.caption(
            "Used when the Azure Retail Prices API is unavailable or the region SKU is not found. "
            "Live-fetched values from the API override these."
        )
        c1, c2 = st.columns(2)
        with c1:
            bm.payg_cost_per_vcpu_hour = st.number_input(
                "PAYG Cost per vCPU/hr ($)", value=bm.payg_cost_per_vcpu_hour,
                step=0.001, format="%.4f", key="bm_payg_cpu",
                help="Benchmark rate (D4s v5 East US ÷ 4 vCPU = $0.048). Override for non-standard regions.")
        with c2:
            bm.payg_cost_per_gb_month = st.number_input(
                "PAYG Cost per GB/month ($)", value=bm.payg_cost_per_gb_month,
                step=0.001, format="%.4f", key="bm_payg_stor",
                help="Standard SSD E10 LRS blended rate. Default $0.018.")

    # ----------------------------------------------------------------
    # Right-sizing Parameters
    # ----------------------------------------------------------------
    with st.expander("📐 Right-Sizing Parameters"):
        st.caption(
            "Used by the auto-derivation pipeline when building the Consumption Plan from RVtools data. "
            "Telemetry-based sizing (vCPU / vMemory tabs) takes priority; fallback factors apply only "
            "when utilisation data is absent for a given VM."
        )
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**When telemetry is available**")
            bm.utilization_percentile = st.number_input(
                "Utilisation Percentile (P-value)", value=float(bm.utilization_percentile),
                step=1.0, min_value=50.0, max_value=99.0, key="bm_pval",
                help="P-value used for per-VM CPU and memory right-sizing. Default P95.")
            bm.utilization_percentile = int(bm.utilization_percentile)
            bm.cpu_rightsizing_headroom_factor = _pct(
                "CPU Headroom above utilisation", bm.cpu_rightsizing_headroom_factor, "bm_cpu_hd",
                help="Buffer added on top of the utilised fraction after rightsizing. Default 20%.")
            bm.memory_rightsizing_headroom_factor = _pct(
                "Memory Headroom above utilisation", bm.memory_rightsizing_headroom_factor, "bm_mem_hd",
                help="Buffer added on top of the utilised fraction after rightsizing. Default 20%.")
        with c2:
            st.markdown("**When telemetry is absent (fallback assumptions)**")
            st.caption(
                "Applied per VM when vCPU / vMemory / vHost tabs provide no utilisation signal. "
                "Override when you have knowledge of how the environment is run."
            )
            bm.cpu_util_fallback_factor = _pct(
                "CPU retain factor (no telemetry)", bm.cpu_util_fallback_factor, "bm_cpu_fb",
                help="Fraction of allocated vCPU to target in Azure when no utilisation data. "
                     "Default 40% (≡ 60% reduction). E.g. 0.40 → a 8-vCPU VM targets 4 vCPU before headroom.")
            bm.mem_util_fallback_factor = _pct(
                "Memory retain factor (no telemetry)", bm.mem_util_fallback_factor, "bm_mem_fb",
                help="Fraction of allocated memory to target in Azure when no utilisation data. "
                     "Default 60% (≡ 40% reduction). E.g. 0.60 → a 16 GiB VM targets 10 GiB before headroom.")
            bm.storage_prov_reduction_factor = _pct(
                "Storage reduction vs Provisioned (last resort)", bm.storage_prov_reduction_factor, "bm_stor_fb",
                help="Applied to vInfo Provisioned MiB when vDisk, vPartition, and In Use are all absent. "
                     "Default 20% reduction (retain 80% of provisioned). "
                     "Priority: vDisk Capacity → vPartition Consumed → vInfo In Use → Provisioned × (1−this).")

    # ----------------------------------------------------------------
    # Save / Reset
    # ----------------------------------------------------------------
    st.divider()
    col_save, col_reset, _ = st.columns([1, 1, 4])
    with col_save:
        if st.button("💾 Save Overrides", type="primary"):
            st.session_state["benchmarks"] = bm
            st.success("Benchmark overrides saved — results will recalculate on Step 4.")
    with col_reset:
        if st.button("↺ Reset to Defaults"):
            st.session_state["benchmarks"] = BenchmarkConfig.from_yaml()
            st.rerun()
