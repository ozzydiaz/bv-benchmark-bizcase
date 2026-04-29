"""
Page — Fact Checker

Validates the Python engine's computed outputs against a saved copy of the
BV Benchmark Business Case Excel workbook (.xlsm / .xlsx).

Three modes
-----------
1. Pipeline health check (no workbook required)
   Catches upstream parser/pricing bugs BEFORE they reach the financial model:
   zero Azure compute cost (broken pricing cache), zero storage (wrong storage
   source), wrong VM counts (bad TCO scope), anomaly SKU matches, etc.
   These are the bugs that were silently producing wrong Customer A numbers.

2. Engine-only sanity check (no workbook required)
   Runs a battery of self-consistency checks directly against the engine
   outputs that are already in session state — no file upload needed.

3. Excel cross-check (requires saved workbook)
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


def _pipeline_health_checks(inputs: "BusinessCaseInputs") -> list[dict[str, str]]:
    """
    Checks that catch upstream parser and pricing bugs BEFORE they reach the
    financial model.  These are the exact failure modes observed in the Customer A
    diagnostic (C1 $0 compute, C2/C3 wrong storage, H1/H2 wrong scope, M1/M4
    anomaly VMs, H4 vcpu/core ratio).

    Unlike the financial sanity checks below, these do not require a completed
    business case — they examine the raw inputs from the pipeline.
    """
    from engine.fact_checker import _check_pipeline_plausibility
    checks: list[dict[str, str]] = []

    def _add(name: str, ok: bool, value: str, expected: str, note: str = "") -> None:
        checks.append({
            "Check": name,
            "Value": value,
            "Expected": expected,
            "Status": "✅ PASS" if ok else "❌ FAIL",
            "Note": note,
        })

    wl = inputs.workloads[0] if inputs.workloads else None
    cp = inputs.consumption_plans[0] if inputs.consumption_plans else None

    if not wl or not cp:
        _add("WorkloadInventory present", False, "None", "present", "Run Layer 1 first")
        return checks

    # ── Inventory scope ───────────────────────────────────────────────────
    _add(
        "num_vms > 0 (parser found VMs)",
        wl.num_vms > 0,
        f"{wl.num_vms:,}",
        "> 0",
        "0 = RVTools vInfo tab missing or wrong sheet name — silent parse failure",
    )
    _add(
        "allocated_vcpu > 0 (TCO baseline)",
        wl.allocated_vcpu > 0,
        f"{wl.allocated_vcpu:,}",
        "> 0",
        "0 = CPUs column not found; licence and TCO costs will be $0",
    )

    if wl.num_vms > 0:
        vcpu_per_vm = wl.allocated_vcpu / wl.num_vms
        _add(
            "vCPU/VM ratio plausible (0.5–64)",
            0.5 <= vcpu_per_vm <= 64,
            f"{vcpu_per_vm:.1f}",
            "0.5–64",
            "Outside range suggests column unit mismatch (MHz vs count)",
        )

    _add(
        "vcpu_per_core_ratio plausible (1.0–8.0)",
        1.0 <= wl.vcpu_per_core_ratio <= 8.0,
        f"{wl.vcpu_per_core_ratio:.2f}",
        "1.0–8.0",
        "< 1.0 = vHost zero-vCPU hosts included (bug H4); "
        "= 1.0 = vHost tab absent (fallback default)",
    )

    # ── Azure sizing ──────────────────────────────────────────────────────
    _add(
        "azure_vcpu > 0 (rightsizing produced output)",
        cp.azure_vcpu > 0,
        f"{cp.azure_vcpu:,}",
        "> 0",
        "0 = vm_records empty or build_with_validation failed",
    )
    _add(
        "azure_storage_gb > 0",
        cp.azure_storage_gb > 0,
        f"{cp.azure_storage_gb:,.0f} GB",
        "> 0 GB",
        "0 = vPartition/vDisk not parsed or _vm_storage_cost() found no source (bug C3)",
    )

    # ── Pricing plausibility (catches broken Azure pricing cache) ─────────
    compute_yr = cp.annual_compute_consumption_lc_y10
    _add(
        "Azure compute cost > $0",
        compute_yr > 0,
        f"${compute_yr:,.0f}/yr",
        "> $0/yr",
        "= $0 means pricing cache is empty/all-zero (bug C1). "
        "Run: scripts/validate_pricing_cache.py --purge",
    )
    if compute_yr > 0 and cp.azure_vcpu > 0:
        cost_per_vcpu = compute_yr / max(cp.azure_vcpu, 1)
        # ~$0.048/hr × 8760 hr = ~$420/vCPU/yr PAYG; $0.02/hr min = ~$175
        plausible = 150 <= cost_per_vcpu <= 8_000
        _add(
            "Azure compute cost/vCPU plausible ($150–$8k/yr)",
            plausible,
            f"${cost_per_vcpu:,.0f}/vCPU/yr",
            "$150–$8,000",
            "< $150 = pricing cache still has $0 entries; > $8k = wrong SKU family or region",
        )

    storage_yr = cp.annual_storage_consumption_lc_y10
    _add(
        "Azure storage cost > $0",
        storage_yr > 0,
        f"${storage_yr:,.0f}/yr",
        "> $0/yr",
        "= $0 despite storage GB set means gb_rate = 0 in cache (bug C2). "
        "Expected ~$0.075/GB/mo",
    )
    if storage_yr > 0 and cp.azure_storage_gb > 0:
        implied_rate = storage_yr / (cp.azure_storage_gb * 12)
        _add(
            "Storage rate plausible ($0.01–$0.50/GB/mo)",
            0.01 <= implied_rate <= 0.50,
            f"${implied_rate:.4f}/GB/mo",
            "$0.01–$0.50",
            "Expected ~$0.075/GB/mo for Premium SSD P-series. "
            "Check _DEFAULT_GB_RATE in azure_sku_matcher.py (bug C2)",
        )

    # ── Azure vs on-prem ratio check (catches anomaly VMs / M1) ──────────
    if wl.allocated_vcpu > 0 and cp.azure_vcpu > 0:
        ratio = cp.azure_vcpu / wl.allocated_vcpu
        _add(
            "Azure vCPU ≤ 2× on-prem vCPU",
            ratio <= 2.0,
            f"{cp.azure_vcpu:,} ({ratio:.2f}× on-prem)",
            "≤ 2.0×",
            "> 2× typically means host-proxy anomaly (bug M1) or like-for-like mode active",
        )

    # ── Additional checks from engine's _check_pipeline_plausibility ─────
    warnings = _check_pipeline_plausibility(inputs)
    for w in warnings:
        # Don't duplicate checks already reported above
        if not any(w[:30] in c["Note"] for c in checks if c["Status"] == "❌ FAIL"):
            checks.append({
                "Check": "Pipeline plausibility",
                "Value": "—",
                "Expected": "No warnings",
                "Status": "⚠️ WARN",
                "Note": w[:200],
            })

    return checks


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
        "Start with **Pipeline Health** to catch upstream bugs, then run a "
        "quick **Engine Sanity** check, or upload the saved Excel workbook for "
        "a full **Excel Cross-Check**."
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

    tab_pipeline, tab_sanity, tab_excel = st.tabs([
        "🚦 Pipeline Health", "🧮 Engine Sanity Checks", "📋 Excel Cross-Check"
    ])

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 0 — Pipeline health (parser/pricing/rightsizing inputs)
    # ─────────────────────────────────────────────────────────────────────────
    with tab_pipeline:
        st.subheader("Pipeline Health")
        st.caption(
            "Checks the raw inputs produced by the parser and rightsizing engine. "
            "These catch upstream bugs (broken pricing cache, wrong TCO scope, "
            "missing storage data) **before** they reach the financial model. "
            "All checks should pass before trusting Layer 3 outputs."
        )

        ph_checks = _pipeline_health_checks(inputs)
        import pandas as pd
        df_ph = pd.DataFrame(ph_checks)

        ph_passed = sum(1 for c in ph_checks if "PASS" in c["Status"])
        ph_warned = sum(1 for c in ph_checks if "WARN" in c["Status"])
        ph_failed = sum(1 for c in ph_checks if "FAIL" in c["Status"])
        ph_ok     = ph_failed == 0 and ph_warned == 0

        score_ph = 100 * ph_passed / max(len(ph_checks), 1)
        color_ph = "#00B050" if ph_ok else ("#FFC000" if ph_failed == 0 else "#FF0000")
        s_ph, r_ph = st.columns([1, 3])
        s_ph.markdown(
            f"<div style='text-align:center'>"
            f"<span style='font-size:2.5rem;font-weight:bold;color:{color_ph}'>{score_ph:.0f}%</span><br>"
            f"<span style='color:gray;font-size:0.8rem'>Health Score</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        with r_ph:
            label_ph = "✅ All pipeline checks passed" if ph_ok else f"❌ {ph_failed} failure(s) — fix before reviewing financial outputs"
            st.markdown(f"**{label_ph}**")
            mp1, mp2, mp3 = st.columns(3)
            mp1.metric("✅ PASS", ph_passed)
            mp2.metric("⚠️ WARN", ph_warned)
            mp3.metric("❌ FAIL", ph_failed)

        if ph_failed > 0:
            st.error(
                "**Pipeline failures detected.**  The financial model outputs are likely wrong. "
                "Fix the issues shown below, re-run the intake, then return here."
            )

        st.divider()

        def _color_ph_row(row: "pd.Series") -> list[str]:
            s = row["Status"]
            if "FAIL" in s: return ["background-color: #FFDDD9"] * len(row)
            if "WARN" in s: return ["background-color: #FFF4CC"] * len(row)
            if "PASS" in s: return ["background-color: #E6F4EA"] * len(row)
            return [""] * len(row)

        st.dataframe(
            df_ph.style.apply(_color_ph_row, axis=1),
            use_container_width=True,
            hide_index=True,
        )

        # ── Quick cost summary ────────────────────────────────────────────
        wl = inputs.workloads[0] if inputs.workloads else None
        cp = inputs.consumption_plans[0] if inputs.consumption_plans else None
        if wl and cp:
            st.divider()
            st.markdown("#### 📦 Raw Pipeline Outputs")
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            c1.metric("VMs (TCO baseline)", f"{wl.num_vms:,}")
            c2.metric("vCPU (TCO baseline)", f"{wl.allocated_vcpu:,}")
            c3.metric("Azure vCPU", f"{cp.azure_vcpu:,}")
            c4.metric("Azure Storage GB", f"{cp.azure_storage_gb:,.0f}")
            c5.metric("Compute Cost/yr", f"${cp.annual_compute_consumption_lc_y10:,.0f}")
            c6.metric("Storage Cost/yr", f"${cp.annual_storage_consumption_lc_y10:,.0f}")

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

                if report.pipeline_warnings:
                    with st.expander(f"🚨 {len(report.pipeline_warnings)} pipeline plausibility warning(s)"):
                        for w in report.pipeline_warnings:
                            st.error(w)

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
