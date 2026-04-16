"""
Azure Retail Prices API client with local disk cache.

Two modes:

1. Reference-SKU pricing (legacy, used by the benchmarks/UI pricing display):
   Fetches PAYG price for Standard_D4s_v5 and E10 LRS disk, converts to
   per-unit rates.  get_pricing() returns AzurePricing.

2. Per-VM SKU matching (per-VM rightsizing engine):
   Loads the static VM catalog from data/azure_vm_catalog.json (D/E/F/M
   series specs), fetches live Linux PAYG PAYG prices for all catalog SKUs
   in the target region, then match_sku() selects the least-cost SKU that
   satisfies target (vcpu, memory_gib) constraints.
   get_vm_catalog() returns list[VMSku] with live prices merged in.

Cache location: .cache/azure_prices/  (relative to cwd, TTL = 24 h)
Requires: stdlib only (urllib.request, json, pathlib, time)
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

import logging
_log = logging.getLogger(__name__)

_PRICES_API = "https://prices.azure.com/api/retail/prices"
_API_VERSION = "api-version=2023-01-01-preview"

# Reference VM: Standard_D4s_v5 — 4 vCPUs, Windows/Linux PAYG, Consumption
_REF_VM_SKU   = "D4s v5"          # matches skuName in API (without "Standard_" prefix)
_REF_VM_VCPUS = 4

# Reference managed disk: Standard SSD LRS E10 = 128 GiB
_REF_DISK_SKU  = "E10 LRS"        # skuName fragment for standard SSD
_REF_DISK_GiB  = 128

# Static VM catalog path (bundled with the package)
_CATALOG_PATH = Path(__file__).parent.parent / "data" / "azure_vm_catalog.json"

# Fallback benchmark rates (match BenchmarkConfig defaults)
_DEFAULT_VCPU_RATE = 0.048   # $/vCPU/hr  (Dv5 PAYG East US average)
_DEFAULT_GB_RATE   = 0.075   # $/GiB/month (Standard SSD LRS E-series approx, East US)

_CACHE_DIR     = Path(".cache") / "azure_prices"
_CACHE_TTL_SEC = 86_400   # 24 hours


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class AzurePricing:
    region: str
    price_per_vcpu_hour_usd: float   # PAYG rate, no discount applied
    price_per_gb_month_usd: float    # managed-disk rate
    vm_sku: str = _REF_VM_SKU
    disk_sku: str = _REF_DISK_SKU
    source: str = "benchmark"        # "api" | "benchmark" | "cache"
    fetched_at: float = field(default_factory=time.time)

    @property
    def price_per_vcpu_hour_display(self) -> str:
        return f"${self.price_per_vcpu_hour_usd:.4f}/vCPU/hr"

    @property
    def price_per_gb_month_display(self) -> str:
        return f"${self.price_per_gb_month_usd:.4f}/GB/mo"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_pricing(
    region: str,
    benchmark_vcpu_rate: float = _DEFAULT_VCPU_RATE,
    benchmark_gb_rate: float = _DEFAULT_GB_RATE,
    timeout_sec: float = 10.0,
) -> AzurePricing:
    """
    Fetch PAYG per-unit rates for *region*.

    Returns AzurePricing with source="api" (live), "cache" (disk hit),
    or "benchmark" (fallback when the API is unreachable or returns no data).
    """
    # ── Cache probe ───────────────────────────────────────────────────────
    cache_path = _cache_path(region)
    cached = _read_cache(cache_path)
    if cached is not None:
        return AzurePricing(
            region=region,
            price_per_vcpu_hour_usd=cached["vcpu_rate"],
            price_per_gb_month_usd=cached["gb_rate"],
            vm_sku=cached.get("vm_sku", _REF_VM_SKU),
            disk_sku=cached.get("disk_sku", _REF_DISK_SKU),
            source="cache",
            fetched_at=cached.get("fetched_at", 0.0),
        )

    # ── Live API fetch ────────────────────────────────────────────────────
    try:
        vcpu_rate, vm_sku = _fetch_vm_rate(region, timeout_sec)
        gb_rate, disk_sku = _fetch_disk_rate(region, timeout_sec)
    except Exception as exc:
        _log.debug(f"[azure_sku_matcher] API fetch failed ({exc!s}) — using benchmark defaults")
        return AzurePricing(
            region=region,
            price_per_vcpu_hour_usd=benchmark_vcpu_rate,
            price_per_gb_month_usd=benchmark_gb_rate,
            source="benchmark",
            fetched_at=0.0,
        )

    # ── Fallback if rates are missing ────────────────────────────────────
    if vcpu_rate <= 0:
        _log.debug(f"[azure_sku_matcher] VM SKU not found in '{region}' — using benchmark vCPU rate")
        vcpu_rate = benchmark_vcpu_rate
        vm_sku = "benchmark"
    if gb_rate <= 0:
        _log.debug(f"[azure_sku_matcher] Disk SKU not found in '{region}' — using benchmark GB rate")
        gb_rate = benchmark_gb_rate
        disk_sku = "benchmark"

    result = AzurePricing(
        region=region,
        price_per_vcpu_hour_usd=vcpu_rate,
        price_per_gb_month_usd=gb_rate,
        vm_sku=vm_sku,
        disk_sku=disk_sku,
        source="api",
        fetched_at=time.time(),
    )
    _write_cache(cache_path, result)
    _log.debug(
        f"[azure_sku_matcher] {region}: {result.price_per_vcpu_hour_display} "
        f"| {result.price_per_gb_month_display}  (source={result.source})"
    )
    return result


def benchmark_pricing(
    benchmark_vcpu_rate: float = _DEFAULT_VCPU_RATE,
    benchmark_gb_rate: float = _DEFAULT_GB_RATE,
) -> AzurePricing:
    """Return a pricing object from benchmark defaults — no API call."""
    return AzurePricing(
        region="benchmark",
        price_per_vcpu_hour_usd=benchmark_vcpu_rate,
        price_per_gb_month_usd=benchmark_gb_rate,
        vm_sku="benchmark",
        disk_sku="benchmark",
        source="benchmark",
        fetched_at=0.0,
    )


# ---------------------------------------------------------------------------
# Per-VM SKU matching — VMSku and catalog functions
# ---------------------------------------------------------------------------

@dataclass
class VMSku:
    """One Azure VM SKU with its spec and live PAYG price."""
    arm_sku_name: str       # e.g. "Standard_D4s_v5"
    family: str             # "D" | "E" | "F" | "M"
    vcpu: int
    memory_gib: int         # as documented by Azure (GiB = MiB ÷ 1024)
    price_per_hour_usd: float = 0.0   # Linux PAYG; 0.0 = price unavailable
    source: str = "catalog"           # "api" | "cache" | "catalog" (no live price)


def get_vm_catalog(
    region: str,
    timeout_sec: float = 15.0,
) -> list[VMSku]:
    """
    Return list[VMSku] for *region* with live Linux PAYG prices merged in.

    Spec data (vcpu, memory_gib, family) comes from the bundled
    data/azure_vm_catalog.json.  Prices are fetched from the Azure Retail
    Prices API in a single paginated call and cached per-region for 24 h.

    If the API is unreachable, VMSku.price_per_hour_usd stays 0.0 and
    VMSku.source == "catalog" (caller falls back to reference-SKU rate).
    """
    # Load static catalog specs
    specs = _load_catalog_specs()
    if not specs:
        return []

    # Probe price cache
    cache_path = _vm_catalog_cache_path(region)
    price_map = _read_vm_price_cache(cache_path)
    cache_source = "cache"

    if price_map is None:
        # Fetch live prices
        try:
            price_map = _fetch_all_vm_prices(region, timeout_sec)
            _write_vm_price_cache(cache_path, price_map)
            cache_source = "api"
        except Exception as exc:
            _log.debug(f"[azure_sku_matcher] VM catalog price fetch failed ({exc!s}) — using 0.0 fallback")
            price_map = {}
            cache_source = "catalog"

    skus: list[VMSku] = []
    for s in specs:
        arm = s["armSkuName"]
        price = price_map.get(arm, 0.0)
        skus.append(VMSku(
            arm_sku_name=arm,
            family=s["family"],
            vcpu=s["vcpu"],
            memory_gib=s["memory_gib"],
            price_per_hour_usd=price,
            source=cache_source if price > 0 else "catalog",
        ))

    priced = sum(1 for s in skus if s.price_per_hour_usd > 0)
    _log.debug(
        f"[azure_sku_matcher] VM catalog: {len(skus)} SKUs, {priced} priced "
        f"(region={region}, source={cache_source})"
    )
    return skus


def match_sku(
    target_vcpu: int,
    target_mem_gib: float,
    catalog: list[VMSku],
    preferred_family: str = "D",
    fallback_ref_price_per_hour: float = 0.0,
    secondary_tolerance: float = 0.20,
    min_vcpu: int = 8,
) -> VMSku:
    """
    Return the least-cost Azure SKU satisfying target_vcpu and target_mem_gib.

    Selection uses an asymmetric 3-pass cascade that mirrors the manual Xa2
    analysis methodology — avoiding the "snap-up on both dimensions" problem
    that occurs when a rightsized target falls between two Azure SKU tiers.

    Pass 1 — Relaxed secondary dimension (cheapest-first, workload-aware)
    -----------------------------------------------------------------------
    Classifies the VM as CPU-skewed or memory-skewed based on memory density:

      CPU-skewed  (mem_gib / vcpu < 5 GiB/vCPU — higher CPU, lower memory):
        Memory is the PRIMARY constraint (must be covered in full).
        CPU is SECONDARY and may be as low as target_vcpu × (1 - tolerance).
        → Avoids a CPU-tier snap-up by finding a smaller-vCPU SKU that still
          fully covers the memory target.  The chosen SKU may carry slightly
          more memory than needed (acceptable over-provision on the secondary).

      Memory-skewed  (mem_gib / vcpu ≥ 5 GiB/vCPU — higher memory, lower CPU):
        CPU is the PRIMARY constraint (must be covered in full).
        Memory is SECONDARY and may be as low as target_mem_gib × (1 - tolerance).
        → Avoids a memory-tier snap-up by finding a lower-memory SKU that still
          fully covers the CPU target.  The chosen SKU may carry slightly more
          vCPUs than needed (acceptable over-provision on the secondary).

    Pass 2 — Strict both-dimensions (original behaviour)
    -----------------------------------------------------------------------
      sku.vcpu >= target_vcpu AND sku.memory_gib >= target_mem_gib
      → Cheapest SKU in preferred_family that fully covers both dimensions.

    Final selection: cheapest result across Pass 1 and Pass 2.
    Pass 1 can only reduce or hold cost relative to Pass 2; it can never inflate.

    Pass 3 — Family fallback (unchanged)
    -----------------------------------------------------------------------
      If neither pass found a priced match in preferred_family, try D→E→M in
      order.  If still nothing, return a synthetic SKU with fallback rate.

    Parameters
    ----------
    target_vcpu : int            Rightsized vCPU target (with headroom already applied).
    target_mem_gib : float       Rightsized memory target in GiB (with headroom).
    catalog : list[VMSku]        Pre-fetched per-region VM SKU list with live prices.
    preferred_family : str       Azure family letter: "D" | "E" | "F" | "M".
    fallback_ref_price_per_hour  Used when no priced SKU is found.
    secondary_tolerance : float  Fraction (0–1) by which the secondary dimension
                                 may fall below its rightsized target in Pass 1.
                                 0.0 = strict (Pass 1 degenerates to Pass 2).
                                 Default 0.20 matches the headroom already baked
                                 into the target, so actual utilisation is still
                                 covered even with the relaxation applied.
    min_vcpu : int               Minimum vCPU floor for any selected SKU regardless
                                 of target_vcpu.  Default 8 (BA methodology).
                                 Set to 1 to disable.
    """
    # Apply minimum vCPU floor (BA: smallest Azure VM used is 8 vCPU)
    effective_target_vcpu = max(target_vcpu, min_vcpu)
    mem_density = target_mem_gib / max(effective_target_vcpu, 1)
    # CPU-skewed threshold: D-series is 4 GiB/vCPU; above 5 → memory-skewed
    _MEM_DENSITY_THRESHOLD = 5.0

    best_pass1: VMSku | None = None
    best_pass2: VMSku | None = None

    families_to_try = _family_fallback_order(preferred_family)

    for family in families_to_try:
        family_skus = [s for s in catalog if s.family == family and s.price_per_hour_usd > 0]

        # ── Pass 1: relaxed secondary dimension ──────────────────────────
        if secondary_tolerance > 0 and best_pass1 is None:
            if mem_density < _MEM_DENSITY_THRESHOLD:
                # CPU-skewed: memory is primary (must be covered); CPU is secondary
                relaxed_vcpu = effective_target_vcpu * (1.0 - secondary_tolerance)
                p1_candidates = [
                    s for s in family_skus
                    if s.memory_gib >= target_mem_gib
                    and s.vcpu >= relaxed_vcpu
                ]
            else:
                # Memory-skewed: CPU is primary (must be covered); memory is secondary
                relaxed_mem = target_mem_gib * (1.0 - secondary_tolerance)
                p1_candidates = [
                    s for s in family_skus
                    if s.vcpu >= effective_target_vcpu
                    and s.memory_gib >= relaxed_mem
                ]
            if p1_candidates:
                best_pass1 = min(p1_candidates, key=lambda s: s.price_per_hour_usd)

        # ── Pass 2: strict both dimensions ───────────────────────────────
        if best_pass2 is None:
            p2_candidates = [
                s for s in family_skus
                if s.vcpu >= effective_target_vcpu and s.memory_gib >= target_mem_gib
            ]
            if p2_candidates:
                best_pass2 = min(p2_candidates, key=lambda s: s.price_per_hour_usd)

        # Once we have at least one candidate in this family, stop trying further families
        if best_pass1 or best_pass2:
            break

    # Final selection: cheapest across pass 1 and pass 2
    candidates = [s for s in (best_pass1, best_pass2) if s is not None]
    if candidates:
        return min(candidates, key=lambda s: s.price_per_hour_usd)

    # No priced match anywhere — try any family ignoring price == 0
    unpriced = [
        s for s in catalog
        if s.vcpu >= effective_target_vcpu and s.memory_gib >= target_mem_gib
    ]
    if unpriced:
        best = min(unpriced, key=lambda s: (s.vcpu, s.memory_gib))
        best.price_per_hour_usd = fallback_ref_price_per_hour
        best.source = "fallback"
        return best

    # Absolute fallback: synthesise a D8s_v5 equivalent (respecting min_vcpu floor)
    return VMSku(
        arm_sku_name="Standard_D8s_v5",
        family="D",
        vcpu=max(effective_target_vcpu, 8),
        memory_gib=max(int(target_mem_gib), 32),
        price_per_hour_usd=fallback_ref_price_per_hour,
        source="fallback",
    )


def _family_fallback_order(preferred: str) -> list[str]:
    """Return families to try in order, starting with preferred."""
    all_families = ["D", "E", "F", "M"]
    order = [preferred] + [f for f in all_families if f != preferred]
    return order


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _api_get(url: str, timeout_sec: float) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode())


def _load_catalog_specs() -> list[dict]:
    """Load VM specs from the bundled JSON catalog."""
    try:
        data = json.loads(_CATALOG_PATH.read_text())
        return data.get("skus", [])
    except Exception as exc:
        _log.debug(f"[azure_sku_matcher] Failed to load VM catalog: {exc!s}")
        return []


def _fetch_all_vm_prices(region: str, timeout: float) -> dict[str, float]:
    """
    Fetch Linux PAYG prices for all catalog SKUs in *region*.
    Returns {armSkuName: price_per_hour_usd}.
    Uses pagination to retrieve all results.
    """
    specs = _load_catalog_specs()
    arm_names = {s["armSkuName"] for s in specs}

    # The API supports up to ~100 SKU names in an 'in' filter, but we use
    # repeated calls by family prefix to stay within URL length limits.
    # Filter: Linux PAYG Consumption, no Spot/Low Priority.
    price_map: dict[str, float] = {}

    for family_prefix in ["Standard_D", "Standard_E", "Standard_F", "Standard_M"]:
        url = (
            f"{_PRICES_API}?{_API_VERSION}"
            f"&$filter={urllib.parse.quote(_vm_price_filter(region, family_prefix))}"
        )
        while url:
            try:
                data = _api_get(url, timeout)
            except Exception as exc:
                _log.debug(f"[azure_sku_matcher] Price fetch error for {family_prefix}: {exc!s}")
                break
            for item in data.get("Items", []):
                arm = item.get("armSkuName", "")
                price = item.get("retailPrice", 0)
                # Only record if this SKU is in our catalog and not Spot/LowPri
                sku_name = item.get("skuName", "")
                if (
                    arm in arm_names
                    and "Spot" not in sku_name
                    and "Low Priority" not in sku_name
                    and isinstance(price, (int, float))
                    and price > 0
                    and arm not in price_map   # keep first (cheapest if duplicates)
                ):
                    price_map[arm] = round(float(price), 6)
            url = data.get("NextPageLink")  # type: ignore[assignment]

    _log.debug(f"[azure_sku_matcher] Fetched {len(price_map)} VM prices for {region}")
    return price_map


def _vm_price_filter(region: str, family_prefix: str) -> str:
    return (
        f"serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{region}' "
        f"and priceType eq 'Consumption' "
        f"and contains(productName, 'Linux') "
        f"and startswith(armSkuName, '{family_prefix}')"
    )


def _vm_catalog_cache_path(region: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9_-]", "_", region.lower())
    return _CACHE_DIR / f"vm_catalog_{safe}.json"


def _read_vm_price_cache(path: Path) -> dict[str, float] | None:
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > _CACHE_TTL_SEC:
        return None
    try:
        data = json.loads(path.read_text())
        # Reject empty or all-zero cache entries — they indicate a failed API fetch
        # that was mistakenly cached (e.g. empty response body or HTTP error).
        if not data or not any(v > 0 for v in data.values()):
            _log.debug(f"[azure_sku_matcher] Rejecting empty/zero VM price cache: {path}")
            return None
        return data
    except Exception:
        return None


def _write_vm_price_cache(path: Path, price_map: dict[str, float]) -> None:
    # Never cache an empty or all-zero price map — it would poison future reads.
    if not price_map or not any(v > 0 for v in price_map.values()):
        _log.debug(f"[azure_sku_matcher] Skipping cache write — no priced SKUs in map")
        return
    try:
        path.write_text(json.dumps(price_map))
    except Exception as exc:
        _log.debug(f"[azure_sku_matcher] Failed to write VM price cache: {exc!s}")


def _fetch_vm_rate(region: str, timeout: float) -> tuple[float, str]:
    """
    Fetch Linux PAYG hourly price for D4s v5 in *region*.
    Returns (price_per_vcpu_hour, sku_name_used).
    """
    # The API returns individual VM sizes; we want Linux (not Windows) PAYG.
    # skuName in the API is e.g. "D4s v5" (without "Standard_" prefix or
    # "Linux" suffix — that's in the productName).
    filt = (
        f"serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{region}' "
        f"and skuName eq '{_REF_VM_SKU}' "
        f"and priceType eq 'Consumption' "
        f"and contains(productName, 'Linux')"
    )
    url = f"{_PRICES_API}?{_API_VERSION}&$filter={urllib.parse.quote(filt)}"
    data = _api_get(url, timeout)
    items = data.get("Items", [])
    for item in items:
        retail = item.get("retailPrice", 0)
        sku = item.get("skuName", _REF_VM_SKU)
        if isinstance(retail, (int, float)) and retail > 0:
            return round(retail / _REF_VM_VCPUS, 6), sku
    return 0.0, _REF_VM_SKU


def _fetch_disk_rate(region: str, timeout: float) -> tuple[float, str]:
    """
    Fetch monthly price for Standard SSD E10 LRS (128 GiB) in *region*.
    Returns (price_per_gb_month, sku_name_used).
    """
    filt = (
        f"serviceName eq 'Storage' "
        f"and armRegionName eq '{region}' "
        f"and skuName eq 'E10 LRS' "
        f"and priceType eq 'Consumption'"
    )
    url = f"{_PRICES_API}?{_API_VERSION}&$filter={urllib.parse.quote(filt)}"
    data = _api_get(url, timeout)
    items = data.get("Items", [])
    for item in items:
        retail = item.get("retailPrice", 0)
        sku = item.get("skuName", _REF_DISK_SKU)
        if isinstance(retail, (int, float)) and retail > 0:
            return round(retail / _REF_DISK_GiB, 6), sku
    return 0.0, _REF_DISK_SKU


# ---------------------------------------------------------------------------
# Disk cache helpers
# ---------------------------------------------------------------------------

def _cache_path(region: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9_-]", "_", region.lower())
    return _CACHE_DIR / f"{safe}.json"


def _read_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > _CACHE_TTL_SEC:
        return None
    try:
        data = json.loads(path.read_text())
        # Reject entries with missing/zero rates — indicates a prior failed API call.
        if not data or data.get("vcpu_rate", 0) <= 0 or data.get("gb_rate", 0) <= 0:
            _log.debug(f"[azure_sku_matcher] Rejecting zero-rate reference cache: {path}")
            return None
        return data
    except Exception:
        return None


def _write_cache(path: Path, p: AzurePricing) -> None:
    try:
        path.write_text(json.dumps({
            "region":     p.region,
            "vcpu_rate":  p.price_per_vcpu_hour_usd,
            "gb_rate":    p.price_per_gb_month_usd,
            "vm_sku":     p.vm_sku,
            "disk_sku":   p.disk_sku,
            "fetched_at": p.fetched_at,
        }))
    except Exception:
        pass  # cache write failure is non-fatal



