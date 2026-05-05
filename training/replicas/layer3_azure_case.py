"""
Layer 3 BA Replica — Azure Case Cash Flow + Retained Costs
===========================================================

Reproduces the Azure-side cash-flow view (Summary Financial Case rows 21-29)
plus the underlying retained-cost mechanics from the
``Retained Costs Estimation`` and ``Depreciation Schedule`` tabs of the
finalised business-case workbook.

Locked-in formulas (Customer A, verified $0.01 parity)
-------------------------------------------------------

Conventions:
    g       = ``client.expected_future_growth_rate`` (e.g. 0.02)
    eoy[t]  = end-of-year migration ramp at year ``t`` (Y0..Y10).
              Y0 is implicit ``0.0`` (no migration yet); Y1..Y10 come
              from ``consumption.migration_ramp_eoy``.
    ramp_lagged[t] = eoy[t-1] (= 0 for t=1; standard "1-year lag" the BA
              uses to keep migrating-year cores on the on-prem ledger).
    fully_migrated[t] = eoy[t] >= 1.0 (= "workload has fully landed in
              Azure by end of year t").

CAPEX (sustaining HW renewal during ramp-up)
    AZ Server / Storage / NW CAPEX[Y0]  = SQ baseline acquisition / depr_life × depr_life = full SQ baseline acq
                                          (Y0 is "current run rate"; we still book the
                                          full baseline acquisition because nothing has migrated yet)
    AZ Line CAPEX[t≥1]                  = baseline_y0 × hw_renewal_pct × (1 - eoy[t])
                                          (we only renew the % of HW that hasn't migrated yet)

OPEX (retained run-the-business spend)
    AZ HW Maintenance  (Server, Storage, NW)
        Y0           = full SQ Y0 baseline
        Yt (t≥1)     = baseline × (1 - eoy[t])         [NO growth, current-year ramp]

    AZ DC Lease / Power / Bandwidth (Proportional exit)
        Y0           = full SQ Y0 baseline
        Yt (t≥1)     = baseline × (1+g)^t × (1 - eoy[t-1])

    AZ Virtualization, ESU, Backup-SW, DR-SW (BYOL=No / not retained)
        Y0           = full SQ Y0 baseline
        Yt (t≥1)     = baseline × (1+g)^(t-1) × (1 - eoy[t-1])
        (zeroes once eoy[t-1] hits 1.0)

    AZ Backup / DR Storage   (only billed when option NOT included in Azure run rate)
        Same lagged formula as Virt (zeroes at full migration).
        Customer A has both options OFF on the SQ side, so Y0..Y10 = 0.

    AZ Windows + SQL OS Licenses   (continue billing on Azure cores)
        Y0           = full SQ Y0 baseline
        Yt (t≥1):
            multiplier = 1.0                          if eoy[t-1] >= 1.0   (fully landed)
                       = 1.0 - eoy[t-1]               otherwise            (still on-prem)
            cost       = baseline × (1+g)^(t-1) × multiplier

    AZ IT Admin (System Administrators)
        retained_admins[t]   = sq_admins[t] - ROUND(sq_admins[t] × reduction × ramp_lagged[t] × recapture)
        cost[t]              = retained_admins[t] × admin_compensation
        For Customer A this lands at a constant 2 admins ⇒ $393,174.42 every year.

Consumption (Azure run-rate)
    AZ Consumption[Y0]       = 0
    AZ Consumption[Yt≥1]     = (compute_y10 + storage_y10 + other_y10)
                                × avg(eoy[t-1], eoy[t]) × (1 + g)
        (the +g uplift is the BA's universal ~2 % cushion baked into
        ``Detailed FC!Q41 = SUM(Wk1..Wk3!E28) * (1 + Client Variables!D26)`` — we
        confirmed it cell-by-cell in the workbook).

Migration cost
    AZ Migration[t]          = (eoy[t] - eoy[t-1]) × total_vms_and_servers
                                × cost_per_vm
        (uses ``1-Client Variables!D41`` = ``total_vms_and_servers_combined``)

Microsoft funding (ACO + ECIF)
    Customer A = 0 in every year. Modeled as a pass-through from the
    consumption inputs (split per-year if the BA enters them per year).

Cash-flow scalars
    AZ Total CF[t]   = AZ CAPEX[t] + AZ OPEX[t] + AZ Consumption[t]
                       + AZ Migration[t] + AZ MS Funding[t]
    Savings[t]       = max(0, SQ Total[t] - AZ Total[t])
    CF Delta[t]      = AZ Total[t] - SQ Total[t]
    CF Rate[t]       = CF Delta[t] / SQ Total[t]   (0 when SQ Total[t] == 0)

NPV scalars
    NPV AZ 10y       = sum_{t=1..10} AZ Total[t] / (1+wacc)^t
    NPV AZ 5y        = sum_{t=1..5}  AZ Total[t] / (1+wacc)^t

NO IMPORTS FROM ``engine/`` — independent oracle.
"""

