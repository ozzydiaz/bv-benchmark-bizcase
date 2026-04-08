"""
engine/export.py
================
Export the business case to PowerPoint (.pptx) and pre-filled Excel (.xlsx).

PowerPoint output
-----------------
Two slides:
  Slide 1 — Executive Summary (KPI cards + dual 5Y/10Y stacked-bar+line chart)
  Slide 2 — Detailed Financial Case (annual cashflow table + cumulative savings)

Excel output
------------
Writes the engine's computed inputs back into a copy of the BV Benchmark
Business Case template, filling every yellow input cell so the workbook
produces the same results as the Python engine when opened and saved.

Usage
-----
>>> from engine.export import build_pptx, build_excel
>>> pptx_bytes = build_pptx(summary, fc, inputs, benchmarks)
>>> xlsx_bytes = build_excel(inputs, benchmarks)
"""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.models import BenchmarkConfig, BusinessCaseInputs
    from engine.financial_case import FinancialCase
    from engine.outputs import BusinessCaseSummary

# Template workbook — path relative to this file so it works on Streamlit Cloud
_TEMPLATE_PATH = Path(__file__).parent.parent / "Template_BV Benchmark Business Case v6.xlsm"

# ---------------------------------------------------------------------------
# Colour palette (consistent with results.py)
# ---------------------------------------------------------------------------
_C_DARK_BG   = "1F1F1F"
_C_PANEL     = "2D2D2D"
_C_SQ        = "FF6B35"   # on-prem coral
_C_AZ        = "0078D4"   # azure blue
_C_CAPEX     = "4A4A6A"   # slate
_C_OPEX      = "C76C00"   # burnt orange
_C_AZURE_BAR = "50B0F0"   # sky blue
_C_MIGRATION = "FFC107"   # amber
_C_WHITE     = "FFFFFF"
_C_GREY      = "AAAAAA"
_C_GREEN     = "00B050"
_C_AMBER     = "FFC000"
_C_RED       = "FF0000"


# ---------------------------------------------------------------------------
# PowerPoint helpers
# ---------------------------------------------------------------------------

def _rgb(hex6: str):
    from pptx.util import Pt  # noqa: F401 – ensure pptx importable
    from pptx.dml.color import RGBColor
    h = hex6.lstrip("#")
    return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _inches(n: float):
    from pptx.util import Inches
    return Inches(n)


def _pt(n: float):
    from pptx.util import Pt
    return Pt(n)


def _add_textbox(slide, left, top, width, height, text, font_size=11,
                 bold=False, color=_C_WHITE, align="left"):
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN
    txb = slide.shapes.add_textbox(
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    tf = txb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}.get(align, PP_ALIGN.LEFT)
    run = p.add_run()
    run.text = text
    run.font.size = Pt(font_size)
    run.font.bold = bold
    run.font.color.rgb = _rgb(color)
    return txb


