"""Debug the NPV discrepancy in the fact checker."""
import sys; sys.path.insert(0, '.')
from scripts.fact_check import build_inputs_from_workbook, _BENCHMARKS_YAML
from engine.models import BenchmarkConfig
from engine import status_quo, retained_costs, depreciation, financial_case
import openpyxl

wb = openpyxl.load_workbook(
    "Reliance_BV Benchmark Business Case v6.xlsm", keep_vba=True, data_only=True
)
inputs = build_inputs_from_workbook(wb)
bm = BenchmarkConfig.from_yaml(str(_BENCHMARKS_YAML))
sq = status_quo.compute(inputs, bm)
depr = depreciation.compute(inputs, bm)
rc = retained_costs.compute(inputs, bm, sq)
fc = financial_case.compute(inputs, bm, sq, rc, depr)

sq_t = fc.sq_total()
az_t = fc.az_total()
wacc = 0.07

print(f"sq_total len: {len(sq_t)}")
print("sq_total Y0-Y10:", [f"{v:,.0f}" for v in sq_t])
npv_sq = sum(sq_t[yr] / (1 + wacc) ** yr for yr in range(1, len(sq_t)))
print(f"hand-computed NPV_SQ: {npv_sq:,.0f}  (expected ~48,251,669)")
print()
print("az_total Y0-Y10:", [f"{v:,.0f}" for v in az_t])
npv_az = sum(az_t[yr] / (1 + wacc) ** yr for yr in range(1, len(az_t)))
print(f"hand-computed NPV_AZ: {npv_az:,.0f}  (expected ~31,232,863)")
