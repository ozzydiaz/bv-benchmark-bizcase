"""
Layer 3 Three-Way Auditor (the "Judge")
========================================

A trust-building, adversarial parity scorer that compares **three** sources
for every Layer 3 financial metric:

    A = BA workbook (oracle, frozen)               ← from layer3_golden_extractor
    B = Python replica (formula-faithful)          ← from layer3_ba_replica
    C = Production engine (engine/financial_*)     ← from engine package

For every metric (label) the auditor records:

    Δ_AB = |B − A|        replica vs BA
    Δ_AC = |C − A|        engine vs BA
    Δ_BC = |B − C|        replica vs engine (sanity)

Each Δ is scored against a **tiered tolerance** that gets stricter as the
absolute amount shrinks (per the user's auditing principle: "the smaller
the absolute currency amount, the greater the need for perfect 0% deviation").

────────────────────────────────────────────────────────────────────────
TOLERANCE TIERS (applied per cell value ``a`` from source A):
    |a| < $1.00       → exact (Δ ≤ $0.005)
    |a| < $100        → Δ ≤ $0.01     (penny tolerance)
    |a| < $10,000     → Δ ≤ $1.00     (dollar tolerance)
    |a| < $1,000,000  → Δ% ≤ 0.10%
    |a| ≥ $1,000,000  → Δ% ≤ 1.00%
    (rates such as ROI are scaled by 1.0, so "$1.00" tier maps to Δ ≤ 0.005)

For values where ``a == 0`` (e.g. Customer A's ACO/ECIF, Y0 savings) the
replica/engine MUST also be zero (Δ ≤ $0.005). This is the strictest tier.

The auditor returns a structured ``ScorecardReport`` and prints an
opinionated, colour-coded markdown summary suitable for CI, code review,
and BA spot-check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .layer3_golden_extractor import CellRef, LayerThreeGolden, flatten_golden


# ---------------------------------------------------------------------------
# Tolerance bands
# ---------------------------------------------------------------------------

ABS_FLOOR_EXACT = 0.005  # $0.005 — accepts noise from BA's own rounding


def absolute_tolerance(reference_value: float) -> float:
    """
    Tiered absolute-dollar tolerance.

    Stricter for small absolute amounts; relaxes (relatively) only for
    very large totals where ±0.1-1% noise is acceptable.
    """
    a = abs(reference_value)
    if a < 1.0:
        return ABS_FLOOR_EXACT  # rates / small numbers must be exact
    if a < 100.0:
        return 0.01  # penny
    if a < 10_000.0:
        return 1.00  # dollar
    if a < 1_000_000.0:
        return a * 0.001  # 0.10%
    return a * 0.01  # 1.00%


def tier_label(reference_value: float) -> str:
    a = abs(reference_value)
    if a < 1.0:
        return "EXACT"
    if a < 100.0:
        return "<$100"
    if a < 10_000.0:
        return "<$10K"
    if a < 1_000_000.0:
        return "<$1M"
    return "≥$1M"


# ---------------------------------------------------------------------------
# Scorecard data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellAudit:
    """Audit result for one (label, cell) tuple across the 3 sources."""

    label: str
    ref: CellRef
    a_value: float  # BA oracle
    b_value: float | None  # replica (None if not yet implemented)
    c_value: float | None  # engine (None if not yet implemented)
    tolerance: float
    tier: str

    @property
    def delta_ab(self) -> float | None:
        return None if self.b_value is None else abs(self.b_value - self.a_value)

    @property
    def delta_ac(self) -> float | None:
        return None if self.c_value is None else abs(self.c_value - self.a_value)

    @property
    def delta_bc(self) -> float | None:
        if self.b_value is None or self.c_value is None:
            return None
        return abs(self.b_value - self.c_value)

    @property
    def replica_passes(self) -> bool | None:
        return None if self.delta_ab is None else self.delta_ab <= self.tolerance

    @property
    def engine_passes(self) -> bool | None:
        return None if self.delta_ac is None else self.delta_ac <= self.tolerance

    @property
    def replica_engine_agree(self) -> bool | None:
        return None if self.delta_bc is None else self.delta_bc <= self.tolerance


@dataclass
class ScorecardReport:
    """Complete 3-way audit report."""

    customer_name: str
    workbook_path: str
    audits: list[CellAudit] = field(default_factory=list)

    # ----- summary statistics --------------------------------------------
    @property
    def total_cells(self) -> int:
        return len(self.audits)

    @property
    def replica_pass_count(self) -> int:
        return sum(1 for a in self.audits if a.replica_passes)

    @property
    def replica_fail_count(self) -> int:
        return sum(1 for a in self.audits if a.replica_passes is False)

    @property
    def replica_unknown_count(self) -> int:
        return sum(1 for a in self.audits if a.replica_passes is None)

    @property
    def engine_pass_count(self) -> int:
        return sum(1 for a in self.audits if a.engine_passes)

    @property
    def engine_fail_count(self) -> int:
        return sum(1 for a in self.audits if a.engine_passes is False)

    @property
    def engine_unknown_count(self) -> int:
        return sum(1 for a in self.audits if a.engine_passes is None)

    def replica_pass_rate(self) -> float:
        n_known = self.total_cells - self.replica_unknown_count
        return self.replica_pass_count / n_known if n_known else 0.0

    def engine_pass_rate(self) -> float:
        n_known = self.total_cells - self.engine_unknown_count
        return self.engine_pass_count / n_known if n_known else 0.0

    # ----- worst offenders ------------------------------------------------
    def worst_replica_offenders(self, top_n: int = 10) -> list[CellAudit]:
        candidates = [a for a in self.audits if a.replica_passes is False]
        candidates.sort(key=lambda x: (x.delta_ab or 0) / max(x.tolerance, 1e-9), reverse=True)
        return candidates[:top_n]

    def worst_engine_offenders(self, top_n: int = 10) -> list[CellAudit]:
        candidates = [a for a in self.audits if a.engine_passes is False]
        candidates.sort(key=lambda x: (x.delta_ac or 0) / max(x.tolerance, 1e-9), reverse=True)
        return candidates[:top_n]


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------


def audit(
    golden: LayerThreeGolden,
    replica_values: dict[str, float] | None = None,
    engine_values: dict[str, float] | None = None,
) -> ScorecardReport:
    """
    Run the 3-way audit.

    ``replica_values`` and ``engine_values`` are dicts keyed by the same
    label strings that ``flatten_golden()`` produces (e.g.
    ``"status_quo.Server Depreciation.Y0"``). Missing keys score as
    ``None`` ("not yet implemented") rather than failing.
    """
    replica_values = replica_values or {}
    engine_values = engine_values or {}

    report = ScorecardReport(customer_name=golden.customer_name, workbook_path=golden.workbook_path)

    for label, ref, a_value in flatten_golden(golden):
        b_value = replica_values.get(label)
        c_value = engine_values.get(label)
        tol = absolute_tolerance(a_value)
        tier = tier_label(a_value)
        report.audits.append(
            CellAudit(
                label=label,
                ref=ref,
                a_value=a_value,
                b_value=b_value,
                c_value=c_value,
                tolerance=tol,
                tier=tier,
            )
        )

    return report


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_summary(report: ScorecardReport) -> str:
    """One-paragraph headline + summary table."""
    lines = []
    lines.append(f"# Layer 3 Parity Audit — {report.customer_name}")
    lines.append("")
    lines.append(f"- **Workbook**: `{report.workbook_path}`")
    lines.append(f"- **Cells audited**: {report.total_cells}")
    lines.append("")
    lines.append("| Source | Pass | Fail | N/A | Pass-rate |")
    lines.append("|---|---:|---:|---:|---:|")
    lines.append(
        f"| **Replica vs BA** (Δ_AB) | {report.replica_pass_count} | "
        f"{report.replica_fail_count} | {report.replica_unknown_count} | "
        f"{report.replica_pass_rate():.1%} |"
    )
    lines.append(
        f"| **Engine vs BA** (Δ_AC) | {report.engine_pass_count} | "
        f"{report.engine_fail_count} | {report.engine_unknown_count} | "
        f"{report.engine_pass_rate():.1%} |"
    )
    return "\n".join(lines)


def render_offender_table(audits: Iterable[CellAudit], delta_attr: str, header: str) -> str:
    rows = list(audits)
    if not rows:
        return f"### {header}\n\n_None — clean run._\n"

    lines = [f"### {header}", ""]
    lines.append("| Label | Cell | BA value | Other | Δ | Tol | Tier |")
    lines.append("|---|---|---:|---:|---:|---:|:---:|")
    for a in rows:
        delta = getattr(a, delta_attr)
        if delta is None:
            continue
        other = a.b_value if delta_attr == "delta_ab" else a.c_value
        lines.append(
            f"| `{a.label}` | `{a.ref.sheet}!{a.ref.address}` "
            f"| {a.a_value:,.2f} | {other:,.2f} | {delta:,.4f} | "
            f"{a.tolerance:,.4f} | {a.tier} |"
        )
    return "\n".join(lines) + "\n"


def render_full_report(report: ScorecardReport, top_n: int = 15) -> str:
    parts = [render_summary(report), "", "---", ""]
    parts.append(render_offender_table(report.worst_replica_offenders(top_n), "delta_ab", f"Worst Replica Offenders (top {top_n})"))
    parts.append(render_offender_table(report.worst_engine_offenders(top_n), "delta_ac", f"Worst Engine Offenders (top {top_n})"))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Verdict — overall pass/fail decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    replica_clean: bool
    engine_clean: bool
    headline: str

    @property
    def overall_clean(self) -> bool:
        return self.replica_clean and self.engine_clean


def verdict(report: ScorecardReport) -> Verdict:
    rc = report.replica_fail_count == 0 and report.replica_unknown_count == 0
    ec = report.engine_fail_count == 0 and report.engine_unknown_count == 0
    if rc and ec:
        head = "✅ CLEAN — replica and engine both within tolerance vs BA spreadsheet"
    elif rc:
        head = "🟡 REPLICA CLEAN, ENGINE FAILING — production code diverges from BA"
    elif ec:
        head = "🟡 ENGINE CLEAN, REPLICA FAILING — replica logic needs review"
    elif report.replica_unknown_count == report.total_cells and report.engine_unknown_count == report.total_cells:
        head = "⚪ NOT IMPLEMENTED — both replica and engine empty"
    else:
        head = "🔴 BOTH FAILING — investigate replica + engine"
    return Verdict(replica_clean=rc, engine_clean=ec, headline=head)
