"""
Full flow-through debug: trace every engine intermediate vs workbook cached values.

Sections:
  1. Inputs reconstructed from workbook
  2. Status Quo per-category vs 'Status Quo Estimation' sheet
  3. Retained Costs per-category vs 'Retained Costs Estimation' sheet
  4. Azure Consumption (new) vs 'Detailed Financial Case' sheet
  5. Azure Case totals vs 'Cash Flow Output - Detailed'
  6. Summary KPIs vs 'Summary Financial Case'
"""
import pathlib, sys, openpyxl
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from scripts.fact_check import build_inputs_from_workbook
from engine.models import BenchmarkConfig
from engine.status_quo import compute as compute_sq
from engine.retained_costs import compute as compute_retained
from engine.depreciation import compute as compute_depr
from engine.financial_case import compute as compute_fc
from engine.outputs import compute as compute_outputs

WB = "<reference-workbook.xlsm>"
wb = openpyxl.load_workbook(WB, data_only=True, keep_vba=True)
inputs = build_inputs_from_workbook(wb)
benchmarks = BenchmarkConfig.from_yaml("data/benchmarks_default.yaml")

# ── helpers ───────────────────────────────────────────────────────────────────
def cv(addr): return wb["1-Client Variables"][addr].value
def cp_ws(addr): return wb["2a-Consumption Plan Wk1"][addr].value
def row_vals(sheet, row_label_col_a, col_b_label=None):
    """Find a row by label in col A (or col B) and return Y0–Y10 values."""
    ws = wb[sheet]
    for r in ws.iter_rows(values_only=True):
        if r[0] == row_label_col_a or (col_b_label and r[1] == col_b_label):
            return list(r[2:13])   # Y0..Y10
    return [None]*11

def compare(label, engine_vals, wb_vals, tol_pct=2.0):
    """Print side-by-side comparison; flag anything >tol_pct off."""
    print(f"\n  ── {label} ──")
    header = f"  {'Yr':>3}  {'Engine':>14}  {'Workbook':>14}  {'Δ%':>7}  Status"
    print(header)
    for i, (e, w) in enumerate(zip(engine_vals, wb_vals)):
        yr = i  # 0-based (Y0..Y10)
        e = e or 0.0
        w = w or 0.0
        if w == 0 and e == 0:
            continue
        delta = ((e - w) / w * 100) if w else float("inf")
        flag = "✓" if abs(delta) <= tol_pct else "✗ FAIL"
        print(f"  {yr:>3}  {e:>14,.0f}  {w:>14,.0f}  {delta:>6.1f}%  {flag}")

# ── 1. KEY INPUTS ──────────────────────────────────────────────────────────────
wl = inputs.workloads[0]
cp = inputs.consumption_plans[0]
print("=" * 70)
print("  1. RECONSTRUCTED INPUTS vs WORKBOOK")
print("=" * 70)
checks = [
    ("num_vms",              wl.num_vms,                          cv("D39")),
    ("allocated_vcpu",       wl.allocated_vcpu,                   cv("D44")),
    ("allocated_storage_gb", wl.allocated_storage_gb,             cv("D54")),
    ("backup_size_gb",       wl.backup_size_gb,                   cv("D58")),
    ("backup_prot_vms",      wl.backup_num_protected_vms,         cv("D59")),
    ("dr_size_gb",           wl.dr_size_gb,                       cv("D60")),
    ("dr_prot_vms",          wl.dr_num_protected_vms,             cv("D61")),
    ("pcores_win",           wl.pcores_with_windows_server,       cv("D67")),
    ("pcores_sql",           wl.pcores_with_sql_server,           cv("D70")),
    ("pcores_sql_esu",       wl.pcores_with_sql_esu,              cv("D71")),
    ("growth_rate",          inputs.hardware.expected_future_growth_rate, cv("D26")),
    ("backup_activated",     cp.backup_activated.value,           cp_ws("E35")),
    ("backup_stor_in_cons",  cp.backup_storage_in_consumption.value, cp_ws("E38")),
    ("backup_sw_in_cons",    cp.backup_software_in_consumption.value, cp_ws("E39")),
    ("dr_activated",         cp.dr_activated.value,               cp_ws("E42")),
    ("dr_stor_in_cons",      cp.dr_storage_in_consumption.value,  cp_ws("E45")),
    ("dr_sw_in_cons",        cp.dr_software_in_consumption.value, cp_ws("E46")),
    ("compute_anchor_y10",   cp.annual_compute_consumption_lc_y10, cp_ws("M28")),
    ("storage_anchor_y10",   cp.annual_storage_consumption_lc_y10, cp_ws("M29")),
    ("ramp_y1",              cp.migration_ramp_pct[0],            cp_ws("E17")),
    ("ramp_y2",              cp.migration_ramp_pct[1],            cp_ws("F17")),
]
print(f"  {'Field':30s} {'Engine':>20} {'Workbook':>20}  Match")
for name, eng, wbk in checks:
    match = "✓" if str(eng) == str(wbk) or (isinstance(eng, float) and isinstance(wbk, float) and abs(eng - wbk) < 0.001) else "✗ MISMATCH"
    print(f"  {name:30s} {str(eng):>20} {str(wbk):>20}  {match}")

