"""
Layer 2 Business-Analyst Replica — Per-VM Azure SKU Right-Sizing
=================================================================

Engine-independent oracle that mechanically follows the rules in
``training/ba_rules/layer2.yaml``. Used as the reference implementation
for Layer 2 parity diffing.

Source authority (DUAL-CITED throughout — see KP.AZURE_RD_RIGHTSIZING_v1):
1. Microsoft Azure R&D — "VMware/OnPrem to Azure Mappings" deck.
   Verbatim formulas at
   ``training/baseline_workflow/azure_rd_rightsizing/canonical_formulas.md``.
2. BA Layer 2 transcript at
   ``training/baseline_workflow/layer2_azure_match/transcript.vtt``.

Scope (Phase 2 / Layer 2):
    * Right-sizing math — vCPU, memory, storage — across the 5 strategies
      defined in KP.STRATEGY_PRECEDENCE.
    * Per-VM authoritative payload (KP.PER_VM_REPRISE).
    * Multi-disk decomposition for storage that exceeds the largest
      single-disk tier.
    * 8-vCPU floor flag honouring KP.WIN_8VCPU_MIN.

NOT in scope for Phase 2 (deferred to Phase 2b after the rightsizing
parity gate clears):
    * Azure Retail Price API calls (PAYG, RI, SP).
    * Match-error retry loop (L2.RIGHTSIZE.CPU.RETRY).
    * Family / processor pin filter.

NO IMPORTS FROM ``engine/`` — this is an independent oracle.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from training.replicas.layer1_ba_replica import (  # noqa: E402  (sibling module)
    MIB_TO_GB_DECIMAL,
    SYNONYMS,
    Layer1Result,
    VMRecord as L1VMRecord,
    _find_sheet,
    _open_workbook,
    _read_headers,
    _read_rows,
    replicate_layer1,
    resolve_column,
)
from training.replicas.azure_pricing import (  # noqa: E402
    DISK_CATALOG_LRS,
    PRICING_OFFERS,
    PricedDisk,
    PricedSku,
    get_priced_disk_catalog,
    get_priced_vm_catalog,
)

_log = logging.getLogger("layer2_ba_replica")


# =============================================================================
# R&D anchors — ladders and helpers (verbatim from canonical_formulas.md)
# =============================================================================

# R&D.SLIDE7.SNAPCPU
SNAPCPU_LADDER: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 48, 64, 96, 128, 176, 192)

# R&D.SLIDE8.SNAPMEM (Azure-realistic memory rungs).
# Includes the half-rungs (48, 96, 160, 192, 320, 384, 432, 672) that map to
# specific Azure SKU memory shapes (D48/D96, E20=160, E48=384, FX24=432,
# FX32=672). Removing them collapses too much mass into power-of-2 buckets
# and degrades exact-SKU match rate.
SNAPMEM_LADDER: tuple[int, ...] = (
    4, 8, 16, 32, 48, 64, 96, 128, 160, 192, 256,
    320, 384, 432, 512, 576, 672, 768, 1024, 1792, 2048,
)

# R&D.SLIDE9 — all storage branches snap to 128-GiB boundaries (P10 minimum).
DISK_SNAP_GIB = 128

# Largest single Azure managed-disk tier today (P80). Anything above
# decomposes per L2.RIGHTSIZE.STORAGE.MULTI_DISK.
MAX_SINGLE_DISK_GIB = 32_767

# Storage size-conversion convention (BA-confirmed via Customer A baseline
# 2026-04-27): the BA's spreadsheet snaps after converting MiB → decimal
# GB (÷ 953.674), NOT MiB → binary GiB (÷ 1024). The R&D deck text says
# '/1024' but the BA's actual XA2 workflow uses decimal GB. We keep BOTH
# helpers and default to the BA-confirmed decimal convention; the binary
# variant is available for any future engagement that prefers it.
_STORAGE_MIB_PER_GB_DECIMAL = 953.674   # 1 decimal GB = 1e9 / 2^20 MiB
_STORAGE_MIB_PER_GIB_BINARY = 1024.0


def snap_cpu(n: float) -> int:
    """R&D.SLIDE7.SNAPCPU — round UP to next vCPU ladder rung.

    Values exceeding the ladder return the largest defined rung; callers
    that need to model 200+ vCPU SKUs should extend ``SNAPCPU_LADDER``.
    """
    if n <= 0:
        return SNAPCPU_LADDER[0]
    target = math.ceil(n)
    for rung in SNAPCPU_LADDER:
        if rung >= target:
            return rung
    return SNAPCPU_LADDER[-1]


def snap_mem_gib(g: float) -> int:
    """R&D.SLIDE8.SNAPMEM — round UP to next memory-tier rung (GiB)."""
    if g <= 0:
        return SNAPMEM_LADDER[0]
    target = math.ceil(g)
    for rung in SNAPMEM_LADDER:
        if rung >= target:
            return rung
    return SNAPMEM_LADDER[-1]


# Azure managed-disk tier ladder (GiB) — both Standard SSD (E-series) and
# Premium SSD (P-series) follow this same capacity ladder.
# E/P 1=4, 2=8, 3=16, 4=32, 6=64, 10=128, 15=256, 20=512, 30=1024, 40=2048,
# 50=4096, 60=8192, 70=16384, 80=32767.
DISK_TIER_LADDER_GIB: tuple[int, ...] = (
    4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32767,
)


def snap_disk_gib(g: float) -> int:
    """R&D.SLIDE9 — round UP to next Azure managed-disk tier capacity.

    Per the BA Customer A audit (2026-04-27), BA's XA2 picks across the
    full E1..E80 ladder (smallest = 4 GiB, largest = 32,767 GiB), not just
    the P10 (128 GiB) floor I previously assumed. Snap to the ladder so
    small per-disk sizes correctly land on E1/E2/E3 etc.
    """
    if g <= 0:
        return DISK_TIER_LADDER_GIB[0]
    target = math.ceil(g)
    for rung in DISK_TIER_LADDER_GIB:
        if rung >= target:
            return rung
    return DISK_TIER_LADDER_GIB[-1]


# =============================================================================
# Strategy enum + 8-vCPU floor (KP.STRATEGY_PRECEDENCE, KP.WIN_8VCPU_MIN)
# =============================================================================

VALID_STRATEGIES = (
    "per_vm_telemetry",
    "host_proxy",
    "flat_reduction",
    "like_for_like",
    "ba_fallback",
)

# KP.BA_FALLBACK_REDUCTIONS — "Ozzie's guess" defaults (BA-editable).
BA_FALLBACK_REDUCTIONS = {
    "cpu_reduction_pct": 60,        # retain 40%
    "mem_reduction_pct": 40,        # retain 60%
    "storage_reduction_pct": 20,    # retain 80%
    "mem_buffer_pct": 0,
    "storage_buffer_pct": 0,
}

# Reference-rate pricing (BA's simpler methodology used in Customer A baseline).
# These match the engine defaults (engine/azure_sku_matcher.py _DEFAULT_VCPU_RATE
# / _DEFAULT_GB_RATE) and are also the values the BA's spreadsheet uses for the
# headline 'Azure compute revenue' calculation when she doesn't break costs out
# per-SKU. The replica surfaces BOTH tier-based AND reference-rate totals so the
# BA can choose which methodology to commit to per engagement.
REFERENCE_RATES = {
    "vcpu_usd_hr":  0.048,    # Linux PAYG D-series average, eastus2
    "gb_usd_month": 0.075,    # Premium SSD LRS Px-series average
}

# KP.WIN_8VCPU_MIN — Windows pattern is shared with engine/rvtools_parser.
_WINDOWS_PATTERN = re.compile(r"windows\s+server", re.IGNORECASE)


def is_windows_server(os_config: str, os_tools: str) -> bool:
    """Match the engine's _WINDOWS_PATTERN against EITHER OS column."""
    return bool(
        _WINDOWS_PATTERN.search(os_config or "")
        or _WINDOWS_PATTERN.search(os_tools or "")
    )


def resolve_min_vcpus(
    vm: L1VMRecord,
    enforce_8vcpu_min_for_windows_server: bool,
) -> tuple[int, str]:
    """Return ``(min_vcpus, vcpu_floor_source)`` per KP.WIN_8VCPU_MIN."""
    if not enforce_8vcpu_min_for_windows_server:
        return 1, "flag_off"
    if is_windows_server(vm.os_config, vm.os_tools):
        return 8, "windows_compliance"
    return 1, "linux_or_unknown"


# =============================================================================
# R&D.SLIDE7 — CPU rightsizing branches
# =============================================================================

def rd_slide7_cpu_default(vinfo_cpus: int, vhost_cpu_pct: float | None, min_vcpus: int) -> int:
    """R&D.SLIDE7.CPU.DEFAULT — host-proxy formula (opt-in only)."""
    pct = max(float(vhost_cpu_pct or 0), 1.0)
    return snap_cpu(max(min_vcpus, math.ceil(vinfo_cpus * pct / 100.0)))


def rd_slide7_cpu_user(vinfo_cpus: int, cpu_reduction_pct: float, min_vcpus: int) -> int:
    """R&D.SLIDE7.CPU.USER — flat reduction; reduction=0 == like-for-like."""
    factor = max(0.0, 1.0 - float(cpu_reduction_pct) / 100.0)
    return snap_cpu(max(min_vcpus, math.ceil(vinfo_cpus * factor)))


