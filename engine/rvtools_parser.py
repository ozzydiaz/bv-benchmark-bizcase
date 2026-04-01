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
COL_TEMPLATE = "Template"
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

# ESU-eligible OS versions: 2003, 2008 (inc R2), and 2012 (inc R2).
# Windows Server 2012/2012 R2 reached end of standard support Oct 2023 and
# became ESU-eligible.  2003/2008/2008 R2 have been ESU for longer.
# NOTE: RVtools' 'OS according to the configuration file' column often shows
# a generic string (e.g. 'Microsoft Windows Server 2016 or later') for older
# VMs whose VMware hardware version predates current OS detection.  The VMware
# Tools column is used as a fallback but may also be unreliable for retired VMs.
# When unversioned Windows Server VMs are detected, a warning is emitted.
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

    Two counting scopes:

      ON-PREM TCO BASELINE — all VMs (powered-on + powered-off), excl. templates:
        num_vms, total_vcpu, total_vmemory_gb, total_storage_in_use_gb,
        pcores_with_windows_server, pcores_with_windows_esu.
        Rationale: the customer paid for all hardware and software licences
        regardless of whether individual VMs are currently running.

      AZURE MIGRATION TARGET — powered-on VMs only:
        num_vms_poweredon, total_vcpu_poweredon, total_vmemory_gb_poweredon,
        total_storage_poweredon_gb.
        Rationale: only running VMs are candidates for migration; powered-off
        VMs will not consume Azure resources.

      ESU detection: both OS columns ('configuration file' and 'VMware Tools')
        are checked.  The configuration-file column is the primary source as it
        more frequently carries explicit version strings.  When Windows Server
        VMs with no detectable version are found (typically pre-2016 VMs whose
        VMware hardware version predates OS detection), esu_count_may_be_understated
        is set to True and a warning is emitted.
    """
    # ── ON-PREM TCO BASELINE (all VMs, incl. powered-off, excl. templates) ──
    num_vms: int = 0
    total_vcpu: int = 0
    total_vmemory_gb: float = 0.0
    total_storage_in_use_gb: float = 0.0  # all VMs, In Use MiB / 953.67

    # Host / virtualisation layer (from vHost tab)
    vcpu_per_core_ratio: float = 0.0
    total_host_pcores: int = 0
    total_host_memory_gb: float = 0.0
    num_hosts: int = 0

    # Windows/SQL pCore estimates — ALL VMs, both OS columns
    pcores_with_windows_server: int = 0
    pcores_with_windows_esu: int = 0
    esu_count_may_be_understated: bool = False
    windows_vms_unknown_version: int = 0  # Windows VMs with no detectable version
    pcores_with_sql_server: int = 0       # default: 10% of windows
    pcores_with_sql_esu: int = 0          # default: 10% of windows_esu

    # ── AZURE MIGRATION TARGET (powered-on VMs only) ──
    num_vms_poweredon: int = 0
    total_vcpu_poweredon: int = 0
    total_vmemory_gb_poweredon: float = 0.0
    total_storage_poweredon_gb: float = 0.0  # powered-on In Use MiB / 953.67

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


def parse(path: str | Path) -> RVToolsInventory:
    """
    Parse an RVTools export and return an aggregated RVToolsInventory.

    Counting rules:
      - TCO baseline metrics (num_vms, vCPU, memory, storage, Windows/ESU
        pCores): ALL non-template VMs regardless of power state.  The customer
        paid for all hardware and software, so every VM is counted.
      - Azure migration sizing (num_vms_poweredon, vcpu_poweredon, etc.):
        powered-on VMs only.  Only running VMs will be migrated.

    Args:
        path: Path to the RVTools .xlsx file.
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

        ci_name     = _col_index(headers, COL_VM_NAME,    warnings)
        ci_power    = _col_index(headers, COL_POWERSTATE, warnings)
        ci_template = _col_index(headers, COL_TEMPLATE,   [])
        ci_cpu      = _col_index(headers, COL_CPUS,       warnings)
        ci_mem      = _col_index(headers, COL_MEMORY_MB,  warnings)
        ci_stor     = _col_index(headers, COL_IN_USE_MIB, warnings)
        ci_os_cfg   = _col_index(headers, COL_OS_CONFIG,  warnings)
        ci_os_tools = _col_index(headers, COL_OS_TOOLS,   warnings)

        # TCO counters — ALL non-template VMs
        total_vcpu_all = 0
        total_mem_mb_all = 0
        total_stor_mib_all = 0.0
        num_vms_all = 0
        win_vcpus_all = 0
        win_esu_vcpus_all = 0
        win_unversioned_all = 0  # Windows VMs with no detectable version string

        # Azure sizing counters — powered-on VMs only
        num_vms_on = 0
        total_vcpu_on = 0
        total_mem_mb_on = 0
        total_stor_mib_on = 0.0

        for row in rows:
            if ci_name is not None and row[ci_name] is None:
                continue

            # Skip template entries — they inflate VM counts and licence metrics
            if ci_template is not None and row[ci_template] is True:
                continue

            is_on = (
                ci_power is not None
                and str(row[ci_power] or "").lower() == "poweredon"
            )

            # ── TCO BASELINE: all VMs ──
            num_vms_all += 1

            cpus = row[ci_cpu] if ci_cpu is not None else None
            vm_cpus = int(cpus) if isinstance(cpus, (int, float)) else 0
            if isinstance(cpus, (int, float)):
                total_vcpu_all += vm_cpus

            mem = row[ci_mem] if ci_mem is not None else None
            if isinstance(mem, (int, float)):
                total_mem_mb_all += mem

            stor = row[ci_stor] if ci_stor is not None else None
            if isinstance(stor, (int, float)):
                total_stor_mib_all += stor

            # OS classification: config-file column is primary (more complete
            # version strings); VMware Tools column is the fallback.
            os_cfg   = str(row[ci_os_cfg]   or "") if ci_os_cfg   is not None else ""
            os_tools = str(row[ci_os_tools] or "") if ci_os_tools is not None else ""

            is_win = (
                bool(_WINDOWS_PATTERN.search(os_cfg))
                or bool(_WINDOWS_PATTERN.search(os_tools))
            )
            is_esu = (
                bool(_WINDOWS_ESU_PATTERN.search(os_cfg))
                or bool(_WINDOWS_ESU_PATTERN.search(os_tools))
            )

            if is_win:
                win_vcpus_all += vm_cpus
                if is_esu:
                    win_esu_vcpus_all += vm_cpus
                else:
                    # Windows Server VM but no year/version in either OS column.
                    # Likely a pre-2016 VM where VMware lost OS version data.
                    win_unversioned_all += 1

            # ── AZURE SIZING: powered-on VMs only ──
            if is_on:
                num_vms_on += 1
                total_vcpu_on += vm_cpus
                if isinstance(mem, (int, float)):
                    total_mem_mb_on += mem
                if isinstance(stor, (int, float)):
                    total_stor_mib_on += stor

        inv.num_vms = num_vms_all
        inv.total_vcpu = total_vcpu_all
        inv.total_vmemory_gb = round(total_mem_mb_all * MB_TO_GB, 2)
        inv.total_storage_in_use_gb = round(total_stor_mib_all * MIB_TO_GB, 2)
        inv.windows_vms_unknown_version = win_unversioned_all
        inv.esu_count_may_be_understated = win_unversioned_all > 0

        inv.num_vms_poweredon = num_vms_on
        inv.total_vcpu_poweredon = total_vcpu_on
        inv.total_vmemory_gb_poweredon = round(total_mem_mb_on * MB_TO_GB, 2)
        inv.total_storage_poweredon_gb = round(total_stor_mib_on * MIB_TO_GB, 2)

        # Store raw vCPU counts; pCores derived after vHost ratio is known
        inv._win_vcpus = win_vcpus_all        # type: ignore[attr-defined]
        inv._win_esu_vcpus = win_esu_vcpus_all  # type: ignore[attr-defined]

        if win_unversioned_all > 0:
            warnings.append(
                f"ESU undercount likely: {win_unversioned_all} Windows Server VMs "
                f"(all power states) have no version string in either OS column "
                f"(config file or VMware Tools).  These are likely pre-2016 VMs "
                f"(2003/2008/2008 R2) that are ESU-eligible but undetectable from "
                f"RVtools OS strings alone.  Review and override 'pCores with ESU' "
                f"in the intake form using a separate OS audit (e.g. MAP Toolkit, "
                f"Azure Migrate, or manual review)."
            )

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
    print()
    print("  ── On-Prem TCO Baseline (all VMs, incl. powered-off) ──")
    print(f"  VMs (total):                  {inv.num_vms:>10,}")
    print(f"  Total vCPU:                   {inv.total_vcpu:>10,}")
    print(f"  Total vMemory (GB):           {inv.total_vmemory_gb:>10,.1f}")
    print(f"  Storage in use (GB):          {inv.total_storage_in_use_gb:>10,.1f}")
    print(f"  Hosts:                        {inv.num_hosts:>10,}")
    print(f"  Host pCores (total):          {inv.total_host_pcores:>10,}")
    print(f"  Host Memory GB:               {inv.total_host_memory_gb:>10,.1f}")
    print(f"  vCPUs per pCore (avg):        {inv.vcpu_per_core_ratio:>10.3f}")
    print(f"  pCores w/ Win Server:         {inv.pcores_with_windows_server:>10,}")
    esu_note = "  ⚑ may be understated" if inv.esu_count_may_be_understated else ""
    print(f"  pCores w/ Win ESU:            {inv.pcores_with_windows_esu:>10,}{esu_note}")
    if inv.esu_count_may_be_understated:
        print(f"  Windows VMs w/ unknown ver:   {inv.windows_vms_unknown_version:>10,}  (check OS audit)")
    print(f"  pCores w/ SQL Server:         {inv.pcores_with_sql_server:>10,}")
    print(f"  pCores w/ SQL ESU:            {inv.pcores_with_sql_esu:>10,}")
    print()
    print("  ── Azure Migration Target (powered-on VMs only) ──")
    print(f"  VMs (powered-on):             {inv.num_vms_poweredon:>10,}")
    print(f"  vCPU (powered-on):            {inv.total_vcpu_poweredon:>10,}")
    print(f"  vMemory GB (powered-on):      {inv.total_vmemory_gb_poweredon:>10,.1f}")
    print(f"  Storage GB (powered-on):      {inv.total_storage_poweredon_gb:>10,.1f}")
    if inv.parse_warnings:
        print(f"\n  Warnings ({len(inv.parse_warnings)}):")
        for w in inv.parse_warnings:
            print(f"    \u26a0  {w}")