def _add_rect(slide, left, top, width, height, fill_hex: str, line_hex: str | None = None):
    from pptx.util import Inches
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE_TYPE  # noqa: F401
    shape = slide.shapes.add_shape(
        1,  # MSO_SHAPE_TYPE.RECTANGLE
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(fill_hex)
    if line_hex:
        shape.line.color.rgb = _rgb(line_hex)
        shape.line.width = _pt(0.5)
    else:
        shape.line.fill.background()
    return shape


def _kpi_card(slide, left: float, top: float, label: str, value: str,
              sub: str = "", accent: str = _C_AZ):
    """Render a KPI card: coloured top accent bar + label + value + sub."""
    card_w, card_h = 1.55, 1.0
    # accent bar
    _add_rect(slide, left, top, card_w, 0.06, accent)
    # card body
    _add_rect(slide, left, top + 0.06, card_w, card_h - 0.06, _C_PANEL)
    # label
    _add_textbox(slide, left + 0.05, top + 0.10, card_w - 0.1, 0.22,
                 label, font_size=7, color=_C_GREY)
    # value
    _add_textbox(slide, left + 0.05, top + 0.30, card_w - 0.1, 0.40,
                 value, font_size=16, bold=True, color=_C_WHITE)
    if sub:
        _add_textbox(slide, left + 0.05, top + 0.72, card_w - 0.1, 0.22,
                     sub, font_size=7, color=_C_GREY)


def _bar_chart_image(summary, horizon: int, width_px=900, height_px=380) -> bytes:
    """
    Render the stacked bar + line chart as a PNG image using plotly's static
    image export (kaleido).  Returns PNG bytes.
    """
    try:
        import plotly.graph_objects as go
        import plotly.io as pio

        years = list(range(1, horizon + 1))
        az_capex = summary.az_cf_capex_by_year[1: horizon + 1]
        az_opex  = summary.az_cf_opex_by_year[1: horizon + 1]
        az_azure = summary.az_cf_azure_by_year[1: horizon + 1]
        az_mig   = [v if v != 0 else None for v in summary.az_cf_migration_by_year[1: horizon + 1]]
        sq_total = summary.sq_cf_by_year[1: horizon + 1]
        az_total = summary.az_cf_by_year[1: horizon + 1]

        fig = go.Figure()
        fig.add_trace(go.Bar(name="Retained CAPEX", x=years, y=az_capex,
                             marker_color=f"#{_C_CAPEX}"))
        fig.add_trace(go.Bar(name="Retained OPEX",  x=years, y=az_opex,
                             marker_color=f"#{_C_OPEX}"))
        fig.add_trace(go.Bar(name="Azure Consumption", x=years, y=az_azure,
                             marker_color=f"#{_C_AZURE_BAR}"))
        fig.add_trace(go.Bar(name="Migration", x=years, y=az_mig,
                             marker_color=f"#{_C_MIGRATION}"))
        fig.add_trace(go.Scatter(name="On-Prem (SQ)", x=years, y=sq_total,
                                 mode="lines+markers",
                                 line=dict(color=f"#{_C_SQ}", width=3), marker=dict(size=6)))
        fig.add_trace(go.Scatter(name="Azure Total", x=years, y=az_total,
                                 mode="lines+markers",
                                 line=dict(color=f"#{_C_AZ}", width=3, dash="dot"),
                                 marker=dict(size=6)))
        fig.update_layout(
            barmode="stack",
            paper_bgcolor="#1F1F1F",
            plot_bgcolor="#1F1F1F",
            font=dict(color="#FFFFFF", size=11),
            xaxis=dict(title="Year", gridcolor="#444444", tickmode="linear", tick0=1, dtick=1),
            yaxis=dict(title="USD", gridcolor="#444444", tickformat="$,.0f"),
            legend=dict(orientation="h", y=-0.25, font=dict(size=9)),
            margin=dict(l=60, r=20, t=20, b=80),
        )
        return pio.to_image(fig, format="png", width=width_px, height=height_px)
    except Exception:
        return b""  # kaleido not installed — skip chart image


# ---------------------------------------------------------------------------
# PowerPoint builder
# ---------------------------------------------------------------------------

def build_pptx(
    summary: "BusinessCaseSummary",
    fc: "FinancialCase",
    inputs: "BusinessCaseInputs",
    benchmarks: "BenchmarkConfig",
) -> bytes:
    """
    Build a 2-slide PowerPoint deck and return as bytes.

    Slide 1 — Executive Summary
      • KPI cards: CF NPV 10Y / CF NPV 5Y / P&L NPV 10Y / ROI 10Y / Payback / Yr10 Savings
      • Stacked bar + line chart: 5-year and 10-year horizons (side-by-side)
      • Cumulative savings headline

    Slide 2 — Annual Cash Flow Detail
      • Annual table (Y1–Y10): SQ CF | Azure CF | Annual Savings | Cumulative Savings
      • Waterfall breakdown summary
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor

    prs = Presentation()
    prs.slide_width  = Inches(13.33)
    prs.slide_height = Inches(7.5)

    blank_layout = prs.slide_layouts[6]  # blank

    client = inputs.engagement.client_name
    pb_str = f"{summary.payback_years:.1f} yrs" if summary.payback_years else "N/A"
    pb_accent = _C_GREEN if (summary.payback_years or 99) <= 3 else (_C_AMBER if (summary.payback_years or 99) <= 5 else _C_RED)
    roi_accent = _C_GREEN if summary.roi_10yr > 0.5 else (_C_AMBER if summary.roi_10yr > 0 else _C_RED)
    npv_accent = _C_GREEN if summary.npv_cf_10yr > 0 else _C_RED

    # ==================================================================
    # SLIDE 1 — Executive Summary
    # ==================================================================
    s1 = prs.slides.add_slide(blank_layout)

    # Background
    _add_rect(s1, 0, 0, 13.33, 7.5, _C_DARK_BG)

    # Header bar
    _add_rect(s1, 0, 0, 13.33, 0.55, _C_AZ)
    _add_textbox(s1, 0.15, 0.08, 8, 0.40, f"{client} — Azure Migration Business Case", font_size=18, bold=True)
    _add_textbox(s1, 9.0, 0.12, 4.0, 0.30, "Executive Summary", font_size=11, color=_C_WHITE, align="right")

    # KPI cards — row 1
    kpi_top = 0.70
    kpis = [
        ("CF NPV (10-Year)", f"${summary.npv_cf_10yr:,.0f}", "Cash-flow basis", npv_accent),
        ("CF NPV (5-Year)",  f"${summary.npv_cf_5yr:,.0f}",  "Cash-flow basis", npv_accent),
        ("P&L NPV (10-Year)",f"${summary.npv_10yr:,.0f}",   "Depreciation basis", _C_AZ),
        ("10-Year ROI",      f"{summary.roi_10yr:.0%}",     "NPV return on investment", roi_accent),
        ("Payback Period",   pb_str,                         "Cash-flow break-even", pb_accent),
        ("Yr-10 Savings",    f"${summary.savings_yr10:,.0f}", "Annual run-rate", _C_GREEN),
        ("On-Prem Cost/VM",  f"${summary.on_prem_cost_per_vm_yr:,.0f}", "Year 10 / VM / yr", _C_SQ),
        ("Azure Cost/VM",    f"${summary.azure_cost_per_vm_yr:,.0f}",  "Year 10 / VM / yr", _C_AZ),
    ]
    for i, (label, val, sub, accent) in enumerate(kpis):
        col = i % 4
        row = i // 4
        _kpi_card(s1, 0.15 + col * 1.65, kpi_top + row * 1.10, label, val, sub, accent)

    # Chart images
    chart_top = 2.95
    chart_h   = 4.30

    img5  = _bar_chart_image(summary, 5,  width_px=520, height_px=350)
    img10 = _bar_chart_image(summary, 10, width_px=520, height_px=350)

    if img5:
        from pptx.util import Inches as _I
        s1.shapes.add_picture(io.BytesIO(img5),  _I(0.15), _I(chart_top), _I(6.4), _I(chart_h))
    else:
        _add_textbox(s1, 0.15, chart_top + 1.5, 6.4, 0.5,
                     "[5-Year chart — install kaleido for image export]",
                     font_size=9, color=_C_GREY, align="center")

    if img10:
        from pptx.util import Inches as _I
        s1.shapes.add_picture(io.BytesIO(img10), _I(6.78), _I(chart_top), _I(6.40), _I(chart_h))
    else:
        _add_textbox(s1, 6.78, chart_top + 1.5, 6.4, 0.5,
                     "[10-Year chart — install kaleido for image export]",
                     font_size=9, color=_C_GREY, align="center")

    # Chart labels
    _add_textbox(s1, 0.15, chart_top - 0.25, 6.4, 0.25,
                 "5-Year Cost Comparison", font_size=10, bold=True, color=_C_WHITE)
    _add_textbox(s1, 6.78, chart_top - 0.25, 6.4, 0.25,
                 "10-Year Cost Comparison", font_size=10, bold=True, color=_C_WHITE)

    # Legend note
    legend = ("■ Retained CAPEX  ■ Retained OPEX  ■ Azure Consumption  ■ Migration (amber)  "
              "— On-Prem SQ (line)  --- Azure Total (dotted)")
    _add_textbox(s1, 0.15, 7.22, 13.0, 0.25, legend, font_size=7, color=_C_GREY)

    # ==================================================================
    # SLIDE 2 — Annual Cash Flow Detail
    # ==================================================================
    s2 = prs.slides.add_slide(blank_layout)
    _add_rect(s2, 0, 0, 13.33, 7.5, _C_DARK_BG)
    _add_rect(s2, 0, 0, 13.33, 0.55, _C_AZ)
    _add_textbox(s2, 0.15, 0.08, 8, 0.40, f"{client} — Annual Cash Flow & Savings", font_size=18, bold=True)

    # Table header
    col_x = [0.15, 2.25, 4.35, 6.45, 8.55, 10.65]
    col_w = [1.9, 2.0, 2.0, 2.0, 2.0, 2.0]
    headers = ["Year", "SQ Total CF", "Azure Total CF", "Retained OPEX", "Azure Costs", "Cum. Savings"]
    row_y = 0.65
    _add_rect(s2, 0.1, row_y, 13.0, 0.28, _C_AZ)
    for i, h in enumerate(headers):
        _add_textbox(s2, col_x[i], row_y + 0.03, col_w[i], 0.24, h, font_size=8, bold=True, align="right" if i > 0 else "left")

    cumulative = 0.0
    for yr in range(1, 11):
        row_y += 0.30
        bg = _C_PANEL if yr % 2 == 0 else "252525"
        _add_rect(s2, 0.1, row_y, 13.0, 0.28, bg)
        cumulative += summary.annual_cf_savings[yr]
        cum_color = _C_GREEN if cumulative >= 0 else _C_RED
        row_vals = [
            (f"Year {yr}", "left", _C_WHITE),
            (f"${summary.sq_cf_by_year[yr]:>12,.0f}", "right", _C_SQ),
            (f"${summary.az_cf_by_year[yr]:>12,.0f}", "right", _C_AZ),
            (f"${summary.az_cf_opex_by_year[yr]:>12,.0f}", "right", _C_GREY),
            (f"${summary.az_cf_azure_by_year[yr]:>12,.0f}", "right", _C_AZURE_BAR),
            (f"${cumulative:>12,.0f}", "right", cum_color),
        ]
        for i, (txt, align, col) in enumerate(row_vals):
            _add_textbox(s2, col_x[i], row_y + 0.03, col_w[i], 0.24, txt,
                         font_size=8, color=col, align=align)

    # Totals row
    row_y += 0.30
    _add_rect(s2, 0.1, row_y, 13.0, 0.28, "003366")
    totals = [
        ("10-Year Total", "left", _C_WHITE),
        (f"${summary.total_sq_cf_10yr:>12,.0f}", "right", _C_SQ),
        (f"${summary.total_az_cf_10yr:>12,.0f}", "right", _C_AZ),
        ("", "right", _C_WHITE),
        ("", "right", _C_WHITE),
        (f"${cumulative:>12,.0f}", "right", _C_GREEN if cumulative >= 0 else _C_RED),
    ]
    for i, (txt, align, col) in enumerate(totals):
        _add_textbox(s2, col_x[i], row_y + 0.03, col_w[i], 0.24, txt,
                     font_size=8, bold=True, color=col, align=align)

    # Waterfall summary
    wf_top = row_y + 0.55
    _add_textbox(s2, 0.15, wf_top, 13.0, 0.25, "Average Annual Savings by Category (P&L, 10-Year)",
                 font_size=9, bold=True)
    wf_top += 0.28
    wf_cols = list(summary.waterfall.items())
    for i, (cat, val) in enumerate(wf_cols):
        col = _C_WHITE
        if "Reduction" in cat:
            col = _C_GREEN
        elif "Increase" in cat or "Azure Case" in cat:
            col = _C_SQ
        x_pos = 0.15 + (i % 4) * 3.25
        y_pos = wf_top + (i // 4) * 0.32
        _add_textbox(s2, x_pos, y_pos, 3.1, 0.28,
                     f"{cat}: ${val:,.0f}/yr", font_size=8, color=col)

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Excel builder — pre-fill template yellow cells
# ---------------------------------------------------------------------------

def build_excel(
    inputs: "BusinessCaseInputs",
    benchmarks: "BenchmarkConfig",
    template_path: "str | Path | None" = None,
) -> bytes:
    """
    Write engine inputs into the BV Benchmark workbook template yellow cells
    and return the modified workbook as bytes (.xlsx, macros stripped).

    The resulting file can be opened in Excel to verify or re-run formulas.
    Cells written: 1-Client Variables (D9, D10, D24, D25, D26, D39, D44, D49,
    D54, D66, D67, D68) + 2a-Consumption Plan (D8, D9, D10, E17:N17, E21:N21,
    E22:N22).
    """
    import openpyxl
    from copy import deepcopy

    resolved = Path(template_path) if template_path else _TEMPLATE_PATH
    wb = openpyxl.load_workbook(resolved, keep_vba=False, data_only=False)
    cv  = wb["1-Client Variables"]
    cp  = wb["2a-Consumption Plan Wk1"]

    wl  = inputs.workloads[0]         if inputs.workloads         else None
    plan = inputs.consumption_plans[0] if inputs.consumption_plans else None

    # Engagement
    cv["D9"]  = inputs.engagement.client_name
    cv["D10"] = inputs.engagement.local_currency_name

    # Hardware lifecycle
    cv["D24"] = inputs.hardware.depreciation_life_years
    cv["D25"] = inputs.hardware.actual_usage_life_years
    cv["D26"] = inputs.hardware.expected_future_growth_rate
    cv["D27"] = inputs.hardware.hardware_renewal_during_migration_pct

    if wl:
        cv["D39"] = wl.num_vms
        cv["D40"] = wl.num_physical_servers_excl_hosts
        cv["D44"] = wl.allocated_vcpu
        cv["D49"] = wl.allocated_vmemory_gb
        cv["D54"] = wl.allocated_storage_gb
        cv["D66"] = wl.vcpu_per_core_ratio
        cv["D67"] = wl.pcores_with_windows_server
        cv["D68"] = wl.pcores_with_windows_esu
        if wl.pcores_with_sql_server is not None:
            cv["D70"] = wl.pcores_with_sql_server
        if wl.pcores_with_sql_esu is not None:
            cv["D71"] = wl.pcores_with_sql_esu

    if plan:
        cp["D8"]  = plan.azure_vcpu
        cp["D9"]  = plan.azure_memory_gb
        cp["D10"] = plan.azure_storage_gb

        ramp_cols = ["E", "F", "G", "H", "I", "J", "K", "L", "M", "N"]
        for i, col in enumerate(ramp_cols):
            cp[f"{col}17"] = plan.migration_ramp_pct[i]
            cp[f"{col}21"] = plan.aco_by_year[i]
            cp[f"{col}22"] = plan.ecif_by_year[i]

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
