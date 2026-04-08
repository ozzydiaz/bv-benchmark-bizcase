"""
Page 0 — Agent Intake (Layered Wizard)

Three-layer checkpoint workflow with per-layer review gates and override options:

  Layer 1 · Inventory   — parse RVTools, infer region, fetch live pricing
  Layer 2 · Rightsizing — per-VM SKU matching, validation checkpoint
  Layer 3 · Financial   — business case computation, scenario comparison

Each layer shows full results before the user explicitly approves and advances.
Override panels allow re-running any layer with different parameters without
losing the other layers' outputs.
"""
from __future__ import annotations

import io
import tempfile
import zipfile

import streamlit as st
import plotly.graph_objects as go

from engine.models import BenchmarkConfig, MIGRATION_RAMP_PRESETS

# ─── Constants ────────────────────────────────────────────────────────────────

_CURRENCIES   = ["USD", "GBP", "EUR", "CAD", "AUD", "JPY", "INR", "BRL", "MXN", "SGD"]
_RAMP_OPTIONS = list(MIGRATION_RAMP_PRESETS.keys())

_PRICING_SRC_LABEL = {
    "api":       "Azure Retail Prices API (live)",
    "cache":     "Azure Retail Prices API (cached)",
    "benchmark": "Benchmark default (offline)",
}

_METRIC_CSS = """
<style>
[data-testid="stMetricValue"]  { font-size: 1.05rem !important; }
[data-testid="stMetricLabel"]  { font-size: 0.72rem !important; }
[data-testid="stMetricDelta"]  { font-size: 0.68rem !important; }
</style>
"""

# ─── Session-state helpers ────────────────────────────────────────────────────

def _step() -> int:
    """0=upload, 1=L1 active, 2=L2 active, 3=L3 active, 4=export."""
    return st.session_state.get("_wiz_step", 0)

def _set_step(n: int) -> None:
    st.session_state["_wiz_step"] = n

def _clear_from(layer: int) -> None:
    keys = {
        1: ["_l1_result", "_l2_result", "_l3_result", "_l3_scenarios", "inputs", "_agent_summary"],
        2: [               "_l2_result", "_l3_result", "_l3_scenarios", "inputs", "_agent_summary"],
        3: [                             "_l3_result", "_l3_scenarios",           "_agent_summary"],
    }
    for k in keys.get(max(1, layer), []):
        st.session_state.pop(k, None)

def _sym(currency: str = "USD") -> str:
    return {"GBP": "£", "EUR": "€"}.get(currency, "$")

def _fmt(v: float, currency: str = "USD") -> str:
    return f"{_sym(currency)}{v:,.0f}"

# ─── Step indicator ───────────────────────────────────────────────────────────

def _step_bar() -> None:
    step = _step()
    labels = ["Upload", "Inventory", "Rightsizing", "Financial", "Export"]
    badges = []
    for i, lbl in enumerate(labels):
        if i < step:
            b = f'<span style="color:#22CC88;font-weight:bold">✅ {lbl}</span>'
        elif i == step:
            b = f'<span style="color:#50B0F0;font-weight:bold">🔵 {lbl}</span>'
        else:
            b = f'<span style="color:#666">○ {lbl}</span>'
        badges.append(b)
        if i < len(labels) - 1:
            badges.append('<span style="color:#444"> → </span>')
    st.markdown(
        '<div style="font-size:0.9rem;padding:6px 0 12px">' + "".join(badges) + "</div>",
        unsafe_allow_html=True,
    )

# ─── Approved-layer compact banners ──────────────────────────────────────────

def _l1_banner() -> None:
    l1 = st.session_state.get("_l1_result", {})
    inv = l1.get("inv")
    if not inv:
        return
    text = (
        f"{inv.num_vms:,} VMs · {inv.num_hosts} hosts · "
        f"{l1.get('region', '?')} · {l1.get('client_name', '')}"
    )
    c1, c2 = st.columns([8, 1])
    c1.success(f"✅ **Layer 1 — Inventory** — {text}")
    if c2.button("← Revise", key="_revise_l1"):
        _clear_from(2)
        _set_step(1)
        st.rerun()

def _l2_banner() -> None:
    l2 = st.session_state.get("_l2_result", {})
    cp = l2.get("cp")
    if not cp:
        return
    l1  = st.session_state.get("_l1_result", {})
    inv = l1.get("inv")
    red = ""
    if inv and inv.total_vcpu_poweredon:
        pct = (1 - cp.azure_vcpu / inv.total_vcpu_poweredon) * 100
        red = f" · {pct:+.0f}% vCPU"
    text = (
        f"{cp.azure_vcpu:,} Azure vCPUs · {cp.azure_memory_gb:,.0f} GB RAM · "
        f"{cp.azure_storage_gb:,.0f} GB storage{red}"
    )
    c1, c2 = st.columns([8, 1])
    c1.success(f"✅ **Layer 2 — Rightsizing** — {text}")
    if c2.button("← Revise", key="_revise_l2"):
        _clear_from(3)
        _set_step(2)
        st.rerun()

def _l3_banner() -> None:
    l3 = st.session_state.get("_l3_result", {})
    summary = l3.get("summary")
    if not summary:
        return
    l1  = st.session_state.get("_l1_result", {})
    cur = l1.get("currency", "USD")
    pb  = f"{summary.payback_cf:.1f} yrs" if summary.payback_cf else ">5 yrs"
    text = (
        f"5-Yr CF ROI: {summary.roi_cf:.0%} · Payback: {pb} · "
        f"CF NPV (5Y): {_fmt(summary.npv_cf_5yr, cur)}"
    )
    c1, c2 = st.columns([8, 1])
    c1.success(f"✅ **Layer 3 — Financial Model** — {text}")
    if c2.button("← Revise", key="_revise_l3"):
        _clear_from(3)
        _set_step(3)
        st.rerun()

