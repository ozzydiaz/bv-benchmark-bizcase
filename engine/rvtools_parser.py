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
    pcores_with_sql_server: int = 0       # default: 10% of windows; overridden by Application detection
    pcores_with_sql_esu: int = 0          # default: 10% of windows_esu

    # SQL detection from Application custom attribute (col 77 in vInfo).
    # When sql_vms_detected > 0 it overrides the 10% Windows default above.
    # Prod/non-Prod split is informational (for presentation); not used in TCO math.
    sql_vms_detected: int = 0             # VMs with 'sql' in Application or OS
    sql_vms_prod: int = 0                 # subset where Environment == 'Production'
    sql_vms_nonprod: int = 0              # remainder
    sql_detection_source: str = "default"  # 'application' | 'default'

    # ── AZURE MIGRATION TARGET (powered-on VMs only) ──
    num_vms_poweredon: int = 0
    total_vcpu_poweredon: int = 0
    total_vmemory_gb_poweredon: float = 0.0
    total_storage_poweredon_gb: float = 0.0  # powered-on In Use MiB / 953.67 (on-prem TCO basis)

    # Provisioned disk capacity from vDisk tab — used for Azure managed disk cost estimation.
    # Azure bills on provisioned tier size, not consumed bytes.  If vDisk tab is absent
    # these remain 0.0 and consumption_builder falls back to in-use × headroom.
    total_disk_provisioned_gb: float = 0.0        # all VMs, vDisk.Capacity MiB sum / 953.67
    total_disk_provisioned_poweredon_gb: float = 0.0  # powered-on VMs only
    # Per-VM disk layout for per-VM managed disk tier costing.
    # Keys are VM names; values are lists of provisioned sizes in GiB (float).
    # Populated only for powered-on, non-template VMs.  Empty when vDisk tab absent.
    vm_disk_sizes_gb: dict[str, list[float]] = field(default_factory=dict)

    # ── UTILISATION TELEMETRY (from vCPU and vMemory tabs) ──
    # Values are fleet P95 fractions (0–1+); 0.0 = telemetry not available.
    # powered-on VMs only; powered-off VMs (Overall==0) are excluded.
    cpu_util_p95: float = 0.0           # P95 of (Overall MHz / Max MHz) per VM
    cpu_util_p95_vm_count: int = 0      # VMs contributing to the P95 calculation
    memory_util_p95: float = 0.0        # P95 of (Consumed MiB / Size MiB) per VM
    memory_util_p95_vm_count: int = 0

    # ── REGION EVIDENCE (from vHost and vMetaData tabs) ──
    # These raw signals are consumed by engine/region_guesser.py to infer an
    # Azure region.  All collections are de-duplicated and sorted.
    datacenter_names: list[str] = field(default_factory=list)         # vHost.Datacenter (unique, sorted)
    datacenter_host_counts: dict[str, int] = field(default_factory=dict)  # {dc_name: host_count}
    timezone_names: list[str] = field(default_factory=list)           # vHost.Time Zone Name
    gmt_offsets: list[str] = field(default_factory=list)              # vHost.GMT Offset (as strings)
    domain_names: list[str] = field(default_factory=list)             # vHost.Domain
    vcenter_fqdns: list[str] = field(default_factory=list)            # vMetaData.Server

    # Parse context
    vhost_available: bool = False           # True when vHost tab was present
    include_powered_off_applied: bool = False  # True = all-VMs TCO baseline used

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


