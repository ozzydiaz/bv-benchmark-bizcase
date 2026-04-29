"""
Azure Pricing Client for the Layer 2 BA Replica
================================================

Lightweight, ENGINE-INDEPENDENT Azure Retail Prices API client used by
``layer2_ba_replica.py`` for SKU least-cost matching and the 5-offer
pricing matrix.

Why a second client (vs reusing ``engine.azure_sku_matcher``)?
    * The replica is an INDEPENDENT ORACLE — no imports from ``engine/``.
    * The engine's client only fetches PAYG; we need RI-1y, RI-3y, SP-1y,
      SP-3y per L2.PRICING.001.
    * Cache lives at ``.cache/azure_prices_l2/`` to avoid touching the
      engine's cache schema or polluting its assumptions.

Source authority for offers + filters:
    * Azure Retail Prices API: https://prices.azure.com/api/retail/prices
    * Catalog data file: ``data/azure_vm_catalog.json`` (read-only data,
      bundled with the engine package; safe to read).

Cache convention:
    * VM SKUs: ``.cache/azure_prices_l2/<region>_vm.json``
    * Managed-disk SKUs: ``.cache/azure_prices_l2/<region>_disk.json``
    * TTL: 24 hours; cache files NEVER written empty.

Fallback policy: when API is unreachable AND no cache exists, callers
receive an empty SKU/disk catalog and must fall back to per-vCPU/per-GiB
benchmark rates surfaced in the replica's BA review packet.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
_CATALOG_PATH = REPO_ROOT / "data" / "azure_vm_catalog.json"
_CACHE_DIR = REPO_ROOT / ".cache" / "azure_prices_l2"
_CACHE_TTL_SEC = 86_400

_PRICES_API = "https://prices.azure.com/api/retail/prices"
_API_VERSION = "api-version=2023-01-01-preview"

# Azure managed-disk catalogs.
# - Premium SSD LRS (P-series): performance-optimized; the engine's previous
#   default but ~2x more expensive per GiB than Standard SSD.
# - Standard SSD LRS (E-series): cost-optimized; what the BA actually uses
#   for business-case workloads in Customer A's Xa2-fixed tab.
DISK_CATALOG_LRS_PREMIUM: tuple[tuple[str, int], ...] = (
    ("P1",  4),    ("P2",  8),    ("P3",  16),   ("P4",  32),
    ("P6",  64),   ("P10", 128),  ("P15", 256),  ("P20", 512),
    ("P30", 1024), ("P40", 2048), ("P50", 4096), ("P60", 8192),
    ("P70", 16384),("P80", 32767),
)
DISK_CATALOG_LRS_STANDARD: tuple[tuple[str, int], ...] = (
    ("E1",  4),    ("E2",  8),    ("E3",  16),   ("E4",  32),
    ("E6",  64),   ("E10", 128),  ("E15", 256),  ("E20", 512),
    ("E30", 1024), ("E40", 2048), ("E50", 4096), ("E60", 8192),
    ("E70", 16384),("E80", 32767),
)
# Default for the BA-canonical Customer A workflow: Standard SSD.
DISK_CATALOG_LRS = DISK_CATALOG_LRS_STANDARD

# Pricing offer codes the BA workflow surfaces (per L2.PRICING.001).
PRICING_OFFERS: tuple[str, ...] = ("payg", "ri1y", "ri3y", "sp1y", "sp3y")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PricedSku:
    arm_sku_name: str       # e.g. "Standard_D4s_v5"
    family: str             # "D" | "E" | "F" | "M"
    vcpu: int
    memory_gib: int
    processor: str          # "Intel" | "AMD" | "ARM" | "Unknown"
    pricing: dict           # {offer_code -> {"usd_hr", "available", "term_years"}}

    @property
    def payg_usd_hr(self) -> float:
        return float(self.pricing.get("payg", {}).get("usd_hr") or 0.0)

    def usd_yr(self, offer: str) -> float | None:
        entry = self.pricing.get(offer, {})
        if not entry.get("available"):
            return None
        return float(entry["usd_hr"]) * 8760.0


@dataclass(frozen=True)
class PricedDisk:
    sku_name: str           # e.g. "P30 LRS"
    tier: str               # "P30"
    gib: int
    usd_month: float

    @property
    def usd_yr(self) -> float:
        return self.usd_month * 12.0


# ---------------------------------------------------------------------------
# Catalog loader
# ---------------------------------------------------------------------------

def _load_static_catalog() -> list[dict]:
    """Return the bundled list of SKU specs (vcpu, memory_gib, family).

    NOTE: This is now a SEED catalog only — the live API is the source of
    truth for Phase 2b+. The static file covers ~50 v5 SKUs; the BA's
    Customer A spreadsheet uses v7 (e.g. Standard_D16als_v7) which is
    discovered live by ``_fetch_all_skus_with_specs`` below.
    """
    if not _CATALOG_PATH.exists():
        _log.debug("Static catalog not found at %s", _CATALOG_PATH)
        return []
    try:
        return json.loads(_CATALOG_PATH.read_text()).get("skus", [])
    except Exception as exc:
        _log.debug("Failed to load static catalog: %s", exc)
        return []


# Hard-coded vCPU/memory specs for ALL Azure D/E/F/M sizes documented by
# Microsoft as of Q2 2026. The Retail Prices API returns prices but NOT
# specs, so we map by armSkuName → (vcpu, memory_gib, family).
#
# Sources:
#   - Azure VM sizes documentation (Dasv5/Dav5, Dadsv5, Dalsv5, Dalsv7, Edsv5,
#     Easv5, Edv5, Esv5, Fsv2, FXmdsv2, Mv2, Mdsmedmem_v3, etc.)
#   - Static seed file data/azure_vm_catalog.json (v5 D/E/F/M)
#
# The mapping uses an algorithm: family letter + numeric vCPU + family-specific
# memory_per_vcpu ratio.
_FAMILY_MEM_PER_VCPU = {
    # General-purpose ("D")
    "D":     4,    # Dsv5/Dasv5/Dlsv5: 4 GiB per vCPU
    "DA":    4,    # AMD Dasv5
    "DAL":   2,    # Dalsv5/Dalsv7 (lite): 2 GiB per vCPU
    "DL":    2,    # Dlsv5: 2 GiB per vCPU
    "DPL":   2,    # ARM lite
    "DC":    4,    # Confidential
    # Memory-optimized ("E")
    "E":     8,    # Esv5/Easv5/Edsv5: 8 GiB per vCPU
    "EA":    8,    # AMD
    "EI":    8,    # Isolated
    "EB":    8,    # Burstable
    "EC":    8,    # Confidential
    # Compute-optimized ("F")
    "F":     2,    # Fsv2: 2 GiB per vCPU
    "FX":    8,    # FXv2: 8 GiB per vCPU
    # Memory-extreme ("M")
    "M":     30,   # Mv2: roughly 30 GiB per vCPU
}

# Ratio overrides for SKUs where the formula doesn't match Azure docs.
# Each entry is keyed by (family-prefix-incl-modifiers, vcpu) -> memory_gib.
# Only listed when the SKU's modifier letters change the GiB/vCPU ratio in
# ways the family-key heuristic cannot capture.
# Sources: Microsoft Learn published VM size series specs (verified 2026-04).
_SIZE_OVERRIDES: dict[tuple[str, int], int] = {
    # Standard_M*bs_v3 (memory-balanced, 12 GiB/vCPU baseline)
    ("M_bs_v3", 16): 192,
    ("M_bs_v3", 32): 384,
    ("M_bs_v3", 48): 576,
    ("M_bs_v3", 64): 1024,
    ("M_bs_v3", 96): 1832,
    ("M_bs_v3", 128): 2589,
    ("M_bs_v3", 176): 2794,
    # Standard_M*bds_v3 (memory-balanced + local disk; same memory profile)
    ("M_bds_v3", 16): 192,
    ("M_bds_v3", 32): 384,
    ("M_bds_v3", 48): 576,
    ("M_bds_v3", 64): 1024,
    ("M_bds_v3", 96): 1832,
    ("M_bds_v3", 128): 2589,
    ("M_bds_v3", 176): 2794,
    # Standard_M*s_v3 / M*ds_v3 (regular, 20 GiB/vCPU)
    ("M_s_v3",  12): 240,
    ("M_s_v3",  24): 480,
    ("M_ds_v3", 12): 240,
    ("M_ds_v3", 24): 480,
    # Standard_M*ms_v3 / M*mds_v3 (memory-extreme, ~30 GiB/vCPU) — default OK
    # Standard_M*s_v2 (legacy, varied)
    ("M_s_v2",  8):  219,
    ("M_s_v2",  16): 437,
    ("M_s_v2",  32): 875,
    # Standard_FX*ms_v2 (memory-rich compute, 21 GiB/vCPU; FX2ms_v2 = 16 GiB)
    ("FX_ms_v2",  2): 16,
    ("FX_ms_v2",  4): 84,
    ("FX_ms_v2",  8): 168,
    ("FX_ms_v2", 12): 252,
    ("FX_ms_v2", 16): 336,
    ("FX_ms_v2", 24): 504,
    ("FX_ms_v2", 32): 672,
    ("FX_ms_v2", 36): 756,
    ("FX_ms_v2", 48): 1008,
    ("FX_ms_v2", 64): 1344,
    ("FX_ms_v2", 96): 2016,
    # Standard_FX*mds_v2 (memory-rich + local disk; same memory profile)
    ("FX_mds_v2",  2): 16,
    ("FX_mds_v2",  4): 84,
    ("FX_mds_v2",  8): 168,
    ("FX_mds_v2", 12): 252,
    ("FX_mds_v2", 16): 336,
    ("FX_mds_v2", 24): 504,
    ("FX_mds_v2", 32): 672,
    ("FX_mds_v2", 36): 756,
    ("FX_mds_v2", 48): 1008,
    ("FX_mds_v2", 64): 1344,
    ("FX_mds_v2", 96): 2016,
}

# Regex to parse Azure SKU names. Two forms supported:
#   a) Standard_<family><base>[<mods>][_<tier>]_v<n>     PARENT SKU
#      e.g., 'Standard_D16als_v7', 'Standard_M96s_2_v3'
#      The optional `_<tier>_v<n>` ('_2_v3') denotes a memory-tier variant
#      within Microsoft's M-series v3 parent naming convention. It is NOT
#      a constrained-cores indicator. See Microsoft Learn:
#      https://learn.microsoft.com/azure/virtual-machines/sizes/memory-optimized/msv3-mdsv3-medium-memory-series
#   b) Standard_<family><base>-<active>[<mods>][_<tier>]_v<n>   CONSTRAINED
#      e.g., 'Standard_E32-16s_v5', 'Standard_M96-48bds_2_v3'
#      Per https://learn.microsoft.com/azure/virtual-machines/constrained-vcpu
_ARM_NAME_RE = re.compile(
    r"^Standard_"
    r"([A-Z]+)"            # 1. family prefix (D / DA / DAL / E / EA / F / FX / M ...)
    r"(\d+)"               # 2. base vCPU count
    r"(?:-(\d+))?"         # 3. OPTIONAL constrained-cores active count (DASH form)
    r"([a-z]*)"            # 4. modifiers (s/d/m/i/...)
    r"(?:_(\d+))?"         # 5. OPTIONAL memory-tier identifier (M-series v3)
    r"_v(\d+)$"            # 6. version
)


# Memory specs (GiB) for M-series v3 PARENT SKUs that use the `_<tier>_v3`
# naming convention. These are NOT constrained-cores — they are full-vCPU
# parents with a tier-specific memory size.
# Sources: Microsoft Learn — Msv3/Mdsv3 Medium/High/Very High Memory Series
# (verified 2026-04-28 against the live Azure Retail Prices API).
# Key: ("family_modifiers_tier_vN", base_vcpu) -> memory_gib
_M_TIER_MEMORY_GIB: dict[tuple[str, int], int] = {
    # Msv3 / Mdsv3 Medium Memory (~20 GiB/vCPU baseline; tier doubles memory)
    ("M_s_1_v3",   48):  974,   ("M_ds_1_v3",  48):  974,
    ("M_s_1_v3",   96):  974,   ("M_ds_1_v3",  96):  974,
    ("M_s_2_v3",   96): 1944,   ("M_ds_2_v3",  96): 1944,
    ("M_s_3_v3",  176): 2794,   ("M_ds_3_v3", 176): 2794,
    ("M_s_4_v3",  176): 3892,   ("M_ds_4_v3", 176): 3892,
    # Msv3 / Mdsv3 High Memory + Very High Memory tiers
    ("M_s_6_v3",  416): 5696,   ("M_ds_6_v3", 416): 5696,
    ("M_s_8_v3",  416): 7600,   ("M_ds_8_v3", 416): 7600,
    ("M_s_12_v3", 624):11400,   ("M_ds_12_v3",624):11400,
    ("M_s_12_v3", 832):11400,   ("M_ds_12_v3",832):11400,
    ("M_is_16_v3",832):15200,   ("M_ids_16_v3",832):15200,
    ("M_ixds_24_v3",896):24000,
    # MSv2 legacy tiers (M416s_8_v2, M416s_9_v2, M416s_10_v2)
    ("M_s_8_v2",  416): 7600,
    ("M_s_9_v2",  416): 9728,
    ("M_s_10_v2", 416):11400,
    # Mbdsv3 memory-balanced + disk (~16-20 GiB/vCPU)
    ("M_bds_1_v3",  64): 1024,
    ("M_bds_2_v3",  96): 1832,
    ("M_bds_3_v3", 128): 2589,
    ("M_bds_4_v3", 176): 2794,
}


def infer_sku_spec(arm_name: str) -> dict | None:
    """Infer (vcpu, memory_gib, family, processor) from ``arm_name``.

    Handles three SKU name shapes:
      * Plain parent (e.g., 'Standard_D16als_v7').
      * Tiered M-series v3 parent (e.g., 'Standard_M96s_2_v3').
      * Constrained-cores via dash (e.g., 'Standard_E32-16s_v5',
        'Standard_M96-48bds_2_v3'). For these:
          - ``vcpu`` is the active count (after the dash).
          - ``memory_gib`` is the parent's memory.

    Returns None when the name doesn't match Azure's convention.
    """
    m = _ARM_NAME_RE.match(arm_name)
    if not m:
        return None
    raw_family, vcpu_str, active_str, modifiers, tier_str, ver = m.groups()
    base_vcpu = int(vcpu_str)
    active_vcpu = int(active_str) if active_str else base_vcpu
    is_constrained = active_str is not None
    has_tier = tier_str is not None

    # Detect AMD ('a' in modifiers) and Lite ('l' in modifiers) and ARM ('p').
    is_amd = "a" in modifiers
    is_lite = "l" in modifiers
    is_arm = "p" in modifiers

    # Build the family key by combining the letter prefix + lite/AMD signal.
    # E.g. 'D' + lite -> 'DAL'/'DL' depending on AMD presence
    if raw_family == "D":
        if is_lite and is_amd:
            fam_key = "DAL"
        elif is_lite and is_arm:
            fam_key = "DPL"
        elif is_lite:
            fam_key = "DL"
        elif is_amd:
            fam_key = "DA"
        else:
            fam_key = "D"
    elif raw_family == "E":
        if is_amd:
            fam_key = "EA"
        else:
            fam_key = "E"
    elif raw_family in ("FX", "F", "M"):
        fam_key = raw_family
    else:
        # Unknown family prefix; fallback to letter only
        fam_key = raw_family[:1]

    mem_per_vcpu = _FAMILY_MEM_PER_VCPU.get(fam_key)
    if mem_per_vcpu is None:
        # If unknown family, fall back to 4 GiB/vCPU (general purpose default)
        mem_per_vcpu = 4

    # Default-derived memory from family ratio.
    memory_gib = base_vcpu * mem_per_vcpu

    # Tiered M-series v3 parents (e.g., M96s_2_v3) have explicit specs
    # in _M_TIER_MEMORY_GIB. Look them up first.
    if has_tier:
        tier_key = (f"{raw_family}_{modifiers}_{tier_str}_v{ver}", base_vcpu)
        tier_mem = _M_TIER_MEMORY_GIB.get(tier_key)
        if tier_mem is not None:
            memory_gib = tier_mem
    else:
        # Plain parent: try _SIZE_OVERRIDES table (FX/M non-tiered).
        override_key = (
            f"{raw_family}_{modifiers}_v{ver}", base_vcpu
        ) if modifiers else (f"{raw_family}_v{ver}", base_vcpu)
        override = _SIZE_OVERRIDES.get(override_key)
        if override is not None:
            memory_gib = override

    # Constrained-cores (dash form): inherit memory from parent. The parent's
    # memory was just resolved above (since is_constrained still uses base_vcpu
    # for memory derivation). Only the active vCPU count differs.
    # Nothing extra to do here — `active_vcpu` is already set correctly.

    # Family letter for downstream filters (D/E/F/M)
    family_letter = raw_family[:1] if raw_family else "?"

    processor = (
        "AMD" if is_amd
        else "ARM" if is_arm
        else "Intel"
    )

    return {
        "armSkuName": arm_name,
        "vcpu": active_vcpu,         # active count for constrained, base otherwise
        "base_vcpu": base_vcpu,      # parent's vCPU count (== vcpu when not constrained)
        "memory_gib": memory_gib,
        "family": family_letter,
        "family_subtype": fam_key,
        "processor": processor,
        "version": int(ver),
        "is_lite": is_lite,
        "is_constrained_cores": is_constrained,
        "memory_tier": int(tier_str) if has_tier else None,
    }


def _processor_from_arm_name(arm_name: str) -> str:
    """Best-effort processor inference from Azure SKU naming convention.

    Conventions: '...as_v5' = AMD; '...ps_v5' = ARM (Cobalt); else Intel.
    """
    if re.search(r"[a-z]as?_v\d+$", arm_name):
        if arm_name.endswith(("as_v5", "as_v6", "asv5", "asv6")):
            return "AMD"
    if "_pl" in arm_name.lower() or "_p" in arm_name.lower() and arm_name.endswith(("ps_v5", "ps_v6")):
        return "ARM"
    return "Intel"


# ---------------------------------------------------------------------------
# Azure Retail Prices API
# ---------------------------------------------------------------------------

def _api_get(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "bv-bench-l2-replica/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _vm_filter(region: str, family_prefix: str) -> str:
    """Single API filter clause for one Azure VM family in one region.

    We pull EVERY pricing tier (Consumption + Reservation) for the family;
    Linux-only / Windows-only is decided downstream by inspecting
    ``productName`` because the Azure API doesn't expose a Linux flag.
    """
    return (
        f"serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{region}' "
        f"and startswith(armSkuName, '{family_prefix}')"
    )


def _disk_filter(region: str, ssd_class: str = "Standard") -> str:
    """``ssd_class`` = 'Standard' (E-series, cheap) | 'Premium' (P-series, fast)."""
    product_name = (
        "Standard SSD Managed Disks" if ssd_class == "Standard"
        else "Premium SSD Managed Disks"
    )
    return (
        f"serviceName eq 'Storage' "
        f"and armRegionName eq '{region}' "
        f"and contains(productName, '{product_name}')"
    )


# ---------------------------------------------------------------------------
# VM-pricing fetcher
# ---------------------------------------------------------------------------

def _empty_pricing_envelope() -> dict:
    return {
        offer: {"usd_hr": 0.0, "available": False, "term_years": 0}
        for offer in PRICING_OFFERS
    }


def _classify_offer(item: dict) -> str | None:
    """Map an API item to one of our 5 offer codes (or None if irrelevant).

    The Azure Retail Prices API uses ``type`` (NOT ``priceType``):
      * 'Consumption'        + ``reservationTerm == None`` + no SP array  → PAYG
      * 'Reservation'        + ``reservationTerm == '1 Year'``            → RI-1y
      * 'Reservation'        + ``reservationTerm == '3 Years'``           → RI-3y
      * 'DevTestConsumption' → ignored (separate offer, not in our 5)

    Savings Plan entries are nested under the ``savingsPlan`` array of a
    Consumption record; they're handled separately by ``_classify_savings_plan``.
    """
    t = (item.get("type") or "").lower()
    if t == "consumption":
        return "payg"
    if t == "reservation":
        term = (item.get("reservationTerm") or "").lower()
        if "1 year" in term:
            return "ri1y"
        if "3 year" in term:
            return "ri3y"
    return None


def _classify_savings_plan(sp_entry: dict) -> str | None:
    term = (sp_entry.get("term") or "").lower()
    if "1 year" in term:
        return "sp1y"
    if "3 year" in term:
        return "sp3y"
    return None


def _fetch_vm_prices(region: str, timeout: float) -> dict[str, dict]:
    """Return ``{armSkuName: {offer: {usd_hr, available, term_years}}}``.

    Phase 2b: discovers ALL D/E/F/M-series SKUs available from the live API
    (not constrained by the bundled static catalog). The BA's spreadsheet
    selected v7 SKUs (e.g. Standard_D16als_v7) which the static seed file
    didn't include; live discovery fixes this.
    """
    result: dict[str, dict] = {}

    # Hours per term (used to convert Reservation total-upfront retailPrice
    # into a per-hour rate that's directly comparable to PAYG).
    HOURS_1Y = 8760.0
    HOURS_3Y = 8760.0 * 3.0

    for family_prefix in ["Standard_D", "Standard_E", "Standard_F", "Standard_M"]:
        url = (
            f"{_PRICES_API}?{_API_VERSION}"
            f"&$filter={urllib.parse.quote(_vm_filter(region, family_prefix))}"
        )
        page = 0
        while url:
            page += 1
            try:
                data = _api_get(url, timeout)
            except Exception as exc:
                _log.debug("Price fetch error %s page %d: %s", family_prefix, page, exc)
                break
            for item in data.get("Items", []):
                arm = item.get("armSkuName") or ""
                if not arm:
                    continue
                # Linux-only: productName must NOT contain 'Windows'.
                product_name = item.get("productName") or ""
                if "Windows" in product_name:
                    continue
                # Skip Spot / Low Priority variants.
                sku_name = item.get("skuName") or ""
                if "Spot" in sku_name or "Low Priority" in sku_name:
                    continue
                # Auto-discover this SKU on first sight.
                if arm not in result:
                    result[arm] = _empty_pricing_envelope()
                price = item.get("retailPrice", 0)
                if not isinstance(price, (int, float)) or price <= 0:
                    continue

                offer = _classify_offer(item)
                if offer == "payg":
                    cur = result[arm][offer]
                    if not cur["available"]:
                        result[arm][offer] = {
                            "usd_hr": round(float(price), 6),
                            "available": True,
                            "term_years": 0,
                        }
                elif offer == "ri1y":
                    cur = result[arm][offer]
                    if not cur["available"]:
                        result[arm][offer] = {
                            "usd_hr": round(float(price) / HOURS_1Y, 6),
                            "available": True,
                            "term_years": 1,
                            "upfront_total_usd": round(float(price), 2),
                        }
                elif offer == "ri3y":
                    cur = result[arm][offer]
                    if not cur["available"]:
                        result[arm][offer] = {
                            "usd_hr": round(float(price) / HOURS_3Y, 6),
                            "available": True,
                            "term_years": 3,
                            "upfront_total_usd": round(float(price), 2),
                        }

                # Savings Plans live in a sub-array of Consumption records.
                # The retailPrice inside each SP entry is already in $/hr.
                for sp in (item.get("savingsPlan") or []):
                    sp_offer = _classify_savings_plan(sp)
                    if not sp_offer:
                        continue
                    sp_price = sp.get("retailPrice", 0)
                    if not isinstance(sp_price, (int, float)) or sp_price <= 0:
                        continue
                    cur = result[arm][sp_offer]
                    if not cur["available"]:
                        result[arm][sp_offer] = {
                            "usd_hr": round(float(sp_price), 6),
                            "available": True,
                            "term_years": 1 if sp_offer == "sp1y" else 3,
                        }

            url = data.get("NextPageLink")  # type: ignore[assignment]

    n_priced = sum(1 for v in result.values() if v["payg"]["available"])
    _log.debug("Fetched VM prices for %s: %d SKUs with PAYG", region, n_priced)
    return result


# ---------------------------------------------------------------------------
# Disk pricing fetcher (Premium SSD LRS)
# ---------------------------------------------------------------------------

_DISK_TIER_RE = re.compile(r"\b[PE]\d{1,2}\b")


def _fetch_disk_prices(region: str, timeout: float, ssd_class: str = "Standard") -> dict[str, float]:
    """Return ``{tier_name: usd_per_month}`` for ``ssd_class`` ('Standard'|'Premium').

    Filters strictly:
      * ``type == 'Consumption'`` (skip Reservation upfront entries)
      * ``unitOfMeasure`` contains 'month' (capacity, not per-IOPS)
      * meter name does NOT contain 'Mount' / 'Burst' (operations fees)
      * sku name ends with ' LRS' (skip ZRS / GRS / RAGRS)
    """
    out: dict[str, float] = {}
    url = (
        f"{_PRICES_API}?{_API_VERSION}"
        f"&$filter={urllib.parse.quote(_disk_filter(region, ssd_class))}"
    )
    while url:
        try:
            data = _api_get(url, timeout)
        except Exception as exc:
            _log.debug("Disk fetch error: %s", exc)
            break
        for item in data.get("Items", []):
            sku = (item.get("skuName") or "").strip()
            meter = (item.get("meterName") or "").strip()
            t = (item.get("type") or "").lower()
            uom = (item.get("unitOfMeasure") or "").lower()

            if t != "consumption":
                continue
            if not sku.endswith(" LRS"):
                continue
            # Exclude ancillary fees that share P-tier names
            if any(token in meter for token in ("Mount", "Burst", "Read", "Write", "Operations")):
                continue
            if "month" not in uom:
                continue

            m = _DISK_TIER_RE.search(sku)
            if not m:
                continue
            tier = m.group(0)
            price = item.get("retailPrice", 0)
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            if tier not in out:
                out[tier] = round(float(price), 6)
        url = data.get("NextPageLink")  # type: ignore[assignment]
    _log.debug("Fetched disk prices for %s: %d tiers", region, len(out))
    return out


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _safe_region(region: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", region.lower())


def _cache_path(region: str, kind: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{_safe_region(region)}_{kind}.json"


def _read_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    if (time.time() - path.stat().st_mtime) > _CACHE_TTL_SEC:
        return None
    try:
        data = json.loads(path.read_text())
        if not data:
            return None
        return data
    except Exception:
        return None


def _write_cache(path: Path, data: dict) -> None:
    if not data:
        return
    try:
        path.write_text(json.dumps(data))
    except Exception as exc:
        _log.debug("Failed to write cache %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_priced_vm_catalog(
    region: str = "eastus2",
    timeout: float = 20.0,
    *,
    use_cache: bool = True,
) -> list[PricedSku]:
    """Return the live Azure VM SKU catalog with pricing for ``region``.

    Phase 2b: returns ALL discoverable D/E/F/M Linux SKUs (not just the
    bundled 50). Specs are inferred from each ``armSkuName`` via the
    Azure naming convention (see ``infer_sku_spec``). SKUs whose name
    cannot be parsed are silently dropped.

    Cache-first; falls through to API on miss/expired.
    """
    pricing_map: dict[str, dict] = {}
    if use_cache:
        cached = _read_cache(_cache_path(region, "vm"))
        if cached:
            pricing_map = cached
            _log.debug("VM pricing for %s: served from cache", region)

    if not pricing_map:
        pricing_map = _fetch_vm_prices(region, timeout)
        if pricing_map and any(v["payg"]["available"] for v in pricing_map.values()):
            _write_cache(_cache_path(region, "vm"), pricing_map)

    out: list[PricedSku] = []
    for arm, pricing in pricing_map.items():
        spec = infer_sku_spec(arm)
        if spec is None:
            continue
        out.append(PricedSku(
            arm_sku_name=arm,
            family=spec["family"],
            vcpu=spec["vcpu"],
            memory_gib=spec["memory_gib"],
            processor=spec["processor"],
            pricing=pricing,
        ))
    out.sort(key=lambda s: (s.family, s.vcpu, s.memory_gib))
    return out


def get_priced_disk_catalog(
    region: str = "eastus2",
    timeout: float = 20.0,
    *,
    use_cache: bool = True,
    ssd_class: str = "Standard",
) -> list[PricedDisk]:
    """Return Azure SSD LRS disk tiers with live monthly prices.

    ``ssd_class`` = 'Standard' (E-series, BA default) | 'Premium' (P-series).
    """
    cache_key = f"disk_{ssd_class.lower()}"
    pricing: dict[str, float] = {}
    if use_cache:
        cached = _read_cache(_cache_path(region, cache_key))
        if cached:
            pricing = cached
            _log.debug("Disk pricing for %s (%s): served from cache", region, ssd_class)

    if not pricing:
        pricing = _fetch_disk_prices(region, timeout, ssd_class)
        if pricing:
            _write_cache(_cache_path(region, cache_key), pricing)

    catalog = (
        DISK_CATALOG_LRS_STANDARD if ssd_class == "Standard"
        else DISK_CATALOG_LRS_PREMIUM
    )
    out: list[PricedDisk] = []
    for tier, gib in catalog:
        if tier in pricing:
            out.append(PricedDisk(
                sku_name=f"{tier} LRS",
                tier=tier,
                gib=gib,
                usd_month=pricing[tier],
            ))
    out.sort(key=lambda d: d.gib)
    return out


# ---------------------------------------------------------------------------
# Convenience: prefetch (idempotent) for tests
# ---------------------------------------------------------------------------

def prefetch(region: str = "eastus2", timeout: float = 30.0) -> tuple[int, int, int]:
    """Force network refresh of all caches; returns (vm_count, std_disk, prem_disk)."""
    vm = _fetch_vm_prices(region, timeout)
    if vm and any(v["payg"]["available"] for v in vm.values()):
        _write_cache(_cache_path(region, "vm"), vm)
    std = _fetch_disk_prices(region, timeout, "Standard")
    if std:
        _write_cache(_cache_path(region, "disk_standard"), std)
    prem = _fetch_disk_prices(region, timeout, "Premium")
    if prem:
        _write_cache(_cache_path(region, "disk_premium"), prem)
    n_vm = sum(1 for v in vm.values() if v["payg"]["available"])
    return n_vm, len(std), len(prem)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import argparse
    p = argparse.ArgumentParser(description="Azure pricing client (Layer 2 replica)")
    p.add_argument("--region", default="eastus2")
    p.add_argument("--prefetch", action="store_true", help="Force-refresh both caches")
    p.add_argument("--show-vm", action="store_true")
    p.add_argument("--show-disk", action="store_true")
    args = p.parse_args()

    if args.prefetch:
        n_vm, n_std, n_prem = prefetch(args.region)
        print(f"Prefetched: {n_vm} VM SKUs, {n_std} Standard-SSD tiers, "
              f"{n_prem} Premium-SSD tiers in {args.region}")

    if args.show_vm:
        cat = get_priced_vm_catalog(args.region)
        priced = [s for s in cat if s.payg_usd_hr > 0]
        print(f"\n{len(priced)} priced VM SKUs in {args.region}:")
        for s in priced[:20]:
            offers = ",".join(o for o in PRICING_OFFERS if s.pricing[o]["available"])
            print(f"  {s.arm_sku_name:<24} vcpu={s.vcpu:>3} mem={s.memory_gib:>4} GiB "
                  f"PAYG=${s.payg_usd_hr:.4f}/hr  offers={offers}")

    if args.show_disk:
        disks = get_priced_disk_catalog(args.region)
        print(f"\n{len(disks)} priced disk tiers in {args.region}:")
        for d in disks:
            print(f"  {d.sku_name:<10} {d.gib:>6} GiB  ${d.usd_month:>9.2f}/mo")
