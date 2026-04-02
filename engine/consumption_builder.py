"""
Consumption plan builder — per-VM rightsizing engine.

For each powered-on VM in the RVToolsInventory:
  1. Resolve utilisation signal (vCPU/vMemory telemetry → vHost proxy → fallback factors).
  2. Rightsize to (target_vcpu, target_mem_gib) with headroom.
  3. Select Azure family (D/E/F/M) based on memory density and workload keywords.
  4. Match least-cost Azure SKU from the pre-fetched VM catalog.
  5. Price: sku.price_per_hour_usd × 8760 hrs.

Storage (per VM, priority order):
  1. vDisk tab — per-disk Capacity MiB (GiB = MiB ÷ 1024) → assign_cheapest()
  2. vPartition tab — Consumed MiB summed per VM (GiB)
  3. vInfo — In Use MiB (GiB)
  4. vInfo — Provisioned MiB × (1 − storage_prov_reduction_factor) (GiB)

All costs in USD, converted to local currency via usd_to_local.
ACD (Azure Consumption Discount) is applied in Step 2 of the financial model,
not here — this builder always returns PAYG list prices.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

_log = logging.getLogger(__name__)

from engine.models import (
    BenchmarkConfig,
    ConsumptionPlan,
    MIGRATION_RAMP_PRESETS,
)
from engine.disk_tier_map import vm_annual_storage_cost_usd as _vm_disk_cost
from engine.vm_rightsizer import resolve_vm_utilisation, rightsize_vm, select_family

if TYPE_CHECKING:
    from engine.rvtools_parser import RVToolsInventory
    from engine.azure_sku_matcher import AzurePricing, VMSku


def build(
    inv: "RVToolsInventory",
    pricing: "AzurePricing",
    benchmarks: BenchmarkConfig | None = None,
    workload_name: str = "",
    usd_to_local: float = 1.0,
    ramp_preset: str = "Extended (100% by Y3)",
    migration_cost_per_vm_lc: float = 1_500.0,
    storage_mode: str = "per_vm",
    disk_type: str = "standard_ssd",
    vm_catalog: list["VMSku"] | None = None,
) -> ConsumptionPlan:
    """
    Build a ConsumptionPlan from RVtools inventory and Azure pricing.

    Parameters
    ----------
    inv : RVToolsInventory
        Parsed RVtools data including vm_records, per-VM util maps, disk sizes.
    pricing : AzurePricing
        Reference pricing from azure_sku_matcher.get_pricing() — used as
        fallback rate when per-VM SKU match has no live price.
    benchmarks : BenchmarkConfig or None
        Right-sizing and threshold parameters.  None → use class defaults.
    workload_name : str
        Label propagated to ConsumptionPlan.
    usd_to_local : float
        FX rate: 1 USD = x local currency.
    ramp_preset : str
        Key from MIGRATION_RAMP_PRESETS.  Default: "Extended (100% by Y3)".
    migration_cost_per_vm_lc : float
        Migration cost per VM in local currency.
    storage_mode : str
        "per_vm" (default) — per-disk cost via assign_cheapest().
        "aggregate" — fleet total × blended per-GB rate from AzurePricing.
    disk_type : str
        Ignored for per_vm mode (always picks cheapest of P/Pv2).
        Used for aggregate fallback: "standard_ssd" or "premium_ssd".
    vm_catalog : list[VMSku] | None
        Pre-fetched VM SKU catalog with live prices.  If None, the engine
        falls back to the reference per-vCPU rate from AzurePricing.
    """
    pb = benchmarks or BenchmarkConfig()
    ref_vcpu_rate = pricing.price_per_vcpu_hour_usd   # fallback rate

    # ── Per-VM compute cost loop ─────────────────────────────────────────
    total_compute_usd_yr = 0.0
    total_storage_usd_yr = 0.0
    total_azure_vcpu     = 0
    total_azure_mem_gib  = 0.0
    total_azure_stor_gib = 0.0
    fallback_vm_count    = 0
    no_price_vm_count    = 0

    vm_records = inv.vm_records  # powered-on, non-template VMs

    if not vm_records:
        # No per-VM records — fall back to legacy fleet-aggregate path
        _log.debug("[consumption_builder] No vm_records; using fleet-aggregate fallback")
        return _aggregate_fallback(inv, pricing, pb, workload_name, usd_to_local,
                                   ramp_preset, migration_cost_per_vm_lc, storage_mode, disk_type)

    for vm in vm_records:
        # ── Utilisation ──────────────────────────────────────────────────
        cpu_util, mem_util, util_src = resolve_vm_utilisation(vm, inv)

        # ── Rightsize ────────────────────────────────────────────────────
        target_vcpu, target_mem_gib = rightsize_vm(vm, cpu_util, mem_util, util_src, pb)
        if util_src == "fallback":
            fallback_vm_count += 1

        # ── Family selection ─────────────────────────────────────────────
        family = select_family(vm, target_vcpu, target_mem_gib, pb)

        # ── SKU match ────────────────────────────────────────────────────
        if vm_catalog:
            from engine.azure_sku_matcher import match_sku
            sku = match_sku(
                target_vcpu, target_mem_gib, vm_catalog, family,
                fallback_ref_price_per_hour=ref_vcpu_rate * target_vcpu,
            )
            vm_price_per_hr = sku.price_per_hour_usd
            matched_vcpu    = sku.vcpu
            matched_mem_gib = sku.memory_gib
            if vm_price_per_hr <= 0:
                no_price_vm_count += 1
                vm_price_per_hr = ref_vcpu_rate * target_vcpu
        else:
            # No catalog — use reference per-vCPU rate
            vm_price_per_hr = ref_vcpu_rate * target_vcpu
            matched_vcpu    = target_vcpu
            matched_mem_gib = target_mem_gib

        total_compute_usd_yr += vm_price_per_hr * pb.hours_per_year
        total_azure_vcpu     += matched_vcpu
        total_azure_mem_gib  += matched_mem_gib

        # ── Storage for this VM ──────────────────────────────────────────
        if storage_mode == "per_vm":
            vm_stor_usd_yr, stor_gib = _vm_storage_cost(vm, pb)
            total_storage_usd_yr += vm_stor_usd_yr
            total_azure_stor_gib += stor_gib
        # aggregate storage handled outside the loop below

    if storage_mode != "per_vm":
        # Aggregate mode — fleet total blended rate
        total_azure_stor_gib = math.ceil(
            (inv.total_disk_provisioned_poweredon_gb or inv.total_storage_poweredon_gb)
        )
        total_storage_usd_yr = total_azure_stor_gib * 12 * pricing.price_per_gb_month_usd

    if fallback_vm_count > 0:
        _log.debug(
            f"[consumption_builder] {fallback_vm_count}/{len(vm_records)} VMs "
            f"used assumption fallback factors (no telemetry)"
        )
    if no_price_vm_count > 0:
        _log.debug(
            f"[consumption_builder] {no_price_vm_count} VMs fell back to ref vCPU rate "
            f"(${ref_vcpu_rate:.4f}/vCPU/hr) — no catalog price available"
        )

    compute_lc_yr = total_compute_usd_yr * usd_to_local
    storage_lc_yr = total_storage_usd_yr * usd_to_local

    ramp = MIGRATION_RAMP_PRESETS.get(ramp_preset) or MIGRATION_RAMP_PRESETS["Extended (100% by Y3)"]

    _log.debug(
        f"[consumption_builder] Per-VM results: {len(vm_records):,} VMs\n"
        f"  Matched Azure vCPU total:  {total_azure_vcpu:,}\n"
        f"  Matched Azure memory GiB:  {total_azure_mem_gib:,.0f}\n"
        f"  Compute Y10 est:           ${total_compute_usd_yr:,.0f}/yr\n"
        f"  Storage Y10 est:           ${total_storage_usd_yr:,.0f}/yr  (mode={storage_mode})\n"
        f"  Region / pricing source:   {pricing.region} / {pricing.source}"
    )

    return ConsumptionPlan(
        workload_name=workload_name,
        azure_vcpu=total_azure_vcpu,
        azure_memory_gb=round(total_azure_mem_gib, 2),
        azure_storage_gb=round(total_azure_stor_gib, 2),
        migration_cost_per_vm_lc=migration_cost_per_vm_lc,
        migration_ramp_pct=list(ramp),
        annual_compute_consumption_lc_y10=round(compute_lc_yr, 2),
        annual_storage_consumption_lc_y10=round(storage_lc_yr, 2),
        annual_other_consumption_lc_y10=0.0,
    )


# ---------------------------------------------------------------------------
# Storage cost helper — per-VM, GiB throughout
# ---------------------------------------------------------------------------

def _vm_storage_cost(vm, pb: BenchmarkConfig) -> tuple[float, float]:
    """
    Return (annual_storage_usd, total_gib) for one VM.
    Priority: vDisk disk_sizes_gib → vPartition → vInfo In Use → vInfo Provisioned × reduction.
    """
    if vm.disk_sizes_gib:
        # Best path: vDisk per-disk GiB assignment
        annual_usd, _ = _vm_disk_cost(vm.disk_sizes_gib)
        total_gib = sum(vm.disk_sizes_gib)
        return annual_usd, total_gib

    if vm.partition_consumed_gib > 0:
        # vPartition consumed (guest OS view)
        gib = vm.partition_consumed_gib
        annual_usd, _ = _vm_disk_cost([gib])
        return annual_usd, gib

    if vm.inuse_gib > 0:
        # vInfo In Use MiB → GiB
        gib = vm.inuse_gib
        annual_usd, _ = _vm_disk_cost([gib])
        return annual_usd, gib

    if vm.provisioned_gib > 0:
        # vInfo Provisioned × (1 − reduction)
        gib = vm.provisioned_gib * (1.0 - pb.storage_prov_reduction_factor)
        annual_usd, _ = _vm_disk_cost([max(gib, 1.0)])
        return annual_usd, max(gib, 1.0)

    # No storage data at all — 0
    return 0.0, 0.0


# ---------------------------------------------------------------------------
# Legacy fleet-aggregate fallback (no vm_records)
# ---------------------------------------------------------------------------

def _aggregate_fallback(
    inv, pricing, pb: BenchmarkConfig,
    workload_name, usd_to_local, ramp_preset,
    migration_cost_per_vm_lc, storage_mode, disk_type,
) -> ConsumptionPlan:
    """
    Fleet-level fallback when vm_records is empty (parse produced no per-VM data).
    Uses the old fleet-aggregate logic as a safety net.
    """
    base_vcpu = inv.total_vcpu_poweredon or inv.total_vcpu
    base_mem  = inv.total_vmemory_gb_poweredon or inv.total_vmemory_gb

    if inv.cpu_util_p95 > 0:
        azure_vcpu = max(1, math.ceil(
            base_vcpu * inv.cpu_util_p95 * (1 + pb.cpu_rightsizing_headroom_factor)
        ))
    else:
        azure_vcpu = max(1, math.ceil(base_vcpu * pb.cpu_util_fallback_factor
                                      * (1 + pb.cpu_rightsizing_headroom_factor)))
    if inv.memory_util_p95 > 0:
        azure_mem_gb = max(1.0, math.ceil(
            base_mem * inv.memory_util_p95 * (1 + pb.memory_rightsizing_headroom_factor)
        ))
    else:
        azure_mem_gb = max(1.0, math.ceil(base_mem * pb.mem_util_fallback_factor
                                          * (1 + pb.memory_rightsizing_headroom_factor)))

    provisioned = getattr(inv, "total_disk_provisioned_poweredon_gb", 0.0) or 0.0
    if provisioned > 0:
        azure_stor_gb = math.ceil(provisioned)
        storage_usd_yr = azure_stor_gb * 12 * pricing.price_per_gb_month_usd
    else:
        base_stor = inv.total_storage_poweredon_gb or inv.total_storage_in_use_gb
        azure_stor_gb = math.ceil(base_stor)
        storage_usd_yr = azure_stor_gb * 12 * pricing.price_per_gb_month_usd

    compute_usd_yr = azure_vcpu * pb.hours_per_year * pricing.price_per_vcpu_hour_usd
    ramp = MIGRATION_RAMP_PRESETS.get(ramp_preset) or MIGRATION_RAMP_PRESETS["Extended (100% by Y3)"]

    return ConsumptionPlan(
        workload_name=workload_name,
        azure_vcpu=azure_vcpu,
        azure_memory_gb=float(azure_mem_gb),
        azure_storage_gb=float(azure_stor_gb),
        migration_cost_per_vm_lc=migration_cost_per_vm_lc,
        migration_ramp_pct=list(ramp),
        annual_compute_consumption_lc_y10=round(compute_usd_yr * usd_to_local, 2),
        annual_storage_consumption_lc_y10=round(storage_usd_yr * usd_to_local, 2),
        annual_other_consumption_lc_y10=0.0,
    )
