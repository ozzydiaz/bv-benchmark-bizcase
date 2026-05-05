"""
Output metrics engine.

Replicates the 'Summary Financial Case' sheet metrics: NPV, ROI,
payback period, and the waterfall breakdown.

All values are in USD unless noted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite

from .models import BenchmarkConfig, BusinessCaseInputs
from .financial_case import FinancialCase
from .productivity import ProductivityBenefit, compute as compute_productivity
from .net_interest_income import NetInterestIncome, compute as compute_nii
from .status_quo import YEARS


import logging
_log = logging.getLogger(__name__)

@dataclass
class BusinessCaseSummary:
    """Key financial metrics for a business case run."""

    # Cash flows: savings by year (positive = Azure saves money)
    annual_savings: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # NPV
    npv_10yr: float = 0.0
    npv_5yr: float = 0.0
    npv_10yr_with_terminal_value: float = 0.0
    npv_5yr_with_terminal_value: float = 0.0

    # ROI
    roi_10yr: float = 0.0    # (total savings - total investment) / total investment
    roi_5yr: float = 0.0

    # Payback period in fractional years
    payback_years: float | None = None

    # Terminal value inputs
    terminal_value: float = 0.0

    # Status quo vs Azure case totals
    total_sq_10yr: float = 0.0
    total_az_10yr: float = 0.0
    total_sq_5yr: float = 0.0
    total_az_5yr: float = 0.0

    # Waterfall breakdown (annual average savings by category)
    waterfall: dict[str, float] = field(default_factory=dict)

    # Cost per VM
    on_prem_cost_per_vm_yr: float = 0.0
    azure_cost_per_vm_yr: float = 0.0
    savings_per_vm_yr: float = 0.0

    # Year 10 annualised
    savings_yr10: float = 0.0
    savings_pct_yr10: float = 0.0

    # IT Productivity benefit
    productivity: ProductivityBenefit | None = None

    # Net Interest Income
    nii: NetInterestIncome | None = None

    # ------------------------------------------------------------------
    # Cash Flow view (acquisition-based CAPEX instead of depreciation)
    # ------------------------------------------------------------------
    # Per-year arrays, index 0 = Y0, index 1 = Y1 … index 10 = Y10
    sq_cf_by_year: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_cf_by_year: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    # SQ breakdown
    sq_cf_capex_by_year: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    sq_cf_opex_by_year: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    # Azure breakdown
    az_cf_capex_by_year: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_cf_opex_by_year: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_cf_azure_by_year: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    az_cf_migration_by_year: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    # Cashflow savings (SQ CF - Azure CF)
    annual_cf_savings: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    # Cashflow NPVs
    npv_cf_10yr: float = 0.0
    npv_cf_5yr: float = 0.0
    # Cashflow totals
    total_sq_cf_10yr: float = 0.0
    total_az_cf_10yr: float = 0.0
    total_sq_cf_5yr: float = 0.0
    total_az_cf_5yr: float = 0.0

    # 5Y CF-based ROI/payback — matches Template '5Y CF with Payback' sheet (I31, I32)
    # Use these for the primary displayed ROI/payback metrics; roi_10yr is informational only.
    roi_cf: float = 0.0
    payback_cf: float | None = None


def compute_cf_roi_and_payback(
    fc: FinancialCase,
    benchmarks: BenchmarkConfig,
) -> tuple[float, float]:
    """
    Replicate the '5Y CF with Payback' sheet formulas for ROI (I31) and
    payback (I32).

    The Template separates one-time migration investment from ongoing P&L
    savings and asks: how quickly do the recurring savings recover the
    up-front migration spend?

    Returns (roi, payback_years) where payback_years == 0.0 means payback
    not achieved within 5 years (Template: "More than 5 years").
    """
    wacc   = benchmarks.wacc
    # BA's "5Y CF with Payback" sheet is CASH FLOW (capex+opex), not P&L
    # (depreciation+opex). Use the *_cf accessors so the ROI/payback computed
    # here matches the workbook's I31/I32 cells.
    sq_cf  = fc.sq_total_cf()
    az_cf  = fc.az_total_cf()
    mig    = fc.az_migration_cf()

    # C40: NPV of one-time migration investment (positive magnitude)
    investment_npv = sum(
        mig[yr] / (1 + wacc) ** yr
        for yr in range(1, min(6, len(mig)))
    )
    if investment_npv <= 0:
        return 0.0, 0.0

    # Ongoing run savings = SQ cash cost − Azure ongoing cash (migration excluded)
    run_savings = [
        sq_cf[yr] - (az_cf[yr] - mig[yr])
        for yr in range(len(sq_cf))
    ]

    # C46…G46: cumulative discounted run savings through Y1…Y5
    cumulative: list[float] = []
    cum = 0.0
    for yr in range(1, 6):
        if yr < len(run_savings):
            cum += run_savings[yr] / (1 + wacc) ** yr
        cumulative.append(cum)

    g46 = cumulative[4] if len(cumulative) >= 5 else (cumulative[-1] if cumulative else 0.0)
    roi = (g46 - investment_npv) / investment_npv

    # I32: fractional payback year — match BA's `5Y CF with Payback` C47..G47
    # logic which only fills a payback value when cumulative *crosses* the
    # investment threshold between two consecutive observed years (C46→D46,
    # D46→E46, etc.). If Y1's cumulative already covers the investment (i.e.
    # payback < 1 year), every row's AND condition is False and BA returns 0
    # as a "less than 1 year" sentinel. Mirror that by requiring the previous
    # year to be strictly below threshold AND the current year ≥ threshold,
    # AND requiring at least one observed prior year (yr ≥ 2).
    payback = 0.0
    prev_cum = 0.0
    for yr_idx, cum_yr in enumerate(cumulative):
        yr = yr_idx + 1
        if yr >= 2 and prev_cum < investment_npv and cum_yr >= investment_npv:
            delta = cum_yr - prev_cum
            frac  = (investment_npv - prev_cum) / delta if delta > 0 else 0.0
            payback = (yr - 1) + frac
            break
        prev_cum = cum_yr

    return roi, payback


def _npv(cash_flows: list[float], wacc: float, years: int = YEARS) -> float:
    """Compute NPV of a cash flow series (index 0 = Y0, discounted from Y1)."""
    total = 0.0
    for yr in range(1, years + 1):
        if yr < len(cash_flows):
            total += cash_flows[yr] / (1 + wacc) ** yr
    return total


def _terminal_value(
    cf_last: float,
    wacc: float,
    growth_rate: float,
    benchmarks: BenchmarkConfig | None = None,
) -> float:
    """Terminal value selector.

    By default (``benchmarks`` omitted, or ``benchmarks.tv_method == "gordon"``)
    this returns the Gordon Growth perpetuity ``cf_last × (1+g) / (wacc - g)``,
    matching the BA workbook and the Layer-3 zero-drift oracle.

    v1.6 alternate methods (opt-in via ``BenchmarkConfig.tv_method``):

    * ``"gordon"``         — Gordon Growth perpetuity (default; back-compat).
    * ``"exit_multiple"``  — ``cf_last × benchmarks.tv_exit_multiple``.
    * ``"none"``           — no terminal value (returns 0).

    When ``benchmarks.tv_floor_at_zero`` is True the result is clipped to ``≥ 0``.
    """
    method = "gordon" if benchmarks is None else benchmarks.tv_method

    if method == "none":
        tv = 0.0
    elif method == "exit_multiple":
        multiple = 8.0 if benchmarks is None else benchmarks.tv_exit_multiple
        tv = cf_last * multiple
    else:  # "gordon" (default)
        if wacc <= growth_rate:
            tv = 0.0
        else:
            tv = cf_last * (1 + growth_rate) / (wacc - growth_rate)

    if benchmarks is not None and benchmarks.tv_floor_at_zero and tv < 0.0:
        tv = 0.0
    return tv


def _payback(cumulative_savings: list[float]) -> float | None:
    """
    Return fractional year when cumulative savings first turns positive.
    Returns None if payback is not achieved within the projection window.
    """
    for i in range(1, len(cumulative_savings)):
        if cumulative_savings[i] >= 0:
            prev = cumulative_savings[i - 1]
            curr = cumulative_savings[i]
            if curr == prev:
                return float(i)
            # Linear interpolation within the year
            fraction = abs(prev) / (curr - prev) if (curr - prev) != 0 else 0
            return i - 1 + fraction
    return None


def compute(
    inputs: BusinessCaseInputs,
    benchmarks: BenchmarkConfig,
    fc: FinancialCase,
) -> BusinessCaseSummary:
    """Compute all summary metrics from the assembled FinancialCase."""
    summary = BusinessCaseSummary()
    wacc = benchmarks.wacc
    g = benchmarks.perpetual_growth_rate
    total_vms = sum(wl.total_vms_and_physical for wl in inputs.workloads)

    savings = fc.savings()
    summary.annual_savings = savings

    # Totals
    summary.total_sq_10yr = sum(fc.sq_total()[1:])
    summary.total_az_10yr = sum(fc.az_total()[1:])
    summary.total_sq_5yr = sum(fc.sq_total()[1:6])
    summary.total_az_5yr = sum(fc.az_total()[1:6])

    # NPV
    summary.npv_10yr = _npv(savings, wacc, YEARS)
    summary.npv_5yr = _npv(savings, wacc, 5)

    tv = _terminal_value(savings[YEARS], wacc, g, benchmarks)
    tv_discounted = tv / (1 + wacc) ** YEARS
    tv_5yr_discounted = _terminal_value(savings[5], wacc, g, benchmarks) / (1 + wacc) ** 5

    # terminal_value stored as the PV (discounted to today) — matches workbook C8
    summary.terminal_value = tv_discounted
    summary.npv_10yr_with_terminal_value = summary.npv_10yr + tv_discounted
    summary.npv_5yr_with_terminal_value = summary.npv_5yr + tv_5yr_discounted

    # ROI: (project NPV incl. terminal value) / NPV of Azure costs — matches workbook E6
    # This is the NPV return multiple: how much value the Azure case generates
    # per dollar of present-valued Azure investment.
    npv_az_10yr = _npv(fc.az_total(), wacc, YEARS)
    npv_az_5yr  = _npv(fc.az_total(), wacc, 5)
    summary.roi_10yr = (summary.npv_10yr_with_terminal_value / npv_az_10yr) if npv_az_10yr else 0.0
    summary.roi_5yr  = (summary.npv_5yr_with_terminal_value  / npv_az_5yr)  if npv_az_5yr  else 0.0

    # Payback
    cumulative = [0.0]
    running = 0.0
    for yr in range(1, YEARS + 1):
        running += savings[yr]
        cumulative.append(running)
    summary.payback_years = _payback(cumulative)

    # Year 10 annualised savings
    summary.savings_yr10 = savings[YEARS]
    sq_yr10 = fc.sq_total()[YEARS]
    summary.savings_pct_yr10 = (summary.savings_yr10 / sq_yr10) if sq_yr10 else 0.0

    # Cost per VM (year 10 run-rate)
    vms = max(total_vms, 1)
    summary.on_prem_cost_per_vm_yr = sq_yr10 / vms
    summary.azure_cost_per_vm_yr = fc.az_total()[YEARS] / vms
    summary.savings_per_vm_yr = summary.savings_yr10 / vms

    # Waterfall: average annual cost reduction by category (Y1–Y10)
    def avg(lst: list[float]) -> float:
        return sum(lst[1:]) / YEARS

    sq_hw = avg(fc.sq_total_hardware())
    az_hw = avg([
        fc.az_server_depreciation[i] + fc.az_server_maintenance[i]
        + fc.az_storage_depreciation[i] + fc.az_storage_maintenance[i]
        + fc.az_nw_depreciation[i] + fc.az_nw_maintenance[i]
        for i in range(YEARS + 1)
    ])
    sq_dc = avg(fc.sq_total_datacenter())
    az_dc = avg([fc.az_bandwidth[i] + fc.az_dc_space[i] + fc.az_dc_power[i] for i in range(YEARS + 1)])
    sq_lic = avg(fc.sq_total_licenses())
    az_lic = avg([
        fc.az_virtualization_licenses[i] + fc.az_windows_licenses[i] + fc.az_sql_licenses[i]
        + fc.az_windows_esu[i] + fc.az_sql_esu[i] + fc.az_backup_software[i] + fc.az_dr_software[i]
        for i in range(YEARS + 1)
    ])
    sq_it = avg(fc.sq_system_admin)
    az_it = avg(fc.az_system_admin)
    az_cloud = avg(fc.az_azure_consumption)

    summary.waterfall = {
        "Status Quo (On-Prem)": sq_hw + sq_dc + sq_lic + sq_it,
        "Hardware Costs Reduction": sq_hw - az_hw,
        "Facilities Costs Reduction": sq_dc - az_dc,
        "Licenses Costs Reduction": sq_lic - az_lic,
        "IT Operations Costs Reduction": sq_it - az_it,
        "Azure Consumption Increase": -az_cloud,
        "Azure Case": -(az_hw + az_dc + az_lic + az_it + az_cloud),
    }

    # IT Productivity benefit
    summary.productivity = compute_productivity(inputs, benchmarks)

    # Net Interest Income
    summary.nii = compute_nii(fc, benchmarks)

    # ------------------------------------------------------------------
    # Cash Flow view
    # ------------------------------------------------------------------
    summary.sq_cf_by_year = fc.sq_total_cf()
    summary.az_cf_by_year = fc.az_total_cf()
    summary.sq_cf_capex_by_year = fc.sq_capex()
    summary.sq_cf_opex_by_year = fc.sq_opex_cf()
    summary.az_cf_capex_by_year = fc.az_capex_cf()
    summary.az_cf_opex_by_year = fc.az_opex_cf()
    summary.az_cf_azure_by_year = fc.az_azure_costs_cf()
    summary.az_cf_migration_by_year = fc.az_migration_cf()
    summary.annual_cf_savings = fc.cf_savings()
    summary.npv_cf_10yr = _npv(summary.annual_cf_savings, wacc, YEARS)
    summary.npv_cf_5yr = _npv(summary.annual_cf_savings, wacc, 5)
    summary.total_sq_cf_10yr = sum(fc.sq_total_cf()[1:])
    summary.total_az_cf_10yr = sum(fc.az_total_cf()[1:])
    summary.total_sq_cf_5yr = sum(fc.sq_total_cf()[1:6])
    summary.total_az_cf_5yr = sum(fc.az_total_cf()[1:6])

    # 5Y CF-based ROI/payback (matches Template '5Y CF with Payback' sheet)
    _roi_cf, _payback_cf = compute_cf_roi_and_payback(fc, benchmarks)
    summary.roi_cf = _roi_cf
    summary.payback_cf = _payback_cf if _payback_cf > 0 else None

    return summary


def print_summary(summary: BusinessCaseSummary) -> None:
    """Print a human-readable business case summary."""
    def fmt(v: float) -> str:
        return f"${v:>15,.0f}"

    _log.debug("\n=== Business Case Summary ===")
    _log.debug(f"  10-Year NPV:                {fmt(summary.npv_10yr)}")
    _log.debug(f"  10-Year NPV (w/ TV):        {fmt(summary.npv_10yr_with_terminal_value)}")
    _log.debug(f"  5-Year NPV:                 {fmt(summary.npv_5yr)}")
    _log.debug(f"  10-Year ROI:                {summary.roi_10yr:>14.1%}")
    _log.debug(f"  5-Year ROI:                 {summary.roi_5yr:>14.1%}")
    pb = f"{summary.payback_years:.1f} years" if summary.payback_years else "Not achieved"
    _log.debug(f"  Payback Period:             {pb:>15s}")
    _log.debug(f"  Year-10 Annual Savings:     {fmt(summary.savings_yr10)}")
    _log.debug(f"  Year-10 Savings %:          {summary.savings_pct_yr10:>14.1%}")
    _log.debug(f"  On-Prem Cost/VM/yr:         {fmt(summary.on_prem_cost_per_vm_yr)}")
    _log.debug(f"  Azure Cost/VM/yr:           {fmt(summary.azure_cost_per_vm_yr)}")
    _log.debug(f"  Savings/VM/yr:              {fmt(summary.savings_per_vm_yr)}")
    _log.debug(f"\n  Waterfall (avg annual):")
    for k, v in summary.waterfall.items():
        _log.debug(f"    {k:<40s}: {fmt(v)}")
    _log.debug(f"\n  Annual Savings by Year:")
    for yr, s in enumerate(summary.annual_savings):
        if yr == 0:
            continue
        _log.debug(f"    Y{yr:>2d}: {fmt(s)}")
    if summary.productivity:
        pb = summary.productivity
        _log.debug(f"\n  IT Productivity Benefit:")
        _log.debug(f"    FTEs saved at full migration: {pb.headcount_saved}")
        _log.debug(f"    Annual benefit (full):        {fmt(pb.annual_benefit_full)}")
    if summary.nii:
        n = summary.nii
        _log.debug(f"\n  Net Interest Income (discounted):  {fmt(n.total_discounted_nii)}")
