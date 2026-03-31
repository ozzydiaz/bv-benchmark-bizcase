"""
Extract benchmark parameter values from the Benchmark Assumptions sheet
and write them to data/benchmarks_default.yaml.
"""
import os
import openpyxl
import yaml

WORKBOOK = "Template_BV Benchmark Business Case v6.xlsm"
SHEET = "Benchmark Assumptions"

# Human-readable keys mapped to row numbers in the sheet
# B col = label, J col = survey backup value, K col = value used (override point)
BENCHMARK_ROWS = {
    # Conversions / constants
    "wacc":                              5,
    "hours_per_year":                    6,
    "watt_to_kwh":                       7,
    "gb_to_tb":                          8,
    # Servers & Storage
    "vm_to_physical_server_ratio":       11,
    "vcpu_to_pcores_ratio":              12,
    "vmem_to_pmem_ratio":                13,
    "server_cost_per_core":              14,
    "server_cost_per_gb_memory":         15,
    "storage_cost_per_gb":               16,
    "storage_gb_included_in_server":     17,
    "backup_storage_cost_per_gb_yr":     18,
    "dr_storage_cost_per_gb_yr":         19,
    "server_hw_maintenance_pct":         20,
    "storage_hw_maintenance_pct":        21,
    # Network & Fitout
    "servers_per_cabinet":               24,
    "core_routers_per_dc":               25,
    "aggregate_routers_per_core":        26,
    "access_switches_per_core":          27,
    "load_balancers_per_core":           28,
    "cabinet_cost":                      30,
    "core_router_cost":                  31,
    "aggregate_router_cost":             32,
    "access_switch_cost":                33,
    "load_balancer_cost":                34,
    "network_hw_maintenance_pct":        35,
    # Licenses - Level B
    "windows_server_license_per_core_yr_b":  38,
    "sql_server_license_per_core_yr_b":      39,
    "windows_esu_per_core_yr_b":             40,
    "sql_esu_per_core_yr_b":                 41,
    # Licenses - Level D
    "windows_server_license_per_core_yr_d":  42,
    "sql_server_license_per_core_yr_d":      43,
    "windows_esu_per_core_yr_d":             44,
    "sql_esu_per_core_yr_d":                 45,
    # Software
    "virtualization_license_per_core_yr":    46,
    "backup_software_per_vm_yr":             47,
    "dr_software_per_vm_yr":                 48,
    # DC / Power
    "unused_power_overhead_pct":             51,
    "space_cost_per_kw_month":               52,
    "power_cost_per_kw_month":               53,
    "on_prem_pue":                           54,
    "thermal_design_power_watt_yr_per_core": 55,
    "storage_power_kwh_yr_per_tb":           56,
    "on_prem_load_factor":                   57,
    # Bandwidth
    "interconnect_cost_per_yr":              60,
    # IT Admin
    "vms_per_sysadmin":                      63,
    "sysadmin_fully_loaded_cost_yr":         64,
    "sysadmin_working_hours_yr":             65,
    "sysadmin_contractor_pct":               66,
    "productivity_reduction_after_migration":67,
    "productivity_recapture_rate":           68,
}


def extract(workbook_path: str = WORKBOOK) -> dict:
    wb = openpyxl.load_workbook(workbook_path, keep_vba=True, data_only=False)
    ws = wb[SHEET]

    # Index all cells by row: col -> value
    row_data: dict[int, dict[int, object]] = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is not None:
                row_data.setdefault(cell.row, {})[cell.column] = cell.value

    result = {}
    for key, row_num in BENCHMARK_ROWS.items():
        cols = row_data.get(row_num, {})
        j_val = cols.get(10)  # column J - survey backup
        k_val = cols.get(11)  # column K - value used

        # K may be a formula that mirrors J (e.g. "=J5") or a literal override
        if isinstance(k_val, str) and k_val.startswith("="):
            k_val = None  # means "same as survey backup"

        # J may also be a formula (e.g. computed constants)
        if isinstance(j_val, str) and j_val.startswith("="):
            j_val = None

        result[key] = {
            "survey_backup": float(j_val) if j_val is not None else None,
            "value_used": float(k_val) if k_val is not None else float(j_val) if j_val is not None else None,
        }

    return result


def main():
    os.makedirs("data", exist_ok=True)
    benchmarks = extract()

    # Flatten for the YAML: just emit value_used as the default, with survey_backup as a comment field
    output = {}
    for key, vals in benchmarks.items():
        output[key] = {
            "default": vals["value_used"],
            "survey_backup": vals["survey_backup"],
        }

    out_path = os.path.join("data", "benchmarks_default.yaml")
    with open(out_path, "w") as f:
        yaml.dump(output, f, default_flow_style=False, sort_keys=False)

    print(f"Wrote {len(output)} benchmark entries to {out_path}")
    for k, v in output.items():
        print(f"  {k:<50s}  default={str(v['default']):<15}  survey={v['survey_backup']}")


if __name__ == "__main__":
    main()
