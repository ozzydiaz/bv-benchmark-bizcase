"""
Shared test fixtures.

The 'contoso' fixture mirrors the pre-filled values in the reference
workbook (Template_BV Benchmark Business Case v6.xlsm) so every test
can validate against known workbook outputs.
"""

import pytest
from engine.models import (
    BusinessCaseInputs, EngagementInfo, PricingConfig, DatacenterConfig,
    HardwareLifecycle, WorkloadInventory, ConsumptionPlan, BenchmarkConfig,
    YesNo, PriceLevel, DCExitType,
)


@pytest.fixture
def contoso_workload() -> WorkloadInventory:
    """Workload #1 as entered in the reference workbook."""
    return WorkloadInventory(
        workload_name="DC Move",
        num_vms=13364,
        allocated_vcpu=69129,
        allocated_vmemory_gb=318640.0,
        allocated_storage_gb=8886099.5,
        vcpu_per_core_ratio=1.97,
        pcores_with_windows_server=5425,
        pcores_with_windows_esu=1020,
        pcores_with_sql_server=543,   # 10% of 5425
        pcores_with_sql_esu=102,       # 10% of 1020
    )


@pytest.fixture
def contoso_plan() -> ConsumptionPlan:
    """Consumption plan Wk1 as entered in the reference workbook."""
    return ConsumptionPlan(
        workload_name="DC Move",
        azure_vcpu=50_000,
        azure_memory_gb=412_000.0,
        azure_storage_gb=7_000_000.0,
        migration_cost_per_vm_lc=1500.0,
        migration_ramp_pct=[0.4, 0.8, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        annual_compute_consumption_lc_y10=12_000_000.0,
        annual_storage_consumption_lc_y10=3_000_000.0,
        annual_other_consumption_lc_y10=0.0,
    )


@pytest.fixture
def contoso_inputs(contoso_workload, contoso_plan) -> BusinessCaseInputs:
    """Full BusinessCaseInputs matching the reference workbook."""
    return BusinessCaseInputs(
        engagement=EngagementInfo(client_name="Contoso"),
        pricing=PricingConfig(
            windows_server_price_level=PriceLevel.D,
            sql_server_price_level=PriceLevel.D,
        ),
        datacenter=DatacenterConfig(
            num_datacenters_to_exit=0,
            dc_exit_type=DCExitType.PROPORTIONAL,
            num_interconnects_to_terminate=0,
        ),
        hardware=HardwareLifecycle(
            depreciation_life_years=5,
            actual_usage_life_years=5,
            expected_future_growth_rate=0.10,
            hardware_renewal_during_migration_pct=0.10,
        ),
        incorporate_productivity_benefit=YesNo.YES,
        workloads=[contoso_workload],
        consumption_plans=[contoso_plan],
    )


@pytest.fixture
def default_benchmarks() -> BenchmarkConfig:
    """Default benchmarks loaded from the extracted YAML."""
    return BenchmarkConfig.from_yaml()
