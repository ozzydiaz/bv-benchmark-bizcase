"""
Azure Retail Prices API client with local disk cache.

Fetches PAYG pricing for a reference VM SKU and managed-disk storage in a
given Azure region, and converts to per-unit rates for the consumption engine.

Reference SKU: Standard_D4s_v5  (4 vCPUs, general-purpose — Dsv5 series)
  price_per_vcpu_hour = VM_hourly_price / 4

Managed-disk reference: "E10" Standard SSD LRS (128 GiB tier)
  price_per_gb_month   = disk_monthly_price / 128

If the API is unreachable (offline, rate-limited) or the region/SKU is not
found, the benchmark defaults are returned and the caller is informed via
the AzurePricing.source field.

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

_PRICES_API = "https://prices.azure.com/api/retail/prices"
_API_VERSION = "api-version=2023-01-01-preview"

# Reference VM: Standard_D4s_v5 — 4 vCPUs, Windows/Linux PAYG, Consumption
_REF_VM_SKU   = "D4s v5"          # matches skuName in API (without "Standard_" prefix)
_REF_VM_VCPUS = 4

# Reference managed disk: Standard SSD LRS E10 = 128 GiB
_REF_DISK_SKU  = "E10 LRS"        # skuName fragment for standard SSD
_REF_DISK_GiB  = 128

# Fallback benchmark rates (match BenchmarkConfig defaults)
_DEFAULT_VCPU_RATE = 0.048   # $/vCPU/hr  (Dv5 PAYG East US average)
_DEFAULT_GB_RATE   = 0.018   # $/GB/month (Standard SSD approximation)

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
        print(f"[azure_sku_matcher] API fetch failed ({exc!s}) — using benchmark defaults")
        return AzurePricing(
            region=region,
            price_per_vcpu_hour_usd=benchmark_vcpu_rate,
            price_per_gb_month_usd=benchmark_gb_rate,
            source="benchmark",
            fetched_at=0.0,
        )

    # ── Fallback if rates are missing ────────────────────────────────────
    if vcpu_rate <= 0:
        print(f"[azure_sku_matcher] VM SKU not found in '{region}' — using benchmark vCPU rate")
        vcpu_rate = benchmark_vcpu_rate
        vm_sku = "benchmark"
    if gb_rate <= 0:
        print(f"[azure_sku_matcher] Disk SKU not found in '{region}' — using benchmark GB rate")
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
    print(
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
# Internal helpers
# ---------------------------------------------------------------------------

def _api_get(url: str, timeout_sec: float) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode())


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
        return json.loads(path.read_text())
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