# ── 2. RUN ENGINE ──────────────────────────────────────────────────────────────
sq   = compute_sq(inputs, benchmarks)
ret  = compute_retained(inputs, benchmarks, sq)
depr = compute_depr(inputs, benchmarks)
fc   = compute_fc(inputs, benchmarks, sq, ret, depr)
out  = compute_outputs(inputs, benchmarks, fc)

# ── 3. STATUS QUO vs 'Status Quo Estimation' sheet ────────────────────────────
print("\n" + "=" * 70)
print("  2. STATUS QUO COSTS vs 'Status Quo Estimation' sheet")
print("=" * 70)
sq_ws = wb["Status Quo Estimation"]

def sq_row(label_col_b):
    for r in sq_ws.iter_rows(values_only=True):
        if r[1] == label_col_b:
            return [r[i] or 0.0 for i in range(2, 13)]
    return [0.0]*11

compare("Backup & DR (storage) — sq_backup_storage + sq_dr_storage",
        [sq.backup_storage_cost[i] + sq.dr_storage_cost[i] for i in range(11)],
        sq_row("Backup & DR "))
compare("Virtualization Licenses",
        sq.virtualization_licenses,
        sq_row("Virtualization Software"))
compare("Windows + SQL Licenses",
        [sq.windows_server_licenses[i] + sq.sql_server_licenses[i] for i in range(11)],
        sq_row("Windows + SQL Server Liceneses"))
compare("Windows + SQL ESU",
        [sq.windows_esu[i] + sq.sql_esu[i] for i in range(11)],
        sq_row("Windows + SQL Server ESU"))
compare("IT Admin",
        sq.system_admin_staff,
        sq_row("IT Operations Costs"))

# ── 4. AZURE CASE COMPONENTS vs 'Cash Flow Output - Detailed' ─────────────────
print("\n" + "=" * 70)
print("  3. AZURE CASE COST LINES vs 'Cash Flow Output - Detailed'")
print("=" * 70)
cfo = wb["Cash Flow Output - Detailed"]

def cfo_row(col_b_label):
    for r in cfo.iter_rows(min_row=1, max_row=80, values_only=True):
        if r[1] == col_b_label:
            return [r[i] or 0.0 for i in range(2, 13)]
    return [0.0]*11

compare("Backup & DR Storage (Azure-retained)",
        [fc.az_storage_backup_cost[i] + fc.az_storage_dr_cost[i] for i in range(11)],
        cfo_row("Backup & DR Storage"))
compare("Windows + SQL Licenses (Azure-retained)",
        [fc.az_windows_licenses[i] + fc.az_sql_licenses[i] for i in range(11)],
        cfo_row("Windows + SQL Server Liceneses"))
compare("Azure Compute (New)",
        fc.az_azure_consumption,
        cfo_row("Azure Compute (New)"))
compare("Migration Services",
        fc.az_migration_costs,
        cfo_row("Migration Services"))