# =============================================================================
# R&D.SLIDE8 — Memory rightsizing branches
# =============================================================================

def rd_slide8_memory_default(
    vmemory_consumed_mib: float, mem_buffer_pct: float, min_mem_gib: int
) -> int:
    """R&D.SLIDE8.MEMORY.DEFAULT — vMemory[Consumed]-based.

    Buffer defaults to 0 when not supplied.
    """
    raw_gib = float(vmemory_consumed_mib or 0) / 1024.0
    buffered = raw_gib * (1.0 + float(mem_buffer_pct) / 100.0)
    return snap_mem_gib(max(min_mem_gib, buffered))


def rd_slide8_memory_user(
    vinfo_memory_mib: float,
    mem_reduction_pct: float,
    mem_buffer_pct: float,
    min_mem_gib: int,
) -> int:
    """R&D.SLIDE8.MEMORY.USER — vInfo[Memory]-based with reduction + buffer.

    Reduction=0 + buffer=0  ↔  like-for-like (snap-only).
    """
    raw_gib = float(vinfo_memory_mib or 0) / 1024.0
    reduced = raw_gib * (1.0 - float(mem_reduction_pct) / 100.0)
    buffered = reduced * (1.0 + float(mem_buffer_pct) / 100.0)
    return snap_mem_gib(max(min_mem_gib, buffered))


# =============================================================================
# R&D.SLIDE9 — Storage rightsizing chain (per VM)
# =============================================================================

def rd_slide9_storage_default(
    sum_consumed_mib: float,
    storage_buffer_pct: float,
    *,
    use_decimal_gb: bool = True,
) -> int:
    """R&D.SLIDE9.STORAGE.DEFAULT — vPartition Consumed-based, snap to 128 GiB.

    ``use_decimal_gb`` (default True per BA-confirmed Customer A baseline)
    converts MiB via /953.674. Set False to use the verbatim R&D /1024.
    """
    divisor = _STORAGE_MIB_PER_GB_DECIMAL if use_decimal_gb else _STORAGE_MIB_PER_GIB_BINARY
    raw_gib = float(sum_consumed_mib or 0) / divisor
    buffered = raw_gib * (1.0 + float(storage_buffer_pct) / 100.0)
    return snap_disk_gib(buffered)


def rd_slide9_storage_capacity(
    sum_capacity_mib: float,
    storage_reduction_pct: float,
    storage_buffer_pct: float,
    *,
    use_decimal_gb: bool = True,
) -> int:
    """R&D.SLIDE9.STORAGE.CAPACITY — vPartition Capacity-based with reduction + buffer."""
    divisor = _STORAGE_MIB_PER_GB_DECIMAL if use_decimal_gb else _STORAGE_MIB_PER_GIB_BINARY
    raw_gib = float(sum_capacity_mib or 0) / divisor
    reduced = raw_gib * (1.0 - float(storage_reduction_pct) / 100.0)
    buffered = reduced * (1.0 + float(storage_buffer_pct) / 100.0)
    return snap_disk_gib(buffered)


def rd_slide9_storage_vinfo(
    vinfo_total_disk_capacity_mib: float,
    storage_reduction_pct: float,
    storage_buffer_pct: float,
    *,
    use_decimal_gb: bool = True,
) -> int:
    """R&D.SLIDE9.STORAGE.VINFO — vInfo[Total disk capacity MiB] fallback."""
    divisor = _STORAGE_MIB_PER_GB_DECIMAL if use_decimal_gb else _STORAGE_MIB_PER_GIB_BINARY
    raw_gib = float(vinfo_total_disk_capacity_mib or 0) / divisor
    reduced = raw_gib * (1.0 - float(storage_reduction_pct) / 100.0)
    buffered = reduced * (1.0 + float(storage_buffer_pct) / 100.0)
    return snap_disk_gib(buffered)


# =============================================================================
# Strategy → reduction-parameter resolution
# =============================================================================

def _resolve_reduction_params(
    strategy: str,
    *,
    cpu_reduction_pct: float = 0.0,
    mem_reduction_pct: float = 0.0,
    mem_buffer_pct: float = 0.0,
    storage_reduction_pct: float = 0.0,
    storage_buffer_pct: float = 0.0,
) -> dict:
    """Return the reduction/buffer parameters in effect for ``strategy``.

    Per KP.STRATEGY_PRECEDENCE:
      - per_vm_telemetry, host_proxy, like_for_like  →  all 0
      - flat_reduction                                →  BA-supplied
      - ba_fallback                                   →  KP.BA_FALLBACK_REDUCTIONS
    """
    if strategy == "ba_fallback":
        return dict(BA_FALLBACK_REDUCTIONS)
    if strategy == "flat_reduction":
        return {
            "cpu_reduction_pct": cpu_reduction_pct,
            "mem_reduction_pct": mem_reduction_pct,
            "mem_buffer_pct": mem_buffer_pct,
            "storage_reduction_pct": storage_reduction_pct,
            "storage_buffer_pct": storage_buffer_pct,
        }
    # per_vm_telemetry, host_proxy, like_for_like → no reduction
    return {
        "cpu_reduction_pct": 0,
        "mem_reduction_pct": 0,
        "mem_buffer_pct": 0,
        "storage_reduction_pct": 0,
        "storage_buffer_pct": 0,
    }


# =============================================================================
# Per-VM L2 record + result envelope
# =============================================================================

@dataclass
class VMRecordL2:
    """Per-VM Layer 2 result — KP.PER_VM_REPRISE authoritative."""

    name: str
    is_powered_on: bool
    is_template: bool

    # L1 source values (FYI; used by replica internals + diagnostics)
    vinfo_cpus: int
    vinfo_memory_mib: float
    os_config: str
    os_tools: str

    # 8-vCPU floor decision
    min_vcpus: int
    vcpu_floor_source: str               # 'windows_compliance' | 'linux_or_unknown' | 'flag_off'

    # Right-sized triple (the formula output, BEFORE any SKU-shape match retry)
    rs_vcpus: int
    rs_mem_gib: int
    rs_disk_gib: int

    # Branch / formula provenance — every entry maps to an R&D anchor.
    cpu_branch: str
    memory_branch: str
    storage_branch: str
    storage_source_tier: str             # 'vpartition_consumed' | 'vpartition_capacity' | 'vinfo_total' | 'unjoined'

    # Multi-disk decomposition (post-snap; populated when rs_disk_gib > MAX_SINGLE_DISK_GIB
    # OR when vInfo[Disks] hint suggests parallel disks).
    disk_layout: list[tuple[int, int]] = field(default_factory=list)
    # list of (disk_count, gib_each) tuples — sum(count*each) >= rs_disk_gib

    # ---------- Phase 2b additions ----------
    # SKU least-cost match (L2.MATCH.001) — the actual Azure SKU shape that was
    # picked. May differ from rs_vcpus/rs_mem_gib if the retry loop bumped them.
    sku_name: str = ""
    sku_family: str = ""
    sku_processor: str = ""
    sku_vcpu: int = 0
    sku_memory_gib: int = 0

    # L2.RIGHTSIZE.CPU.RETRY — BA's iterative ±1 vCPU/+1 GiB memory bump loop.
    final_vcpus: int = 0                 # post-retry; equals sku_vcpu when matched
    final_mem_gib: int = 0
    retry_iterations: int = 0
    retry_path: str = "none"             # 'none' | 'decrement_vcpu' | 'increment_mem' | 'failed'
    match_failed: bool = False

    # L2.PRICING.001 — per-VM pricing matrix (5 offers, USD/hr each).
    pricing: dict = field(default_factory=dict)

    # L2.STORAGE_PRICE.001 — per-VM disk PAYG total (USD/year, LRS).
    storage_payg_usd_yr: float = 0.0
    disk_layout_priced: list[dict] = field(default_factory=list)
    # list of {tier, count, gib_each, usd_month_each, usd_yr_total}

    # KP.BA_SMALL_VM_FLOOR — BA's manual pad on undersized source VMs.
    # ba_floor_applied is True when the floor flag triggered for this VM.
    ba_floor_applied: bool = False
    effective_source_vcpu: int = 0       # max(vinfo_cpus, ba_min_vcpu_floor) | 0 == not set
    effective_source_mem_gib: float = 0.0  # max(vinfo_memory_mib/1024, ba_min_mem_gib_floor) | 0 == not set

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class Layer2Result:
    input_file: str
    strategy: str
    enforce_8vcpu_min_for_windows_server: bool
    reduction_params: dict
    region: str
    family_pin: str | None
    processor_pin: str | None
    per_vm: list[VMRecordL2]
    fyi_aggregates: dict
    rule_provenance: dict
    pricing_available: bool

    def to_dict(self) -> dict:
        return {
            "input_file": self.input_file,
            "strategy": self.strategy,
            "enforce_8vcpu_min_for_windows_server": self.enforce_8vcpu_min_for_windows_server,
            "reduction_params": self.reduction_params,
            "region": self.region,
            "family_pin": self.family_pin,
            "processor_pin": self.processor_pin,
            "pricing_available": self.pricing_available,
            "fyi_aggregates": self.fyi_aggregates,
            "rule_provenance": self.rule_provenance,
            "per_vm_count": len(self.per_vm),
            "per_vm_sample": [vm.to_dict() for vm in self.per_vm[:5]],
        }


# =============================================================================
# vPartition aggregation (per L2.RIGHTSIZE.STORAGE.001 + L1.INPUT.004 fallback)
# =============================================================================

