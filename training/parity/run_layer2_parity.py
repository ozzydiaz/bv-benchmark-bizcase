"""
Layer 2 Parity Harness
======================

Runs the Layer 2 BA replica with the recorded customer strategy and
diffs every aggregate against the BA's expected values in
``training/baselines/<sample>/ba_expected.yaml`` (``layer2:`` block).

Phase-2 scope (per L2 rule book):
    * Right-sizing math only (vCPU, memory, storage).
    * No SKU least-cost match / pricing API yet (Phase 2b).
    * No L2.RIGHTSIZE.CPU.RETRY iterative-bump loop (Phase 2b).

Tolerances for Phase 2 are widened intentionally where the BA's
manual workflow does post-rightsizing micro-adjustments that the
replica cannot reproduce until Phase 2b. They will be tightened
once the Azure Retail Price API integration lands.

Usage::

    python training/parity/run_layer2_parity.py \\
        --input "$BV_PARITY_INPUT" \\
        --baseline training/baselines/customer_a_2024_10/ba_expected.yaml \\
        --report training/baselines/customer_a_2024_10/parity_report_l2.md
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from training.replicas.layer2_ba_replica import replicate_layer2  # noqa: E402

_log = logging.getLogger("layer2_parity")


# ---------------------------------------------------------------------------
# Field map: BA expected key → replica fyi_aggregates key
# ---------------------------------------------------------------------------
@dataclass
class FieldMap:
    expected_key: str            # key in baseline['layer2'][<key>]
    replica_field: str           # key in result.fyi_aggregates
    description: str

LAYER2_FIELDS = [
    FieldMap("azure_vcpu_total",
             "azure_vcpu_total_matched_sku_shape",
             "\u03a3_VM matched_SKU.vcpu (BA-canonical post-XA2-pick)"),
    FieldMap("azure_memory_gib",
             "azure_memory_gib_total_matched_sku_shape",
             "\u03a3_VM matched_SKU.memory_gib (BA-canonical post-XA2-pick)"),
    FieldMap("azure_storage_gib",
             "azure_storage_gib_vpartition_intrinsic",
             "Total Azure storage GiB (vPartition intrinsic, decimal-GB convention)"),
    # Phase 2c — BA Xa2-fixed authoritative pricing
    FieldMap("azure_compute_payg_usd_yr",
             "azure_compute_usd_yr.payg",
             "Annual Azure compute PAYG (per-VM tier-based, BA Xa2 authoritative)"),
    FieldMap("azure_compute_ri3y_usd_yr",
             "azure_compute_usd_yr.ri3y",
             "Annual Azure compute 3yr RI (per-VM tier-based)"),
    FieldMap("azure_storage_usd_yr",
             "azure_storage_payg_usd_yr",
             "Annual Azure storage PAYG (per-VM tier-based, BA Xa2 authoritative)"),
]


def _delta_pct(actual: float | None, expected: float | None) -> float | None:
    if actual is None or expected is None:
        return None
    if expected == 0:
        return float("inf") if actual != 0 else 0.0
    return (actual - expected) / expected * 100.0


def _classify(delta_pct: float | None, tolerance_pct: float) -> str:
    if delta_pct is None:
        return "MISSING"
    a = abs(delta_pct)
    if a <= tolerance_pct:
        return "PASS"
    if a <= tolerance_pct * 3:
        return "WARN"
    return "FAIL"


def _strategy_kwargs(baseline: dict) -> dict:
    """Pull the customer's recorded strategy from the baseline YAML."""
    cs = baseline.get("layer2", {}).get("customer_strategy", {})
    return {
        "strategy": cs.get("utilisation_strategy", "like_for_like"),
        "enforce_8vcpu_min_for_windows_server": cs.get(
            "enforce_8vcpu_min_for_windows_server", False
        ),
        "cpu_reduction_pct": cs.get("cpu_reduction_pct", 0.0) or 0.0,
        "mem_reduction_pct": cs.get("mem_reduction_pct", 0.0) or 0.0,
        "mem_buffer_pct": cs.get("mem_buffer_pct", 0.0) or 0.0,
        "storage_reduction_pct": cs.get("storage_reduction_pct", 0.0) or 0.0,
        "storage_buffer_pct": cs.get("storage_buffer_pct", 0.0) or 0.0,
    }


