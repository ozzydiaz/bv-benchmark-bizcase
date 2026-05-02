"""
Layer 3 BA Replica — Project NPV, ROI, Payback, and 5Y CF Payback Breakdown
============================================================================

Closes the remaining 21 cells of the Layer 3 oracle:

* 12 ``headline.*`` scalars (Summary Financial Case rows 6-12)
* 9  ``five_payback.*`` scalars (5Y CF with Payback sheet)

Locked-in formulas (Customer A, $0.01 parity)
---------------------------------------------

Terminal value (relative savings)
    SQ TV raw[N]        = SQ Total CF[YN] × (1 + perpetual_growth) / (WACC - perpetual_growth)
    AZ TV raw[N]        = AZ Total CF[YN] × (1 + perpetual_growth) / (WACC - perpetual_growth)
    SQ TV PV[N]         = SQ TV raw[N] / (1 + WACC)^N
    AZ TV PV[N]         = AZ TV raw[N] / (1 + WACC)^N
    headline.terminal_value_10y = SQ TV PV[10] - AZ TV PV[10]
    headline.terminal_value_5y  = SQ TV PV[5]  - AZ TV PV[5]

    The "5-year" variants use the Y5 run-rate (not Y10) — the BA effectively
    asks "if we stopped after Y5, what would the perpetual savings be?".

Project NPV
    headline.project_npv_excl_tv_N = NPV_SQ[N] - NPV_AZ[N]
    headline.project_npv_with_tv_N = (NPV_SQ[N] - NPV_AZ[N]) + headline.terminal_value_N

Year-10 / Year-5 savings shortcuts
    headline.y10_savings_10y_cf  = AZ Total CF[Y10] - SQ Total CF[Y10]    (= cash_flow.CF Delta Y10)
    headline.y10_savings_5y_cf   = AZ Total CF[Y5]  - SQ Total CF[Y5]
    headline.y10_savings_rate_10y= cash_flow.CF Rate.Y10
    headline.y10_savings_rate_5y = cash_flow.CF Rate.Y5

5Y CF with Payback breakdown (undiscounted ``H`` column)
    For t in {Y1..Y5}:
        infra_savings[t]   = (SQ_CAPEX[t]+SQ_OPEX[t]-SQ_admin[t])
                            - (AZ_CAPEX[t]+AZ_OPEX[t]-AZ_admin[t])
        admin_savings[t]   = SQ_admin[t] - AZ_admin[t]
        azure_run[t]       = -AZ_Consumption[t]
        migration[t]       = -AZ_Migration[t]
        total_benefits[t]  = infra_savings[t] + admin_savings[t]
        total_costs[t]     = azure_run[t] + migration[t]

    five_payback.infra_cost_reduction_npv  = SUM_{t=1..5} infra_savings[t]
    five_payback.infra_admin_reduction_npv = SUM_{t=1..5} admin_savings[t]
    five_payback.total_benefits_npv        = SUM_{t=1..5} total_benefits[t]
    five_payback.incremental_azure_npv     = SUM_{t=1..5} azure_run[t]
    five_payback.migration_npv             = SUM_{t=1..5} migration[t]
    five_payback.total_costs_npv           = SUM_{t=1..5} total_costs[t]

    The BA labels these "NPV" but column H is the *undiscounted* 5Y total.
    The discounted column (I) is computed separately for ROI/Payback only.

ROI (5Y CF, displayed)
    investment_npv_5y    = NPV_5y(-AZ_Migration[t])           (negative = cost)
    net_benefits_npv_5y  = NPV_SQ[5] - NPV_AZ[5]              (= project_npv_excl_tv_5y)
    five_payback.net_benefits_npv = net_benefits_npv_5y
    five_payback.roi_5y_cf        = -net_benefits_npv_5y / investment_npv_5y
                                    (with investment_npv_5y < 0; ROI is negative when net is negative)
    headline.roi_5y_cf            = five_payback.roi_5y_cf

Payback (years; only for crossing within Y1..Y5)
    threshold = -investment_npv_5y                          (= |migration NPV|, positive)
    cumul[Yt] = SUM_{k=1..t} disc(benefits + run_costs)[k]
              = SUM_{k=1..t} disc(SQ_total - AZ_total + AZ_Migration)[k]
    If cumul ever crosses ``threshold`` between Y_m and Y_{m+1}:
        payback = m + (threshold - cumul[Y_m]) / (cumul[Y_{m+1}] - cumul[Y_m])
    Else:
        payback = 0                                          (BA "More than 5 years" → SUM = 0)
    five_payback.payback_years = headline.payback_years.

NO IMPORTS FROM ``engine/`` — independent oracle.
"""

