import sys
sys.path.insert(0, '.')
from engine.rvtools_parser import parse, summarize
from engine import region_guesser
from engine.azure_sku_matcher import benchmark_pricing
from engine.consumption_builder import build
from engine.models import BenchmarkConfig

inv = parse('RVTools_export_VCP003_2026-01-05_13.14.03.xlsx')
print()
summarize(inv)
print()

region = region_guesser.guess(inv)
print(f'Region: {region}')

pricing = benchmark_pricing()
benchmarks = BenchmarkConfig()

print('\n=== AGGREGATE MODE ===')
cp_agg = build(inv=inv, pricing=pricing, benchmarks=benchmarks,
               workload_name='DC Move', storage_mode='aggregate')
print(f'  azure_stor_gb: {cp_agg.azure_storage_gb:,}')
print(f'  storage_lc:    ${cp_agg.annual_storage_consumption_lc_y10:,.0f}/yr')

print('\n=== PER-VM MODE (Standard SSD) ===')
cp_vm = build(inv=inv, pricing=pricing, benchmarks=benchmarks,
              workload_name='DC Move', storage_mode='per_vm', disk_type='standard_ssd')
print(f'  azure_stor_gb: {cp_vm.azure_storage_gb:,}')
print(f'  storage_lc:    ${cp_vm.annual_storage_consumption_lc_y10:,.0f}/yr')

print('\n=== PER-VM MODE (Premium SSD) ===')
cp_prem = build(inv=inv, pricing=pricing, benchmarks=benchmarks,
                workload_name='DC Move', storage_mode='per_vm', disk_type='premium_ssd')
print(f'  azure_stor_gb: {cp_prem.azure_storage_gb:,}')
print(f'  storage_lc:    ${cp_prem.annual_storage_consumption_lc_y10:,.0f}/yr')
