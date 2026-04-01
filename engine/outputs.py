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


def _npv(cash_flows: list[float], wacc: float, years: int = YEARS) -> float:
    """Compute NPV of a cash flow series (index 0 = Y0, discounted from Y1)."""
    total = 0.0
    for yr in range(1, years + 1):
        if yr < len(cash_flows):
            total += cash_flows[yr] / (1 + wacc) ** yr
    return total


def _terminal_value(cf_last: float, wacc: float, growth_rate: float) -> float:
    """Gordon Growth Model terminal value."""
    if wacc <= growth_rate:
        return 0.0
    return cf_last * (1 + growth_rate) / (wacc - growth_rate)


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

    tv = _terminal_value(savings[YEARS], wacc, g)
    tv_discounted = tv / (1 + wacc) ** YEARS
    tv_5yr_discounted = _terminal_value(savings[5], wacc, g) / (1 + wacc) ** 5

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

    return summary


def print_summary(summary: BusinessCaseSummary) -> None:
    """Print a human-readable business case summary."""
    def fmt(v: float) -> str:
        return f"${v:>15,.0f}"

    print("\n=== Business Case Summary ===")
    print(f"  10-Year NPV:                {fmt(summary.npv_10yr)}")
    print(f"  10-Year NPV (w/ TV):        {fmt(summary.npv_10yr_with_terminal_value)}")
    print(f"  5-Year NPV:                 {fmt(summary.npv_5yr)}")
    print(f"  10-Year ROI:                {summary.roi_10yr:>14.1%}")
    print(f"  5-Year ROI:                 {summary.roi_5yr:>14.1%}")
    pb = f"{summary.payback_years:.1f} years" if summary.payback_years else "Not achieved"
    print(f"  Payback Period:             {pb:>15s}")
    print(f"  Year-10 Annual Savings:     {fmt(summary.savings_yr10)}")
    print(f"  Year-10 Savings %:          {summary.savings_pct_yr10:>14.1%}")
    print(f"  On-Prem Cost/VM/yr:         {fmt(summary.on_prem_cost_per_vm_yr)}")
    print(f"  Azure Cost/VM/yr:           {fmt(summary.azure_cost_per_vm_yr)}")
    print(f"  Savings/VM/yr:              {fmt(summary.savings_per_vm_yr)}")
    print(f"\n  Waterfall (avg annual):")
    for k, v in summary.waterfall.items():
        print(f"    {k:<40s}: {fmt(v)}")
    print(f"\n  Annual Savings by Year:")
    for yr, s in enumerate(summary.annual_savings):
        if yr == 0:
            continue
        print(f"    Y{yr:>2d}: {fmt(s)}")
    if summary.productivity:
        pb = summary.productivity
        print(f"\n  IT Productivity Benefit:")
        print(f"    FTEs saved at full migration: {pb.headcount_saved}")
        print(f"    Annual benefit (full):        {fmt(pb.annual_benefit_full)}")
    if summary.nii:
        n = summary.nii
        print(f"\n  Net Interest Income (discounted):  {fmt(n.total_discounted_nii)}")
