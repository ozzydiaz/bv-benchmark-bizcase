"""
Layer 1 Parity Test \u2014 CI gate

Runs the BA replica AND the engine against the Customer A reference RVTools
sample, then asserts that every Layer 1 field matches the BA's
hand-computed expected values within tolerance.

This is the executable acceptance test for the BV Benchmark engine: any
PR that breaks Layer 1 parity must update the rule book + replica AND
explain why in version-history.md.

The test is skipped automatically when the Customer A sample is unavailable
(e.g. in CI environments without access to the customer file). When
that happens, run it locally before merging:

    python -m pytest tests/test_layer1_parity.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Parametrised test input — never hardcode customer filenames.
# Set BV_PARITY_INPUT to point at the local RVTools file under test (gitignored).
# Falls back to the engagement-aliased filename in the repo root for local
# convenience. CI environments without the file simply skip these tests.
_DEFAULT_INPUT = REPO_ROOT / "customer_a_rvtools_2024-10-29.xlsx"
CUSTOMER_RVTOOLS = Path(os.environ.get("BV_PARITY_INPUT", str(_DEFAULT_INPUT)))
BASELINE = REPO_ROOT / "training" / "baselines" / "customer_a_2024_10" / "ba_expected.yaml"

pytestmark = pytest.mark.skipif(
    not CUSTOMER_RVTOOLS.exists() or not BASELINE.exists(),
    reason=(
        "Layer 1 parity input not available in this environment. "
        "Set BV_PARITY_INPUT=<path-to-rvtools.xlsx> or place the file at "
        f"{_DEFAULT_INPUT.name}."
    ),
)


@pytest.fixture(scope="module")
def baseline() -> dict:
    return yaml.safe_load(BASELINE.read_text())


@pytest.fixture(scope="module")
def replica_result():
    from training.replicas.layer1_ba_replica import replicate_layer1
    return replicate_layer1(CUSTOMER_RVTOOLS)


@pytest.fixture(scope="module")
def engine_result():
    from engine.rvtools_to_inputs import build_business_case
    return build_business_case(
        str(CUSTOMER_RVTOOLS),
        client_name="CustomerA-Parity-Test",
        currency="USD",
        ramp_preset="Extended (100% by Y3)",
    )


# --- replica vs BA: the replica IS the rule book; deltas are bugs in the replica ---

@pytest.mark.parametrize(
    "field, attr, default_tolerance_pct",
    [
        ("num_vms_all",                "num_vms_all",                0.0),
        ("num_vms_poweredon",          "num_vms_poweredon",          0.0),
        ("total_vcpu_all",             "total_vcpu_all",             0.0),
        ("total_memory_gb_poweredon",  "total_memory_gb_poweredon",  1.0),
        ("total_provisioned_gb_all",   "total_provisioned_gb_all",   0.5),
        ("num_hosts",                  "num_hosts",                  0.0),
        ("vcpu_per_core_ratio",        "vcpu_per_core_ratio",        2.0),
    ],
)
def test_replica_matches_ba(replica_result, baseline, field, attr, default_tolerance_pct):
    spec = baseline["layer1"][field]
    expected = spec["expected"]
    actual = replica_result.fyi_aggregates[attr]
    if "tolerance_abs" in spec:
        assert abs(actual - expected) <= spec["tolerance_abs"], (
            f"{field}: replica={actual} expected={expected} (abs tol={spec['tolerance_abs']})"
        )
    else:
        tol = spec.get("tolerance_pct", default_tolerance_pct)
        delta_pct = abs((actual - expected) / expected) * 100.0 if expected else 0.0
        assert delta_pct <= tol, (
            f"{field}: replica={actual} expected={expected} delta={delta_pct:.3f}% > {tol}%"
        )


# --- engine vs BA: deltas here are engine bugs to be tracked in the rule book ---

ENGINE_FIELD_MAP = {
    "num_vms_all":               lambda r: r.inventory.num_vms,
    "num_vms_poweredon":         lambda r: r.inventory.num_vms_poweredon,
    "total_vcpu_all":            lambda r: r.inventory.total_vcpu,
    "total_memory_gb_poweredon": lambda r: r.inventory.total_vmemory_gb_poweredon,
    "total_provisioned_gb_all":  lambda r: r.inventory.total_storage_provisioned_gb,
    "num_hosts":                 lambda r: r.inventory.num_hosts,
    "vcpu_per_core_ratio":       lambda r: r.inventory.vcpu_per_core_ratio,
}


@pytest.mark.parametrize(
    "field, default_tolerance_pct",
    [
        ("num_vms_all",                0.0),
        ("num_vms_poweredon",          0.0),
        ("total_vcpu_all",             0.0),
        ("total_memory_gb_poweredon",  1.0),
        ("total_provisioned_gb_all",   0.5),
        ("num_hosts",                  0.0),
        ("vcpu_per_core_ratio",        2.0),
    ],
)
def test_engine_matches_ba(engine_result, baseline, field, default_tolerance_pct):
    spec = baseline["layer1"][field]
    expected = spec["expected"]
    actual = ENGINE_FIELD_MAP[field](engine_result)
    if "tolerance_abs" in spec:
        assert abs(actual - expected) <= spec["tolerance_abs"], (
            f"{field}: engine={actual} expected={expected} (abs tol={spec['tolerance_abs']})"
        )
    else:
        tol = spec.get("tolerance_pct", default_tolerance_pct)
        delta_pct = abs((actual - expected) / expected) * 100.0 if expected else 0.0
        assert delta_pct <= tol, (
            f"{field}: engine={actual} expected={expected} delta={delta_pct:.3f}% > {tol}%"
        )
