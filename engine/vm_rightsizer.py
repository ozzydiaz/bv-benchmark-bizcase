"""
Per-VM rightsizing and Azure family selection.

Given a VMRecord from the parsed RVTools inventory, this module:
  1. Resolves the best available utilisation signal (per-VM telemetry →
     vHost-level proxy → assumption fallback factors).
  2. Computes the rightsized target (vcpu, memory_gib) with headroom.
  3. Selects the Azure VM family (D / E / F / M) based on memory density
     and workload-type keywords.

All memory values are in GiB (MiB ÷ 1024) to match Azure SKU catalog units.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.rvtools_parser import RVToolsInventory, VMRecord
    from engine.models import BenchmarkConfig

import logging
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Utilisation safety cap
# ---------------------------------------------------------------------------

# VMware's Consumed/Size MiB ratio can legally exceed 1.0 due to memory
# ballooning, TPS (Transparent Page Sharing), and swap reclaim — metrics
# that reflect VMware's memory management, not actual application demand.
# Capping at 0.95 prevents these artefacts from causing Azure targets to
# exceed the on-prem provisioned size and consequent SKU snap-up inflation.
# The same cap is applied to CPU utilisation as a general safety net.
_UTIL_CAP: float = 0.95

# ---------------------------------------------------------------------------
# Keyword patterns for workload-type detection
# ---------------------------------------------------------------------------

_HPC_PATTERN = re.compile(r"\bhpc\b", re.IGNORECASE)
_SAP_PATTERN = re.compile(r"\bsap\b", re.IGNORECASE)
_ORACLE_PATTERN = re.compile(r"\boracle\b", re.IGNORECASE)

# Strings that suggest memory-intensive workloads (E-series bias)
_MEM_HEAVY_PATTERN = re.compile(
    r"\b(sap|oracle|database|db|mem|memory|cache|redis|mongo|elastic)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Utilisation resolution
# ---------------------------------------------------------------------------

def resolve_vm_utilisation(
    vm: "VMRecord",
    inv: "RVToolsInventory",
) -> tuple[float, float, str]:
    """
    Return (cpu_util_fraction, mem_util_fraction, source_label).

    Priority:
      1. Per-VM telemetry from vCPU / vMemory tabs.
      2. Host-level CPU usage % / Memory usage % as proxy.
      3. Fallback factors from BenchmarkConfig (caller applies these).

    Returns 0.0 for each metric when absent — caller distinguishes by
    checking source_label ("vm_telemetry", "host_proxy", or "fallback").
    """
    # 1. Per-VM vCPU / vMemory telemetry ---------------------------------
    cpu_util = inv.vm_cpu_util.get(vm.name, 0.0)
    mem_util = inv.vm_mem_util.get(vm.name, 0.0)
    if cpu_util > 0 and mem_util > 0:
        return cpu_util, mem_util, "vm_telemetry"
    if cpu_util > 0:
        # CPU from telemetry; memory from host or fallback
        host_mem = _host_mem_util(vm, inv)
        if host_mem > 0:
            return cpu_util, host_mem, "vm_telemetry+host_proxy"
    if mem_util > 0:
        host_cpu = _host_cpu_util(vm, inv)
        if host_cpu > 0:
            return host_cpu, mem_util, "host_proxy+vm_telemetry"

    # 2. Host-level proxy -------------------------------------------------
    host_cpu = _host_cpu_util(vm, inv)
    host_mem = _host_mem_util(vm, inv)
    if host_cpu > 0 or host_mem > 0:
        return host_cpu, host_mem, "host_proxy"

    # 3. No signal — caller will apply fallback factors -------------------
    return 0.0, 0.0, "fallback"


def _host_cpu_util(vm: "VMRecord", inv: "RVToolsInventory") -> float:
    """Return host CPU utilisation fraction (0–1) for VM's host, or 0.0."""
    host = inv.vm_to_host.get(vm.name, "")
    if not host:
        return 0.0
    util_pct = inv.host_cpu_util.get(host, 0.0)
    return util_pct / 100.0 if util_pct > 0 else 0.0


def _host_mem_util(vm: "VMRecord", inv: "RVToolsInventory") -> float:
    """Return host memory utilisation fraction (0–1) for VM's host, or 0.0."""
    host = inv.vm_to_host.get(vm.name, "")
    if not host:
        return 0.0
    util_pct = inv.host_mem_util.get(host, 0.0)
    return util_pct / 100.0 if util_pct > 0 else 0.0


# ---------------------------------------------------------------------------
# Right-sizing computation
# ---------------------------------------------------------------------------