@dataclass
class _VPartAgg:
    capacity_mib: float = 0.0
    consumed_mib: float = 0.0


def _build_vpartition_index(workbook_path: Path) -> tuple[dict[str, _VPartAgg], dict]:
    """Return ``({vm_name -> aggregate}, summary)`` from the vPartition tab.

    Aggregates ALL rows where Powerstate is poweredOn (Layer 2 scope).
    The VM name used is vPartition's own VM column — the L1 join may fail
    when vInfo names are redacted (per L1.INPUT.004); that is handled
    downstream by falling through to vInfo Total disk capacity for sizing
    and reporting an aggregate-only fallback for display totals.
    """
    summary = {"present": False, "vm_count": 0, "row_count": 0, "powered_on_row_count": 0}
    wb = _open_workbook(workbook_path)
    sheet_name = _find_sheet(wb, "vpartition")
    if sheet_name is None:
        return {}, summary

    summary["present"] = True
    ws = wb[sheet_name]
    headers = _read_headers(ws)

    vm_col = resolve_column(headers, "vm_name")
    cap_col = resolve_column(headers, "provisioned_storage", prefer_units=("mib",))
    if vm_col is None or cap_col is None:
        return {}, summary

    # Locate Powerstate and Consumed columns directly (no synonym needed for the L2 path).
    ps_idx = next(
        (i for i, h in enumerate(headers)
         if h.strip().lower() in ("powerstate", "power state")),
        None,
    )
    consumed_idx = next(
        (i for i, h in enumerate(headers)
         if h.strip().lower() in ("consumed mib", "consumed")),
        None,
    )

    by_vm: dict[str, _VPartAgg] = {}
    for row in _read_rows(ws):
        summary["row_count"] += 1
        if ps_idx is not None:
            ps_val = str(row[ps_idx] or "").strip().lower().replace(" ", "")
            if ps_val and ps_val != "poweredon":
                continue
        summary["powered_on_row_count"] += 1
        vm = row[vm_col.col_index]
        if vm in (None, ""):
            continue
        agg = by_vm.setdefault(str(vm), _VPartAgg())
        cap = row[cap_col.col_index]
        if isinstance(cap, (int, float)):
            agg.capacity_mib += float(cap)
        if consumed_idx is not None:
            con = row[consumed_idx]
            if isinstance(con, (int, float)):
                agg.consumed_mib += float(con)

    summary["vm_count"] = len(by_vm)
    return by_vm, summary


# =============================================================================
# vInfo "Disks" / "Total disk capacity MiB" columns (used by L2 storage chain)
# =============================================================================

def _read_vinfo_disk_columns(workbook_path: Path) -> dict[str, dict]:
    """Return ``{vm_name -> {disk_count, total_disk_capacity_mib}}``.

    Reads vInfo directly because the L1 VMRecord doesn't carry these.
    Used as the Tier-3 storage source (vPartition absent) AND as the
    multi-disk-decomposition hint (number of disks per VM).
    """
    wb = _open_workbook(workbook_path)
    sheet_name = _find_sheet(wb, "vinfo")
    if sheet_name is None:
        return {}
    ws = wb[sheet_name]
    headers = _read_headers(ws)

    vm_col = resolve_column(headers, "vm_name")
    if vm_col is None:
        return {}

    # 'Disks' (count) and 'Total disk capacity MiB' have stable names across RVTools versions.
    disks_idx = next(
        (i for i, h in enumerate(headers) if h.strip().lower() == "disks"),
        None,
    )
    tdc_idx = next(
        (i for i, h in enumerate(headers)
         if h.strip().lower() in ("total disk capacity mib", "total disk capacity")),
        None,
    )

    out: dict[str, dict] = {}
    for row in _read_rows(ws):
        vm = row[vm_col.col_index]
        if vm in (None, ""):
            continue
        rec = {"disk_count": 0, "total_disk_capacity_mib": 0.0}
        if disks_idx is not None and isinstance(row[disks_idx], (int, float)):
            rec["disk_count"] = int(row[disks_idx])
        if tdc_idx is not None and isinstance(row[tdc_idx], (int, float)):
            rec["total_disk_capacity_mib"] = float(row[tdc_idx])
        out[str(vm)] = rec
    return out


# =============================================================================
# Multi-disk decomposition (L2.RIGHTSIZE.STORAGE.MULTI_DISK)
# =============================================================================

def decompose_disks(rs_disk_gib: int, hint_disk_count: int) -> list[tuple[int, int]]:
    """Decompose a rightsized GiB total into ``[(count, gib_each), …]``.

    BA-confirmed methodology (Customer A Xa2-fixed audit 2026-04-27):
      - BA's XA2 GETMANAGEDDISK4 takes the source disk count as a HINT.
      - But BA sometimes REDUCES the disk count when consolidation produces
        a lower total provisioned size at a cheaper tier ladder.
        Example vm1591: source has 5 disks for 3,432 GiB. BA picked
        '4#E30 LRS' (4 × 1024 = 4096 GiB) instead of '5#E30 LRS'
        (5 × 1024 = 5120 GiB) — same per-disk tier, fewer disks, ~20%
        less total provisioned.

    Strategy:
      1. Start at hint_disk_count (max 1).
      2. Try counts in [hint_disk_count, hint_disk_count-1, ..., 1].
      3. For each count, compute per_disk_gib = snap_disk_gib(rs_disk_gib / count).
      4. Pick the count whose total_provisioned (count * per_disk) is LOWEST,
         with stable tie-break preferring fewer disks (lower count).
      5. If per_disk exceeds MAX_SINGLE_DISK_GIB, bump count up.
    """
    n_max = max(1, hint_disk_count) if hint_disk_count else 1

    candidates: list[tuple[int, int, int]] = []  # (total_gib, count, per_disk)
    # Also try counts ABOVE the hint when per-disk would exceed MAX_SINGLE_DISK_GIB.
    upper_bound = max(n_max, math.ceil(rs_disk_gib / MAX_SINGLE_DISK_GIB)) if rs_disk_gib > 0 else 1
    # We try hint_count and all values BELOW it down to 1 (consolidation),
    # plus values ABOVE it if needed to fit.
    for count in range(1, max(upper_bound, n_max) + 1):
        per_disk = snap_disk_gib(rs_disk_gib / count)
        if per_disk > MAX_SINGLE_DISK_GIB:
            continue  # not a valid layout
        total = count * per_disk
        if total >= rs_disk_gib:  # must satisfy capacity
            candidates.append((total, count, per_disk))

    if not candidates:
        # Fallback: bump count until per_disk fits.
        n = max(1, hint_disk_count)
        per_disk = snap_disk_gib(rs_disk_gib / n)
        while per_disk > MAX_SINGLE_DISK_GIB:
            n += 1
            per_disk = snap_disk_gib(rs_disk_gib / n)
        return [(n, per_disk)]

    # Pick lowest total_gib; tie-break by FEWER disks (closer to BA's
    # consolidation tendency).
    candidates.sort(key=lambda x: (x[0], x[1]))
    _, count, per_disk = candidates[0]
    return [(count, per_disk)]


# =============================================================================
# L2.MATCH.001 + L2.RIGHTSIZE.CPU.RETRY — SKU least-cost match with retry loop
# =============================================================================

# Maximum retry iterations per VM (safety bound; BA's manual workflow rarely
# exceeds 10–20 increments before giving up and accepting the cheapest).
_MAX_RETRY_ITERATIONS = 64

VALID_MATCHER_ALGORITHMS = (
    "ba_iterative_match",       # No-snap: BA's iterative ±1 vCPU / +1 GiB bump from raw source
    "ba_xa2_match",             # Snap-first (R&D Slide 7 snapCPU/snapMem) then iterative bump
    "pure_least_cost_no_arm",   # Absolute floor with over-spec ceilings (diagnostic only)
)

# Backward-compatible aliases. Older callers used `rd_rightsized` for the
# snap-first algorithm; the BA confirmed (28 Apr) it more accurately mirrors
# her XA2 spreadsheet's behavior so we renamed it. Both names continue to
# resolve to the same algorithm.
_LEGACY_MATCHER_ALIASES = {
    "ba_least_cost":    "ba_iterative_match",
    "pure_least_cost":  "pure_least_cost_no_arm",
    "rd_rightsized":    "ba_xa2_match",
}

# Exclusions per BA-confirmed audit of Customer A's Xa2-fixed picks (2026-04-27):
#   - 0 ARM picks  (BA: "customer's workloads aren't ARM-capable")
#   - 0 Spot/Low-Priority (XA2 default; already filtered upstream)
#   - 0 Confidential Compute (DC* and EC*)
#   - 0 picks of legacy v2/v3/v4 EXCEPT for families that only exist in those
#     versions: FX-only-v2, M-only-v3, HB-only-v4
#   - All families used: D, E, F, FX, HB, M
VERSION_ALLOWED_LATEST = {"v5", "v6", "v7"}
FAMILIES_WITH_LEGACY_ALLOWED = {
    # SKU prefix — if family STARTS with this, allow its legacy versions
    "FX":  {"v2"},          # FX-series only exists in v2
    "M":   {"v2", "v3"},    # M-series in v2 / v3 (not v5)
    "HB":  {"v3", "v4"},    # HPC HB-series in v3 / v4
    "HC":  {"v3"},
    "H":   {"v3"},
}