compare("IT Operations (Azure-retained)",
        fc.az_system_admin,
        cfo_row("IT Operations Costs"))

# ── 5. TOTAL AZURE CASE vs workbook ────────────────────────────────────────────
print("\n" + "=" * 70)
print("  4. AZURE TOTAL vs WORKBOOK 'Cloud Scenario Total'")
print("=" * 70)

def cfo_total_row(col_a=None, col_b=None):
    for r in cfo.iter_rows(min_row=1, max_row=80, values_only=True):
        if (col_a and r[0] == col_a) or (col_b and r[1] == col_b):
            return [r[i] or 0.0 for i in range(2, 13)]
    return [0.0]*11

compare("SQ Total",      fc.sq_total(),   cfo_total_row(col_b="On-Prem Total"))
compare("Azure Total",   fc.az_total(),   cfo_total_row(col_b="Cloud Scenario Total"))
compare("Annual Savings (SQ−Azure)", fc.savings(), cfo_total_row(col_b="Annual Delta (savings)"))

# ── 6. SUMMARY KPIs ────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  5. SUMMARY KPIs vs 'Summary Financial Case'")
print("=" * 70)
sfc = wb["Summary Financial Case"]
def sfc_val(row, col): return sfc.cell(row=row, column=col).value or 0.0

kpis = [
    ("npv_sq_10yr",       out.total_sq_10yr - out.total_az_10yr,  sfc_val(6, 3), "npv_azure: sq-az totals differ?"),
    ("npv_azure_10yr (fact_chkr cell C7)", out.npv_10yr,         sfc_val(7, 3), ""),
    ("npv_sq_10yr (fact_chkr cell C6)",    out.total_sq_10yr,     sfc_val(6, 3), ""),
    ("terminal_value",    out.terminal_value,                     sfc_val(8, 3), ""),
    ("project_npv_10yr",  out.npv_10yr_with_terminal_value,       sfc_val(9, 3), ""),
    ("payback_years",     out.payback_years or 0,                 sfc_val(11, 5), ""),
    ("roi_10yr",          out.roi_10yr,                           sfc_val(6, 5), ""),
]
print(f"  {'Metric':35s} {'Engine':>16} {'Workbook':>16}  {'Δ%':>7}  Status")
for name, eng, wbk, note in kpis:
    eng = eng or 0.0; wbk = wbk or 0.0
    delta = ((eng - wbk) / wbk * 100) if wbk else float("inf")
    flag = "✓" if abs(delta) <= 2.0 else "✗ FAIL"
    print(f"  {name:35s} {eng:>16,.2f} {wbk:>16,.2f}  {delta:>6.1f}%  {flag}  {note}")

# ── 7. BACKUP/DR RAW VALUES ────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  6. BACKUP/DR COST ISOLATION")
print("=" * 70)
print(f"  backup_size_gb in inputs:         {wl.backup_size_gb}")
print(f"  dr_size_gb in inputs:             {wl.dr_size_gb}")
print(f"  backup_activated:                 {cp.backup_activated}")
print(f"  backup_storage_in_consumption:    {cp.backup_storage_in_consumption}")
print(f"  dr_activated:                     {cp.dr_activated}")
print(f"  dr_storage_in_consumption:        {cp.dr_storage_in_consumption}")
print(f"  sq.backup_storage_cost Y1:        {sq.backup_storage_cost[1]:,.2f}")
print(f"  sq.dr_storage_cost Y1:            {sq.dr_storage_cost[1]:,.2f}")
print(f"  ret.backup_storage_cost Y1:       {ret.backup_storage_cost[1]:,.2f}")
print(f"  ret.dr_storage_cost Y1:           {ret.dr_storage_cost[1]:,.2f}")
print(f"  fc.az_storage_backup_cost Y1:     {fc.az_storage_backup_cost[1]:,.2f}")
print(f"  fc.az_storage_dr_cost Y1:         {fc.az_storage_dr_cost[1]:,.2f}")
wb_backup_dr_y1 = (cfo_row("Backup & DR Storage")[1])
print(f"  workbook 'Backup & DR Storage' Y1:{wb_backup_dr_y1:,.2f}")

