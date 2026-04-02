"""
Azure region inference from RVtools metadata.

Reads region_map.yaml and applies a priority-ordered set of heuristics
against the region evidence collected by rvtools_parser.py:

  1. Country-code TLD in domain_names / vcenter_fqdns
  2. Datacenter name consensus — if ≥50% of hosts share a named datacenter
     that has a keyword match, that datacenter wins.  A datacenter label is a
     direct, administrator-assigned geographic name (e.g., "Phoenix") and is
     more reliable than a server-configured timezone (which enterprises
     frequently set to UTC globally regardless of physical location).
  3. GMT offset (vHost.GMT Offset) — used only when no consensus DC exists
  4. Datacenter name keyword (any match, no quorum required)
  5. Fallback: data/region_map.yaml → fallback_region (default "eastus")

The result is an Azure armRegionName string suitable for the Azure Retail
Prices API filter (e.g. "uksouth", "eastus", "centralindia").
"""

from __future__ import annotations

import re
import yaml
from pathlib import Path
from typing import TYPE_CHECKING

import logging
_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from engine.rvtools_parser import RVToolsInventory

_DEFAULT_MAP_PATH = Path(__file__).parent.parent / "data" / "region_map.yaml"

_region_map_cache: dict | None = None


def _load_map(path: Path | None = None) -> dict:
    global _region_map_cache
    if _region_map_cache is None:
        p = path or _DEFAULT_MAP_PATH
        with open(p) as f:
            _region_map_cache = yaml.safe_load(f)
    return _region_map_cache


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def guess(
    inv: "RVToolsInventory",
    map_path: Path | None = None,
) -> str:
    """
    Infer an Azure region from RVtools metadata.

    Returns an armRegionName string (e.g. "uksouth").
    Never raises; falls back to the configured fallback_region.
    """
    rm = _load_map(map_path)
    fallback = rm.get("fallback_region", "eastus")
    kw_map: list[tuple[str, str | None]] = [
        (k, v) for k, v in rm.get("datacenter_keyword_to_region", {}).items()
    ]

    # ── Step 1: Country-code TLD ─────────────────────────────────────────
    tld_map: dict[str, str] = rm.get("tld_to_region", {})
    all_fqdns = list(inv.domain_names) + list(inv.vcenter_fqdns)
    for fqdn in all_fqdns:
        region = _match_tld(fqdn.lower(), tld_map)
        if region:
            _log(f"Region {region!r} ← TLD match on '{fqdn}'")
            return region

    # ── Step 2: Datacenter consensus (≥50% of hosts in one named DC) ────
    # Administrator-assigned datacenter names are more geographically
    # reliable than server-configured timezones, which enterprises often
    # force to UTC globally regardless of physical location.  Require a
    # quorum (majority) to avoid a single mis-labelled host winning.
    dc_counts: dict[str, int] = getattr(inv, "datacenter_host_counts", {})
    total_hosts = sum(dc_counts.values()) if dc_counts else 0
    if total_hosts > 0:
        # Sort by count descending so the plurality DC is checked first
        for dc_name, count in sorted(dc_counts.items(), key=lambda x: -x[1]):
            if count / total_hosts >= 0.50:
                region = _match_keyword(dc_name.lower(), kw_map)
                if region:
                    pct = count / total_hosts
                    _log(
                        f"Region {region!r} ← datacenter consensus: "
                        f"'{dc_name}' has {count}/{total_hosts} hosts ({pct:.0%})"
                    )
                    return region

    # ── Step 3: GMT offset ───────────────────────────────────────────────
    # Fallback when no consensus datacenter name exists.  Note: UTC (offset 0)
    # is extremely common as a corporate server timezone policy even for
    # datacenters physically located in non-UTC timezones, so this signal
    # carries lower confidence than a named datacenter.
    gmt_map: dict[str, str] = rm.get("gmt_offset_to_region", {})
    for offset_str in inv.gmt_offsets:
        key = str(offset_str).strip()
        region = gmt_map.get(key)
        if region:
            _log(f"Region {region!r} ← GMT offset '{key}'")
            return region

    # ── Step 4: Datacenter keyword (any match, no quorum) ────────────────
    for dc_name in inv.datacenter_names:
        region = _match_keyword(dc_name.lower(), kw_map)
        if region:
            _log(f"Region {region!r} ← datacenter keyword match on '{dc_name}'")
            return region

    # ── Step 5: Fallback ─────────────────────────────────────────────────
    _log(f"No region signal found — using fallback '{fallback}'")
    return fallback


def _match_tld(fqdn: str, tld_map: dict[str, str]) -> str | None:
    """Return the region for the first matching TLD in the FQDN, or None."""
    # Try longest TLD match first (e.g. .co.uk before .uk)
    sorted_tlds = sorted(tld_map.keys(), key=len, reverse=True)
    for tld in sorted_tlds:
        if fqdn.endswith(tld):
            return tld_map[tld]
    return None


def _match_keyword(dc_lower: str, kw_map: list[tuple[str, str | None]]) -> str | None:
    """Return the region for the first keyword found in dc_lower, or None."""
    for keyword, region in kw_map:
        if region and keyword in dc_lower:
            return region
    return None


def _log(msg: str) -> None:
    _logger.debug(f"[region_guesser] {msg}")
