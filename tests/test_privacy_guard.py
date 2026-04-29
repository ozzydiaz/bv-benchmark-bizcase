"""
Customer-Privacy Guard
======================

CI gate that prevents customer-identifying tokens from being committed to
the repository. Per-engagement aliases (``customer_a``, ``customer_b``,
``Customer A``, etc.) are PERMITTED — these are the deliberately anonymised
labels we use throughout the codebase. Real customer names, datacenter
labels, hostnames and similar PII are NOT permitted in tracked source.

If you legitimately need to add a new alias, extend ``PERMITTED_ALIASES``
below AND get an explicit privacy-review nod in the PR.

To run locally::

    python -m pytest tests/test_privacy_guard.py -v
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# -----------------------------------------------------------------------------
# Forbidden tokens — extend deliberately and only with privacy review.
# Each entry is a regex (case-insensitive), checked against tracked source
# files. Patterns must be specific enough to avoid collateral damage on
# unrelated identifiers.
# -----------------------------------------------------------------------------
FORBIDDEN_PATTERNS: list[str] = [
    # Healthcare-system customer A acronyms / common nicknames
    r"\bUHHS\b",
    r"\bUHHC\b",
    r"\bUH\s*HC\b",
    # Customer A's regional cluster / datacenter labels
    r"\bUCRC\b",
    r"\bUHEPIC\b",
    # Reference workbook source that contained customer-A intermediates
    r"\bRELIANCE\b",
    # Geographic / org identifiers
    r"\bCleveland\b",
    r"\bUniversity\s+Hospitals\b",
    # Customer A's specific datacenter names
    r"\bSamaritan\b",
    r"\bINVOLTA\b",
]

# Files that are KNOWN to legitimately contain a forbidden token
# (e.g. this guard test itself, which references the tokens to forbid them).
PATH_ALLOWLIST: set[str] = {
    "tests/test_privacy_guard.py",
}

# File extensions to scan. Binary extensions are deliberately omitted —
# RVTools workbooks are gitignored by .gitignore policy and never tracked.
SCANNED_EXTENSIONS: set[str] = {
    ".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".json",
    ".cfg", ".ini", ".sh", ".html",
}


def _list_tracked_files() -> list[Path]:
    """Return the list of git-tracked files under SCANNED_EXTENSIONS."""
    try:
        out = subprocess.check_output(
            ["git", "ls-files"], cwd=REPO_ROOT, text=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("git not available — privacy guard cannot enumerate tracked files")
    files = []
    for line in out.splitlines():
        p = REPO_ROOT / line
        if p.suffix.lower() in SCANNED_EXTENSIONS and line not in PATH_ALLOWLIST:
            if p.exists():
                files.append(p)
    return files


def test_no_customer_names_in_tracked_files() -> None:
    """No tracked source file may contain a known customer-identifying token."""
    compiled = [(p, re.compile(p, re.IGNORECASE)) for p in FORBIDDEN_PATTERNS]
    violations: list[str] = []

    for path in _list_tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern_str, pattern in compiled:
            for m in pattern.finditer(text):
                # Locate line number for a useful error message.
                line_no = text.count("\n", 0, m.start()) + 1
                line = text.splitlines()[line_no - 1].strip()
                violations.append(
                    f"  {path.relative_to(REPO_ROOT)}:{line_no} matches {pattern_str!r}: {line[:120]}"
                )

    if violations:
        pytest.fail(
            "Customer-identifying tokens found in tracked files. Replace them "
            "with the anonymised aliases (`customer_a`, `Customer A`, "
            "`<datacenter-name>`, etc.) and re-run.\n\n"
            + "\n".join(violations[:50])
            + (f"\n... (+{len(violations) - 50} more)" if len(violations) > 50 else "")
        )


def test_no_customer_names_in_tracked_filenames() -> None:
    """No tracked file path itself may contain a customer-identifying token."""
    name_patterns = [re.compile(p, re.IGNORECASE) for p in FORBIDDEN_PATTERNS]
    violations: list[str] = []

    try:
        out = subprocess.check_output(
            ["git", "ls-files"], cwd=REPO_ROOT, text=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("git not available")

    for line in out.splitlines():
        for pattern in name_patterns:
            if pattern.search(line):
                violations.append(f"  {line} (matches {pattern.pattern!r})")
                break

    if violations:
        pytest.fail(
            "Customer-identifying tokens found in tracked filenames. "
            "Rename via `git mv` to use anonymised aliases.\n\n"
            + "\n".join(violations)
        )
