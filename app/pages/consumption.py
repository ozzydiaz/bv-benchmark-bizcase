"""
Page 2 — Azure Consumption Plan

Handles: 10-year migration ramp and Azure spend entry per workload.
Auto-computed values from Step 1 (RVtools + Azure Retail Prices API) are
shown as defaults and can be overridden.
"""

import streamlit as st
from engine.models import ConsumptionPlan, YesNo, MIGRATION_RAMP_PRESETS


def render():
    st.title("Step 2 · Azure Consumption Plan")

    if "inputs" not in st.session_state:
        st.warning("Complete Step 1 first.")
        return

    inputs = st.session_state["inputs"]
    updated_plans = []

    for idx, (wl, cp) in enumerate(zip(inputs.workloads, inputs.consumption_plans)):
        st.subheader(f"Workload: {wl.workload_name or f'#{idx+1}'}")

        # ── Auto-compute callout (shown when RVtools was uploaded) ────────
        auto_src = st.session_state.get("_auto_plan_source")
        if auto_src and idx == 0:
            src_label = {"api": "Azure Retail Prices API (live)", "cache": "Azure Retail Prices (cached)", "benchmark": "benchmark defaults"}.get(auto_src, auto_src)
            region_label = wl.inferred_azure_region or "—"
            st.info(
                f"Values below were auto-computed from your RVtools file "
                f"using pricing for **{region_label}** ({src_label}). "
                f"Review and override if needed."
            )

        # Azure workload profile (right-sized target — D8/D9/D10 in 2a sheet)
        st.markdown("**Azure Right-Sized Profile** *(derived from RVtools utilisation telemetry)*")
        az_col1, az_col2, az_col3 = st.columns(3)
        az_vcpu = az_col1.number_input("Azure vCPU", value=cp.azure_vcpu, min_value=0, step=100, key=f"az_vcpu_{idx}")
        az_mem = az_col2.number_input("Azure Memory (GB)", value=cp.azure_memory_gb, min_value=0.0, step=1000.0, key=f"az_mem_{idx}")
        az_stor = az_col3.number_input("Azure Storage (GB)", value=cp.azure_storage_gb, min_value=0.0, step=10000.0, key=f"az_stor_{idx}")

        # Migration ramp
        st.markdown("**Migration Ramp-Up (EOY cumulative % migrated)**")
        preset_names = list(MIGRATION_RAMP_PRESETS.keys())
        # Detect which preset matches the current ramp (if any)
        default_preset = "Custom"
        for name, vals in MIGRATION_RAMP_PRESETS.items():
            if vals is not None and vals == cp.migration_ramp_pct:
                default_preset = name
                break
        selected_preset = st.selectbox(
            "Ramp preset",
            preset_names,
            index=preset_names.index(default_preset),
            key=f"ramp_preset_{idx}",
            help=(
                "Express: 100% migrated by end of Y1 (50% avg Azure spend in Y1). "
                "Standard: 100% by Y2. Extended: 100% by Y3 (Template default). "
                "Custom: set each year manually below."
            ),
        )
        # Apply preset to ramp values (Custom leaves current values)
        preset_vals = MIGRATION_RAMP_PRESETS[selected_preset]
        if preset_vals is not None:
            ramp_defaults = list(preset_vals)
        else:
            ramp_defaults = list(cp.migration_ramp_pct)
        cols = st.columns(10)
        ramp = []
        for i, col in enumerate(cols):
            val = col.number_input(
                f"Y{i+1}", value=ramp_defaults[i],
                min_value=0.0, max_value=1.0, step=0.1, format="%.1f",
                key=f"ramp_{idx}_{i}_{selected_preset}",
                disabled=(selected_preset != "Custom"),
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

        # Azure Consumption Discount
        acd = st.number_input(
            "Azure Consumption Discount — ACD (fraction, 0 = PAYG list price)",
            value=cp.azure_consumption_discount, min_value=0.0, max_value=1.0, step=0.01, format="%.2f",
            help="E.g. 0.12 = 12% discount off PAYG via EA/CSP/MCA. Leave at 0 for pay-as-you-go list rates.",
            key=f"acd_{idx}",
        )

        # Microsoft Funding (ACO / ECIF) — collapsible
        with st.expander("Microsoft Funding — ACO & ECIF (optional)"):
            st.caption("Enter any Azure Consumption Obligation (ACO) or Eligible Credit Investment Fund (ECIF) amounts per year. Positive = inflow (reduces net Azure cost).")
            fund_cols = st.columns(10)
            aco = []
            ecif = []
            for i, col in enumerate(fund_cols):
                aco.append(col.number_input(f"ACO Y{i+1}", value=cp.aco_by_year[i], step=10_000.0, key=f"aco_{idx}_{i}"))
                ecif.append(col.number_input(f"ECIF Y{i+1}", value=cp.ecif_by_year[i], step=10_000.0, key=f"ecif_{idx}_{i}"))

        # Options — auto-derive from backup/DR sizes entered in Step 1 (D58–D61).
        # If the user entered a non-zero size, backup/DR is activated and storage
        # is automatically included in consumption — no selector needed.
        # The only remaining user choice is whether the *software* license cost
        # is also included in the Azure consumption estimate.
        backup_size = wl.backup_size_gb or 0
        dr_size     = wl.dr_size_gb or 0

        if backup_size > 0:
            backup_on = "Yes"
            backup_stor_in = "Yes"
            st.markdown("**Backup** — activated automatically (backup storage size entered in Step 1)")
            backup_sw_in = st.selectbox(
                "Backup software cost included in Azure consumption?",
                ["No", "Yes"], key=f"backup_sw_{idx}",
                help="Yes if the backup software licence/SaaS fee is billed through Azure consumption.",
            )
        else:
            st.markdown("**Options**")
            o1, o2 = st.columns(2)
            backup_on = o1.selectbox("Backup Option", ["No", "Yes"], key=f"backup_{idx}")
            backup_sw_in = backup_stor_in = "No"
            if backup_on == "Yes":
                st.caption(
                    "No backup storage size was entered in Step 1 — storage cost will be $0. "
                    "Return to Step 1 and enter a Backup Storage size (D58) to include it."
                )
                backup_sw_in = o2.selectbox(
                    "Backup software included in Azure consumption?", ["No", "Yes"],
                    key=f"backup_sw_{idx}",
                )

        if dr_size > 0:
            dr_on = "Yes"
            dr_stor_in = "Yes"
            st.markdown("**DR** — activated automatically (DR storage size entered in Step 1)")
            dr_sw_in = st.selectbox(
                "DR software cost included in Azure consumption?",
                ["No", "Yes"], key=f"dr_sw_{idx}",
                help="Yes if the DR software licence/SaaS fee is billed through Azure consumption.",
            )
        else:
            d1, d2 = st.columns(2)
            dr_on = d1.selectbox("DR Option", ["No", "Yes"], key=f"dr_{idx}")
            dr_sw_in = dr_stor_in = "No"
            if dr_on == "Yes":
                st.caption(
                    "No DR storage size was entered in Step 1 — storage cost will be $0. "
                    "Return to Step 1 and enter a DR Storage size (D60) to include it."
                )
                dr_sw_in = d2.selectbox(
                    "DR software included in Azure consumption?", ["No", "Yes"],
                    key=f"dr_sw_{idx}",
                )

        updated_plans.append(ConsumptionPlan(
            workload_name=wl.workload_name,
            azure_vcpu=az_vcpu,
            azure_memory_gb=az_mem,
            azure_storage_gb=az_stor,
            migration_cost_per_vm_lc=mig_cost,
            migration_ramp_pct=ramp,
            annual_compute_consumption_lc_y10=compute_lc,
            annual_storage_consumption_lc_y10=storage_lc,
            annual_other_consumption_lc_y10=other_lc,
            azure_consumption_discount=acd,
            aco_by_year=aco,
            ecif_by_year=ecif,
            backup_activated=YesNo(backup_on),
            backup_software_in_consumption=YesNo(backup_sw_in),
            backup_storage_in_consumption=YesNo(backup_stor_in),
            dr_activated=YesNo(dr_on),
            dr_software_in_consumption=YesNo(dr_sw_in),
            dr_storage_in_consumption=YesNo(dr_stor_in),
        ))
        st.divider()

    if st.button("💾 Save & Continue →", type="primary"):
        inputs.consumption_plans = updated_plans
        st.session_state["inputs"] = inputs
        st.success("Consumption plan saved.")