_ARM_PATTERN = re.compile(r"^standard_[def]\d+([a-z]*p[a-z]*)_v\d+$")
_DC_PATTERN  = re.compile(r"^standard_dc")     # Confidential Compute D-series
_EC_PATTERN  = re.compile(r"^standard_ec")     # Confidential Compute E-series
_VERSION_RE  = re.compile(r"_v(\d+)$")
_SKU_FAMILY_RE = re.compile(r"^standard_([A-Za-z]+?)\d")


def _sku_version(arm_name: str) -> str | None:
    m = _VERSION_RE.search(arm_name)
    return f"v{m.group(1)}" if m else None


def _sku_family_prefix(arm_name: str) -> str:
    """Return the LETTER prefix (e.g. 'D', 'E', 'FX', 'M', 'HB') of a SKU."""
    m = _SKU_FAMILY_RE.match(arm_name.lower())
    return m.group(1).upper() if m else ""

# Algorithm 1 / 3 knobs — BA-overridable per engagement.
_DEFAULT_VCPU_DECREMENT_FLOOR     = 1     # don't go below 1 vCPU
_DEFAULT_VCPU_DECREMENT_MAX_STEPS = 4     # don't strip more than 4 vCPU off original
_DEFAULT_MEM_INCREMENT_CAP_GIB    = 16    # never bump memory by more than 16 GiB

# Algorithm 2 knobs.
_DEFAULT_MAX_VCPU_OVERSPEC = 2.0   # picked sku.vcpu / source.cpu
_DEFAULT_MAX_MEM_OVERSPEC  = 3.0   # picked sku.memory_gib / source.mem_gib


def _is_excluded(sku: PricedSku) -> bool:
    """Per KP.LEAST_COST_BIZ_CASE: exclude ARM + Confidential + obsolete versions.

    Spot/Low-Priority are already filtered upstream by the API fetcher.
    Family pools (D/E/F/FX/HB/M) are NOT restricted here — BA confirmed
    her XA2 picks use HB and M when warranted.
    """
    n = sku.arm_sku_name.lower()
    if _ARM_PATTERN.search(n):
        return True
    if _DC_PATTERN.search(n) or _EC_PATTERN.search(n):
        return True
    # Version exclusion: only allow v5/v6/v7 by default,
    # except for families that only exist in older versions.
    ver = _sku_version(sku.arm_sku_name)
    if ver and ver not in VERSION_ALLOWED_LATEST:
        fam = _sku_family_prefix(sku.arm_sku_name)
        legacy_allowed = FAMILIES_WITH_LEGACY_ALLOWED.get(fam, set())
        if ver not in legacy_allowed:
            return True
    return False


def apply_matcher_filter(
    catalog: list[PricedSku],
    *,
    algorithm: str = "ba_iterative_match",
    family_pin: str | None = None,
    processor_pin: str | None = None,
) -> list[PricedSku]:
    """Reduce the catalog to the candidate set for the chosen algorithm.

    Per KP.LEAST_COST_BIZ_CASE:
      - ba_iterative_match:          exclude ARM + Confidential. No family bias.
                                      No snap; matcher's >= test handles the
                                      ladder. Decrement-vCPU / increment-mem
                                      bump on no-match (BA transcript 00:05:49).
      - ba_xa2_match (recommended):  same pool; SOURCE values are first snapped
                                      to the Azure ladder per R&D.SLIDE7.SNAPCPU
                                      and R&D.SLIDE8.SNAPMEM, mirroring BA's
                                      XA2 spreadsheet behavior.
      - pure_least_cost_no_arm:      diagnostic floor with overspec ceilings.
    """
    algorithm = _LEGACY_MATCHER_ALIASES.get(algorithm, algorithm)
    if algorithm not in VALID_MATCHER_ALGORITHMS:
        raise ValueError(f"algorithm={algorithm!r} not in {VALID_MATCHER_ALGORITHMS!r}")

    out = [s for s in catalog if s.payg_usd_hr > 0 and not _is_excluded(s)]

    if family_pin:
        family_map = {
            "GeneralPurpose":          {"D"},
            "ComputeOptimized":        {"F"},
            "MemoryOptimized":         {"E", "M"},
            "HighPerformanceCompute":  {"H"},
            "StorageOptimized":        {"L"},
            "GPU":                     {"N"},
            "FPGAInstances":           {"NP"},
        }
        wanted = family_map.get(family_pin, {family_pin})
        out = [s for s in out if s.family in wanted]
    if processor_pin:
        out = [s for s in out if s.processor.lower() == processor_pin.lower()]
    return out


# Backward-compatible alias for tests that import _filter_sku_candidates.
def _filter_sku_candidates(
    catalog: list[PricedSku],
    *,
    family_pin: str | None = None,
    processor_pin: str | None = None,
) -> list[PricedSku]:
    return apply_matcher_filter(
        catalog,
        algorithm="ba_iterative_match",
        family_pin=family_pin,
        processor_pin=processor_pin,
    )


def find_least_cost_sku(
    target_vcpus: int,
    target_mem_gib: int,
    candidates: list[PricedSku],
    *,
    max_vcpu_overspec: float | None = None,
    max_mem_overspec: float | None = None,
) -> PricedSku | None:
    """L2.MATCH.001 — pick the lowest PAYG SKU that satisfies BOTH constraints.

    When ``max_vcpu_overspec`` and/or ``max_mem_overspec`` are set, the
    returned SKU must ALSO satisfy:
        sku.vcpu       <= target_vcpus    * max_vcpu_overspec
        sku.memory_gib <= target_mem_gib  * max_mem_overspec
    These ceilings keep the floor-price algorithm from picking absurdly
    over-provisioned SKUs (e.g. an E96 for a 4-vCPU source).

    Tie-break: when two SKUs share the cheapest PAYG, prefer the one with
    the HIGHER version (v7 > v6 > v5). This matches BA's XA2 picks where
    AMD-lite v7 is preferred over v6 at identical price.

    Returns None when no SKU in the candidate set satisfies the constraints.
    """
    viable = [
        s for s in candidates
        if s.vcpu >= target_vcpus and s.memory_gib >= target_mem_gib
    ]
    if max_vcpu_overspec is not None:
        ceiling = target_vcpus * max_vcpu_overspec
        viable = [s for s in viable if s.vcpu <= ceiling]
    if max_mem_overspec is not None:
        ceiling = target_mem_gib * max_mem_overspec
        viable = [s for s in viable if s.memory_gib <= ceiling]
    if not viable:
        return None
    # Sort by (price asc, then version desc) so the cheapest+newest wins.
    def _sort_key(s: PricedSku) -> tuple:
        v = _sku_version(s.arm_sku_name) or "v0"
        v_num = int(v[1:])
        return (s.payg_usd_hr, -v_num)
    return min(viable, key=_sort_key)


def match_with_retry(
    rs_vcpus: int,
    rs_mem_gib: int,
    *,
    min_vcpus: int,
    candidates: list[PricedSku],
    vcpu_decrement_floor: int = _DEFAULT_VCPU_DECREMENT_FLOOR,
    vcpu_decrement_max_steps: int = _DEFAULT_VCPU_DECREMENT_MAX_STEPS,
    mem_increment_cap_gib: int = _DEFAULT_MEM_INCREMENT_CAP_GIB,
) -> tuple[PricedSku | None, int, int, str, int]:
    """L2.RIGHTSIZE.CPU.RETRY — BA's iterative bump until a SKU matches.

    Returns ``(sku, final_vcpus, final_mem_gib, retry_path, iterations)``.

    Algorithm (BA-confirmed from Layer 2 transcript 00:05:49 / 00:08:09 /
    00:11:29 / 00:12:07; integer-step semantics confirmed by user 2026-04-27):

      1. Try (rs_vcpus, rs_mem_gib). XA2-equivalent semantics: the matcher
         returns the cheapest SKU whose vcpu >= target AND memory_gib >=
         target. This means asking for 3 vCPU may return a 4-core SKU
         because 4 >= 3 (XA2 has its own tolerance window).

      2. If no match, DECREMENT vCPU by ONE INTEGER at a time (not by
         ladder rung). BA explicit cue: 'one vCPU at a time until I got a
         successful match' (00:06:10). The matcher's >= test handles the
         ladder — asking for 13 vCPU on a D-family with rungs (8, 16) will
         return D16. We stop the moment a SKU is returned. Floor is
         max(min_vcpus, vcpu_decrement_floor); also bounded by
         vcpu_decrement_max_steps below the original.

      3. If still no match (vCPU exhausted), INCREMENT memory by 1 GiB at
         a time. BA explicit cue: 'incrementing on a one GB basis until I
         got the first least cost Azure virtual machine match' (00:12:07).
         BA bias is AGAINST this (00:06:49: 'we would end up over
         inflating'); so cap at mem_increment_cap_gib.

      4. If exhausted, return None (caller flags match_failed=True).
    """
    # Step 1: initial try.
    sku = find_least_cost_sku(rs_vcpus, rs_mem_gib, candidates)
    if sku:
        return sku, rs_vcpus, rs_mem_gib, "none", 0

    iters = 0
    floor = max(min_vcpus, vcpu_decrement_floor)
    min_vcpu_attempt = max(floor, rs_vcpus - vcpu_decrement_max_steps)

    # Step 2: decrement vCPU by ONE INTEGER at a time.
    cur_vcpus = rs_vcpus
    while cur_vcpus > min_vcpu_attempt:
        cur_vcpus -= 1
        iters += 1
        if iters >= _MAX_RETRY_ITERATIONS:
            break
        sku = find_least_cost_sku(cur_vcpus, rs_mem_gib, candidates)
        if sku:
            return sku, cur_vcpus, rs_mem_gib, "decrement_vcpu", iters

    # Step 3: increment memory by 1 GiB at a time.
    cur_mem = rs_mem_gib
    max_mem = rs_mem_gib + mem_increment_cap_gib
    while cur_mem < max_mem:
        cur_mem += 1
        iters += 1
        if iters >= _MAX_RETRY_ITERATIONS:
            break
        sku = find_least_cost_sku(rs_vcpus, cur_mem, candidates)
        if sku:
            return sku, rs_vcpus, cur_mem, "increment_mem", iters

    # Step 4: failed.
    return None, rs_vcpus, rs_mem_gib, "failed", iters