def parse(path: str | Path, include_powered_off: bool | None = None) -> RVToolsInventory:
    """
    Parse an RVTools export and return an aggregated RVToolsInventory.

    TCO baseline scope (num_vms, vCPU, memory, storage, Windows/ESU pCores)
    is auto-detected from vHost tab availability when include_powered_off=None:

      vHost tab present  → powered-on VMs only (default).  The vHost tab
        confirms the physical host inventory; powered-off VMs represent idle
        capacity not consuming active hardware resources.
        Override: pass include_powered_off=True to also count powered-off VMs.

      vHost tab absent   → all VMs, powered-on + powered-off (default).
        Without host data the inventory may be incomplete, so every VM is
        included to avoid understating the baseline.
        Override: pass include_powered_off=False to limit to powered-on only.

    Azure migration sizing fields (num_vms_poweredon, total_vcpu_poweredon,
    total_vmemory_gb_poweredon, total_storage_poweredon_gb) are ALWAYS
    powered-on only regardless of this setting.

    Args:
        path: Path to the RVTools .xlsx file.
        include_powered_off: Override for TCO baseline scope.
            None (default) = auto-detect from vHost availability.
            True  = include powered-off VMs (even if vHost is present).
            False = exclude powered-off VMs (even if vHost is absent).
    """
    path = Path(path)
    inv = RVToolsInventory(source_file=str(path))
    warnings = inv.parse_warnings

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)

    # Counters populated in vInfo block; scope resolved into inv fields after
    # vHost tab availability is determined.
    num_vms_all = 0;       total_vcpu_all = 0;    total_mem_mb_all = 0.0
    total_stor_mib_all = 0.0
    win_vcpus_all = 0;     win_esu_vcpus_all = 0; win_unversioned_all = 0
    num_vms_on = 0;        total_vcpu_on = 0;     total_mem_mb_on = 0.0
    total_stor_mib_on = 0.0
    win_vcpus_on = 0;      win_esu_vcpus_on = 0;  win_unversioned_on = 0
    vhost_found = False
    # SQL-from-Application tracking (all-VM scope, silent columns)
    sql_vcpus_all = 0; sql_vms_all = 0; sql_prod_all = 0; sql_nonprod_all = 0

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
        # Optional custom-attribute columns — silent on miss (customer-specific)
        ci_app      = _col_index(headers, "Application",  [])
        ci_env      = _col_index(headers, "Environment",  [])

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

            cpus = row[ci_cpu] if ci_cpu is not None else None
            vm_cpus = int(cpus) if isinstance(cpus, (int, float)) else 0
            mem  = row[ci_mem]  if ci_mem  is not None else None
            stor = row[ci_stor] if ci_stor is not None else None

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

            # SQL detection: Application custom attribute (preferred) + OS fallback
            app_str = str(row[ci_app] or "").lower() if ci_app is not None else ""
            env_str = str(row[ci_env] or "").lower() if ci_env is not None else ""
            is_sql = (
                "sql" in app_str
                or "sql server" in os_cfg.lower()
                or "sql server" in os_tools.lower()
            )

            # ── All-VM accumulators ──
            num_vms_all += 1
            if isinstance(cpus, (int, float)):
                total_vcpu_all += vm_cpus
            if isinstance(mem, (int, float)):
                total_mem_mb_all += mem
            if isinstance(stor, (int, float)):
                total_stor_mib_all += stor
            if is_win:
                win_vcpus_all += vm_cpus
                if is_esu:
                    win_esu_vcpus_all += vm_cpus
                else:
                    win_unversioned_all += 1
            if is_sql:
                sql_vcpus_all += vm_cpus
                sql_vms_all += 1
                if "production" in env_str:
                    sql_prod_all += 1
                else:
                    sql_nonprod_all += 1

            # ── Powered-on accumulators ──
            if is_on:
                num_vms_on += 1
                if isinstance(cpus, (int, float)):
                    total_vcpu_on += vm_cpus
                if isinstance(mem, (int, float)):
                    total_mem_mb_on += mem
                if isinstance(stor, (int, float)):
                    total_stor_mib_on += stor
                if is_win:
                    win_vcpus_on += vm_cpus
                    if is_esu:
                        win_esu_vcpus_on += vm_cpus
                    else:
                        win_unversioned_on += 1

        # Counters ready; scope resolved + inv fields assigned after vHost below.

    # ------------------------------------------------------------------
    # vHost tab
    # ------------------------------------------------------------------
    if "vHost" not in wb.sheetnames:
        warnings.append("vHost tab not found — no host data extracted")
    else:
        vhost_found = True
        ws2 = wb["vHost"]
        rows2 = ws2.iter_rows(values_only=True)
        headers2 = list(next(rows2))

        ci_host = _col_index(headers2, COL_HOST, warnings)
        ci_cores = _col_index(headers2, COL_CORES, warnings)
        ci_hmem = _col_index(headers2, COL_HOST_MEMORY_MB, warnings)
        ci_vpc = _col_index(headers2, COL_VCPUS_PER_CORE, warnings)

        # Region-evidence column indices (silent — no warning on miss)
        ci_dc     = _col_index(headers2, "Datacenter",     [])
        ci_tz     = _col_index(headers2, "Time Zone Name", [])
        ci_gmt    = _col_index(headers2, "GMT Offset",     [])
        ci_domain = _col_index(headers2, "Domain",         [])

        total_cores = 0
        total_hmem_mb = 0.0
        vcpu_per_core_values: list[float] = []
        num_hosts = 0
        dc_names: set[str] = set()
        dc_counts: dict[str, int] = {}
        tz_names: set[str] = set()
        gmt_vals: set[str] = set()
        domain_vals: set[str] = set()

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

            if ci_dc is not None and row2[ci_dc]:
                dc = str(row2[ci_dc]).strip()
                dc_names.add(dc)
                dc_counts[dc] = dc_counts.get(dc, 0) + 1
            if ci_tz is not None and row2[ci_tz]:
                tz_names.add(str(row2[ci_tz]).strip())
            if ci_gmt is not None and row2[ci_gmt] is not None:
                gmt_vals.add(str(row2[ci_gmt]).strip())
            if ci_domain is not None and row2[ci_domain]:
                domain_vals.add(str(row2[ci_domain]).strip().lower())

        inv.num_hosts = num_hosts
        inv.total_host_pcores = total_cores
        inv.total_host_memory_gb = round(total_hmem_mb * MB_TO_GB, 2)
        inv.vcpu_per_core_ratio = (
            round(sum(vcpu_per_core_values) / len(vcpu_per_core_values), 4)
            if vcpu_per_core_values else 1.0
        )
        inv.datacenter_names      = sorted(dc_names)
        inv.datacenter_host_counts = dc_counts
        inv.timezone_names         = sorted(tz_names)
        inv.gmt_offsets            = sorted(gmt_vals)
        inv.domain_names           = sorted(domain_vals)

    # ------------------------------------------------------------------
    # Resolve TCO baseline scope and populate primary inv fields
    # ------------------------------------------------------------------
    inv.vhost_available = vhost_found
    if include_powered_off is None:
        # Auto-detect: powered-on only when vHost confirms complete inventory;
        # all VMs when vHost is absent.
        resolved_poff = not vhost_found
    else:
        resolved_poff = include_powered_off
    inv.include_powered_off_applied = resolved_poff

    if resolved_poff:
        # TCO baseline = all VMs (vHost absent, or explicit override=True)
        inv.num_vms               = num_vms_all
        inv.total_vcpu            = total_vcpu_all
        inv.total_vmemory_gb      = round(total_mem_mb_all * MB_TO_GB, 2)
        inv.total_storage_in_use_gb = round(total_stor_mib_all * MIB_TO_GB, 2)
        inv._win_vcpus     = win_vcpus_all      # type: ignore[attr-defined]
        inv._win_esu_vcpus = win_esu_vcpus_all  # type: ignore[attr-defined]
        win_unversioned    = win_unversioned_all
    else:
        # TCO baseline = powered-on only (vHost present, or explicit override=False)
        inv.num_vms               = num_vms_on
        inv.total_vcpu            = total_vcpu_on
        inv.total_vmemory_gb      = round(total_mem_mb_on * MB_TO_GB, 2)
        inv.total_storage_in_use_gb = round(total_stor_mib_on * MIB_TO_GB, 2)
        inv._win_vcpus     = win_vcpus_on       # type: ignore[attr-defined]
        inv._win_esu_vcpus = win_esu_vcpus_on   # type: ignore[attr-defined]
        win_unversioned    = win_unversioned_on

    # Azure sizing: always powered-on regardless of TCO scope
    inv.num_vms_poweredon          = num_vms_on
    inv.total_vcpu_poweredon       = total_vcpu_on
    inv.total_vmemory_gb_poweredon = round(total_mem_mb_on * MB_TO_GB, 2)
    inv.total_storage_poweredon_gb = round(total_stor_mib_on * MIB_TO_GB, 2)

    inv.windows_vms_unknown_version  = win_unversioned
    inv.esu_count_may_be_understated = win_unversioned > 0

    # Log which scope was applied and why
    scope_label = "all VMs" if resolved_poff else "powered-on VMs only"
    if include_powered_off is not None:
        reason = f"override (include_powered_off={include_powered_off})"
    elif vhost_found:
        reason = "vHost tab present"
    else:
        reason = "vHost tab absent"
    print(f"[rvtools_parser] TCO baseline: {scope_label} ({inv.num_vms:,} VMs) — {reason}")

    if win_unversioned > 0:
        warnings.append(
            f"ESU undercount likely: {win_unversioned} Windows Server VMs in the "
            f"TCO baseline scope have no version string in either OS column "
            f"(config file or VMware Tools).  These are likely pre-2016 VMs "
            f"(2003/2008/2008 R2) that are ESU-eligible but undetectable from "
            f"RVtools OS strings alone.  Review and override 'pCores with ESU' "
            f"in the intake form using a separate OS audit (e.g. MAP Toolkit, "
            f"Azure Migrate, or manual review)."
        )

    # ------------------------------------------------------------------
    # Derive Windows/SQL pCore estimates using the vCPU/core ratio
    # ------------------------------------------------------------------
    ratio = inv.vcpu_per_core_ratio if inv.vcpu_per_core_ratio > 0 else 1.0
    win_vcpus = getattr(inv, "_win_vcpus", 0)
    win_esu_vcpus = getattr(inv, "_win_esu_vcpus", 0)
    inv.pcores_with_windows_server = round(win_vcpus / ratio)
    inv.pcores_with_windows_esu = round(win_esu_vcpus / ratio)

    # SQL pCores: prefer Application-detected count; fall back to 10% of Windows
    inv.sql_vms_detected = sql_vms_all
    inv.sql_vms_prod     = sql_prod_all
    inv.sql_vms_nonprod  = sql_nonprod_all
    if sql_vcpus_all > 0:
        inv.pcores_with_sql_server = round(sql_vcpus_all / ratio)
        inv.sql_detection_source   = "application"
        # ESU SQL pCores: proportion of SQL pCores relative to total Windows
        win_pcore_total = max(inv.pcores_with_windows_server, 1)
        sql_esu_fraction = inv.pcores_with_windows_esu / win_pcore_total
        inv.pcores_with_sql_esu = round(inv.pcores_with_sql_server * sql_esu_fraction)
        print(
            f"[rvtools_parser] SQL detection (Application): "
            f"{sql_vms_all} VMs → {inv.pcores_with_sql_server} pCores  "
            f"({sql_prod_all} Prod / {sql_nonprod_all} non-Prod declared)"
        )
    else:
        inv.pcores_with_sql_server = round(inv.pcores_with_windows_server * 0.10)
        inv.pcores_with_sql_esu    = round(inv.pcores_with_windows_esu * 0.10)
        inv.sql_detection_source   = "default"

    # Clean up private attrs
    for attr in ("_win_vcpus", "_win_esu_vcpus"):
        if hasattr(inv, attr):
            delattr(inv, attr)

    # ------------------------------------------------------------------
    # vCPU tab — per-VM CPU utilisation (Overall MHz / Max MHz) → P95
    # Powered-on VMs only; VMs with Max == 0 are skipped.
    # ------------------------------------------------------------------
    if "vCPU" in wb.sheetnames:
        ws_cpu = wb["vCPU"]
        rc = ws_cpu.iter_rows(values_only=True)
        hc = list(next(rc))
        ci_cp  = _col_index(hc, "Powerstate", [])
        ci_max = _col_index(hc, "Max",        [])
        ci_ovr = _col_index(hc, "Overall",    [])
        cpu_utils: list[float] = []
        for row in rc:
            if ci_cp is None:
                break
            if str(row[ci_cp] or "").lower() != "poweredon":
                continue
            mx = row[ci_max] if ci_max is not None else None
            ov = row[ci_ovr] if ci_ovr is not None else None
            if isinstance(mx, (int, float)) and isinstance(ov, (int, float)) and mx > 0:
                cpu_utils.append(float(ov) / float(mx))
        if cpu_utils:
            cpu_utils.sort()
            p = min(int(len(cpu_utils) * 0.95), len(cpu_utils) - 1)
            inv.cpu_util_p95 = round(cpu_utils[p], 4)
            inv.cpu_util_p95_vm_count = len(cpu_utils)
            print(
                f"[rvtools_parser] CPU P95 utilisation: {inv.cpu_util_p95:.1%}"
                f" ({inv.cpu_util_p95_vm_count:,} powered-on VMs)"
            )
    else:
        warnings.append(
            "vCPU tab not found — CPU utilisation telemetry unavailable. "
            "CPU right-sizing will use the benchmark fallback reduction factor "
            "(default: 40%). For better accuracy re-export RVtools with the vCPU tab enabled."
        )

    # ------------------------------------------------------------------
    # vMemory tab — per-VM memory utilisation (Consumed MiB / Size MiB) → P95
    # ------------------------------------------------------------------
    if "vMemory" in wb.sheetnames:
        ws_mem = wb["vMemory"]
        rm = ws_mem.iter_rows(values_only=True)
        hm = list(next(rm))
        ci_mp  = _col_index(hm, "Powerstate", [])
        ci_sz  = _col_index(hm, "Size MiB",   [])
        ci_con = _col_index(hm, "Consumed",    [])
        mem_utils: list[float] = []
        for row in rm:
            if ci_mp is None:
                break
            if str(row[ci_mp] or "").lower() != "poweredon":
                continue
            sz = row[ci_sz]  if ci_sz  is not None else None
            co = row[ci_con] if ci_con is not None else None
            if isinstance(sz, (int, float)) and isinstance(co, (int, float)) and sz > 0:
                mem_utils.append(float(co) / float(sz))
        if mem_utils:
            mem_utils.sort()
            p = min(int(len(mem_utils) * 0.95), len(mem_utils) - 1)
            inv.memory_util_p95 = round(mem_utils[p], 4)
            inv.memory_util_p95_vm_count = len(mem_utils)
            print(
                f"[rvtools_parser] Memory P95 utilisation: {inv.memory_util_p95:.1%}"
                f" ({inv.memory_util_p95_vm_count:,} powered-on VMs)"
            )
    else:
        warnings.append(
            "vMemory tab not found — memory utilisation telemetry unavailable. "
            "Memory right-sizing will use the benchmark fallback reduction factor "
            "(default: 20%). For better accuracy re-export RVtools with the vMemory tab enabled."
        )

    # ------------------------------------------------------------------
    # vDisk tab — provisioned disk capacity per VM
    # Azure managed disk cost is based on provisioned tier size, not used
    # bytes, so this is the correct basis for the Azure storage estimate.
    # ------------------------------------------------------------------
    if "vDisk" in wb.sheetnames:
        ws_disk = wb["vDisk"]
        rd = ws_disk.iter_rows(values_only=True)
        hd = list(next(rd))
        ci_dvm = _col_index(hd, "VM",           [])
        ci_dp  = _col_index(hd, "Powerstate",   [])
        ci_cap = _col_index(hd, "Capacity MiB", [])
        ci_dt  = _col_index(hd, "Template",     [])
        total_cap_mib_all = 0.0
        total_cap_mib_on  = 0.0
        vm_disks: dict[str, list[float]] = {}
        for row in rd:
            if ci_dt is not None and row[ci_dt] is True:
                continue
            cap = row[ci_cap] if ci_cap is not None else None
            if not isinstance(cap, (int, float)):
                continue
            total_cap_mib_all += float(cap)
            is_on = (
                ci_dp is not None
                and str(row[ci_dp] or "").lower() == "poweredon"
            )
            if is_on:
                total_cap_mib_on += float(cap)
                if ci_dvm is not None and row[ci_dvm]:
                    vm_name = str(row[ci_dvm])
                    if vm_name not in vm_disks:
                        vm_disks[vm_name] = []
                    vm_disks[vm_name].append(round(float(cap) * MIB_TO_GB, 4))
        inv.total_disk_provisioned_gb           = round(total_cap_mib_all * MIB_TO_GB, 2)
        inv.total_disk_provisioned_poweredon_gb = round(total_cap_mib_on  * MIB_TO_GB, 2)
        inv.vm_disk_sizes_gb = vm_disks
        print(
            f"[rvtools_parser] vDisk provisioned: all={inv.total_disk_provisioned_gb:,.0f} GB  "
            f"powered-on={inv.total_disk_provisioned_poweredon_gb:,.0f} GB  "
            f"({len(vm_disks):,} VMs, {sum(len(v) for v in vm_disks.values()):,} disks)"
        )

    # ------------------------------------------------------------------
    # vMetaData tab — vCenter FQDN(s) for region inference
    # ------------------------------------------------------------------
    if "vMetaData" in wb.sheetnames:
        ws_md = wb["vMetaData"]
        rmd = ws_md.iter_rows(values_only=True)
        hmd = list(next(rmd))
        ci_srv = _col_index(hmd, "Server", [])
        fqdns: list[str] = []
        for row in rmd:
            if ci_srv is not None and row[ci_srv]:
                fqdns.append(str(row[ci_srv]).strip().lower())
        inv.vcenter_fqdns = fqdns

    if warnings:
        print(f"[rvtools_parser] {len(warnings)} warning(s):")
        for w in warnings:
            print(f"  - {w}")

    return inv