# ─── Layer 1 — Inventory ─────────────────────────────────────────────────────

def _run_layer1(
    file_bytes:      bytes,
    client_name:     str,
    currency:        str,
    region_override: str,
) -> dict | None:
    from engine.rvtools_parser import parse as _parse
    from engine.region_guesser import guess as _guess_region
    from engine.azure_sku_matcher import get_pricing as _get_pricing, get_vm_catalog as _get_vm_catalog

    if not zipfile.is_zipfile(io.BytesIO(file_bytes)):
        st.error(
            "🔒 File appears encrypted or corrupted.  \n"
            "Open in Excel → File → Info → Protect Workbook → remove password → save → re-upload."
        )
        return None

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        with st.status("🔍 Layer 1 — Parsing inventory…", expanded=True) as _s:
            st.write("Parsing RVTools export…")
            inv = _parse(tmp_path)
            st.write(f"✔  {inv.num_vms:,} VMs · {inv.num_hosts} hosts")

            st.write("Inferring Azure region…")
            # Fleet-level region: used for display and as the fallback for any VM
            # whose host had no geographic signal.
            region = region_override.strip() if region_override.strip() else _guess_region(inv)

            if region_override.strip():
                # User forced a region — override every per-VM region assignment
                # (the parser stamped them using vHost signals; a manual override
                # means the user explicitly wants all VMs priced against one region).
                for vm in inv.vm_records:
                    vm.azure_region = region
                    vm.azure_region_source = "override"
                st.write(f"✔  Region (override): {region} — applied to all {len(inv.vm_records):,} VMs")
            else:
                distinct_regions = sorted({vm.azure_region for vm in inv.vm_records if vm.azure_region})
                if len(distinct_regions) > 1:
                    st.write(
                        f"✔  Per-VM regions inferred: "
                        + ", ".join(distinct_regions)
                        + f"  (fleet default: {region})"
                    )
                else:
                    st.write(f"✔  Region: {region}")

            st.write(f"Fetching Azure pricing and VM catalog for fleet region {region}…")
            pricing    = _get_pricing(region=region)
            vm_catalog = _get_vm_catalog(region=region)
            st.write(f"✔  {_PRICING_SRC_LABEL.get(pricing.source, pricing.source)}")

            distinct_regions = sorted({vm.azure_region for vm in inv.vm_records if vm.azure_region})
            region_label = (
                f"{len(distinct_regions)} regions"
                if len(distinct_regions) > 1 else region
            )
            _s.update(
                label=f"✅ Inventory ready — {inv.num_vms:,} VMs · {region_label}",
                state="complete", expanded=False,
            )
    except Exception as exc:
        st.error(f"Layer 1 failed: {exc}")
        return None

    return {
        "inv":         inv,
        "region":      region,
        "pricing":     pricing,
        "vm_catalog":  vm_catalog,
        "client_name": client_name,
        "currency":    currency,
        "tmp_path":    tmp_path,
    }

