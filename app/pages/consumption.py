"""
Page 2 — Azure Consumption Plan

Handles: 10-year migration ramp and Azure spend entry per workload.
Auto-computed values from Step 1 (RVtools + Azure Retail Prices API) are
shown as defaults and can be overridden.
"""

import streamlit as st
from engine.models import BenchmarkConfig, ConsumptionPlan, YesNo, MIGRATION_RAMP_PRESETS
from engine import pricing_offers


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

    # ── Pricing-offer sensitivity (v1.7, FYI-only) ─────────────────────
    # FYI-ONLY interim flat-% sensitivity. Applies static benchmark
    # discount fractions (RI-1Y 20%, RI-3Y 36%, SP-1Y 18%, SP-3Y 30%) to
    # the per-plan PAYG aggregate. NOT per-VM API pricing. NOT a financial
    # input. NPV / ROI continue to use the BA-truth ACD above.
    # See docs/RFC-v1.8-per-vm-pricing.md for the per-VM-from-API plan.
    _render_pricing_offer_breakdown(updated_plans)

    if st.button("💾 Save & Continue →", type="primary"):
        inputs.consumption_plans = updated_plans
        st.session_state["inputs"] = inputs
        st.success("Consumption plan saved.")


def _render_pricing_offer_breakdown(plans: list[ConsumptionPlan]) -> None:
    """Render the FYI-only flat-%% pricing-offer sensitivity table.

    **Honest scope (v1.7):** This is a *fleet-aggregate × static-benchmark-%*
    sensitivity, NOT per-VM-from-API offer pricing. It multiplies the
    per-plan PAYG aggregate (``ConsumptionPlan.annual_compute_consumption_lc_y10``)
    by the static discount fractions on ``BenchmarkConfig`` (RI-1Y 20%,
    RI-3Y 36%, SP-1Y 18%, SP-3Y 30%). It does NOT consult the Azure Retail
    Price API for per-VM RI/SP rates and does NOT feed any financial output.
    See ``docs/RFC-v1.8-per-vm-pricing.md`` for the per-VM contract.

    A final 'BA-truth' row anchors the comparison to the discount actually
    fed into NPV (``ConsumptionPlan.azure_consumption_discount``).
    """
    if not plans:
        return

    # Use the active session benchmarks (set during L3 run) so any user
    # edits to discount rates flow through; fall back to defaults otherwise.
    bm: BenchmarkConfig = st.session_state.get("_l3_result", {}).get("bm") \
        or st.session_state.get("benchmarks") \
        or BenchmarkConfig.from_yaml()

    with st.expander(
        "💰 Pricing-offer sensitivity (FYI-only flat-% — NOT per-VM API pricing)",
        expanded=False,
    ):
        st.warning(
            "⚠️ **FYI-ONLY · interim flat-% sensitivity.** This panel applies "
            "static benchmark discounts (RI-1Y 20%, RI-3Y 36%, SP-1Y 18%, SP-3Y 30%) "
            "to the **per-plan PAYG aggregate**. It is **NOT** per-VM API-sourced "
            "pricing and **does NOT** feed NPV / ROI / ACR. True per-VM RI/SP "
            "pricing from the Azure Retail Price API is planned for v1.8 "
            "(see `docs/RFC-v1.8-per-vm-pricing.md`)."
        )
        st.caption(
            "Reading the table: each row answers _'if **all VMs** in this plan "
            "were on offer X at the benchmark discount, what would Y10 compute "
            "cost?'_. The actual answer requires per-VM API rates (v1.8). "
            "Storage and 'other' Azure services are not RI/SP-eligible and "
            "are priced at PAYG across all offers."
        )

        # Per-plan tables (one workload at a time).
        for plan in plans:
            per = pricing_offers.compute_for_plan(plan, bm)
            label = plan.workload_name or "Unnamed workload"
            st.markdown(f"**Workload: {label}**")

            if per.payg_compute_y10 <= 0:
                st.info(
                    "PAYG list-price compute is $0 for this plan — "
                    "enter an annual compute estimate above to see offers."
                )
                continue

            # Build the rows table.
            rows_for_table = []
            for r in per.rows:
                rows_for_table.append({
                    "Offer": r.offer,
                    "Discount off PAYG": f"{r.discount_pct:.1%}",
                    "Y10 compute total": f"${r.annual_total:,.0f}",
                    "Annual savings vs PAYG": f"${r.savings_vs_payg:,.0f}",
                    "% saved vs PAYG": f"{r.savings_pct_vs_payg:.1%}",
                })
            st.dataframe(rows_for_table, hide_index=True, use_container_width=True)

            # Quick summary line — what's the gap between BA-truth and the
            # cheapest standard offer?
            standard = [r for r in per.rows if r.offer != "BA-truth (current ACD)"]
            best = max(standard, key=lambda r: r.savings_vs_payg)
            ba = next(r for r in per.rows if r.offer == "BA-truth (current ACD)")
            delta = ba.annual_total - best.annual_total
            if abs(delta) > 1.0:
                if delta > 0:
                    st.info(
                        f"💡 **{best.offer}** would save an extra "
                        f"**${delta:,.0f}/yr** vs your current ACD "
                        f"({ba.discount_pct:.1%})."
                    )
                else:
                    st.success(
                        f"✅ Your current ACD ({ba.discount_pct:.1%}) is "
                        f"**${-delta:,.0f}/yr** better than the best standard "
                        f"offer ({best.offer} at {best.discount_pct:.1%})."
                    )

            # Storage / other footnote.
            if per.storage_y10 > 0 or per.other_y10 > 0:
                st.caption(
                    f"Storage Y10 ${per.storage_y10:,.0f} and Other Y10 "
                    f"${per.other_y10:,.0f} are not eligible for RI/SP and are "
                    f"priced at PAYG list across all offers."
                )

        # Optional: cross-plan fleet roll-up if multiple plans.
        if len(plans) > 1:
            st.markdown("**Fleet roll-up (all workloads combined)**")
            per_plan = [pricing_offers.compute_for_plan(p, bm) for p in plans]
            payg_total = sum(p.payg_compute_y10 for p in per_plan)
            fleet_rows = []
            for offer in ("PAYG", "RI 1Y", "RI 3Y", "SP 1Y", "SP 3Y", "BA-truth (current ACD)"):
                tot = 0.0
                for pp in per_plan:
                    for r in pp.rows:
                        if r.offer == offer:
                            tot += r.annual_total
                            break
                save = payg_total - tot
                pct = (save / payg_total) if payg_total > 0 else 0.0
                fleet_rows.append({
                    "Offer": offer,
                    "Fleet Y10 compute total": f"${tot:,.0f}",
                    "Fleet savings vs PAYG": f"${save:,.0f}",
                    "% saved vs PAYG": f"{pct:.1%}",
                })
            st.dataframe(fleet_rows, hide_index=True, use_container_width=True)