from __future__ import annotations

from .layer3_azure_case import compute_azure_case_cash_flow, _az_it_admin_series, _ramp_y0_to_y10
from .layer3_cash_flow import compute_status_quo_cash_flow, discount_series
from .layer3_inputs import InputsBenchmark, InputsClient, InputsConsumption
from .layer3_status_quo import N_YEARS, compute_it_admin_series


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _terminal_value_pv(cf_terminal: float, wacc: float, perp: float, periods: int) -> float:
    """Gordon growth TV discounted ``periods`` years to today."""
    if wacc <= perp:
        raise ValueError(f"WACC ({wacc}) must exceed perpetual growth ({perp})")
    raw = cf_terminal * (1.0 + perp) / (wacc - perp)
    return raw / (1.0 + wacc) ** periods


# ---------------------------------------------------------------------------
# Top-level dict
# ---------------------------------------------------------------------------


def compute_project_npv_dict(
    client: InputsClient,
    bm: InputsBenchmark,
    consumption: InputsConsumption,
) -> dict[str, float]:
    """Return a flat dict whose keys match auditor labels for:

    * ``headline.terminal_value_10y/5y``
    * ``headline.project_npv_with_tv_10y/5y``
    * ``headline.project_npv_excl_tv_10y/5y``
    * ``headline.roi_5y_cf``
    * ``headline.payback_years``
    * ``headline.y10_savings_10y_cf/5y_cf``
    * ``headline.y10_savings_rate_10y/5y``
    * ``five_payback.infra_cost_reduction_npv``
    * ``five_payback.infra_admin_reduction_npv``
    * ``five_payback.total_benefits_npv``
    * ``five_payback.incremental_azure_npv``
    * ``five_payback.migration_npv``
    * ``five_payback.total_costs_npv``
    * ``five_payback.net_benefits_npv``
    * ``five_payback.roi_5y_cf``
    * ``five_payback.payback_years``
    """
    sq_capex, sq_opex, sq_total = compute_status_quo_cash_flow(client, bm)
    az_capex, az_opex, az_consumption, az_migration, _, az_total = (
        compute_azure_case_cash_flow(client, bm, consumption)
    )

    sq_admin = compute_it_admin_series(client, bm)
    ramp = _ramp_y0_to_y10(consumption)
    az_admin = _az_it_admin_series(client, bm, ramp)

    wacc = bm.wacc
    perp = bm.perpetual_growth_rate

    # --- 10y / 5y NPV totals (already exposed elsewhere; re-computed here for self-containment) ---
    npv_sq_10y = sum(discount_series(sq_total, wacc, n_years=10))
    npv_sq_5y = sum(discount_series(sq_total, wacc, n_years=5))
    npv_az_10y = sum(discount_series(az_total, wacc, n_years=10))
    npv_az_5y = sum(discount_series(az_total, wacc, n_years=5))

    # --- Terminal Value (relative SQ-AZ savings, present value at "today") ---
    tv_sq_10y = _terminal_value_pv(sq_total[10], wacc, perp, periods=10)
    tv_az_10y = _terminal_value_pv(az_total[10], wacc, perp, periods=10)
    tv_sq_5y = _terminal_value_pv(sq_total[5], wacc, perp, periods=5)
    tv_az_5y = _terminal_value_pv(az_total[5], wacc, perp, periods=5)

    terminal_value_10y = tv_sq_10y - tv_az_10y
    terminal_value_5y = tv_sq_5y - tv_az_5y

    project_npv_excl_tv_10y = npv_sq_10y - npv_az_10y
    project_npv_excl_tv_5y = npv_sq_5y - npv_az_5y
    project_npv_with_tv_10y = project_npv_excl_tv_10y + terminal_value_10y
    project_npv_with_tv_5y = project_npv_excl_tv_5y + terminal_value_5y

    # --- Y10/Y5 cash-flow shortcuts ---
    y10_savings_10y_cf = az_total[10] - sq_total[10]
    y10_savings_5y_cf = az_total[5] - sq_total[5]
    y10_savings_rate_10y = (
        y10_savings_10y_cf / sq_total[10] if sq_total[10] else 0.0
    )
    y10_savings_rate_5y = (
        y10_savings_5y_cf / sq_total[5] if sq_total[5] else 0.0
    )

    # --- 5Y CF with Payback breakdown (undiscounted Y1..Y5 sums) ---
    infra_y1_y5 = [
        (sq_capex[t] + sq_opex[t] - sq_admin[t])
        - (az_capex[t] + az_opex[t] - az_admin[t])
        for t in range(1, 6)
    ]
    admin_y1_y5 = [sq_admin[t] - az_admin[t] for t in range(1, 6)]
    azure_run_y1_y5 = [-az_consumption[t] for t in range(1, 6)]
    migration_y1_y5 = [-az_migration[t] for t in range(1, 6)]
    benefits_y1_y5 = [
        infra_y1_y5[i] + admin_y1_y5[i] for i in range(5)
    ]
    costs_y1_y5 = [
        azure_run_y1_y5[i] + migration_y1_y5[i] for i in range(5)
    ]

    infra_npv = sum(infra_y1_y5)
    admin_npv = sum(admin_y1_y5)
    total_benefits_npv = sum(benefits_y1_y5)
    incremental_azure_npv = sum(azure_run_y1_y5)
    migration_npv = sum(migration_y1_y5)
    total_costs_npv = sum(costs_y1_y5)

    # --- ROI 5Y CF (using DISCOUNTED Y1..Y5 sums) ---
    investment_disc_y1_y5 = [
        migration_y1_y5[i] / (1.0 + wacc) ** (i + 1) for i in range(5)
    ]
    investment_npv_5y_disc = sum(investment_disc_y1_y5)  # negative

    net_benefits_npv_5y_disc = npv_sq_5y - npv_az_5y  # = project_npv_excl_tv_5y

    if investment_npv_5y_disc != 0.0:
        roi_5y_cf = -net_benefits_npv_5y_disc / investment_npv_5y_disc
    else:
        roi_5y_cf = 0.0

    # --- Payback years (discounted cumulative crosses |investment| within Y1..Y5) ---
    benefits_disc_y1_y5 = [
        benefits_y1_y5[i] / (1.0 + wacc) ** (i + 1) for i in range(5)
    ]
    azure_run_disc_y1_y5 = [
        azure_run_y1_y5[i] / (1.0 + wacc) ** (i + 1) for i in range(5)
    ]
    benefit_plus_run_disc = [
        benefits_disc_y1_y5[i] + azure_run_disc_y1_y5[i] for i in range(5)
    ]
    threshold = -investment_npv_5y_disc  # |investment|, positive
    cumul = [0.0]
    for x in benefit_plus_run_disc:
        cumul.append(cumul[-1] + x)
    # cumul[0] = 0 (start), cumul[1] = end-of-Y1, ..., cumul[5] = end-of-Y5
    payback_years = 0.0
    for m in range(1, 6):  # check crossings between Y_m and Y_{m+1}
        if m + 1 > 5:
            break
        prev_c = cumul[m]
        next_c = cumul[m + 1]
        if prev_c < threshold and next_c >= threshold:
            payback_years = m + (threshold - prev_c) / (next_c - prev_c)
            break

    out: dict[str, float] = {
        "headline.terminal_value_10y": terminal_value_10y,
        "headline.terminal_value_5y": terminal_value_5y,
        "headline.project_npv_with_tv_10y": project_npv_with_tv_10y,
        "headline.project_npv_with_tv_5y": project_npv_with_tv_5y,
        "headline.project_npv_excl_tv_10y": project_npv_excl_tv_10y,
        "headline.project_npv_excl_tv_5y": project_npv_excl_tv_5y,
        "headline.roi_5y_cf": roi_5y_cf,
        "headline.payback_years": payback_years,
        "headline.y10_savings_10y_cf": y10_savings_10y_cf,
        "headline.y10_savings_5y_cf": y10_savings_5y_cf,
        "headline.y10_savings_rate_10y": y10_savings_rate_10y,
        "headline.y10_savings_rate_5y": y10_savings_rate_5y,
        "five_payback.infra_cost_reduction_npv": infra_npv,
        "five_payback.infra_admin_reduction_npv": admin_npv,
        "five_payback.total_benefits_npv": total_benefits_npv,
        "five_payback.incremental_azure_npv": incremental_azure_npv,
        "five_payback.migration_npv": migration_npv,
        "five_payback.total_costs_npv": total_costs_npv,
        "five_payback.net_benefits_npv": net_benefits_npv_5y_disc,
        "five_payback.roi_5y_cf": roi_5y_cf,
        "five_payback.payback_years": payback_years,
    }
    return out
