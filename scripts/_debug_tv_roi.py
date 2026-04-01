"""Probe WACC, perpetual growth rate, ROI formula, and terminal value in workbook."""
import pathlib, sys, openpyxl
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from engine.models import BenchmarkConfig
from scripts.fact_check import build_inputs_from_workbook
from engine.status_quo import compute as compute_sq
from engine.retained_costs import compute as compute_retained
from engine.depreciation import compute as compute_depr
from engine.financial_case import compute as compute_fc
from engine.outputs import compute as compute_outputs

bm = BenchmarkConfig.from_yaml("data/benchmarks_default.yaml")
print(f"Engine WACC:                  {bm.wacc}")
print(f"Engine perpetual_growth_rate: {bm.perpetual_growth_rate}")

wb = openpyxl.load_workbook("Reliance_BV Benchmark Business Case v6.xlsm", data_only=True, keep_vba=True)
sfc = wb["Summary Financial Case"]

print("\n=== Summary Financial Case: all non-empty rows (cols B, G, H) ===")
for row in range(1, 35):
    b = sfc.cell(row=row, column=2).value
    g = sfc.cell(row=row, column=7).value
    h = sfc.cell(row=row, column=8).value
    if b is not None or g is not None or h is not None:
        print(f"  row {row:2d}  B={str(b or '')[:45]:45s}  G={g!r}  H={h!r}")

# Also check Benchmark Assumptions sheet for WACC / TV params
ba = wb["Benchmark Assumptions"]
print("\n=== Benchmark Assumptions: rows 1-60 ===")
for r in ba.iter_rows(min_row=1, max_row=60, min_col=1, max_col=6, values_only=True):
    if any(v is not None for v in r):
        print(f"  {list(r)}")

# Run engine and print key intermediates
inputs = build_inputs_from_workbook(wb)
sq    = compute_sq(inputs, bm)
ret   = compute_retained(inputs, bm, sq)
depr  = compute_depr(inputs, bm)
fc    = compute_fc(inputs, bm, sq, ret, depr)
out   = compute_outputs(inputs, bm, fc)

savings = fc.savings()
print(f"\nEngine savings[10]: {savings[10]:,.2f}")
print(f"Engine total_sq_10yr: {out.total_sq_10yr:,.2f}")
print(f"Engine total_az_10yr: {out.total_az_10yr:,.2f}")
print(f"Engine roi_10yr: {out.roi_10yr:.4f}")
print(f"Engine terminal_value: {out.terminal_value:,.2f}")
print(f"Engine npv_10yr: {out.npv_10yr:,.2f}")
total_az_investment = sum(fc.az_azure_consumption[1:]) + sum(fc.az_migration_costs[1:])
total_az_investment_ms_funding = total_az_investment + sum(fc.az_microsoft_funding[1:])
print(f"\nAz consumption sum Y1-Y10: {sum(fc.az_azure_consumption[1:]):,.2f}")
print(f"Az migration sum Y1-Y10:   {sum(fc.az_migration_costs[1:]):,.2f}")
print(f"Total investment (used for ROI): {total_az_investment:,.2f}")
print(f"Total savings Y1-Y10:      {sum(savings[1:]):,.2f}")
print(f"ROI = savings/investment = {sum(savings[1:])/total_az_investment:.4f}")

# Workbook ROI/Terminal Value cells
print("\nWorkbook Summary Financial Case values:")
print(f"  C6 (npv_sq_10yr):      {sfc['C6'].value}")
print(f"  D6 (npv_sq_5yr):       {sfc['D6'].value}")
print(f"  E6 (ROI):              {sfc['E6'].value}")
print(f"  C7 (npv_azure_10yr):   {sfc['C7'].value}")
print(f"  C8 (terminal_value):   {sfc['C8'].value}")
print(f"  C9 (project_npv_10yr): {sfc['C9'].value}")
print(f"  E11 (payback_years):   {sfc['E11'].value}")
print(f"  I32 (payback alt):     {sfc['I32'].value if sfc['I32'].value else 'N/A'}")
