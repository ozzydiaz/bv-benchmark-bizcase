"""Probe right-sizing factor: what it is, how it's computed, and engine vs workbook delta."""
import pathlib, sys, openpyxl
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from scripts.fact_check import build_inputs_from_workbook
from engine.models import BenchmarkConfig
from engine.status_quo import compute as compute_sq
from engine.retained_costs import compute as compute_retained
from engine.depreciation import compute as compute_depr
from engine.financial_case import compute as compute_fc
from engine.outputs import compute as compute_outputs

def load(path):
    return openpyxl.load_workbook(path, data_only=True, keep_vba=True)

for wb_name, label in [
    ("Template_BV Benchmark Business Case v6.xlsm", "TEMPLATE"),
    ("Reliance_BV Benchmark Business Case v6.xlsm", "RELIANCE"),
]:
    wb = load(wb_name)
    cv = wb["1-Client Variables"]
    cp2a = wb["2a-Consumption Plan Wk1"]
    rc = wb["Retained Costs Estimation"]

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")

    # Azure right-sized profile from 2a
    az_vcpu   = cp2a["D8"].value
    az_mem_gb = cp2a["D9"].value
    az_stor_gb = cp2a["D10"].value
    on_prem_vcpu = cv["D44"].value
    on_prem_pcores = cv["D66"].value   # vCPU-per-pCore ratio
    win_pcores = cv["D67"].value
    sql_pcores = cv["D70"].value

    print(f"\n  Azure right-sized profile (2a D8/D9/D10):")
    print(f"    Azure vCPU (D8):     {az_vcpu}")
    print(f"    Azure Memory GB (D9):{az_mem_gb}")
    print(f"    Azure Storage GB (D10):{az_stor_gb}")
    print(f"  On-prem:")
    print(f"    Allocated vCPU (D44):  {on_prem_vcpu}")
    print(f"    vCPU-per-pCore (D66):  {on_prem_pcores}")
    print(f"    Win pCores (D67):      {win_pcores}")
    print(f"    SQL pCores (D70):      {sql_pcores}")

    if az_vcpu and on_prem_vcpu and on_prem_pcores:
        az_pcores = az_vcpu / on_prem_pcores
        right_sizing_pct = (az_pcores / win_pcores) - 1 if win_pcores else None
        print(f"\n  Derived Azure pCores = {az_vcpu}/{on_prem_pcores} = {az_pcores:.1f}")
        print(f"  Right-sizing factor = az_pcores/win_pcores - 1 = {right_sizing_pct:.4f}" if right_sizing_pct is not None else "  (cannot derive)")

    # Read Avg. Cores Right-sizing and Revised Qty from retained costs sheet
    print(f"\n  Retained Costs — Windows right-sizing rows:")
    capture = False
    for r in rc.iter_rows(min_row=1, max_row=150, min_col=1, max_col=10, values_only=True):
        label2 = str(r[1] or "")
        if "5.1.1.2 Windows" in label2:
            capture = True
        if capture and any(k in label2 for k in ("Right-sizing", "Revised Qty", "Initial Qty", "Yearly Cost", "Has workload", "Cores moved")):
            print(f"    {label2[:55]:55s} {list(r[2:8])}")
        if capture and "5.1.1.3" in label2:
            break

# Now check what the 2.3% gap actually traces to in Reliance
print(f"\n{'='*70}")
print("  RESIDUAL 2.3% GAP ANALYSIS — Reliance")
print(f"{'='*70}")
wb = load("Reliance_BV Benchmark Business Case v6.xlsm")
inputs = build_inputs_from_workbook(wb)
bm = BenchmarkConfig.from_yaml("data/benchmarks_default.yaml")
sq  = compute_sq(inputs, bm)
ret = compute_retained(inputs, bm, sq)
depr = compute_depr(inputs, bm)
fc  = compute_fc(inputs, bm, sq, ret, depr)
out = compute_outputs(inputs, bm, fc)

cfo = wb["Cash Flow Output - Detailed"]
sfc = wb["Summary Financial Case"]

def cfo_row(col_b):
    for r in cfo.iter_rows(min_row=1, max_row=80, values_only=True):
        if r[1] == col_b:
            return [r[i] or 0.0 for i in range(2, 13)]
    return [0.0]*11

print("\n  Year-by-year SQ vs Azure Total:")
print(f"  {'Yr':>3}  {'Eng SQ':>14}  {'WB SQ':>14}  {'SQ Δ':>7}  |  {'Eng Az':>14}  {'WB Az':>14}  {'Az Δ':>7}  |  {'Eng Sav':>14}  {'WB Sav':>14}  {'Sav Δ':>7}")
wb_sq  = cfo_row("On-Prem Total")
wb_az  = cfo_row("Cloud Scenario Total")
wb_sav = cfo_row("Annual Delta (savings)")
sq_tot = fc.sq_total()
az_tot = fc.az_total()
sav    = fc.savings()
for yr in range(1, 11):
    sq_d  = (sq_tot[yr]-wb_sq[yr])/wb_sq[yr]*100   if wb_sq[yr]  else 0
    az_d  = (az_tot[yr]-wb_az[yr])/wb_az[yr]*100   if wb_az[yr]  else 0
    sav_d = (sav[yr]-wb_sav[yr])/wb_sav[yr]*100    if wb_sav[yr] else 0
    print(f"  {yr:>3}  {sq_tot[yr]:>14,.0f}  {wb_sq[yr]:>14,.0f}  {sq_d:>6.1f}%  |  {az_tot[yr]:>14,.0f}  {wb_az[yr]:>14,.0f}  {az_d:>6.1f}%  |  {sav[yr]:>14,.0f}  {wb_sav[yr]:>14,.0f}  {sav_d:>6.1f}%")

# Drill into retained cost line items vs workbook at Y5 (steady-state)
print("\n  Retained cost line items at Y5 (steady state, engine vs workbook):")
rc_sheet = wb["Retained Costs Estimation"]

def rc_ytot(label_substr):
    for r in rc_sheet.iter_rows(min_row=1, max_row=200, values_only=True):
        if r[1] and label_substr.lower() in str(r[1]).lower() and r[1].strip().startswith("Yearly Cost"):
            return list(r[2:13])
    return None

retained_lines = [
    ("Windows retained[5]",  ret.windows_server_licenses[5]),
    ("SQL retained[5]",      ret.sql_server_licenses[5]),
    ("Virt retained[5]",     ret.virtualization_licenses[5]),
    ("Win ESU retained[5]",  ret.windows_esu[5]),
    ("SQL ESU retained[5]",  ret.sql_esu[5]),
    ("IT admin retained[5]", ret.system_admin_staff[5]),
    ("Backup sw retained[5]",ret.backup_software[5]),
    ("DR sw retained[5]",    ret.dr_software[5]),
    ("DC space retained[5]", ret.dc_lease_space[5]),
    ("DC power retained[5]", ret.dc_power[5]),
]
for name, eng_val in retained_lines:
    print(f"    {name:30s} {eng_val:>14,.2f}")
