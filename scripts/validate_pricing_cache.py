#!/usr/bin/env python3
"""
validate_pricing_cache.py
─────────────────────────
Read all Azure pricing cache files under .cache/azure_prices/ and report
coverage metrics for each.  Optionally purge invalid entries.

Usage:
    python scripts/validate_pricing_cache.py [--purge] [--cache-dir PATH]

Outputs per-file:
  - region / cache type
  - entry count
  - priced count (price > 0)
  - zero-price entries (list, up to first 20)
  - min / max price
  - validity verdict: OK | EMPTY | ALL_ZERO | MOSTLY_ZERO

With --purge, deletes cache files that are EMPTY or ALL_ZERO so they are
refreshed on the next app run.
"""

import argparse
import json
import os
import sys
from pathlib import Path


# ─── helpers ──────────────────────────────────────────────────────────────────

def _describe_vm_catalog(data: dict, path: Path) -> dict:
    """Analyse a vm_catalog_*.json file: {sku: {price_per_hour_usd, vcpu, ...}}"""
    if not data:
        return {"verdict": "EMPTY", "count": 0, "priced": 0}

    count   = len(data)
    priced  = sum(1 for v in data.values() if isinstance(v, dict) and v.get("price_per_hour_usd", 0) > 0)
    zeros   = [k for k, v in data.items() if isinstance(v, dict) and v.get("price_per_hour_usd", 0) == 0]
    prices  = [v["price_per_hour_usd"] for v in data.values() if isinstance(v, dict) and v.get("price_per_hour_usd", 0) > 0]

    if count == 0:
        verdict = "EMPTY"
    elif priced == 0:
        verdict = "ALL_ZERO"
    elif priced < count * 0.5:
        verdict = "MOSTLY_ZERO"
    else:
        verdict = "OK"

    return {
        "verdict":       verdict,
        "count":         count,
        "priced":        priced,
        "zero_count":    len(zeros),
        "zero_examples": zeros[:20],
        "min_price":     min(prices) if prices else 0.0,
        "max_price":     max(prices) if prices else 0.0,
    }


def _describe_ref_pricing(data: dict, path: Path) -> dict:
    """Analyse a <region>.json file: {vcpu_rate, gb_rate, region, ...}"""
    vcpu_rate = data.get("price_per_vcpu_hour_usd", data.get("vcpu_rate", 0))
    gb_rate   = data.get("price_per_gb_month_usd",  data.get("gb_rate", 0))

    if not data:
        verdict = "EMPTY"
    elif vcpu_rate == 0 and gb_rate == 0:
        verdict = "ALL_ZERO"
    elif vcpu_rate == 0 or gb_rate == 0:
        verdict = "PARTIAL_ZERO"
    else:
        verdict = "OK"

    return {
        "verdict":   verdict,
        "vcpu_rate": vcpu_rate,
        "gb_rate":   gb_rate,
        "region":    data.get("region", "?"),
    }


def _should_purge(verdict: str) -> bool:
    return verdict in ("EMPTY", "ALL_ZERO")


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Azure pricing cache files.")
    parser.add_argument("--purge",     action="store_true", help="Delete invalid cache files.")
    parser.add_argument("--cache-dir", default=".cache/azure_prices",
                        help="Cache directory (default: .cache/azure_prices)")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    if not cache_dir.exists():
        print(f"Cache directory not found: {cache_dir}")
        return 0

    files = sorted(cache_dir.glob("*.json"))
    if not files:
        print(f"No cache files in {cache_dir}")
        return 0

    any_invalid = False
    for fpath in files:
        try:
            raw = fpath.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            print(f"  ✗  {fpath.name}  — unreadable: {exc}")
            any_invalid = True
            if args.purge:
                fpath.unlink()
                print(f"     [PURGED]")
            continue

        name = fpath.name
        is_catalog = name.startswith("vm_catalog_")

        if is_catalog:
            info = _describe_vm_catalog(data, fpath)
            verdict = info["verdict"]
            print(
                f"  {'✓' if verdict == 'OK' else '✗'}  {name}"
                f"  [{verdict}]"
                f"  entries={info['count']}  priced={info['priced']}"
                f"  zeros={info['zero_count']}"
                + (f"  price=[{info['min_price']:.4f}–{info['max_price']:.4f}]" if info["priced"] else "")
            )
            if info["zero_examples"]:
                preview = ", ".join(info["zero_examples"][:5])
                if len(info["zero_examples"]) > 5:
                    preview += f" … (+{len(info['zero_examples'])-5} more)"
                print(f"     zero-price SKUs: {preview}")
        else:
            info = _describe_ref_pricing(data, fpath)
            verdict = info["verdict"]
            print(
                f"  {'✓' if verdict == 'OK' else '✗'}  {name}"
                f"  [{verdict}]"
                f"  region={info['region']}"
                f"  vcpu_rate=${info['vcpu_rate']:.6f}/hr"
                f"  gb_rate=${info['gb_rate']:.6f}/mo"
            )

        if _should_purge(verdict):
            any_invalid = True
            if args.purge:
                fpath.unlink()
                print(f"     [PURGED]")
            else:
                print(f"     ⚠️  Run with --purge to delete this file.")

    if not any_invalid:
        print("\nAll cache files are valid.")
        return 0
    elif args.purge:
        print("\nInvalid cache files purged.  They will be refreshed on the next app run.")
        return 0
    else:
        print("\nRun with --purge to delete invalid files.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
