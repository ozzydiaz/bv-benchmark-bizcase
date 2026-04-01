"""Debug productivity and NII calculation."""
import sys
sys.path.insert(0, '/Users/ozdiaz/dev/bv-benchmark-bizcase')

from engine.models import (
    BenchmarkConfig, BusinessCaseInputs, WorkloadInventory, ConsumptionPlan,
    EngagementInfo, PricingConfig, DatacenterConfig, HardwareLifecycle, AzureRunRate, YesNo
)
from engine import productivity

bm = BenchmarkConfig.from_yaml('/Users/ozdiaz/dev/bv-benchmark-bizcase/data/benchmarks_default.yaml')
print(f"vms_per_sysadmin: {bm.vms_per_sysadmin}")
print(f"productivity_reduction: {bm.productivity_reduction_after_migration}")
print(f"productivity_recapture: {bm.productivity_recapture_rate}")

inputs = BusinessCaseInputs(
    engagement=EngagementInfo(client_name='Reference Client'),
    pricing=PricingConfig(),
    datacenter=DatacenterConfig(),
    hardware=HardwareLifecycle(expected_future_growth_rate=0.02),
    incorporate_productivity_benefit=YesNo.YES,
    workloads=[WorkloadInventory(
        workload_name='DC Move', num_vms=2155, allocated_vcpu=8755,
        allocated_vmemory_gb=32763, allocated_storage_gb=1172264,
        vcpu_per_core_ratio=1.97, pcores_with_virtualization=4412,
        pcores_with_windows_server=3454, pcores_with_windows_esu=2124,
    )],
    consumption_plans=[ConsumptionPlan(workload_name='DC Move', annual_compute_consumption_lc_y10=2581857.72)],
    azure_run_rate=AzureRunRate(),
)

pb = productivity.compute(inputs, bm)
print(f"\ntotal_vms: {sum(wl.total_vms_and_physical for wl in inputs.workloads)}")
print(f"vms_y10:          {pb.vms_y10:.2f}  (expected: 2626.93)")
print(f"on_prem_fte_y10:  {pb.on_prem_fte_y10:.4f}  (expected: 2.189)")
print(f"adjusted_gain_fte:{pb.adjusted_gain_fte:.4f}  (expected: 0.873)")
print(f"headcount_saved:  {pb.headcount_saved}       (expected: 1)")
print(f"annual_benefit:   {pb.annual_benefit_full:.2f}  (expected: 196587.21)")
