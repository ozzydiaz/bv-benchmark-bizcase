"""
Net Interest Income (NII) module.

Replicates the hidden 'Net Interest Income' sheet in the BV Benchmark workbook.

Logic (from worksheet reverse-engineering):
  The customer earns interest on the cash surplus that accumulates when the
  Azure scenario is cheaper than the Status Quo.  Conversely, during periods
  when migration investment makes Azure more expensive, the cash position is
  negative and no interest is earned (interest expense is not modelled).

  Year-by-year algorithm:
    net_outlay_y  = sq_cashflow_y − az_cashflow_y
                    (positive = customer keeps cash by choosing Azure)
    ending_cash_y = ending_cash_{y-1} + net_outlay_y
    NII_y         = max(0, ending_cash_{y-1}) × nii_interest_rate
    disc_NII_y    = NII_y / (1 + wacc)^y
    cumul_disc_y  = cumul_disc_{y-1} + disc_NII_y

  ending_cash carries forward any previously accumulated surplus/deficit, so
  a deficit in early years (migration costs) naturally suppresses NII until
  the position turns positive.

Reference (reference workbook, Y3-Y5 validation):
  NII_y3 = 76,649   ≈ 2,554,969 × 0.03  ✓
  disc_y3 = 62,568  ≈ 76,649 / 1.07^3   ✓
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import BenchmarkConfig
from .financial_case import FinancialCase
from .status_quo import YEARS


@dataclass
class NetInterestIncome:
    """Year-by-year Net Interest Income metrics."""

    # Cash position
    net_cash_outlay: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    ending_cash: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # NII flows
    nii: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    discounted_nii: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))
    cumulative_discounted_nii: list[float] = field(default_factory=lambda: [0.0] * (YEARS + 1))

    # Summary
    total_discounted_nii: float = 0.0


def compute(
    fc: FinancialCase,
    benchmarks: BenchmarkConfig,
) -> NetInterestIncome:
    """Compute Net Interest Income from the assembled FinancialCase."""
    nii_obj = NetInterestIncome()
    rate = benchmarks.nii_interest_rate
    wacc = benchmarks.wacc

    sq_cf = fc.sq_total()
    az_cf = fc.az_total()

    ending_cash = 0.0
    cumul_disc = 0.0

    for yr in range(YEARS + 1):
        net_outlay = sq_cf[yr] - az_cf[yr]
        nii_obj.net_cash_outlay[yr] = net_outlay

        # Interest earned only when beginning cash (= previous ending cash) > 0
        beginning_cash = ending_cash
        nii_yr = max(0.0, beginning_cash) * rate

        ending_cash = beginning_cash + net_outlay
        nii_obj.ending_cash[yr] = ending_cash

        nii_obj.nii[yr] = nii_yr
        disc = nii_yr / (1.0 + wacc) ** yr if yr > 0 else 0.0
        nii_obj.discounted_nii[yr] = disc
        cumul_disc += disc
        nii_obj.cumulative_discounted_nii[yr] = cumul_disc

    nii_obj.total_discounted_nii = cumul_disc
    return nii_obj
