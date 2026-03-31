"""
RVtools export parser.

Reads a standard RVTools .xlsx export and extracts the fields needed
to populate a WorkloadInventory for the business case engine.

RVTools column names are used (not positional indices) so the parser
is robust across different RVTools export versions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

# ---------------------------------------------------------------------------
# Column name constants
# ---------------------------------------------------------------------------

# vInfo columns
COL_VM_NAME = "VM"
COL_POWERSTATE = "Powerstate"
COL_CPUS = "CPUs"
COL_MEMORY_MB = "Memory"          # megabytes
COL_IN_USE_MIB = "In Use MiB"     # storage in use
COL_OS_CONFIG = "OS according to the configuration file"
COL_OS_TOOLS = "OS according to the VMware Tools"

# vHost columns
COL_HOST = "Host"
COL_CORES = "# Cores"            # total physical cores per host
COL_HOST_MEMORY_MB = "# Memory"  # megabytes
COL_VCPUS_PER_CORE = "vCPUs per Core"

# OS filter patterns for Windows Server
_WINDOWS_PATTERN = re.compile(r"windows\s+server", re.IGNORECASE)
# ESU-eligible: Windows Server 2012 and older (2003, 2008, 2012)
_WINDOWS_ESU_PATTERN = re.compile(
    r"windows\s+server\s+(2003|2008|2012)", re.IGNORECASE
)

MIB_TO_GB = 1 / 953.67   # MiB → GB (binary → decimal)
MB_TO_GB = 1 / 1024.0


# ---------------------------------------------------------------------------
# Raw parsed result
# ---------------------------------------------------------------------------

@dataclass
class RVToolsInventory:
    """
    Aggregated inventory derived from a single RVTools export.
    All numeric fields are totals across all (non-template) VMs/hosts.
    """
    # VM counts
    num_vms: int = 0

    # CPU
    total_vcpu: int = 0

    # Memory
    total_vmemory_gb: float = 0.0

    # Storage
    total_storage_in_use_gb: float = 0.0

    # License inventory
    vcpu_per_core_ratio: float = 0.0   # average vCPUs per pCore across all hosts
    total_host_pcores: int = 0
    total_host_memory_gb: float = 0.0
    num_hosts: int = 0

    # Windows/SQL estimates (from OS column)
    pcores_with_windows_server: int = 0
    pcores_with_windows_esu: int = 0

    # Derived (populated after parsing)
    pcores_with_sql_server: int = 0    # default: 10% of windows
    pcores_with_sql_esu: int = 0       # default: 10% of windows_esu

    # Metadata
    source_file: str = ""
    parse_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _col_index(headers: list, name: str, warn_list: list[str]) -> int | None:
    """Return 0-based column index for a named header, or None on miss."""
    try:
        return headers.index(name)
    except ValueError:
        warn_list.append(f"Column not found: '{name}'")
        return None


def parse(path: str | Path, include_powered_off: bool = True) -> RVToolsInventory:
    """
    Parse an RVTools export and return an aggregated RVToolsInventory.

    Args:
        path: Path to the RVTools .xlsx file.
        include_powered_off: If False, only count powered-on VMs.
                             Default True matches workbook behaviour.
    """
    path = Path(path)
    inv = RVToolsInventory(source_file=str(path))
    warnings = inv.parse_warnings

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)

    # ------------------------------------------------------------------
    # vInfo tab
    # ------------------------------------------------------------------
    if "vInfo" not in wb.sheetnames:
        warnings.append("vInfo tab not found — no VM data extracted")
    else:
        ws = wb["vInfo"]
        rows = ws.iter_rows(values_only=True)
        headers = list(next(rows))

        ci_name = _col_index(headers, COL_VM_NAME, warnings)
        ci_power = _col_index(headers, COL_POWERSTATE, warnings)
        ci_cpu = _col_index(headers, COL_CPUS, warnings)
        ci_mem = _col_index(headers, COL_MEMORY_MB, warnings)
        ci_stor = _col_index(headers, COL_IN_USE_MIB, warnings)
        ci_os_cfg = _col_index(headers, COL_OS_CONFIG, warnings)
        ci_os_tools = _col_index(headers, COL_OS_TOOLS, warnings)

        total_vcpu = 0
        total_mem_mb = 0
        total_stor_mib = 0
        num_vms = 0
        # For Windows pCore estimation: track per-VM vCPU to convert later
        win_vcpus = 0
        win_esu_vcpus = 0

        for row in rows:
            # Skip templates / empty rows
            if ci_name is not None and (row[ci_name] is None):
                continue

            # Powerstate filter
            if not include_powered_off and ci_power is not None:
                if (row[ci_power] or "").lower() != "poweredon":
                    continue

            num_vms += 1

            # vCPU
            cpus = row[ci_cpu] if ci_cpu is not None else None
            if isinstance(cpus, (int, float)):
                total_vcpu += int(cpus)

            # Memory
            mem = row[ci_mem] if ci_mem is not None else None
            if isinstance(mem, (int, float)):
                total_mem_mb += mem

            # Storage
            stor = row[ci_stor] if ci_stor is not None else None
            if isinstance(stor, (int, float)):
                total_stor_mib += stor

            # OS classification
            os_str = ""
            if ci_os_cfg is not None and row[ci_os_cfg]:
                os_str = str(row[ci_os_cfg])
            elif ci_os_tools is not None and row[ci_os_tools]:
                os_str = str(row[ci_os_tools])

            vm_cpus = int(cpus) if isinstance(cpus, (int, float)) else 0
            if _WINDOWS_PATTERN.search(os_str):
                win_vcpus += vm_cpus
            if _WINDOWS_ESU_PATTERN.search(os_str):
                win_esu_vcpus += vm_cpus

        inv.num_vms = num_vms
        inv.total_vcpu = total_vcpu
        inv.total_vmemory_gb = round(total_mem_mb * MB_TO_GB, 2)
        inv.total_storage_in_use_gb = round(total_stor_mib * MIB_TO_GB, 2)
        # Windows pCores estimated by dividing vCPU count by ratio
        # (ratio is set from vHost below; store raw vCPU counts for now)
        inv._win_vcpus = win_vcpus            # type: ignore[attr-defined]
        inv._win_esu_vcpus = win_esu_vcpus    # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # vHost tab
    # ------------------------------------------------------------------
    if "vHost" not in wb.sheetnames:
        warnings.append("vHost tab not found — no host data extracted")
    else:
        ws2 = wb["vHost"]
        rows2 = ws2.iter_rows(values_only=True)
        headers2 = list(next(rows2))

        ci_host = _col_index(headers2, COL_HOST, warnings)
        ci_cores = _col_index(headers2, COL_CORES, warnings)
        ci_hmem = _col_index(headers2, COL_HOST_MEMORY_MB, warnings)
        ci_vpc = _col_index(headers2, COL_VCPUS_PER_CORE, warnings)

        total_cores = 0
        total_hmem_mb = 0.0
        vcpu_per_core_values: list[float] = []
        num_hosts = 0

        for row2 in rows2:
            if ci_host is not None and row2[ci_host] is None:
                continue
            num_hosts += 1

            cores = row2[ci_cores] if ci_cores is not None else None
            if isinstance(cores, (int, float)):
                total_cores += int(cores)

            hmem = row2[ci_hmem] if ci_hmem is not None else None
            if isinstance(hmem, (int, float)):
                total_hmem_mb += hmem

            vpc = row2[ci_vpc] if ci_vpc is not None else None
            if isinstance(vpc, (int, float)) and vpc > 0:
                vcpu_per_core_values.append(float(vpc))

        inv.num_hosts = num_hosts
        inv.total_host_pcores = total_cores
        inv.total_host_memory_gb = round(total_hmem_mb * MB_TO_GB, 2)
        inv.vcpu_per_core_ratio = (
            round(sum(vcpu_per_core_values) / len(vcpu_per_core_values), 4)
            if vcpu_per_core_values else 1.0
        )

    # ------------------------------------------------------------------
    # Derive Windows/SQL pCore estimates using the vCPU/core ratio
    # ------------------------------------------------------------------
    ratio = inv.vcpu_per_core_ratio if inv.vcpu_per_core_ratio > 0 else 1.0
    win_vcpus = getattr(inv, "_win_vcpus", 0)
    win_esu_vcpus = getattr(inv, "_win_esu_vcpus", 0)
    inv.pcores_with_windows_server = round(win_vcpus / ratio)
    inv.pcores_with_windows_esu = round(win_esu_vcpus / ratio)
    inv.pcores_with_sql_server = round(inv.pcores_with_windows_server * 0.10)
    inv.pcores_with_sql_esu = round(inv.pcores_with_windows_esu * 0.10)

    # Clean up private attrs
    for attr in ("_win_vcpus", "_win_esu_vcpus"):
        if hasattr(inv, attr):
            delattr(inv, attr)

    if warnings:
        print(f"[rvtools_parser] {len(warnings)} warning(s):")
        for w in warnings:
            print(f"  - {w}")

    return inv


def summarize(inv: RVToolsInventory) -> None:
    """Print a human-readable summary of a parsed RVToolsInventory."""
    print(f"Source: {inv.source_file}")
    print(f"  VMs:                    {inv.num_vms:>10,}")
    print(f"  Hosts:                  {inv.num_hosts:>10,}")
    print(f"  Total vCPU:             {inv.total_vcpu:>10,}")
    print(f"  Total vMemory (GB):     {inv.total_vmemory_gb:>10,.1f}")
    print(f"  Storage in use (GB):    {inv.total_storage_in_use_gb:>10,.1f}")
    print(f"  Host pCores (total):    {inv.total_host_pcores:>10,}")
    print(f"  Host Memory GB:         {inv.total_host_memory_gb:>10,.1f}")
    print(f"  vCPUs per pCore (avg):  {inv.vcpu_per_core_ratio:>10.3f}")
    print(f"  pCores w/ Win Server:   {inv.pcores_with_windows_server:>10,}")
    print(f"  pCores w/ Win ESU:      {inv.pcores_with_windows_esu:>10,}")
    print(f"  pCores w/ SQL Server:   {inv.pcores_with_sql_server:>10,}")
    print(f"  pCores w/ SQL ESU:      {inv.pcores_with_sql_esu:>10,}")
