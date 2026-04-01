"""
IT Productivity Benefit module.

Replicates the hidden 'IT Productivity' sheet in the BV Benchmark workbook.

Logic (from worksheet reverse-engineering):
  1.  Project total VM + physical server count to Year 10 using the expected
      growth rate.
  2.  Compute the on-prem FTE headcount at Y10: vms_y10 / vms_per_sysadmin.
  3.  Productivity reduction = on_prem_fte_y10 × productivity_reduction_pct.
  4.  Realization premium  = productivity_reduction × (1 − recapture_rate).
  5.  Adjusted gain FTE    = productivity_reduction − realization_premium
                           = on_prem_fte_y10 × reduction × recapture_rate.
  6.  Headcount saved      = floor(adjusted_gain_fte)  (whole FTEs only).
  7.  Annual benefit       = headcount_saved × sysadmin_fully_loaded_cost_yr.
  8.  The benefit ramps in proportionally with the migration schedule.

When incorporate_productivity_benefit = No, all values are zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import BenchmarkConfig, BusinessCaseInputs, YesNo
from .status_quo import YEARS


@dataclass
class ProductivityBenefit:
    """
    Year-by-year IT productivity benefit (reduction in IT operations cost).

    Positive values = savings (Azure case IT cost is lower than SQ).
    """
    # Annual saving per year Y0–Y10 (always 0 at Y0)
    annual_benefit: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # Diagnostic fields
    vms_y10: float = 0.0              # Projected VM count at Y10 (with growth)
    on_prem_fte_y10: float = 0.0      # FTE headcount at Y10 (on SQ trajectory)
    adjusted_gain_fte: float = 0.0    # Net FTE reduction achievable
    headcount_saved: int = 0          # Whole FTEs saved at full migration
    annual_benefit_full: float = 0.0  # $ benefit per year at 100% migration


def compute(
    inputs: BusinessCaseInputs,
    benchmarks: BenchmarkConfig,
) -> ProductivityBenefit:
    """Compute IT productivity benefit for each year Y0–Y10."""
    pb = ProductivityBenefit()

    if inputs.incorporate_productivity_benefit != YesNo.YES:
        return pb

    g = inputs.hardware.expected_future_growth_rate
    total_vms = sum(wl.total_vms_and_physical for wl in inputs.workloads)

    # Step 1 — Project VMs to Y10
    vms_y10 = total_vms * (1.0 + g) ** YEARS
    pb.vms_y10 = vms_y10

    # Step 2 — On-prem FTE at Y10
    on_prem_fte = vms_y10 / benchmarks.vms_per_sysadmin
    pb.on_prem_fte_y10 = on_prem_fte

    # Step 3-5 — Compute gain components
    reduction = on_prem_fte * benchmarks.productivity_reduction_after_migration
    realization_premium = reduction * (1.0 - benchmarks.productivity_recapture_rate)
    adjusted_gain = reduction - realization_premium   # = reduction × recapture_rate
    pb.adjusted_gain_fte = adjusted_gain

    # Step 6 — Whole headcounts only.
    # The Excel sheet applies ceil() to the productivity reduction and floor() to
    # the realization premium, then subtracts to get net headcount saved:
    #   headcount_saved = ceil(reduction_fte) - floor(premium_fte)
    # Example: reduction=0.919 → ceil=1; premium=0.046 → floor=0; saved=1 FTE ✓
    headcount_saved = math.ceil(reduction) - math.floor(realization_premium)
    pb.headcount_saved = headcount_saved

    # Step 7 — Annual dollar benefit
    annual_full = headcount_saved * benchmarks.sysadmin_fully_loaded_cost_yr
    pb.annual_benefit_full = annual_full

    # Step 8 — Ramp proportionally with migration schedule
    # Use the combined average ramp across all consumption plans
    for yr in range(1, YEARS + 1):
        if not inputs.consumption_plans:
            ramp = 0.0
        else:
            ramp = sum(cp.migration_ramp_pct[yr - 1] for cp in inputs.consumption_plans) / len(
                inputs.consumption_plans
            )
        pb.annual_benefit[yr] = annual_full * ramp

    return pb
