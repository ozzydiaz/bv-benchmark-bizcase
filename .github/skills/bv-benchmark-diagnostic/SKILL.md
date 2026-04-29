---
name: bv-benchmark-diagnostic
description: >
  Full diagnostic of the BV Benchmark Business Case app: codebase scan,
  RVTools file test run, error analysis vs expected results, and structured
  improvement proposals with implementation comparison.
argument-hint: >
  Optional context, e.g. "focus on region inference" or "include Azure cost
  analysis in the financial model review".
---

# BV Benchmark Business Case — Diagnostic Skill

Run this skill whenever you receive a new input file, observe unexpected
pipeline output, or need to evaluate the app before proposing enhancements.
It produces a structured report covering codebase state, live test results,
error analysis, and ranked improvement options.

---

## Step 0 — Orientation (always run first)

Before touching any file:

1. Read `version-history.md` top-to-bottom. Note the **current version**, last
   3 commits, any open "known issues", and any bugs that were recently fixed.
2. Read `README.md` and `GETTING_STARTED.md` for the public contract.
3. Skim `engine/models.py` (all Pydantic models — `WorkloadInventory`,
   `ConsumptionPlan`, `BenchmarkConfig`, `RVToolsInventory`).
4. Skim `engine/rvtools_parser.py` — note every column constant (`COL_*`),
   the `VMRecord` dataclass fields, and TCO scope resolution logic.
5. Skim `engine/rvtools_to_inputs.py` — note `build_business_case()` parameter
   list and `PipelineResult` fields.
6. Skim `engine/consumption_builder.py` — note `RightsizingValidation` field
   names (they change between versions; do **not** guess attribute names).
7. Check `engine/parsers/__init__.py` for the `InventoryParser` protocol.

> **Decision point:** If the version history references a breaking change in
> the last 2 commits that touches the parser or consumption builder, re-read
> those files fully before proceeding.

---

## Step 1 — Locate the Input File

```python
# Find the .xlsx file in the project root (exclude archive/ and Template*)
find /path/to/project -maxdepth 1 -name "*.xlsx" -not -name "Template*"
```

- Confirm exactly **one** file is present.
- Note: `Template_BV Benchmark Business Case v6.xlsm` is the output template,
  not an input. `archive/*.xlsx` are reference files. Neither should be used
  as the test input.

---

## Step 2 — Pre-flight: Inspect the File Before Running the Parser

Before calling `build_business_case()`, always inspect the raw file:

```python
import openpyxl
wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
print("Sheet names:", wb.sheetnames)
for shname in wb.sheetnames[:5]:
    ws = wb[shname]
    rows = list(ws.iter_rows(max_row=2, values_only=True))
    print(f"\n--- {shname} ---")
    print("Headers:", list(rows[0])[:20])
    if len(rows) > 1:
        print("Sample:", list(rows[1])[:20])
```

**Classify the file format** using this decision tree:

| Indicator | Format | Expected parser behaviour |
|-----------|--------|--------------------------|
| Has `vInfo` tab + `VM`, `Powerstate`, `CPUs`, `Memory` (MB) columns | Standard RVTools | Full parse — all fields populated |
| Has `vInfo` tab but `Memory` column values are <10 for a typical server | RVTools with GB units | Silent parse error — memory will be understated ~1024× |
| Tab named `vinfo*` (case-insensitive, non-exact) but no `vInfo` | PowerCLI or other export | **Parser returns all zeros — no error raised** |
| Single tab, columns `Name`, `PowerState`, `NumCPU`, `MemoryGB`, `Guest` | VMware PowerCLI (`Get-VM`) | Parser returns all zeros |
| Columns include `_index`, `LoadDate`, `VMID` | vSphere database export | Parser returns all zeros |

> **Critical check:** If `inv.num_vms == 0` after parsing, the file is the
> wrong format — **not** an empty environment. The parser silently no-ops when
> the `vInfo` tab is absent; it never raises an exception.

---

## Step 3 — Run the Pipeline and Capture All Outputs

```python
import logging
logging.basicConfig(level=logging.WARNING)
from engine.rvtools_to_inputs import build_business_case

result = build_business_case(path, client_name="TestCo", currency="USD",
                             ramp_preset="Extended (100% by Y3)")
inv = result.inventory
```

**Required checks — compare against expected values:**

