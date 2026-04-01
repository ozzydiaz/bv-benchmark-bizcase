"""
Data models for the BV Benchmark Business Case engine.

All inputs are typed with Pydantic v2. Defaults match the Excel workbook's
pre-filled values so that a minimal run (just workload inventory) produces
a valid business case.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Migration ramp presets
# ---------------------------------------------------------------------------

MIGRATION_RAMP_PRESETS: dict[str, list[float]] = {
    "Express (100% by Y1)":   [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    "Standard (100% by Y2)":  [0.5, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    "Extended (100% by Y3)":  [0.4, 0.8, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    "Custom":                 None,  # type: ignore[dict-item]
}


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class PriceLevel(str, Enum):
    B = "B"
    D = "D"


class DCExitType(str, Enum):
    STATIC = "Static"
    PROPORTIONAL = "Proportional"


class YesNo(str, Enum):
    YES = "Yes"
    NO = "No"


# ---------------------------------------------------------------------------
# Engagement metadata
# ---------------------------------------------------------------------------

class EngagementInfo(BaseModel):
    client_name: str = "Contoso"
    local_currency_name: str = "USD"
    usd_to_local_rate: float = Field(1.0, description="1 USD = x Local Currency")


# ---------------------------------------------------------------------------
# Pricing configuration
# ---------------------------------------------------------------------------

class PricingConfig(BaseModel):
    windows_server_price_level: PriceLevel = PriceLevel.D
    sql_server_price_level: PriceLevel = PriceLevel.D


# ---------------------------------------------------------------------------
# Datacenter configuration
# ---------------------------------------------------------------------------

class DatacenterConfig(BaseModel):
    num_datacenters_to_exit: int = Field(0, ge=0)
    dc_exit_type: DCExitType = DCExitType.PROPORTIONAL
    num_interconnects_to_terminate: int = Field(0, ge=0)


# ---------------------------------------------------------------------------
# Hardware lifecycle assumptions
# ---------------------------------------------------------------------------

class HardwareLifecycle(BaseModel):
    depreciation_life_years: int = Field(5, ge=1)
    actual_usage_life_years: int = Field(5, ge=1)
    expected_future_growth_rate: float = Field(0.10, description="Annual growth rate over 10 years")
    hardware_renewal_during_migration_pct: float = Field(0.10, description="% of on-prem hardware renewed during migration")


# ---------------------------------------------------------------------------
# Per-workload inventory (sourced from RVtools)
# ---------------------------------------------------------------------------

class WorkloadInventory(BaseModel):
    """
    Technical inventory for one workload (Workload #1, #2, or #3).
    VM fields come from the vInfo tab; host fields from the vHost tab.
    """
    workload_name: str = ""

    # VM counts
    num_vms: int = Field(0, ge=0, description="vInfo tab, count of column A")
    num_physical_servers_excl_hosts: int = Field(0, ge=0)

    # CPU
    allocated_vcpu: int = Field(0, ge=0, description="vInfo tab, sum of column O")
    allocated_pcores_excl_hosts: int = Field(0, ge=0)

    # Memory
    allocated_vmemory_gb: float = Field(0.0, ge=0, description="vInfo tab, sum column P / 1024")
    allocated_pmemory_gb_excl_hosts: float = Field(0.0, ge=0)

    # Storage
    allocated_storage_gb: float = Field(0.0, ge=0, description="vInfo tab, sum column AT / 953.67")

    # Utilisation telemetry (from vCPU.Overall/Max and vMemory.Consumed/Size MiB)
    # 0.0 means not available — consumption_builder will use fallback reduction factors.
    cpu_util_p95: float = Field(0.0, ge=0.0, le=500.0, description="P95 CPU utilisation fraction across fleet (Overall/Max)")
    memory_util_p95: float = Field(0.0, ge=0.0, le=500.0, description="P95 memory utilisation fraction across fleet (Consumed/Size MiB)")
    util_vm_count: int = Field(0, ge=0, description="Number of powered-on VMs contributing to utilisation P95")

    # Azure region inferred from vHost/vMetaData metadata
    inferred_azure_region: str = Field("", description="Azure region string inferred from RVtools metadata (e.g. 'uksouth')")

    # Backup / DR (populated if options activated)
    backup_size_gb: Optional[float] = None
    backup_num_protected_vms: Optional[int] = None   # defaults to num_vms + num_physical_servers_excl_hosts
    dr_size_gb: Optional[float] = None
    dr_num_protected_vms: Optional[int] = None        # defaults to num_vms + num_physical_servers_excl_hosts

    # Ratios (used for derived fields; mirrors benchmark K11/K12 but stored on the workload
    # so each workload can have its own ratio if needed)
    vm_to_server_ratio: float = Field(12.0, description="Benchmark K11: VM-to-physical-server ratio")

    # License inventory
    byol_virtualization_for_avs: YesNo = YesNo.NO
    vcpu_per_core_ratio: float = Field(1.97, description="vHost tab, avg column Y (vCPUs per pCore)")
    pcores_with_virtualization: Optional[int] = Field(None, ge=0, description="pCores running virtualization SW; defaults to allocated_vcpu / vcpu_per_core_ratio")
    pcores_with_windows_server: int = Field(0, ge=0, description="vInfo filtered to Windows OS, sum CPU / vcpu_per_core_ratio")
    pcores_with_windows_esu: int = Field(0, ge=0, description="ESU-eligible Windows (pre-2012)")
    pcores_with_sql_server: Optional[int] = None   # defaults to 10% of windows_server if None
    pcores_with_sql_esu: Optional[int] = None       # defaults to 10% of windows_esu if None

    @model_validator(mode="after")
    def derive_defaults(self) -> "WorkloadInventory":
        if self.pcores_with_virtualization is None:
            self.pcores_with_virtualization = round(self.allocated_vcpu / max(self.vcpu_per_core_ratio, 0.01))
        if self.pcores_with_sql_server is None:
            self.pcores_with_sql_server = round(self.pcores_with_windows_server * 0.10)
        if self.pcores_with_sql_esu is None:
            self.pcores_with_sql_esu = round(self.pcores_with_windows_esu * 0.10)
        if self.backup_num_protected_vms is None:
            self.backup_num_protected_vms = self.total_vms_and_physical
        if self.dr_num_protected_vms is None:
            self.dr_num_protected_vms = self.total_vms_and_physical
        return self

    # Derived fields (computed, not entered)
    @property
    def total_vms_and_physical(self) -> int:
        return self.num_vms + self.num_physical_servers_excl_hosts

    @property
    def est_physical_servers_incl_hosts(self) -> float:
        """Estimated total physical servers including VM hosts (workbook D42 = D39/K11 + D40)."""
        return self.num_vms / max(self.vm_to_server_ratio, 0.01) + self.num_physical_servers_excl_hosts

    @property
    def est_allocated_pcores_incl_hosts(self) -> float:
        return self.allocated_vcpu / max(self.vcpu_per_core_ratio, 0.01) + self.allocated_pcores_excl_hosts

    @property
    def _pcores_with_virt_derived(self) -> float:
        """Fallback derived value; actual field pcores_with_virtualization takes precedence."""
        return self.allocated_vcpu / max(self.vcpu_per_core_ratio, 0.01)


# ---------------------------------------------------------------------------
# Per-workload Azure consumption plan (10-year time-series)
# ---------------------------------------------------------------------------

class ConsumptionPlan(BaseModel):
    """
    Azure consumption plan for one workload.
    Year indices 1–10 (index 0 = Y0 baseline, always 0 for consumption).
    """
    workload_name: str = ""

    # Azure workload profile (right-sized targets)
    azure_vcpu: int = Field(0, ge=0)
    azure_memory_gb: float = Field(0.0, ge=0)
    azure_storage_gb: float = Field(0.0, ge=0)

    # Migration
    migration_cost_per_vm_lc: float = Field(1500.0, description="In local currency")

    # Migration ramp-up: EOY cumulative % migrated, years 1–10
    # Defaults: 40% Y1, 80% Y2, 100% Y3 and beyond
    migration_ramp_pct: list[float] = Field(
        default=[0.4, 0.8, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        min_length=10,
        max_length=10,
        description="EOY cumulative migration % for years 1–10",
    )

    # Azure annual consumption in local currency (Y10 anchor; prior years interpolated)
    annual_compute_consumption_lc_y10: float = Field(0.0, ge=0)
    annual_storage_consumption_lc_y10: float = Field(0.0, ge=0)
    annual_other_consumption_lc_y10: float = Field(0.0, ge=0)

    # Azure Consumption Discount — optional, 0.0 = PAYG list price
    # (e.g. 0.15 = 15% off PAYG via CSP/EA/MCA agreement)
    azure_consumption_discount: float = Field(
        0.0, ge=0.0, le=1.0,
        description="ACD: fractional discount off PAYG (0.0–1.0; 0 = no discount)",
    )

    # Microsoft funding (negative values = inflows)
    aco_by_year: list[float] = Field(default=[0.0] * 10, min_length=10, max_length=10)
    ecif_by_year: list[float] = Field(default=[0.0] * 10, min_length=10, max_length=10)

    # Options
    backup_activated: YesNo = YesNo.NO
    backup_storage_in_consumption: YesNo = YesNo.NO
    backup_software_in_consumption: YesNo = YesNo.NO

    dr_activated: YesNo = YesNo.NO
    dr_storage_in_consumption: YesNo = YesNo.NO
    dr_software_in_consumption: YesNo = YesNo.NO


# ---------------------------------------------------------------------------
# Existing Azure run rate (optional)
# ---------------------------------------------------------------------------

class AzureRunRate(BaseModel):
    include_in_business_case: YesNo = YesNo.NO
    current_acd: float = 0.0
    new_acd: float = 0.0
    monthly_spend_usd: float = 0.0
    paygo_mix: float = 0.0
    reserved_instances_mix: float = 0.0
    savings_plan_mix: float = 0.0
    sku_discount_mix: float = 0.0


# ---------------------------------------------------------------------------
# Top-level input container
# ---------------------------------------------------------------------------

class BusinessCaseInputs(BaseModel):
    """
    Complete set of inputs for one business case run.
    Mirrors the `1-Client Variables` + `2a/2b/2c-Consumption Plan` sheets.
    """
    engagement: EngagementInfo = Field(default_factory=EngagementInfo)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    datacenter: DatacenterConfig = Field(default_factory=DatacenterConfig)
    hardware: HardwareLifecycle = Field(default_factory=HardwareLifecycle)
    incorporate_productivity_benefit: YesNo = YesNo.YES

    # Up to 3 workloads
    workloads: list[WorkloadInventory] = Field(default_factory=list, max_length=3)
    consumption_plans: list[ConsumptionPlan] = Field(default_factory=list, max_length=3)

    azure_run_rate: AzureRunRate = Field(default_factory=AzureRunRate)

    @model_validator(mode="after")
    def align_workloads_and_plans(self) -> "BusinessCaseInputs":
        if len(self.workloads) != len(self.consumption_plans):
            raise ValueError(
                f"Number of workloads ({len(self.workloads)}) must match "
                f"number of consumption plans ({len(self.consumption_plans)})"
            )
        return self


# ---------------------------------------------------------------------------
# Benchmark configuration (loaded from data/benchmarks_default.yaml)
# ---------------------------------------------------------------------------

class BenchmarkConfig(BaseModel):
    """
    All 51 benchmark parameters. Defaults match the YAML extracted from
    the reference workbook. Override any value for a client-specific run.
    """
    # Conversions
    wacc: float = 0.07
    hours_per_year: float = 8760.0
    watt_to_kwh: float = 0.001
    gb_to_tb: float = 0.001

    # Servers & Storage
    vm_to_physical_server_ratio: float = 12.0
    vcpu_to_pcores_ratio: float = 7.0
    vmem_to_pmem_ratio: float = 1.0
    server_cost_per_core: float = 147.0
    server_cost_per_gb_memory: float = 16.503
    storage_cost_per_gb: float = 2.2
    storage_gb_included_in_server: float = 0.0
    backup_storage_cost_per_gb_yr: float = 0.15
    dr_storage_cost_per_gb_yr: float = 0.15
    server_hw_maintenance_pct: float = 0.05
    storage_hw_maintenance_pct: float = 0.10

    # Network
    servers_per_cabinet: float = 16.0
    core_routers_per_dc: float = 2.0
    aggregate_routers_per_core: float = 3.0
    access_switches_per_core: float = 13.0
    load_balancers_per_core: float = 2.0
    cabinet_cost: float = 905.995
    core_router_cost: float = 86607.14
    aggregate_router_cost: float = 14572.73
    access_switch_cost: float = 4317.76
    load_balancer_cost: float = 96333.33
    network_hw_maintenance_pct: float = 0.10

    # Licenses - Level B
    windows_server_license_per_core_yr_b: float = 86.16
    sql_server_license_per_core_yr_b: float = 1814.46
    windows_esu_per_core_yr_b: float = 343.72
    sql_esu_per_core_yr_b: float = 6598.34

    # Licenses - Level D
    windows_server_license_per_core_yr_d: float = 73.08
    sql_server_license_per_core_yr_d: float = 1539.60
    windows_esu_per_core_yr_d: float = 291.65
    sql_esu_per_core_yr_d: float = 5598.68

    # Software
    virtualization_license_per_core_yr: float = 208.0
    backup_software_per_vm_yr: float = 239.0
    dr_software_per_vm_yr: float = 240.0

    # DC / Power
    unused_power_overhead_pct: float = 0.25
    space_cost_per_kw_month: float = 338.44
    power_cost_per_kw_month: float = 52.28
    on_prem_pue: float = 1.56
    thermal_design_power_watt_yr_per_core: float = 10.056
    storage_power_kwh_yr_per_tb: float = 10.0
    on_prem_load_factor: float = 0.30

    # Bandwidth
    interconnect_cost_per_yr: float = 100_000.0

    # IT Admin
    vms_per_sysadmin: float = 1200.0
    sysadmin_fully_loaded_cost_yr: float = 196_587.21
    sysadmin_working_hours_yr: float = 2040.0
    sysadmin_contractor_pct: float = 0.32
    productivity_reduction_after_migration: float = 0.42
    productivity_recapture_rate: float = 0.95

    # Azure PAYG baseline rates (for auto-estimating consumption from RVtools inventory)
    # Defaults approximate Azure Dv3 general-purpose PAYG pricing
    payg_cost_per_vcpu_hour: float = 0.048   # $/vCPU/hr  (e.g. D2s v3 = $0.096/hr ÷ 2 vCPU)
    payg_cost_per_gb_month: float = 0.018    # $/GB/month (Standard SSD managed disk tier)

    # NII interest rate — short-term deposit / treasury rate applied to
    # the customer's positive cash differential position
    nii_interest_rate: float = 0.03

    # Financial
    perpetual_growth_rate: float = 0.03

    # Right-sizing parameters
    # When RVtools utilisation telemetry is available (vCPU.Overall /
    # vMemory.Consumed), the engine uses fleet P{utilization_percentile}
    # × headroom factor.  When no telemetry is present, the
    # fallback_reduction factors apply instead.
    cpu_rightsizing_headroom_factor: float = 0.20     # 20% headroom above P95 in Azure
    memory_rightsizing_headroom_factor: float = 0.20  # 20% headroom above P95
    storage_rightsizing_headroom_factor: float = 1.20  # in-use × 1.20
    cpu_rightsizing_fallback_reduction: float = 0.40   # 40% vCPU reduction without telemetry
    memory_rightsizing_fallback_reduction: float = 0.20  # 20% memory reduction without telemetry
    utilization_percentile: int = 95                   # P-value for utilisation analysis

    @classmethod
    def from_yaml(cls, path: str = "data/benchmarks_default.yaml") -> "BenchmarkConfig":
        """Load benchmark defaults from YAML, accepting only the `default` sub-key."""
        import yaml
        with open(path) as f:
            raw = yaml.safe_load(f)
        flat = {k: v["default"] for k, v in raw.items() if v.get("default") is not None}
        return cls(**{k: v for k, v in flat.items() if k in cls.model_fields})

    def windows_license_per_core(self, level: PriceLevel) -> float:
        return (
            self.windows_server_license_per_core_yr_b
            if level == PriceLevel.B
            else self.windows_server_license_per_core_yr_d
        )

    def sql_license_per_core(self, level: PriceLevel) -> float:
        return (
            self.sql_server_license_per_core_yr_b
            if level == PriceLevel.B
            else self.sql_server_license_per_core_yr_d
        )

    def windows_esu_per_core(self, level: PriceLevel) -> float:
        return (
            self.windows_esu_per_core_yr_b
            if level == PriceLevel.B
            else self.windows_esu_per_core_yr_d
        )

    def sql_esu_per_core(self, level: PriceLevel) -> float:
        return (
            self.sql_esu_per_core_yr_b
            if level == PriceLevel.B
            else self.sql_esu_per_core_yr_d
        )
