"""
Page — Fact Checker

Validates the Python engine's computed outputs against a saved copy of the
BV Benchmark Business Case Excel workbook (.xlsm / .xlsx).

Two modes
---------
1. Engine-only sanity check (no workbook required)
   Runs a battery of self-consistency checks directly against the engine
   outputs that are already in session state — no file upload needed.

2. Excel cross-check (requires saved workbook)
   Compares every material KPI against the workbook's cached formula
   values using engine.fact_checker.  The workbook must have been saved
   in Excel so formula cells contain their most recently computed values.
"""
from __future__ import annotations

import pathlib
import tempfile

import streamlit as st

from engine.models import BenchmarkConfig, BusinessCaseInputs
from engine.outputs import BusinessCaseSummary


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _fmt(v: float) -> str:
    return f"${v:,.0f}"


def _sanity_checks(summary: "BusinessCaseSummary", inputs: "BusinessCaseInputs") -> list[dict[str, str]]:
    """
    Run engine-only self-consistency checks that need no Excel workbook.
    Returns a list of check dicts: {name, value, expected, status, note}.
    """
    checks: list[dict[str, str]] = []

    def _add(name: str, ok: bool, value: str, expected: str, note: str = "") -> None:
        checks.append({
            "Check": name,
            "Value": value,
            "Expected": expected,
            "Status": "✅ PASS" if ok else "❌ FAIL",
            "Note": note,
        })

    # 1. NPV 10yr ≥ NPV 5yr (more compounding → larger NPV)
    _add(
        "CF NPV 10-yr ≥ CF NPV 5-yr",
        summary.npv_cf_10yr >= summary.npv_cf_5yr,
        _fmt(summary.npv_cf_10yr),
        f"≥ {_fmt(summary.npv_cf_5yr)}",
        "Cumulative savings grow over time",
    )

    # 2. Payback within projection window (if set)
    if summary.payback_years is not None:
        _add(
            "Payback within 10 years",
            summary.payback_years <= 10,
            f"{summary.payback_years:.1f} yrs",
            "≤ 10 yrs",
        )
    else:
        checks.append({
            "Check": "Payback within 10 years",
            "Value": "N/A",
            "Expected": "≤ 10 yrs",
            "Status": "⚠️ WARN",
            "Note": "Payback not achieved within projection window — review migration costs",
        })

    # 3. ROI 10yr > ROI 5yr
    _add(
        "ROI 10-yr ≥ ROI 5-yr",
        summary.roi_10yr >= summary.roi_5yr,
        f"{summary.roi_10yr:.1%}",
        f"≥ {summary.roi_5yr:.1%}",
        "Longer horizon should compound returns",
    )

    # 4. Yr-10 saving positive
    _add(
        "Year-10 annual saving > 0",
        summary.savings_yr10 > 0,
        _fmt(summary.savings_yr10),
        "> 0",
        "Azure should be cheaper than on-prem at steady state",
    )

    # 5. Azure cost/VM < On-Prem cost/VM
    _add(
        "Azure cost/VM < On-Prem cost/VM",
        summary.azure_cost_per_vm_yr < summary.on_prem_cost_per_vm_yr,
        _fmt(summary.azure_cost_per_vm_yr),
        f"< {_fmt(summary.on_prem_cost_per_vm_yr)}",
        "Core efficiency premise",
    )

    # 6. Annual savings series has at least one positive value post-Y1
    post_y1_positive = any(v > 0 for v in summary.annual_savings[2:])
    _add(
        "Positive annual saving in at least one year post-Y1",
        post_y1_positive,
        f"max Y2–Y10: {_fmt(max(summary.annual_savings[2:]))}",
        "> 0",
        "Migration costs typically cause Y1 loss — subsequent years should recover",
    )

    # 7. SQ year-1 cost aligns with TCO total (rough sanity)
    if inputs and inputs.workloads:
        num_vms = sum(w.num_vms for w in inputs.workloads)
        sq_yr1 = summary.sq_cf_by_year[1] if summary.sq_cf_by_year else 0
        if num_vms > 0 and sq_yr1 > 0:
            cost_per_vm = sq_yr1 / num_vms
            ok = 1_000 < cost_per_vm < 500_000   # broad sanity range
            _add(
                "On-prem cost/VM plausible ($1k–$500k/yr)",
                ok,
                _fmt(cost_per_vm),
                "$1,000 – $500,000",
                "Rule-of-thumb range; flag if wildly outside",
            )

    # 8. CF savings arrays are consistent
    try:
        calc_cf_savings = [
            sq - az
            for sq, az in zip(summary.sq_cf_by_year, summary.az_cf_by_year)
        ]
        max_delta = max(
            abs(a - b) for a, b in zip(calc_cf_savings, summary.annual_cf_savings)
        )
        _add(
            "CF savings = SQ − Azure (array consistency)",
            max_delta < 1.0,
            f"max |Δ| = ${max_delta:,.2f}",
            "< $1",
            "Internal consistency check",
        )
    except Exception:
        pass

    return checks