def _show_layer1() -> None:
    l1  = st.session_state["_l1_result"]
    inv = l1["inv"]

    st.markdown("##### Fleet Overview")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("VMs (TCO scope)",  f"{inv.num_vms:,}")
    c2.metric("Powered-On VMs",   f"{inv.num_vms_poweredon:,}")
    c3.metric("ESX Hosts",        f"{inv.num_hosts:,}")
    c4.metric("Total vCPUs",      f"{inv.total_vcpu:,}")
    c5.metric("Total Memory",     f"{inv.total_vmemory_gb:,.0f} GB")

    distinct_vm_regions = sorted({vm.azure_region for vm in inv.vm_records if vm.azure_region})
    region_display = (
        f"{len(distinct_vm_regions)} regions"
        if len(distinct_vm_regions) > 1
        else (distinct_vm_regions[0] if distinct_vm_regions else l1["region"])
    )
    d1, d2, d3, d4, d5 = st.columns(5)
    d1.metric("Storage (prov.)",  f"{inv.total_disk_provisioned_gb:,.0f} GB")
    d2.metric("vCPU/pCore ratio", f"{inv.vcpu_per_core_ratio:.2f}×")
    d3.metric("Azure Region(s)",  region_display)
    d4.metric("Pricing",          _PRICING_SRC_LABEL.get(l1["pricing"].source, l1["pricing"].source))
    d5.metric("Parse Warnings",   f"{len(inv.parse_warnings)}" if inv.parse_warnings else "None")

    # Show per-region VM breakdown if the merged file spans multiple geographies
    if len(distinct_vm_regions) > 1:
        from collections import Counter
        region_counts = Counter(vm.azure_region for vm in inv.vm_records if vm.azure_region)
        fallback_count = sum(1 for vm in inv.vm_records if vm.azure_region_source == "fallback")
        with st.expander(
            f"🌍 Multi-region detected — {len(distinct_vm_regions)} Azure regions across "
            f"{len(inv.vm_records):,} powered-on VMs"
        ):
            import pandas as pd
            # Source breakdown per region
            source_by_region: dict[str, Counter] = {}
            for vm in inv.vm_records:
                if vm.azure_region:
                    source_by_region.setdefault(vm.azure_region, Counter())[vm.azure_region_source] += 1
            rows = []
            for r in sorted(distinct_vm_regions, key=lambda r: -region_counts[r]):
                src_counts = source_by_region.get(r, Counter())
                signal_parts = []
                for src, label in [("tld", "TLD"), ("dc_keyword", "DC keyword"), ("gmt", "GMT offset"), ("fallback", "no signal ⚠"), ("override", "override")]:
                    if src_counts.get(src):
                        signal_parts.append(f"{src_counts[src]} {label}")
                rows.append({
                    "Azure Region": r,
                    "VM Count": region_counts[r],
                    "% of Fleet": f"{region_counts[r]/len(inv.vm_records):.0%}",
                    "Signal breakdown": ", ".join(signal_parts),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            if fallback_count:
                st.warning(
                    f"**{fallback_count} VM(s) have no geographic signal** — host had no recognisable "
                    f"TLD, datacenter name, or GMT offset. Priced against the fallback region "
                    f"(`{distinct_vm_regions[0] if fallback_count == len(inv.vm_records) else 'eastus2'}`). "
                    f"Use the region override below to force a specific region if needed."
                )
            st.caption(
                "Each VM is priced against its own Azure region's live PAYG rates in Layer 2. "
                "Use the override below to force a single region if needed."
            )
    elif inv.vm_records:
        # Single region — check if it's all fallbacks (no signal in the whole fleet)
        fallback_count = sum(1 for vm in inv.vm_records if vm.azure_region_source == "fallback")
        if fallback_count == len(inv.vm_records):
            st.warning(
                f"**No geographic signal detected** — {fallback_count} VM(s) had no recognisable "
                f"TLD, datacenter name, or GMT offset in vHost data. "
                f"Defaulting to `{distinct_vm_regions[0] if distinct_vm_regions else 'eastus2'}`. "
                f"Use the region override below to set the correct Azure region."
            )

    st.markdown("##### License Profile")
    e1, e2, e3, e4, e5 = st.columns(5)
    e1.metric("Windows pCores",   f"{inv.pcores_with_windows_server:,}")
    esu_lbl = f"{inv.pcores_with_windows_esu:,}" + (" ⚠" if inv.esu_count_may_be_understated else "")
    e2.metric("ESU pCores",       esu_lbl)
    src_badge = "detected" if inv.sql_detection_source == "application" else "estimated (10%)"
    e3.metric(
        "SQL pCores",
        f"{inv.pcores_with_sql_server:,}",
        delta=f"{inv.sql_vms_detected} VMs — {src_badge}",
        delta_color="off",
    )
    e4.metric("SQL ESU pCores",   f"{inv.pcores_with_sql_esu:,}")
    prod_lbl = (
        f"{inv.sql_vms_prod} Prod / {inv.sql_vms_nonprod} Non-Prod"
        if not inv.sql_prod_assumed
        else f"{inv.sql_vms_prod} (assumed prod)"
    )
    e5.metric("SQL Prod split", prod_lbl)

    if inv.esu_count_may_be_understated:
        st.warning(
            f"**ESU undercount likely** — {inv.windows_vms_unknown_version:,} Windows VMs "
            "have no detectable OS version string. ESU pCore count may be understated."
        )
    if inv.sql_prod_assumed:
        st.info(
            f"🟡 **SQL Production assumed** — no Environment tags found. "
            f"All {inv.sql_vms_detected} SQL VMs treated as Production for licensing costs."
        )
    if inv.parse_warnings:
        with st.expander(f"⚠️ {len(inv.parse_warnings)} parser warning(s)"):
            for w in inv.parse_warnings:
                st.warning(w)

def _render_l1_override() -> None:
    l1 = st.session_state["_l1_result"]
    with st.expander("⚙️ Override inventory parameters & re-run", expanded=False):
        c1, c2, c3 = st.columns(3)
        new_region   = c1.text_input(
            "Force Azure region",
            value=l1["region"],
            help="E.g. 'uksouth', 'eastus', 'australiaeast'",
            key="_l1ov_region",
        )
        new_name     = c2.text_input("Client name", value=l1["client_name"], key="_l1ov_name")
        new_currency = c3.selectbox(
            "Currency", _CURRENCIES,
            index=_CURRENCIES.index(l1.get("currency", "USD")),
            key="_l1ov_currency",
        )
        if st.button("↺ Re-run Inventory with these settings", key="_rerun_l1"):
            file_bytes = st.session_state.get("_wiz_file_bytes")
            if not file_bytes:
                st.error("File not in session — use 'Start over' to re-upload.")
                return
            result = _run_layer1(file_bytes, new_name.strip(), new_currency, new_region)
            if result:
                _clear_from(2)
                st.session_state["_l1_result"] = result
                _set_step(1)
                st.rerun()

# ─── Layer 2 — Rightsizing ────────────────────────────────────────────────────

def _run_layer2(
    ramp_preset:  str,
    storage_mode: str,
    bm:           BenchmarkConfig,
) -> dict | None:
    from engine.consumption_builder import build_with_validation
    from engine.rvtools_to_inputs import workload_inventory_from_rvtools

    l1 = st.session_state.get("_l1_result")
    if not l1:
        st.error("Layer 1 result missing — complete inventory step first.")
        return None

    inv         = l1["inv"]
    pricing     = l1["pricing"]
    region      = l1["region"]
    client_name = l1["client_name"]

    try:
        with st.status("⚙️ Layer 2 — Per-VM rightsizing…", expanded=True) as _s:
            st.write(f"Rightsizing {len(inv.vm_records):,} powered-on VMs…")
            cp, rv = build_with_validation(
                inv=inv,
                pricing=pricing,
                benchmarks=bm,
                workload_name=client_name,
                storage_mode=storage_mode,
                ramp_preset=ramp_preset,
                vm_catalog=l1.get("vm_catalog"),
            )
            wl = workload_inventory_from_rvtools(inv, region=region, workload_name=client_name)
            _s.update(
                label=(
                    f"✅ Rightsizing — {cp.azure_vcpu:,} vCPUs · "
                    f"{cp.azure_memory_gb:,.0f} GB RAM · {cp.azure_storage_gb:,.0f} GB storage"
                ),
                state="complete", expanded=False,
            )
    except Exception as exc:
        st.error(f"Layer 2 failed: {exc}")
        return None

    return {
        "cp":           cp,
        "rv":           rv,
        "wl":           wl,
        "ramp_preset":  ramp_preset,
        "storage_mode": storage_mode,
        "bm":           bm,
    }

def _show_layer2() -> None:
    l1  = st.session_state["_l1_result"]
    l2  = st.session_state["_l2_result"]
    inv = l1["inv"]
    cp  = l2["cp"]
    rv  = l2.get("rv")

    reduction_vcpu = (1 - cp.azure_vcpu / max(inv.total_vcpu_poweredon, 1)) * 100
    reduction_mem  = (1 - cp.azure_memory_gb / max(inv.total_vmemory_gb_poweredon, 1)) * 100

    st.markdown("##### Right-Sized Azure Footprint")
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("On-Prem vCPUs",  f"{inv.total_vcpu_poweredon:,}")
    r2.metric("Azure vCPUs",    f"{cp.azure_vcpu:,}",              delta=f"{reduction_vcpu:+.0f}%")
    r3.metric("On-Prem Memory", f"{inv.total_vmemory_gb_poweredon:,.0f} GB")
    r4.metric("Azure Memory",   f"{cp.azure_memory_gb:,.0f} GB",  delta=f"{reduction_mem:+.0f}%")
    r5.metric("Azure Storage",  f"{cp.azure_storage_gb:,.0f} GB")

    if rv:
        parts = []
        if rv.telemetry_vm_count:  parts.append(f"{rv.telemetry_vm_count} VM telemetry")
        if rv.host_proxy_vm_count: parts.append(f"{rv.host_proxy_vm_count} host proxy")
        if rv.fallback_vm_count:   parts.append(f"{rv.fallback_vm_count} fallback factors")
        sig_str = " + ".join(parts) if parts else "fallback only"
        bm = l2["bm"]
        tol_pct = int(bm.sku_match_secondary_tolerance * 100)
        tol_label = f"SKU tolerance: {tol_pct}% (strict)" if tol_pct == 0 else f"SKU tolerance: {tol_pct}%"
        st.caption(
            f"Signal: {sig_str}  |  {rv.telemetry_coverage_pct:.0%} telemetry coverage  |  "
            f"storage mode: {l2['storage_mode']}  |  horizon: {l2['ramp_preset']}  |  {tol_label}"
        )
        for w in rv.warnings:
            st.warning(w)
        if rv.anomaly_vm_count > 0:
            with st.expander(
                f"🔍 {rv.anomaly_vm_count} SKU anomal{'y' if rv.anomaly_vm_count == 1 else 'ies'} "
                "(matched SKU > 2× source vCPU — inspect before approving)"
            ):
                import pandas as pd
                st.dataframe(pd.DataFrame(rv.anomaly_vms[:20]), use_container_width=True)

    st.markdown("##### Azure Cost Estimate (PAYG list price, Y10 run-rate)")
    m1, m2, m3, m4 = st.columns(4)
    total_az = cp.annual_compute_consumption_lc_y10 + cp.annual_storage_consumption_lc_y10
    m1.metric("Compute/yr",     f"${cp.annual_compute_consumption_lc_y10:,.0f}")
    m2.metric("Storage/yr",     f"${cp.annual_storage_consumption_lc_y10:,.0f}")
    m3.metric("Total Azure/yr", f"${total_az:,.0f}")
    src = l1["pricing"].source
    distinct_vm_regions = sorted({vm.azure_region for vm in inv.vm_records if vm.azure_region})
    region_label = (
        f"{len(distinct_vm_regions)} regions (per-VM)"
        if len(distinct_vm_regions) > 1
        else l1["region"]
    )
    m4.metric("Pricing source", region_label, delta=_PRICING_SRC_LABEL.get(src, src), delta_color="off")

    if src in ("api", "cache"):
        st.info(
            "**Pricing:** [Azure Retail Prices API](https://prices.azure.com/api/retail/prices) — "
            "live PAYG list rates, never hard-coded.",
            icon="🔗",
        )
    else:
        st.warning("Offline mode — benchmark default rates. Reconnect to fetch live prices.", icon="⚠️")

def _render_l2_override() -> None:
    l2 = st.session_state["_l2_result"]
    bm = l2["bm"]
    with st.expander("⚙️ Override rightsizing parameters & re-run", expanded=False):
        c1, c2 = st.columns(2)
        new_ramp = c1.selectbox(
            "Migration horizon",
            _RAMP_OPTIONS,
            index=_RAMP_OPTIONS.index(l2["ramp_preset"]),
            key="_l2ov_ramp",
        )
        new_storage_sel = c2.radio(
            "Storage mode",
            ["Per-VM disk tiers (accurate)", "Fleet aggregate (fast)"],
            index=0 if l2["storage_mode"] == "per_vm" else 1,
            horizontal=True,
            key="_l2ov_storage",
        )

        st.markdown("**Headroom and fallback factors**")
        st.caption(
            "Headroom adds buffer on top of measured utilisation. "
            "Fallback factors apply only to VMs with no telemetry signal."
        )
        h1, h2, h3, h4 = st.columns(4)
        cpu_head = h1.slider(
            "CPU headroom %", 0, 50,
            int(bm.cpu_rightsizing_headroom_factor * 100), 5,
            key="_l2ov_cpu_head",
        ) / 100
        mem_head = h2.slider(
            "Mem headroom %", 0, 50,
            int(bm.memory_rightsizing_headroom_factor * 100), 5,
            key="_l2ov_mem_head",
        ) / 100
        cpu_fb = h3.slider(
            "CPU fallback %", 10, 80,
            int(bm.cpu_util_fallback_factor * 100), 5,
            key="_l2ov_cpu_fb",
        ) / 100
        mem_fb = h4.slider(
            "Mem fallback %", 10, 80,
            int(bm.mem_util_fallback_factor * 100), 5,
            key="_l2ov_mem_fb",
        ) / 100

        st.markdown("**SKU matching — secondary dimension tolerance**")
        st.caption(
            "Azure VM SKUs come in fixed vCPU/memory tiers. When a rightsized target falls "
            "between tiers, a strict match forces a snap-up on *both* dimensions simultaneously — "
            "inflating cost by jumping to a much larger SKU than needed. "
            "This tolerance allows the **secondary** dimension (the one that is *not* the "
            "bottleneck for a given VM) to be satisfied slightly below the rightsized target, "
            "landing on a cheaper tier:\n\n"
            "- **CPU-skewed VM** (high CPU, low memory — e.g. web/app servers): "
            "memory must be fully covered; CPU may be up to *tolerance* smaller. "
            "Avoids a CPU-tier snap-up at the cost of slightly over-provisioning memory.\n"
            "- **Memory-skewed VM** (high memory, low CPU — e.g. databases, caches): "
            "CPU must be fully covered; memory may be up to *tolerance* smaller. "
            "Avoids a memory-tier snap-up at the cost of slightly over-provisioning CPU.\n\n"
            "The engine always picks the **cheapest result** across the relaxed and strict passes — "
            "this can only reduce or hold cost, never inflate it. "
            "Default 20% matches the headroom already built into the target, "
            "so actual VM utilisation remains covered. Set to 0% to restore strict matching."
        )
        sku_tol = st.slider(
            "SKU match tolerance %", 0, 35,
            int(bm.sku_match_secondary_tolerance * 100), 5,
            key="_l2ov_sku_tol",
            help=(
                "How far below the rightsized target the secondary resource dimension "
                "(CPU for memory-skewed VMs; memory for CPU-skewed VMs) may be when "
                "searching for the least-cost Azure SKU. 0% = strict both-dimensions "
                "coverage (original behaviour). 20% = default."
            ),
        ) / 100

        if st.button("↺ Re-run Rightsizing with these settings", key="_rerun_l2"):
            bm_new = BenchmarkConfig(**{
                **bm.model_dump(),
                "cpu_rightsizing_headroom_factor":    cpu_head,
                "memory_rightsizing_headroom_factor": mem_head,
                "cpu_util_fallback_factor":           cpu_fb,
                "mem_util_fallback_factor":           mem_fb,
                "sku_match_secondary_tolerance":      sku_tol,
            })
            smode  = "per_vm" if "Per-VM" in new_storage_sel else "aggregate"
            result = _run_layer2(new_ramp, smode, bm_new)
            if result:
                _clear_from(3)
                st.session_state["_l2_result"] = result
                st.session_state["benchmarks"]  = bm_new
                st.rerun()

# ─── Layer 3 — Financial Model ────────────────────────────────────────────────

def _build_l3_inputs(
    aco_by_year:  list[float],
    ecif_by_year: list[float],
    num_dc_exit:  int,
):
    from engine.models import (
        BusinessCaseInputs, DatacenterConfig, EngagementInfo, HardwareLifecycle,
    )
    l1 = st.session_state["_l1_result"]
    l2 = st.session_state["_l2_result"]

    def _pad10(v: list[float]) -> list[float]:
        out = list(v)[:10]
        while len(out) < 10:
            out.append(0.0)
        return out

    cp = l2["cp"].model_copy(update={
        "aco_by_year":  _pad10(aco_by_year),
        "ecif_by_year": _pad10(ecif_by_year),
    })
    return BusinessCaseInputs(
        engagement=EngagementInfo(
            client_name=l1["client_name"],
            local_currency_name=l1["currency"],
            usd_to_local_rate=1.0,
        ),
        hardware=HardwareLifecycle(),
        datacenter=DatacenterConfig(num_datacenters_to_exit=num_dc_exit),
        workloads=[l2["wl"]],
        consumption_plans=[cp],
    )

def _run_layer3(
    aco_by_year:  list[float],
    ecif_by_year: list[float],
    num_dc_exit:  int,
    bm:           BenchmarkConfig,
    label:        str = "Base",
) -> dict | None:
    from engine import status_quo, retained_costs, depreciation, financial_case, outputs

    try:
        inputs = _build_l3_inputs(aco_by_year, ecif_by_year, num_dc_exit)
        with st.spinner(f"Computing financial model — {label}…"):
            sq      = status_quo.compute(inputs, bm)
            depr    = depreciation.compute(inputs, bm)
            ret     = retained_costs.compute(inputs, bm, sq)
            fc      = financial_case.compute(inputs, bm, sq, ret, depr)
            summary = outputs.compute(inputs, bm, fc)
    except Exception as exc:
        st.error(f"Layer 3 failed ({label}): {exc}")
        return None

    return {
        "inputs":      inputs,
        "fc":          fc,
        "summary":     summary,
        "bm":          bm,
        "aco":         aco_by_year,
        "ecif":        ecif_by_year,
        "num_dc_exit": num_dc_exit,
        "label":       label,
    }

def _show_layer3(horizon: int = 5) -> None:
    l3        = st.session_state["_l3_result"]
    scenarios = st.session_state.get("_l3_scenarios", [])
    all_sc    = [l3] + scenarios
    l1        = st.session_state["_l1_result"]
    cur       = l1.get("currency", "USD")
    fmt       = lambda v: _fmt(v, cur)  # noqa: E731

    if len(all_sc) > 1:
        st.markdown("##### Scenario Comparison")
        cols = st.columns(len(all_sc))
        for col, sc in zip(cols, all_sc):
            s   = sc["summary"]
            pb  = f"{s.payback_cf:.1f} yrs" if s.payback_cf else ">5 yrs"
            aco = sum(abs(x) for x in sc.get("aco", []))
            col.subheader(sc["label"])
            col.metric("5-Yr CF ROI",    f"{s.roi_cf:.0%}")
            col.metric("Payback (5Y)",   pb)
            col.metric("CF NPV (5-Yr)",  fmt(s.npv_cf_5yr))
            col.metric("CF NPV (10-Yr)", fmt(s.npv_cf_10yr))
            col.metric("Yr-10 Savings",  fmt(s.savings_yr10))
            if aco > 0:
                col.metric("ACO Credits", fmt(aco), delta="in base", delta_color="off")
    else:
        summary = l3["summary"]
        pb_str  = f"{summary.payback_cf:.1f} yrs" if summary.payback_cf else ">5 yrs"
        st.markdown("##### Business Case Summary")
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        npv_cf = summary.npv_cf_10yr if horizon >= 10 else summary.npv_cf_5yr
        npv_pl = summary.npv_10yr    if horizon >= 10 else summary.npv_5yr
        k1.metric(f"CF NPV ({horizon}Y)",  fmt(npv_cf))
        k2.metric(f"P&L NPV ({horizon}Y)", fmt(npv_pl))
        k3.metric("5-Yr CF ROI",           f"{summary.roi_cf:.0%}")
        k4.metric("Payback (5Y CF)",        pb_str)
        k5.metric("Yr-10 Savings",          fmt(summary.savings_yr10))
        k6.metric("SQ Cost/VM/yr",          fmt(summary.on_prem_cost_per_vm_yr))

        v1, v2, v3, _ = st.columns([1, 1, 1, 3])
        v1.metric("Azure Cost/VM/yr", fmt(summary.azure_cost_per_vm_yr))
        v2.metric("Savings/VM/yr",    fmt(summary.savings_per_vm_yr))

    _chart(l3, horizon, fmt)

def _chart(l3: dict, horizon: int, fmt) -> None:
    summary = l3["summary"]
    n       = horizon
    years   = list(range(1, n + 1))
    fig     = go.Figure()
    fig.add_trace(go.Bar(name="Retained CAPEX",    x=years, y=summary.az_cf_capex_by_year[1:n+1],  marker_color="#4A4A6A"))
    fig.add_trace(go.Bar(name="Retained OPEX",     x=years, y=summary.az_cf_opex_by_year[1:n+1],   marker_color="#C76C00"))
    fig.add_trace(go.Bar(name="Azure Consumption", x=years, y=summary.az_cf_azure_by_year[1:n+1],  marker_color="#50B0F0"))
    mig = [v if v != 0 else None for v in summary.az_cf_migration_by_year[1:n+1]]
    fig.add_trace(go.Bar(name="Migration",         x=years, y=mig,                                  marker_color="#FFC107"))
    fig.add_trace(go.Scatter(
        name="On-Prem (SQ)", x=years, y=summary.sq_cf_by_year[1:n+1],
        mode="lines+markers", line=dict(color="#FF6B35", width=3), marker=dict(size=8),
    ))
    cf_savings = summary.annual_cf_savings[1:n+1]
    avg = sum(cf_savings) / max(n, 1)
    fig.add_hline(
        y=avg, line_dash="dash", line_color="#22CC88",
        annotation_text=f"Avg annual saving: {fmt(avg)}",
        annotation_position="top right",
        annotation_font_size=11, annotation_font_color="#22CC88",
    )
    fig.update_layout(
        barmode="stack",
        title=f"{n}-Year Cost Comparison — {l3['label']}",
        xaxis=dict(title="Year", tickmode="linear", tick0=1, dtick=1),
        yaxis=dict(title=l3["inputs"].engagement.local_currency_name, tickformat="$,.0f"),
        legend=dict(
            orientation="v", x=1.02, xanchor="left", y=1.0, yanchor="top",
            font=dict(size=11), bgcolor="rgba(0,0,0,0)",
        ),
        height=360,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=50, b=20, r=170),
    )
    st.plotly_chart(fig, use_container_width=True)

def _render_l3_override() -> None:
    l3        = st.session_state["_l3_result"]
    scenarios = st.session_state.get("_l3_scenarios", [])
    bm        = l3["bm"]

    with st.expander(
        "⚙️ Override financial parameters · add comparison scenario",
        expanded=False,
    ):
        c1, c2, c3 = st.columns(3)
        dc_exit     = c1.number_input(
            "Datacenters to exit", min_value=0, max_value=10,
            value=l3["num_dc_exit"], key="_l3ov_dc",
        )
        wacc_pct    = c2.slider(
            "WACC (discount rate) %", 3, 15,
            int(bm.wacc * 100), 1, key="_l3ov_wacc",
        ) / 100
        horizon_sel = c3.radio(
            "Analysis horizon", ["5-Year", "10-Year"],
            index=0 if st.session_state.get("_agent_horizon", 5) < 10 else 1,
            horizontal=True, key="_l3ov_horizon",
        )
        st.session_state["_agent_horizon"] = 5 if "5" in horizon_sel else 10

        st.markdown("**Microsoft Funding Credits** *(positive = inflow to client)*")
        cr1, cr2, cr3, cr4, cr5 = st.columns(5)

        def _aco_disp(i: int) -> float:
            v = l3.get("aco", [])
            return abs(v[i]) if len(v) > i else 0.0

        def _ecif_disp(i: int) -> float:
            v = l3.get("ecif", [])
            return abs(v[i]) if len(v) > i else 0.0

        aco_y1  = cr1.number_input("ACO Yr 1",  min_value=0.0, value=_aco_disp(0),  step=10_000.0, format="%.0f", key="_l3ov_a1")
        aco_y2  = cr2.number_input("ACO Yr 2",  min_value=0.0, value=_aco_disp(1),  step=10_000.0, format="%.0f", key="_l3ov_a2")
        aco_y3  = cr3.number_input("ACO Yr 3",  min_value=0.0, value=_aco_disp(2),  step=10_000.0, format="%.0f", key="_l3ov_a3")
        ecif_y1 = cr4.number_input("ECIF Yr 1", min_value=0.0, value=_ecif_disp(0), step=10_000.0, format="%.0f", key="_l3ov_e1")
        ecif_y2 = cr5.number_input("ECIF Yr 2", min_value=0.0, value=_ecif_disp(1), step=10_000.0, format="%.0f", key="_l3ov_e2")

        aco  = [-aco_y1, -aco_y2, -aco_y3, 0, 0, 0, 0, 0, 0, 0]
        ecif = [-ecif_y1, -ecif_y2, 0, 0, 0, 0, 0, 0, 0, 0]

        scenario_name = st.text_input(
            "Scenario label",
            value=f"Scenario {len(scenarios) + 2:02d}",
            key="_l3ov_name",
        )

        sc1, sc2 = st.columns(2)
        with sc1:
            if st.button("↺ Update base scenario", key="_rerun_l3_base"):
                bm_new = BenchmarkConfig(**{**bm.model_dump(), "wacc": wacc_pct})
                result = _run_layer3(aco, ecif, int(dc_exit), bm_new, label="Base (updated)")
                if result:
                    st.session_state["_l3_result"]     = result
                    st.session_state["_l3_scenarios"]  = []
                    st.session_state["inputs"]         = result["inputs"]
                    st.session_state["_agent_summary"] = result["summary"]
                    st.rerun()
        with sc2:
            if st.button(f"➕ Add comparison: {scenario_name}", key="_add_scenario"):
                bm_new = BenchmarkConfig(**{**bm.model_dump(), "wacc": wacc_pct})
                result = _run_layer3(aco, ecif, int(dc_exit), bm_new, label=scenario_name)
                if result:
                    st.session_state["_l3_scenarios"] = scenarios + [result]
                    st.rerun()

        if scenarios:
            if st.button("🗑 Clear comparison scenarios", key="_clear_sc"):
                st.session_state["_l3_scenarios"] = []
                st.rerun()

# ─── Upload form (step 0) ─────────────────────────────────────────────────────

def _render_upload() -> None:
    c1, c2, _ = st.columns([2, 1, 3])
    client_name = c1.text_input(
        "Customer Name *",
        value=st.session_state.get("_agent_client_name", ""),
        placeholder="e.g. Contoso Corp",
    )
    currency = c2.selectbox(
        "Currency",
        _CURRENCIES,
        index=_CURRENCIES.index(st.session_state.get("_agent_currency", "USD")),
    )

    sensitivity_ok = st.checkbox(
        "✅ I confirm this RVTools export is classified **General** sensitivity or lower "
        "and does not contain Confidential, Restricted, or higher-sensitivity data.",
        key="_sensitivity_cb",
    )

    uploaded = None
    if sensitivity_ok:
        uploaded = st.file_uploader(
            "Upload RVTools Export (.xlsx) *",
            type=["xlsx"],
            help="RVTools: File → Export → All to xlsx",
        )
    else:
        st.caption("☑️ Confirm sensitivity classification above to enable file upload.")

    with st.expander("⚙️ Optional: force Azure region", expanded=False):
        region_override = st.text_input(
            "Override region",
            placeholder="e.g. uksouth — leave blank for auto-detection",
            key="_upload_region_override",
        )

    if st.button("⚡ Parse Inventory →", type="primary", use_container_width=True):
        if not client_name.strip():
            st.error("⚠️ Customer name is required.")
            return
        if not sensitivity_ok:
            st.error("⚠️ Confirm the sensitivity classification above.")
            return
        if uploaded is None:
            st.error("⚠️ Upload an RVTools .xlsx export.")
            return

        file_bytes = uploaded.read()
        _clear_from(1)
        st.session_state["_wiz_file_bytes"]    = file_bytes
        st.session_state["_agent_client_name"] = client_name.strip()
        st.session_state["_agent_currency"]    = currency

        result = _run_layer1(file_bytes, client_name.strip(), currency, region_override)
        if result:
            st.session_state["_l1_result"] = result
            _set_step(1)
            st.rerun()

# ─── Layer checkpoint pages ──────────────────────────────────────────────────

def _render_l1_checkpoint() -> None:
    bm = st.session_state.get("benchmarks", BenchmarkConfig.from_yaml())

    st.subheader("📋 Layer 1 — Inventory Review")
    st.caption(
        "Review the parsed fleet inventory, OS/license profile, and inferred Azure region. "
        "Use the override panel to change any parameter and re-run. "
        "Pre-select rightsizing settings, then approve to proceed."
    )

    _show_layer1()
    _render_l1_override()

    st.divider()
    st.markdown("**Pre-flight settings for Rightsizing →**")
    pf1, pf2 = st.columns(2)
    ramp = pf1.selectbox(
        "Migration horizon",
        _RAMP_OPTIONS,
        index=_RAMP_OPTIONS.index("Extended (100% by Y3)"),
        key="_pf_ramp",
    )
    storage_sel = pf2.radio(
        "Storage costing mode",
        ["Per-VM disk tiers (accurate)", "Fleet aggregate (fast)"],
        index=0, horizontal=True, key="_pf_storage",
    )

    if st.button(
        "✅ Approve Inventory — Run Rightsizing →",
        type="primary", use_container_width=True, key="_approve_l1",
    ):
        smode  = "per_vm" if "Per-VM" in storage_sel else "aggregate"
        result = _run_layer2(ramp, smode, bm)
        if result:
            st.session_state["_l2_result"] = result
            _set_step(2)
            st.rerun()


def _render_l2_checkpoint() -> None:
    bm = st.session_state.get("benchmarks", BenchmarkConfig.from_yaml())

    st.subheader("⚙️ Layer 2 — Rightsizing Review")
    st.caption(
        "Review per-VM rightsizing results and the Layer 1 → Layer 2 validation checkpoint. "
        "Check the anomaly list and telemetry coverage. "
        "Adjust headroom/fallback factors via the override panel if needed. "
        "Enter funding credits, select analysis horizon, then approve."
    )

    _show_layer2()
    _render_l2_override()

    st.divider()
    st.markdown("**Pre-flight settings for Financial Model →**")
    pf1, pf2 = st.columns(2)
    dc_exit     = pf1.number_input(
        "Datacenters to exit",
        min_value=0, max_value=10, value=0, key="_pf_dc_exit",
    )
    horizon_sel = pf2.radio(
        "Analysis horizon",
        ["5-Year", "10-Year"],
        index=0, horizontal=True, key="_pf_horizon_sel",
    )
    st.session_state["_agent_horizon"] = 5 if "5" in horizon_sel else 10

    st.markdown("**Microsoft Funding Credits** *(optional — leave 0 if not applicable)*")
    fc1, fc2, fc3, fc4, fc5 = st.columns(5)
    aco_y1  = fc1.number_input("ACO Yr 1",  min_value=0.0, value=0.0, step=10_000.0, format="%.0f", key="_pf_a1")
    aco_y2  = fc2.number_input("ACO Yr 2",  min_value=0.0, value=0.0, step=10_000.0, format="%.0f", key="_pf_a2")
    aco_y3  = fc3.number_input("ACO Yr 3",  min_value=0.0, value=0.0, step=10_000.0, format="%.0f", key="_pf_a3")
    ecif_y1 = fc4.number_input("ECIF Yr 1", min_value=0.0, value=0.0, step=10_000.0, format="%.0f", key="_pf_e1")
    ecif_y2 = fc5.number_input("ECIF Yr 2", min_value=0.0, value=0.0, step=10_000.0, format="%.0f", key="_pf_e2")

    if st.button(
        "✅ Approve Rightsizing — Build Financial Model →",
        type="primary", use_container_width=True, key="_approve_l2",
    ):
        aco    = [-aco_y1, -aco_y2, -aco_y3, 0, 0, 0, 0, 0, 0, 0]
        ecif   = [-ecif_y1, -ecif_y2, 0, 0, 0, 0, 0, 0, 0, 0]
        result = _run_layer3(aco, ecif, int(dc_exit), bm, label="Base")
        if result:
            _clear_from(3)
            st.session_state["_l3_result"]     = result
            st.session_state["_l3_scenarios"]  = []
            st.session_state["inputs"]         = result["inputs"]
            st.session_state["_agent_summary"] = result["summary"]
            _set_step(3)
            st.rerun()


def _render_l3_checkpoint() -> None:
    horizon = st.session_state.get("_agent_horizon", 5)

    st.subheader("📊 Layer 3 — Financial Model Review")
    st.caption(
        "Review the business case output. "
        "Use the override panel to test different WACC, funding credits, or DC-exit scenarios. "
        "Add named comparison scenarios to compare assumptions side-by-side. "
        "Approve to proceed to export."
    )

    _show_layer3(horizon=horizon)
    _render_l3_override()

    st.divider()
    if st.button(
        "✅ Approve Business Case — Proceed to Export →",
        type="primary", use_container_width=True, key="_approve_l3",
    ):
        _set_step(4)
        st.rerun()


def _render_export() -> None:
    st.subheader("📥 Ready for Export")
    l3      = st.session_state.get("_l3_result", {})
    summary = l3.get("summary")
    l1      = st.session_state.get("_l1_result", {})
    cur     = l1.get("currency", "USD")

    if summary:
        pb = f"{summary.payback_cf:.1f} yrs" if summary.payback_cf else ">5 yrs"
        st.success(
            f"📊 **{l1.get('client_name', 'Client')}** — "
            f"5-Yr CF ROI: **{summary.roi_cf:.0%}** · Payback: **{pb}** · "
            f"CF NPV (5Y): **{_fmt(summary.npv_cf_5yr, cur)}**"
        )

    st.info(
        "**'4 · Results'** in the sidebar → full interactive analysis, deep-dive charts, "
        "waterfall, and engineering fact-check.  \n"
        "**'5 · Export'** → PowerPoint board deck or pre-filled Excel workbook.",
        icon="💡",
    )

# ─── Main render ─────────────────────────────────────────────────────────────

def render() -> None:
    st.title("⚡ Agent Intake — Layered Business Case Builder")
    st.caption(
        "Three-layer checkpoint workflow: "
        "**Inventory → Rightsizing → Financial Model**. "
        "Review and approve each layer before proceeding. "
        "Override and re-run any layer independently without losing other results."
    )
    st.markdown(_METRIC_CSS, unsafe_allow_html=True)

    step = _step()
    _step_bar()

    # Approved-layer compact summaries shown above the active layer
    if step > 1:
        _l1_banner()
    if step > 2:
        _l2_banner()
    if step > 3:
        _l3_banner()

    # Start-over escape hatch (shown once any layer has run)
    if step > 0:
        with st.expander("🔄 Start over (re-upload a different file)", expanded=False):
            st.caption("Clears all results and returns to the upload form.")
            if st.button("⬅ Start Over", key="_start_over"):
                for k in list(st.session_state.keys()):
                    if k.startswith("_"):
                        st.session_state.pop(k, None)
                st.rerun()

    st.divider()

    # Dispatch to the active layer
    if step == 0:
        _render_upload()
    elif step == 1:
        _render_l1_checkpoint()
    elif step == 2:
        _render_l2_checkpoint()
    elif step == 3:
        _render_l3_checkpoint()
    else:
        _render_export()
