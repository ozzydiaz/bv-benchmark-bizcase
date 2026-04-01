"""
Page 4 — Results

Runs the full calculation engine and displays NPV, ROI, payback,
cash flow table, and waterfall chart.
"""

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pathlib

from engine.models import BenchmarkConfig
from engine import status_quo, retained_costs, depreciation, financial_case, outputs
from engine.fact_checker import run as fact_check_run


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

    # ----------------------------------------------------------------
    # Fact Checker — optional workbook validation
    # ----------------------------------------------------------------
    st.divider()
    st.subheader("🔍 Fact Check — Validate Against Excel Workbook")
    st.caption(
        "Upload a saved copy of the BV Benchmark Business Case workbook (.xlsm/.xlsx) "
        "to compare every material output against the Python engine's results. "
        "**The workbook must have been saved in Excel so formula cells contain cached values.**"
    )

    uploaded_wb = st.file_uploader(
        "Upload reference workbook", type=["xlsm", "xlsx"], key="fact_check_wb"
    )

    if uploaded_wb:
        import tempfile, openpyxl
        with tempfile.NamedTemporaryFile(suffix=uploaded_wb.name[-5:], delete=False) as tmp:
            tmp.write(uploaded_wb.read())
            tmp_path = pathlib.Path(tmp.name)

        with st.spinner("Running fact check..."):
            try:
                report = fact_check_run(str(tmp_path), inputs, bm)
            except Exception as exc:
                st.error(f"Fact check failed: {exc}")
                report = None

        if report:
            # Confidence gauge
            score = report.confidence_score
            color = "#00B050" if score >= 90 else ("#FFC000" if score >= 70 else "#FF0000")
            c_score, c_result = st.columns([1, 3])
            c_score.markdown(
                f"<h1 style='color:{color};text-align:center'>{score:.0f}%</h1>"
                f"<p style='text-align:center;color:gray'>Confidence Score</p>",
                unsafe_allow_html=True,
            )
            overall = "✅ PASS" if report.passed_overall else "❌ FAIL"
            with c_result:
                st.markdown(f"**Overall: {overall}**")
                r1, r2, r3, r4 = st.columns(4)
                r1.metric("PASS", report.passed)
                r2.metric("WARN", report.warned)
                r3.metric("FAIL", report.failed)
                r4.metric("SKIP", report.skipped)

            # Input mismatches
            if report.input_mismatches:
                with st.expander(f"⚠ {len(report.input_mismatches)} input mismatch(es) detected"):
                    for m in report.input_mismatches:
                        st.warning(m)

            # Check lines table
            import pandas as pd
            rows = []
            for c in report.checks:
                icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "SKIP": "–"}.get(c.status, "")
                rows.append({
                    "Status": f"{icon} {c.status}",
                    "Metric": c.name,
                    "Excel Value": f"${c.excel_value:,.0f}" if c.excel_value != 0 else "–",
                    "Engine Value": f"${c.engine_value:,.0f}",
                    "Delta %": f"{c.delta_pct:+.1f}%" if c.status != "SKIP" else "–",
                    "Note": c.note,
                })
            df_fc = pd.DataFrame(rows)

            def _color_row(row):
                s = row["Status"]
                if "FAIL" in s:
                    return ["background-color: #FFDDD9"] * len(row)
                if "WARN" in s:
                    return ["background-color: #FFF4CC"] * len(row)
                if "PASS" in s:
                    return ["background-color: #E6F4EA"] * len(row)
                return [""] * len(row)

            st.dataframe(
                df_fc.style.apply(_color_row, axis=1),
                use_container_width=True,
                hide_index=True,
            )
