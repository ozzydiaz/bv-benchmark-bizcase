"""
Consumption plan builder.

Derives a ConsumptionPlan from a parsed RVToolsInventory, region-inferred
Azure pricing, and benchmark configuration.

Right-sizing logic
──────────────────
vCPU / memory
  When utilisation telemetry is available (cpu_util_p95 > 0 / memory_util_p95 > 0):
    azure_vcpu   = max(1, ceil(vcpu_poweredon × p95 × (1 + headroom_factor)))
    azure_mem_gb = max(1.0, mem_gb_poweredon × p95_mem × (1 + headroom_factor))

  When telemetry absent (vCPU / vMemory tabs missing from RVtools export):
    azure_vcpu   = max(1, ceil(vcpu_poweredon × (1 - cpu_rightsizing_fallback_reduction)))
    azure_mem_gb = max(1.0, mem_gb_poweredon × (1 - memory_rightsizing_fallback_reduction))
    Both defaults are configurable in BenchmarkConfig / benchmarks_default.yaml.

Storage
  Two modes controlled by the storage_mode parameter:

  "aggregate" (default)
    Uses the fleet total from vDisk.Capacity MiB (provisioned) when the vDisk
    tab is present, otherwise vInfo.In Use MiB, multiplied by
    storage_rightsizing_headroom_factor.  A single blended per-GB rate from
    azure_sku_matcher is applied.

    storage_usd = total_provisioned_gb × headroom × 12 × price_per_gb_month

  "per_vm"
    Each disk from vDisk.Capacity MiB is individually assigned to the correct
    Azure managed disk tier (Standard SSD E-series by default, or Premium SSD
    P-series via disk_type parameter) using disk_tier_map.assign_tier().
    The sum of per-disk tier prices gives a more accurate storage estimate,
    particularly where the fleet has a mix of large and small disks.

    storage_usd = Σ_disks( assign_tier(disk_gib).price_per_month × 12 )

    Requires the vDisk tab.  Falls back to "aggregate" with a warning if
    vm_disk_sizes_gb is empty.

All costs converted to local currency via usd_to_local.  ACD applied in Step 2.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

_log = logging.getLogger(__name__)

from engine.models import (
    ConsumptionPlan,
    MIGRATION_RAMP_PRESETS,
)
from engine.disk_tier_map import fleet_annual_cost_usd as _fleet_disk_cost

if TYPE_CHECKING:
    from engine.rvtools_parser import RVToolsInventory
    from engine.azure_sku_matcher import AzurePricing


def build(
    inv: "RVToolsInventory",
    pricing: "AzurePricing",
    benchmarks: BenchmarkConfig | None = None,
    workload_name: str = "",
    usd_to_local: float = 1.0,
    ramp_preset: str = "Extended (100% by Y3)",
    migration_cost_per_vm_lc: float = 1_500.0,
    storage_mode: str = "aggregate",
    disk_type: str = "standard_ssd",
) -> ConsumptionPlan:
    """
    Build a ConsumptionPlan from RVtools inventory and Azure pricing.

    Parameters
    ----------
    inv : RVToolsInventory
        Parsed RVtools data with utilisation telemetry and powered-on sizing.
    pricing : AzurePricing
        Per-unit PAYG rates from azure_sku_matcher.get_pricing().
    benchmarks : BenchmarkConfig or None
        Right-sizing parameters.  None → use class defaults.
    workload_name : str
        Label propagated to ConsumptionPlan.workload_name.
    usd_to_local : float
        FX rate: 1 USD = x local currency units.  Cost estimates are stored in
        local currency (matching the rest of the engine).
    ramp_preset : str
        Key from MIGRATION_RAMP_PRESETS.  Defaults to "Extended (100% by Y3)".
    migration_cost_per_vm_lc : float
        Migration cost per VM in local currency (default $1,500).
    storage_mode : str
        "aggregate" (default) — fleet total × blended per-GB rate.
        "per_vm" — each disk individually assigned to an Azure managed disk
        tier using disk_tier_map.assign_tier().  Requires vDisk tab.
    disk_type : str
        Managed disk family for per_vm mode: "standard_ssd" (default) or
        "premium_ssd".  Ignored for aggregate mode.

    Returns
    -------
    ConsumptionPlan
        Fully populated plan with auto-computed azure_vcpu, azure_memory_gb,
        azure_storage_gb, and annual consumption anchors.
    """
    pb = benchmarks or BenchmarkConfig()

    # ── Right-size vCPU ─────────────────────────────────────────────────
    base_vcpu = inv.total_vcpu_poweredon or inv.total_vcpu
    if inv.cpu_util_p95 > 0:
        azure_vcpu = max(1, math.ceil(
            base_vcpu * inv.cpu_util_p95 * (1 + pb.cpu_rightsizing_headroom_factor)
        ))
        cpu_method = f"P95={inv.cpu_util_p95:.0%} + {pb.cpu_rightsizing_headroom_factor:.0%} headroom"
    else:
        azure_vcpu = max(1, math.ceil(
            base_vcpu * (1 - pb.cpu_rightsizing_fallback_reduction)
        ))
        cpu_method = f"fallback −{pb.cpu_rightsizing_fallback_reduction:.0%}"

    # ── Right-size memory ────────────────────────────────────────────────
    base_mem = inv.total_vmemory_gb_poweredon or inv.total_vmemory_gb
    if inv.memory_util_p95 > 0:
        azure_mem_gb = max(1.0, math.ceil(
            base_mem * inv.memory_util_p95 * (1 + pb.memory_rightsizing_headroom_factor)
        ))
        mem_method = f"P95={inv.memory_util_p95:.0%} + {pb.memory_rightsizing_headroom_factor:.0%} headroom"
    else:
        azure_mem_gb = max(1.0, math.ceil(
            base_mem * (1 - pb.memory_rightsizing_fallback_reduction)
        ))
        mem_method = f"fallback −{pb.memory_rightsizing_fallback_reduction:.0%}"

    # ── Right-size storage ───────────────────────────────────────────────
    # Azure managed disk pricing is per provisioned tier, not per consumed byte.
    # Prefer vDisk.Capacity MiB (provisioned) when available; fall back to
    # vInfo In Use MiB × headroom_factor as an approximation.
    vm_disks: dict[str, list[float]] = getattr(inv, "vm_disk_sizes_gb", {}) or {}
    provisioned = getattr(inv, "total_disk_provisioned_poweredon_gb", 0.0) or 0.0

    resolved_mode = storage_mode
    if storage_mode == "per_vm" and not vm_disks:
        _log.debug("[consumption_builder] WARNING: per_vm mode requested but vDisk tab absent — falling back to aggregate")
        resolved_mode = "aggregate"

    if resolved_mode == "per_vm":
        # Per-disk tier assignment — more accurate for mixed-size fleets
        storage_usd_yr, tier_counts, total_prov_gib = _fleet_disk_cost(vm_disks, disk_type)
        azure_stor_gb = math.ceil(total_prov_gib)
        top_tiers = sorted(tier_counts.items(), key=lambda x: -x[1])[:3]
        tier_summary = ", ".join(f"{t}×{n}" for t, n in top_tiers)
        stor_method = (
            f"per_vm / {disk_type} — {len(vm_disks):,} VMs, "
            f"{sum(tier_counts.values()):,} disks "
            f"(top tiers: {tier_summary})"
        )
    else:
        # Aggregate mode — fleet total × blended per-GB rate
        if provisioned > 0:
            azure_stor_gb = math.ceil(provisioned * pb.storage_rightsizing_headroom_factor)
            stor_method = f"aggregate / vDisk provisioned {provisioned:,.0f} GB × {pb.storage_rightsizing_headroom_factor}"
        else:
            base_stor = inv.total_storage_poweredon_gb or inv.total_storage_in_use_gb
            azure_stor_gb = math.ceil(base_stor * pb.storage_rightsizing_headroom_factor)
            stor_method = f"aggregate / vInfo in-use {base_stor:,.0f} GB × {pb.storage_rightsizing_headroom_factor} (vDisk tab absent)"
        storage_usd_yr = azure_stor_gb * 12 * pricing.price_per_gb_month_usd

    # ── Annual consumption estimates (Y10 steady-state, local currency) ──
    hours = pb.hours_per_year  # 8760 by default
    compute_usd_yr = azure_vcpu * hours * pricing.price_per_vcpu_hour_usd
    # storage_usd_yr already set in the storage block above

    compute_lc_yr = compute_usd_yr * usd_to_local
    storage_lc_yr = storage_usd_yr * usd_to_local

    # ── Migration ramp ────────────────────────────────────────────────────
    ramp = MIGRATION_RAMP_PRESETS.get(ramp_preset) or MIGRATION_RAMP_PRESETS["Extended (100% by Y3)"]

    # ── Log summary ───────────────────────────────────────────────────────
    _log.debug(
        f"[consumption_builder] Right-sized vCPU: {base_vcpu:,} → {azure_vcpu:,}  ({cpu_method})\n"
        f"[consumption_builder] Right-sized mem:  {base_mem:,.0f} GB → {azure_mem_gb:,.0f} GB  ({mem_method})\n"
        f"[consumption_builder] Right-sized stor: → {azure_stor_gb:,.0f} GB  ({stor_method})\n"
        f"[consumption_builder] Compute Y10 est:  ${compute_usd_yr:,.0f}/yr "
        f"({pricing.price_per_vcpu_hour_display} × {azure_vcpu} vCPU × {hours:.0f} h)\n"
        f"[consumption_builder] Storage Y10 est:  ${storage_usd_yr:,.0f}/yr  "
        f"(mode={resolved_mode})\n"
        f"[consumption_builder] Region / source:  {pricing.region} / {pricing.source}\n"
        f"[consumption_builder] Ramp preset:      {ramp_preset}"
    )

    return ConsumptionPlan(
        workload_name=workload_name,
        azure_vcpu=azure_vcpu,
        azure_memory_gb=float(azure_mem_gb),
        azure_storage_gb=float(azure_stor_gb),
        migration_cost_per_vm_lc=migration_cost_per_vm_lc,
        migration_ramp_pct=list(ramp),
        annual_compute_consumption_lc_y10=round(compute_lc_yr, 2),
        annual_storage_consumption_lc_y10=round(storage_lc_yr, 2),
        annual_other_consumption_lc_y10=0.0,
    )
