# BV Benchmark Business Case — Version History

All versions correspond to Git commits on the `main` branch.  
Dates are commit dates (Pacific Time). Test counts reflect the state at each commit.

---

## v1.5.0 — Layer 3 BA Parity: ZERO ENGINE DRIFT
**Commits:** `552b106` … `c544441` | **Date:** 2026-05-04 | **Tests:** 29 layer3 parity ✅ + 31 privacy/parity gate ✅

### What changed

The engine now **matches the BA workbook exactly** across all 395 Layer 3 oracle
keys for the Customer A reference workbook. **`MAX_ENGINE_DRIFT = 0`** is locked
into `tests/test_layer3_parity.py` — any future regression is a hard CI fail.

**Parity ratchet (drift = number of oracle keys outside per-tier tolerance):**

| Step | Commit | Drift | Fix |
|------|--------|-------|-----|
| 12.1 | `552b106` | 212 → 210 | Bridge wiring fixes |
| 12.2 | `869d344` | 210 → 117 | Engine pmem additive-channel + bridge totals |
| 12.3a | `31e17f3` | 117 → 111 | AZ CAPEX Y0 + static baseline |
| 12.3b | `ee4ed86` | 111 → 85 | SQ Depreciation rolling-avg over `depr_life` |
| 12.3c | `eb5b1f2` | 85 → 55 | Retained IT admin + Win/SQL license multipliers (per-year integer arithmetic + 10-nines epsilon) |
| 12.3d | `7c8a112` | 55 → 37 | pCore counts `int → float` (BA hand-types fractional values via `=12405/1.48`) |
| 12.3e | `191fa00` | 37 → 21 | CF Rate sign + `Savings = max(0, …)` + `y10_savings_5y_cf` separate from Y10 |
| 12.3f | `6f4e196` | 21 → 14 | Migration cost counts `num_vms` only (not topology residual `num_physical_servers_excl_hosts`) |
| 12.3g | `4537b35` | 14 → 6 | `five_payback.*_npv` undiscounted Y1..Y5 sums (column H semantics); engine `compute_cf_roi_and_payback` switched from P&L → CF accessors (UI now shows BA-truth ROI -47.03% instead of buggy -55.58%) |
| **12.3h** | **`c544441`** | **6 → 0** | **`num_physical_servers_excl_hosts: int → float`** preserves the fractional topology residual `D42 − num_vms / vm_to_server_ratio` (e.g. Customer A: `280 − 2831/12 = 44.0833`). Eliminates the last 1/12-host shortfall propagating to NW+Fitout depreciation Y5..Y10. |

### Architecture: 3-way auditor

`training/replicas/layer3_judge.py` compares **BA workbook** ↔ **replica oracle** ↔
**engine bridge** with tiered absolute tolerance (`<$1: $0.005`, `<$100: $0.01`,
`<$10K: $1`, `<$1M: 0.1%`, `≥$1M: 1%`). The replica stays at 395/395 CLEAN
throughout; the engine ratchet only ever decreases.

### Layer 3 invariants (NEVER REGRESS)

- `fc.sq_total()` / `fc.az_total()` = **P&L** (depreciation + opex)
- `fc.sq_total_cf()` / `fc.az_total_cf()` = **CASH FLOW** (capex + opex)
- BA's "5Y CF with Payback" sheet is **CASH FLOW**. `compute_cf_roi_and_payback`
  must use `_cf` accessors.
- `five_payback.net_benefits_npv` = **DISCOUNTED** `npv_sq_5y − npv_az_5y`; the
  other 6 `five_payback.*_npv` labels are **UNDISCOUNTED** raw Y1..Y5 sums (BA's
  column H semantics).
- BA hand-types fractional values via Excel formulas. Engine fields that mirror
  BA cells (`pcores_*`, `num_physical_servers_excl_hosts`) **must be `float`**.

### UI impact

`summary.roi_cf` (rendered in `app/pages/agent_intake.py`, `app/pages/results.py`)
now displays the BA-truth value `-47.03%` instead of the buggy `-55.58%`. This
is a **correction**, not a regression — pre-existing tests already expected
`-0.4703`.

### Validation

- **Replica**: 395/395 CLEAN (no regression across 12.1 → 12.3h)
- **Engine drift**: 0 (was 212 at start of v1.5.0 cycle)
- **Tests**: 90 pass / 10 pre-existing unrelated / 23 skip; 29 layer3 parity tests pass
- **Methodology**: every fix gated by adversarial Explore review BEFORE apply,
  independent 3-way audit AFTER apply, ratchet tightening at each commit.

---

## v1.4.0 — Layer 1 BA Parity (training corpus + engine convergence)
**Date:** 2026-04-27 | **Tests:** 14 new parity tests ✅ + existing suite unchanged

### What changed

Established a **BA-trained ground-truth pipeline** for Layer 1 (RVTools ingest)
and converged the engine to match it. The training corpus, replica oracle, and
parity harness now form an executable specification of the human BA's workflow.

**New: `training/` corpus (Phase 0–2, Layer 1)**
- `training/baseline_workflow/layer{1,2,3}_*/` — verbatim BA training
  transcripts (.vtt + .docx) and recording hint files.
- `training/ba_rules/layer1.yaml` — 22 BA-reviewed rules + 6 cross-cutting
  key principles (KP.PER_VM, KP.MIB_DEFAULT, KP.SYNONYM_HEADERS,
  KP.MANDATORY_VINFO, KP.PROVISIONED_FOR_TCO, KP.BA_APPROVAL_GATE).
- `training/replicas/layer1_ba_replica.py` — engine-independent oracle
  implementing the rule book; exposes `replicate_layer1(path) -> Layer1Result`
  with per-VM authoritative payload + BA review packet.
