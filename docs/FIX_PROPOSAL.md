# BV Benchmark Business Case — Targeted Fix Proposal
**File:** UHHS_RVTools_export_all_2024-10-29  
**Reference:** UHHS-analysis-feedback.md  
**Model source of truth:** Template_BV Benchmark Business Case v6.xlsm  
**Date:** 2026-04-xx

---

## 0. Executive Summary

The BA comparison shows three categories of error: inventory counts are wrong, Azure pricing is broken, and storage sizing uses the wrong source. These three issues cascade — they all feed directly into the financial model, which is why Layer 3 looks "totally off." The financial model's *structure* in Python is correct (NPV, depreciation, retained costs formulas all match the Excel). Only the inputs are wrong.

This document describes **12 targeted, point-specific fixes** across the three layers, ranks them by severity, and assesses which functions should become discrete tools or skill entries.

---

## 1. Issues by Layer and Severity

### CRITICAL — directly zeros out financial outputs

| # | Layer | File | Issue | Impact |
|---|-------|------|-------|--------|
| C1 | 2 | `azure_sku_matcher.py` | VM catalog cache files are empty `{}` — all Azure VM compute prices = $0.00 | Azure compute cost ≈ $0 instead of ~$6.7M/yr |
| C2 | 2 | `azure_sku_matcher.py` | Disk rate constant `_DEFAULT_GB_RATE = 1.6e-05` is ~4,688× too low | Storage cost ≈ $0 instead of ~$4.1M/yr |
| C3 | 1 | `rvtools_parser.py` | Storage source is vDisk provisioned (`/1024` GiB) instead of vPartition **Capacity** (`/953.67` GB) | Azure storage 3.08M GB vs BA 4.39M GB (29% under) |

### HIGH — inventory scope errors drive wrong status quo TCO

| # | Layer | File | Issue | Impact |
|---|-------|------|-------|--------|
| H1 | 1 | `rvtools_parser.py` | Template VMs (34 rows) excluded from `num_vms` / `total_vcpu` / TCO baseline | Total VMs 2,797 vs BA 2,831; TCO under-sized |
| H2 | 1 | `rvtools_parser.py` | When vHost is present, TCO baseline auto-triggers to powered-on only — excludes 136 powered-off VMs | TCO vCPU 14,628 vs BA 15,330 |
| H3 | 1 | `rvtools_parser.py` | vPartition MiB → GB conversion uses `/1024` (binary GiB) instead of `/953.67` (decimal GB) | Partition-based storage understated ~7.4% |
| H4 | 1 | `rvtools_parser.py` | 6 EPIC-RDB cluster hosts with `vCPUs per Core == 0` included in ratio average | vCPU/Core ratio 1.52 vs BA 1.58 |

### MEDIUM — sizing accuracy

| # | Layer | File | Issue | Impact |
|---|-------|------|-------|--------|
| M1 | 2 | `consumption_builder.py` | vCPU/vMemory telemetry path still active despite obfuscated VM names (`vm100`...`vm2661`) that never match | 641 anomaly VMs; Azure vCPU inflated +25% |
| M2 | 2 | `azure_sku_matcher.py` | No 8-core minimum in `match_sku()` | Small VMs snapped to 2- or 4-core SKUs; BA uses 8-core floor |
| M3 | 2 | `consumption_builder.py` | No like-for-like (non-rightsized) sizing path | BA shows both 16,318 vCPU provisioned and rightsized count |
| M4 | 1 | `rvtools_parser.py` | EPIC-RDB hosts with 0 assigned VMs inflate `num_hosts` (280 vs BA 242) | Host-based benchmarks (cabinets, NW cost) overstated ~16% |

### LOW — data quality / display

| # | Layer | File | Issue | Impact |
|---|-------|------|-------|--------|
| L1 | 1 | `rvtools_parser.py` | `env_tagging_present=True` fires on backup job labels (`Backups`, `Backup_CLE_ENT01`) which are Avamar job names, not lifecycle env tags | Misleading "env tagging present" badge in UI; no financial impact |

---

## 2. Fix Specifications

### Fix C1 — VM Catalog Cache Validation and Refresh
**File:** `engine/azure_sku_matcher.py`  
**Root cause:** `_read_cache()` accepts `{}` as a valid cache hit. A prior API call returned an empty response body, was cached, and is now served as a valid catalog.  