# =============================================================================
# L2.STORAGE_PRICE.001 — LRS managed-disk pricing
# =============================================================================

def _disk_tier_for_size(
    gib: int,
    priced_disks: list[PricedDisk],
) -> PricedDisk | None:
    """Return the smallest LRS disk tier whose capacity ≥ ``gib``."""
    viable = [d for d in priced_disks if d.gib >= gib]
    if not viable:
        return None
    return min(viable, key=lambda d: d.gib)


def price_disk_layout(
    disk_layout: list[tuple[int, int]],
    priced_disks: list[PricedDisk],
) -> tuple[float, list[dict]]:
    """Return ``(total_usd_yr, [{tier, count, gib_each, usd_month_each, usd_yr_total}])``.

    For each (count, gib_each) in disk_layout: pick the smallest LRS tier
    that fits gib_each, multiply by count, sum over the layout.
    """
    total = 0.0
    detail: list[dict] = []
    for count, gib_each in disk_layout:
        tier = _disk_tier_for_size(gib_each, priced_disks)
        if tier is None:
            detail.append({
                "tier": "NONE",
                "count": count,
                "gib_each": gib_each,
                "usd_month_each": 0.0,
                "usd_yr_total": 0.0,
                "unpriced": True,
            })
            continue
        usd_yr = tier.usd_month * 12.0 * count
        total += usd_yr
        detail.append({
            "tier": tier.sku_name,
            "count": count,
            "gib_each": gib_each,
            "usd_month_each": tier.usd_month,
            "usd_yr_total": round(usd_yr, 2),
        })
    return round(total, 2), detail


# =============================================================================
# Per-VM right-sizing dispatcher
# =============================================================================

