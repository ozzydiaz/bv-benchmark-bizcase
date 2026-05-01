"""
Layer 3 BA Replica — Cash Flow View + Status-Quo NPV
=====================================================

Translates the Status Quo P&L into the **Cash Flow View** that drives every
NPV / ROI / Payback calculation in the BA workbook, and computes the pure
Status-Quo NPV scalars (project NPV requires the Azure case — separate
module).

Cash Flow View (Summary Financial Case rows 16-19)
--------------------------------------------------
The BA spreads on-prem cost into two cash-flow buckets:

* **CAPEX** = sustaining hardware-replacement spending. Uses the
  baseline acquisition cost (server + storage + NW) growing at
  ``(1+g)^t`` straight (NOT the rolling 5-year P&L average).
* **OPEX**  = every cost that is NOT depreciation: maintenance, DC space
  + power, bandwidth, all licenses, IT admin. Smooth lines grow at
  ``(1+g)^t`` while the IT-admin step function is added separately.
* **Total** = CAPEX + OPEX.

NPV Calculation
---------------
* Discount rate: ``Benchmark Assumptions!K5`` (WACC, default 7%).
* Annual NPV at year *t* = ``CF[t] / (1 + wacc) ** t`` for ``t in 1..N``.
* Y0 is excluded — it represents "now", before any future discounting.
* Terminal value (Gordon growth): ``CF[Y10] * (1 + g_perp) / (wacc - g_perp)``.

This module covers 41 of the remaining 164 uncovered cells:
  * ``cash_flow.SQ CAPEX.Y0..Y10``      (11)
  * ``cash_flow.SQ OPEX.Y0..Y10``       (11)
  * ``cash_flow.SQ Total CF.Y0..Y10``   (11)
  * ``headline.npv_sq_10y/5y``           (2)
  * ``detailed_npv.{wacc, perpetual_growth_rate, npv_10y_excl_tv,
    annual_npv_y1, annual_npv_y10, terminal_value_10y_raw,
    npv_with_tv_10y_raw}``               (7)
"""

from __future__ import annotations

from .layer3_inputs import InputsBenchmark, InputsClient
from .layer3_status_quo import (
    N_YEARS,
    compute_baselines,
    compute_it_admin_series,
)


# ---------------------------------------------------------------------------
# Cash-flow view helpers
# ---------------------------------------------------------------------------


def _smooth_opex_baseline(client: InputsClient, bm: InputsBenchmark) -> float:
    """
    Y0 baseline of every OPEX line that grows smoothly at (1+g)^t.

    Excludes IT Admin (step function — handled separately).
    """
    base = compute_baselines(client, bm)
    return (
        base.server_maintenance_y0
        + base.storage_maintenance_y0
        + base.network_maintenance_y0
        + base.storage_backup_y0
        + base.storage_dr_y0
        + base.bandwidth_y0
        + base.dc_lease_space_y0
        + base.dc_power_y0
        + base.virtualization_licenses_y0
        + base.windows_licenses_y0
        + base.sql_licenses_y0
        + base.windows_esu_y0
        + base.sql_esu_y0
        + base.backup_licenses_y0
        + base.dr_licenses_y0
    )


def _capex_baseline(client: InputsClient, bm: InputsBenchmark) -> float:
    """Y0 baseline CAPEX = server_depr_y0 + storage_depr_y0 + nw_depr_y0."""
    base = compute_baselines(client, bm)
    return (
        base.server_depreciation_y0
        + base.storage_depreciation_y0
        + base.nw_fitout_depreciation_y0
    )


