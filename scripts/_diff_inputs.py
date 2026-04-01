"""Compare inputs reconstructed by fact_check vs validate script."""
import openpyxl, sys
sys.path.insert(0, '.')
from scripts.fact_check import build_inputs_from_workbook
from scripts.validate_vs_reference import build_inputs as val_inputs

wb = openpyxl.load_workbook(
    "Reliance_BV Benchmark Business Case v6.xlsm", keep_vba=True, data_only=True
)
fc_in  = build_inputs_from_workbook(wb)
val_in = val_inputs()

wl_fc  = fc_in.workloads[0]
wl_val = val_in.workloads[0]
cp_fc  = fc_in.consumption_plans[0]
cp_val = val_in.consumption_plans[0]
hw_fc  = fc_in.hardware
hw_val = val_in.hardware

print("=== WorkloadInventory diff ===")
for f in ["num_vms","allocated_vcpu","allocated_vmemory_gb","allocated_storage_gb",
          "vcpu_per_core_ratio","pcores_with_windows_server","pcores_with_windows_esu"]:
    a, b = getattr(wl_fc, f), getattr(wl_val, f)
    diff = abs(float(a) - float(b))
    mark = "  *** DIFF" if diff > 0.5 else ""
    print(f"  {f}: fact_check={a}  validate={b}{mark}")

print("\n=== HardwareLifecycle diff ===")
for f in ["depreciation_life_years","actual_usage_life_years",
          "expected_future_growth_rate","hardware_renewal_during_migration_pct"]:
    a, b = getattr(hw_fc, f), getattr(hw_val, f)
    mark = "  *** DIFF" if float(a) != float(b) else ""
    print(f"  {f}: fact_check={a}  validate={b}{mark}")

print("\n=== ConsumptionPlan diff ===")
for f in ["annual_compute_consumption_lc_y10","annual_storage_consumption_lc_y10",
          "migration_cost_per_vm_lc"]:
    a, b = getattr(cp_fc, f), getattr(cp_val, f)
    diff = abs(float(a) - float(b))
    mark = "  *** DIFF" if diff > 0.5 else ""
    print(f"  {f}: fact_check={a}  validate={b}{mark}")
print(f"  ramp fact_check: {cp_fc.migration_ramp_pct}")
print(f"  ramp validate:   {cp_val.migration_ramp_pct}")
