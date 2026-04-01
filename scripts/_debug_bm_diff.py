"""Compare benchmarks and derived workload fields between fact_check and validate runs."""
import sys; sys.path.insert(0, '.')
from scripts.fact_check import build_inputs_from_workbook, _BENCHMARKS_YAML
from scripts.validate_vs_reference import build_inputs as val_inputs, load_benchmarks as val_bm_loader
from engine.models import BenchmarkConfig
import openpyxl, dataclasses

wb = openpyxl.load_workbook(
    "Reliance_BV Benchmark Business Case v6.xlsm", keep_vba=True, data_only=True
)
fc_in = build_inputs_from_workbook(wb)
val_in = val_inputs()
bm_fc = BenchmarkConfig.from_yaml(str(_BENCHMARKS_YAML))
bm_val = val_bm_loader()

print("=== BenchmarkConfig diffs ===")
any_diff = False
for f in bm_fc.model_fields:
    a = getattr(bm_fc, f)
    b = getattr(bm_val, f)
    if a != b:
        print(f"  DIFF: {f}: fact_check={a}  validate={b}")
        any_diff = True
if not any_diff:
    print("  (all identical)")

print("\n=== Derived WorkloadInventory fields ===")
wl_fc = fc_in.workloads[0]
wl_val = val_in.workloads[0]
for attr in ["pcores_with_virtualization", "pcores_with_sql_server", "pcores_with_sql_esu",
             "total_vms_and_physical", "est_physical_servers_incl_hosts"]:
    a = getattr(wl_fc, attr)
    b = getattr(wl_val, attr)
    mark = "  *** DIFF" if a != b else ""
    print(f"  {attr}: fc={a}  val={b}{mark}")
