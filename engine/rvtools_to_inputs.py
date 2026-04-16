"""
RVTools → BusinessCaseInputs pipeline.

Full automated path from a raw RVTools .xlsx export to a ready-to-run
BusinessCaseInputs object — no manual numeric entry required.

Public API
----------
workload_inventory_from_rvtools(inv, region) -> WorkloadInventory
    Maps a parsed RVToolsInventory to a WorkloadInventory model.

build_business_case(rvtools_path, client_name, ...) -> PipelineResult
    End-to-end: parse → region-guess → price-fetch → right-size → compose.

Design principle: the user provides ONLY customer name, currency, and
optional ACO/ECIF amounts.  Every numeric input is derived from the
RVTools export and public Azure Retail Prices API.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from engine.rvtools_parser import RVToolsInventory, parse as _parse_rv
from engine.region_guesser import guess as _guess_region
from engine.azure_sku_matcher import get_pricing as _get_pricing, get_vm_catalog as _get_vm_catalog, AzurePricing
from engine.consumption_builder import build_with_validation as _build_cp_with_validation, RightsizingValidation
from engine.models import (
    BenchmarkConfig,
    BusinessCaseInputs,
    ConsumptionPlan,
    DatacenterConfig,
    EngagementInfo,
    HardwareLifecycle,
    MIGRATION_RAMP_PRESETS,
    WorkloadInventory,
    YesNo,
)


# ---------------------------------------------------------------------------
# WorkloadInventory mapper
# ---------------------------------------------------------------------------

def workload_inventory_from_rvtools(
    inv: RVToolsInventory,
    region: str = "",
    workload_name: str = "",
    vcpu_ratio: float = 1.97,
) -> WorkloadInventory:
    """
    Map a parsed RVToolsInventory to a WorkloadInventory.

    TCO scope (all VMs vs powered-on) is already resolved by the parser
    and reflected in inv.num_vms / total_vcpu / etc.  This function is a
    direct field-to-field mapping — no additional inference is done here.

    Physical server count (num_physical_servers_excl_hosts) is set to 0
    because RVTools only exposes virtualised workloads.  The engine will
    derive estimated physical counts from the VM/server ratio benchmark.

    vcpu_ratio : float
        The vCPU-to-pCore ratio to use for pCore derivation.  The Template
        benchmark default is 1.97.  The vHost-calculated average is available
        in inv.vcpu_per_core_ratio for informational display.
    """
    # Always use 1.97 by default (Template benchmark) — caller can
    # override with the vHost-calculated value if preferred.
    vcpu_ratio = vcpu_ratio if vcpu_ratio > 0 else 1.97

    return WorkloadInventory(
        workload_name=workload_name or "RVTools Import",

        # VM counts — TCO scope resolved by parser
        num_vms=inv.num_vms,
        num_physical_servers_excl_hosts=0,  # RVTools = VMs only

        # CPU
        allocated_vcpu=inv.total_vcpu,
        allocated_pcores_excl_hosts=inv.total_host_pcores,

        # Memory
        allocated_vmemory_gb=inv.total_vmemory_gb,
        allocated_pmemory_gb_excl_hosts=round(inv.total_host_memory_gb, 2),

        # Storage: prefer provisioned (Azure bills on provisioned); fall back to in-use
        allocated_storage_gb=(
            inv.total_disk_provisioned_gb
            if inv.total_disk_provisioned_gb > 0
            else inv.total_storage_in_use_gb
        ),

        # Utilisation telemetry (0.0 = not available → engine uses fallback factor)
        cpu_util_p95=inv.cpu_util_p95,
        memory_util_p95=inv.memory_util_p95,
        util_vm_count=inv.cpu_util_p95_vm_count,

        # Region
        inferred_azure_region=region,

        # vCPU/pCore ratio from vHost tab
        vcpu_per_core_ratio=vcpu_ratio,

        # License inventory
        pcores_with_windows_server=inv.pcores_with_windows_server,
        pcores_with_windows_esu=inv.pcores_with_windows_esu,
        pcores_with_sql_server=inv.pcores_with_sql_server,
        pcores_with_sql_esu=inv.pcores_with_sql_esu,
    )


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """All outputs from a single build_business_case() call."""
    inputs: BusinessCaseInputs
    inventory: RVToolsInventory
    pricing: AzurePricing
    region: str
    workload: WorkloadInventory
    plan: ConsumptionPlan
    rightsizing_validation: RightsizingValidation | None = None
    storage_mode: str = "per_vm"   # "per_vm" | "aggregate" — resolved mode used for cost calc
    vcpu_ratio_used: float = 1.97  # ratio applied for pCore derivation
    vcpu_ratio_vhost: float = 0.0  # vHost-calculated average (0 = vHost tab absent)
    warnings: list[str] = field(default_factory=list)

    # Inventory summary helpers ─────────────────────────────────────────────
    @property
    def sql_summary(self) -> dict:
        """SQL detection summary for presentation display."""
        inv = self.inventory
        return {
            "detected":          inv.sql_vms_detected,
            "prod":              inv.sql_vms_prod,
            "nonprod":           inv.sql_vms_nonprod,
            "source":            inv.sql_detection_source,
            "pcores":            inv.pcores_with_sql_server,
            "esu_pcores":        inv.pcores_with_sql_esu,
            "prod_assumed":      inv.sql_prod_assumed,
            "env_tagging":       inv.lifecycle_env_tags_present,
        }

    @property
    def os_summary(self) -> dict:
        """OS distribution summary for display."""
        inv = self.inventory
        total = max(inv.num_vms, 1)
        win = inv.pcores_with_windows_server
        return {
            "total_vms": inv.num_vms,
            "poweredon_vms": inv.num_vms_poweredon,
            "windows_pcores": win,
            "esu_pcores": inv.pcores_with_windows_esu,
            "esu_may_be_understated": inv.esu_count_may_be_understated,
        }


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------

def build_business_case(
    rvtools_path: str | Path,
    client_name: str,
    currency: str = "USD",
    usd_to_local: float = 1.0,
    ramp_preset: str = "Extended (100% by Y3)",
    aco_by_year: list[float] | None = None,
    ecif_by_year: list[float] | None = None,
    benchmarks: BenchmarkConfig | None = None,
    storage_mode: str = "per_vm",
    vcpu_ratio_override: float | None = None,
    workload_name: str = "",
    num_datacenters_to_exit: int = 0,
) -> PipelineResult:
    """
    Full automated pipeline: RVTools file → ready-to-run BusinessCaseInputs.

    Parameters
    ----------
    rvtools_path : str | Path
        Path to the RVTools .xlsx export.
    client_name : str
        Customer name (label only — no inference done from it).
    currency : str
        ISO currency code string for display (default "USD").
    usd_to_local : float
        FX rate: 1 USD = x local currency units.  For USD-denominated
        cases this is always 1.0.
    ramp_preset : str
        Key from MIGRATION_RAMP_PRESETS.  Default = "Extended (100% by Y3)".
    aco_by_year : list[float] | None
        ACO (Azure Consumption Offer) credits per year, years 1–10.
        Pass [aco_y1, aco_y2, ...] or None / all-zeros for no credits.
        Values are negative (inflows) — the function handles sign.
    ecif_by_year : list[float] | None
        ECIF credits per year, years 1–10.  Same sign convention.
    benchmarks : BenchmarkConfig | None
        Override benchmarks.  None → load from data/benchmarks_default.yaml.
    storage_mode : str
        "per_vm" (default — individual disk tier costing) or "aggregate".
    workload_name : str
        Label for the workload.  Defaults to "RVTools Import".
    num_datacenters_to_exit : int
        Number of datacenters the customer intends to exit.  Default 0.

    Returns
    -------
    PipelineResult
        Full result including BusinessCaseInputs and all intermediate objects.
    """
    if benchmarks is None:
        benchmarks = BenchmarkConfig.from_yaml()

    # vCPU/pCore ratio: caller can specify; otherwise use benchmark default 1.97.
    # The vHost-calculated average (inv.vcpu_per_core_ratio) is captured after
    # parsing and stored in PipelineResult for informational display.
    _benchmark_ratio = benchmarks.vcpu_to_pcores_ratio  # 1.97 from Template
    _vhost_ratio = 0.0  # filled after parse
    vcpu_ratio_for_wl = (
        vcpu_ratio_override
        if vcpu_ratio_override is not None and vcpu_ratio_override > 0
        else _benchmark_ratio
    )

    warnings: list[str] = []

    # 1 ── Parse RVTools
    inv = _parse_rv(rvtools_path)

    # Now that inv is available, capture the vHost-calculated ratio for display
    _vhost_ratio = inv.vcpu_per_core_ratio  # 0.0 if vHost tab absent

    # 2 ── Infer Azure region
    region = _guess_region(inv)

    # 3 ── Fetch Azure PAYG pricing (cached 24 h; falls back to benchmarks)
    pricing = _get_pricing(region)

    # 3b ── Fetch per-VM SKU catalog with live prices (cached separately 24 h)
    #      Falls back gracefully to reference per-vCPU rate if offline.
    try:
        vm_catalog = _get_vm_catalog(region)
    except Exception:
        vm_catalog = None

    # 4 ── Build WorkloadInventory
    wl = workload_inventory_from_rvtools(
        inv,
        region=region,
        workload_name=workload_name or client_name,
        vcpu_ratio=vcpu_ratio_for_wl,
    )

    # 5 ── Build ConsumptionPlan (right-sizing + Azure cost estimate)
    _aco  = _pad10(aco_by_year  or [0.0])
    _ecif = _pad10(ecif_by_year or [0.0])

    cp, rs_validation = _build_cp_with_validation(
        inv=inv,
        pricing=pricing,
        benchmarks=benchmarks,
        workload_name=workload_name or client_name,
        usd_to_local=usd_to_local,
        ramp_preset=ramp_preset,
        storage_mode=storage_mode,
        vm_catalog=vm_catalog,
    )

    # Apply ACO / ECIF to the plan
    cp = cp.model_copy(update={
        "aco_by_year":  [v for v in _aco],
        "ecif_by_year": [v for v in _ecif],
    })

    # 6 ── Compose BusinessCaseInputs
    inputs = BusinessCaseInputs(
        engagement=EngagementInfo(
            client_name=client_name,
            local_currency_name=currency,
            usd_to_local_rate=usd_to_local,
        ),
        hardware=HardwareLifecycle(),
        datacenter=DatacenterConfig(
            num_datacenters_to_exit=num_datacenters_to_exit,
        ),
        workloads=[wl],
        consumption_plans=[cp],
    )

    # Carry parser warnings through
    warnings.extend(inv.parse_warnings)

    return PipelineResult(
        inputs=inputs,
        inventory=inv,
        pricing=pricing,
        region=region,
        workload=wl,
        plan=cp,
        rightsizing_validation=rs_validation,
        storage_mode=storage_mode,
        vcpu_ratio_used=vcpu_ratio_for_wl,
        vcpu_ratio_vhost=_vhost_ratio,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pad10(values: list[float]) -> list[float]:
    """Pad or truncate to exactly 10 elements."""
    out = list(values)[:10]
    while len(out) < 10:
        out.append(0.0)
    return out


def build_business_case_from_bytes(
    file_bytes: bytes,
    client_name: str,
    **kwargs,
) -> PipelineResult:
    """
    Convenience wrapper for Streamlit file_uploader.

    Accepts the raw bytes from st.file_uploader, writes them to a temp file,
    and delegates to build_business_case().
    """
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)
    return build_business_case(tmp_path, client_name, **kwargs)