def rightsize_vm(
    vm: "VMRecord",
    cpu_util: float,
    mem_util: float,
    util_source: str,
    benchmarks: "BenchmarkConfig",
) -> tuple[int, float]:
    """
    Return (target_vcpu: int, target_mem_gib: float) for one VM.

    When util_source == "fallback", cpu_util and mem_util are 0.0 and the
    BenchmarkConfig fallback factors are applied instead.
    All memory arithmetic is in GiB (vm.memory_mib ÷ 1024).

    Source-size ceiling
    -------------------
    The rightsized target is capped at the source VM's own allocation.
    Rationale: this tool sizes for *migration*, not for upgrades.  If
    measured utilisation × headroom would push the target above the
    source allocation, the source is at or near capacity — the right
    Azure size is at most the same as the source, not larger.  Without
    this cap, high-utilisation VMs (≥ ~83% with 20% headroom) produce
    targets above the source vCPU/memory, which then snap up to the next
    Azure SKU tier and inflate pricing dramatically.
    """
    mem_gib = vm.memory_mib / 1024.0

    if util_source == "fallback" or (cpu_util <= 0 and mem_util <= 0):
        # No utilisation signal — apply assumption factors.
        # Fallback factors are < 1.0 so these always reduce the target;
        # the source-size ceiling is implicitly satisfied.
        target_vcpu = max(1, math.ceil(
            vm.vcpu * benchmarks.cpu_util_fallback_factor
            * (1 + benchmarks.cpu_rightsizing_headroom_factor)
        ))
        target_mem_gib = max(1.0, math.ceil(
            mem_gib * benchmarks.mem_util_fallback_factor
            * (1 + benchmarks.memory_rightsizing_headroom_factor)
        ))
    else:
        # Utilisation-based sizing.
        # Cap utilisation fractions at _UTIL_CAP (0.95) before applying headroom.
        # VMware memory "Consumed/Size" can exceed 1.0 due to ballooning/TPS;
        # CPU "Overall/Max" can spike briefly above 100%.
        eff_cpu = min(cpu_util if cpu_util > 0 else benchmarks.cpu_util_fallback_factor, _UTIL_CAP)
        eff_mem = min(mem_util if mem_util > 0 else benchmarks.mem_util_fallback_factor, _UTIL_CAP)

        raw_vcpu = max(1, math.ceil(
            vm.vcpu * eff_cpu * (1 + benchmarks.cpu_rightsizing_headroom_factor)
        ))
        raw_mem_gib = max(1.0, math.ceil(
            mem_gib * eff_mem * (1 + benchmarks.memory_rightsizing_headroom_factor)
        ))

        # Cap at source allocation: never size UP beyond the existing VM.
        # A high-utilisation VM is already at capacity — its Azure equivalent
        # should be the same size, not larger.  SKU-tier snap-up may still add
        # a small amount (unavoidable with Azure's discrete tiers), but the
        # starting target is always ≤ source.
        target_vcpu    = min(vm.vcpu,    raw_vcpu)
        target_mem_gib = min(mem_gib,    raw_mem_gib)

    return target_vcpu, target_mem_gib


# ---------------------------------------------------------------------------
# Family selection
# ---------------------------------------------------------------------------

def select_family(
    vm: "VMRecord",
    target_vcpu: int,
    target_mem_gib: float,
    benchmarks: "BenchmarkConfig",
) -> str:
    """
    Return the preferred Azure VM family letter: "D", "E", "F", or "M".

    Logic (in priority order):
      1. SAP / Oracle keywords in VM name or application metadata → M-series
         if target_mem > M-threshold per vCPU; otherwise E-series.
      2. HPC keywords in VM name → F-series (compute-optimised).
      3. SQL flag on this VM OR memory-heavy keywords in name/app → E-series
         bias (even if vcpu count is larger in matched SKU).
      4. Memory density: mem_gib / vcpu vs E-/M-series thresholds.
      5. Default: D-series.
    """
    mem_density = target_mem_gib / max(target_vcpu, 1)

    # Scan name and application string for workload keywords
    scan_text = f"{vm.name} {vm.app_str}".lower()

    if _SAP_PATTERN.search(scan_text) or _ORACLE_PATTERN.search(scan_text):
        if mem_density >= benchmarks.mem_per_vcpu_m_series_threshold_gib:
            return "M"
        return "E"

    if _HPC_PATTERN.search(scan_text):
        return "F"

    # SQL VMs: force E-series (databases drive memory density)
    if vm.is_sql:
        return "E"

    # Memory-heavy keyword hints
    if _MEM_HEAVY_PATTERN.search(scan_text):
        return "E"

    # Density thresholds
    if mem_density >= benchmarks.mem_per_vcpu_m_series_threshold_gib:
        return "M"
    if mem_density >= benchmarks.mem_per_vcpu_e_series_threshold_gib:
        return "E"

    return "D"
