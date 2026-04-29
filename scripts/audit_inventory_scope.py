#!/usr/bin/env python3
"""
audit_inventory_scope.py
────────────────────────
Parse an RVTools export and print a side-by-side inventory scope summary
comparing the Python engine's parsed values against the BA ground-truth
expected values (Customer A 2024-10-29 defaults).

Usage:
    python scripts/audit_inventory_scope.py <rvtools_file.xlsx> [--expected-file FILE]

If --expected-file is provided it should be a JSON file with keys matching
the metrics below.  Otherwise, the Customer A defaults are used.

Example output:
    ┌─────────────────────────────────────────────────────────────────┐
    │  Metric                          │  Parsed   │  Expected │  Δ  │
    ├─────────────────────────────────────────────────────────────────┤
    │  num_vms (all incl. templates)   │   2,831   │    2,831  │  ✓  │
    │  num_vms_poweredon               │   2,618   │    2,618  │  ✓  │
    │  templates_counted_in_tco        │      34   │       34  │  ✓  │
    │  total_vcpu (TCO)                │  15,330   │   15,330  │  ✓  │
    │  vcpu_per_core                   │    1.58   │     1.58  │  ✓  │
    │  num_hosts                       │     242   │      242  │  ✓  │
    │  total_partition_capacity_gb     │ 4,387,213 │ 4,390,000 │  ~  │
    │  total_disk_provisioned_gb       │ 3,240,000 │ 3,240,000 │  ✓  │
    │  lifecycle_env_tags_present      │   False   │    False  │  ✓  │
    └─────────────────────────────────────────────────────────────────┘
"""

import argparse
import json
import sys
from pathlib import Path


# Customer A 2024-10-29 BA ground-truth defaults
_REFERENCE_EXPECTED = {
    "num_vms":                    2831,
    "num_vms_poweredon":          None,   # unknown from BA doc
    "total_vcpu":                 15330,
    "total_vcpu_poweredon":       None,
    "num_hosts":                  242,
    "vcpu_per_core":              1.58,
    "total_partition_capacity_gb": 4_390_000,  # approximate from BA
    "total_disk_provisioned_gb":   None,
    "lifecycle_env_tags_present":  False,
}

_COL_WIDTH = 34


def _fmt(val) -> str:
    if val is None:
        return "—"
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, float):
        return f"{val:,.2f}"
    if isinstance(val, int):
        return f"{val:,}"
    return str(val)


def _delta(parsed, expected) -> str:
    if expected is None:
        return "?"
    if parsed is None:
        return "?"
    if isinstance(parsed, bool):
        return "✓" if parsed == expected else "✗"
    if isinstance(parsed, (int, float)):
        diff_pct = abs(parsed - expected) / max(abs(expected), 1) * 100
        if diff_pct < 0.5:
            return "✓"
        if diff_pct < 5.0:
            return f"~{diff_pct:+.1f}%"
        return f"✗ {diff_pct:+.1f}%"
    return "✓" if str(parsed) == str(expected) else "✗"


def _row(label: str, parsed, expected) -> str:
    label_s   = label.ljust(_COL_WIDTH)
    parsed_s  = _fmt(parsed).rjust(12)
    expected_s = _fmt(expected).rjust(12)
    delta_s   = _delta(parsed, expected).rjust(10)
    return f"  {label_s}  {parsed_s}   {expected_s}  {delta_s}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit RVTools inventory scope.")
    parser.add_argument("rvtools_file", help="Path to the RVTools .xlsx export.")
    parser.add_argument(
        "--expected-file",
        help="JSON file with expected metric values (overrides Customer A defaults).",
    )
    args = parser.parse_args()

    fpath = Path(args.rvtools_file)
    if not fpath.exists():
        print(f"File not found: {fpath}", file=sys.stderr)
        return 1

    expected = dict(_REFERENCE_EXPECTED)
    if args.expected_file:
        with open(args.expected_file) as f:
            expected.update(json.load(f))

    # Add the project root to sys.path so engine imports work
    root = Path(__file__).parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    print(f"\nParsing: {fpath.name}")
    try:
        from engine.rvtools_parser import parse
        inv = parse(fpath)
    except Exception as exc:
        print(f"Parse failed: {exc}", file=sys.stderr)
        return 2

    # ── Collect parsed metrics ─────────────────────────────────────────
    template_count = inv.num_vms - inv.num_vms_poweredon  # approximate

    rows = [
        ("num_vms (all incl. templates)",   inv.num_vms,                      expected.get("num_vms")),
        ("num_vms_poweredon",               inv.num_vms_poweredon,            expected.get("num_vms_poweredon")),
        ("templates approx (all − on)",     template_count,                   None),
        ("total_vcpu (TCO baseline)",        inv.total_vcpu,                   expected.get("total_vcpu")),
        ("total_vcpu_poweredon",             inv.total_vcpu_poweredon,         expected.get("total_vcpu_poweredon")),
        ("num_hosts",                        inv.num_hosts,                    expected.get("num_hosts")),
        ("vcpu_per_core",                    round(inv.vcpu_per_core, 2),      expected.get("vcpu_per_core")),
        ("total_partition_capacity_gb",      inv.total_partition_capacity_gb,  expected.get("total_partition_capacity_gb")),
        ("total_disk_provisioned_gb",        inv.total_disk_provisioned_gb,    expected.get("total_disk_provisioned_gb")),
        ("lifecycle_env_tags_present",       inv.lifecycle_env_tags_present,   expected.get("lifecycle_env_tags_present")),
    ]

    # ── Print table ───────────────────────────────────────────────────
    hdr_label   = "Metric".ljust(_COL_WIDTH)
    hdr_parsed  = "Parsed".rjust(12)
    hdr_exp     = "Expected".rjust(12)
    hdr_delta   = "Delta".rjust(10)
    divider     = "─" * (_COL_WIDTH + 40)

    print(f"\n  {hdr_label}  {hdr_parsed}   {hdr_exp}  {hdr_delta}")
    print(f"  {divider}")
    for label, parsed, exp in rows:
        print(_row(label, parsed, exp))
    print()

    # ── per-VM record summary ────────────────────────────────────────
    vm_records = inv.vm_records
    with_partition = sum(1 for v in vm_records if v.partition_capacity_gb > 0)
    with_disk      = sum(1 for v in vm_records if v.disk_sizes_gib)
    with_neither   = sum(1 for v in vm_records if v.partition_capacity_gb == 0 and not v.disk_sizes_gib)

    print(f"  VM records (powered-on, non-template):  {len(vm_records):,}")
    print(f"    with vPartition capacity:             {with_partition:,}  ({with_partition/max(len(vm_records),1)*100:.1f}%)")
    print(f"    with vDisk only:                      {with_disk - with_partition:,}")
    print(f"    with neither (vInfo fallback):        {with_neither:,}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
