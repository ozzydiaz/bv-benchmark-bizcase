"""Probe Summary Financial Case and 5Y CF sheets to map output cell addresses."""
import openpyxl

wb = openpyxl.load_workbook(
    "Template_BV Benchmark Business Case v6.xlsm",
    keep_vba=True,
    data_only=True,
)

for sheet_name in ["Summary Financial Case", "5Y CF with Payback"]:
    ws = wb[sheet_name]
    print(f"\n=== {sheet_name} ===")
    for row in ws.iter_rows(min_row=1, max_row=80, max_col=10):
        for cell in row:
            if cell.value is not None and str(cell.value).strip():
                print(f"  {cell.coordinate:<8}  {repr(cell.value)[:80]}")