def _rightsize_vm(
    vm: L1VMRecord,
    *,
    strategy: str,
    enforce_8vcpu_min: bool,
    reduction: dict,
    vpart_by_vm: dict[str, _VPartAgg],
    vinfo_disk_index: dict[str, dict],
    sku_candidates: list[PricedSku] | None = None,
    priced_disks: list[PricedDisk] | None = None,
    matcher_algorithm: str = "ba_iterative_match",
    max_vcpu_overspec: float | None = None,
    max_mem_overspec: float | None = None,
    ba_min_vcpu_floor: int | None = None,
    ba_min_memory_gib_floor: int | None = None,
) -> VMRecordL2:
    """Apply the strategy-driven R&D formulas to a single VM."""

    min_vcpus, vcpu_floor_source = resolve_min_vcpus(vm, enforce_8vcpu_min)

    # KP.BA_SMALL_VM_FLOOR — pad undersized source VMs (BA judgment-overlay).
    # Defaults OFF (None). When set, clamps the source vCPU and memory floor
    # BEFORE the rightsizing formulas run. Annotated on the per-VM record so
    # the BA can audit which VMs were padded vs raw.
    raw_source_vcpu = vm.vcpu
    raw_source_mem_mib = (
        vm.vinfo_memory_mib if hasattr(vm, "vinfo_memory_mib")
        else vm.memory_gb_decimal * 1024.0
    )
    floored_vcpu = (
        max(raw_source_vcpu, ba_min_vcpu_floor)
        if ba_min_vcpu_floor is not None else raw_source_vcpu
    )
    floored_mem_mib = (
        max(raw_source_mem_mib, ba_min_memory_gib_floor * 1024.0)
        if ba_min_memory_gib_floor is not None else raw_source_mem_mib
    )
    ba_floor_applied = (
        floored_vcpu != raw_source_vcpu or floored_mem_mib != raw_source_mem_mib
    )

    # Whether to apply R&D snap ladders (snapCPU/snapMem/snapDisk).
    # XA2 snap behavior. BA-confirmed (28 Apr 2026) that her XA2 spreadsheet
    # snaps SOURCE values to the Azure ladder (R&D.SLIDE7.SNAPCPU,
    # R&D.SLIDE8.SNAPMEM) BEFORE the API lookup. The `ba_xa2_match` algorithm
    # turns this on; the older `ba_iterative_match` leaves it off so the
    # matcher's `>= target` test handles the ladder via raw source values.
    #   - matcher_algorithm == 'ba_xa2_match'         → snap ON
    #   - matcher_algorithm == 'ba_iterative_match'   → snap OFF
    #   - matcher_algorithm == 'pure_least_cost_no_arm' → snap OFF
    apply_rd_ladder = (matcher_algorithm == "ba_xa2_match")

    # ---------- CPU ----------
    if strategy == "host_proxy" and vm.host_proxy_cpu_pct is not None:
        rs_vcpus = rd_slide7_cpu_default(floored_vcpu, vm.host_proxy_cpu_pct, min_vcpus)
        cpu_branch = "rd_slide7_cpu_default"
    elif strategy == "per_vm_telemetry" and vm.cpu_util_pct is not None:
        rs_vcpus = rd_slide7_cpu_user(
            floored_vcpu, max(0.0, 100.0 - float(vm.cpu_util_pct)), min_vcpus
        )
        cpu_branch = "rd_slide7_cpu_user_per_vm_telemetry"
    elif strategy == "like_for_like":
        # Pass raw source CPU; matcher handles the ladder via >= test.
        # When apply_rd_ladder is True, snap to ladder first (ba_xa2_match).
        rs_vcpus = (
            rd_slide7_cpu_user(floored_vcpu, 0.0, min_vcpus)
            if apply_rd_ladder
            else max(min_vcpus, floored_vcpu)
        )
        cpu_branch = "rd_slide7_cpu_user_like_for_like" if apply_rd_ladder else "raw_like_for_like"
    elif strategy == "ba_fallback":
        # ba_fallback ALWAYS snaps (it's an R&D-formulated reduction).
        rs_vcpus = rd_slide7_cpu_user(floored_vcpu, reduction["cpu_reduction_pct"], min_vcpus)
        cpu_branch = "rd_slide7_cpu_user_ba_fallback"
    elif strategy == "flat_reduction":
        # flat_reduction follows the R&D formula whether snapped or not.
        rs_vcpus = rd_slide7_cpu_user(floored_vcpu, reduction["cpu_reduction_pct"], min_vcpus)
        cpu_branch = "rd_slide7_cpu_user_flat_reduction"
    else:
        rs_vcpus = (
            rd_slide7_cpu_user(floored_vcpu, 0.0, min_vcpus)
            if apply_rd_ladder
            else max(min_vcpus, floored_vcpu)
        )
        cpu_branch = "raw_fallback_no_host_data"

    if ba_floor_applied and floored_vcpu != raw_source_vcpu:
        cpu_branch = cpu_branch + "+ba_small_vm_floor"

    # ---------- Memory ----------
    # vInfo Memory is conventionally MB in RVTools (see L1.UNITS.002).
    # The L1 replica stored memory_gb_decimal computed from /1024 — recover MiB
    # equivalent via *1024 for the R&D formulas (sizing math is binary).
    # When KP.BA_SMALL_VM_FLOOR is ON, the floored value is used instead of raw.
    vinfo_mem_mib = floored_mem_mib

    if strategy == "per_vm_telemetry" and vm.mem_util_pct is not None:
        # Approximate vMemory[Consumed] from util_pct × provisioned (fallback when
        # the vMemory tab is absent but per-VM utilisation IS available).
        consumed_mib = vinfo_mem_mib * (float(vm.mem_util_pct) / 100.0)
        rs_mem_gib = rd_slide8_memory_default(consumed_mib, reduction["mem_buffer_pct"], 1)
        memory_branch = "rd_slide8_memory_default_per_vm_telemetry"
    elif strategy == "like_for_like" or strategy == "host_proxy":
        # Pass raw source memory; matcher handles ladder via >= test.
        # Convert MiB to GiB (binary) without snap unless ba_xa2_match.
        if apply_rd_ladder:
            rs_mem_gib = rd_slide8_memory_user(vinfo_mem_mib, 0.0, 0.0, 1)
        else:
            rs_mem_gib = max(1, math.ceil(vinfo_mem_mib / 1024.0))
        memory_branch = (
            "rd_slide8_memory_user_like_for_like" if apply_rd_ladder
            else "raw_like_for_like"
        )
    elif strategy == "ba_fallback":
        rs_mem_gib = rd_slide8_memory_user(
            vinfo_mem_mib,
            reduction["mem_reduction_pct"],
            reduction["mem_buffer_pct"],
            1,
        )
        memory_branch = "rd_slide8_memory_user_ba_fallback"
    else:  # flat_reduction
        rs_mem_gib = rd_slide8_memory_user(
            vinfo_mem_mib,
            reduction["mem_reduction_pct"],
            reduction["mem_buffer_pct"],
            1,
        )
        memory_branch = "rd_slide8_memory_user_flat_reduction"

    if ba_floor_applied and floored_mem_mib != raw_source_mem_mib:
        memory_branch = memory_branch + "+ba_small_vm_floor"

    # ---------- Storage ----------
    # Per BA Customer A audit (2026-04-27 + transcript 00:17:31): the BA's
    # XA2 GETMANAGEDDISK4 is sized on vInfo[In Use MiB] / 1024 (the "In Use
    # GB (storage)" column) for like_for_like — NOT on vPartition. The
    # BA explicitly says "this utilized storage is what is going into the
    # matching of a managed disk" at 00:17:31.
    vpart = vpart_by_vm.get(vm.name)
    vinfo_disk_rec = vinfo_disk_index.get(vm.name, {})
    vinfo_total_disk_mib = float(vinfo_disk_rec.get("total_disk_capacity_mib", 0.0))
    disk_count_hint = int(vinfo_disk_rec.get("disk_count", 0))
    # Per-VM In Use GB from L1 (vInfo In Use MiB / 1024)
    vm_in_use_gib = float(getattr(vm, "in_use_gb_decimal", 0.0) or 0.0)
    # Note: L1 stored decimal-GB (/953.674); for disk sizing we want binary GiB
    # (/1024) per BA's =BB2/1024 formula. Convert back: gib = gb * 953.674 / 1024.
    vm_in_use_gib_binary = vm_in_use_gib * 953.674 / 1024.0 if vm_in_use_gib > 0 else 0.0

    # Strategy dispatch
    if strategy in ("per_vm_telemetry", "like_for_like") and vm_in_use_gib_binary > 0:
        # BA-canonical path: vInfo In Use, snapped per-disk per source disk count.
        rs_disk_gib = max(DISK_TIER_LADDER_GIB[0], math.ceil(vm_in_use_gib_binary))
        storage_branch = "rd_slide9_storage_default_in_use"
        storage_source_tier = "vinfo_in_use"
    elif vpart and vpart.consumed_mib > 0 and strategy in ("per_vm_telemetry", "like_for_like"):
        rs_disk_gib = rd_slide9_storage_default(vpart.consumed_mib, reduction["storage_buffer_pct"])
        storage_branch = "rd_slide9_storage_default"
        storage_source_tier = "vpartition_consumed"
    elif vpart and vpart.capacity_mib > 0:
        rs_disk_gib = rd_slide9_storage_capacity(
            vpart.capacity_mib,
            reduction["storage_reduction_pct"],
            reduction["storage_buffer_pct"],
        )
        storage_branch = "rd_slide9_storage_capacity"
        storage_source_tier = "vpartition_capacity"
    elif vinfo_total_disk_mib > 0:
        rs_disk_gib = rd_slide9_storage_vinfo(
            vinfo_total_disk_mib,
            reduction["storage_reduction_pct"],
            reduction["storage_buffer_pct"],
        )
        storage_branch = "rd_slide9_storage_vinfo"
        storage_source_tier = "vinfo_total"
    else:
        rs_disk_gib = DISK_TIER_LADDER_GIB[0]
        storage_branch = "rd_slide9_storage_floor_only"
        storage_source_tier = "unjoined"

    disk_layout = decompose_disks(rs_disk_gib, disk_count_hint)

    # ---------- Phase 2b: SKU least-cost match + retry + pricing ----------
    sku_name = sku_family = sku_processor = ""
    sku_vcpu = sku_memory_gib = 0
    final_vcpus, final_mem_gib = rs_vcpus, rs_mem_gib
    retry_iterations = 0
    retry_path = "none"
    match_failed = False
    pricing: dict = {offer: {"usd_hr": 0.0, "usd_yr": 0.0, "available": False} for offer in PRICING_OFFERS}
    storage_payg_usd_yr = 0.0
    disk_layout_priced: list[dict] = []

    if sku_candidates:
        # Algorithm 2 'pure_least_cost_no_arm' uses over-spec ceilings to
        # prevent picking grossly over-provisioned cheap SKUs (e.g. an E96
        # for a 4-vCPU source). Algorithms 1 and 3 do not (they iterate
        # via decrement/increment instead).
        if matcher_algorithm == "pure_least_cost_no_arm":
            sku = find_least_cost_sku(
                rs_vcpus, rs_mem_gib, sku_candidates,
                max_vcpu_overspec=max_vcpu_overspec,
                max_mem_overspec=max_mem_overspec,
            )
            # Relax ceilings one step at a time if no match.
            ovs_v = max_vcpu_overspec or _DEFAULT_MAX_VCPU_OVERSPEC
            ovs_m = max_mem_overspec or _DEFAULT_MAX_MEM_OVERSPEC
            relaxations = 0
            while sku is None and relaxations < 6:
                relaxations += 1
                ovs_v += 0.5
                ovs_m += 0.5
                sku = find_least_cost_sku(
                    rs_vcpus, rs_mem_gib, sku_candidates,
                    max_vcpu_overspec=ovs_v,
                    max_mem_overspec=ovs_m,
                )
            fv, fm = rs_vcpus, rs_mem_gib
            rp = "none" if relaxations == 0 else "overspec_relaxed"
            ri = relaxations
        else:
            sku, fv, fm, rp, ri = match_with_retry(
                rs_vcpus, rs_mem_gib,
                min_vcpus=min_vcpus,
                candidates=sku_candidates,
            )
        retry_iterations = ri
        retry_path = rp
        final_vcpus = fv
        final_mem_gib = fm
        if sku is None:
            match_failed = True
        else:
            sku_name = sku.arm_sku_name
            sku_family = sku.family
            sku_processor = sku.processor
            sku_vcpu = sku.vcpu
            sku_memory_gib = sku.memory_gib
            # Per the VMRecordL2 spec: final_vcpus / final_mem_gib reflect
            # the SKU actually deployed (post-snap), not the query target.
            # When the matcher returns SKUs that are larger than the query
            # (e.g. D16 for a 12-vCPU target), the deployed shape governs.
            final_vcpus = sku.vcpu
            final_mem_gib = sku.memory_gib
            for offer in PRICING_OFFERS:
                entry = sku.pricing.get(offer, {})
                if entry.get("available"):
                    pricing[offer] = {
                        "usd_hr": entry["usd_hr"],
                        "usd_yr": round(entry["usd_hr"] * 8760.0, 2),
                        "available": True,
                    }

    if priced_disks:
        storage_payg_usd_yr, disk_layout_priced = price_disk_layout(disk_layout, priced_disks)

    return VMRecordL2(
        name=vm.name,
        is_powered_on=vm.is_powered_on,
        is_template=vm.is_template,
        vinfo_cpus=vm.vcpu,
        vinfo_memory_mib=vinfo_mem_mib,
        os_config=vm.os_config,
        os_tools=vm.os_tools,
        min_vcpus=min_vcpus,
        vcpu_floor_source=vcpu_floor_source,
        ba_floor_applied=ba_floor_applied,
        effective_source_vcpu=int(floored_vcpu),
        effective_source_mem_gib=floored_mem_mib / 1024.0,
        rs_vcpus=rs_vcpus,
        rs_mem_gib=rs_mem_gib,
        rs_disk_gib=rs_disk_gib,
        cpu_branch=cpu_branch,
        memory_branch=memory_branch,
        storage_branch=storage_branch,
        storage_source_tier=storage_source_tier,
        disk_layout=disk_layout,
        sku_name=sku_name,
        sku_family=sku_family,
        sku_processor=sku_processor,
        sku_vcpu=sku_vcpu,
        sku_memory_gib=sku_memory_gib,
        final_vcpus=final_vcpus,
        final_mem_gib=final_mem_gib,
        retry_iterations=retry_iterations,
        retry_path=retry_path,
        match_failed=match_failed,
        pricing=pricing,
        storage_payg_usd_yr=storage_payg_usd_yr,
        disk_layout_priced=disk_layout_priced,
    )


# =============================================================================
# Top-level entry point
# =============================================================================