- `training/parity/run_layer1_parity.py` — three-way diff (replica vs BA vs
  engine) with per-field tolerance and Markdown report output.
- `training/baselines/customer_a_2024_10/` — Customer A reference: BA-expected values,
  replica outputs, full per-VM dump, parity report.

**Engine fixes (drove engine to 7/7 parity at 0.00% delta on Customer A)**
- **L1.STORAGE_PROV.001 (DELTA → MATCH):** New `RVToolsInventory.total_storage_provisioned_gb`
  + `storage_provisioned_source` ("vpartition" | "vinfo"). Aggregation now
  follows BA source preference: vPartition Capacity (sum across ALL rows,
  regardless of powerstate, for the canonical TCO sum) → vInfo Provisioned
  per-VM fallback when vPartition is absent. Customer A: 4,389,810 (BA) vs 4,389,831
  (engine) = 0.00%.
- **L1.HOST.002 (DELTA → MATCH):** Reverted v1.3.0 H3's `_hosts_with_vms`
  cross-match. Per BA: count ALL vHost rows (the customer paid for every
  host listed; out-of-scope filtering is the customer/BA's responsibility
  upstream of the file). Customer A: 280 (BA) vs 280 (engine) = 0.00%.

**Tests**
- `tests/test_layer1_parity.py` — 14 parametrized parity assertions (replica
  vs BA + engine vs BA) on 7 Layer 1 fields. CI-skips when Customer A sample is
  unavailable; otherwise hard-fails on >tolerance delta.
- Pre-existing failures in `TestRVToolsParser` (missing fixture file) and
  `TestConsumptionBuilderStorage` (3 storage-priority tests) are unchanged
  by this work — verified by `git stash` + re-run.

**Per-VM directive (KP.PER_VM)**
The rule book now formalises that all business-case math is per-VM and
summed; fleet aggregates exist only as FYI for the BA. Replica enforces
this end-to-end. Engine convergence in Layer 2/3 will follow the same rule.

---

## v1.3.1 — Refactor: Fact Checker — Pipeline Health Checks
**Commits:** `532c68b`, `19e28bb` | **Date:** 2026-04-16

### What changed

Refactored the fact-checker subsystem to catch the exact parser/pricing bugs (C1–C3, H4, M1) that produced incorrect outputs in prior Customer A analysis — bugs the existing Excel cross-check could not detect because they affected inputs, not the financial model math.

**`engine/fact_checker.py`:**
- New `_check_pipeline_plausibility(inputs)` — catches: `annual_compute_consumption_lc_y10 = 0` (broken pricing cache, C1), implied storage rate outside `$0.01–$0.50/GB/mo` (wrong `gb_rate`, C2), `azure_storage_gb = 0` with on-prem storage present (wrong storage source, C3), `num_vms = 0` or `allocated_vcpu = 0` (silent parse failure), `vcpu_per_core` outside `[1.0–8.0]` (zero-vCPU hosts, H4), `azure_vcpu > 2× on-prem vCPU` (host-proxy anomaly, M1), compute cost/vCPU outside `$150–$8k/yr`.
- `FactCheckReport` gains `pipeline_warnings: list[str]` field; each warning deducts 10% from confidence score (max 50% penalty).
- `_compare_inputs()` now also cross-checks `ConsumptionPlan.azure_vcpu`, `azure_memory_gb`, `azure_storage_gb` against Excel `2a-Consumption Plan Wk1` cells D8–D10.
- `passed_overall` now requires zero pipeline warnings in addition to zero FAIL checks.

**`app/pages/fact_checker_page.py`:**
- New **"🚦 Pipeline Health"** tab (first tab) — 12 targeted checks covering all Customer A failure modes; shows red banner with remediation instructions when any check fails; raw metric tiles for VMs, vCPU, Azure vCPU, storage GB, compute cost/yr, storage cost/yr.
- Excel Cross-Check tab now also surfaces pipeline warnings above the check table.
- Tab order: 🚦 Pipeline Health → 🧮 Engine Sanity → 📋 Excel Cross-Check.

---

## v1.3.0 — Fix: 12 Parser/Pricing/Scope Bugs (C1–C3, H1–H4, M1–M4, L1)
**Commit:** `532c68b` | **Date:** 2026-04-16

### What changed

Resolved 12 bugs identified in post-engagement Customer A analysis. Full details in `FIX_PROPOSAL.md`.

**Critical (C-series):**
- **C1 — Broken pricing cache guard** (`azure_sku_matcher.py`): empty/all-zero cache files were silently read, producing `$0` compute costs. Added guard in `_read_vm_price_cache` / `_read_cache` to reject empty or all-zero caches.
- **C2 — Wrong storage GB rate** (`azure_sku_matcher.py`): `_DEFAULT_GB_RATE` was `0.00` (bug), set to `0.075` (`$0.075/GB/mo` — Azure P10 managed disk list rate).
- **C3 — Wrong storage source priority** (`consumption_builder.py`): `_vm_storage_cost()` now uses vPartition Capacity MiB first → vDisk provisioned GB second → vInfo last resort. Removed the in-use/consumed paths that undercounted storage by 60–80%.

