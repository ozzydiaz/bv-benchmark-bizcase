"""
Page 5 — Export & Presentation

Three modes accessible via tabs:
  📥 Downloads   — PowerPoint deck (.pptx) + pre-filled Excel workbook (.xlsx)
  🖥️ Slides      — Full-screen presentation view (exec summary chart + KPIs)
  📋 Detail      — Full annual cashflow table for copy/paste into other tools
"""
from __future__ import annotations

import io
import streamlit as st
import plotly.graph_objects as go

from engine.models import BenchmarkConfig
from engine import status_quo, retained_costs, depreciation, financial_case, outputs
from engine.export import build_pptx, build_excel

_C_SQ        = "#FF6B35"
_C_AZ        = "#0078D4"
_C_CAPEX     = "#4A4A6A"
_C_OPEX      = "#C76C00"
_C_AZURE_BAR = "#50B0F0"
_C_MIGRATION = "#FFC107"
_C_GREEN     = "#00B050"
_C_RED       = "#FF0000"
_C_AMBER     = "#FFC000"


def _exec_chart(summary, horizon: int) -> go.Figure:
    years  = list(range(1, horizon + 1))
    az_capex = summary.az_cf_capex_by_year[1: horizon + 1]
    az_opex  = summary.az_cf_opex_by_year[1: horizon + 1]
    az_azure = summary.az_cf_azure_by_year[1: horizon + 1]
    az_mig   = [v if v != 0 else None for v in summary.az_cf_migration_by_year[1: horizon + 1]]
    sq_total = summary.sq_cf_by_year[1: horizon + 1]
    az_total = summary.az_cf_by_year[1: horizon + 1]

    fig = go.Figure()
    fig.add_trace(go.Bar(name="Retained CAPEX",     x=years, y=az_capex, marker_color=_C_CAPEX,
                         hovertemplate="Y%{x} CAPEX: $%{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Bar(name="Retained OPEX",      x=years, y=az_opex,  marker_color=_C_OPEX,
                         hovertemplate="Y%{x} OPEX: $%{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Bar(name="Azure Consumption",  x=years, y=az_azure, marker_color=_C_AZURE_BAR,
                         hovertemplate="Y%{x} Azure: $%{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Bar(name="Migration Costs",    x=years, y=az_mig,   marker_color=_C_MIGRATION,
                         hovertemplate="Y%{x} Migration: $%{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Scatter(name="On-Prem (SQ)",   x=years, y=sq_total,
                             mode="lines+markers", line=dict(color=_C_SQ, width=3), marker=dict(size=8),
                             hovertemplate="Y%{x} On-Prem: $%{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Scatter(name="Azure Total",    x=years, y=az_total,
                             mode="lines+markers", line=dict(color=_C_AZ, width=3, dash="dot"), marker=dict(size=8),
                             hovertemplate="Y%{x} Azure: $%{y:,.0f}<extra></extra>"))
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


def _kpi_metric(col, label: str, value: str, delta: str | None = None, color: str = "normal"):
    col.metric(label, value, delta=delta)


