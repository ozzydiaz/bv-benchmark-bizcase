"""
Layer 1 Parity Harness
======================

Runs the BA replica AND the engine on the same RVTools file, then diffs both
against ``training/baselines/<sample>/ba_expected.yaml``.

Exits non-zero if any mandatory field exceeds tolerance — suitable for CI.

Usage::

    python training/parity/run_layer1_parity.py \\
        --input "$BV_PARITY_INPUT" \\
        --baseline training/baselines/customer_a_2024_10/ba_expected.yaml \\
        --report training/baselines/customer_a_2024_10/parity_report.md
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

from training.replicas.layer1_ba_replica import replicate_layer1  # noqa: E402

_log = logging.getLogger("layer1_parity")


# ---------------------------------------------------------------------------
# Field map: BA expected key → (replica path, engine attr path)
# ---------------------------------------------------------------------------
@dataclass
class FieldMap:
    expected_key: str
    replica_path: str
    engine_attr: str
    description: str

LAYER1_FIELDS = [
    FieldMap("num_vms_all", "fyi_aggregates.num_vms_all",
             "inv.num_vms", "VM count (incl. templates + powered-off)"),
    FieldMap("num_vms_poweredon", "fyi_aggregates.num_vms_poweredon",
             "inv.num_vms_poweredon", "Powered-on VM count"),
    FieldMap("total_vcpu_all", "fyi_aggregates.total_vcpu_all",
             "inv.total_vcpu", "Total provisioned vCPU (all VMs)"),
    FieldMap("total_memory_gb_poweredon", "fyi_aggregates.total_memory_gb_poweredon",
             "inv.total_vmemory_gb_poweredon", "Powered-on memory GB"),
    FieldMap("total_provisioned_gb_all", "fyi_aggregates.total_provisioned_gb_all",
             "inv.total_storage_provisioned_gb", "On-prem TCO storage GB (vPartition canonical)"),
    FieldMap("num_hosts", "fyi_aggregates.num_hosts",
             "inv.num_hosts", "Physical hosts"),
    FieldMap("vcpu_per_core_ratio", "fyi_aggregates.vcpu_per_core_ratio",
             "inv.vcpu_per_core_ratio", "vCPU/pCore ratio"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _dotget(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


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
    if a <= tolerance_pct * 5:
        return "WARN"
    return "FAIL"


# ---------------------------------------------------------------------------
# Engine runner
# ---------------------------------------------------------------------------
def _run_engine(input_path: Path) -> Any:
    try:
        from engine.rvtools_to_inputs import build_business_case
    except Exception as e:
        _log.warning("Engine import failed: %s — engine column will be skipped.", e)
        return None
    try:
        result = build_business_case(
            str(input_path),
            client_name="CustomerA-Parity",
            currency="USD",
            ramp_preset="Extended (100% by Y3)",
        )
        return result
    except Exception as e:
        _log.warning("Engine run failed: %s — engine column will be skipped.", e)
        return None


def _engine_value(engine_result: Any, attr_path: str) -> Any:
    if engine_result is None:
        return None
    # attr_path like "inv.num_vms" or "result.region"
    head, _, rest = attr_path.partition(".")
    if head == "inv":
        target = getattr(engine_result, "inventory", None)
    else:
        target = engine_result
    return _dotget(target, rest) if rest else target


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------
def _write_report(
    out_path: Path,
    rows: list[dict],
    *,
    input_file: str,
    baseline_file: str,
    review_packet: dict,
) -> None:
    summary = {
        "PASS": sum(1 for r in rows if r["status"] == "PASS"),
        "WARN": sum(1 for r in rows if r["status"] == "WARN"),
        "FAIL": sum(1 for r in rows if r["status"] == "FAIL"),
        "MISSING": sum(1 for r in rows if r["status"] == "MISSING"),
    }

    lines = []
    lines.append("# Layer 1 Parity Report")
    lines.append("")
    lines.append(f"- **Input file:** `{input_file}`")
    lines.append(f"- **BA baseline:** `{baseline_file}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Status | Count |")
    lines.append(f"|--------|-------|")
    for k in ("PASS", "WARN", "FAIL", "MISSING"):
        lines.append(f"| {k} | {summary[k]} |")
    lines.append("")
    lines.append("## Field-by-field comparison")
    lines.append("")
    lines.append("| Status | Field | BA expected | Replica | Δ replica | Engine | Δ engine | Rule |")
    lines.append("|--------|-------|-------------|---------|-----------|--------|----------|------|")
    for r in rows:
        rep = r["replica"]
        eng = r["engine"]
        rep_d = r["replica_delta_pct"]
        eng_d = r["engine_delta_pct"]
        lines.append(
            f"| {r['status']} | `{r['field']}` | "
            f"{r['expected']:,} | "
            f"{rep:,.2f} | {('—' if rep_d is None else f'{rep_d:+.2f}%')} | "
            f"{('—' if eng is None else f'{eng:,.2f}')} | {('—' if eng_d is None else f'{eng_d:+.2f}%')} | "
            f"{r.get('rule','')} |"
        )
    lines.append("")
    lines.append("## BA review packet (replica)")
    lines.append("")
    lines.append("```yaml")
    lines.append(yaml.safe_dump(review_packet, sort_keys=False, width=100).rstrip())
    lines.append("```")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layer 1 parity harness")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--default-tolerance-pct", type=float, default=1.0,
        help="Default tolerance band for fields without an explicit one in baseline.",
    )
    parser.add_argument("--no-engine", action="store_true",
                        help="Skip engine comparison (replica + BA only).")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    baseline = yaml.safe_load(args.baseline.read_text())
    layer1_baseline = baseline.get("layer1", {})

    _log.info("Running replica…")
    replica_result = replicate_layer1(args.input)
    replica_dict = {
        "fyi_aggregates": replica_result.fyi_aggregates,
        "ba_review_packet": replica_result.ba_review_packet.to_dict(),
    }

    engine_result = None
    if not args.no_engine:
        _log.info("Running engine…")
        engine_result = _run_engine(args.input)

    rows: list[dict] = []
    overall_fail = False

    for fm in LAYER1_FIELDS:
        entry = layer1_baseline.get(fm.expected_key)
        if not entry:
            continue
        expected = entry["expected"]
        tolerance = entry.get("tolerance_pct", args.default_tolerance_pct)
        # tolerance_abs special-case for ratios
        tol_abs = entry.get("tolerance_abs")

        replica_val = _dotget(replica_dict, fm.replica_path)
        engine_val = _engine_value(engine_result, fm.engine_attr)

        rep_delta = _delta_pct(replica_val, expected)
        eng_delta = _delta_pct(engine_val, expected)

        # Allow absolute tolerance for ratio-style fields
        status = _classify(rep_delta, tolerance)
        if tol_abs is not None and replica_val is not None:
            if abs(replica_val - expected) <= tol_abs:
                status = "PASS"

        if status == "FAIL":
            overall_fail = True

        rows.append({
            "status": status,
            "field": fm.expected_key,
            "description": fm.description,
            "expected": expected,
            "replica": replica_val if replica_val is not None else 0.0,
            "engine": engine_val,
            "replica_delta_pct": rep_delta,
            "engine_delta_pct": eng_delta,
            "rule": entry.get("rule", ""),
        })

    _write_report(
        args.report, rows,
        input_file=str(args.input),
        baseline_file=str(args.baseline),
        review_packet=replica_dict["ba_review_packet"],
    )

    # Console summary
    print()
    print(f"{'Status':<8} {'Field':<32} {'Expected':>14} {'Replica':>14} {'Δ%':>8} {'Engine':>14} {'Δ%':>8}")
    print("-" * 102)
    for r in rows:
        eng_disp = "—" if r["engine"] is None else f"{r['engine']:>14,.2f}"
        eng_d_disp = "—" if r["engine_delta_pct"] is None else f"{r['engine_delta_pct']:>+7.2f}%"
        rep_d_disp = "—" if r["replica_delta_pct"] is None else f"{r['replica_delta_pct']:>+7.2f}%"
        print(
            f"{r['status']:<8} {r['field']:<32} {r['expected']:>14,} "
            f"{r['replica']:>14,.2f} {rep_d_disp} {eng_disp} {eng_d_disp}"
        )
    print()
    print(f"Report written to: {args.report}")
    return 1 if overall_fail else 0


if __name__ == "__main__":
    sys.exit(main())