```
inv.num_vms                  > 0        (zero → wrong format or empty file)
inv.num_vms_poweredon        > 0
inv.total_vcpu               > 0
inv.total_vmemory_gb         > 0
inv.total_disk_provisioned_gb ≥ 0      (0 = vDisk tab absent — fallback to in-use)
inv.vhost_available          True/False (affects TCO scope)
inv.include_powered_off_applied         (True when vHost absent)
inv.vcpu_per_core_ratio      > 0        (0 = vHost tab absent)
inv.cpu_util_p95             > 0        (0 = no vCPU tab)
inv.memory_util_p95          > 0        (0 = no vMemory tab)
inv.parse_warnings           []         (any warnings indicate missing data)
result.region                not ""     (empty = no region evidence)
len(inv.vm_records)          == inv.num_vms_poweredon
```

**Attribute names to use (verify against current `consumption_builder.py`):**

```python
rv = result.rightsizing_validation
# RightsizingValidation fields (as of v1.2.3):
rv.on_prem_vcpu           # source fleet vCPU
rv.azure_vcpu             # target Azure vCPU
rv.on_prem_memory_gb      # source fleet memory
rv.azure_memory_gb        # target Azure memory
rv.on_prem_vm_count       # VM count
rv.telemetry_vm_count     # VMs with per-VM telemetry
rv.host_proxy_vm_count    # VMs using host-level proxy
rv.fallback_vm_count      # VMs using fallback reduction
rv.anomaly_vm_count       # anomaly SKU matches (>2× vCPU)
rv.vcpu_increased         # bool: azure > on-prem
rv.memory_increased       # bool: azure > on-prem * 1.25
rv.telemetry_coverage_pct # property: (telemetry + proxy) / total
```

> **Do NOT use `total_annual_azure_cost_usd`, `source_total_vcpu`,
> `target_total_vcpu`, `host_proxy_count`, `fallback_count`, `anomaly_count`
> — these do not exist on the current models.**

---

## Step 4 — Error Classification

After collecting results, classify every anomaly using this severity table:

| Class | Examples | Impact |
|-------|----------|--------|
| **CRITICAL** | `num_vms == 0`, `total_vcpu == 0`, wrong format silently accepted | Pipeline produces invalid business case with zero cost |
| **HIGH** | Region falls to `eastus2` fallback with no evidence, no utilisation telemetry | Cost estimate may be wrong region; rightsizing uses fallback factors |
| **MEDIUM** | ESU count may be understated (`esu_count_may_be_understated=True`), SQL detection uses 10% default | Licence cost estimates understated |
| **LOW** | `vcpu_per_core_ratio == 1.0` (vHost absent), missing vDisk tab | Storage costs use in-use heuristic instead of provisioned |
| **INFO** | `parse_warnings` non-empty, `azure_region_source == "fallback"` | Transparent — UI shows warning banners |

**Compare actual vs expected:**

- `inv.num_vms` should match the VM count visible in the Excel file
- `inv.total_vcpu` ÷ `inv.total_vmemory_gb` should be in the range 0.5–4 GB/vCPU
- `result.region` should reflect the geography suggested by domain/datacenter names
- `rv.telemetry_coverage_pct` should be > 0 if `vCPU`/`vMemory` tabs are present
- `rv.vcpu_increased == False` — Azure vCPU should never exceed on-prem (v1.2.3+)

---

## Step 5 — Known Format Variants and Workarounds

### PowerCLI / vSphere API Export (`Get-VM`)

Recognizable by: single tab, columns `Name` (numeric IDs), `PowerState`
(PascalCase), `NumCPU`, `MemoryGB`, `UsedSpaceGB`, `Guest`, `VMHost`, `VMHostID`.

**Current behaviour:** Parser returns all zeros, falls back to `eastus2`.

**Manual workaround (until a native parser exists):**
1. Ask customer to re-export from RVTools instead.
2. Or use the `engine/parsers/` Protocol to implement a
   `PowerCLIParser` (see Scenario B in proposals below).

### RVTools Export with Non-Standard Sheet Names

Recognizable by: columns match RVTools standard but sheet name has a date
suffix or version prefix (e.g. `vInfo_2025-01`, `vInfo (2)`).

**Current behaviour:** Tab not found → all zeros.

**Workaround:** Open the file in Excel, rename the tab to `vInfo`, save, re-upload.

### Merged Multi-Site RVTools Export