from __future__ import annotations

from .layer3_cash_flow import (
    compute_status_quo_cash_flow,
    discount_series,
)
from .layer3_inputs import InputsBenchmark, InputsClient, InputsConsumption
from .layer3_status_quo import (
    N_YEARS,
    _round_half_up,
    compute_baselines,
)


# ---------------------------------------------------------------------------
# Ramp helpers
# ---------------------------------------------------------------------------


def _ramp_y0_to_y10(consumption: InputsConsumption) -> tuple[float, ...]:
    """Return the EOY migration ramp indexed Y0..Y10.

    ``consumption.migration_ramp_eoy`` covers Y1..Y10; Y0 is implicitly 0.
    """
    ramp = (0.0,) + tuple(consumption.migration_ramp_eoy)
    if len(ramp) != N_YEARS:
        raise ValueError(
            f"Expected ramp length {N_YEARS} (Y0..Y10); got {len(ramp)}"
        )
    return ramp


def _prior(ramp: tuple[float, ...], t: int) -> float:
    """``eoy[t-1]`` with ``eoy[Y(-1)]`` clamped to 0 (matches BA Y1 boundary)."""
    return ramp[t - 1] if t >= 1 else 0.0


# ---------------------------------------------------------------------------
# Per-line AZ cost shapes
# ---------------------------------------------------------------------------


def _az_hw_maint(baseline_y0: float, ramp: tuple[float, ...]) -> tuple[float, ...]:
    """HW maintenance: NO growth, current-year ramp factor."""
    out = [baseline_y0]
    for t in range(1, N_YEARS):
        out.append(baseline_y0 * (1.0 - ramp[t]))
    return tuple(out)


def _az_dc_or_bandwidth(
    baseline_y0: float, g: float, ramp: tuple[float, ...]
) -> tuple[float, ...]:
    """DC Lease / DC Power / Bandwidth retained-cost decay.

    Mirrors the BA's chained formula in ``Retained Costs Estimation``
    rows 287/293/295::

        retained[t] = retained[t-1] × (1 + g) × (1 - eoy_ramp[t-1])

    Equivalently::

        retained[t] = baseline_y0 × (1 + g)^t × Π_{k=1..t-1} (1 - eoy_ramp[k])

    For Customer A's ramp pattern (e.g. ``[0.5, 1.0, 1.0, ...]``) the
    cumulative product collapses to a single factor (every term after the
    first ``1.0`` is zero), which is why the simpler formula
    ``(1 - eoy_ramp[t-1])`` happened to match. Customer B has an
    intermediate ramp ``[0.33, 0.66, 1.0, ...]`` so Y3 needs the full
    product ``(1-0.33) * (1-0.66)``.
    """
    out = [baseline_y0]
    for t in range(1, N_YEARS):
        decay = 1.0
        for k in range(1, t):
            decay *= 1.0 - ramp[k]
        out.append(baseline_y0 * (1.0 + g) ** t * decay)
    return tuple(out)


def _az_lagged_zero(
    baseline_y0: float, g: float, ramp: tuple[float, ...]
) -> tuple[float, ...]:
    """Virt / ESU / Backup-SW / DR-SW / Backup-Storage / DR-Storage:

    growth^(t-1) × (1 - eoy[t-1]) — zeroes at full migration.
    """
    out = [baseline_y0]
    for t in range(1, N_YEARS):
        out.append(baseline_y0 * (1.0 + g) ** (t - 1) * (1.0 - _prior(ramp, t)))
    return tuple(out)


def _az_continuing_license(
    baseline_y0: float, g: float, ramp: tuple[float, ...]
) -> tuple[float, ...]:
    """Win + SQL OS licenses: continue at full cost once fully migrated.

    multiplier = 1.0                if eoy[t-1] >= 1.0
               = 1.0 - eoy[t-1]     otherwise
    """
    out = [baseline_y0]
    for t in range(1, N_YEARS):
        prior = _prior(ramp, t)
        multiplier = 1.0 if prior >= 0.9999999999 else (1.0 - prior)
        out.append(baseline_y0 * (1.0 + g) ** (t - 1) * multiplier)
    return tuple(out)


