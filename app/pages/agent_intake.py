"""
Page 0 — Agent Intake (Automated)

Cloud Economics & Business Value analyst persona.
User provides: customer name, currency, RVTools export.
Agent handles: inventory parsing, region inference, Azure pricing,
right-sizing, and business case composition.

Optional inputs: migration horizon, ACO / ECIF credits.
"""
from __future__ import annotations

import io
import zipfile
import streamlit as st
import plotly.graph_objects as go

from engine.models import BenchmarkConfig, MIGRATION_RAMP_PRESETS

_CURRENCIES = ["USD", "GBP", "EUR", "CAD", "AUD", "JPY", "INR", "BRL", "MXN", "SGD"]

_RAMP_OPTIONS = list(MIGRATION_RAMP_PRESETS.keys())

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(v: float) -> str:
    return f"${v:,.0f}"


def _run_pipeline(
    file_bytes: bytes,
    client_name: str,
    currency: str,
    ramp_preset: str,
    aco_y1: float,
    aco_y2: float,
    aco_y3: float,
    ecif_y1: float,
    ecif_y2: float,
    benchmarks: BenchmarkConfig,
) -> "PipelineResult":  # noqa: F821 (imported inside to avoid top-level error on missing deps)
    from engine.rvtools_to_inputs import build_business_case_from_bytes
    aco  = [aco_y1,  aco_y2,  aco_y3,  0, 0, 0, 0, 0, 0, 0]
    ecif = [ecif_y1, ecif_y2, 0,       0, 0, 0, 0, 0, 0, 0]
    return build_business_case_from_bytes(
        file_bytes=file_bytes,
        client_name=client_name,
        currency=currency,
        ramp_preset=ramp_preset,
        aco_by_year=aco,
        ecif_by_year=ecif,
        benchmarks=benchmarks,
    )


def _inv_summary_card(result) -> None:
    """Render parsed inventory summary as metric cards."""
    inv = result.inventory

    st.markdown("#### 📋 Parsed Inventory")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("VMs (TCO scope)",  f"{inv.num_vms:,}")
    c2.metric("Powered-On VMs",   f"{inv.num_vms_poweredon:,}")
    c3.metric("ESX Hosts",        f"{inv.num_hosts:,}")
    c4.metric("Total vCPUs",      f"{inv.total_vcpu:,}")
    c5.metric("Total Memory",     f"{inv.total_vmemory_gb:,.0f} GB")

    d1, d2, d3, d4, d5 = st.columns(5)
    d1.metric("Storage (prov.)",  f"{inv.total_disk_provisioned_gb:,.0f} GB")
    d2.metric("vCPU/pCore ratio", f"{inv.vcpu_per_core_ratio:.2f}×")
    d3.metric("Inferred Region",  result.region)
    d4.metric("Azure Pricing src", result.pricing.source.upper())
    d5.metric("Warnings",         len(result.warnings) or "None")

    st.markdown("#### 🖥️ OS & License Profile")
    e1, e2, e3, e4, e5 = st.columns(5)
    e1.metric("Windows pCores",   f"{inv.pcores_with_windows_server:,}")
    esu_label = f"{inv.pcores_with_windows_esu:,}" + (" ⚠" if inv.esu_count_may_be_understated else "")
    e2.metric("ESU pCores",       esu_label)

    # SQL block
    sql = result.sql_summary
    src_badge = "🔍 detected" if sql["source"] == "application" else "📐 estimated (10% default)"
    e3.metric("SQL Server pCores", f"{sql['pcores']:,}", delta=f"{sql['detected']} VMs — {src_badge}", delta_color="off")
    e4.metric("SQL ESU pCores",   f"{sql['esu_pcores']:,}")

    if sql["prod_assumed"]:
        prod_label  = f"{sql['prod']} (all prod, assumed)"
        prod_delta  = "no Environment tags — all assumed Production"
    else:
        prod_label  = f"{sql['prod']} Prod / {sql['nonprod']} Non-Prod"
        prod_delta  = "from Environment tags"
    e5.metric("SQL Prod / Non-Prod", prod_label, delta=prod_delta, delta_color="off")

    if inv.esu_count_may_be_understated:
        st.warning(
            f"**ESU undercount likely** — {inv.windows_vms_unknown_version:,} Windows Server VMs "
            f"have no detectable OS version string (typically pre-2016). "
            f"ESU pCore count may be understated. Override in Step 3 · Benchmarks if you have a separate OS audit."
        )

    if sql["prod_assumed"]:
        st.info(
            f"🟡 **Production assumed** — no Environment tags found in this inventory. "
            f"All {inv.pcores_with_windows_server:,} Windows Server pCores and "
            f"all {sql['detected']} SQL Server VMs are treated as **Production** for licensing cost purposes. "
            f"If this estate includes Dev/Test workloads, tag the VMs in RVTools (Environment = \"Dev\" / \"Test\") "
            f"and re-upload to recalculate with a split."
        )
    elif not sql["env_tagging"]:
        pass  # no Windows/SQL VMs so nothing to note

    if sql["source"] == "default":
        st.info(
            "SQL Server count estimated as 10% of Windows pCores (no Application attribute data found). "
            "If actual SQL licensing data is available, override in Step 3 · Benchmarks."
        )


