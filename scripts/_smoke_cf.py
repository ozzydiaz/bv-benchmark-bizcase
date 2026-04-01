"""Smoke test for cashflow model additions."""
import sys
sys.path.insert(0, ".")

from engine.rvtools_parser import parse
from engine.region_guesser import guess as guess_region
from engine.azure_sku_matcher import get_pricing as fetch_pricing
from engine.consumption_builder import build
from engine.models import (
    BenchmarkConfig, BusinessCaseInputs, EngagementInfo,
    WorkloadInventory, HardwareLifecycle,
)
from engine import status_quo, retained_costs, depreciation, financial_case, outputs

inv = parse("RVTools_export_VCP003_2026-01-05_13.14.03.xlsx")
region = guess_region(inv)
pricing = fetch_pricing(region)
cp = build(inv, pricing)

bm = BenchmarkConfig.from_yaml()
wl = WorkloadInventory(
    workload_name="DC Move",
    num_vms=inv.num_vms,
    allocated_vcpu=inv.total_vcpu,
    allocated_vmemory_gb=inv.total_vmemory_gb,
    allocated_storage_gb=inv.total_storage_in_use_gb,
    pcores_with_windows_server=inv.pcores_with_windows_server,
    pcores_with_windows_esu=inv.pcores_with_windows_esu,
)
inputs = BusinessCaseInputs(
    engagement=EngagementInfo(client_name="VCP003"),
    workloads=[wl],
    consumption_plans=[cp],
    hardware=HardwareLifecycle(),
)

sq_costs = status_quo.compute(inputs, bm)
depr = depreciation.compute(inputs, bm)
ret = retained_costs.compute(inputs, bm, sq_costs)
fc = financial_case.compute(inputs, bm, sq_costs, ret, depr)
summary = outputs.compute(inputs, bm, fc)

print(f"CF NPV 10yr:  ${summary.npv_cf_10yr:>15,.0f}")
print(f"CF NPV 5yr:   ${summary.npv_cf_5yr:>15,.0f}")
print(f"P&L NPV 10yr: ${summary.npv_10yr:>15,.0f}")
print(f"P&L NPV 5yr:  ${summary.npv_5yr:>15,.0f}")
print()
header = f"{'Year':<5}  {'SQ P&L':>12}  {'SQ CF':>12}  {'Az P&L':>12}  {'Az CF':>12}  {'Retained CAPEX':>14}  {'Retained OPEX':>13}  {'Azure Costs':>11}  {'Migration':>10}"
print(header)
print("-" * len(header))
for yr in range(1, 11):
    print(
        f"  Y{yr:<2}  "
        f"${fc.sq_total()[yr]:>11,.0f}  "
        f"${summary.sq_cf_by_year[yr]:>11,.0f}  "
        f"${fc.az_total()[yr]:>11,.0f}  "
        f"${summary.az_cf_by_year[yr]:>11,.0f}  "
        f"${summary.az_cf_capex_by_year[yr]:>13,.0f}  "
        f"${summary.az_cf_opex_by_year[yr]:>12,.0f}  "
        f"${summary.az_cf_azure_by_year[yr]:>10,.0f}  "
        f"${summary.az_cf_migration_by_year[yr]:>9,.0f}"
    )
print()
print(f"Total SQ CF 10yr:  ${summary.total_sq_cf_10yr:>14,.0f}")
print(f"Total Az CF 10yr:  ${summary.total_az_cf_10yr:>14,.0f}")
print(f"Total SQ CF 5yr:   ${summary.total_sq_cf_5yr:>14,.0f}")
print(f"Total Az CF 5yr:   ${summary.total_az_cf_5yr:>14,.0f}")