**Changes:**
1. In `_read_cache()`, add a guard: reject a cache entry if `len(data) < 5` (empty or near-empty dict).
2. In `get_vm_catalog()`, after merging live prices into the catalog, reject and do not cache results where `sum(s.price_per_hour_usd for s in skus) == 0` (all-zero means the price fetch failed silently).
3. Add a CLI-runnable `scripts/validate_pricing_cache.py` that: reads each cache file, prints coverage metrics, and purges invalid entries.
4. Delete `.cache/azure_prices/vm_catalog_eastus2.json` and `vm_catalog_westus3.json` as part of this fix (they are known broken).

**Verification:** After fix, run `scripts/validate_pricing_cache.py` — should show >500 SKUs with non-zero prices for `eastus2`.

---

### Fix C2 — Correct Disk Rate Default
**File:** `engine/azure_sku_matcher.py`  
**Root cause:** `_DEFAULT_GB_RATE = 1.6e-05` is the wrong order of magnitude. Standard SSD E-series (LRS) pricing in East US 2 is ~$0.075/GiB/month.

**Change (single line):**
```python
# Before:
_DEFAULT_GB_RATE = 1.6e-05   # $/GB/month (managed disk — WRONG, was ~4688x too low)

# After:
_DEFAULT_GB_RATE = 0.075     # $/GiB/month (Standard SSD LRS E-series approx, East US)
```

Also clear the stale reference pricing cache entries (`eastus2.json`, `westus3.json`) so they are re-fetched with the correct fallback.

**Verification:** Running `engine/azure_sku_matcher.py` standalone against `eastus2` should return `gb_rate` ≈ 0.075.

---

### Fix C3 — Storage Source: vPartition Capacity (not Consumed)
**Files:** `engine/rvtools_parser.py`, `engine/consumption_builder.py`  

**Root cause:** Two separate bugs compounding:  
(a) `_vm_storage_cost()` uses `partition_consumed_gib` (in-use bytes) as primary — BA wants **provisioned/allocated capacity**  
(b) vPartition MiB→GB uses `MIB_TO_GIB = 1/1024` — BA uses decimal GB (`/953.67`)  

