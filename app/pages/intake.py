"""
Page 1 — Client Intake

Handles: RVtools file upload OR manual entry of workload inventory.
Populates session_state['inputs'] with a BusinessCaseInputs object.
"""

import streamlit as st
from pathlib import Path

from engine.models import (
    BusinessCaseInputs, EngagementInfo, PricingConfig, DatacenterConfig,
    HardwareLifecycle, WorkloadInventory, ConsumptionPlan, YesNo, PriceLevel, DCExitType,
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
    upload_tab, manual_tab = st.tabs(["📁 Upload RVtools File", "✏️ Manual Entry"])

    inventory = None
    with upload_tab:
        uploaded = st.file_uploader("Upload RVtools .xlsx export", type=["xlsx"])
        if uploaded:
            tmp_path = Path("/tmp") / uploaded.name
            tmp_path.write_bytes(uploaded.read())
            with st.spinner("Parsing RVtools file..."):
                inventory = parse_rvtools(tmp_path)
            st.success("RVtools file parsed.")
            col_a, col_b = st.columns(2)
            col_a.metric("VMs detected", f"{inventory.num_vms:,}")
            col_b.metric("Hosts detected", f"{inventory.num_hosts:,}")
            col_a.metric("Total vCPU", f"{inventory.total_vcpu:,}")
            col_b.metric("Total vMemory (GB)", f"{inventory.total_vmemory_gb:,.0f}")
            col_a.metric("Storage in use (GB)", f"{inventory.total_storage_in_use_gb:,.0f}")
            col_b.metric("vCPUs per pCore (avg)", f"{inventory.vcpu_per_core_ratio:.2f}")
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

    # ----------------------------------------------------------------
    # Save to session state
    # ----------------------------------------------------------------
    productivity = st.selectbox("Incorporate IT Productivity Benefit?", ["Yes", "No"], index=0)

    if st.button("💾 Save & Continue →", type="primary"):
        # Build WorkloadInventory from RVtools parse or manual entry
        if inventory:
            wl = WorkloadInventory(
                workload_name="DC Move",
                num_vms=inventory.num_vms,
                allocated_vcpu=inventory.total_vcpu,
                allocated_vmemory_gb=inventory.total_vmemory_gb,
                allocated_storage_gb=inventory.total_storage_in_use_gb,
                vcpu_per_core_ratio=inventory.vcpu_per_core_ratio,
                pcores_with_windows_server=inventory.pcores_with_windows_server,
                pcores_with_windows_esu=inventory.pcores_with_windows_esu,
            )
        else:
            wl = WorkloadInventory(
                workload_name="DC Move",
                num_vms=m_vms,
                allocated_vcpu=m_vcpu,
                allocated_vmemory_gb=m_vmem,
                allocated_storage_gb=m_stor,
                vcpu_per_core_ratio=m_ratio,
                pcores_with_windows_server=m_win,
                pcores_with_windows_esu=m_win_esu,
            )

        # Default consumption plan (user fills in Step 2)
        cp = ConsumptionPlan(workload_name="DC Move")

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
        )
        st.session_state["inputs"] = inputs
        st.success("Saved! Go to Step 2 to enter the Azure consumption plan.")