def compute_status_quo_cash_flow(
    client: InputsClient, bm: InputsBenchmark
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    """Return ``(sq_capex, sq_opex, sq_total)`` series of length ``N_YEARS``."""
    g = client.expected_future_growth_rate
    capex_base = _capex_baseline(client, bm)
    opex_base = _smooth_opex_baseline(client, bm)
    admin_series = compute_it_admin_series(client, bm)

    sq_capex = tuple(capex_base * (1.0 + g) ** t for t in range(N_YEARS))
    sq_opex = tuple(opex_base * (1.0 + g) ** t + admin_series[t] for t in range(N_YEARS))
    sq_total = tuple(sq_capex[t] + sq_opex[t] for t in range(N_YEARS))
    return sq_capex, sq_opex, sq_total


# ---------------------------------------------------------------------------
# NPV math
# ---------------------------------------------------------------------------


def discount_series(cash_flows: tuple[float, ...], wacc: float, n_years: int) -> tuple[float, ...]:
    """
    Annual NPV: ``CF[t] / (1+wacc)^t`` for ``t in 1..n_years``.

    ``cash_flows`` must be indexed Y0..YN (so ``cash_flows[1]`` is Y1).
    Y0 is **excluded** from NPV (it represents present-time, not discounted).
    """
    return tuple(cash_flows[t] / (1.0 + wacc) ** t for t in range(1, n_years + 1))


def total_npv(cash_flows: tuple[float, ...], wacc: float, n_years: int) -> float:
    """Sum of discounted Y1..YN cash flows."""
    return sum(discount_series(cash_flows, wacc, n_years))


def terminal_value(cf_terminal: float, wacc: float, perp_growth: float) -> float:
    """
    Gordon growth model: ``CF * (1 + g) / (wacc - g)``.

    ``cf_terminal`` is the run-rate cash flow in the terminal year (typically Y10).
    Returned value is **undiscounted** — caller divides by ``(1+wacc)^N`` to
    get the present value.
    """
    if wacc <= perp_growth:
        raise ValueError(f"WACC ({wacc}) must exceed perpetual growth ({perp_growth})")
    return cf_terminal * (1.0 + perp_growth) / (wacc - perp_growth)


# ---------------------------------------------------------------------------
# Main entry — emits the auditor-keyed dict
# ---------------------------------------------------------------------------


def compute_status_quo_cash_flow_dict(
    client: InputsClient, bm: InputsBenchmark
) -> dict[str, float]:
    """
    Return a flat dict whose keys match the auditor's labels for:

    * ``cash_flow.SQ CAPEX.Y0..Y10``
    * ``cash_flow.SQ OPEX.Y0..Y10``
    * ``cash_flow.SQ Total CF.Y0..Y10``
    * ``headline.npv_sq_10y``, ``headline.npv_sq_5y``
    * ``detailed_npv.wacc``, ``detailed_npv.perpetual_growth_rate``
    * ``detailed_npv.npv_10y_excl_tv``
    * ``detailed_npv.annual_npv_y1``, ``detailed_npv.annual_npv_y10``
    * ``detailed_npv.terminal_value_10y_raw``
    * ``detailed_npv.npv_with_tv_10y_raw``
    """
    sq_capex, sq_opex, sq_total = compute_status_quo_cash_flow(client, bm)

    out: dict[str, float] = {}
    for t in range(N_YEARS):
        out[f"cash_flow.SQ CAPEX.Y{t}"] = sq_capex[t]
        out[f"cash_flow.SQ OPEX.Y{t}"] = sq_opex[t]
        out[f"cash_flow.SQ Total CF.Y{t}"] = sq_total[t]

    wacc = bm.wacc
    perp = bm.perpetual_growth_rate

    annual_10y = discount_series(sq_total, wacc, n_years=10)
    npv_sq_10y = sum(annual_10y)
    npv_sq_5y = sum(discount_series(sq_total, wacc, n_years=5))

    # Terminal value (undiscounted) at Y10
    tv_y10_raw = terminal_value(sq_total[10], wacc, perp)
    # Present value of TV (discounted from Y10 back to today)
    tv_pv = tv_y10_raw / (1.0 + wacc) ** 10
    npv_with_tv_total = npv_sq_10y + tv_pv

    out["detailed_npv.wacc"] = wacc
    out["detailed_npv.perpetual_growth_rate"] = perp
    out["detailed_npv.npv_10y_excl_tv"] = npv_sq_10y
    out["detailed_npv.annual_npv_y1"] = annual_10y[0]
    out["detailed_npv.annual_npv_y10"] = annual_10y[9]
    out["detailed_npv.terminal_value_10y_raw"] = tv_y10_raw
    out["detailed_npv.npv_with_tv_10y_raw"] = npv_with_tv_total

    out["headline.npv_sq_10y"] = npv_sq_10y
    out["headline.npv_sq_5y"] = npv_sq_5y

    return out