def render():
    st.title("Step 5 · Export & Presentation")

    if "inputs" not in st.session_state:
        st.warning("Complete Steps 1–2 first.")
        return

    inputs  = st.session_state["inputs"]
    bm: BenchmarkConfig = st.session_state.get("benchmarks", BenchmarkConfig.from_yaml())

    with st.spinner("Running engine…"):
        sq   = status_quo.compute(inputs, bm)
        depr = depreciation.compute(inputs, bm)
        ret  = retained_costs.compute(inputs, bm, sq)
        fc   = financial_case.compute(inputs, bm, sq, ret, depr)
        summary = outputs.compute(inputs, bm, fc)

    client = inputs.engagement.client_name

    tab_dl, tab_slides, tab_detail = st.tabs(["📥 Downloads", "🖥️ Presentation View", "📋 Detail Table"])

    # ================================================================
    # DOWNLOADS TAB
    # ================================================================
    with tab_dl:
        st.subheader("Export Business Case")

        col_pptx, col_xlsx = st.columns(2)

        # --- PowerPoint ---
        with col_pptx:
            st.markdown("#### PowerPoint Deck (.pptx)")
            st.caption(
                "2-slide deck: **Slide 1** — KPI cards + dual 5Y/10Y "
                "stacked bar+line charts.  **Slide 2** — Annual cashflow table "
                "and savings waterfall."
            )
            with st.spinner("Building PowerPoint…"):
                try:
                    pptx_bytes = build_pptx(summary, fc, inputs, bm)
                    st.download_button(
                        label="⬇️ Download PowerPoint",
                        data=pptx_bytes,
                        file_name=f"{client.replace(' ', '_')}_BizCase.pptx",
                        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        type="primary",
                    )
                    st.success(f"Ready — {len(pptx_bytes) / 1024:.0f} KB")

                    # Chart quality note
                    try:
                        import kaleido  # noqa: F401
                        st.caption("✓ kaleido installed — charts rendered as images in the deck.")
                    except ImportError:
                        st.info(
                            "Install `kaleido` (`pip install kaleido`) to embed "
                            "chart images in the PowerPoint slides. "
                            "The placeholders in the deck will be replaced with rendered charts."
                        )
                except Exception as exc:
                    st.error(f"PowerPoint build failed: {exc}")

        # --- Excel ---
        with col_xlsx:
            st.markdown("#### Pre-Filled Excel Workbook (.xlsx)")
            st.caption(
                "The BV Benchmark Business Case template with all yellow input cells "
                "pre-filled from this session's inputs. Open in Excel and press "
                "**Ctrl+Alt+F9** to recalculate — results should match the Step 4 output."
            )
            import pathlib
            template_path = "Template_BV Benchmark Business Case v6.xlsm"
            if pathlib.Path(template_path).exists():
                with st.spinner("Building Excel workbook…"):
                    try:
                        xlsx_bytes = build_excel(inputs, bm, template_path)
                        st.download_button(
                            label="⬇️ Download Excel",
                            data=xlsx_bytes,
                            file_name=f"{client.replace(' ', '_')}_BizCase.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            type="primary",
                        )
                        st.success(f"Ready — {len(xlsx_bytes) / 1024:.0f} KB")
                        st.caption(
                            "Note: VBA macros are stripped from the export (openpyxl limitation). "
                            "If you need the macro-enabled version (.xlsm), fill the yellow cells manually "
                            "using the values from the Pre-Filled Excel as a reference."
                        )
                    except Exception as exc:
                        st.error(f"Excel build failed: {exc}")
            else:
                st.warning(
                    f"Template workbook not found at `{template_path}`. "
                    "Place it in the project root directory to enable Excel export."
                )

    # ================================================================
    # PRESENTATION VIEW TAB
    # ================================================================
    with tab_slides:
        st.markdown(
            f"## {client} — Azure Migration Business Case",
        )
        st.caption("Presentation mode — designed for screen-share. Use browser full-screen (F11) for best results.")

        pb_str   = f"{summary.payback_years:.1f} yrs" if summary.payback_years else "N/A"

        # Row 1: headline KPIs
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("CF NPV (10-Year)",  f"${summary.npv_cf_10yr:,.0f}")
        k2.metric("CF NPV (5-Year)",   f"${summary.npv_cf_5yr:,.0f}")
        k3.metric("P&L NPV (10-Year)", f"${summary.npv_10yr:,.0f}")
        k4.metric("10-Year ROI",       f"{summary.roi_10yr:.0%}")
        k5.metric("Payback",           pb_str)
        k6.metric("Yr-10 Savings",     f"${summary.savings_yr10:,.0f}")

        st.divider()

        # Row 2: cost/VM
        v1, v2, v3, _ = st.columns([1, 1, 1, 3])
        v1.metric("On-Prem Cost/VM/yr", f"${summary.on_prem_cost_per_vm_yr:,.0f}")
        v2.metric("Azure Cost/VM/yr",   f"${summary.azure_cost_per_vm_yr:,.0f}")
        v3.metric("Savings/VM/yr",      f"${summary.savings_per_vm_yr:,.0f}")

        st.divider()

        # Dual horizon charts side-by-side
        st.subheader("Annual Cost Comparison — On-Prem vs Azure Migration")
        st.caption(
            "**Stacked bars** = Azure case cost components (Retained CAPEX + OPEX + Azure Consumption + Migration).  "
            "**Lines** = all-up cost for each scenario."
        )

        ch5, ch10 = st.columns(2)
        with ch5:
            st.markdown("##### 5-Year Horizon")
            st.plotly_chart(_exec_chart(summary, 5), use_container_width=True)
        with ch10:
            st.markdown("##### 10-Year Horizon")
            st.plotly_chart(_exec_chart(summary, 10), use_container_width=True)

        st.divider()

        # Cumulative savings
        st.subheader("Cumulative Cash Savings")
        cs5, cs10 = st.columns(2)
        years10 = list(range(1, 11))
        cf_sav   = summary.annual_cf_savings[1:]
        cumul = []
        running = 0.0
        for s in cf_sav:
            running += s
            cumul.append(running)

        for col, h in [(cs5, 5), (cs10, 10)]:
            with col:
                fig_c = go.Figure()
                fig_c.add_trace(go.Scatter(
                    x=years10[:h], y=cumul[:h],
                    mode="lines+markers",
                    line=dict(color=_C_AZ, width=2),
                    fill="tozeroy", fillcolor="rgba(0,120,212,0.15)",
                    hovertemplate="Y%{x}: $%{y:,.0f}<extra></extra>",
                ))
                fig_c.add_hline(y=0, line_dash="dash", line_color="gray", annotation_text="Break-even")
                fig_c.update_layout(
                    title=f"{h}-Year",
                    xaxis=dict(title="Year", tickmode="linear", tick0=1, dtick=1),
                    yaxis=dict(title="Cumulative Savings (USD)", tickformat="$,.0f"),
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                    margin=dict(t=30, b=40),
                )
                st.plotly_chart(fig_c, use_container_width=True)

        st.divider()

        # Waterfall
        st.subheader("Transformative Value Waterfall (Avg Annual, 10-Year P&L)")
        wf     = summary.waterfall
        labels = list(wf.keys())
        values = list(wf.values())
        measure = ["absolute"] + ["relative"] * (len(labels) - 2) + ["total"]
        fig_wf = go.Figure(go.Waterfall(
            orientation="v", measure=measure, x=labels, y=values,
            connector={"line": {"color": "rgb(63,63,63)"}},
            increasing={"marker": {"color": _C_AZ}},
            decreasing={"marker": {"color": _C_SQ}},
            totals={"marker": {"color": "#50E6FF"}},
        ))
        fig_wf.update_layout(
            xaxis_title="Category", yaxis_title="USD/yr (avg)",
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_wf, use_container_width=True)

    # ================================================================
    # DETAIL TABLE TAB
    # ================================================================
    with tab_detail:
        st.subheader("Annual Cash Flow — Full Detail (Y1–Y10)")
        st.caption("Copy this table into Excel or another tool for further analysis.")

        import pandas as pd
        years10 = list(range(1, 11))
        running_cf = 0.0
        running_pl = 0.0
        rows = []
        for yr in years10:
            running_cf += summary.annual_cf_savings[yr]
            running_pl += summary.annual_savings[yr]
            rows.append({
                "Year":             yr,
                "SQ On-Prem CF":    summary.sq_cf_by_year[yr],
                "Azure Total CF":   summary.az_cf_by_year[yr],
                "  Retained CAPEX": summary.az_cf_capex_by_year[yr],
                "  Retained OPEX":  summary.az_cf_opex_by_year[yr],
                "  Azure Consump.": summary.az_cf_azure_by_year[yr],
                "  Migration":      summary.az_cf_migration_by_year[yr],
                "CF Savings":       summary.annual_cf_savings[yr],
                "Cum CF Savings":   running_cf,
                "P&L SQ":           fc.sq_total()[yr],
                "P&L Azure":        fc.az_total()[yr],
                "P&L Savings":      summary.annual_savings[yr],
                "Cum P&L Savings":  running_pl,
            })

        df = pd.DataFrame(rows)

        # Format as currency
        def fmt(v):
            return f"${v:,.0f}"

        fmt_df = df.copy()
        for col in df.columns[1:]:
            fmt_df[col] = df[col].apply(fmt)

        st.dataframe(fmt_df, use_container_width=True, hide_index=True)

        # 5-year subtotals
        st.markdown("**5-Year Subtotals**")
        sub5_cols = st.columns(4)
        sub5_cols[0].metric("SQ CF (5-yr)",    f"${summary.total_sq_cf_5yr:,.0f}")
        sub5_cols[1].metric("Azure CF (5-yr)",  f"${summary.total_az_cf_5yr:,.0f}")
        sub5_cols[2].metric("CF NPV (5-yr)",    f"${summary.npv_cf_5yr:,.0f}")
        sub5_cols[3].metric("P&L NPV (5-yr)",   f"${summary.npv_5yr:,.0f}")

        st.markdown("**10-Year Totals**")
        sub10_cols = st.columns(4)
        sub10_cols[0].metric("SQ CF (10-yr)",   f"${summary.total_sq_cf_10yr:,.0f}")
        sub10_cols[1].metric("Azure CF (10-yr)", f"${summary.total_az_cf_10yr:,.0f}")
        sub10_cols[2].metric("CF NPV (10-yr)",   f"${summary.npv_cf_10yr:,.0f}")
        sub10_cols[3].metric("P&L NPV (10-yr)",  f"${summary.npv_10yr:,.0f}")