**High (H-series):**
- **H1 — Template VMs included in TCO scope** (`rvtools_parser.py`): `is_template` detection moved before powered-on/off accumulators; all-VM counters run for templates too but template records are skipped from `vm_records`.
- **H2 — Scope applied conditionally** (`rvtools_parser.py`): `include_powered_off` scope is always applied (`include_powered_off_applied = True`); all VMs (powered-on + powered-off) always count toward TCO scope.
- **H3 — vHost loop counted hosts without powered-on VMs** (`rvtools_parser.py`): `num_hosts` only increments for hosts that have at least one powered-on VM.
- **H4 — env_tagging_present was always False** (`rvtools_parser.py`): renamed to `lifecycle_env_tags_present`; now uses regex pattern `_LIFECYCLE_ENV_PATTERN` to detect prod/dev/test/uat/qa/staging tags.

**Medium (M-series):**
- **M1 — vm_rightsizer used broken host-proxy path** (`vm_rightsizer.py`): `resolve_vm_utilisation()` simplified to always return `(0.0, 0.0, "fallback")` — removes unreliable telemetry and host-proxy paths.
- **M2 — `azure_sku_matcher` min vCPU floor** (`azure_sku_matcher.py`): `min_vcpu = 8` floor applied in `match_sku()`; absolute fallback is `Standard_D8s_v5`.
- **M3 — vPartition Capacity MiB added** (`rvtools_parser.py`): `VMRecord.partition_capacity_gb` and `RVToolsInventory.total_partition_capacity_gb` populated from vPartition `Capacity MiB`.
- **M4 — like-for-like sizing mode** (`consumption_builder.py`, `agent_intake.py`): new `sizing_mode` parameter; `"like_for_like"` bypasses rightsizing to match on-prem vCPU/memory directly.

**Low (L-series):**
- **L1 — env_tagging field rename** (`rvtools_to_inputs.py`, `tests/`): `inv.env_tagging_present` → `inv.lifecycle_env_tags_present`.

**New scripts:** `scripts/validate_pricing_cache.py`, `scripts/audit_inventory_scope.py`.

---

## v1.2.3 — Fix: Source-Size Ceiling for Rightsizing
**Commit:** `eb60743` | **Date:** 2026-04-08 | **Tests:** 34 ✅ (57 skip/pre-existing fixture)

### What changed

**Root cause fixed:** high-utilisation VMs (running at ≥ ~83%) were producing rightsized targets **above** the source VM's own allocation after the 20% headroom factor was applied. Azure SKU matching then snapped up to the next tier (e.g. 9 vCPU target → D16s = 16 vCPU), compounding across large fleets to produce 2–3× the on-prem vCPU and memory totals.

**Fix — `engine/vm_rightsizer.py`:**
- `rightsize_vm()` now caps both `target_vcpu` and `target_mem_gib` at the source VM's own allocation: `target = min(source, ceil(source × util × (1 + headroom)))`.
- Rationale: this tool sizes for *migration*, not upgrades. A VM at capacity already has the right size; SKU-tier rounding handles any remaining gap.
- Fallback-mode targets (no telemetry) are always below source by construction (fallback factors < 1.0) — unaffected.

**UI — `app/pages/agent_intake.py`:**
- Layer 2 headroom caption updated: documents the source-size ceiling so users understand why `Azure vCPUs ≤ On-Prem vCPUs` is now guaranteed.

**Docs — `GETTING_STARTED.md`, `README.md`:**
- Layer 2 section updated with source-size ceiling explanation.
- Right-sizing formula table updated: `min(vCPU, ceil(...))` notation.

---

## v1.2.2 — Feat: Asymmetric SKU Matching (Layer 2)
**Commit:** `05dc7ce` | **Date:** 2026-04-08 | **Tests:** 34 ✅

### What changed

Implements the manual Xa2 analysis methodology as an automated **3-pass cascade** in `match_sku()`, replacing the naive strict both-dimensions approach that forced simultaneous vCPU + memory snap-ups between Azure SKU tiers.

**`engine/azure_sku_matcher.py` — `match_sku()`:**
- New `secondary_tolerance` parameter (default 0.20).
- **Pass 1 — Relaxed secondary:** classifies VM as CPU-skewed (mem/vCPU < 5 GiB) or memory-skewed (mem/vCPU ≥ 5 GiB). Primary dimension must be fully covered; secondary dimension may be up to `tolerance%` below target. Mirrors the "try slightly lower first" manual Xa2 step.
- **Pass 2 — Strict both dimensions:** original behaviour unchanged.
- Final result: cheapest across Pass 1 and Pass 2 — can only reduce or hold cost, never inflate.

**`engine/models.py`:** `BenchmarkConfig.sku_match_secondary_tolerance = 0.20` (new field, fully documented).

**`engine/consumption_builder.py`:** passes `pb.sku_match_secondary_tolerance` to `match_sku()`.

**`app/pages/agent_intake.py`:**
- New **"SKU match tolerance %"** slider in Layer 2 overrides (0–35%, default 20%).
- Active tolerance shown in results caption (e.g. `SKU tolerance: 20%`).
- Full plain-English methodology explanation in the override panel.

**`GETTING_STARTED.md`, `README.md`:** Layer 2 section updated with the 3-pass methodology description.

---

## v1.2.1 — Feat: No-Signal VM Tagging in Layer 1
**Commits:** `81ce248`, `1307846` | **Date:** 2026-04-08 | **Tests:** 34 ✅

### What changed

**`engine/rvtools_parser.py` — `VMRecord`:**
- New field `azure_region_source: str` — populated alongside `azure_region` with the signal that produced the region: `"tld"` | `"dc_keyword"` | `"gmt"` | `"fallback"` | `"override"`.

**`engine/region_guesser.py` — `guess_for_host()`:**
- Now returns `(region, source)` tuple instead of plain `str`.

