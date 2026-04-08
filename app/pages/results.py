"""
Page 4 — Results

Runs the full calculation engine and displays:
  • Exec Summary — KPI cards + dual-horizon (5Y/10Y) stacked bar + line chart
  • Cash Flow — primary financial view (acquisition-based CAPEX)
  • P&L — depreciation-based P&L tables (retained for reference)
  • Fact Check — validate against saved Excel workbook
"""

import streamlit as st
import plotly.graph_objects as go
import pathlib

from engine.models import BenchmarkConfig
from engine import status_quo, retained_costs, depreciation, financial_case, outputs
from engine.fact_checker import run as fact_check_run

# --------------------------------------------------------------------------
# Colour palette (Microsoft / Azure brand)
# --------------------------------------------------------------------------
_C_SQ_LINE   = "#FF6B35"   # on-prem status quo line — coral orange
_C_AZ_LINE   = "#0078D4"   # azure total line — azure blue
_C_CAPEX     = "#4A4A6A"   # retained CAPEX bars — slate
_C_OPEX      = "#C76C00"   # retained OPEX bars — burnt orange
_C_AZURE     = "#50B0F0"   # azure consumption bars — sky blue
_C_MIGRATION = "#FFC107"   # migration one-time — amber


def _exec_chart(summary, horizon: int) -> go.Figure:
    """
    Stacked bar + line chart for the exec summary.

    Bars (Azure case breakdown per year):
      ■ Retained CAPEX    (hardware acquisition for un-migrated fleet)
      ■ Retained OPEX     (maintenance, DC, licenses, IT admin)
      ■ Azure Consumption (new cloud costs growing with migration ramp)
      ■ Migration Costs   (one-time, shown only while active)

    Lines:
      — On-Prem SQ total cost
      — Azure total cost (= bar tops)
    """
    years = list(range(1, horizon + 1))

    az_capex   = summary.az_cf_capex_by_year[1: horizon + 1]
    az_opex    = summary.az_cf_opex_by_year[1: horizon + 1]
    az_azure   = summary.az_cf_azure_by_year[1: horizon + 1]
    az_mig     = summary.az_cf_migration_by_year[1: horizon + 1]
    sq_total   = summary.sq_cf_by_year[1: horizon + 1]
    az_total   = summary.az_cf_by_year[1: horizon + 1]

    fig = go.Figure()

    # --- Stacked bars (Azure case) ---
    fig.add_trace(go.Bar(
        name="Retained CAPEX",
        x=years, y=az_capex,
        marker_color=_C_CAPEX,
        hovertemplate="Y%{x} Retained CAPEX: $%{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        name="Retained OPEX",
        x=years, y=az_opex,
        marker_color=_C_OPEX,
        hovertemplate="Y%{x} Retained OPEX: $%{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        name="Azure Consumption",
        x=years, y=az_azure,
        marker_color=_C_AZURE,
        hovertemplate="Y%{x} Azure Consumption: $%{y:,.0f}<extra></extra>",
    ))
    # Migration bars — only render years where value > 0
    mig_visible = [v if v != 0 else None for v in az_mig]
    fig.add_trace(go.Bar(
        name="Migration Costs",
        x=years, y=mig_visible,
        marker_color=_C_MIGRATION,
        hovertemplate="Y%{x} Migration: $%{y:,.0f}<extra></extra>",
    ))

    # --- Lines ---
    fig.add_trace(go.Scatter(
        name="On-Prem (Status Quo)",
        x=years, y=sq_total,
        mode="lines+markers",
        line=dict(color=_C_SQ_LINE, width=3),
        marker=dict(size=7),
        hovertemplate="Y%{x} On-Prem: $%{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        name="Azure Total",
        x=years, y=az_total,
        mode="lines+markers",
        line=dict(color=_C_AZ_LINE, width=3, dash="dot"),
        marker=dict(size=7),
        hovertemplate="Y%{x} Azure: $%{y:,.0f}<extra></extra>",
    ))

    fig.update_layout(
        barmode="stack",
        xaxis=dict(title="Year", tickmode="linear", tick0=1, dtick=1),
        yaxis=dict(title="Total Cost (USD)", tickformat="$,.0f"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=40, b=40),
    )
    return fig