def _az_capex_line(
    baseline_acq_y0: float, hw_renewal: float, ramp: tuple[float, ...]
) -> tuple[float, ...]:
    """CAPEX line: Y0 = full baseline acq; Yt = acq × hw_renewal × (1 - eoy[t])."""
    out = [baseline_acq_y0]
    for t in range(1, N_YEARS):
        out.append(baseline_acq_y0 * hw_renewal * (1.0 - ramp[t]))
    return tuple(out)


# ---------------------------------------------------------------------------
# IT Admin retained
# ---------------------------------------------------------------------------


def _az_it_admin_series(
    client: InputsClient,
    bm: InputsBenchmark,
    ramp: tuple[float, ...],
) -> tuple[float, ...]:
    """Retained sysadmin cost across Y0..Y10.

    sq_admins[t]    = ROUND(VMs × (1+g)^t / vms_per_admin)
    reduced[t]      = ROUND(sq_admins[t] × reduction × ramp_lagged[t] × recapture)
                      where ramp_lagged[t] = eoy[t-1] (= 0 for t=0,1).
                      Multi-year lag is captured by the formula's ramp_lagged
                      argument; reduced[t]=0 when ramp_lagged[t]=0.
    retained[t]     = max(sq_admins[t] - reduced[t], 0)
    cost[t]         = retained[t] × admin_compensation

    For Customer A the productivity option is "Yes" (D31) and
    sq_admins[t] follows the step function 2,2,2,3,3,3,3,3,3,3,3 across Y0..Y10
    with ramp_lagged[t] giving reduced = 0,0,0,1,1,1,1,1,1,1,1 ⇒ retained = 2 flat.
    """
    g = client.expected_future_growth_rate
    vms_per_admin = bm.vms_per_sysadmin
    admin_cost = bm.sysadmin_fully_loaded_cost_yr
    reduction = bm.productivity_reduction_after_migration
    recapture = bm.productivity_recapture_rate

    apply_productivity = client.incorporate_productivity

    out = []
    for t in range(N_YEARS):
        vms_t = client.nb_vms * (1.0 + g) ** t
        sq_admins = _round_half_up(vms_t / vms_per_admin)
        if apply_productivity:
            ramp_lagged = _prior(ramp, t)
            reduced = _round_half_up(sq_admins * reduction * ramp_lagged * recapture)
        else:
            reduced = 0
        retained = max(sq_admins - reduced, 0)
        out.append(retained * admin_cost)
    return tuple(out)


# ---------------------------------------------------------------------------
# Consumption + Migration + MS Funding (Y10 anchor model)
# ---------------------------------------------------------------------------


def _az_consumption_series(
    client: InputsClient,
    consumption: InputsConsumption,
    ramp: tuple[float, ...],
) -> tuple[float, ...]:
    """Azure consumption per year.

    Plan cells (Wk1!E28..N28 etc.) store ``Y10_anchor × avg(eoy[t-1], eoy[t])``.
    The Detailed FC then uplifts by ``(1 + Client Variables!D26)`` (= 1+g).
    """
    g = client.expected_future_growth_rate
    y10_anchor = (
        consumption.compute_consumption_y10
        + consumption.storage_consumption_y10
        + consumption.other_consumption_y10
    )
    out = [0.0]  # Y0
    for t in range(1, N_YEARS):
        avg_ramp = (_prior(ramp, t) + ramp[t]) / 2.0
        out.append(y10_anchor * avg_ramp * (1.0 + g))
    return tuple(out)


def _az_migration_series(
    client: InputsClient,
    consumption: InputsConsumption,
    ramp: tuple[float, ...],
) -> tuple[float, ...]:
    """NET migration cost per year, matching BA `Detailed Financial Case!Q46`:

        Q46 = SUM('2a'!E20, '2b'!E20, '2c'!E20) + Q47
            = (gross migration: total_VMs * Δramp * cost_per_VM)
            + (Microsoft funding: ACO + ECIF per year)

    Customer A funding is zero across all years → Q46 = gross only (unchanged).
    Customer B has ECIF -1.05M Y1–Y3 → Q46 = gross + ECIF (NET, smaller).
    """
    total_vms = client.total_vms_and_servers_combined
    cost_per = consumption.migration_cost_per_vm
    aco = consumption.aco_by_year
    ecif = consumption.ecif_by_year
    out = [0.0]
    for t in range(1, N_YEARS):
        delta_ramp = ramp[t] - _prior(ramp, t)
        gross = total_vms * delta_ramp * cost_per
        funding = aco[t - 1] + ecif[t - 1]
        out.append(gross + funding)
    return tuple(out)


