"""
Page 2 — Azure Consumption Plan

Handles: 10-year migration ramp and Azure spend entry per workload.
"""

import streamlit as st
from engine.models import ConsumptionPlan, YesNo


def render():
    st.title("Step 2 · Azure Consumption Plan")

    if "inputs" not in st.session_state:
        st.warning("Complete Step 1 first.")
        return

    inputs = st.session_state["inputs"]
    updated_plans = []

    for idx, (wl, cp) in enumerate(zip(inputs.workloads, inputs.consumption_plans)):
        st.subheader(f"Workload: {wl.workload_name or f'#{idx+1}'}")

        # Migration ramp
        st.markdown("**Migration Ramp-Up (EOY cumulative % migrated)**")
        cols = st.columns(10)
        ramp = []
        for i, col in enumerate(cols):
            val = col.number_input(
                f"Y{i+1}", value=cp.migration_ramp_pct[i],
                min_value=0.0, max_value=1.0, step=0.1, format="%.1f",
                key=f"ramp_{idx}_{i}",
            )
            ramp.append(val)

        # Migration cost
        mig_cost = st.number_input(
            "Migration cost per VM (local currency)",
            value=cp.migration_cost_per_vm_lc, step=100.0,
            key=f"migcost_{idx}",
        )

        # Azure consumption anchors
        st.markdown("**Azure Annual Consumption (Y10 steady-state, local currency)**")
        c1, c2, c3 = st.columns(3)
        compute_lc = c1.number_input("Compute", value=cp.annual_compute_consumption_lc_y10, step=100_000.0, key=f"compute_{idx}")
        storage_lc = c2.number_input("Storage", value=cp.annual_storage_consumption_lc_y10, step=100_000.0, key=f"storage_{idx}")
        other_lc = c3.number_input("Other", value=cp.annual_other_consumption_lc_y10, step=100_000.0, key=f"other_{idx}")

        # Options
        st.markdown("**Options**")
        o1, o2 = st.columns(2)
        backup_on = o1.selectbox("Backup Option", ["No", "Yes"], key=f"backup_{idx}")
        dr_on = o2.selectbox("DR Option", ["No", "Yes"], key=f"dr_{idx}")

        updated_plans.append(ConsumptionPlan(
            workload_name=wl.workload_name,
            migration_cost_per_vm_lc=mig_cost,
            migration_ramp_pct=ramp,
            annual_compute_consumption_lc_y10=compute_lc,
            annual_storage_consumption_lc_y10=storage_lc,
            annual_other_consumption_lc_y10=other_lc,
            backup_activated=YesNo(backup_on),
            dr_activated=YesNo(dr_on),
        ))
        st.divider()

    if st.button("💾 Save & Continue →", type="primary"):
        inputs.consumption_plans = updated_plans
        st.session_state["inputs"] = inputs
        st.success("Consumption plan saved.")
