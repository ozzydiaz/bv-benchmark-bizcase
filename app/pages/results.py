"""
Page 4 — Results

Runs the full calculation engine and displays NPV, ROI, payback,
cash flow table, and waterfall chart.
"""

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px

from engine.models import BenchmarkConfig
from engine import status_quo, retained_costs, depreciation, financial_case, outputs


def render():
    st.title("Step 4 · Results")

    if "inputs" not in st.session_state:
        st.warning("Complete Steps 1 and 2 first.")
        return

    inputs = st.session_state["inputs"]
    bm: BenchmarkConfig = st.session_state.get("benchmarks", BenchmarkConfig.from_yaml())

    with st.spinner("Running business case engine..."):
        sq = status_quo.compute(inputs, bm)
        depr = depreciation.compute(inputs, bm)
        ret = retained_costs.compute(inputs, bm, sq)
        fc = financial_case.compute(inputs, bm, sq, ret, depr)
        summary = outputs.compute(inputs, bm, fc)

    client = inputs.engagement.client_name
    st.header(f"Business Case — {client}")

    # ----------------------------------------------------------------
    # KPI cards
    # ----------------------------------------------------------------
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("10-Year NPV", f"${summary.npv_10yr:,.0f}")
    k2.metric("5-Year NPV", f"${summary.npv_5yr:,.0f}")
    k3.metric("10-Year ROI", f"{summary.roi_10yr:.0%}")
    pb = f"{summary.payback_years:.1f} yrs" if summary.payback_years else "N/A"
    k4.metric("Payback", pb)
    k5.metric("Yr-10 Annual Savings", f"${summary.savings_yr10:,.0f}")

    st.divider()

    # ----------------------------------------------------------------
    # Cash flow chart
    # ----------------------------------------------------------------
    st.subheader("10-Year Cash Flow: Status Quo vs Azure")

    years = list(range(1, 11))
    sq_totals = fc.sq_total()[1:]
    az_totals = fc.az_total()[1:]
    savings = summary.annual_savings[1:]

    fig_cf = go.Figure()
    fig_cf.add_trace(go.Bar(name="Status Quo", x=years, y=sq_totals, marker_color="#FF6B35"))
    fig_cf.add_trace(go.Bar(name="Azure Case", x=years, y=az_totals, marker_color="#0078D4"))
    fig_cf.add_trace(go.Scatter(name="Annual Savings", x=years, y=savings,
                                mode="lines+markers", line=dict(color="#50E6FF", width=2)))
    fig_cf.update_layout(barmode="group", xaxis_title="Year", yaxis_title="USD",
                         legend=dict(orientation="h"))
    st.plotly_chart(fig_cf, use_container_width=True)

    # ----------------------------------------------------------------
    # Cumulative savings / payback
    # ----------------------------------------------------------------
    cumulative = []
    running = 0.0
    for s in savings:
        running += s
        cumulative.append(running)

    fig_pb = go.Figure()
    fig_pb.add_trace(go.Scatter(x=years, y=cumulative, mode="lines+markers",
                                name="Cumulative Savings", line=dict(color="#50E6FF", width=2)))
    fig_pb.add_hline(y=0, line_dash="dash", line_color="gray")
    fig_pb.update_layout(title="Cumulative Savings (Payback Curve)",
                         xaxis_title="Year", yaxis_title="USD (cumulative)")
    st.plotly_chart(fig_pb, use_container_width=True)

    # ----------------------------------------------------------------
    # Waterfall chart
    # ----------------------------------------------------------------
    st.subheader("Transformative Value Waterfall (Average Annual)")
    wf = summary.waterfall
    labels = list(wf.keys())
    values = list(wf.values())
    measure = ["absolute"] + ["relative"] * (len(labels) - 2) + ["total"]
    fig_wf = go.Figure(go.Waterfall(
        name="Waterfall", orientation="v",
        measure=measure,
        x=labels, y=values,
        connector={"line": {"color": "rgb(63, 63, 63)"}},
        increasing={"marker": {"color": "#0078D4"}},
        decreasing={"marker": {"color": "#FF6B35"}},
        totals={"marker": {"color": "#50E6FF"}},
    ))
    fig_wf.update_layout(xaxis_title="Category", yaxis_title="USD/yr (avg)")
    st.plotly_chart(fig_wf, use_container_width=True)

    # ----------------------------------------------------------------
    # Detailed table
    # ----------------------------------------------------------------
    st.subheader("Annual Detail")
    import pandas as pd
    df = pd.DataFrame({
        "Year": years,
        "Status Quo ($)": [f"${v:,.0f}" for v in sq_totals],
        "Azure Case ($)": [f"${v:,.0f}" for v in az_totals],
        "Annual Savings ($)": [f"${v:,.0f}" for v in savings],
        "Cumulative Savings ($)": [f"${v:,.0f}" for v in cumulative],
    })
    st.dataframe(df, use_container_width=True, hide_index=True)

    # ----------------------------------------------------------------
    # Cost per VM
    # ----------------------------------------------------------------
    st.subheader("Cost per VM (Year 10 Run-Rate)")
    vm1, vm2, vm3 = st.columns(3)
    vm1.metric("On-Prem Cost/VM/yr", f"${summary.on_prem_cost_per_vm_yr:,.0f}")
    vm2.metric("Azure Cost/VM/yr", f"${summary.azure_cost_per_vm_yr:,.0f}")
    vm3.metric("Savings/VM/yr", f"${summary.savings_per_vm_yr:,.0f}")