def replicate_layer2(
    workbook_path: Path,
    *,
    strategy: str = "like_for_like",
    enforce_8vcpu_min_for_windows_server: bool = False,
    cpu_reduction_pct: float = 0.0,
    mem_reduction_pct: float = 0.0,
    mem_buffer_pct: float = 0.0,
    storage_reduction_pct: float = 0.0,
    storage_buffer_pct: float = 0.0,
    region: str = "eastus2",
    family_pin: str | None = None,
    processor_pin: str | None = None,
    enable_pricing: bool = True,
    matcher_algorithm: str = "ba_iterative_match",
    disk_class: str = "Standard",
    max_vcpu_overspec: float = _DEFAULT_MAX_VCPU_OVERSPEC,
    max_mem_overspec: float = _DEFAULT_MAX_MEM_OVERSPEC,
    ba_min_vcpu_floor: int | None = None,
    ba_min_memory_gib_floor: int | None = None,
    layer1_result: Layer1Result | None = None,
) -> Layer2Result:
    """Run Layer 2 right-sizing PER VM and return the per-VM payload + aggregates.

    When ``enable_pricing`` is True (default) the replica also performs SKU
    least-cost match (L2.MATCH.001), the BA's iterative bump retry loop
    (L2.RIGHTSIZE.CPU.RETRY) and the 5-offer pricing matrix
    (L2.PRICING.001 + L2.STORAGE_PRICE.001). When pricing is disabled or the
    catalog cache is empty, the replica still produces the rightsizing
    triple (vCPU, mem, disk) per the R&D formulas — useful for offline
    diagnostics.

    ``family_pin`` (e.g. ``'GeneralPurpose'``) and ``processor_pin``
    (``'Intel'`` | ``'AMD'`` | ``'ARM'``) implement R&D.OPTIONAL.FAMILY_PROCESSOR.
    """

    if strategy not in VALID_STRATEGIES:
        raise ValueError(
            f"strategy={strategy!r} not in {VALID_STRATEGIES!r}"
        )

    # Resolve legacy aliases so the rest of the pipeline works with the
    # canonical name. (e.g. 'rd_rightsized' -> 'ba_xa2_match')
    matcher_algorithm = _LEGACY_MATCHER_ALIASES.get(matcher_algorithm, matcher_algorithm)

    # Layer 1 supplies per_vm[] (vInfo identity, decoded OS, etc.)
    if layer1_result is None:
        layer1_result = replicate_layer1(workbook_path)

    # vPartition aggregation (powered-on only) — independent of Layer 1's join.
    vpart_by_vm, vpart_summary = _build_vpartition_index(workbook_path)

    # vInfo['Disks', 'Total disk capacity MiB'] — for tier-3 fallback + multi-disk hint.
    vinfo_disk_index = _read_vinfo_disk_columns(workbook_path)

    reduction = _resolve_reduction_params(
        strategy,
        cpu_reduction_pct=cpu_reduction_pct,
        mem_reduction_pct=mem_reduction_pct,
        mem_buffer_pct=mem_buffer_pct,
        storage_reduction_pct=storage_reduction_pct,
        storage_buffer_pct=storage_buffer_pct,
    )

    # ---------- Phase 2b: load priced SKU + disk catalogs ----------
    sku_candidates: list[PricedSku] = []
    priced_disks: list[PricedDisk] = []
    pricing_available = False
    if enable_pricing:
        full_catalog = get_priced_vm_catalog(region)
        sku_candidates = apply_matcher_filter(
            full_catalog,
            algorithm=matcher_algorithm,
            family_pin=family_pin,
            processor_pin=processor_pin,
        )
        priced_disks = get_priced_disk_catalog(region, ssd_class=disk_class)
        pricing_available = bool(sku_candidates) and bool(priced_disks)
        _log.info(
            "Pricing for %s: %d viable SKUs (algo=%s, family=%s, processor=%s, disk=%s SSD), %d disk tiers",
            region, len(sku_candidates), matcher_algorithm, family_pin, processor_pin, disk_class, len(priced_disks),
        )

    # Per-VM rightsizing — powered-on, non-template scope per L2.SCOPE.001
    per_vm: list[VMRecordL2] = []
    for vm in layer1_result.per_vm:
        if not vm.is_powered_on or vm.is_template:
            continue
        per_vm.append(_rightsize_vm(
            vm,
            strategy=strategy,
            enforce_8vcpu_min=enforce_8vcpu_min_for_windows_server,
            reduction=reduction,
            vpart_by_vm=vpart_by_vm,
            vinfo_disk_index=vinfo_disk_index,
            sku_candidates=sku_candidates if pricing_available else None,
            priced_disks=priced_disks if pricing_available else None,
            matcher_algorithm=matcher_algorithm,
            max_vcpu_overspec=max_vcpu_overspec,
            max_mem_overspec=max_mem_overspec,
            ba_min_vcpu_floor=ba_min_vcpu_floor,
            ba_min_memory_gib_floor=ba_min_memory_gib_floor,
        ))

    # ---------- FYI aggregates (KP.PER_VM_REPRISE — sum of per-VM only) ----------
    sum_vcpus = sum(vm.rs_vcpus for vm in per_vm)
    sum_mem_gib = sum(vm.rs_mem_gib for vm in per_vm)
    sum_disk_gib_per_vm = sum(vm.rs_disk_gib for vm in per_vm)

    # Phase 2b: post-retry final values (these are what the BA's spreadsheet
    # actually reports because she manually adjusted to a viable SKU shape).
    sum_final_vcpus = sum(vm.final_vcpus for vm in per_vm if vm.sku_name) or sum_vcpus
    sum_final_mem_gib = sum(vm.final_mem_gib for vm in per_vm if vm.sku_name) or sum_mem_gib
    # Sum of MATCHED SKU shapes — the values that actually drive Azure billing.
    sum_sku_vcpus = sum(vm.sku_vcpu for vm in per_vm if vm.sku_name)
    sum_sku_mem_gib = sum(vm.sku_memory_gib for vm in per_vm if vm.sku_name)

    # Storage has a special aggregator: when per-VM join failed (storage_source_tier
    # == 'unjoined' for many VMs because vInfo names are redacted), the sum-of-per-VM
    # is misleadingly small. The BA's spreadsheet computes the storage total from
    # vPartition's OWN VM identity, snapped per VM, decimal-GB convention. Compute
    # both that aggregate AND the per-VM-join aggregate; downstream picks the
    # better one based on join coverage.
    sum_disk_gib_vpartition_intrinsic_decimal = sum(
        snap_disk_gib(agg.capacity_mib / _STORAGE_MIB_PER_GB_DECIMAL)
        for agg in vpart_by_vm.values()
        if agg.capacity_mib > 0
    )
    sum_disk_gib_vpartition_intrinsic_binary = sum(
        snap_disk_gib(agg.capacity_mib / _STORAGE_MIB_PER_GIB_BINARY)
        for agg in vpart_by_vm.values()
        if agg.capacity_mib > 0
    )

    # Multi-disk total = sum across all per-VM disk_layout entries
    sum_disk_gib_with_layout = sum(
        sum(count * gib_each for count, gib_each in vm.disk_layout)
        for vm in per_vm
    )

    floor_breakdown = {
        "windows_compliance": sum(1 for vm in per_vm if vm.vcpu_floor_source == "windows_compliance"),
        "linux_or_unknown":   sum(1 for vm in per_vm if vm.vcpu_floor_source == "linux_or_unknown"),
        "flag_off":           sum(1 for vm in per_vm if vm.vcpu_floor_source == "flag_off"),
    }
    storage_tier_breakdown = {
        "vpartition_consumed":  sum(1 for vm in per_vm if vm.storage_source_tier == "vpartition_consumed"),
        "vpartition_capacity":  sum(1 for vm in per_vm if vm.storage_source_tier == "vpartition_capacity"),
        "vinfo_total":          sum(1 for vm in per_vm if vm.storage_source_tier == "vinfo_total"),
        "unjoined":             sum(1 for vm in per_vm if vm.storage_source_tier == "unjoined"),
    }
    retry_breakdown = {
        "none":            sum(1 for vm in per_vm if vm.retry_path == "none"),
        "decrement_vcpu":  sum(1 for vm in per_vm if vm.retry_path == "decrement_vcpu"),
        "increment_mem":   sum(1 for vm in per_vm if vm.retry_path == "increment_mem"),
        "failed":          sum(1 for vm in per_vm if vm.match_failed),
    }

    # ---------- Phase 2b: storage pricing from vPartition intrinsic aggregation ----------
    # When the per-VM vInfo<->vPartition join fails (redacted names; per
    # L1.INPUT.004), the per-VM disk_layout above is built from vInfo Total
    # which over-counts the floor (every VM gets a 128 GiB disk regardless of
    # actual provisioning). For an accurate fleet $$ aggregate we ALSO price
    # the vPartition intrinsic layout (per vPartition's own VM identity).
    # This matches the BA's spreadsheet totals.
    sum_storage_payg_usd_yr_intrinsic = 0.0
    if pricing_available:
        for agg in vpart_by_vm.values():
            if agg.capacity_mib <= 0:
                continue
            gib = snap_disk_gib(agg.capacity_mib / _STORAGE_MIB_PER_GB_DECIMAL)
            layout = decompose_disks(gib, 0)
            usd_yr, _ = price_disk_layout(layout, priced_disks)
            sum_storage_payg_usd_yr_intrinsic += usd_yr
        sum_storage_payg_usd_yr_intrinsic = round(sum_storage_payg_usd_yr_intrinsic, 2)

    # Pricing aggregates per L2.PRICING.001 — $/yr per offer, summed across VMs.
    pricing_totals_usd_yr = {
        offer: round(sum(vm.pricing.get(offer, {}).get("usd_yr", 0.0) for vm in per_vm), 2)
        for offer in PRICING_OFFERS
    }
    sum_storage_payg_usd_yr = round(sum(vm.storage_payg_usd_yr for vm in per_vm), 2)

    # Reference-rate pricing alongside tier-based (BA's Customer A methodology).
    # Per KP.PER_VM_REPRISE these are computed PER VM and summed — NEVER as
    # fleet_total_vcpu * rate. The two methodologies (tier-based vs reference-
    # rate) are surfaced so the BA can choose which to commit to per engagement.
    HOURS_YR = 8760.0
    rate_vcpu = REFERENCE_RATES["vcpu_usd_hr"]
    rate_gb_mo = REFERENCE_RATES["gb_usd_month"]
    azure_compute_ref_rate_usd_yr = {
        # Pre-match (R&D rightsized vCPU per VM × rate × hours)
        "pre_match_vcpu":      round(sum(vm.rs_vcpus    * rate_vcpu * HOURS_YR for vm in per_vm), 2),
        # Post-match (matched-SKU vCPU shape per VM × rate × hours)
        "post_match_sku_vcpu": round(
            sum(vm.sku_vcpu * rate_vcpu * HOURS_YR for vm in per_vm if vm.sku_name),
            2,
        ),
    }
    # Storage ref-rate: per-VM disk_layout GiB × rate × 12, summed.
    # ALSO compute the vPartition-intrinsic flavour per-VM (one PER vPartition VM identity).
    storage_ref_rate_per_vm_join = round(
        sum(
            sum(count * gib for count, gib in vm.disk_layout) * rate_gb_mo * 12
            for vm in per_vm
        ),
        2,
    )
    storage_ref_rate_vpartition_intrinsic = 0.0
    for agg in vpart_by_vm.values():
        if agg.capacity_mib <= 0:
            continue
        per_vm_gib = snap_disk_gib(agg.capacity_mib / _STORAGE_MIB_PER_GB_DECIMAL)
        storage_ref_rate_vpartition_intrinsic += per_vm_gib * rate_gb_mo * 12
    azure_storage_ref_rate_usd_yr = {
        "per_vm_join_gib":          storage_ref_rate_per_vm_join,
        "vpartition_intrinsic_gib": round(storage_ref_rate_vpartition_intrinsic, 2),
    }

    fyi = {
        # Right-sized triple (R&D formulas only; what each VM 'should' be)
        "azure_vcpu_total": sum_vcpus,
        "azure_memory_gib_total": sum_mem_gib,
        "azure_storage_gib_per_vm_join": sum_disk_gib_per_vm,
        "azure_storage_gib_per_vm_with_layout": sum_disk_gib_with_layout,
        # Canonical default per BA Customer A baseline 2026-04-27 — decimal GB.
        "azure_storage_gib_vpartition_intrinsic": sum_disk_gib_vpartition_intrinsic_decimal,
        "azure_storage_gib_vpartition_intrinsic_binary": sum_disk_gib_vpartition_intrinsic_binary,
        # Phase 2b: post-retry shapes (what the BA actually uses for billing)
        "azure_vcpu_total_post_retry": sum_final_vcpus,
        "azure_memory_gib_total_post_retry": sum_final_mem_gib,
        "azure_vcpu_total_matched_sku_shape": sum_sku_vcpus,
        "azure_memory_gib_total_matched_sku_shape": sum_sku_mem_gib,
        # Pricing aggregates
        "azure_compute_usd_yr": pricing_totals_usd_yr,    # one entry per offer (tier-based)
        "azure_compute_ref_rate_usd_yr": azure_compute_ref_rate_usd_yr,
        "azure_storage_payg_usd_yr": sum_storage_payg_usd_yr,
        "azure_storage_payg_usd_yr_vpartition_intrinsic": sum_storage_payg_usd_yr_intrinsic,
        "azure_storage_ref_rate_usd_yr": azure_storage_ref_rate_usd_yr,
        "reference_rates": REFERENCE_RATES,
        # Counts
        "vm_count_powered_on": len(per_vm),
        "vm_count_with_sku_match": sum(1 for vm in per_vm if vm.sku_name),
        "vm_count_match_failed": sum(1 for vm in per_vm if vm.match_failed),
        "vcpu_floor_breakdown": floor_breakdown,
        "storage_tier_breakdown": storage_tier_breakdown,
        "retry_breakdown": retry_breakdown,
        "vpartition_summary": vpart_summary,
    }

    rule_provenance = {
        "L2.SCOPE.001":               ["per_vm[] = powered-on, non-template only"],
        "L2.RIGHTSIZE.CPU.001":       ["per_vm[].rs_vcpus", "per_vm[].cpu_branch"],
        "L2.RIGHTSIZE.CPU.WINDOWS_FLOOR": ["per_vm[].min_vcpus", "per_vm[].vcpu_floor_source"],
        "L2.RIGHTSIZE.MEMORY.001":    ["per_vm[].rs_mem_gib", "per_vm[].memory_branch"],
        "L2.RIGHTSIZE.STORAGE.001":   ["per_vm[].rs_disk_gib", "per_vm[].storage_branch"],
        "L2.RIGHTSIZE.STORAGE.MULTI_DISK": ["per_vm[].disk_layout"],
        "KP.STRATEGY_PRECEDENCE":     [f"strategy={strategy}"],
        "KP.WIN_8VCPU_MIN":           [f"enforce_8vcpu_min_for_windows_server={enforce_8vcpu_min_for_windows_server}"],
        "KP.BA_FALLBACK_REDUCTIONS":  ["reduction_params"] if strategy == "ba_fallback" else [],
        "KP.AZURE_RD_RIGHTSIZING_v1": [
            "rd_slide7_cpu_default", "rd_slide7_cpu_user",
            "rd_slide8_memory_default", "rd_slide8_memory_user",
            "rd_slide9_storage_default", "rd_slide9_storage_capacity", "rd_slide9_storage_vinfo",
            "snap_cpu", "snap_mem_gib", "snap_disk_gib",
        ],
    }

    return Layer2Result(
        input_file=str(workbook_path),
        strategy=strategy,
        enforce_8vcpu_min_for_windows_server=enforce_8vcpu_min_for_windows_server,
        reduction_params=reduction,
        region=region,
        family_pin=family_pin,
        processor_pin=processor_pin,
        per_vm=per_vm,
        fyi_aggregates=fyi,
        rule_provenance=rule_provenance,
        pricing_available=pricing_available,
    )