def _rightsizing_card(result) -> None:
    """Right-sizing and Azure cost estimate."""
    inv = result.inventory
    plan = result.plan

    reduction_vcpu = (1 - plan.azure_vcpu / max(inv.total_vcpu_poweredon, 1)) * 100
    reduction_mem  = (1 - plan.azure_memory_gb / max(inv.total_vmemory_gb_poweredon, 1)) * 100

    st.markdown("#### ☁️ Azure Right-Sizing")
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("On-Prem vCPUs (powered-on)", f"{inv.total_vcpu_poweredon:,}")
    r2.metric("Right-Sized Azure vCPUs",
              f"{plan.azure_vcpu:,}",
              delta=f"-{reduction_vcpu:.0f}% right-sized")
    r3.metric("On-Prem Memory (powered-on)", f"{inv.total_vmemory_gb_poweredon:,.0f} GB")
    r4.metric("Right-Sized Azure Memory",
              f"{plan.azure_memory_gb:,.0f} GB",
              delta=f"{reduction_mem:+.0f}%")
    r5.metric("Azure Storage (prov.)",      f"{plan.azure_storage_gb:,.0f} GB")

    util_note = ""
    if inv.cpu_util_p95 > 0:
        util_note = f"CPU P95 utilisation: {inv.cpu_util_p95:.0%}"
    if inv.memory_util_p95 > 0:
        util_note += f"  |  Memory P95: {inv.memory_util_p95:.0%}"
    if util_note:
        st.caption(util_note + "  (right-sizing based on actual telemetry from vCPU/vMemory tabs)")
    else:
        st.caption("Telemetry unavailable — right-sizing used benchmark fallback reduction factors.")

    st.markdown("#### 💰 Azure Cost Estimate (Y10 run-rate, PAYG list price)")
    m1, m2, m3, m4 = st.columns(4)
    total_az = plan.annual_compute_consumption_lc_y10 + plan.annual_storage_consumption_lc_y10
    m1.metric("Compute/yr",  _fmt(plan.annual_compute_consumption_lc_y10))
    m2.metric("Storage/yr",  _fmt(plan.annual_storage_consumption_lc_y10))
    m3.metric("Total Azure/yr", _fmt(total_az))
    m4.metric("Pricing source", f"{result.region} / {result.pricing.source}")
    st.caption(
        f"Reference SKU: {result.pricing.vm_sku} at "
        f"{result.pricing.price_per_vcpu_hour_display}  |  "
        f"Disk: {result.pricing.disk_sku} at {result.pricing.price_per_gb_month_display}"
    )


def _results_kpi_preview(result) -> None:
    """Run engine and show headline KPIs."""
    from engine import status_quo, retained_costs, depreciation, financial_case, outputs
    inputs  = result.inputs
    bm      = st.session_state.get("benchmarks", BenchmarkConfig.from_yaml())

    with st.spinner("Calculating business case…"):
        sq      = status_quo.compute(inputs, bm)
        depr    = depreciation.compute(inputs, bm)
        ret     = retained_costs.compute(inputs, bm, sq)
        fc      = financial_case.compute(inputs, bm, sq, ret, depr)
        summary = outputs.compute(inputs, bm, fc)

    pb_str = f"{summary.payback_years:.1f} yrs" if summary.payback_years else "N/A"
    st.markdown("#### 📊 Business Case Preview")
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("CF NPV (10-Yr)",    _fmt(summary.npv_cf_10yr))
    k2.metric("CF NPV (5-Yr)",     _fmt(summary.npv_cf_5yr))
    k3.metric("P&L NPV (10-Yr)",   _fmt(summary.npv_10yr))
    k4.metric("10-Year ROI",        f"{summary.roi_10yr:.0%}")
    k5.metric("Payback",            pb_str)
    k6.metric("Yr-10 Savings",      _fmt(summary.savings_yr10))

    v1, v2, v3, _ = st.columns([1, 1, 1, 3])
    v1.metric("On-Prem Cost/VM/yr", _fmt(summary.on_prem_cost_per_vm_yr))
    v2.metric("Azure Cost/VM/yr",   _fmt(summary.azure_cost_per_vm_yr))
    v3.metric("Savings/VM/yr",      _fmt(summary.savings_per_vm_yr))

    # Quick overview chart — 5-Year
    years5 = list(range(1, 6))
    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Retained CAPEX",    x=years5, y=summary.az_cf_capex_by_year[1:6],  marker_color="#4A4A6A"))
    fig.add_trace(go.Bar(
        name="Retained OPEX",     x=years5, y=summary.az_cf_opex_by_year[1:6],   marker_color="#C76C00"))
    fig.add_trace(go.Bar(
        name="Azure Consumption", x=years5, y=summary.az_cf_azure_by_year[1:6],  marker_color="#50B0F0"))
    mig = [v if v != 0 else None for v in summary.az_cf_migration_by_year[1:6]]
    fig.add_trace(go.Bar(
        name="Migration",         x=years5, y=mig,                               marker_color="#FFC107"))
    fig.add_trace(go.Scatter(
        name="On-Prem (SQ)", x=years5, y=summary.sq_cf_by_year[1:6],
        mode="lines+markers", line=dict(color="#FF6B35", width=3), marker=dict(size=8)))
    fig.update_layout(
        barmode="stack", title="5-Year Cost Comparison",
        xaxis=dict(title="Year", tickmode="linear", tick0=1, dtick=1),
        yaxis=dict(title="USD", tickformat="$,.0f"),
        legend=dict(orientation="h"),
        height=320,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=40, b=20),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Store summary in session state for Results page to re-use
    st.session_state["_agent_summary"] = summary


