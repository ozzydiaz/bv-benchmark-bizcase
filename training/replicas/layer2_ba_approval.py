"""KP.BA_APPROVAL_GATE — Layer 2 → Layer 3 BA approval payload.

Produces a structured, fully-cited payload describing every right-sizing
decision in a Layer 2 run so the BA can audit it before approving the
transition to Layer 3.

The payload format is engine-agnostic JSON — the same structure can drive a
Streamlit table, an HTML report, or a CSV export.

Citation registry maps each ``cpu_branch`` / ``memory_branch`` /
``storage_branch`` tag to its verbatim R&D-slide anchor + transcript
timestamp + plain-English description.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from training.replicas.layer2_ba_replica import Layer2Result, VMRecordL2


# =============================================================================
# Branch-tag → citation registry
# Keys match the strings emitted by training.replicas.layer2_ba_replica
# (cpu_branch / memory_branch / storage_branch). The "+ba_small_vm_floor"
# suffix is handled separately so it composes with any base branch.
# =============================================================================
BRANCH_CITATIONS: dict[str, dict[str, str]] = {
    # ---------- CPU ----------
    "rd_slide7_cpu_default": {
        "anchor": "R&D.SLIDE7.CPU.DEFAULT",
        "slide": "R&D Slide 7",
        "formula": "rs_vcpus = snapCPU(max(min_vcpus, ceil(vInfo[CPUs] × vHost[CPU%] / 100)))",
        "trigger": "host_proxy strategy + vHost CPU% available",
        "applies": "CPU",
        "rule": "L2.RIGHTSIZE.CPU.HOST_PROXY",
    },
    "rd_slide7_cpu_user_per_vm_telemetry": {
        "anchor": "R&D.SLIDE7.CPU.USER",
        "slide": "R&D Slide 7",
        "formula": "rs_vcpus = snapCPU(max(min_vcpus, ceil(vInfo[CPUs] × (1 − cpu_util_pct/100))))",
        "trigger": "per_vm_telemetry strategy with vMemory/vCpu utilisation present",
        "applies": "CPU",
        "rule": "L2.RIGHTSIZE.CPU.PER_VM_TELEMETRY",
    },
    "rd_slide7_cpu_user_like_for_like": {
        "anchor": "R&D.SLIDE7.CPU.USER",
        "slide": "R&D Slide 7 (reduction% = 0)",
        "formula": "rs_vcpus = snapCPU(max(min_vcpus, vInfo[CPUs]))",
        "trigger": "like_for_like + apply_rd_ladder=True (matcher_algorithm='ba_xa2_match')",
        "applies": "CPU",
        "rule": "L2.RIGHTSIZE.CPU.LIKE_FOR_LIKE",
    },
    "raw_like_for_like": {
        "anchor": "BA.XA2.PASSTHROUGH",
        "slide": "n/a (BA transcript 00:05:49)",
        "formula": "rs_vcpus = max(min_vcpus, vInfo[CPUs])  # NO snap",
        "trigger": "like_for_like + ba_iterative_match (matcher's >= test handles ladder)",
        "applies": "CPU",
        "rule": "L2.RIGHTSIZE.BA_ITERATIVE_MATCH",
    },
    "rd_slide7_cpu_user_ba_fallback": {
        "anchor": "R&D.SLIDE7.CPU.USER",
        "slide": "R&D Slide 7 + KP.BA_FALLBACK_REDUCTIONS (cpu=60%)",
        "formula": "rs_vcpus = snapCPU(max(min_vcpus, ceil(vInfo[CPUs] × (1 − 0.60))))",
        "trigger": "ba_fallback strategy (no telemetry; user-default reductions)",
        "applies": "CPU",
        "rule": "L2.RIGHTSIZE.BA_FALLBACK",
    },
    "rd_slide7_cpu_user_flat_reduction": {
        "anchor": "R&D.SLIDE7.CPU.USER",
        "slide": "R&D Slide 7",
        "formula": "rs_vcpus = snapCPU(max(min_vcpus, ceil(vInfo[CPUs] × (1 − cpu_reduction_pct/100))))",
        "trigger": "flat_reduction strategy with user-supplied cpu_reduction_pct",
        "applies": "CPU",
        "rule": "L2.RIGHTSIZE.FLAT_REDUCTION",
    },
    "raw_fallback_no_host_data": {
        "anchor": "BA.XA2.PASSTHROUGH",
        "slide": "n/a",
        "formula": "rs_vcpus = max(min_vcpus, vInfo[CPUs])",
        "trigger": "host_proxy requested but no host CPU% available",
        "applies": "CPU",
        "rule": "L2.FALLBACK.NO_HOST_DATA",
    },
    # ---------- Memory ----------
    "rd_slide8_memory_default_per_vm_telemetry": {
        "anchor": "R&D.SLIDE8.MEMORY.DEFAULT",
        "slide": "R&D Slide 8 (telemetry path)",
        "formula": "rs_mem_gib = snapMem(max(1, vMemory[Consumed]/1024 × (1 + buffer_pct/100)))",
        "trigger": "per_vm_telemetry strategy with vMemory.Consumed present (no reduction% applied)",
        "applies": "Memory",
        "rule": "L2.RIGHTSIZE.MEMORY.TELEMETRY",
    },
    "rd_slide8_memory_user_like_for_like": {
        "anchor": "R&D.SLIDE8.MEMORY.USER",
        "slide": "R&D Slide 8 (reduction=0, buffer=0)",
        "formula": "rs_mem_gib = snapMem(max(1, vInfo[Memory]/1024))",
        "trigger": "like_for_like + apply_rd_ladder=True",
        "applies": "Memory",
        "rule": "L2.RIGHTSIZE.MEMORY.LIKE_FOR_LIKE",
    },
    "rd_slide8_memory_user_ba_fallback": {
        "anchor": "R&D.SLIDE8.MEMORY.USER",
        "slide": "R&D Slide 8 + KP.BA_FALLBACK_REDUCTIONS (mem=40%, buffer=0%)",
        "formula": "rs_mem_gib = snapMem(max(1, vInfo[Memory]/1024 × (1 − 0.40)))",
        "trigger": "ba_fallback strategy (no telemetry; user-default reductions)",
        "applies": "Memory",
        "rule": "L2.RIGHTSIZE.BA_FALLBACK",
    },
    "rd_slide8_memory_user_flat_reduction": {
        "anchor": "R&D.SLIDE8.MEMORY.USER",
        "slide": "R&D Slide 8",
        "formula": "rs_mem_gib = snapMem(max(1, vInfo[Memory]/1024 × (1 − reduction_pct/100) × (1 + buffer_pct/100)))",
        "trigger": "flat_reduction strategy with user-supplied reduction_pct and/or buffer_pct",
        "applies": "Memory",
        "rule": "L2.RIGHTSIZE.FLAT_REDUCTION",
    },
    # ---------- Storage ----------
    "rd_slide9_storage_default_in_use": {
        "anchor": "BA.XA2.GETMANAGEDDISK4",
        "slide": "BA transcript 00:17:31",
        "formula": "rs_disk_gib = max(4, ceil(vInfo[In Use MiB]/1024))",
        "trigger": "like_for_like or per_vm_telemetry + vInfo In Use GB > 0",
        "applies": "Storage",
        "rule": "L2.RIGHTSIZE.STORAGE.IN_USE",
    },
    "rd_slide9_storage_default": {
        "anchor": "R&D.SLIDE9.STORAGE.DEFAULT",
        "slide": "R&D Slide 9 (telemetry path)",
        "formula": "rs_disk_gib = ceil((Σ vPartition[Consumed]/1024 × (1 + buffer/100)) / 128) × 128",
        "trigger": "per_vm_telemetry + vPartition.Consumed > 0",
        "applies": "Storage",
        "rule": "L2.RIGHTSIZE.STORAGE.TELEMETRY",
    },
    "rd_slide9_storage_capacity": {
        "anchor": "R&D.SLIDE9.STORAGE.CAPACITY",
        "slide": "R&D Slide 9",
        "formula": "rs_disk_gib = ceil((Σ vPartition[Capacity]/1024 × (1 − red/100) × (1 + buf/100)) / 128) × 128",
        "trigger": "vPartition.Capacity present, Consumed = 0/null OR user-supplied reduction/buffer",
        "applies": "Storage",
        "rule": "L2.RIGHTSIZE.STORAGE.CAPACITY",
    },
    "rd_slide9_storage_vinfo": {
        "anchor": "R&D.SLIDE9.STORAGE.VINFO",
        "slide": "R&D Slide 9 (no vPartition fallback)",
        "formula": "rs_disk_gib = ceil((vInfo[Total disk capacity MiB]/1024 × (1 − red/100) × (1 + buf/100)) / 128) × 128",
        "trigger": "vPartition tab absent — sized from vInfo[Total disk capacity MiB]",
        "applies": "Storage",
        "rule": "L2.RIGHTSIZE.STORAGE.VINFO",
    },
}

BA_FLOOR_CITATION = {
    "anchor": "KP.BA_SMALL_VM_FLOOR",
    "slide": "BA judgment overlay (Customer A 2024-10 Xa2-fixed audit)",
    "formula": "effective_source = max(raw_source, ba_min_*_floor)  # before R&D formula",
    "trigger": "ba_min_vcpu_floor or ba_min_memory_gib_floor option set; raw source < floor",
    "applies": "CPU/Memory pre-sizing pad",
    "rule": "L2.RIGHTSIZE.BA_SMALL_VM_FLOOR",
}


def cite_branch(branch_tag: str) -> dict[str, Any]:
    """Decompose a branch tag (possibly suffixed with '+ba_small_vm_floor')
    and return its citation(s) as a list."""
    parts = branch_tag.split("+")
    base = parts[0]
    base_cite = BRANCH_CITATIONS.get(base, {
        "anchor": "UNKNOWN",
        "slide": "?",
        "formula": "?",
        "trigger": branch_tag,
        "applies": "?",
        "rule": "?",
    })
    cites = [base_cite]
    if "ba_small_vm_floor" in parts[1:]:
        cites.append(BA_FLOOR_CITATION)
    return {
        "branch_tag": branch_tag,
        "primary": base_cite,
        "modifiers": cites[1:],
    }


def build_per_vm_annotation(vm: VMRecordL2) -> dict[str, Any]:
    """Return one row of the BA approval payload for a single VM."""
    return {
        "vm": vm.name,
        "source": {
            "raw_vcpu": vm.vinfo_cpus,
            "raw_memory_gib": round(vm.vinfo_memory_mib / 1024.0, 2),
            "effective_vcpu": vm.effective_source_vcpu or vm.vinfo_cpus,
            "effective_memory_gib": round(vm.effective_source_mem_gib or vm.vinfo_memory_mib / 1024.0, 2),
            "ba_floor_applied": vm.ba_floor_applied,
        },
        "rightsized": {
            "rs_vcpu": vm.rs_vcpus,
            "rs_mem_gib": vm.rs_mem_gib,
            "rs_disk_gib": vm.rs_disk_gib,
            "min_vcpus": vm.min_vcpus,
            "vcpu_floor_source": vm.vcpu_floor_source,
        },
        "sku": {
            "name": vm.sku_name,
            "family": vm.sku_family,
            "processor": vm.sku_processor,
            "vcpu": vm.sku_vcpu,
            "memory_gib": vm.sku_memory_gib,
        },
        "match": {
            "final_vcpu": vm.final_vcpus,
            "final_mem_gib": vm.final_mem_gib,
            "retry_path": vm.retry_path,
            "iterations": vm.retry_iterations,
            "match_failed": vm.match_failed,
        },
        "pricing_per_offer_usd_yr": {
            offer: round(vm.pricing.get(offer, {}).get("usd_yr", 0.0), 2)
            for offer in ("payg", "ri1y", "ri3y", "sp1y", "sp3y")
        },
        "storage_payg_usd_yr": round(vm.storage_payg_usd_yr, 2),
        "citations": {
            "cpu":     cite_branch(vm.cpu_branch),
            "memory":  cite_branch(vm.memory_branch),
            "storage": cite_branch(vm.storage_branch),
        },
    }


def build_ba_approval_payload(result: Layer2Result, *, run_meta: dict | None = None) -> dict[str, Any]:
    """Produce the full BA approval payload for a Layer 2 run.

    Returns a JSON-serialisable dict with:
      - run_meta: which strategy/algorithm/floor flags were used
      - aggregates: total vCPU, memory, storage, ACR per offer
      - per_vm: list of per-VM annotations (every right-sizing decision cited)
    """
    payload = {
        "run_meta": run_meta or {},
        "aggregates": {
            "vm_count": len(result.per_vm),
            "sum_vcpu": sum(vm.final_vcpus or vm.rs_vcpus for vm in result.per_vm),
            "sum_memory_gib": sum(vm.final_mem_gib or vm.rs_mem_gib for vm in result.per_vm),
            "sum_storage_gib": sum(vm.rs_disk_gib for vm in result.per_vm),
            "acr_payg_usd_yr": round(sum(vm.pricing.get("payg", {}).get("usd_yr", 0.0) for vm in result.per_vm), 2),
            "acr_ri3y_usd_yr": round(sum(vm.pricing.get("ri3y", {}).get("usd_yr", 0.0) for vm in result.per_vm), 2),
            "storage_payg_usd_yr": round(sum(vm.storage_payg_usd_yr for vm in result.per_vm), 2),
            "vms_with_ba_floor": sum(1 for vm in result.per_vm if vm.ba_floor_applied),
        },
        "per_vm": [build_per_vm_annotation(vm) for vm in result.per_vm],
    }
    return payload