Recognizable by: `vHost.Datacenter` contains 3+ distinct names, multiple
domain TLDs in `vHost.Domain`.

**Current behaviour (v1.2.0+):** Per-VM region inference — handled correctly.
Check `inv.datacenter_host_counts` and the signal breakdown in the UI.

---

## Step 6 — Improvement Proposals

When the diagnostic identifies errors, propose exactly **3 scenarios** using
this comparison structure. Always include a "quick win" (low effort), a
"right architecture" (medium effort), and a "modernization" (higher effort).

### Template for each scenario:

```
### Scenario [A/B/C]: [Name]
**Scope:** [engine only / engine + UI / full stack]
**Effort:** [Low (~1 day) / Medium (~1 week) / High (>1 week)]

**What it does:**
- Bullet list of changes

**Pros:**
- ...

**Cons:**
- ...

**Implementation outline:**
1. [file] — [change]
2. [file] — [change]
...

**Key risk:**
- ...
```

### Pre-built Scenario: PowerCLI Format Support

#### Scenario A — Column Alias Mapper (Low effort, backward-compatible)

**Scope:** `engine/rvtools_parser.py` only  
**Effort:** Low (~1 day)

**What it does:**
- Add sheet name normalization: case-insensitive search for `vinfo` prefix
  before strict `vInfo` lookup
- Add column alias dictionary for known PowerCLI → RVTools mappings
  (`NumCPU` → `CPUs`, `MemoryGB` → `Memory` with GB→MB conversion,
   `Name` → `VM`, `PowerState` → `Powerstate`, `Guest` → `OS according to
  the configuration file`)
- Detect units automatically: if `Memory` max value < 100 for a VM record,
  assume values are already in GB (multiply by 1024 to convert back to MB
  for consistent internal handling)
- Emit `parse_warnings` entry identifying the alias substitutions used

**Pros:** No new files, minimal risk, handles the common PowerCLI case, backward-compatible

**Cons:** vHost data still absent (no host-level aggregates, no multi-region inference),
no vDisk/vPartition/vCPU/vMemory telemetry, complex alias maintenance as
more formats emerge

#### Scenario B — Dedicated `PowerCLIParser` (Medium effort, best architecture)

**Scope:** `engine/parsers/powercli_parser.py` (new) + pipeline auto-detection  
**Effort:** Medium (~1 week)

**What it does:**
- Implement `InventoryParser` protocol in `engine/parsers/powercli_parser.py`
- Auto-detect format in `build_business_case()`: sniff sheet names + column
  fingerprint; if PowerCLI signature found, dispatch to `PowerCLIParser`
- Parse the flat single-tab structure natively:
  - Map `PowerState == "PoweredOn"` → powered-on scope
  - Aggregate `VMHost` values → synthetic host records with derived
    `total_host_pcores` (sum of `CoresPerSocket × NumCPU` per unique host)
  - Parse `Guest` as single OS column; match against `_WINDOWS_PATTERN`,
    `_WINDOWS_ESU_PATTERN`
  - Handle `MemoryGB` natively (no MB→GiB conversion needed)
  - Populate `vm_records[]` for per-VM rightsizing
- Emit `source_type = "powercli"` for UI labelling

**Pros:** Clean separation of concerns, extensible Protocol already in place,
accurate native parsing, `vm_records` available for per-VM rightsizing,
`CoresPerSocket` can improve vCPU/pCore ratio accuracy

**Cons:** Still no utilisation telemetry (no vCPU/vMemory tabs in PowerCLI exports),
host-level aggregates are synthetic (may over/under-count pCores per host),
requires format auto-detection logic that must be maintained

#### Scenario C — Pre-flight Validation + User-Guided Column Remapping UI (Low effort, high UX impact)

**Scope:** `app/pages/agent_intake.py` (upload gate) + `engine/rvtools_to_inputs.py`  
**Effort:** Low–Medium (~2–3 days)

**What it does:**
- Pre-parse validation gate in Layer 1 upload step: before calling the parser,
  sniff sheet names + row 1 headers; classify format
- If `vInfo` tab absent: show a **format mismatch banner** listing:
  - Sheets found vs expected (`vInfo`, `vHost`, `vDisk`, etc.)
  - Most similar columns found vs expected
  - Specific re-export instruction (e.g. "Open RVTools → Export → All to xlsx")
