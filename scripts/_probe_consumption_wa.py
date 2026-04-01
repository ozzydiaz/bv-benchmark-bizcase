"""Probe the Reliance 2a-Consumption Plan sheet specifically."""
import openpyxl

wb = openpyxl.load_workbook(
    "Reliance_BV Benchmark Business Case v6.xlsm", keep_vba=True, data_only=True
)

cp = wb["2a-Consumption Plan Wk1"]
print("=== 2a-Consumption Plan Wk1 (all non-None rows 1-60) ===")
for row in cp.iter_rows(min_row=1, max_row=60, max_col=16):
    for cell in row:
        if cell.value is not None:
            print(f"  {cell.coordinate:<8} {repr(cell.value)[:70]}")
