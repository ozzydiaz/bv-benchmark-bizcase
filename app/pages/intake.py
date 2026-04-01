"""
Page 1 — Client Intake

Handles: RVtools file upload OR manual entry of workload inventory.
Populates session_state['inputs'] with a BusinessCaseInputs object and
session_state['auto_plan'] with a consumption estimate built automatically
from the RVtools file (region + utilisation data).
"""

import streamlit as st
from pathlib import Path

from engine.models import (
    BusinessCaseInputs, EngagementInfo, PricingConfig, DatacenterConfig,
    HardwareLifecycle, WorkloadInventory, ConsumptionPlan, AzureRunRate,
    YesNo, PriceLevel, DCExitType, MIGRATION_RAMP_PRESETS,
)
from engine.rvtools_parser import parse as parse_rvtools, summarize


def render():
    st.title("Step 1 · Client Intake")
    st.markdown("Enter client environment details, or upload an RVtools export to auto-populate the inventory.")

    # ----------------------------------------------------------------
    # Engagement info
    # ----------------------------------------------------------------
    st.subheader("Engagement Info")
    col1, col2, col3 = st.columns(3)
    client_name = col1.text_input("Client Name", value="Contoso")
    currency = col2.text_input("Local Currency", value="USD")
    fx_rate = col3.number_input("1 USD = x Local Currency", value=1.0, step=0.01)

    # ----------------------------------------------------------------
    # Pricing
    # ----------------------------------------------------------------
    st.subheader("Pricing Configuration")
    col4, col5 = st.columns(2)
    win_level = col4.selectbox("Windows Server Price Level", ["B", "D"], index=1)
    sql_level = col5.selectbox("SQL Server Price Level", ["B", "D"], index=1)

    # ----------------------------------------------------------------
    # Datacenter
    # ----------------------------------------------------------------
    st.subheader("Datacenter")
    col6, col7, col8 = st.columns(3)
    num_dc = col6.number_input("# Datacenters to Exit", value=0, min_value=0, step=1)
    dc_exit = col7.selectbox("DC Exit Type", ["Proportional", "Static"])
    num_ic = col8.number_input("# Interconnects to Terminate", value=0, min_value=0, step=1)

    # ----------------------------------------------------------------
    # Hardware lifecycle
    # ----------------------------------------------------------------
    st.subheader("Hardware Lifecycle")
    col9, col10, col11, col12 = st.columns(4)
    depr_life = col9.number_input("Depreciation Life (years)", value=5, min_value=1, step=1)
    actual_life = col10.number_input("Actual Usage Life (years)", value=5, min_value=1, step=1)
    growth = col11.number_input("Expected Growth Rate", value=0.10, step=0.01, format="%.2f")
    hw_renewal = col12.number_input("HW Renewal During Migration (%)", value=0.10, step=0.01, format="%.2f")

    # ----------------------------------------------------------------
    # Workload inventory — RVtools upload or manual
    # ----------------------------------------------------------------
    st.subheader("Workload Inventory")
    workload_name = st.text_input("Workload Name", value="DC Move", help="Label for this workload group (e.g. 'DC Move', 'SAP', 'Dev/Test')")
    upload_tab, manual_tab = st.tabs(["📁 Upload RVtools File", "✏️ Manual Entry"])

    inventory = None
    with upload_tab:
        uploaded = st.file_uploader("Upload RVtools .xlsx export", type=["xlsx"])
        if uploaded:
            tmp_path = Path("/tmp") / uploaded.name
            tmp_path.write_bytes(uploaded.read())
            with st.spinner("Parsing RVtools file…"):
                inventory = parse_rvtools(tmp_path)
            st.success("RVtools file parsed.")

            st.caption("**On-Prem TCO Baseline** — all VMs (incl. powered-off)")
            col_a, col_b = st.columns(2)
            col_a.metric("VMs (total)", f"{inventory.num_vms:,}")
            col_b.metric("Hosts", f"{inventory.num_hosts:,}")
            col_a.metric("Total vCPU", f"{inventory.total_vcpu:,}")
            col_b.metric("Total vMemory (GB)", f"{inventory.total_vmemory_gb:,.0f}")
            col_a.metric("Storage in use (GB)", f"{inventory.total_storage_in_use_gb:,.0f}")
            col_b.metric("vCPUs per pCore (avg)", f"{inventory.vcpu_per_core_ratio:.2f}")

            st.caption("**Azure Migration Target** — powered-on VMs only")
            col_c, col_d = st.columns(2)
            col_c.metric("VMs (powered-on)", f"{inventory.num_vms_poweredon:,}")
            col_d.metric("vCPU (powered-on)", f"{inventory.total_vcpu_poweredon:,}")
            col_c.metric("vMemory GB (powered-on)", f"{inventory.total_vmemory_gb_poweredon:,.0f}")
            col_d.metric("Storage GB (powered-on)", f"{inventory.total_storage_poweredon_gb:,.0f}")

            # Utilisation telemetry
            if inventory.cpu_util_p95 > 0 or inventory.memory_util_p95 > 0:
                st.caption("**Utilisation Telemetry** — P95 across powered-on fleet")
                col_e, col_f = st.columns(2)
                if inventory.cpu_util_p95 > 0:
                    col_e.metric(
                        "CPU P95 utilisation",
                        f"{inventory.cpu_util_p95:.1%}",
                        help=f"Derived from vCPU.Overall/Max across {inventory.cpu_util_p95_vm_count:,} powered-on VMs",
                    )
                else:
                    col_e.metric("CPU P95 utilisation", "n/a")
                if inventory.memory_util_p95 > 0:
                    col_f.metric(
                        "Memory P95 utilisation",
                        f"{inventory.memory_util_p95:.1%}",
                        help=f"Derived from vMemory.Consumed/Size MiB across {inventory.memory_util_p95_vm_count:,} powered-on VMs",
                    )
                else:
                    col_f.metric("Memory P95 utilisation", "n/a")

            # Inferred region
            with st.spinner("Inferring Azure region…"):
                from engine import region_guesser
                inferred_region = region_guesser.guess(inventory)
            region_evidence = []
            if inventory.datacenter_names:
                region_evidence.append(f"DC: {', '.join(inventory.datacenter_names)}")
            if inventory.gmt_offsets:
                region_evidence.append(f"GMT offset: {', '.join(inventory.gmt_offsets)}")
            if inventory.domain_names:
                region_evidence.append(f"domain: {', '.join(inventory.domain_names[:2])}")
            evidence_str = " · ".join(region_evidence) if region_evidence else "no metadata"
            st.info(f"Inferred Azure region: **{inferred_region}** ({evidence_str})")
            st.session_state["_inferred_region"] = inferred_region
            st.session_state["_rvtools_inv"] = inventory

            if inventory.parse_warnings:
                for w in inventory.parse_warnings:
                    st.warning(w)

    with manual_tab:
        st.caption("Enter values directly — you can always revisit to correct after uploading an RVtools file.")
        m_col1, m_col2 = st.columns(2)
        m_vms = m_col1.number_input("Number of VMs", value=0, min_value=0, step=1)
        m_vcpu = m_col2.number_input("Total Allocated vCPU", value=0, min_value=0, step=1)
        m_vmem = m_col1.number_input("Total vMemory (GB)", value=0.0, step=100.0)
        m_stor = m_col2.number_input("Total Storage in Use (GB)", value=0.0, step=1000.0)
        m_ratio = m_col1.number_input("vCPU per pCore ratio", value=1.97, step=0.01, format="%.2f")
        m_win = m_col2.number_input("pCores with Windows Server", value=0, min_value=0, step=1)
        m_win_esu = m_col1.number_input("pCores with Windows ESU (pre-2012)", value=0, min_value=0, step=1)

        with st.expander("Physical Server Detail (optional — leave at 0 if no bare-metal servers outside VM hosts)"):
            p_col1, p_col2 = st.columns(2)
            m_phys = p_col1.number_input("Physical servers (excl. VM hosts)", value=0, min_value=0, step=1)
            m_pcores = p_col2.number_input("pCores (excl. VM hosts)", value=0, min_value=0, step=1)
            m_pmem = p_col1.number_input("pMemory GB (excl. VM hosts)", value=0.0, step=100.0)

        with st.expander("SQL Server License Inventory (optional — defaults to 10% of Windows Server pCores)"):
            s_col1, s_col2 = st.columns(2)
            m_sql = s_col1.number_input("pCores with SQL Server", value=0, min_value=0, step=1,
                                        help="Leave at 0 to auto-derive as 10% of Windows Server pCores")
            m_sql_esu = s_col2.number_input("pCores with SQL ESU (pre-2012)", value=0, min_value=0, step=1,
                                             help="Leave at 0 to auto-derive as 10% of Windows ESU pCores")

        with st.expander("Backup & DR Sizing (required when backup or DR storage is included in the Azure consumption plan)"):
            st.caption(
                "Leave at 0 if backup/DR will not be activated, or if the storage is not included in the "
                "Azure consumption estimate. Values here gate the corresponding options in Step 2 — if a "
                "size is 0 the 'included in consumption' flag will be forced to No."
            )
            bd_col1, bd_col2 = st.columns(2)
            m_backup_size = bd_col1.number_input(
                "Backup Storage (GB) — D58", value=0.0, step=1000.0,
                help="Total backup storage capacity to be hosted in Azure. Align with the consumption plan estimate.",
            )
            m_backup_vms = bd_col2.number_input(
                "Backup — # protected VMs — D59", value=0, min_value=0, step=1,
                help="VMs covered by the backup solution. Leave at 0 to default to all migrated VMs.",
            )
            m_dr_size = bd_col1.number_input(
                "DR Storage (GB) — D60", value=0.0, step=1000.0,
                help="Total DR storage capacity to be hosted in Azure. Align with the consumption plan estimate.",
            )
            m_dr_vms = bd_col2.number_input(
                "DR — # protected VMs — D61", value=0, min_value=0, step=1,
                help="VMs covered by the DR solution. Leave at 0 to default to all migrated VMs.",
            )

    # ----------------------------------------------------------------
    # IT Productivity Benefit
    # ----------------------------------------------------------------
    productivity = st.selectbox("Incorporate IT Productivity Benefit?", ["Yes", "No"], index=0)

    # ----------------------------------------------------------------
    # Existing Azure Run Rate (optional — D153–D166 in 1-Client Variables)
    # ----------------------------------------------------------------
    with st.expander("Existing Azure Run Rate (optional — leave collapsed if no current Azure spend)"):
        st.caption("Include if the client already has Azure consumption to be factored into the baseline comparison.")
        rr_include = st.selectbox("Include existing run rate in business case?", ["No", "Yes"], key="rr_include")
        if rr_include == "Yes":
            rr_col1, rr_col2 = st.columns(2)
            rr_current_acd = rr_col1.number_input("Current ACD (fraction)", value=0.0, min_value=0.0, max_value=1.0, step=0.01, format="%.2f",
                                                   help="Current Azure Consumption Discount, e.g. 0.12 = 12%")
            rr_new_acd = rr_col2.number_input("New ACD (fraction)", value=0.0, min_value=0.0, max_value=1.0, step=0.01, format="%.2f",
                                               help="ACD under the proposed new agreement")
            rr_monthly = rr_col1.number_input("Monthly Spend (USD)", value=0.0, step=1000.0)
            st.markdown("**Spend mix (must sum to 1.0):**")
            mix_col1, mix_col2, mix_col3, mix_col4 = st.columns(4)
            rr_paygo = mix_col1.number_input("PayGo", value=1.0, min_value=0.0, max_value=1.0, step=0.05, format="%.2f", key="rr_paygo")
            rr_ri = mix_col2.number_input("Reserved Instances", value=0.0, min_value=0.0, max_value=1.0, step=0.05, format="%.2f", key="rr_ri")
            rr_sp = mix_col3.number_input("Savings Plan", value=0.0, min_value=0.0, max_value=1.0, step=0.05, format="%.2f", key="rr_sp")
            rr_sku = mix_col4.number_input("SKU Discount", value=0.0, min_value=0.0, max_value=1.0, step=0.05, format="%.2f", key="rr_sku")
        else:
            rr_current_acd = rr_new_acd = rr_monthly = rr_paygo = rr_ri = rr_sp = rr_sku = 0.0

    # ----------------------------------------------------------------
    # Storage mode (only relevant when RVtools vDisk tab is present)
    # ----------------------------------------------------------------
    st.subheader("Azure Storage Estimation Mode")
    stor_col1, stor_col2 = st.columns(2)
    storage_mode = stor_col1.selectbox(
        "Storage costing mode",
        ["aggregate", "per_vm"],
        index=0,
        help=(
            "**aggregate** (default): fleet total provisioned GB × blended per-GB rate. "
            "Fast and sufficient for most business cases.\n\n"
            "**per_vm**: each disk individually assigned to its Azure managed disk tier "
            "(E-series Standard SSD or P-series Premium SSD) based on provisioned size. "
            "More accurate for fleets with a mix of large and small disks. "
            "Requires the vDisk tab in the RVtools export."
        ),
    )
    disk_type = stor_col2.selectbox(
        "Managed disk family (per_vm mode only)",
        ["standard_ssd", "premium_ssd"],
        index=0,
        help="Standard SSD (E-series) for general workloads; Premium SSD (P-series) for latency-sensitive / database workloads.",
        disabled=(storage_mode == "aggregate"),
    )

    if st.button("💾 Save & Continue →", type="primary"):
        # Build WorkloadInventory from RVtools parse or manual entry
        if inventory:
            wl = WorkloadInventory(
                workload_name=workload_name,
                num_vms=inventory.num_vms,
                allocated_vcpu=inventory.total_vcpu,
                allocated_vmemory_gb=inventory.total_vmemory_gb,
                allocated_storage_gb=inventory.total_storage_in_use_gb,
                vcpu_per_core_ratio=inventory.vcpu_per_core_ratio,
                pcores_with_windows_server=inventory.pcores_with_windows_server,
                pcores_with_windows_esu=inventory.pcores_with_windows_esu,
                cpu_util_p95=inventory.cpu_util_p95,
                memory_util_p95=inventory.memory_util_p95,
                util_vm_count=inventory.cpu_util_p95_vm_count,
                inferred_azure_region=st.session_state.get("_inferred_region", ""),
                backup_size_gb=m_backup_size if m_backup_size > 0 else None,
                backup_num_protected_vms=m_backup_vms if m_backup_vms > 0 else None,
                dr_size_gb=m_dr_size if m_dr_size > 0 else None,
                dr_num_protected_vms=m_dr_vms if m_dr_vms > 0 else None,
            )
        else:
            wl = WorkloadInventory(
                workload_name=workload_name,
                num_vms=m_vms,
                allocated_vcpu=m_vcpu,
                allocated_vmemory_gb=m_vmem,
                allocated_storage_gb=m_stor,
                vcpu_per_core_ratio=m_ratio,
                pcores_with_windows_server=m_win,
                pcores_with_windows_esu=m_win_esu,
                num_physical_servers_excl_hosts=m_phys,
                allocated_pcores_excl_hosts=m_pcores,
                allocated_pmemory_gb_excl_hosts=m_pmem,
                pcores_with_sql_server=m_sql if m_sql > 0 else None,
                pcores_with_sql_esu=m_sql_esu if m_sql_esu > 0 else None,
                backup_size_gb=m_backup_size if m_backup_size > 0 else None,
                backup_num_protected_vms=m_backup_vms if m_backup_vms > 0 else None,
                dr_size_gb=m_dr_size if m_dr_size > 0 else None,
                dr_num_protected_vms=m_dr_vms if m_dr_vms > 0 else None,
            )

        # Auto-build ConsumptionPlan when RVtools data is available
        if inventory:
            try:
                from engine import azure_sku_matcher, consumption_builder
                from engine.models import BenchmarkConfig
                region = st.session_state.get("_inferred_region", "eastus")
                pricing = azure_sku_matcher.get_pricing(region)
                benchmarks = BenchmarkConfig()
                cp = consumption_builder.build(
                    inv=inventory,
                    pricing=pricing,
                    benchmarks=benchmarks,
                    workload_name=workload_name,
                    usd_to_local=fx_rate,
                    storage_mode=storage_mode,
                    disk_type=disk_type,
                )
                st.session_state["_auto_plan_source"] = pricing.source
            except Exception as exc:
                st.warning(f"Auto-compute failed ({exc!s}) — using blank consumption plan.")
                cp = ConsumptionPlan(workload_name=workload_name)
        else:
            cp = ConsumptionPlan(workload_name=workload_name)

        inputs = BusinessCaseInputs(
            engagement=EngagementInfo(
                client_name=client_name,
                local_currency_name=currency,
                usd_to_local_rate=fx_rate,
            ),
            pricing=PricingConfig(
                windows_server_price_level=PriceLevel(win_level),
                sql_server_price_level=PriceLevel(sql_level),
            ),
            datacenter=DatacenterConfig(
                num_datacenters_to_exit=num_dc,
                dc_exit_type=DCExitType(dc_exit),
                num_interconnects_to_terminate=num_ic,
            ),
            hardware=HardwareLifecycle(
                depreciation_life_years=depr_life,
                actual_usage_life_years=actual_life,
                expected_future_growth_rate=growth,
                hardware_renewal_during_migration_pct=hw_renewal,
            ),
            incorporate_productivity_benefit=YesNo(productivity),
            workloads=[wl],
            consumption_plans=[cp],
            azure_run_rate=AzureRunRate(
                include_in_business_case=YesNo(rr_include),
                current_acd=rr_current_acd,
                new_acd=rr_new_acd,
                monthly_spend_usd=rr_monthly,
                paygo_mix=rr_paygo,
                reserved_instances_mix=rr_ri,
                savings_plan_mix=rr_sp,
                sku_discount_mix=rr_sku,
            ),
        )
        st.session_state["inputs"] = inputs
        st.success("Saved! Go to Step 2 to enter the Azure consumption plan.")