- If format is recognized as PowerCLI: offer a **column-mapping UI** (selectbox
  per required field → maps to the file's actual column name), generates a
  `column_aliases` dict, passes to parser
- Fail fast and loudly (never silently return all zeros)

**Pros:** Zero engine changes, immediately improves user experience for all wrong-format uploads,
actionable error messages instead of silent zeros, foundation for future format support

**Cons:** Does not actually parse the alternate format (still requires Scenario A or B for
full support), column-mapping UI is manual/error-prone for large column sets

---

### Comparison Matrix

| Criterion | Scenario A (Alias Mapper) | Scenario B (PowerCLI Parser) | Scenario C (Pre-flight UI) |
|-----------|--------------------------|------------------------------|---------------------------|
| Effort | Low | Medium | Low–Medium |
| Risk | Low | Low–Medium | Low |
| vHost data available | No | Partial (synthetic) | No |
| Telemetry available | No | No | No |
| Per-VM rightsizing | No (fallback only) | Yes | No |
| Region inference | No (fallback only) | Partial (no domain/TLD) | No |
| Backward-compatible | Yes | Yes | Yes |
| User experience | Silent improvement | Silent improvement | Explicit error + guidance |
| Recommended order | 3rd | 1st | 2nd |

**Recommended implementation order:**  
`B → C → A`  
Build the PowerCLI parser first (correct architecture, reuses Protocol),
then add the pre-flight UI (user-facing quality gate), then add alias
mapper as a catch-all for unknown variants.

---

## Step 7 — Completing the Report

Your final output must include all of the following sections:

1. **Codebase Summary** — current version, last 3 commits, active architecture layers
2. **File Inspection** — format classification, sheet names, column mapping, VM/host counts
3. **Pipeline Run Results** — full table of actual vs expected values for all key fields
4. **Error Log** — every anomaly classified by severity (CRITICAL / HIGH / MEDIUM / LOW / INFO)
5. **Improvement Proposals** — exactly 3 scenarios with pros/cons and implementation outline
6. **Comparison Matrix** — side-by-side for the 3 scenarios on 5–8 criteria
7. **Recommended Path** — which scenario to implement first and why

---

## Quality Checklist

Before delivering the report, verify:

- [ ] Every field in Step 3 was actually read from the live parse output (not guessed)
- [ ] `RightsizingValidation` attribute names were verified against `consumption_builder.py`
- [ ] `ConsumptionPlan` attribute names were verified against `models.py` before use
- [ ] No scenario recommends changes to `models.py` without checking for downstream impacts on `financial_case.py`, `export.py`, `fact_checker.py`
- [ ] Silent-zero detection check was explicitly performed (`inv.num_vms == 0` condition)
- [ ] Each improvement scenario includes a specific file-level implementation outline

---

## Reference: Counting Scope Definitions

The pipeline uses **three distinct counting scopes**.  Confusing them is the
most common source of TCO vs Azure estimate discrepancies.

### Scope 1 — All-VM TCO Baseline (includes templates)

Used for: `num_vms`, `total_vcpu`, `total_vmemory_gb`, `total_storage_in_use_gb`,
`win_vcpu_count`, `win_esu_vcpu_count`, SQL VM counts.

**Includes:** powered-on VMs + powered-off VMs + template VMs.

**Why templates are included:** The customer pays for the hardware and licences
that cover the entire estate.  A template is a frozen VM image that still
occupies vCPU/memory allocations on the host until deleted.  The BA spreadsheet
cell `'1-Client Variables'!D39` counts all VMs regardless of power state.

**Customer A EXPECTED (2024-10-29):** `num_vms = 2,831`, `total_vcpu = 15,330`.

### Scope 2 — Powered-On Provisioned (Azure migration sizing)

Used for: `num_vms_poweredon`, `total_vcpu_poweredon`, `total_vmemory_gb_poweredon`,
`total_storage_poweredon_gb`, `vm_records[]`.

**Includes:** powered-on, non-template VMs only.

**Why powered-off excluded:** Powered-off VMs are not migrated to Azure unless
the customer explicitly says so.  Azure pricing is based on running instances.

**Customer A EXPECTED:** `num_vms_poweredon` ≈ 2,618 (2,831 − templates − powered-off).

### Scope 3 — Per-VM Azure Cost Sizing (rightsized or like-for-like)

Used for: `azure_vcpu`, `azure_memory_gb`, `azure_storage_gb`, `ConsumptionPlan`.

**Includes:** powered-on, non-template VMs from `vm_records[]`, after applying
utilisation × headroom reduction (rightsized) or direct provisioned match (like-for-like).

**Customer A EXPECTED (like-for-like):** `azure_vcpu ≈ 16,318` (all provisioned vCPUs
snapped up to 8-core minimum SKU, not reduced by utilisation).

### Quick-check table

| Field | Scope | Expected Customer A value |
|-------|-------|---------------------|
| `inv.num_vms` | All-VM (incl. templates) | 2,831 |
| `inv.total_vcpu` | All-VM (incl. templates) | 15,330 |
| `inv.num_vms_poweredon` | Powered-on only | ~2,618 |
| `inv.num_hosts` | Hosts with ≥1 powered-on VM | 242 |
| `inv.vcpu_per_core` | Powered-on hosts, vpc>0 | ~1.58 |
| `azure_vcpu` (like-for-like) | SKU-matched powered-on | ~16,318 |

---

## Reference: Pricing Cache Health Diagnostic

The Azure pricing cache lives under `.cache/azure_prices/`.  Two file types:

| File pattern | Content |
|--------------|---------|
| `<region>.json` | Reference pricing: `price_per_vcpu_hour_usd`, `price_per_gb_month_usd` |
| `vm_catalog_<region>.json` | SKU catalog: `{sku_name: {price_per_hour_usd, vcpu, memory_gib, ...}}` |

### Common failure modes

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| `vm_catalog_*.json` exists but all prices are $0.00 | File was written as empty `{}` on a failed API call; parser accepted it as valid | Delete the file or run `scripts/validate_pricing_cache.py --purge` |
| `total_azure_compute_usd` is near-zero but `azure_vcpu > 0` | VM catalog has $0 entries; engine falls back to reference rate but reference rate is also near-zero | Check `<region>.json` → `price_per_vcpu_hour_usd` should be ~0.048 for D-series |
| `total_storage_usd` is tiny (< $1k/yr for 3,000 VMs) | `_DEFAULT_GB_RATE` was 0.018 instead of 0.075, or `gb_rate` in cache is 0.0 | Delete cache and let it refresh; confirm `_DEFAULT_GB_RATE = 0.075` in `azure_sku_matcher.py` |
| Cache file exists but from a prior failed run | `_write_vm_price_cache()` wrote an empty dict before retry | Guard: never write `{}` to cache (v1.2.4+ fix in place) |

### Diagnostic commands

```bash
# Quick check — show verdict for all cache files
python scripts/validate_pricing_cache.py

# Purge invalid cache files (they refresh on next app run)
python scripts/validate_pricing_cache.py --purge

# Manual inspection
cat .cache/azure_prices/eastus2.json | python3 -m json.tool | head -20
cat .cache/azure_prices/vm_catalog_eastus2.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{len(d)} SKUs'); print(sum(1 for v in d.values() if v.get(\"price_per_hour_usd\",0)>0), 'priced')"
```

### Expected values (eastus2)

```json
{
  "price_per_vcpu_hour_usd": 0.048,     // ~$0.048/vCPU/hr PAYG D-series
  "price_per_gb_month_usd":  0.075,     // $0.075/GB/mo Premium SSD P-series
  "region": "eastus2"
}
```

VM catalog: `vm_catalog_eastus2.json` should have **~400–600 SKU entries**,
all with `price_per_hour_usd > 0`.  If `priced < 50%` of count, purge and refresh.

---

## Reference: Storage Source Priority Decision Tree

The `_vm_storage_cost()` function in `consumption_builder.py` selects the
storage size to use for Azure managed disk cost estimation.

```
For each VM:

1. vm.partition_capacity_gb > 0?
   YES → use vPartition.Capacity MiB / 953.67 (decimal GB)
         This is the provisioned filesystem capacity — what Azure
         needs to allocate.  Primary source; matches BA methodology.

2. vm.disk_sizes_gib (vDisk tab populated)?
   YES → sum all disk sizes from vDisk.Capacity MiB ÷ 1024 (GiB)
         NO reduction factor applied — BA uses full provisioned.

3. vm.provisioned_gib > 0?
   YES → use vInfo.Provisioned MiB / 953.67 (last resort)
         Typical for vInfo-only files without vDisk/vPartition tabs.

4. No storage data → return 0 (storage cost = $0 for this VM)
```

### Unit conversion reference

| Source | Column | Conversion | Output unit |
|--------|--------|-----------|-------------|
| vPartition.Capacity MiB | `Capacity MiB` | ÷ 953.67 | decimal GB (for BA matching) |
| vPartition.Consumed MiB | `Consumed MiB` | ÷ 1024 | binary GiB (reporting only) |
| vDisk.Capacity MiB | `Capacity MiB` | ÷ 1024 | GiB (matches Azure catalog) |
| vInfo.Provisioned MiB | `Provisioned MiB` | ÷ 953.67 | decimal GB |
| vInfo.In Use MiB | `In Use MiB` | ÷ 953.67 | decimal GB |

> **Why 953.67?**  1 GiB = 1,073,741,824 bytes = 1,073.74 MB (decimal).
> 1 MiB = 1,048,576 bytes = 1.049 MB.  1,000 MiB = 1,048.576 MB ≈ 1.049 GB.
> But the BA uses a different rounding: 1 GiB ≈ 0.9766 GB, so
> 1,000 MiB / 953.67 ≈ 1.049 GB (decimal).  The constant `MIB_TO_GB = 1/953.67`
> reproduces the BA's conversion exactly.

### Coverage flags

| Flag | Set when |
|------|---------|
| `inv.total_partition_capacity_gb > 0` | vPartition tab present; capacity populated |
| `inv.total_disk_provisioned_gb > 0` | vDisk tab present; provisioned capacity populated |
| `vm.partition_capacity_gb > 0` | This VM has vPartition data (per-VM) |
| `vm.disk_sizes_gib` non-empty | This VM has vDisk data (per-VM) |

---

## Reference: Layer 3 Input → Formula Mapping

Python engine fields map to specific Excel cells in
`Template_BV Benchmark Business Case v6.xlsm`.

### `1-Client Variables` tab (input cells — yellow)

| Python field | Excel cell | Notes |
|--------------|-----------|-------|
| `inv.num_vms` | D39 | All VMs incl. templates + powered-off |
| `inv.total_vcpu` | D40 | TCO baseline vCPU |
| `inv.total_vmemory_gb` | D41 | TCO baseline memory GB |
| `inv.total_storage_in_use_gb` | D42 | In-use storage GB |
| `inv.win_vcpu_count` | D45 | Windows-licensed vCPUs |
| `inv.win_esu_vcpu_count` | D46 | ESU-eligible Windows vCPUs |
| `inv.sql_vms_prod` | D49 | SQL Server prod VMs |
| `inv.sql_vms_nonprod` | D50 | SQL Server non-prod VMs |
| `inv.num_hosts` | D54 | ESX hosts with ≥1 powered-on VM |
| `inv.vcpu_per_core` | D55 | Avg vCPU/pCore ratio from vHost |

### `2a-Consumption Plan Wk1` tab (Azure sizing inputs)

| Python field | Excel cell | Notes |
|--------------|-----------|-------|
| `cp.azure_vcpu` | D14 | Azure vCPU (rightsized or like-for-like) |
| `cp.azure_memory_gb` | D15 | Azure memory GB |
| `cp.azure_storage_gb` | D16 | Azure managed disk GB |
| `cp.total_compute_usd_yr` | D18 | Annual compute cost USD |
| `cp.total_storage_usd_yr` | D19 | Annual storage cost USD |

### `Benchmark Assumptions` tab

| Python field | Excel cell | Notes |
|--------------|-----------|-------|
| `pb.cpu_util_fallback_factor` | D8 | CPU utilisation % used when no telemetry |
| `pb.memory_rightsizing_headroom_factor` | D12 | Memory headroom multiplier |
| `pb.hours_per_year` | D25 | 8,760 (fixed) |

### `Status Quo Estimation` tab

| Python source | Excel cell | Notes |
|--------------|-----------|-------|
| `financial_case.py → server_capex` | K/L columns rows 15–24 | Derived from `num_vms × avg_server_cost` |
| `financial_case.py → win_licence_cost` | row 28 | `win_vcpu × sql_licence_per_vcpu` |

> **Rule:** If a financial output cell is wrong, trace it through this table
> to find which Python field is the source.  Fix the parser/consumption_builder
> field — never patch the financial model output directly.

