"""
engine/fact_checker.py
======================
Compares a completed client workbook (.xlsm/.xlsx, saved with computed values)
against the Python engine's output.  Treats the Excel workbook as the
authoritative reference; flags discrepancies by severity.

Typical usage:
    from engine.fact_checker import run, FactCheckReport
    report = run(workbook_path="client.xlsm", inputs=my_inputs, benchmarks=my_bm)
    report.print()

The workbook must have been **saved** in Excel so that formula cells contain
their most-recently computed numeric values (openpyxl reads cached values only;
it does not execute formulas).
"""
from __future__ import annotations

import math
import pathlib
from dataclasses import dataclass, field
from typing import Optional

import openpyxl

from engine.models import BusinessCaseInputs, BenchmarkConfig
from engine import status_quo, retained_costs, depreciation, financial_case, outputs
from engine.productivity import compute as compute_productivity
from engine.net_interest_income import compute as compute_nii


# ---------------------------------------------------------------------------
# Cell address maps — calibrated against Template_BV Benchmark Business Case v6
# ---------------------------------------------------------------------------

# 1-Client Variables: yellow input cells (column D unless noted)
CLIENT_VAR_CELLS: dict[str, str] = {
    "client_name":                  "D9",
    "local_currency":               "D10",
    "workload_name":                "D35",
    "num_vms":                      "D39",
    "num_physical_servers":         "D40",
    "allocated_vcpu":               "D44",
    "allocated_pcores":             "D45",
    "allocated_vmemory_gb":         "D49",
    "allocated_pmemory_gb":         "D50",
    "allocated_storage_gb":         "D54",
    # Backup / DR sizing — required when backup or DR is activated AND
    # included in the Azure consumption plan.  If blank, backup/DR storage
    # should NOT be set to Yes in 2a (the engine treats None/0 as not present).
    "backup_size_gb":               "D58",
    "backup_num_protected_vms":     "D59",
    "dr_size_gb":                   "D60",
    "dr_num_protected_vms":         "D61",
    "vcpu_per_core_ratio":          "D66",
    "pcores_windows_server":        "D67",
    "pcores_windows_esu":           "D68",
    # SQL Server pCore counts — formula default =D67*0.1 / =D68*0.1;
    # overridden with actuals when derived from RVtools or OS audit
    "pcores_sql_server":            "D70",
    "pcores_sql_esu":               "D71",
    "include_run_rate":             "D153",
    "current_acd":                  "D156",
    "new_acd":                      "D157",
    "monthly_spend_usd":            "D160",
    "paygo_mix":                    "D163",
    "ri_mix":                       "D164",
    "sp_mix":                       "D165",
    "sku_mix":                      "D166",
}

# 2a-Consumption Plan: yellow input cells
CONSUMPTION_CELLS: dict[str, str] = {
    "azure_vcpu":                   "D8",
    "azure_memory_gb":              "D9",
    "azure_storage_gb":             "D10",
    # Migration ramp E17:N17 (years 1-10)
    "ramp_y1": "E17", "ramp_y2": "F17", "ramp_y3": "G17", "ramp_y4": "H17",
    "ramp_y5": "I17", "ramp_y6": "J17", "ramp_y7": "K17", "ramp_y8": "L17",
    "ramp_y9": "M17", "ramp_y10": "N17",
    # Per-year ACO (E21:N21)
    "aco_y1": "E21", "aco_y2": "F21", "aco_y3": "G21", "aco_y4": "H21",
    "aco_y5": "I21", "aco_y6": "J21", "aco_y7": "K21", "aco_y8": "L21",
    "aco_y9": "M21", "aco_y10": "N21",
    # Per-year ECIF (E22:N22)
    "ecif_y1": "E22", "ecif_y2": "F22", "ecif_y3": "G22", "ecif_y4": "H22",
    "ecif_y5": "I22", "ecif_y6": "J22", "ecif_y7": "K22", "ecif_y8": "L22",
    "ecif_y9": "M22", "ecif_y10": "N22",
    # Consumption anchors (full-run values; Y10 anchor in col M for 9-year ramp)
    "compute_y1": "E28", "compute_y2": "F28", "compute_y3": "G28",
    "compute_y9": "M28",
    "storage_y1": "E29", "storage_y2": "F29", "storage_y3": "G29",
    "storage_y9": "M29",
    # Options
    "backup_activated":             "E35",
    "backup_stor_in_consumption":   "E38",  # E38 = storage; E39 = software
    "backup_sw_in_consumption":     "E39",
    "dr_activated":                 "E42",
    "dr_stor_in_consumption":       "E45",  # E45 = storage; E46 = software
    "dr_sw_in_consumption":         "E46",
}