def _write_report(
    out_path: Path,
    rows: list[dict],
    *,
    input_file: str,
    baseline_file: str,
    strategy_kwargs: dict,
) -> None:
    summary = {k: sum(1 for r in rows if r["status"] == k) for k in ("PASS", "WARN", "FAIL", "MISSING")}
    lines = [
        "# Layer 2 Parity Report",
        "",
        f"- **Input file:** `{input_file}`",
        f"- **BA baseline:** `{baseline_file}`",
        f"- **Strategy:** `{strategy_kwargs['strategy']}`",
        f"- **enforce_8vcpu_min_for_windows_server:** `{strategy_kwargs['enforce_8vcpu_min_for_windows_server']}`",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|--------|-------|",
    ]
    for k in ("PASS", "WARN", "FAIL", "MISSING"):
        lines.append(f"| {k} | {summary[k]} |")
    lines += [
        "",
        "## Field-by-field comparison",
        "",
        "| Status | Field | BA expected | Replica | Δ% | Tolerance | Rule |",
        "|--------|-------|-------------|---------|-----|-----------|------|",
    ]
    for r in rows:
        delta = "—" if r["delta_pct"] is None else f"{r['delta_pct']:+.2f}%"
        lines.append(
            f"| {r['status']} | `{r['field']}` | "
            f"{r['expected']:,} | {r['replica']:,.2f} | {delta} | "
            f"±{r['tolerance_pct']:.1f}% | {r.get('rule','')} |"
        )
    lines += [
        "",
        "## Phase 2 deferred items",
        "",
        "- **`L2.MATCH.001`** — Azure Retail Price API SKU least-cost match.",
        "- **`L2.RIGHTSIZE.CPU.RETRY`** — iterative ±1 vCPU/memory bump when no SKU satisfies the rightsized triple.",
        "- **`L2.PRICING.001` / `L2.PRICING.002`** — five-offer price matrix (PAYG, RI-1y, RI-3y, SP-1y, SP-3y).",
        "- **`L2.STORAGE_PRICE.001`** — managed-disk LRS pricing.",
        "- **`L2.FAMILY_PIN.001`** — family/processor pin filter.",
        "",
        "These land in Phase 2b once Azure Retail Price API integration is in place.",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layer 2 parity harness")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--default-tolerance-pct", type=float, default=2.0,
        help="Default tolerance for fields without an explicit one in baseline.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    baseline = yaml.safe_load(args.baseline.read_text())
    layer2_baseline = baseline.get("layer2", {})
    sk = _strategy_kwargs(baseline)

    _log.info("Running Layer 2 replica with strategy=%s, win_floor=%s",
              sk["strategy"], sk["enforce_8vcpu_min_for_windows_server"])
    result = replicate_layer2(args.input, **sk)
    fyi = result.fyi_aggregates

    rows: list[dict] = []
    overall_fail = False

    for fm in LAYER2_FIELDS:
        spec = layer2_baseline.get(fm.expected_key)
        if not spec:
            continue
        expected = spec["expected"]
        tol = spec.get("tolerance_pct", args.default_tolerance_pct)
        # Replica field can be dotted (e.g. 'azure_compute_ref_rate_usd_yr.pre_match_vcpu')
        actual: object = fyi
        for key in fm.replica_field.split("."):
            if isinstance(actual, dict):
                actual = actual.get(key)
            else:
                actual = None
                break
        if not isinstance(actual, (int, float)):
            actual = None
        delta = _delta_pct(actual, expected)
        status = _classify(delta, tol)
        if status == "FAIL":
            overall_fail = True
        rows.append({
            "status": status,
            "field": fm.expected_key,
            "description": fm.description,
            "expected": expected,
            "replica": actual if actual is not None else 0.0,
            "delta_pct": delta,
            "tolerance_pct": tol,
            "rule": spec.get("rule", ""),
        })

    _write_report(
        args.report, rows,
        input_file=str(args.input),
        baseline_file=str(args.baseline),
        strategy_kwargs=sk,
    )

    print()
    print(f"{'Status':<8} {'Field':<28} {'Expected':>14} {'Replica':>14} {'Δ%':>8} {'Tol':>6}")
    print("-" * 88)
    for r in rows:
        d = "—" if r["delta_pct"] is None else f"{r['delta_pct']:>+7.2f}%"
        print(
            f"{r['status']:<8} {r['field']:<28} {r['expected']:>14,} "
            f"{r['replica']:>14,.2f} {d} {r['tolerance_pct']:>5.1f}%"
        )
    print()
    print(f"Strategy: {sk['strategy']}, win_floor={sk['enforce_8vcpu_min_for_windows_server']}")
    print(f"Report written to: {args.report}")
    return 1 if overall_fail else 0


if __name__ == "__main__":
    sys.exit(main())