# =============================================================================
# CLI
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layer 2 BA replica oracle")
    parser.add_argument("input", type=Path, help="RVTools .xlsx input file")
    parser.add_argument(
        "--strategy", choices=VALID_STRATEGIES, default="like_for_like",
        help="Right-sizing strategy (per KP.STRATEGY_PRECEDENCE).",
    )
    parser.add_argument(
        "--enforce-8vcpu-min-for-windows-server", action="store_true",
        help="Apply 8-vCPU floor to Windows Server VMs (default off; per KP.WIN_8VCPU_MIN).",
    )
    parser.add_argument("--cpu-reduction-pct", type=float, default=0.0)
    parser.add_argument("--mem-reduction-pct", type=float, default=0.0)
    parser.add_argument("--mem-buffer-pct", type=float, default=0.0)
    parser.add_argument("--storage-reduction-pct", type=float, default=0.0)
    parser.add_argument("--storage-buffer-pct", type=float, default=0.0)
    parser.add_argument("--region", default="eastus2",
                        help="Azure region for pricing (per R&D.OPTIONAL.REGION_CODES).")
    parser.add_argument("--family-pin", default=None,
                        help="Optional family filter per R&D.OPTIONAL.FAMILY_PROCESSOR.")
    parser.add_argument("--processor-pin", default=None, choices=[None, "Intel", "AMD", "ARM"],
                        help="Optional processor filter per R&D.OPTIONAL.FAMILY_PROCESSOR.")
    parser.add_argument("--matcher-algorithm",
                        choices=list(VALID_MATCHER_ALGORITHMS),
                        default="ba_iterative_match",
                        help="SKU match algorithm (default: BA-confirmed iterative match).")
    parser.add_argument("--disk-class", choices=("Standard", "Premium"), default="Standard",
                        help="Managed-disk SSD class (BA default: Standard SSD LRS).")
    parser.add_argument("--max-vcpu-overspec", type=float, default=_DEFAULT_MAX_VCPU_OVERSPEC,
                        help="Algorithm 2 only: max picked sku.vcpu / source.cpu (default 2.0).")
    parser.add_argument("--max-mem-overspec", type=float, default=_DEFAULT_MAX_MEM_OVERSPEC,
                        help="Algorithm 2 only: max picked sku.memory_gib / source.mem_gib (default 3.0).")
    parser.add_argument("--no-pricing", action="store_true",
                        help="Skip SKU match + pricing matrix (rightsizing only).")
    parser.add_argument("--out", type=Path, help="Write summary YAML to this path.")
    parser.add_argument("--per-vm-out", type=Path, help="Optional per-VM dump.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    result = replicate_layer2(
        args.input,
        strategy=args.strategy,
        enforce_8vcpu_min_for_windows_server=args.enforce_8vcpu_min_for_windows_server,
        cpu_reduction_pct=args.cpu_reduction_pct,
        mem_reduction_pct=args.mem_reduction_pct,
        mem_buffer_pct=args.mem_buffer_pct,
        storage_reduction_pct=args.storage_reduction_pct,
        storage_buffer_pct=args.storage_buffer_pct,
        region=args.region,
        family_pin=args.family_pin,
        processor_pin=args.processor_pin,
        enable_pricing=not args.no_pricing,
        matcher_algorithm=args.matcher_algorithm,
        disk_class=args.disk_class,
        max_vcpu_overspec=args.max_vcpu_overspec,
        max_mem_overspec=args.max_mem_overspec,
    )

    summary = result.to_dict()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(yaml.safe_dump(summary, sort_keys=False, width=100))
        _log.info("Layer 2 summary written to %s", args.out)
    else:
        print(yaml.safe_dump(summary, sort_keys=False, width=100))

    if args.per_vm_out:
        args.per_vm_out.parent.mkdir(parents=True, exist_ok=True)
        args.per_vm_out.write_text(
            yaml.safe_dump([vm.to_dict() for vm in result.per_vm], sort_keys=False, width=120)
        )
        _log.info("Per-VM dump written to %s", args.per_vm_out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