def _cumulative_savings_chart(summary, horizon: int) -> go.Figure:
    """Cumulative cashflow savings with payback marker."""
    years = list(range(1, horizon + 1))
    cf_savings = summary.annual_cf_savings[1: horizon + 1]
    cumulative = []
    running = 0.0
    for s in cf_savings:
        running += s
        cumulative.append(running)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=years, y=cumulative,
        mode="lines+markers",
        name="Cumulative Cash Savings",
        line=dict(color=_C_AZ_LINE, width=2),
        fill="tozeroy",
        fillcolor="rgba(0,120,212,0.15)",
        hovertemplate="Y%{x}: $%{y:,.0f}<extra></extra>",
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="gray", annotation_text="Break-even")
    fig.update_layout(
        xaxis=dict(title="Year", tickmode="linear", tick0=1, dtick=1),
        yaxis=dict(title="Cumulative Savings (USD)", tickformat="$,.0f"),
        hovermode="x unified",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=20, b=40),
    )
    return fig


def _annual_table(sq_vals, az_vals, savings_vals, horizon: int):
    """Return a DataFrame for the annual cost table."""
    import pandas as pd
    years = list(range(1, horizon + 1))
    cumulative = []
    running = 0.0
    for s in savings_vals[1: horizon + 1]:
        running += s
        cumulative.append(running)
    return pd.DataFrame({
        "Year": years,
        "Status Quo": [f"${v:,.0f}" for v in sq_vals[1: horizon + 1]],
        "Azure Case": [f"${v:,.0f}" for v in az_vals[1: horizon + 1]],
        "Annual Savings": [f"${v:,.0f}" for v in savings_vals[1: horizon + 1]],
        "Cumulative Savings": [f"${v:,.0f}" for v in cumulative],
    })


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

    # ================================================================
    # Tabs: Exec Summary | Cash Flow | P&L | Fact Check
    # ================================================================
    tab_exec, tab_cf, tab_pl, tab_fc, tab_pres = st.tabs([
        "📊 Exec Summary", "💰 Cash Flow", "📋 P&L", "🔍 Fact Check", "📽️ Present"
    ])

    # ----------------------------------------------------------------
    # TAB 1 — EXEC SUMMARY
    # ----------------------------------------------------------------
    with tab_exec:
        # KPI cards (cashflow-primary)
        pb_str = f"{summary.payback_cf:.1f} yrs" if summary.payback_cf else "N/A"
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("CF NPV 10-Year",  f"${summary.npv_cf_10yr:,.0f}")
        k2.metric("CF NPV 5-Year",   f"${summary.npv_cf_5yr:,.0f}")
        k3.metric("P&L NPV 10-Year", f"${summary.npv_10yr:,.0f}")
        k4.metric("5-Yr CF ROI",     f"{summary.roi_cf:.0%}")
        k5.metric("Payback (5Y CF)", pb_str)
        k6.metric("Yr-10 Savings",   f"${summary.savings_yr10:,.0f}")

        st.divider()

        # Dual-horizon stacked bar + line charts
        st.subheader("Cost Comparison — On-Prem vs Azure")
        st.caption(
            "**Bars** = Azure case annual spend (retained CAPEX + retained OPEX + "
            "Azure consumption + migration). **Lines** = all-up cost for each scenario."
        )

        col5, col10 = st.columns(2)
        with col5:
            st.markdown("##### 5-Year Horizon")
            st.plotly_chart(_exec_chart(summary, 5), use_container_width=True, key="exec_cost_5yr")
        with col10:
            st.markdown("##### 10-Year Horizon")
            st.plotly_chart(_exec_chart(summary, 10), use_container_width=True, key="exec_cost_10yr")

        st.divider()

        # Cumulative savings (payback) — both horizons
        st.subheader("Cumulative Cash Savings")
        c5, c10 = st.columns(2)
        with c5:
            st.markdown("##### 5-Year")
            st.plotly_chart(_cumulative_savings_chart(summary, 5), use_container_width=True, key="exec_cumsav_5yr")
        with c10:
            st.markdown("##### 10-Year")
            st.plotly_chart(_cumulative_savings_chart(summary, 10), use_container_width=True, key="exec_cumsav_10yr")

        st.divider()

        # Waterfall (average annual savings breakdown)
        st.subheader("Transformative Value Waterfall (Average Annual, 10-Year P&L)")
        wf = summary.waterfall
        labels = list(wf.keys())
        values = list(wf.values())
        measure = ["absolute"] + ["relative"] * (len(labels) - 2) + ["total"]
        fig_wf = go.Figure(go.Waterfall(
            name="Waterfall", orientation="v",
            measure=measure,
            x=labels, y=values,
            connector={"line": {"color": "rgb(63,63,63)"}},
            increasing={"marker": {"color": _C_AZ_LINE}},
            decreasing={"marker": {"color": _C_SQ_LINE}},
            totals={"marker": {"color": "#50E6FF"}},
        ))
        fig_wf.update_layout(xaxis_title="Category", yaxis_title="USD/yr (avg)",
                             plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig_wf, use_container_width=True, key="exec_waterfall")

        # Cost per VM
        vm1, vm2, vm3 = st.columns(3)
        vm1.metric("On-Prem Cost/VM/yr", f"${summary.on_prem_cost_per_vm_yr:,.0f}")
        vm2.metric("Azure Cost/VM/yr",   f"${summary.azure_cost_per_vm_yr:,.0f}")
        vm3.metric("Savings/VM/yr",      f"${summary.savings_per_vm_yr:,.0f}")

    # ----------------------------------------------------------------
    # TAB 2 — CASH FLOW
    # ----------------------------------------------------------------
    with tab_cf:
        st.subheader("Cash Flow View")
        st.caption(
            "CAPEX = actual hardware acquisition spend (not depreciated). "
            "Retained CAPEX/OPEX declines as the migration ramp completes. "
            "Migration costs are shown separately and are one-time."
        )

        cf_horizon = st.radio("Horizon", ["5-Year", "10-Year"], horizontal=True, key="cf_horizon")
        h = 5 if cf_horizon == "5-Year" else 10

        # Summary totals
        sq_cf_total   = summary.total_sq_cf_5yr  if h == 5 else summary.total_sq_cf_10yr
        az_cf_total   = summary.total_az_cf_5yr  if h == 5 else summary.total_az_cf_10yr
        cf_npv        = summary.npv_cf_5yr        if h == 5 else summary.npv_cf_10yr
        m1, m2, m3 = st.columns(3)
        m1.metric(f"SQ {h}-Year Total",    f"${sq_cf_total:,.0f}")
        m2.metric(f"Azure {h}-Year Total", f"${az_cf_total:,.0f}")
        m3.metric(f"CF NPV ({h}-Year)",    f"${cf_npv:,.0f}")

        st.plotly_chart(_exec_chart(summary, h), use_container_width=True, key="cf_cost_chart")

        st.subheader(f"Annual Cash Flow Detail ({h}-Year)")
        st.dataframe(
            _annual_table(summary.sq_cf_by_year, summary.az_cf_by_year, summary.annual_cf_savings, h),
            use_container_width=True, hide_index=True,
        )

        st.subheader(f"Azure Case Breakdown ({h}-Year)")
        import pandas as pd
        years = list(range(1, h + 1))
        df_az = pd.DataFrame({
            "Year":               years,
            "Retained CAPEX":     [f"${v:,.0f}" for v in summary.az_cf_capex_by_year[1: h + 1]],
            "Retained OPEX":      [f"${v:,.0f}" for v in summary.az_cf_opex_by_year[1: h + 1]],
            "Azure Consumption":  [f"${v:,.0f}" for v in summary.az_cf_azure_by_year[1: h + 1]],
            "Migration Costs":    [f"${v:,.0f}" for v in summary.az_cf_migration_by_year[1: h + 1]],
            "Total Azure":        [f"${v:,.0f}" for v in summary.az_cf_by_year[1: h + 1]],
        })
        st.dataframe(df_az, use_container_width=True, hide_index=True)

    # ----------------------------------------------------------------
    # TAB 3 — P&L (depreciation-based)
    # ----------------------------------------------------------------
    with tab_pl:
        st.subheader("P&L View (Depreciation-Based)")
        st.caption(
            "Hardware costs are shown as annual depreciation (not acquisition), "
            "providing the income-statement view matching the original Excel model."
        )

        pl_horizon = st.radio("Horizon", ["5-Year", "10-Year"], horizontal=True, key="pl_horizon")
        ph = 5 if pl_horizon == "5-Year" else 10

        sq_pl_total = summary.total_sq_5yr if ph == 5 else summary.total_sq_10yr
        az_pl_total = summary.total_az_5yr if ph == 5 else summary.total_az_10yr
        pl_npv      = summary.npv_5yr       if ph == 5 else summary.npv_10yr
        p1, p2, p3 = st.columns(3)
        p1.metric(f"SQ {ph}-Year P&L",    f"${sq_pl_total:,.0f}")
        p2.metric(f"Azure {ph}-Year P&L", f"${az_pl_total:,.0f}")
        p3.metric(f"P&L NPV ({ph}-Year)", f"${pl_npv:,.0f}")

        sq_pl    = fc.sq_total()
        az_pl    = fc.az_total()
        pl_sav   = summary.annual_savings

        # P&L bar chart (grouped)
        years_pl = list(range(1, ph + 1))
        fig_pl = go.Figure()
        fig_pl.add_trace(go.Bar(
            name="Status Quo", x=years_pl, y=sq_pl[1: ph + 1], marker_color=_C_SQ_LINE,
        ))
        fig_pl.add_trace(go.Bar(
            name="Azure Case", x=years_pl, y=az_pl[1: ph + 1], marker_color=_C_AZ_LINE,
        ))
        fig_pl.add_trace(go.Scatter(
            name="Annual P&L Savings", x=years_pl, y=pl_sav[1: ph + 1],
            mode="lines+markers", line=dict(color="#50E6FF", width=2),
        ))
        fig_pl.update_layout(
            barmode="group",
            xaxis=dict(title="Year", tickmode="linear", tick0=1, dtick=1),
            yaxis=dict(title="USD", tickformat="$,.0f"),
            legend=dict(orientation="h"),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_pl, use_container_width=True, key="pl_chart")

        st.subheader(f"Annual P&L Detail ({ph}-Year)")
        st.dataframe(
            _annual_table(sq_pl, az_pl, pl_sav, ph),
            use_container_width=True, hide_index=True,
        )

    # ----------------------------------------------------------------
    # TAB 4 — FACT CHECK
    # ----------------------------------------------------------------
    with tab_fc:
        st.subheader("Fact Check — Validate Against Excel Workbook")
        st.caption(
            "Upload a saved copy of the BV Benchmark Business Case workbook (.xlsm/.xlsx) "
            "to compare every material output against the Python engine's results. "
            "**The workbook must have been saved in Excel so formula cells contain cached values.**"
        )

        uploaded_wb = st.file_uploader(
            "Upload reference workbook", type=["xlsm", "xlsx"], key="fact_check_wb"
        )

        if uploaded_wb:
            import tempfile
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

                if report.input_mismatches:
                    with st.expander(f"⚠ {len(report.input_mismatches)} input mismatch(es) detected"):
                        for m in report.input_mismatches:
                            st.warning(m)

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

    # ----------------------------------------------------------------
    # TAB 5 — PRESENTATION VIEW
    # ----------------------------------------------------------------
    with tab_pres:
        pb_str = f"{summary.payback_cf:.1f} yrs" if summary.payback_cf else "N/A"

        # Full-width title block — clean, no Step nav chrome
        st.markdown(
            f"<h1 style='text-align:center;margin-bottom:4px'>{client}</h1>"
            "<p style='text-align:center;color:gray;margin-top:0'>Azure Migration — Business Case</p>",
            unsafe_allow_html=True,
        )
        st.caption("Tip: press F11 for full-screen browser presentation mode.")
        st.divider()

        # Row 1 — headline KPIs
        p1, p2, p3, p4, p5, p6 = st.columns(6)
        p1.metric("CF NPV (10-Yr)",   f"${summary.npv_cf_10yr:,.0f}")
        p2.metric("CF NPV (5-Yr)",    f"${summary.npv_cf_5yr:,.0f}")
        p3.metric("P&L NPV (10-Yr)",  f"${summary.npv_10yr:,.0f}")
        p4.metric("5-Yr CF ROI",      f"{summary.roi_cf:.0%}")
        p5.metric("Payback (5Y CF)",  pb_str)
        p6.metric("Yr-10 Savings",    f"${summary.savings_yr10:,.0f}")

        # Row 2 — cost per VM
        v1, v2, v3, _ = st.columns([1, 1, 1, 3])
        v1.metric("On-Prem/VM/yr",  f"${summary.on_prem_cost_per_vm_yr:,.0f}")
        v2.metric("Azure/VM/yr",    f"${summary.azure_cost_per_vm_yr:,.0f}")
        v3.metric("Savings/VM/yr",  f"${summary.savings_per_vm_yr:,.0f}")

        st.divider()

        # Dual horizon cost charts
        st.subheader("Annual Cost — On-Prem vs Azure")
        st.caption(
            "Stacked bars = Azure case cost breakdown.  "
            "Lines = total cost per scenario."
        )
        ch5, ch10 = st.columns(2)
        with ch5:
            st.markdown("##### 5-Year")
            st.plotly_chart(_exec_chart(summary, 5), use_container_width=True, key="pres_cost_5yr")
        with ch10:
            st.markdown("##### 10-Year")
            st.plotly_chart(_exec_chart(summary, 10), use_container_width=True, key="pres_cost_10yr")

        st.divider()

        # Cumulative savings charts
        st.subheader("Cumulative Cash Savings")
        cs5, cs10 = st.columns(2)
        with cs5:
            st.markdown("##### 5-Year")
            st.plotly_chart(_cumulative_savings_chart(summary, 5), use_container_width=True, key="pres_cumsav_5yr")
        with cs10:
            st.markdown("##### 10-Year")
            st.plotly_chart(_cumulative_savings_chart(summary, 10), use_container_width=True, key="pres_cumsav_10yr")

        st.divider()

        # Waterfall
        st.subheader("Value Waterfall — Where the Savings Come From")
        wf = summary.waterfall
        wf_labels = list(wf.keys())
        wf_values = list(wf.values())
        wf_measure = ["absolute"] + ["relative"] * (len(wf_labels) - 2) + ["total"]
        fig_wf2 = go.Figure(go.Waterfall(
            orientation="v", measure=wf_measure,
            x=wf_labels, y=wf_values,
            connector={"line": {"color": "rgb(63,63,63)"}},
            increasing={"marker": {"color": _C_AZ_LINE}},
            decreasing={"marker": {"color": _C_SQ_LINE}},
            totals={"marker": {"color": "#50E6FF"}},
        ))
        fig_wf2.update_layout(
            xaxis_title="Category", yaxis_title="USD/yr (avg)",
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_wf2, use_container_width=True, key="pres_waterfall")

        st.divider()
        st.page_link("pages/export_page.py", label="Go to Export — download as PowerPoint or Excel →", icon="📥")