def _az_ms_funding_series(consumption: InputsConsumption) -> tuple[float, ...]:
    """Microsoft funding (ACO + ECIF) per year, matching BA `Detailed Financial
    Case!Q47 = SUM('2a'!E23, '2b'!E23, '2c'!E23)` where '2a'!E23 = SUM(E21:E22)
    is the per-year sum of ACO (row 21) and ECIF (row 22).

    Step 15: previously hard-coded zeros (Customer A pattern). Customer B
    populates E22:N22 with per-year ECIF subsidies, which now flow through here.
    For Customer A all per-year cells are blank → 0.0 across Y0..Y10 (unchanged).
    """
    aco = consumption.aco_by_year
    ecif = consumption.ecif_by_year
    out = [0.0]  # Y0
    for t in range(1, N_YEARS):
        out.append(aco[t - 1] + ecif[t - 1])
    return tuple(out)


# ---------------------------------------------------------------------------
# OPEX + CAPEX assembly
# ---------------------------------------------------------------------------


def _az_capex_total(
    client: InputsClient, bm: InputsBenchmark, ramp: tuple[float, ...]
) -> tuple[float, ...]:
    """Sum AZ CAPEX across Server, Storage, NW lines.

    Per the Depreciation Schedule tab, the BA's "yearly acquisition" baseline
    equals ``acquisition / depr_life`` (= the per-year sustaining HW spend),
    NOT the full one-time acquisition cost. AZ CAPEX[Y0] mirrors that
    baseline (same as SQ CAPEX Y0); Y1+ scales by hw_renewal × (1 - eoy[t]).
    """
    base = compute_baselines(client, bm)
    hw_renewal = client.hw_renewal_during_migration_pct
    server = _az_capex_line(base.server_depreciation_y0, hw_renewal, ramp)
    storage = _az_capex_line(base.storage_depreciation_y0, hw_renewal, ramp)
    nw = _az_capex_line(base.nw_fitout_depreciation_y0, hw_renewal, ramp)
    return tuple(server[t] + storage[t] + nw[t] for t in range(N_YEARS))


