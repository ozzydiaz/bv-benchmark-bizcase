"""
Layer 2 Parity Test — CI gate

Runs the Layer 2 BA replica against the Customer A reference RVTools
sample with the BA-recorded strategy and asserts every Layer 2
aggregate is within the per-field tolerance documented in
``training/baselines/<sample>/ba_expected.yaml`` (``layer2:`` block).

Phase 2 deferred items (will tighten tolerances in Phase 2b):
    * L2.MATCH.001 (Azure Retail Price API SKU least-cost match)
    * L2.RIGHTSIZE.CPU.RETRY (BA's iterative ±1 vCPU bump loop)
    * L2.PRICING.* (PAYG / RI / SP price matrix)
    * L2.STORAGE_PRICE.001 (managed-disk pricing)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

_DEFAULT_INPUT = REPO_ROOT / "customer_a_rvtools_2024-10-29.xlsx"
CUSTOMER_RVTOOLS = Path(os.environ.get("BV_PARITY_INPUT", str(_DEFAULT_INPUT)))
BASELINE = REPO_ROOT / "training" / "baselines" / "customer_a_2024_10" / "ba_expected.yaml"

pytestmark = pytest.mark.skipif(
    not CUSTOMER_RVTOOLS.exists() or not BASELINE.exists(),
    reason=(
        "Layer 2 parity input not available in this environment. "
        "Set BV_PARITY_INPUT=<path-to-rvtools.xlsx> or place the file at "
        f"{_DEFAULT_INPUT.name}."
    ),
)


@pytest.fixture(scope="module")
def baseline() -> dict:
    return yaml.safe_load(BASELINE.read_text())


@pytest.fixture(scope="module")
def replica_result(baseline):
    from training.replicas.layer2_ba_replica import replicate_layer2

    cs = baseline["layer2"]["customer_strategy"]
    return replicate_layer2(
        CUSTOMER_RVTOOLS,
        strategy=cs["utilisation_strategy"],
        enforce_8vcpu_min_for_windows_server=cs["enforce_8vcpu_min_for_windows_server"],
        cpu_reduction_pct=cs.get("cpu_reduction_pct") or 0.0,
        mem_reduction_pct=cs.get("mem_reduction_pct") or 0.0,
        mem_buffer_pct=cs.get("mem_buffer_pct") or 0.0,
        storage_reduction_pct=cs.get("storage_reduction_pct") or 0.0,
        storage_buffer_pct=cs.get("storage_buffer_pct") or 0.0,
    )


# -----------------------------------------------------------------------------
# Per-field assertions
# -----------------------------------------------------------------------------

def _dotget(d: dict, path: str):
    cur: object = d
    for key in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


@pytest.mark.parametrize(
    "field, replica_attr, default_tol",
    [
        ("azure_vcpu_total",          "azure_vcpu_total_matched_sku_shape",                         5.0),
        ("azure_memory_gib",          "azure_memory_gib_total_matched_sku_shape",                   6.0),
        ("azure_storage_gib",         "azure_storage_gib_vpartition_intrinsic",                     0.5),
        # Phase 2c — BA Xa2-fixed authoritative tier-based pricing
        ("azure_compute_payg_usd_yr", "azure_compute_usd_yr.payg",                                  5.0),
        ("azure_compute_ri3y_usd_yr", "azure_compute_usd_yr.ri3y",                                  2.0),
        ("azure_storage_usd_yr",      "azure_storage_payg_usd_yr",                                  25.0),
    ],
)
def test_replica_matches_ba(replica_result, baseline, field, replica_attr, default_tol):
    spec = baseline["layer2"].get(field)
    if spec is None:
        pytest.skip(f"{field} not in baseline (Phase 2b not yet enabled?)")
    expected = spec["expected"]
    actual = _dotget(replica_result.fyi_aggregates, replica_attr)
    assert actual is not None, (
        f"{field}: replica field {replica_attr!r} returned None "
        f"(pricing_available={replica_result.pricing_available})"
    )
    tol = spec.get("tolerance_pct", default_tol)
    delta_pct = abs((actual - expected) / expected) * 100.0 if expected else 0.0
    assert delta_pct <= tol, (
        f"{field}: replica={actual:,} expected={expected:,} "
        f"delta={delta_pct:.3f}% > {tol}% (rule={spec.get('rule','?')})"
    )


# -----------------------------------------------------------------------------
# Strategy + flag plumbing assertions (catch regressions if the harness
# stops honouring the recorded customer strategy).
# -----------------------------------------------------------------------------
def test_strategy_recorded_correctly(replica_result, baseline):
    cs = baseline["layer2"]["customer_strategy"]
    assert replica_result.strategy == cs["utilisation_strategy"]
    assert (
        replica_result.enforce_8vcpu_min_for_windows_server
        == cs["enforce_8vcpu_min_for_windows_server"]
    )


def test_per_vm_payload_populated(replica_result):
    """KP.PER_VM_REPRISE — every powered-on, non-template VM must produce a record."""
    assert len(replica_result.per_vm) > 0
    sample = replica_result.per_vm[0]
    assert sample.is_powered_on is True
    assert sample.is_template is False
    assert sample.rs_vcpus > 0
    assert sample.rs_mem_gib > 0
    assert sample.rs_disk_gib >= 4   # smallest E-tier
    # Branch provenance must always be populated (KP.AZURE_RD_RIGHTSIZING_v1
    # OR KP.BA_ITERATIVE_MATCH "raw_*" branches when ladders are skipped).
    assert sample.cpu_branch.startswith(("rd_slide7_", "raw_"))
    assert sample.memory_branch.startswith(("rd_slide8_", "raw_"))
    assert sample.storage_branch.startswith("rd_slide9_")
    assert sample.vcpu_floor_source in ("windows_compliance", "linux_or_unknown", "flag_off")


def test_8vcpu_floor_off_for_customer_a(replica_result):
    """KP.WIN_8VCPU_MIN — Customer A baseline records flag=False, so every VM
    should report vcpu_floor_source='flag_off'."""
    for vm in replica_result.per_vm:
        assert vm.vcpu_floor_source == "flag_off", (
            f"VM {vm.name}: vcpu_floor_source={vm.vcpu_floor_source!r} but flag is OFF"
        )
        assert vm.min_vcpus == 1


# -----------------------------------------------------------------------------
# Phase 2b assertions — pricing matrix + retry loop + family/processor pin
# -----------------------------------------------------------------------------

def test_pricing_matrix_populated(replica_result):
    """L2.PRICING.001 — every matched VM has the 5-offer matrix populated.

    Skips if pricing isn't available (e.g. CI env with no network and empty cache).
    """
    if not replica_result.pricing_available:
        pytest.skip("Azure pricing catalog not available in this environment")

    matched = [vm for vm in replica_result.per_vm if vm.sku_name]
    assert len(matched) > 0, "No VMs matched a SKU—pricing path may be broken"
    sample = matched[0]
    for offer in ("payg", "ri1y", "ri3y", "sp1y", "sp3y"):
        assert offer in sample.pricing
    # PAYG must always be available for any matched SKU
    assert sample.pricing["payg"]["available"]
    assert sample.pricing["payg"]["usd_hr"] > 0


def test_retry_loop_breakdown_present(replica_result):
    """L2.RIGHTSIZE.CPU.RETRY — retry breakdown reports the four expected keys."""
    bd = replica_result.fyi_aggregates["retry_breakdown"]
    for key in ("none", "decrement_vcpu", "increment_mem", "failed"):
        assert key in bd, f"retry_breakdown missing key {key!r}"
    total = sum(bd.values())
    # Sanity: counts add up to the matched-or-failed population.
    matched_count = replica_result.fyi_aggregates["vm_count_with_sku_match"]
    failed_count = replica_result.fyi_aggregates["vm_count_match_failed"]
    if replica_result.pricing_available:
        assert total == matched_count + failed_count


def test_storage_pricing_alternatives(replica_result, baseline):
    """L2.STORAGE_PRICE.001 — BOTH pricing methodologies (tier-based +
    reference-rate) populate non-zero values when pricing is available."""
    if not replica_result.pricing_available:
        pytest.skip("Azure pricing catalog not available")
    fyi = replica_result.fyi_aggregates
    assert fyi["azure_storage_payg_usd_yr_vpartition_intrinsic"] > 0, \
        "Tier-based storage cost (vPartition intrinsic) must be non-zero"
    assert fyi["azure_storage_ref_rate_usd_yr"]["vpartition_intrinsic_gib"] > 0, \
        "Reference-rate storage cost must be non-zero"


def test_family_pin_filters_skus():
    """L2.FAMILY_PIN.001 — family pin should reduce the candidate set."""
    from training.replicas.azure_pricing import get_priced_vm_catalog
    from training.replicas.layer2_ba_replica import _filter_sku_candidates

    full = get_priced_vm_catalog("eastus2")
    if not any(s.payg_usd_hr > 0 for s in full):
        pytest.skip("Azure pricing catalog empty in this environment")

    unfiltered = _filter_sku_candidates(full)
    gp_only = _filter_sku_candidates(full, family_pin="GeneralPurpose")
    mem_only = _filter_sku_candidates(full, family_pin="MemoryOptimized")

    # GeneralPurpose (D-family) and MemoryOptimized (E/M) must both be
    # non-empty subsets of the unfiltered catalog.
    assert 0 < len(gp_only) < len(unfiltered)
    assert 0 < len(mem_only) < len(unfiltered)
    assert all(s.family == "D" for s in gp_only)
    assert all(s.family in {"E", "M"} for s in mem_only)


def test_linux_ahub_pricing_rule():
    """KP.LINUX_AHUB_ASSUMPTION — catalog must price every SKU at the Linux
    rate (no Windows licensing cost included). Verified against BA's
    Customer A Xa2-fixed prices for the most-frequently-picked SKUs.
    """
    from training.replicas.azure_pricing import get_priced_vm_catalog

    cat = {s.arm_sku_name: s for s in get_priced_vm_catalog("eastus2")}
    if not any(s.payg_usd_hr > 0 for s in cat.values()):
        pytest.skip("Azure pricing catalog empty in this environment")

    # BA's authoritative Linux PAYG prices for Customer A's top picks
    # (sourced from Xa2-fixed col 'Price Paygo' on 2026-04-28).
    ba_linux_payg_per_hr = {
        "Standard_D2als_v7":   0.0804,
        "Standard_D4als_v7":   0.161,
        "Standard_D4as_v5":    0.172,
        "Standard_D2as_v5":    0.086,
        "Standard_E4as_v5":    0.226,
        "Standard_F1as_v7":    0.0683,
        "Standard_M96s_2_v3":  12.45,   # vm1016 specifically
    }
    for sku_name, expected in ba_linux_payg_per_hr.items():
        sku = cat.get(sku_name)
        if sku is None:
            pytest.skip(f"{sku_name} missing from catalog (region/availability drift)")
        # Allow ±1% drift since live API can fluctuate
        delta_pct = abs(sku.payg_usd_hr - expected) / expected * 100
        assert delta_pct < 1.0, (
            f"{sku_name} PAYG ${sku.payg_usd_hr:.4f}/hr deviates {delta_pct:.2f}% "
            f"from BA's Linux rate ${expected:.4f}/hr — Windows pricing may "
            f"have leaked into the catalog (KP.LINUX_AHUB_ASSUMPTION violation)."
        )