**`app/pages/agent_intake.py` — Layer 1 inventory view:**
- Multi-region expander table gains a **"Signal breakdown"** column per region (e.g. `"42 TLD, 8 DC keyword, 15 no signal ⚠"`).
- Single-region case: if all VMs fell to fallback, a warning banner explains the situation and prompts the user to use the region override.
- Region override now also sets `azure_region_source = "override"` on each VMRecord.

---

## v1.2.0 — Feat: Per-VM Azure Region Inference for Merged RVtools Exports
**Commit:** `81ce248` | **Date:** 2026-04-08 | **Tests:** 34 ✅

### What changed

Previously the engine inferred a single fleet-level Azure region and applied it to all VMs. This was incorrect for merged RVtools exports (multiple datacenters/countries combined into one file).

**`engine/region_guesser.py`:**
- New `guess_for_host(host_fqdn, datacenter, domain, gmt_offset, fallback)` function — returns `(region, source)` using the same priority order as the fleet-level `guess()` but operating on a single host's signals.
- UTC (offset 0) removed from `gmt_offset_to_region` — not a geographic signal; was incorrectly mapping UTC-configured servers to `uksouth`.

**`engine/rvtools_parser.py`:**
- vHost loop now builds `host_region_signals: dict[str, dict]` mapping each host FQDN to `{dc, domain, gmt}`.
- `RVToolsInventory.host_region_signals` field stores the per-host signals.
- `VMRecord.azure_region` and `VMRecord.azure_region_source` stamped after vHost parse using `guess_for_host()` per VM.

**`engine/consumption_builder.py`:**
- Per-VM loop now reads `vm.azure_region` and lazily fetches pricing + VM catalog per distinct region (24h disk-cached). VMs from different hosts are priced against their own Azure region.

**`app/pages/agent_intake.py` — Layer 1:**
- Region override overwrites all VMRecord `azure_region` fields.
- Multi-region expander shows per-region VM count + signal breakdown for user review before approving Layer 1.
- Layer 2 results "Pricing source" metric shows `"N regions (per-VM)"` when applicable.

**`data/region_map.yaml`:**
- UTC offset 0 → `uksouth` removed (not a geographic signal).
- `fallback_region` changed `eastus` → `eastus2`.

**`app/pages/intake.py`:** fallback default updated to `eastus2`.

---

## v1.1.1 — Streamlit Cloud Deployment
**Commits:** `2df5a81`, `e7769cb` | **Date:** 2026-04-03 | **Tests:** 64 ✅

### What changed

**Streamlit Cloud readiness (`2df5a81`):**
- `requirements.txt`: removed `pytest` (dev-only dep, adds ~30s to Cloud builds)
- `requirements-dev.txt`: new file — `pytest>=8.0` only; used for local test runs
- `packages.txt`: `libexpat1` for Streamlit Cloud Ubuntu runner
- `.streamlit/config.toml`: dark theme, 100 MB upload limit, `gatherUsageStats = false`
- `.gitignore`: added `.cache/` (Azure pricing JSON cache, Cloud fetches fresh); `.streamlit/secrets.toml` excluded, `config.toml` tracked
- `Dockerfile`: `python:3.11-slim`, EXPOSE 8501, healthcheck at `/app/main.py`
- `docker-compose.yml`: single `bv-bc` service, port 8501, project root volume-mounted

**Fix: `packages.txt` comments caused fatal build error (`e7769cb`):**  
`apt-get install` parses every non-blank line as a package name — including `#` comment lines. The error `E: Unable to locate package #` caused Streamlit Cloud builds to fail. Removed all comment lines; `packages.txt` now contains only `libexpat1`.