def _az_opex_total(
    client: InputsClient,
    bm: InputsBenchmark,
    consumption: InputsConsumption,
    ramp: tuple[float, ...],
) -> tuple[float, ...]:
    """Sum every retained-cost line across Y0..Y10."""
    base = compute_baselines(client, bm)
    g = client.expected_future_growth_rate

    # HW maintenance — current-year ramp, no growth
    server_maint = _az_hw_maint(base.server_maintenance_y0, ramp)
    storage_maint = _az_hw_maint(base.storage_maintenance_y0, ramp)
    nw_maint = _az_hw_maint(base.network_maintenance_y0, ramp)

    # DC + bandwidth — full growth × (1 - eoy[t-1])
    dc_space = _az_dc_or_bandwidth(base.dc_lease_space_y0, g, ramp)
    dc_power = _az_dc_or_bandwidth(base.dc_power_y0, g, ramp)
    bandwidth = _az_dc_or_bandwidth(base.bandwidth_y0, g, ramp)

    # Backup / DR storage — only billed when not bundled into Azure run rate.
    # For Customer A both options are OFF, so storage baselines are 0 and
    # the lagged formula returns zeros. Mirror exactly.
    storage_backup = _az_lagged_zero(base.storage_backup_y0, g, ramp)
    storage_dr = _az_lagged_zero(base.storage_dr_y0, g, ramp)

    # Licenses
    virt = _az_lagged_zero(base.virtualization_licenses_y0, g, ramp)
    win_esu = _az_lagged_zero(base.windows_esu_y0, g, ramp)
    sql_esu = _az_lagged_zero(base.sql_esu_y0, g, ramp)
    backup_sw = _az_lagged_zero(base.backup_licenses_y0, g, ramp)
    dr_sw = _az_lagged_zero(base.dr_licenses_y0, g, ramp)

    # Win/SQL OS licenses — continue in Azure on migrated cores
    win = _az_continuing_license(base.windows_licenses_y0, g, ramp)
    sql = _az_continuing_license(base.sql_licenses_y0, g, ramp)

    # IT admin — retained sysadmin step
    it_admin = _az_it_admin_series(client, bm, ramp)

    out = []
    for t in range(N_YEARS):
        out.append(
            server_maint[t]
            + storage_maint[t]
            + nw_maint[t]
            + dc_space[t]
            + dc_power[t]
            + bandwidth[t]
            + storage_backup[t]
            + storage_dr[t]
            + virt[t]
            + win_esu[t]
            + sql_esu[t]
            + backup_sw[t]
            + dr_sw[t]
            + win[t]
            + sql[t]
            + it_admin[t]
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Top-level entry — Azure case cash flow + NPV scalars
# ---------------------------------------------------------------------------


def compute_azure_case_cash_flow(
    client: InputsClient,
    bm: InputsBenchmark,
    consumption: InputsConsumption,
) -> tuple[
    tuple[float, ...],  # az_capex
    tuple[float, ...],  # az_opex
    tuple[float, ...],  # az_consumption
    tuple[float, ...],  # az_migration
    tuple[float, ...],  # az_ms_funding
    tuple[float, ...],  # az_total
]:
    """Return per-year AZ-side cash-flow components."""
    ramp = _ramp_y0_to_y10(consumption)
    az_capex = _az_capex_total(client, bm, ramp)
    az_opex = _az_opex_total(client, bm, consumption, ramp)
    az_consumption = _az_consumption_series(client, consumption, ramp)
    az_migration = _az_migration_series(client, consumption, ramp)
    az_ms_funding = _az_ms_funding_series(consumption)
    az_total = tuple(
        az_capex[t]
        + az_opex[t]
        + az_consumption[t]
        + az_migration[t]
        + az_ms_funding[t]
        for t in range(N_YEARS)
    )
    return az_capex, az_opex, az_consumption, az_migration, az_ms_funding, az_total


def compute_azure_case_dict(
    client: InputsClient,
    bm: InputsBenchmark,
    consumption: InputsConsumption,
) -> dict[str, float]:
    """Return a flat dict whose keys match auditor labels for:

    * ``cash_flow.AZ CAPEX.Y0..Y10``
    * ``cash_flow.AZ OPEX.Y0..Y10``
    * ``cash_flow.AZ Consumption.Y0..Y10``
    * ``cash_flow.AZ Migration.Y0..Y10``
    * ``cash_flow.AZ MS Funding.Y0..Y10``
    * ``cash_flow.AZ Total CF.Y0..Y10``
    * ``cash_flow.Savings (SQ-AZ).Y0..Y10``
    * ``cash_flow.CF Delta (AZ-SQ).Y0..Y10``
    * ``cash_flow.CF Rate.Y0..Y10``
    * ``headline.npv_az_10y``, ``headline.npv_az_5y``
    """
    az_capex, az_opex, az_consumption, az_migration, az_ms_funding, az_total = (
        compute_azure_case_cash_flow(client, bm, consumption)
    )
    _, _, sq_total = compute_status_quo_cash_flow(client, bm)

    out: dict[str, float] = {}
    for t in range(N_YEARS):
        out[f"cash_flow.AZ CAPEX.Y{t}"] = az_capex[t]
        out[f"cash_flow.AZ OPEX.Y{t}"] = az_opex[t]
        out[f"cash_flow.AZ Consumption.Y{t}"] = az_consumption[t]
        out[f"cash_flow.AZ Migration.Y{t}"] = az_migration[t]
        out[f"cash_flow.AZ MS Funding.Y{t}"] = az_ms_funding[t]
        out[f"cash_flow.AZ Total CF.Y{t}"] = az_total[t]

        savings = max(0.0, sq_total[t] - az_total[t])
        delta = az_total[t] - sq_total[t]
        rate = delta / sq_total[t] if sq_total[t] else 0.0
        out[f"cash_flow.Savings (SQ-AZ).Y{t}"] = savings
        out[f"cash_flow.CF Delta (AZ-SQ).Y{t}"] = delta
        out[f"cash_flow.CF Rate.Y{t}"] = rate

    # NPV of AZ total cash flows
    wacc = bm.wacc
    annual_10y = discount_series(az_total, wacc, n_years=10)
    npv_az_10y = sum(annual_10y)
    npv_az_5y = sum(discount_series(az_total, wacc, n_years=5))
    out["headline.npv_az_10y"] = npv_az_10y
    out["headline.npv_az_5y"] = npv_az_5y

    return out
