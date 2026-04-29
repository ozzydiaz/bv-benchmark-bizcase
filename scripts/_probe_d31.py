"""Probe D31 productivity toggle and IT admin formula linkage across Template and reference."""
import pathlib, sys, openpyxl
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

def load(name):
    return openpyxl.load_workbook(name, data_only=True, keep_vba=True)

wb_t = load("Template_BV Benchmark Business Case v6.xlsm")
wb_r = load("<reference-workbook.xlsm>")

for label, wb in [("TEMPLATE", wb_t), ("REFERENCE", wb_r)]:
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")

    cv = wb["1-Client Variables"]
    print("\n-- 1-Client Variables: rows 26-35 (left context) --")
    for row in range(26, 36):
        for col in ("C", "D", "E", "F"):
            v = cv[f"{col}{row}"].value
            if v is not None:
                print(f"  {col}{row} = {v!r}")

    # Status Quo Estimation — find IT Operations / admin rows
    sq = wb["Status Quo Estimation"]
    print("\n-- Status Quo Estimation: all rows (cols A-H, skip empties) --")
    for r in sq.iter_rows(min_row=1, max_row=100, min_col=1, max_col=8, values_only=True):
        if any(v is not None for v in r):
            print(f"  {list(r)}")

    # Retained Costs Estimation — full scan
    rc = wb["Retained Costs Estimation"]
    print("\n-- Retained Costs Estimation: all rows (cols A-H, skip empties) --")
    for r in rc.iter_rows(min_row=1, max_row=100, min_col=1, max_col=8, values_only=True):
        if any(v is not None for v in r):
            print(f"  {list(r)}")

    # IT Productivity sheet — first 20 rows
    try:
        prod = wb["IT Productivity"]
        print("\n-- IT Productivity: rows 1-25 --")
        for r in prod.iter_rows(min_row=1, max_row=25, min_col=1, max_col=8, values_only=True):
            if any(v is not None for v in r):
                print(f"  {list(r)}")
    except KeyError:
        print("\n-- No 'IT Productivity' sheet --")
