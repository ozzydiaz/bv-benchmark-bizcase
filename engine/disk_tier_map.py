"""
Azure managed disk tier mapping.

Maps a provisioned disk size in GiB to the correct Azure managed disk tier
and its monthly price, allowing per-VM storage cost estimation.

Azure tiers are fixed steps ("E" for Standard SSD, "P" for Premium SSD).
A disk is always billed at the next tier that fits its provisioned capacity.
Example: a 100 GiB disk fits the E10/P10 tier (128 GiB) so it is billed
at the 128 GiB tier price, not at 100 GiB.

Prices sourced from Azure Retail Prices API (East US, LRS, April 2025).
These are hardcoded defaults; at runtime the caller may substitute
region-specific prices from azure_sku_matcher.

References:
  https://azure.microsoft.com/en-us/pricing/details/managed-disks/
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Tier tables
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiskTier:
    sku: str           # API skuName, e.g. "E10 LRS"
    capacity_gib: int  # provisioned size ceiling in GiB
    price_per_month_usd: float  # East US LRS list price (Monthly)


# Standard SSD (E-series) — LRS
STANDARD_SSD_TIERS: list[DiskTier] = [
    DiskTier("E1 LRS",   4,      0.30),
    DiskTier("E2 LRS",   8,      0.60),
    DiskTier("E3 LRS",   16,     1.20),
    DiskTier("E4 LRS",   32,     1.92),
    DiskTier("E6 LRS",   64,     3.84),
    DiskTier("E10 LRS",  128,    2.304),   # E10 price dip (MS promotional tier)
    DiskTier("E15 LRS",  256,    9.216),
    DiskTier("E20 LRS",  512,    18.43),
    DiskTier("E30 LRS",  1_024,  36.86),
    DiskTier("E40 LRS",  2_048,  73.73),
    DiskTier("E50 LRS",  4_096,  147.46),
    DiskTier("E60 LRS",  8_192,  286.72),
    DiskTier("E70 LRS",  16_384, 573.44),
    DiskTier("E80 LRS",  32_767, 1_146.88),
]

# Premium SSD (P-series) — LRS
PREMIUM_SSD_TIERS: list[DiskTier] = [
    DiskTier("P1 LRS",   4,      1.54),
    DiskTier("P2 LRS",   8,      3.07),
    DiskTier("P3 LRS",   16,     6.14),
    DiskTier("P4 LRS",   32,     5.28),
    DiskTier("P6 LRS",   64,     10.21),
    DiskTier("P10 LRS",  128,    19.71),
    DiskTier("P15 LRS",  256,    38.13),
    DiskTier("P20 LRS",  512,    73.73),
    DiskTier("P30 LRS",  1_024,  122.88),
    DiskTier("P40 LRS",  2_048,  245.76),
    DiskTier("P50 LRS",  4_096,  491.52),
    DiskTier("P60 LRS",  8_192,  983.04),
    DiskTier("P70 LRS",  16_384, 1_638.40),
    DiskTier("P80 LRS",  32_767, 3_276.80),
]

# Convenience lookup
TIER_TABLES: dict[str, list[DiskTier]] = {
    "standard_ssd": STANDARD_SSD_TIERS,
    "premium_ssd":  PREMIUM_SSD_TIERS,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assign_tier(size_gib: float, disk_type: str = "standard_ssd") -> DiskTier:
    """
    Return the cheapest tier that accommodates *size_gib*.

    Parameters
    ----------
    size_gib : float
        Provisioned disk size in GiB (may be fractional from MiB conversion).
    disk_type : str
        "standard_ssd" (default) or "premium_ssd".

    Returns
    -------
    DiskTier
        The matching tier, or the largest tier if size_gib exceeds all tiers.
    """
    tiers = TIER_TABLES.get(disk_type, STANDARD_SSD_TIERS)
    for tier in tiers:
        if size_gib <= tier.capacity_gib:
            return tier
    return tiers[-1]  # larger than largest tier — bill at max


def monthly_cost_usd(size_gib: float, disk_type: str = "standard_ssd") -> tuple[DiskTier, float]:
    """
    Return (tier, monthly_cost_usd) for a single disk of *size_gib*.
    """
    tier = assign_tier(size_gib, disk_type)
    return tier, tier.price_per_month_usd


# Premium SSD v2 — per-GiB capacity pricing (baseline provisioning, no IOPS/throughput).
# East US LRS list price as of Q1 2026.  Override via assign_cheapest() pv2_price param.
_PREMIUM_SSD_V2_PRICE_PER_GIB_MONTH: float = 0.17   # $/GiB/month


def assign_cheapest(
    size_gib: float,
    pv2_price_per_gib_month: float = _PREMIUM_SSD_V2_PRICE_PER_GIB_MONTH,
) -> tuple[str, float]:
    """
    Return (label, monthly_cost_usd) for the least-cost managed disk option
    covering *size_gib* GiB, comparing Premium SSD (P-series tiers) vs
    Premium SSD v2 (raw capacity × per-GiB rate).

    Premium SSD v2 is billed on provisioned capacity at a flat per-GiB rate
    (no fixed tier steps), so it favours odd/large sizes where P-tier rounds
    up significantly.

    Parameters
    ----------
    size_gib : float
        Provisioned disk size in GiB.
    pv2_price_per_gib_month : float
        Premium SSD v2 per-GiB/month rate.  Override with region-specific
        rate when available.

    Returns
    -------
    label : str
        Selected option: P-tier SKU name (e.g. "P10 LRS") or "Premium_SSDv2".
    monthly_cost_usd : float
        Monthly cost for that option.
    """
    p_tier, p_cost = monthly_cost_usd(size_gib, "premium_ssd")
    pv2_cost = max(size_gib, 1.0) * pv2_price_per_gib_month
    if pv2_cost < p_cost:
        return "Premium_SSDv2", round(pv2_cost, 6)
    return p_tier.sku, round(p_cost, 6)


def vm_annual_storage_cost_usd(
    disk_sizes_gib: list[float],
    pv2_price_per_gib_month: float = _PREMIUM_SSD_V2_PRICE_PER_GIB_MONTH,
) -> tuple[float, list[str]]:
    """
    Return (annual_cost_usd, [tier_labels]) for one VM's list of disk sizes.
    Each disk independently picks the cheaper of P-tier vs Premium SSD v2.
    disk_sizes_gib values are GiB (MiB ÷ 1024).
    """
    annual = 0.0
    labels: list[str] = []
    for size in disk_sizes_gib:
        label, monthly = assign_cheapest(size, pv2_price_per_gib_month)
        annual += monthly * 12
        labels.append(label)
    return round(annual, 4), labels


def fleet_annual_cost_usd(
    vm_disk_sizes_gb: dict[str, list[float]],
    disk_type: str = "standard_ssd",
) -> tuple[float, dict[str, int], float]:
    """
    Compute fleet-total annual managed disk cost using per-disk tier assignment.
    Legacy aggregate mode — used when vDisk tab is absent.

    Parameters
    ----------
    vm_disk_sizes_gb : dict[str, list[float]]
        Mapping of VM name → list of provisioned disk sizes (decimal GB, not GiB).
        From RVToolsInventory.vm_disk_sizes_gb (legacy field).
    disk_type : str
        "standard_ssd" or "premium_ssd".

    Returns
    -------
    annual_cost_usd : float
    tier_counts : dict[str, int]
    total_provisioned_gib : float
    """
    annual_cost = 0.0
    tier_counts: dict[str, int] = {}
    total_gib = 0.0

    for disks in vm_disk_sizes_gb.values():
        for size_gb in disks:
            tier, monthly = monthly_cost_usd(size_gb, disk_type)
            annual_cost += monthly * 12
            tier_counts[tier.sku] = tier_counts.get(tier.sku, 0) + 1
            total_gib += size_gb

    return annual_cost, tier_counts, total_gib
