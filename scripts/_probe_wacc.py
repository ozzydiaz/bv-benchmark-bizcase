"""Scan Benchmark Assumptions sheet for all column values to find WACC."""
import pathlib, sys, openpyxl
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

wb = openpyxl.load_workbook("<reference-workbook.xlsm>", data_only=True, keep_vba=True)
ba = wb["Benchmark Assumptions"]
print("=== Benchmark Assumptions — all non-empty rows (cols A-M) ===")
for r in ba.iter_rows(min_row=1, max_row=80, min_col=1, max_col=13, values_only=True):
    if any(v is not None for v in r):
        print(f"  {[v for v in r]}")
