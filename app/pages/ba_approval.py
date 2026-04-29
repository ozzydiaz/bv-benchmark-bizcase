"""Step 2.5 — BA Approval Gate (KP.BA_APPROVAL_GATE).

Renders the per-VM right-sizing decisions with cited R&D-slide formulas
and BA-judgment overlays. The BA reviews this page before approving the
transition from Layer 2 (right-sizing) to Layer 3 (financial case).

Required session state (set by the Consumption Plan page):
    st.session_state["layer2_result"] : Layer2Result
    st.session_state["layer2_run_meta"] : dict (strategy, algorithm, floors, region…)
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from training.replicas.layer2_ba_approval import (
    BRANCH_CITATIONS,
    BA_FLOOR_CITATION,
    build_ba_approval_payload,
    cite_branch,
)


def render() -> None:
    st.title("Step 2.5 · BA Approval Gate (Layer 2 → Layer 3)")
    st.caption(
        "Every right-sizing decision below is cited to a verbatim R&D slide "
        "or BA judgment overlay. Approve to proceed to the financial case."
    )

    result = st.session_state.get("layer2_result")
    run_meta = st.session_state.get("layer2_run_meta", {})
    if result is None:
        st.warning("No Layer 2 result in session. Run the Consumption Plan first.")
        return

    payload = build_ba_approval_payload(result, run_meta=run_meta)

    # ---------- Run summary ----------
    st.subheader("Run configuration")
    if run_meta:
        st.json(run_meta, expanded=False)
    else:
        st.info("No run_meta recorded. Defaults assumed.")

    # ---------- Aggregates ----------
    st.subheader("Aggregates")
    agg = payload["aggregates"]
    cols = st.columns(4)
    cols[0].metric("VMs (powered-on)", agg["vm_count"])
    cols[1].metric("Σ vCPU", f"{agg['sum_vcpu']:,}")
    cols[2].metric("Σ Memory (GiB)", f"{agg['sum_memory_gib']:,}")
    cols[3].metric("Σ Storage (GiB)", f"{agg['sum_storage_gib']:,}")

    cols = st.columns(3)
    cols[0].metric("ACR PAYG (USD/yr)", f"${agg['acr_payg_usd_yr']:,.0f}")
    cols[1].metric("ACR RI-3y (USD/yr)", f"${agg['acr_ri3y_usd_yr']:,.0f}")
    cols[2].metric("Disk PAYG (USD/yr)", f"${agg['storage_payg_usd_yr']:,.0f}")

    if agg.get("vms_with_ba_floor"):
        st.info(
            f"KP.BA_SMALL_VM_FLOOR padded {agg['vms_with_ba_floor']} undersized VMs. "
            "Click rows below where the 'BA floor' column is True to see the citation."
        )

    # ---------- Citation legend ----------
    with st.expander("Formula citation legend"):
        legend_rows = [
            {"branch_tag": tag, **cite}
            for tag, cite in BRANCH_CITATIONS.items()
        ]
        legend_rows.append({"branch_tag": "+ba_small_vm_floor (modifier)", **BA_FLOOR_CITATION})
        st.dataframe(pd.DataFrame(legend_rows), use_container_width=True)

    # ---------- Per-VM table ----------
    st.subheader("Per-VM right-sizing decisions")
    rows = []
    for r in payload["per_vm"]:
        rows.append({
            "VM": r["vm"],
            "src_vCPU": r["source"]["raw_vcpu"],
            "src_GiB": r["source"]["raw_memory_gib"],
            "BA floor": r["source"]["ba_floor_applied"],
            "rs vCPU": r["rightsized"]["rs_vcpu"],
            "rs GiB": r["rightsized"]["rs_mem_gib"],
            "rs disk GiB": r["rightsized"]["rs_disk_gib"],
            "SKU": r["sku"]["name"],
            "PAYG $/yr": r["pricing_per_offer_usd_yr"]["payg"],
            "RI3y $/yr": r["pricing_per_offer_usd_yr"]["ri3y"],
            "CPU branch": r["citations"]["cpu"]["primary"]["anchor"],
            "Memory branch": r["citations"]["memory"]["primary"]["anchor"],
            "Storage branch": r["citations"]["storage"]["primary"]["anchor"],
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, height=420)

    # ---------- Drill-down ----------
    st.subheader("Per-VM citation drill-down")
    vm_pick = st.selectbox("Select a VM", [r["vm"] for r in payload["per_vm"]])
    if vm_pick:
        sel = next(r for r in payload["per_vm"] if r["vm"] == vm_pick)
        st.json(sel)

    # ---------- Approval gate ----------
    st.divider()
    st.subheader("Approval gate")
    approver = st.text_input("Approver name", value=st.session_state.get("ba_approver", ""))
    if st.button("✅ Approve and proceed to Layer 3"):
        if not approver.strip():
            st.error("Approver name is required.")
        else:
            st.session_state["layer2_approved"] = True
            st.session_state["ba_approver"] = approver.strip()
            st.success(f"Approved by {approver}. Proceed to Step 4 · Results.")

    # ---------- Export ----------
    st.download_button(
        "Download approval payload (JSON)",
        data=json.dumps(payload, indent=2, default=str),
        file_name="ba_approval_payload.json",
        mime="application/json",
    )