# ─────────────────────────────────────────────────────────────────────────────
# Main render
# ─────────────────────────────────────────────────────────────────────────────

def render() -> None:
    st.title("⚡ Agent Intake — Automated Business Case")
    st.caption(
        "Upload an RVTools export (.xlsx). The engine automatically parses your inventory, "
        "infers the Azure region, fetches live PAYG pricing, right-sizes to Azure, "
        "and builds the full business case — no manual number entry required."
    )

    bm = st.session_state.get("benchmarks", BenchmarkConfig.from_yaml())

    # ── STAGE 0: Input form ───────────────────────────────────────────────────
    with st.container():
        col_name, col_cur, col_space = st.columns([2, 1, 3])
        client_name = col_name.text_input(
            "Customer Name *",
            value=st.session_state.get("_agent_client_name", ""),
            placeholder="e.g. Contoso Corp",
        )
        currency = col_cur.selectbox(
            "Currency",
            _CURRENCIES,
            index=_CURRENCIES.index(st.session_state.get("_agent_currency", "USD")),
        )

        sensitivity_confirmed = st.checkbox(
            "✅ I confirm this RVTools export is classified **General** sensitivity or lower "
            "and does not contain Confidential, Restricted, or higher-sensitivity data.",
            key="_sensitivity_cb",
        )

        if sensitivity_confirmed:
            uploaded = st.file_uploader(
                "Upload RVTools Export (.xlsx) *",
                type=["xlsx"],
                help="Export from RVTools: File → Export → All to xlsx",
            )
        else:
            uploaded = None
            st.caption("☑️ Check the box above to enable file upload.")

    # ── OPTIONAL PARAMETERS (always visible, collapsed by default) ────────────
    with st.expander("⚙️ Optional Parameters", expanded=False):
        st.caption("Leave defaults for a quick first pass. Adjust here for more specific scenarios.")
        opt_c1, opt_c2 = st.columns(2)

        ramp_preset = opt_c1.selectbox(
            "Migration Horizon",
            _RAMP_OPTIONS,
            index=_RAMP_OPTIONS.index("Extended (100% by Y3)"),
            help="How quickly the estate migrates to Azure.",
        )
        dc_exit = opt_c2.number_input(
            "Datacenters to Exit",
            min_value=0, max_value=10,
            value=0,
            help="Number of on-prem DCs the customer plans to close after migration.",
        )

        st.markdown("**Microsoft Funding Credits (optional)**")
        st.caption(
            "ACO = Azure Consumption Offer (applied as reduction to migration costs). "
            "ECIF = Eligible Credit Investment Fund. Enter total amounts by year — leave 0 if not applicable."
        )
        fc1, fc2, fc3, fc4, fc5 = st.columns(5)
        aco_y1  = fc1.number_input("ACO Year 1",  min_value=0.0, value=0.0, step=10_000.0, format="%.0f")
        aco_y2  = fc2.number_input("ACO Year 2",  min_value=0.0, value=0.0, step=10_000.0, format="%.0f")
        aco_y3  = fc3.number_input("ACO Year 3",  min_value=0.0, value=0.0, step=10_000.0, format="%.0f")
        ecif_y1 = fc4.number_input("ECIF Year 1", min_value=0.0, value=0.0, step=10_000.0, format="%.0f")
        ecif_y2 = fc5.number_input("ECIF Year 2", min_value=0.0, value=0.0, step=10_000.0, format="%.0f")

    # ── SUBMIT BUTTON ─────────────────────────────────────────────────────────
    btn_label = "🔄 Re-analyse" if "_agent_result" in st.session_state else "⚡ Build Business Case"
    submit_clicked = st.button(btn_label, type="primary", use_container_width=True)

    if submit_clicked:
        # Validate only on submit — never show errors on first page load
        _errors: list[str] = []
        if not client_name.strip():
            st.error("⚠️ Customer name is required.")
            _errors.append("name")
        if not sensitivity_confirmed:
            st.error("⚠️ Please confirm the file sensitivity classification above before proceeding.")
            _errors.append("sensitivity")
        elif uploaded is None:
            st.error("⚠️ Please upload an RVTools .xlsx export.")
            _errors.append("file")

        if not _errors:
            for key in ["_agent_result", "inputs", "_agent_summary"]:
                st.session_state.pop(key, None)

            st.session_state["_agent_client_name"] = client_name.strip()
            st.session_state["_agent_currency"]     = currency

            file_bytes = uploaded.read()
            _pipeline_ok = False

            with st.status("⚡ Building your business case…", expanded=True) as _status:
                st.write("🔍 Step 1 — Validating file format…")

                if not zipfile.is_zipfile(io.BytesIO(file_bytes)):
                    _status.update(label="❌ File could not be read", state="error", expanded=True)
                    st.error(
                        "🔒 This file cannot be read — it appears to be **encrypted or "
                        "password-protected**.  \n"
                        "Encrypted files are often protected by a sensitivity label **above General**.  \n\n"
                        "To fix: open the file in Excel → **File → Info → Protect Workbook → "
                        "Encrypt with Password** → remove the password → save → re-upload."
                    )
                else:
                    st.write(
                        "📊 Step 2 — Parsing inventory · inferring Azure region · "
                        "fetching live pricing · right-sizing · building financial model…"
                    )
                    try:
                        from engine.rvtools_to_inputs import build_business_case_from_bytes
                        aco  = [aco_y1, aco_y2, aco_y3,  0, 0, 0, 0, 0, 0, 0]
                        ecif = [ecif_y1, ecif_y2, 0,      0, 0, 0, 0, 0, 0, 0]
                        result = build_business_case_from_bytes(
                            file_bytes=file_bytes,
                            client_name=client_name.strip(),
                            currency=currency,
                            ramp_preset=ramp_preset,
                            aco_by_year=aco,
                            ecif_by_year=ecif,
                            benchmarks=bm,
                            num_datacenters_to_exit=int(dc_exit),
                        )
                        st.write(
                            f"✔ {result.inventory.num_vms:,} VMs parsed · "
                            f"region: {result.region} · pricing: {result.pricing.source}"
                        )
                        st.session_state["_agent_result"] = result
                        st.session_state["inputs"]        = result.inputs
                        st.session_state["benchmarks"]    = bm
                        _pipeline_ok = True
                        _status.update(
                            label=(
                                f"✅ Business case ready — "
                                f"{result.inventory.num_vms:,} VMs · {result.region}"
                            ),
                            state="complete",
                            expanded=False,
                        )
                    except Exception as exc:
                        try:
                            from openpyxl.utils.exceptions import InvalidFileException as _IFE
                        except ImportError:
                            _IFE = None
                        _status.update(label="❌ Pipeline failed", state="error", expanded=True)
                        if isinstance(exc, zipfile.BadZipFile) or (_IFE and isinstance(exc, _IFE)):
                            st.error(
                                "🔒 Could not open the file — it may be **encrypted or corrupted**. "
                                "Save an unprotected copy and re-upload."
                            )
                        else:
                            st.error(f"Pipeline failed: {exc}")
                            raise

            if _pipeline_ok:
                st.rerun()

    # ── RESULTS DISPLAY (if result already in session) ────────────────────────
    if "_agent_result" not in st.session_state:
        return

    result = st.session_state["_agent_result"]

    st.divider()
    st.success(
        f"✅ Business case compiled for **{result.inputs.engagement.client_name}**  |  "
        f"{result.inventory.num_vms:,} VMs  |  region: **{result.region}**  |  "
        f"pricing: **{result.pricing.source}**"
    )

    # Warnings
    if result.warnings:
        with st.expander(f"⚠️ {len(result.warnings)} parser warning(s)", expanded=False):
            for w in result.warnings:
                st.warning(w)

    st.divider()
    _inv_summary_card(result)
    st.divider()
    _rightsizing_card(result)
    st.divider()
    _results_kpi_preview(result)

    st.divider()
    st.info(
        "**Business case is ready.** Click **'4 · Results'** in the sidebar for the full "
        "interactive analysis, charts, and fact-check.  "
        "Click **'5 · Export'** to download the PowerPoint deck or pre-filled Excel workbook.  \n"
        "To override any benchmark assumption, use **'3 · Benchmarks'** and then re-run this page."
    )