**Changes in `rvtools_parser.py`:**
1. Add `partition_capacity_gib: float = 0.0` to `VMRecord` (alongside existing `partition_consumed_gib`).
2. In the vPartition parsing block, look for column `"Capacity MiB"` (in addition to `"Consumed MiB"`) and populate `vm.partition_capacity_gib = capacity_mib / 953.67`.
3. Add `total_partition_capacity_gb` to `RVToolsInventory` (sum of all powered-on VMs' `partition_capacity_gib`).
4. Keep the existing `partition_consumed_gib` and `total_storage_in_use_gb` fields — they are used in other contexts.

**Changes in `engine/consumption_builder.py`:**  
In `_vm_storage_cost()`, change the priority order:
```
1. vm.partition_capacity_gib > 0   ← NEW primary (BA: provisioned capacity)
2. vm.disk_sizes_gib (vDisk)       ← unchanged fallback
3. vm.provisioned_gib (vInfo)      ← unchanged last resort
(Remove the vm.partition_consumed_gib path from this function — it is not what BA uses)
(Remove the vm.inuse_gib path — also not what BA uses)
```

Note: `partition_consumed_gib` is still useful for status quo "in-use" storage reporting — just not for Azure sizing.

**Expected result:** Azure storage sizing ≈ 4,389,810 GB (BA value).

---

### Fix H1 — Include Template VMs in TCO Baseline Count
**File:** `engine/rvtools_parser.py`  

**Root cause:** The parser does `continue` on any row where `Template == True`, skipping templates from all counters.

**Change:** Split the template check: templates are excluded from `vm_records` (Azure migration sizing) but included in `num_vms_all` / `total_vcpu_all` (TCO baseline).

```python
# After reading Template column:
is_template = (ci_template is not None and row[ci_template] is True)

# Increment TCO baseline counters for ALL VMs (incl. templates):
num_vms_all += 1
total_vcpu_all += cpu
total_mem_mb_all += mem_mb
# ... etc.

# Only skip adding to vm_records if this is a template:
if is_template:
    continue   # don't rightsize templates → don't add to vm_records
```

This will produce `num_vms = 2,831` matching the BA expectation.

---

### Fix H2 — TCO Baseline Always Uses All-VM Scope
**File:** `engine/rvtools_parser.py`  

**Root cause:** The `include_powered_off` auto-detect logic sets `num_vms = num_vms_on` when vHost is present. But the Excel's `1-Client Variables!D39` = total VMs (all power states, incl. templates).

**Change:** Decouple TCO baseline scope from vHost availability:
- `num_vms`, `total_vcpu`, `total_vmemory_gb` — **always** the all-VM all-powerstate count (post Fix H1: includes templates)
- `num_vms_poweredon`, `total_vcpu_poweredon` — always powered-on only
- Remove the auto-detect logic that conditionally assigns `num_vms_all` vs `num_vms_on` based on vHost

The `include_powered_off` parameter can remain for backwards compatibility but should default to `True` for the TCO baseline fields regardless of vHost presence.

**Rationale:** The Excel status quo hardware sizing uses `D47 = D44 / BenchmarkAssumptions!K12` (pCores from vCPU / ratio) — where `D44` = ALL allocated vCPU. Restricting to powered-on understates the server estate being paid for.

---

### Fix H3 — vPartition MiB to GB Conversion
**File:** `engine/rvtools_parser.py`  

**Root cause:** `MIB_TO_GIB = 1/1024` is used to convert vPartition MiB values, producing binary GiB. The BA uses decimal GB (1 GB = 1,000,000 bytes = 953.67 MiB exactly).

**Change:** When populating `partition_capacity_gib` (from Fix C3) and `partition_consumed_gib`, use `MIB_TO_GB = 1/953.67` not `MIB_TO_GIB`. Rename the field from `_gib` to `_gb` to be accurate.

Note: `disk_sizes_gib` (from vDisk) should remain in GiB (binary) because Azure VM catalog memory/disk specs are in GiB. Only vPartition → on-prem storage GB should use the decimal conversion.

---

### Fix H4 — Exclude Zero-Ratio Hosts from vCPU/Core Average
**File:** `engine/rvtools_parser.py`  

**Root cause:** 6 EPIC-RDB hosts have `vCPUs per Core == 0` (empty value in RVTools export). Including them in the average pulls the ratio from 1.58 to 1.52.

**Change:** In the vHost parsing block, when computing `vcpu_per_core_ratio`:
```python
# Before:
ratio_sum += vcpus_per_core
ratio_count += 1

# After:
if vcpus_per_core > 0:   # exclude hosts with no ratio data
    ratio_sum += vcpus_per_core
    ratio_count += 1
```

**Expected result:** `vcpu_per_core_ratio ≈ 1.58` matching BA.

---

### Fix M1 — Remove vCPU/vMemory Telemetry Path
**File:** `engine/consumption_builder.py` (`resolve_vm_utilisation()`)  

**User requirement:** "Ignore vCPU and vMemory tabs entirely."  
**Root cause:** The VM name obfuscation in UHHS's export (`vm100`...`vm2661`) means `inv.vm_cpu_util` lookup always misses, but the host-proxy path still fires (using host-level CPU util from vHost), causing 641 anomaly VMs.

**Change:** In `resolve_vm_utilisation()`, remove the telemetry branch entirely — always return `util_src = "fallback"` with `BenchmarkConfig.cpu_util_fallback_factor` and `mem_util_fallback_factor`. The fallback is the defensible conservative assumption: `cpu_util = 0.65`, `mem_util = 0.65`.

This also means `RVToolsInventory.vm_cpu_util` and `vm_mem_util` dicts can remain (they are populated in the parser) but will no longer be consulted for rightsizing.

**Impact:** Eliminates the 641 anomaly VMs; Azure vCPU result will be based on provisioned sizing with fallback factors.

---

### Fix M2 — 8-Core Minimum in SKU Matching
**File:** `engine/azure_sku_matcher.py` (`match_sku()`)  

**Change:** Add a `min_vcpu: int = 8` parameter to `match_sku()`:
```python
def match_sku(
    target_vcpu: int,
    target_mem_gib: float,
    catalog: list[VMSku],
    family: str | None = None,
    min_vcpu: int = 8,          # ← new parameter
    ...
) -> VMSku:
    # Filter candidates: must have vcpu >= max(target_vcpu, min_vcpu)
    effective_target_vcpu = max(target_vcpu, min_vcpu)
    candidates = [s for s in catalog if s.vcpu >= effective_target_vcpu and ...]
```

Pass `min_vcpu=8` from `consumption_builder.build_with_validation()` via `BenchmarkConfig` (add `sku_min_vcpu: int = 8` to the config dataclass).

---

### Fix M3 — Like-for-Like Sizing Mode
**Files:** `engine/consumption_builder.py`, `engine/vm_rightsizer.py`  

**Change:** Add `sizing_mode: str = "rightsized"` parameter to `build_with_validation()`:  
- `"rightsized"` (default): current behavior — apply util × headroom factors  
- `"like_for_like"`: use `target_vcpu = vm.vcpu`, `target_mem_gib = vm.memory_mib / 1024` (no reduction)

This allows the Layer 2 wizard to produce both counts side-by-side. Wire the UI toggle in `app/pages/agent_intake.py` Layer 2 panel.

**Expected like-for-like result:** Azure vCPU ≈ 16,318 (matching BA's "provisioned" count).

---

### Fix M4 — Exclude Empty Hosts from Host Count
**File:** `engine/rvtools_parser.py`  

**Change:** After vInfo is parsed, build a set of host names that have at least 1 assigned VM. When populating `num_hosts` from vHost, only count hosts present in that set.
```python
# After vInfo pass:
hosts_with_vms = {vm.host_name for vm in inv.vm_records}  # only powered-on VMs

# In vHost pass:
if host_name in hosts_with_vms:
    num_hosts += 1
```

Alternatively: count hosts where `CPU usage %` > 0 OR at least 1 VM assigned.

**Expected result:** `num_hosts ≈ 242` matching BA.

---

### Fix L1 — Backup Tag Misclassification
**File:** `engine/rvtools_parser.py`  

**Change:** The `_ENV_NONPROD_PATTERN` regex correctly does NOT match "Backups" or "Backup_CLE_ENT01". The `env_tagging_present` flag, however, is set to `True` whenever any VM has any non-empty Environment column value — and the UHHS export uses this column for Avamar backup job labels.

Rename and re-document: rename `env_tagging_present` → `lifecycle_env_tags_present` and only set it to `True` when the environment value matches a known lifecycle keyword (`prod`, `dev`, `test`, `staging`, `uat`, `qa`, etc.) — not any arbitrary non-empty string.

---

## 3. Assessment: Which Functions Should Become Skills or Tools

### Functions that should be **dedicated CLI tools** (`scripts/`)

| Function | Proposed tool | Reason |
|----------|--------------|--------|
| Azure pricing cache inspect + repair | `scripts/validate_pricing_cache.py` | Pricing bugs (C1, C2) are non-obvious; a standalone script that prints coverage metrics and purges zero-price entries gives the BA visibility and a recovery path without re-running the full pipeline |
| Inventory scope audit | `scripts/audit_inventory_scope.py` | Given an RVTools file, print: all-VM count, powered-on count, template count, vPartition capacity total, vDisk total — with the Excel-expected values side-by-side |
| vPartition coverage check | Already partially in `scripts/probe_rvtools.py` | Extend to report vPartition tab presence, Capacity MiB column availability, and per-VM coverage rate |

### Functions that should be **dedicated skills** (SKILL.md entries)

| Domain | Skill entry needed | Reason |
|--------|-------------------|--------|
| Inventory scope definition | Add a "Counting Scope Definitions" section to SKILL.md | The 3-scope rule (all-VM TCO / powered-on provisioned / powered-on in-use) is subtle and recurrently confused |
| Azure pricing validation | Add a "Pricing Cache Health" diagnostic step to SKILL.md | The $0 catalog bug will recur whenever the API returns an empty page; the skill should include how to spot it and what to do |
| Storage source selection | Add a "Storage Source Priority" decision tree to SKILL.md | Per-VM vPartition Capacity → vDisk → vInfo Provisioned ordering must be documented for future contributors |
| Financial model trace | Add "Layer 3 Input → Formula Mapping" to SKILL.md | Maps each Python field to its Excel cell so future formula changes can be cross-checked quickly |

### Functions that should stay as-is (no skill needed)

- `region_guesser.py` — already self-contained and tested  
- `depreciation.py` — complex but well-isolated; the Excel mapping is documented in code comments  
- `retained_costs.py` — mirrors `status_quo.py` structure; same  
- `vm_rightsizer.py` — logic is changing (Fix M1, M2, M3) but the function boundary is correct  

---

## 4. Implementation Plan

### Phase 1 — Critical pricing fixes (immediate, 1–2 hours)
1. **C2**: Fix `_DEFAULT_GB_RATE` (1-line change + cache clear)  
2. **C1**: Add empty-cache guard in `_read_cache()` + delete broken cache files  
3. Verify pricing by running `scripts/validate_pricing_cache.py`  
4. Re-run UHHS file end-to-end — Azure compute and storage costs should now be non-zero

### Phase 2 — Parser inventory fixes (2–4 hours)
5. **H1**: Count template VMs in TCO baseline  
6. **H2**: Decouple TCO scope from vHost presence  
7. **H3 + C3**: Add `partition_capacity_gb` field to VMRecord; fix vPartition MiB→GB conversion; update `_vm_storage_cost()` priority  
8. **H4**: Exclude zero-ratio hosts from vCPU/Core average  
9. **M4**: Exclude empty hosts from `num_hosts`  
10. Run `tests/test_rvtools_to_inputs.py` — update assertions to match new inventory counts  
11. Verify against BA: num_vms=2831, total_vcpu=15330, vcpu_per_core=1.58, partition_capacity≈4.39M GB

### Phase 3 — Consumption builder fixes (2–3 hours)
12. **M1**: Remove telemetry branch in `resolve_vm_utilisation()` — always use fallback  
13. **M2**: Add `min_vcpu=8` floor to `match_sku()` + `BenchmarkConfig.sku_min_vcpu`  
14. **M3**: Add `like_for_like` sizing mode  
15. Update `tests/test_engine.py` for new rightsizing behavior  
16. Verify: Azure vCPU like-for-like ≈ 16,318; rightsized ≈ BA's rightsized count

### Phase 4 — Cleanup and documentation (1 hour)
17. **L1**: Fix `env_tagging_present` → `lifecycle_env_tags_present`  
18. Create `scripts/validate_pricing_cache.py`  
19. Update `SKILL.md` with all 4 new sections listed above  
20. Final end-to-end run on UHHS file; compare financial outputs to BA comparison doc

---

## 5. Expected Outcomes After All Fixes

| Metric | BA Expected | Before Fixes | After Fixes |
|--------|------------|-------------|-------------|
| Total VMs | 2,831 | 2,661 | ~2,831 |
| Total vCPU (TCO) | 15,330 | 14,628 | ~15,330 |
| ESX Hosts | 242 | 280 | ~242 |
| vCPU/Core ratio | 1.58 | 1.52 | ~1.58 |
| Azure storage GB | 4,389,810 | 3,081,602 | ~4,389,810 |
| Azure vCPU (like-for-like) | 16,318 | 11,872 | ~16,318 |
| Compute PayG annual | ~$6.7M | ~$2.6M* | ~$6.7M |
| Storage cost annual | ~$4.1M | ~$3.6M* | ~$4.1M |

*App figures from BA comparison doc. Compute was near-$0 internally before C1/C2 fix; the $2.6M figure may already incorporate some partial correction.

---

## 6. What the Financial Model Does NOT Need

After the Excel audit, the Python `financial_case.py` and `outputs.py` are **structurally correct**:

- NPV formula: `Σ CF_yr / (1+WACC)^yr` — matches `Detailed Financial Case` row 94  
- SQ costs: depreciation from `DepreciationSchedule`, OPEX from `StatusQuoCosts` with `(1+g)` annual growth — matches Excel rows 8–35  
- Azure consumption: `avg_ramp_yr × Y10_rate × (1+g)` — matches `2a-Consumption Plan Wk1` rows 28–29  
- Terminal value: Gordon Growth Model — matches Excel row 93  
- The ROI/payback from `compute_cf_roi_and_payback()` replicates `5Y CF with Payback` sheet  

**No structural changes needed in Layer 3.** Once the input data is corrected (Phases 1–3), the financial outputs will align with the BA's Excel results.

---

## 7. Risks and Notes

- **Cache TTL**: After clearing the cache in Phase 1, the app will call the Azure Retail Prices API on first run. If the environment lacks internet access (CI/CD containers), the fallback `_DEFAULT_GB_RATE = 0.075` will be used — which is correct.
- **Test assertions**: Several existing tests hard-code inventory counts derived from the broken parser. These will fail after Phase 2 changes and need to be updated — this is expected and not a regression.
- **vPartition tab availability**: Not all RVTools exports include vPartition. Fix C3 must gracefully fall back to vDisk when the tab is absent. The existing fallback chain in `_vm_storage_cost()` handles this correctly after the priority reorder.
- **3-scenario data model**: The valid scenarios remain: (1) vInfo only, (2) vInfo + vHost, (3) vInfo + vHost + vPartition. Fix H2 changes the default behavior within scenario 2 (no longer restricts TCO to powered-on only).
