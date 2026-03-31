"""
Page 3 — Benchmark Overrides

Allows overriding any of the 51 benchmark parameters from their defaults.
"""

import streamlit as st
from engine.models import BenchmarkConfig


def render():
    st.title("Step 3 · Benchmark Assumptions")
    st.caption("All values are pre-loaded from survey-backed defaults. Override only where you have better client-specific data.")

    if "benchmarks" not in st.session_state:
        st.session_state["benchmarks"] = BenchmarkConfig.from_yaml()

    bm: BenchmarkConfig = st.session_state["benchmarks"]

    with st.expander("Financial", expanded=True):
        bm.wacc = st.number_input("WACC", value=bm.wacc, step=0.001, format="%.3f")
        bm.perpetual_growth_rate = st.number_input("Perpetual Growth Rate", value=bm.perpetual_growth_rate, step=0.001, format="%.3f")

    with st.expander("Servers & Storage"):
        bm.vm_to_physical_server_ratio = st.number_input("VM-to-Physical Server Ratio", value=bm.vm_to_physical_server_ratio, step=1.0)
        bm.server_cost_per_core = st.number_input("Server Cost per Core ($)", value=bm.server_cost_per_core, step=1.0)
        bm.server_cost_per_gb_memory = st.number_input("Server Cost per GB Memory ($)", value=bm.server_cost_per_gb_memory, step=0.5)
        bm.storage_cost_per_gb = st.number_input("Storage Cost per GB ($)", value=bm.storage_cost_per_gb, step=0.1)
        bm.server_hw_maintenance_pct = st.number_input("Server Maintenance (% of acq)", value=bm.server_hw_maintenance_pct, step=0.01, format="%.2f")

    with st.expander("DC / Power"):
        bm.on_prem_pue = st.number_input("On-Prem PUE", value=bm.on_prem_pue, step=0.01, format="%.2f")
        bm.space_cost_per_kw_month = st.number_input("Space Cost per kW/month ($)", value=bm.space_cost_per_kw_month, step=1.0)
        bm.power_cost_per_kw_month = st.number_input("Power Cost per kW/month ($)", value=bm.power_cost_per_kw_month, step=1.0)

    with st.expander("IT Admin"):
        bm.vms_per_sysadmin = st.number_input("VMs Managed per Sysadmin", value=bm.vms_per_sysadmin, step=10.0)
        bm.sysadmin_fully_loaded_cost_yr = st.number_input("Sysadmin Fully-Loaded Cost/yr ($)", value=bm.sysadmin_fully_loaded_cost_yr, step=1000.0)
        bm.productivity_reduction_after_migration = st.number_input("Productivity Reduction after Migration", value=bm.productivity_reduction_after_migration, step=0.01, format="%.2f")

    with st.expander("Licenses"):
        bm.virtualization_license_per_core_yr = st.number_input("Virtualization License/core/yr ($)", value=bm.virtualization_license_per_core_yr, step=1.0)
        bm.windows_server_license_per_core_yr_b = st.number_input("Windows Server License/core/yr — Level B ($)", value=bm.windows_server_license_per_core_yr_b, step=1.0)
        bm.windows_server_license_per_core_yr_d = st.number_input("Windows Server License/core/yr — Level D ($)", value=bm.windows_server_license_per_core_yr_d, step=1.0)
        bm.sql_server_license_per_core_yr_b = st.number_input("SQL Server License/core/yr — Level B ($)", value=bm.sql_server_license_per_core_yr_b, step=10.0)
        bm.sql_server_license_per_core_yr_d = st.number_input("SQL Server License/core/yr — Level D ($)", value=bm.sql_server_license_per_core_yr_d, step=10.0)

    if st.button("💾 Save Benchmarks", type="primary"):
        st.session_state["benchmarks"] = bm
        st.success("Benchmark overrides saved.")