def summarize(inv: RVToolsInventory) -> None:
    """Print a human-readable summary of a parsed RVToolsInventory."""
    scope = "all VMs" if inv.include_powered_off_applied else "powered-on only"
    print(f"Source: {inv.source_file}")
    print(f"  vHost data available:         {'yes' if inv.vhost_available else 'no':>10}")
    print(f"  TCO baseline scope:           {scope}")
    print()
    print(f"  ── On-Prem TCO Baseline ({scope}) ──")
    print(f"  VMs:                          {inv.num_vms:>10,}")
    print(f"  Hosts:                        {inv.num_hosts:>10,}")
    print(f"  Total vCPU:                   {inv.total_vcpu:>10,}")
    print(f"  Total vMemory (GB):           {inv.total_vmemory_gb:>10,.1f}")
    print(f"  Storage in use (GB):          {inv.total_storage_in_use_gb:>10,.1f}")
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
    print("  ── Azure Migration Target (powered-on only) ──")
    print(f"  VMs (powered-on):             {inv.num_vms_poweredon:>10,}")
    print(f"  vCPU (powered-on):            {inv.total_vcpu_poweredon:>10,}")
    print(f"  vMemory GB (powered-on):      {inv.total_vmemory_gb_poweredon:>10,.1f}")
    print(f"  Storage GB (powered-on):      {inv.total_storage_poweredon_gb:>10,.1f}")
    print()
    print("  ── Utilisation Telemetry ──")
    if inv.cpu_util_p95 > 0:
        print(f"  CPU P95 utilisation:          {inv.cpu_util_p95:>9.1%}  ({inv.cpu_util_p95_vm_count:,} VMs)")
    else:
        print("  CPU P95 utilisation:            n/a  (vCPU tab absent or all VMs powered-off)")
    if inv.memory_util_p95 > 0:
        print(f"  Memory P95 utilisation:       {inv.memory_util_p95:>9.1%}  ({inv.memory_util_p95_vm_count:,} VMs)")
    else:
        print("  Memory P95 utilisation:         n/a  (vMemory tab absent)")
    print()
    print("  ── Region Evidence ──")
    if inv.datacenter_names:
        print(f"  Datacenter(s):  {', '.join(inv.datacenter_names)}")
    if inv.timezone_names:
        print(f"  Time zone(s):   {', '.join(inv.timezone_names)}")
    if inv.gmt_offsets:
        print(f"  GMT offset(s):  {', '.join(inv.gmt_offsets)}")
    if inv.domain_names:
        print(f"  Domain(s):      {', '.join(inv.domain_names)}")
    if inv.vcenter_fqdns:
        print(f"  vCenter FQDN(s): {', '.join(inv.vcenter_fqdns)}")
    if inv.parse_warnings:
        print(f"\n  Warnings ({len(inv.parse_warnings)}):")
        for w in inv.parse_warnings:
            print(f"    \u26a0  {w}")