The app is now live at **[bv-benchmark-bizcase.streamlit.app](https://bv-benchmark-bizcase.streamlit.app)**.

---

## v1.1.0 — UI: 3-Layer Checkpoint Wizard
**Commit:** `2ea5c64` | **Date:** 2026-04-03 | **Tests:** 64 ✅

### What changed

Complete rewrite of `app/pages/agent_intake.py` (925 lines). The Agent Intake page is now a sequential **3-layer checkpoint wizard**. The engine stops after each layer, displays the full results, and waits for the user's explicit approval before proceeding. No work is lost if a layer is revised.

#### Architecture

Session state machine keyed on `_wiz_step` (0 = Upload → 4 = Export):

```
Upload(0) → Inventory(1) → Rightsizing(2) → Financial(3) → Export(4)
```

Visual step bar with ✅ / 🔵 / ○ badges. Approved layers collapse to compact green banners with a **← Revise** button.

#### Layer 1 — Inventory checkpoint
- Parse RVTools export, infer Azure region, fetch live PAYG pricing
- Shows fleet summary, OS/SQL profile, pricing source
- **Override panel** (collapsed by default): force region, rename client, change currency → triggers L1 re-run
- **Pre-flight** before approving: migration horizon (5/10 yr), storage mode (per-VM / fleet aggregate)

#### Layer 2 — Rightsizing checkpoint
- Runs per-VM rightsizing using P95 telemetry (with `RightsizingValidation` checkpoint)
- Shows telemetry coverage %, vCPU/memory delta vs on-prem, anomaly list (VMs where matched SKU > 2× source vCPU)
- **Override panel**: CPU headroom % slider, memory headroom % slider, fallback %, storage mode toggle → triggers L2 re-run

#### Layer 3 — Financial model checkpoint
- Runs full financial model (P&L, CF, NPV, ROI, payback, NII, productivity)
- Shows headline KPIs (NPV, ROI, payback, cost/VM/yr), cost comparison chart with CF savings annotation, engine sanity checks
- **Override panel**: WACC slider, DC exit count, horizon, ACO/ECIF credits → triggers L3 re-run
- **Scenario comparison**: Add named alternative L3 scenarios side-by-side (KPI table + chart lines)

#### Layer 4 — Export
- Download PowerPoint (dark-theme deck) or pre-filled Excel (Template v6 yellow cells)

#### Key session state keys
`_wiz_step`, `_wiz_file_bytes`, `_l1_result`, `_l2_result`, `_l3_result`, `_l3_scenarios`, `_agent_horizon`, `_agent_summary`, `_agent_client_name`, `_agent_currency`

#### Supporting engine changes (part of `2ea5c64`)
- `agent_intake.py`: ROI/payback display updated to `roi_cf` / `payback_cf` (CF-based methodology)
- `results.py`: Exec summary + presentation tabs updated to `roi_cf` / `payback_cf`

---

## v1.0.0 — Engine: 3-Layer Architecture + Validation Checkpoint
**Commit:** `aa77156` | **Date:** 2026-04-03 | **Tests:** 64 ✅

### What changed

**Layer 2 — Rightsizing: utilisation cap + `RightsizingValidation`**

- `vm_rightsizer.py`: cap CPU/memory utilisation at `_UTIL_CAP = 0.95` before applying headroom.  
  Prevents Azure vCPU inflation from VMware ballooning artefacts where `Consumed/Size > 1`.
- `consumption_builder.py`:
  - New `RightsizingValidation` dataclass (L1→L2 checkpoint): telemetry coverage %, anomaly detection (matched vCPU > 2× source), `vcpu_increased` / `memory_increased` flags
  - `build()` retained as thin wrapper; `build_with_validation()` returns `(ConsumptionPlan, RightsizingValidation)`
  - `_vm_storage_cost()`: flip storage priority to **in-use first** — `partition_consumed → inuse → disk_sizes×reduction → provisioned×reduction`
  - Tracks `telemetry_count / host_proxy_count / fallback_count` per VM

**Layer 3 — Financial Model: CF ROI/payback as primary output**

- `outputs.py`: `compute_cf_roi_and_payback()` moved from `fact_checker.py` to `outputs.py` (public); `BusinessCaseSummary` gains `roi_cf` and `payback_cf` fields; `outputs.compute()` populates both at engine run time
- `fact_checker.py`: imports `compute_cf_roi_and_payback` from `outputs`; backward-compat alias retained

**Parser: `source_type` field**

- `rvtools_parser.py`: `RVToolsInventory` gains `source_type = 'rvtools'`
- `engine/parsers/__init__.py`: new `InventoryParser` Protocol (source-agnostic interface for future parsers)

### New tests (+6, total 64)
- `TestVMRightsizer`: util cap prevents inflation; fallback stays ≤ source vCPU
- `TestConsumptionBuilderStorage`: storage priority order — partition_consumed > inuse > disk_sizes > provisioned
- `TestFactCheckerCFMetrics`: backward-compat alias confirmed

---

## v0.9.0 — Phase 3: Fact Checker Rearchitecture
**Commit:** `b2f1598` | **Date:** 2026-04-02 | **Tests:** 58 ✅

### What changed

Three systematic fixes in `engine/fact_checker.py`:

**1. npv_azure_5yr was never compared**  
`SFC!D7` (NPV of Azure costs, 5-year) was listed in `SUMMARY_OUTPUT_CELLS` but omitted from `engine_vals` — it produced a `SKIP` result every run and contributed nothing to the confidence score. Now included as `_npv_series(az_total, 5)` and added to `SEVERITY_CONFIG` with matching weight/threshold parity to `npv_sq_5yr`.

**2. ROI and Payback used wrong methodology**  
Template cells `E6` and `E11` reference `'5Y CF with Payback'!I31` and `I32`, which use a **5-year discounted CF** methodology: it measures how quickly ongoing P&L savings recover the one-time migration investment NPV. The engine previously compared these against `summary.roi_10yr` (a 10-year P&L multiple on total Azure NPV) and `summary.payback_years` (undiscounted cumulative break-even) — fundamentally different ratios that diverge even when all inputs are identical, causing systematic false-FAIL.

New `_compute_cf_roi_and_payback()` replicates the Template formula exactly:
- `investment_npv` = NPV of `az_migration_cf()` over Y1–Y5
- `run_savings[yr]` = `sq_total()[yr] − (az_total()[yr] − mig[yr])`
- Cumulative discounted run savings through Y5
- `ROI = (cum_Y5 − investment) / investment` (mirrors `−(G46+C40)/C40`)
- `payback` = interpolated year when cumulative ≥ investment (mirrors `I32`)

**3. SEVERITY_CONFIG phantom keys removed**  
Five entries with no matching `engine_vals` keys (`npv_total_benefits`, `npv_total_costs`, `npv_infra_savings`, `npv_admin_savings`, `investment_npv`) were assigning weight to metrics that were never evaluated. This capped the maximum achievable confidence score at ~65% even with all real checks passing. Replaced with real weights for all 9 checked output cells, now summing to exactly 1.00.

### New confidence score weight allocation

| Metric | Weight |
|---|---|
| Project NPV (10-yr) | 25% |
| Payback period (5Y CF) | 20% |
| ROI (5Y CF) | 15% |
| Terminal value | 10% |
| NPV SQ (10-yr) | 8% |
| NPV Azure (10-yr) | 8% |
| Project NPV excl. TV | 6% |
| NPV SQ (5-yr) | 4% |
| NPV Azure (5-yr) | 4% |

### New tests
`TestFactCheckerCFMetrics` (5 tests): positive ROI, bounded payback (≤5yr), zero-migration guard, 5-year window, sign consistency with P&L payback.

---

## v0.8.0 — Phase 2b: Template Formula Audit
**Commit:** `4af09f7` | **Date:** 2026-04-02 | **Tests:** 53 ✅

### What changed

Three discrepancies found by auditing `Template_BV Benchmark Business Case v6.xlsm` formulas with openpyxl against the Python engine logic:

**1. `engine/retained_costs.py` — Retained hardware maintenance grew incorrectly**  
- **Bug:** `status_quo[yr] × (1 − ramp)` — since `status_quo[yr]` already compounds at `(1+g)^yr`, maintenance on the un-migrated fraction grew each year as if hardware was being refreshed.
- **Fix:** `status_quo[0] × (1 − ramp)` — uses Y0 static baseline, because on the Azure migration track hardware is not refreshed; only the retained fraction declines.
- **Template reference:** Depreciation Schedule row M41: `$L41 × (1 − M30)` where L41 is a static baseline.

**2. `engine/financial_case.py` — M12 hardware renewal factor was never applied**  
- **Bug:** `hardware_renewal_during_migration_pct` (M12, default 10%) was stored in the Pydantic model and exported to the Template yellow cell D27, but never used in the compute step — retained CAPEX and depreciation were calculated at 100% of their normal rate.
- **Fix:** Added `m12 = inputs.hardware.hardware_renewal_during_migration_pct`; multiplied all `az_*_acquisition` and `az_*_depreciation` lines by `m12`.
- **Template reference:** Depreciation Schedule row M31: `M8 × (1 − ramp) × M12`.

**3. `engine/status_quo.py` — Sysadmin headcount used wrong rounding and base**  
- **Bug:** `math.ceil(Y0_VMs / ratio)` as a fixed headcount, then applied `growth_rate` to the cost separately — this used CEIL (not ROUND), froze the headcount at Y0, and applied two separate growth multipliers.
- **Fix:** `round(grown_vms_yr / ratio)` per year, where `grown_vms_yr = total_vms × (1 + g)^yr`. Cost = headcount × compensation, no extra growth multiplier.
- **Template reference:** Status Quo Estimation row 147: `ROUND(grown_VMs / vms_per_admin, 0)` × compensation, where VMs include annual growth.

---

## v0.7.1 — Phase 2b: Benchmarks UI Wiring Fix
**Commit:** `8573aa7` | **Date:** 2026-04-02 | **Tests:** 53 ✅

### What changed

Benchmarks page Right-Sizing Parameters expander updated to match renamed/restructured fields from Phase 2a:

- **Added (with telemetry):** Utilisation Percentile, CPU Headroom (`cpu_rightsizing_headroom_factor`), Memory Headroom (`memory_rightsizing_headroom_factor`)
- **Added (fallback tier):** CPU retain factor (`cpu_util_fallback_factor`, 40%), Memory retain factor (`mem_util_fallback_factor`, 60%), Storage reduction vs Provisioned (`storage_prov_reduction_factor`, 20%)
- **Removed stale references:** `cpu_rightsizing_fallback_reduction`, `memory_rightsizing_fallback_reduction`, `storage_rightsizing_headroom_factor` (renamed in Phase 2a)

---

## v0.7.0 — Phase 2a: Per-VM Rightsizing Engine
**Commit:** `3468f21` | **Date:** 2026-04-02 | **Tests:** 53 ✅

### What changed

Complete rewrite of the rightsizing pipeline from fleet-aggregate to per-VM SKU matching.

**Parser (`engine/rvtools_parser.py`):**
- New `VMRecord` dataclass: per-VM fields (vcpu, memory_mib, host_name, OS, disk_sizes_gib, cpu_util, mem_util)
- `vm_records[]` accumulated for all powered-on non-template VMs during vInfo pass
- vCPU/vMemory tabs build per-VM utilisation maps in addition to fleet P95
- vHost: captures CPU % and Memory % usage per host for proxy utilisation when per-VM data absent
- vPartition: new block builds `vm_partition_consumed_gib` per VM
- vDisk: populates `VMRecord.disk_sizes_gib` in GiB (MiB ÷ 1024, not ÷ 953.67) — GiB accuracy fix
- `MIB_TO_GIB = 1/1024` constant added; `MIB_TO_GB` retained for TCO summary fields only

**New: `engine/vm_rightsizer.py`:**
- `resolve_vm_utilisation()`: 3-tier fallback (per-VM telemetry → host proxy utilisation → benchmark fallback)
- `rightsize_vm()`: per-VM `(target_vcpu, target_mem_gib)` with headroom
- `select_family()`: D/E/F/M family selection based on memory density + workload keywords (SQL/HPC/SAP/Oracle)

**New: `data/azure_vm_catalog.json`:**
- Static specs for D2s_v5–D96s_v5, E2s_v5–E96s_v5 (including AMD variants), F2s_v2–F72s_v2, M8ms–M416ms_v2
- Live prices per SKU fetched from Azure Retail Prices API per region; cached 24h

**`engine/azure_sku_matcher.py`:**
- New `VMSku` dataclass
- `get_vm_catalog(region)`: loads catalog + live Linux PAYG prices per family
- `match_sku()`: least-cost fitting with family fallback order (D→E→F→M)

**`engine/disk_tier_map.py`:**
- Premium SSD v2 flat per-GiB rate added
- `assign_cheapest()`: returns min(P-tier, Pv2 raw) per disk
- `vm_annual_storage_cost_usd()`: per-VM storage cost using cheapest tier assignment

**`engine/consumption_builder.py`:** Full rewrite — per-VM loop over `inv.vm_records`; fleet-aggregate path retained as fallback for no-VM-records edge case.

**`data/benchmarks_default.yaml` + `models.py`:** Old fleet-fallback fields replaced with `cpu_util_fallback_factor=0.40`, `mem_util_fallback_factor=0.60`, `storage_prov_reduction_factor=0.20`; E/M series memory thresholds added.

---

## v0.6.0 — Phase 1: vCPU/pCore Ratio Fix + Azure Retail API Attribution
**Commit:** `aa0c1fd` | **Date:** 2026-04-02 | **Tests:** 53 ✅

### What changed

**vCPU/pCore ratio default corrected (7.0 → 1.97):**  
The engine default was 7.0 — a stale placeholder. Template Benchmark Assumptions col K uses 1.97 (derived from the vHost tab's per-host vCPUs/Core column). The parser derives the actual ratio from the vHost tab; previously this was ignored and 7.0 was used throughout, overstating on-prem pCore counts and understating per-core license costs.

- `benchmarks_default.yaml` + `BenchmarkConfig`: `vcpu_to_pcores_ratio` 7.0 → 1.97
- `rvtools_to_inputs`: default to benchmark 1.97; vHost-calculated ratio stored as `vcpu_ratio_vhost` in `PipelineResult` for opt-in UI override
- Agent Intake: vCPU/pCore ratio toggle in Optional Parameters (benchmark 1.97 vs vHost-calculated)

**Azure Retail Prices API attribution:**  
Info banner added to Agent Intake results page with link to prices.azure.com; pricing source labels updated to distinguish `'Azure Retail Prices API'`, `'cached'`, and `'Default (benchmark)'`.

---

## v0.5.0 — OS Parsing, Storage Mode, Time Horizon, Fact Checker Page
**Commit:** `5576b94` | **Date:** 2026-04-02 | **Tests:** 53 ✅

### What changed

**Bug fixes:**
- Parser: `_WINDOWS_VERSIONED_PATTERN` added (4-digit year regex). Windows Server 2016/2019/2022 VMs no longer miscounted as "unversioned" — only truly version-less OS strings increment `windows_vms_unknown_version`.
- Agent Intake: pricing source display now shows `'Azure API'` (api/cache) or `'Default'` (benchmark) with delta annotation, not the raw source key.
- Agent Intake: SKU/pricing caption rewritten to distinguish fleet-level vCPU rate vs per-VM matching and aggregate vs per-VM storage mode.

**Features added:**
- **Storage mode selector** in Agent Intake Optional Parameters: `'Per-VM disk tiers'` or `'Fleet aggregate'`; propagated through the pipeline.
- **Analysis time horizon selector**: 5-Year / 10-Year radio; KPI metrics and chart scope update accordingly.
- **Chart improvements:** legend moved to vertical right-side panel to prevent overlap; average annual CF savings annotated as a dashed horizontal line with value in the chart title.
- **Fact Checker page** (`app/pages/fact_checker_page.py`): new standalone sidebar page `'🔍 Fact Checker'` with two tabs:
  - *Engine Sanity Checks* — 8 self-consistency checks (NPV ordering, payback, ROI, Azure<OnPrem cost/VM, CF array consistency) scored 0–100% confidence; no file upload required.
  - *Excel Cross-Check* — upload a saved `.xlsm/.xlsx`; calls `engine.fact_checker.run()` and renders coloured comparison table.
- `main.py`: Fact Checker added to sidebar navigation.

---

## v0.4.2 — Agent Intake Form UX
**Commit:** `3972d8d` | **Date:** 2026-04-02

### What changed

- **Deferred validation:** errors (required fields) only shown after the user clicks Submit, never on page load.
- **Submit button** always visible below form fields.
- **`st.status()` progress panel:** replaces spinner with expandable two-step progress (file validation → pipeline); collapses to summary badge on success, expands with error on failure.

---

## v0.4.1 — Agent Intake Security Gates
**Commit:** `0aba331` | **Date:** 2026-04-02

### What changed

- **Sensitivity gate:** acknowledgment checkbox must be checked before the file uploader appears.
- **Required-field validation:** `st.error()` for missing customer name; `st.warning()` when sensitivity not acknowledged.
- **Encrypted file detection:** `zipfile.is_zipfile()` pre-check blocks OLE-format (password-protected) `.xlsx` files with a clear user instruction to remove the password in Excel. `zipfile.BadZipFile` / `openpyxl.InvalidFileException` caught as fallback.

---

## v0.4.0 — Automated Agent Intake Pipeline
**Commit:** `3a28836` | **Date:** 2026-04-02

### What changed

**New `engine/rvtools_to_inputs.py`:**
- `workload_inventory_from_rvtools()` — `RVToolsInventory` → `WorkloadInventory` field mapping
- `build_business_case()` — full end-to-end pipeline (parse → region → pricing → right-size → compose)
- `build_business_case_from_bytes()` — Streamlit `file_uploader` convenience wrapper
- `PipelineResult` typed dataclass with `sql_summary` and `os_summary` helpers

**New `app/pages/agent_intake.py`:**
- `'⚡ Agent Intake'` page: customer name, currency, RVTools upload (+ optional migration horizon, ACO Y1–Y3, ECIF Y1–Y2, DCs to exit)
- Post-parse: inventory cards, OS/SQL profile, Azure right-sizing summary, KPI preview + 5-year cost comparison chart
- Populates `session_state[inputs]` so Steps 4 & 5 work immediately after
- SQL detection: uses Application custom attribute when available; falls back to 10%-of-Windows default; Prod/Non-Prod split from Environment attribute

---

## v0.3.1 — Assume Production When No Environment Tags
**Commit:** `6fa7a72` | **Date:** 2026-04-02

### What changed

- Default classification for VMs with no `Environment` tag changed from non-prod to **Production**.
- Only explicit non-production tags (`dev/development/test/testing/uat/qa/staging/sandbox/non-prod`) override to Non-Production.
- New `RVToolsInventory` fields: `sql_prod_assumed`, `env_tagging_present`.
- Agent Intake: SQL Prod/Non-Prod metric shows `'(all prod, assumed)'` with amber info banner when `sql_prod_assumed = True`, explaining the assumption and how to correct it (tag VMs in vCenter, re-export, re-upload).

---

## v0.3.0 — Results Presentation Tab + Demo Cheat Sheet
**Commit:** `9087bbb` | **Date:** 2026-04-02

### What changed

- Results page: new `'📽️ Present'` tab (full-screen screen-share mode — all charts and KPIs, no app chrome).
- `DEMO_CHEAT_SHEET.md` added.

---

## v0.2.0 — Export & Presentation Page
**Commit:** `62554f2` | **Date:** 2026-04-01

### What changed

- New `app/pages/export.py` (Step 5): PowerPoint and Excel export.
- PowerPoint output: dark-theme deck, 2 slides — KPI cards + dual 5Y/10Y charts (Slide 1), annual cashflow table (Slide 2).
- Excel output: pre-fills `Template_BV Benchmark Business Case v6.xlsm` yellow input cells via openpyxl; user recalculates macros in Excel.

---

## v0.1.4 — Cashflow Model + Exec Summary Charts
**Commit:** `cc2ece3` | **Date:** 2026-04-01

### What changed

- **Cash Flow view** added to `engine/financial_case.py`: acquisition-based CAPEX (actual spend year of purchase) rather than depreciation-smoothed P&L.
- `BusinessCaseSummary` extended with CF arrays (`sq_cf_by_year`, `az_cf_by_year`, etc.), CF NPVs, CF totals.
- Results page: Exec Summary tab with stacked bar + line chart (5Y and 10Y toggle); separate Cash Flow tab alongside P&L tab.

---

## v0.1.3 — Auto-Pipeline: Region Consensus, Per-VM Disk Tiers, Fallback Warnings
**Commit:** `0ae6b24` | **Date:** 2026-04-01

### What changed

- **Region inference:** DC consensus algorithm (≥50% of hosts in a named DC → that DC wins); priority order: TLD → DC consensus → GMT offset → keyword → `eastus` fallback.
- **Per-VM disk tier assignment:** `engine/disk_tier_map.py` with Standard SSD E-series and Premium SSD P-series tier tables (East US LRS list prices).
- **Fallback warnings:** CPU and memory rightsizing fallback warnings surfaced in UI when vCPU/vMemory tabs absent from the RVtools export.

---

## v0.1.2 — Parser: vHost-Aware Auto-Detection for TCO Baseline
**Commit:** `4f39430` | **Date:** 2026-04-01

### What changed

TCO baseline scope is now automatically determined based on the presence of the vHost tab:

| vHost tab | TCO scope | Rationale |
|---|---|---|
| Present | Powered-on VMs only | vHost confirms actual host inventory; powered-off = idle/decommissioned |
| Absent | All VMs (on + off) | Without host data, all VMs included to avoid understating baseline |

Override via `parse(path, include_powered_off=True/False)`.

---

## v0.1.1 — Parser Hardening: Powered-On Filter, Template Exclusion, ESU Dual-Column
**Commit:** `aea2eb5` | **Date:** 2026-04-01

### What changed

- Powered-on filter for storage, license, and per-VM sizing counts (excludes powered-off VMs from Azure sizing target).
- Template VM exclusion (vInfo `Template` column = TRUE).
- ESU dual-column detection: checks both `'OS according to the configuration file'` and `'OS according to VMware Tools'` columns; emits `esu_count_may_be_understated = True` warning when pre-2016 VMs have only generic OS strings.

---

## v0.1.0 — Engine Validation: 5 Bug Fixes, Track A+B PASS
**Commit:** `ed4864` | **Date:** 2026-03-31

### What changed

Full validation run against the reference workbook. Five engine bugs fixed:

1. Server acquisition cost: missing memory cost term
2. DC power: TDP formula used wrong load factor denominator
3. Windows license: `pcores_with_windows_esu` incorrectly included in non-ESU count
4. Migration ramp: off-by-one in `(ramp_y − ramp_{y−1})` incremental fraction calculation
5. NPV: Y0 was being discounted (should be undiscounted)

`scripts/validate_vs_reference.py` added: Track A (parser accuracy vs workbook inputs) and Track B (engine output accuracy vs workbook financial outputs).

---

## v0.0.1 — Initial Scaffold
**Commit:** `32c281d` | **Date:** 2026-03-31 | **Tests:** 30 ✅

### What was built

- Full engine scaffold: `models.py`, `status_quo.py`, `retained_costs.py`, `depreciation.py`, `financial_case.py`, `outputs.py`, `productivity.py`, `net_interest_income.py`
- Streamlit app: Steps 1–4 (Intake, Consumption Plan, Benchmarks, Results)
- `engine/rvtools_parser.py`: vInfo, vCPU, vMemory, vDisk, vHost, vMetaData parsing
- `engine/region_guesser.py`: GMT offset + keyword region inference (initial version)
- `engine/azure_sku_matcher.py`: Azure Retail Prices API client (D4s v5 + E10 LRS)
- `data/benchmarks_default.yaml`: 57+ benchmark parameters
- `data/region_map.yaml`: GMT offset / TLD / datacenter keyword → Azure region map
- 30/30 tests passing

---

## v0.0.0 — Repository Created
**Commit:** `ceb38b1` | **Date:** 2026-03-31

Initial repository creation.