# ──────────────────────────────────────────────────────────────────────────────
# Main render
# ──────────────────────────────────────────────────────────────────────────────

def render() -> None:
    st.title("🔍 Fact Checker")
    st.caption(
        "Validate the business case numbers before presenting to a customer. "
        "Run a quick engine sanity check against session results, or upload the "
        "saved Excel workbook for a full line-by-line cross-check."
    )

    from engine import status_quo, retained_costs, depreciation, financial_case, outputs as eng_outputs

    inputs = st.session_state.get("inputs")
    bm: BenchmarkConfig = st.session_state.get("benchmarks", BenchmarkConfig.from_yaml())

    if inputs is None:
        st.warning(
            "No business case in session. Run **⚡ Agent Intake** or complete "
            "**1 · Client Intake** and **2 · Consumption Plan** first."
        )
        return

    # Recompute summary (cheap — pure Python, no I/O)
    with st.spinner("Computing…"):
        sq   = status_quo.compute(inputs, bm)
        depr = depreciation.compute(inputs, bm)
        ret  = retained_costs.compute(inputs, bm, sq)
        fc   = financial_case.compute(inputs, bm, sq, ret, depr)
        summary = eng_outputs.compute(inputs, bm, fc)

    client = inputs.engagement.client_name or "—"
    st.markdown(f"**Engagement:** {client}")

    tab_sanity, tab_excel = st.tabs(["🧮 Engine Sanity Checks", "📋 Excel Cross-Check"])

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 1 — Engine-only sanity checks (no workbook needed)
    # ─────────────────────────────────────────────────────────────────────────
    with tab_sanity:
        st.subheader("Engine Sanity Checks")
        st.caption(
            "Self-consistency tests run directly against the computed business case. "
            "No file upload required. Results update automatically when you rerun the engine."
        )

        checks = _sanity_checks(summary, inputs)
        import pandas as pd
        df = pd.DataFrame(checks)

        passed  = sum(1 for c in checks if "PASS" in c["Status"])
        warned  = sum(1 for c in checks if "WARN" in c["Status"])
        failed  = sum(1 for c in checks if "FAIL" in c["Status"])
        overall_ok = failed == 0

        # Score bar
        score = 100 * passed / max(len(checks), 1)
        color = "#00B050" if score >= 90 else ("#FFC000" if score >= 70 else "#FF0000")
        s_col, r_col = st.columns([1, 3])
        s_col.markdown(
            f"<div style='text-align:center'>"
            f"<span style='font-size:2.5rem;font-weight:bold;color:{color}'>{score:.0f}%</span><br>"
            f"<span style='color:gray;font-size:0.8rem'>Confidence Score</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        with r_col:
            overall_label = "✅ All checks passed" if overall_ok else f"❌ {failed} check(s) failed"
            st.markdown(f"**{overall_label}**")
            m1, m2, m3 = st.columns(3)
            m1.metric("✅ PASS", passed)
            m2.metric("⚠️ WARN", warned)
            m3.metric("❌ FAIL", failed)

        st.divider()

        def _color_row(row: "pd.Series") -> list[str]:
            s = row["Status"]
            if "FAIL" in s:
                return ["background-color: #FFDDD9"] * len(row)
            if "WARN" in s:
                return ["background-color: #FFF4CC"] * len(row)
            if "PASS" in s:
                return ["background-color: #E6F4EA"] * len(row)
            return [""] * len(row)

        st.dataframe(
            df.style.apply(_color_row, axis=1),
            use_container_width=True,
            hide_index=True,
        )

        st.divider()
        st.markdown("#### 📊 Key Outputs")
        pb_str = f"{summary.payback_years:.1f} yrs" if summary.payback_years else "N/A"
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("CF NPV (10-yr)",  f"${summary.npv_cf_10yr:,.0f}")
        c2.metric("CF NPV (5-yr)",   f"${summary.npv_cf_5yr:,.0f}")
        c3.metric("P&L NPV (10-yr)", f"${summary.npv_10yr:,.0f}")
        c4.metric("10-yr ROI",       f"{summary.roi_10yr:.0%}")
        c5.metric("Payback",         pb_str)
        c6.metric("Yr-10 Saving",    f"${summary.savings_yr10:,.0f}")

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 2 — Excel cross-check
    # ─────────────────────────────────────────────────────────────────────────
    with tab_excel:
        st.subheader("Excel Cross-Check")
        st.caption(
            "Upload a saved copy of the **BV Benchmark Business Case** workbook "
            "(.xlsm or .xlsx). The workbook must have been **saved in Excel** so "
            "that all formula cells contain their most-recently computed values — "
            "openpyxl reads cached values only and does not execute formulas."
        )

        uploaded_wb = st.file_uploader(
            "Upload reference workbook (.xlsm / .xlsx)",
            type=["xlsm", "xlsx"],
            key="fc_page_wb",
        )

        if uploaded_wb:
            with tempfile.NamedTemporaryFile(suffix=uploaded_wb.name[-5:], delete=False) as tmp:
                tmp.write(uploaded_wb.read())
                tmp_path = pathlib.Path(tmp.name)

            with st.spinner("Running fact check against Excel…"):
                try:
                    from engine.fact_checker import run as _fc_run
                    report = _fc_run(str(tmp_path), inputs, bm)
                except Exception as exc:
                    st.error(f"Fact check failed: {exc}")
                    report = None

            if report:
                score2 = report.confidence_score
                color2 = "#00B050" if score2 >= 90 else ("#FFC000" if score2 >= 70 else "#FF0000")
                sc2, rc2 = st.columns([1, 3])
                sc2.markdown(
                    f"<div style='text-align:center'>"
                    f"<span style='font-size:2.5rem;font-weight:bold;color:{color2}'>{score2:.0f}%</span><br>"
                    f"<span style='color:gray;font-size:0.8rem'>Confidence Score</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                with rc2:
                    overall2 = "✅ PASS" if report.passed_overall else "❌ FAIL"
                    st.markdown(f"**Overall: {overall2}**")
                    x1, x2, x3, x4 = st.columns(4)
                    x1.metric("✅ PASS", report.passed)
                    x2.metric("⚠️ WARN", report.warned)
                    x3.metric("❌ FAIL", report.failed)
                    x4.metric("– SKIP", report.skipped)

                if report.input_mismatches:
                    with st.expander(f"⚠️ {len(report.input_mismatches)} input mismatch(es) detected"):
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
                        "Δ %": f"{c.delta_pct:+.1f}%" if c.status != "SKIP" else "–",
                        "Note": c.note,
                    })
                df_fc = pd.DataFrame(rows)

                def _csv(row: "pd.Series") -> list[str]:
                    s = row["Status"]
                    if "FAIL" in s:
                        return ["background-color: #FFDDD9"] * len(row)
                    if "WARN" in s:
                        return ["background-color: #FFF4CC"] * len(row)
                    if "PASS" in s:
                        return ["background-color: #E6F4EA"] * len(row)
                    return [""] * len(row)

                st.dataframe(
                    df_fc.style.apply(_csv, axis=1),
                    use_container_width=True,
                    hide_index=True,
                )
        else:
            st.info(
                "Upload the reference workbook above to compare engine outputs "
                "against the Excel model line-by-line."
            )
