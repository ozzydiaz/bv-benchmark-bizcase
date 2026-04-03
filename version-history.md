# BV Benchmark Business Case — Version History

All versions correspond to Git commits on the `main` branch.  
Dates are commit dates (Pacific Time). Test counts reflect the state at each commit.

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

`scripts/validate_vs_reliance.py` added: Track A (parser accuracy vs workbook inputs) and Track B (engine output accuracy vs workbook financial outputs).

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