# Summary Financial Case: key output cells
SUMMARY_OUTPUT_CELLS: dict[str, str] = {
    # Top-level KPIs — present in all workbook versions
    "npv_sq_10yr":          "C6",
    "npv_sq_5yr":           "D6",
    "roi_10yr":             "E6",
    "npv_azure_10yr":       "C7",
    "npv_azure_5yr":        "D7",
    "terminal_value":       "C8",
    "project_npv_10yr":     "C9",
    "project_npv_excl_tv":  "C10",
    "payback_years":        "E11",  # E11 is consistent; I32 only exists in some versions
}


# ---------------------------------------------------------------------------
# Severity thresholds
# ---------------------------------------------------------------------------

# (label_prefix, weight, critical_pct, warn_pct)
# weight is used for the composite confidence score
SEVERITY_CONFIG: dict[str, tuple[float, float, float]] = {
    # (weight, critical_pct, warn_pct)
    # Weights drive the composite confidence score; critical/warn are Δ% thresholds.
    "project_npv_10yr":     (0.25, 2.0,  5.0),
    "payback_years":        (0.20, 5.0, 10.0),   # 5Y CF-based payback
    "roi_10yr":             (0.15, 2.0,  5.0),   # 5Y CF-based ROI
    "terminal_value":       (0.10, 3.0,  7.0),
    "npv_sq_10yr":          (0.08, 2.0,  5.0),
    "npv_azure_10yr":       (0.08, 2.0,  5.0),
    "npv_sq_5yr":           (0.04, 2.0,  5.0),
    "npv_azure_5yr":        (0.04, 2.0,  5.0),
    "project_npv_excl_tv":  (0.06, 2.0,  5.0),
    "_default":             (0.00, 5.0, 10.0),
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CheckLine:
    name: str
    excel_value: float
    engine_value: float
    delta_pct: float       # (engine - excel) / |excel| × 100
    status: str            # "PASS" | "WARN" | "FAIL" | "SKIP"
    weight: float = 0.0
    note: str = ""


@dataclass
class FactCheckReport:
    workbook_path: str
    checks: list[CheckLine] = field(default_factory=list)
    input_mismatches: list[str] = field(default_factory=list)
    confidence_score: float = 0.0   # 0–100%
    passed: int = 0
    warned: int = 0
    failed: int = 0
    skipped: int = 0

    def print(self) -> None:
        """Print a formatted report to stdout."""
        print(f"\n{'='*70}")
        print(f"  FACT CHECK REPORT")
        print(f"  Workbook: {self.workbook_path}")
        print(f"{'='*70}")

        if self.input_mismatches:
            print("\n  ⚠  INPUT MISMATCHES (workbook vs engine inputs):")
            for m in self.input_mismatches:
                print(f"     • {m}")

        print(f"\n  {'Metric':<35} {'Excel':>16} {'Engine':>16} {'Δ%':>7}  Status")
        print(f"  {'-'*35} {'-'*16} {'-'*16} {'-'*7}  ------")
        for c in self.checks:
            flag = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗", "SKIP": "–"}.get(c.status, " ")
            print(
                f"  {c.name:<35} {c.excel_value:>16,.0f} {c.engine_value:>16,.0f}"
                f" {c.delta_pct:>+6.1f}%  {flag} {c.status}"
                + (f"  [{c.note}]" if c.note else "")
            )

        print(f"\n  Confidence score : {self.confidence_score:.1f}%")
        print(f"  Results          : {self.passed} PASS  {self.warned} WARN  {self.failed} FAIL  {self.skipped} SKIP")
        overall = "PASS ✓" if self.failed == 0 else "FAIL ✗"
        print(f"  Overall          : {overall}")
        print(f"{'='*70}\n")

    @property
    def passed_overall(self) -> bool:
        return self.failed == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cell(ws, addr: str):
    """Return numeric value of a cell, or None."""
    v = ws[addr].value
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct_delta(engine: float, excel: float) -> float:
    if excel == 0:
        return 0.0 if engine == 0 else float("inf")
    return (engine - excel) / abs(excel) * 100.0


def _status(delta_pct: float, crit: float, warn: float) -> str:
    if not math.isfinite(delta_pct):
        return "SKIP"
    if abs(delta_pct) <= warn:
        return "PASS" if abs(delta_pct) <= crit else "WARN"
    return "FAIL"


def _check(name: str, excel_val: Optional[float], engine_val: float) -> CheckLine:
    if excel_val is None or not math.isfinite(engine_val):
        return CheckLine(name, 0.0, engine_val, 0.0, "SKIP", note="Excel value not available")
    weight, crit, warn = SEVERITY_CONFIG.get(name, SEVERITY_CONFIG["_default"])
    dp = _pct_delta(engine_val, excel_val)
    st = _status(dp, crit, warn)
    return CheckLine(name, excel_val, engine_val, dp, st, weight=weight)


# ---------------------------------------------------------------------------
# Input comparison
# ---------------------------------------------------------------------------

def _compare_inputs(
    wb: openpyxl.Workbook,
    inputs: BusinessCaseInputs,
) -> list[str]:
    """
    Read yellow input cells from the workbook and compare to the supplied
    BusinessCaseInputs.  Returns a list of human-readable mismatch strings.
    """
    mismatches: list[str] = []
    cv = wb["1-Client Variables"]
    cp_ws = wb["2a-Consumption Plan Wk1"]

    def check_val(label: str, excel_val, engine_val, tol: float = 0.01):
        if excel_val is None:
            return
        try:
            e = float(excel_val)
            v = float(engine_val) if engine_val is not None else 0.0
            if abs(e) > 0 and abs((v - e) / e) > tol:
                mismatches.append(f"{label}: workbook={e:,.2f}  engine={v:,.2f}")
            elif abs(e) == 0 and abs(v) > tol:
                mismatches.append(f"{label}: workbook=0  engine={v:,.2f}")
        except (TypeError, ValueError):
            # String fields — compare directly
            if str(excel_val).strip().lower() != str(engine_val).strip().lower():
                mismatches.append(f"{label}: workbook={excel_val!r}  engine={engine_val!r}")

    wl = inputs.workloads[0] if inputs.workloads else None
    cp = inputs.consumption_plans[0] if inputs.consumption_plans else None

    check_val("num_vms",          cv[CLIENT_VAR_CELLS["num_vms"]].value,          wl.num_vms if wl else 0)
    check_val("allocated_vcpu",   cv[CLIENT_VAR_CELLS["allocated_vcpu"]].value,    wl.allocated_vcpu if wl else 0)
    check_val("allocated_vmemory_gb", cv[CLIENT_VAR_CELLS["allocated_vmemory_gb"]].value, wl.allocated_vmemory_gb if wl else 0)
    check_val("allocated_storage_gb", cv[CLIENT_VAR_CELLS["allocated_storage_gb"]].value, wl.allocated_storage_gb if wl else 0)
    check_val("vcpu_per_core_ratio",  cv[CLIENT_VAR_CELLS["vcpu_per_core_ratio"]].value,  wl.vcpu_per_core_ratio if wl else 0)
    check_val("pcores_windows_server", cv[CLIENT_VAR_CELLS["pcores_windows_server"]].value, wl.pcores_with_windows_server if wl else 0)
    check_val("pcores_windows_esu",    cv[CLIENT_VAR_CELLS["pcores_windows_esu"]].value,    wl.pcores_with_windows_esu if wl else 0)

    if cp:
        ramp_cells = ["E17","F17","G17","H17","I17","J17","K17","L17","M17","N17"]
        for i, addr in enumerate(ramp_cells):
            check_val(f"ramp_y{i+1}", cp_ws[addr].value, cp.migration_ramp_pct[i])

    return mismatches


# ---------------------------------------------------------------------------
# CF-based ROI and payback (mirrors '5Y CF with Payback' sheet)
# ---------------------------------------------------------------------------

def _compute_cf_roi_and_payback(
    fc: "financial_case.FinancialCase",
    benchmarks: BenchmarkConfig,
) -> tuple[float, float]:
    """
    Replicate the '5Y CF with Payback' sheet formulas for ROI (I31) and
    payback (I32).

    The Template separates one-time migration investment from ongoing P&L
    savings and asks: how quickly do the recurring savings recover the
    up-front migration spend?

    Mapping to Template cells:
        C40 (Investment NPV)  = NPV of net migration costs over Y1–Y5
        G46 (Cumulative run)  = running sum of discounted ongoing P&L savings
        I31 (ROI)             = -(G46 + C40) / C40
        I32 (Payback)         = SUM(C47:G47)  ← interpolated payback year

    Returns (roi, payback_years).
    ``payback_years`` is 0.0 when payback is not achieved within 5 years
    (the Template would show "More than 5 years" and SUM → 0).
    """
    wacc = benchmarks.wacc
    sq_pl = fc.sq_total()          # P&L (depreciation-based) SQ costs
    az_pl = fc.az_total()          # Azure P&L total (includes migration)
    mig   = fc.az_migration_cf()   # net one-time migration costs, positive = cost

    # C40: one-time investment NPV = discounted migration costs Y1–Y5 (positive magnitude)
    investment_npv = sum(
        mig[yr] / (1 + wacc) ** yr
        for yr in range(1, min(6, len(mig)))
    )

    if investment_npv <= 0:
        # No one-time investment → ROI undefined, payback is immediate
        return 0.0, 0.0

    # Ongoing run savings = SQ P&L cost − Azure ongoing P&L (migration excluded)
    # Mirrors DFC rows 78+79 (SQ infra + admin) − same rows (Azure side)
    # plus row 80 (Azure run costs = Azure consumption − 0 for SQ)
    run_savings = [
        sq_pl[yr] - (az_pl[yr] - mig[yr])
        for yr in range(len(sq_pl))
    ]

    # Build C46…G46: cumulative discounted run savings through Y1…Y5
    cumulative: list[float] = []
    cum = 0.0
    for yr in range(1, 6):
        if yr < len(run_savings):
            cum += run_savings[yr] / (1 + wacc) ** yr
        cumulative.append(cum)

    # G46 = cumulative through Y5
    g46 = cumulative[4] if len(cumulative) >= 5 else (cumulative[-1] if cumulative else 0.0)

    # I31: ROI = -(G46 + C40) / C40  where C40 = −investment_npv in the sheet
    # Equivalent: (g46 − investment_npv) / investment_npv
    roi = (g46 - investment_npv) / investment_npv

    # I32: payback = SUM(C47:G47) — fractional year when cumulative >= investment_npv
    payback = 0.0
    prev_cum = 0.0
    for yr_idx, cum_yr in enumerate(cumulative):
        yr = yr_idx + 1          # 1-based year label (C4=1, D4=2, …)
        if cum_yr >= investment_npv:
            delta = cum_yr - prev_cum
            frac = (investment_npv - prev_cum) / delta if delta > 0 else 0.0
            payback = (yr - 1) + frac   # fractional years from projection start
            break
        prev_cum = cum_yr
    # payback == 0.0 → not achieved within 5 years (Template: "More than 5 years")

    return roi, payback


# ---------------------------------------------------------------------------
# Main fact-check runner
# ---------------------------------------------------------------------------

def run(
    workbook_path: str | pathlib.Path,
    inputs: BusinessCaseInputs,
    benchmarks: Optional[BenchmarkConfig] = None,
) -> FactCheckReport:
    """
    Run the full fact check.

    Parameters
    ----------
    workbook_path : path to a saved .xlsm/.xlsx with formula values cached.
    inputs        : BusinessCaseInputs for this engagement.
    benchmarks    : BenchmarkConfig; defaults to BenchmarkConfig() if omitted.

    Returns
    -------
    FactCheckReport
    """
    if benchmarks is None:
        benchmarks = BenchmarkConfig()

    wb = openpyxl.load_workbook(str(workbook_path), keep_vba=True, data_only=True)
    report = FactCheckReport(workbook_path=str(workbook_path))

    # --- 1. Input comparison ------------------------------------------------
    report.input_mismatches = _compare_inputs(wb, inputs)

    # --- 2. Run engine -------------------------------------------------------
    sq = status_quo.compute(inputs, benchmarks)
    depr = depreciation.compute(inputs, benchmarks)
    rc = retained_costs.compute(inputs, benchmarks, sq)
    fc = financial_case.compute(inputs, benchmarks, sq, rc, depr)
    summary = outputs.compute(inputs, benchmarks, fc)
    productivity = compute_productivity(inputs, benchmarks)
    nii = compute_nii(fc, benchmarks)

    # --- 3. Extract Excel outputs -------------------------------------------
    sfc = wb["Summary Financial Case"]

    excel_vals: dict[str, Optional[float]] = {
        k: _cell(sfc, addr)
        for k, addr in SUMMARY_OUTPUT_CELLS.items()
    }

    # --- 4. Build engine comparison values ----------------------------------
    wacc = benchmarks.wacc

    def _npv_series(series: list[float], years: int = 10) -> float:
        return sum(series[yr] / (1 + wacc) ** yr for yr in range(1, min(years + 1, len(series))))

    sq_total = fc.sq_total()
    az_total = fc.az_total()

    # ROI and payback use the same 5Y CF methodology as the Template's
    # '5Y CF with Payback' sheet (I31 and I32) — one-time migration
    # investment vs ongoing P&L savings — NOT the 10Y P&L multi on
    # total Azure costs stored in summary.roi_10yr.
    cf_roi, cf_payback = _compute_cf_roi_and_payback(fc, benchmarks)

    engine_vals: dict[str, float] = {
        "project_npv_10yr":     summary.npv_10yr_with_terminal_value,
        "project_npv_excl_tv":  summary.npv_10yr,
        "terminal_value":       summary.terminal_value,
        "npv_sq_10yr":          _npv_series(sq_total),
        "npv_sq_5yr":           _npv_series(sq_total, 5),
        "npv_azure_10yr":       _npv_series(az_total),
        "npv_azure_5yr":        _npv_series(az_total, 5),
        # 5Y CF-based metrics — matches Template '5Y CF with Payback'!I31 / I32
        "roi_10yr":             cf_roi,
        "payback_years":        cf_payback,
    }

    # --- 5. Generate check lines --------------------------------------------
    for name, engine_val in engine_vals.items():
        report.checks.append(_check(name, excel_vals.get(name), engine_val))

    # --- 6. Tally and compute confidence score ------------------------------
    weighted_pass = 0.0
    total_weight = 0.0
    for c in report.checks:
        if c.status == "PASS":
            report.passed += 1
            weighted_pass += c.weight
        elif c.status == "WARN":
            report.warned += 1
            weighted_pass += c.weight * 0.5
        elif c.status == "FAIL":
            report.failed += 1
        else:
            report.skipped += 1
        total_weight += c.weight

    if total_weight > 0:
        report.confidence_score = (weighted_pass / total_weight) * 100.0
    else:
        non_skip = [c for c in report.checks if c.status != "SKIP"]
        if non_skip:
            report.confidence_score = report.passed / len(non_skip) * 100.0

    return report
