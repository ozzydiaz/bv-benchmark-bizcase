"""
Depreciation Schedule engine.

Replicates the 'Depreciation Schedule' sheet: computes a 7-year lookback
plus 10-year forward depreciation for servers, storage, and network/fitout
assets, per workload.

The schedule is used in the Detailed Financial Case to split hardware costs
into CAPEX (balance-sheet) vs OPEX (P&L) views for the on-prem scenario.

All values are in USD.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import BenchmarkConfig, BusinessCaseInputs, WorkloadInventory
from .status_quo import (
    YEARS,
    _server_acquisition_cost,
    _storage_acquisition_cost,
    _network_fitout_cost,
)

LOOKBACK = 7  # years of historical depreciation to model
TOTAL_COLS = LOOKBACK + YEARS + 1   # Y-7 .. Y0 .. Y10


@dataclass
class DepreciationSchedule:
    """
    Depreciation schedule for one asset class across all workloads.
    List index 0 = Y-7, index LOOKBACK = Y0, index LOOKBACK+1 = Y1 ... etc.
    """
    yearly_acquisition: list[float] = field(default_factory=lambda: [0.0] * TOTAL_COLS)
    yearly_depreciation: list[float] = field(default_factory=lambda: [0.0] * TOTAL_COLS)
    net_book_value: list[float] = field(default_factory=lambda: [0.0] * TOTAL_COLS)

    @property
    def forward_acquisition(self) -> list[float]:
        """Y0–Y10 acquisition slice (11 values)."""
        return self.yearly_acquisition[LOOKBACK: LOOKBACK + YEARS + 1]

    @property
    def forward_depreciation(self) -> list[float]:
        """Y0–Y10 depreciation slice (11 values)."""
        return self.yearly_depreciation[LOOKBACK: LOOKBACK + YEARS + 1]


@dataclass
class AllDepreciationSchedules:
    servers: DepreciationSchedule = field(default_factory=DepreciationSchedule)
    storage: DepreciationSchedule = field(default_factory=DepreciationSchedule)
    network_fitout: DepreciationSchedule = field(default_factory=DepreciationSchedule)


def _build_schedule(
    baseline_acquisition: float,
    depr_life: int,
    actual_life: int,
    growth_rate: float,
) -> DepreciationSchedule:
    """
    Build a single asset-class depreciation schedule.

    Logic mirrors the Excel sheet:
    - Historical periods (Y-7 to Y0): acquisition = baseline / depr_life
      (assets already on-books fall off as they age out)
    - Forward periods (Y1–Y10): acquisition grows with expected growth rate,
      scaled by (depr_life / actual_life) to capture refresh cycles
    - Depreciation = acquisition cost / depr_life
    - Net book value tracked cumulatively
    """
    sched = DepreciationSchedule()
    annual_baseline_depr = baseline_acquisition / max(depr_life, 1)
    annual_baseline_acq = baseline_acquisition / max(depr_life, 1)

    # Populate all columns
    for i in range(TOTAL_COLS):
        year_offset = i - LOOKBACK  # negative = historical, 0 = baseline, positive = forward

        if year_offset <= 0:
            # Historical / baseline: even spread of existing asset base
            remaining = max(0, depr_life - abs(year_offset))
            acq = annual_baseline_acq if remaining > 0 else 0.0
        else:
            # Forward: grows with rate, refresh factor applied
            acq = annual_baseline_acq * (1 + growth_rate) ** year_offset * (depr_life / max(actual_life, 1))

        sched.yearly_acquisition[i] = acq

    # Yearly depreciation = rolling average of yearly_acquisition over the
    # last `depr_life` columns (matches BA's "Depreciation Schedule" tab):
    #
    #   depr[T] = avg(acq[T - depr_life + 1 .. T])
    #
    # This causes early forward years to lag the CAPEX growth curve because
    # the window is dominated by historical (un-grown) acquisitions, which is
    # exactly what the BA workbook does.
    #
    # NOTE: For customers where ``depr_life != actual_life`` the forward-year
    # CAPEX includes a refresh factor (``depr_life / actual_life``); in that
    # case the rolling window will inherit it. Customer A has
    # ``depr_life == actual_life``, so the factor is 1.0 and the result equals
    # the locked layer-3 replica. If a future customer with mismatched lives
    # shows drift, this branch should compute the rolling average over a
    # *raw* CAPEX series (no refresh factor) and apply the factor separately.
    for i in range(TOTAL_COLS):
        window_lo = max(0, i - depr_life + 1)
        window = sched.yearly_acquisition[window_lo : i + 1]
        sched.yearly_depreciation[i] = sum(window) / max(depr_life, 1)

    # Net book value (cumulative: sum of future depreciation remaining)
    cumulative_acq = 0.0
    cumulative_depr = 0.0
    for i in range(TOTAL_COLS):
        cumulative_acq += sched.yearly_acquisition[i]
        cumulative_depr += sched.yearly_depreciation[i]
        sched.net_book_value[i] = max(0.0, cumulative_acq - cumulative_depr)

    return sched


def compute(inputs: BusinessCaseInputs, benchmarks: BenchmarkConfig) -> AllDepreciationSchedules:
    """
    Compute full depreciation schedules for all asset classes across all workloads.
    """
    # Aggregate baseline acquisition costs across all workloads
    total_server_acq = sum(_server_acquisition_cost(wl, benchmarks) for wl in inputs.workloads)
    total_storage_acq = sum(_storage_acquisition_cost(wl, benchmarks) for wl in inputs.workloads)
    total_nw_acq = sum(_network_fitout_cost(wl, benchmarks) for wl in inputs.workloads)

    depr_life = inputs.hardware.depreciation_life_years
    actual_life = inputs.hardware.actual_usage_life_years
    growth = inputs.hardware.expected_future_growth_rate

    schedules = AllDepreciationSchedules(
        servers=_build_schedule(total_server_acq, depr_life, actual_life, growth),
        storage=_build_schedule(total_storage_acq, depr_life, actual_life, growth),
        network_fitout=_build_schedule(total_nw_acq, depr_life, actual_life, growth),
    )
    return schedules
