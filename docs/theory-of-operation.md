# BV Benchmark Business Case — Theory of Operation & Workflows

This document describes the end-to-end logic of the three-layer checkpoint wizard in
enough detail that a collaborator can understand **what** the engine computes, **how** it
computes it, and **why** each design decision was made — without reading any source code.

---

## Overview

The application takes a single RVTools `.xlsx` export from a VMware-based on-premises
environment and produces a fully modelled, 10-year financial business case comparing
staying on-premises (the "Status Quo") against migrating workloads to Azure. The workflow
is divided into three sequential layers, each of which produces a checkpoint that a user
can review and override before advancing to the next:

| Layer | Name | What it produces |
|-------|------|-----------------|
| **1** | Inventory | Parsed VM fleet, inferred Azure region, live PAYG pricing |
| **2** | Rightsizing | Per-VM Azure SKU matches, validated consumption plan |
| **3** | Financial Model | 10-year P&L and cash-flow comparison, NPV, ROI, payback |

Each approved layer collapses to a compact summary banner. A **← Revise** button on any
banner re-opens that layer and discards downstream work, so revisions are never destructive.

---

## Data Flow Diagram

```
RVTools .xlsx export
        │
        ▼
[LAYER 1 — INVENTORY]
  rvtools_parser.py
    vInfo     → VM counts, vCPU, memory, storage, OS/ESU/SQL detection
    vCPU      → per-VM CPU utilisation P95
    vMemory   → per-VM memory utilisation P95
    vDisk     → per-disk provisioned capacity
    vPartition→ per-VM filesystem consumed/provisioned capacity
    vHost     → pCore counts, datacenter names, GMT offsets, host count
    vMetaData → vCenter FQDNs
        │
  region_guesser.py  (priority: TLD → DC consensus → GMT → DC keyword → fallback)
        │
  azure_sku_matcher.py  (Azure Retail Prices API; 24-hour disk cache)
        │
[LAYER 2 — RIGHTSIZING]
  consumption_builder.py  (orchestrates per-VM loop)
    vm_rightsizer.py      → utilisation resolution, target vCPU/memory
    azure_sku_matcher.match_sku() → least-cost Azure SKU per VM
    disk_tier_map.py      → per-VM managed disk cost (vPartition → vDisk → vInfo)
        │
  RightsizingValidation checkpoint
        │
[LAYER 3 — FINANCIAL MODEL]
  status_quo.py     → 10-year on-prem cost baseline (CAPEX + OPEX)
  depreciation.py   → 7-year lookback + 10-year forward depreciation schedule
  retained_costs.py → on-prem cost tail under the Azure migration ramp
  financial_case.py → full 34-row × 11-year P&L matrix + cash-flow matrix
  outputs.py        → NPV, ROI, payback, waterfall, productivity, NII
        │
  BusinessCaseSummary (displayed in UI, exported to PowerPoint / Excel)
```

---

---

# Layer 1 — Inventory

## Purpose

Layer 1 transforms a raw RVTools export into three outputs:
1. A structured **`RVToolsInventory`** object capturing fleet size, compute, storage,
   OS profile, utilisation telemetry, and per-VM records.
2. An **Azure region string** (e.g. `uksouth`, `westus3`) inferred from the inventory's
   geographic signals.
3. **Live Azure PAYG pricing** for that region (compute per-vCPU-hour and storage
   per-GB-month), fetched from the Azure Retail Prices API.

## 1.1 RVTools File Format and Tab Structure

RVTools exports are multi-sheet Excel workbooks. The parser reads the following tabs using
**column names** (not positional indices), so it is robust to RVTools version differences.

| Tab | Key columns consumed | What it produces |
|-----|---------------------|-----------------|
| `vInfo` | VM, Powerstate, Template, CPUs, Memory (MB), In Use MiB, OS (config file), OS (VMware Tools) | Core VM count, vCPU, memory, storage, OS/ESU flags |
| `vCPU` | VM, Overall MHz, Max MHz | Per-VM CPU utilisation fraction (P95) |
| `vMemory` | VM, Consumed MiB, Size MiB | Per-VM memory utilisation fraction (P95) |
| `vDisk` | VM, Capacity MiB | Per-disk provisioned sizes in GiB |
| `vPartition` | VM, Capacity MiB, Consumed MiB | Per-VM filesystem provisioned and consumed capacity |
| `vHost` | Host, Datacenter, Domain, GMT Offset, # Cores, # Memory, vCPUs per Core | Physical cores, datacenter names, GMT offsets, host utilisation |
| `vMetaData` | Server | vCenter FQDNs (used for TLD-based region detection) |

If any optional tab (vCPU, vMemory, vDisk, vPartition, vHost) is absent, the parser
records that fact and downstream logic falls back to safe defaults rather than failing.

## 1.2 Two Counting Scopes — TCO Baseline vs. Azure Migration Target

The inventory maintains **two distinct VM scopes** that serve different purposes in the
business case:

### TCO Baseline (all VMs including powered-off)
Fields: `num_vms`, `total_vcpu`, `total_vmemory_gb`, `total_storage_in_use_gb`,
`pcores_with_windows_server`, `pcores_with_windows_esu`

**Why:** The customer is already paying for server hardware, software licences, and
datacenter facilities for their entire estate — including powered-off VMs. A powered-off
VM is still physically racked, still consuming licensed pCore slots, and still occupying
datacenter space. Excluding powered-off VMs would understate the on-premises cost baseline
and make the Azure case look smaller than it is.

### Azure Migration Target (powered-on VMs only)
Fields: `num_vms_poweredon`, `total_vcpu_poweredon`, `total_vmemory_gb_poweredon`,
`total_storage_poweredon_gb`

**Why:** Only running VMs will consume Azure IaaS resources after migration. Sizing Azure
consumption (compute, storage, cost) against powered-off VMs would produce Azure bills
that never materialise in practice, making the migration appear more expensive than it is.

> **Key insight:** The cost **saved** by migrating is measured against the full TCO
> baseline (all VMs). The cost **incurred** in Azure is sized against powered-on VMs only.
> This asymmetry is intentional and correct.

### Auto-Detection Rule for Include-Powered-Off

| `vHost` tab present? | Default TCO scope | Rationale |
|---------------------|------------------|-----------|
| Yes | Powered-on VMs only | vHost confirms actual host inventory; powered-off VMs represent idle capacity not driving active hardware costs |
| No | All VMs (on + off) | Without host data, the full inventory may be under-counted; including all VMs avoids understating the baseline |

An explicit override (`include_powered_off=True/False`) is available for edge cases.

## 1.3 OS and ESU Detection

The parser checks **both** OS columns — `OS according to the configuration file` (primary)
and `OS according to the VMware Tools` (fallback). The configuration-file column is
primary because it more frequently contains explicit version strings.

### Windows Server Detection
Pattern: `windows\s+server` (case-insensitive). Any matching VM increments
`pcores_with_windows_server` by its host's physical-core count divided by the
vCPU-to-pCore ratio.

### ESU-Eligible Detection
Pattern: `windows\s+server\s+(2003|2008|2012)` — Windows Server 2003, 2008 (incl. R2),
and 2012 (incl. R2). These versions reached end of standard support and require Extended
Security Updates (ESU), which are expensive on-premises but **free** in Azure under the
Azure Hybrid Benefit.

### Unversioned Windows Caveat
Pre-2016 VMs often have a generic OS string (e.g. `Windows Server 2016 or later`) in
both columns because the VMware hardware version predates reliable OS fingerprinting. The
parser detects this and sets `esu_count_may_be_understated = True`, emitting a warning.
Best practice: supplement with an OS audit tool (Azure Migrate, MAP Toolkit, Intune).

### SQL Server Detection
The `Application` custom attribute (vInfo column 77) is checked for the string `sql`.
When SQL VMs are detected this way, it overrides the default assumption of 10% of Windows
VMs having SQL. VMs are split into Production and Non-Production using environment tags;
if no tags are present, all SQL VMs are assumed Production (conservative default).

## 1.4 Region Inference

Each VM is assigned an individual Azure region based on its own host's geographic signals.
This is important for merged RVTools exports that span multiple datacenters in different
countries — the engine will price UK VMs against `uksouth` rates and India VMs against
`centralindia` rates simultaneously.

### Priority Order (applied per host)

| Priority | Signal | Source | Notes |
|----------|--------|--------|-------|
| 1 | Country-code TLD | `vHost.Domain`, host FQDN, `vMetaData.Server` | `.uk` → `uksouth`; `.de` → `germanywestcentral`. Most reliable. |
| 2 | Datacenter consensus | `vHost.Datacenter` (host counts) | If ≥50% of hosts share a named DC with a keyword match, that DC wins. Quorum requirement prevents one mis-labelled host from dominating. |
| 3 | Datacenter keyword (no quorum) | `vHost.Datacenter` | Any keyword match on the DC name, e.g. `Phoenix` → `westus3`. |
| 4 | GMT offset | `vHost.GMT Offset` | Non-UTC offsets only. UTC (offset=0) is **intentionally excluded** — enterprises globally force server clocks to UTC regardless of physical location, so UTC carries zero geographic information. |
| 5 | Fallback | — | `eastus2` |

**Why not trust timezones?** Enterprises routinely configure all server clocks to UTC as
an operational policy (log correlation, support handoffs). A datacenter physically located
in London or Mumbai may report `UTC+0` if the IT team has a UTC-everywhere policy. The
parser therefore treats UTC as "no signal" rather than "Europe/UK".

### Fleet-Level Override
A user can force a single region for all VMs via the Layer 1 override panel. This
re-stamps every `vm.azure_region` to the forced value and sets
`azure_region_source = "override"`.

## 1.5 Azure Pricing Fetch

After region inference, the engine calls the **Azure Retail Prices API** for:
- **Per-vCPU-hour rate** — derived from the PAYG price of `Standard_D4s_v5` (4 vCPUs),
  used as a fallback rate when per-VM SKU matching has no catalog price.
- **Per-GB-month managed disk rate** — derived from Standard SSD LRS `E10 LRS` (128 GiB).

These serve as reference benchmarks. The actual per-VM pricing in Layer 2 comes from the
full VM SKU catalog, not these reference rates.

### Caching
Results are cached to disk under `.cache/azure_prices/` with a 24-hour TTL. If the API is
unreachable or returns no data, the engine falls back to built-in benchmark defaults
(`$0.048/vCPU/hr`, `$0.075/GB/month`) and marks `source = "benchmark"`. The cache
guards against empty/all-zero files to prevent silent $0 compute costs.

## 1.6 Layer 1 Outputs Reviewed by the User

The UI displays:
- **Fleet overview**: total VMs (TCO scope), powered-on VMs, hosts, total vCPUs, total memory
- **Storage, vCPU/pCore ratio, Azure region(s), pricing source, parse warnings**
- **Multi-region breakdown** (if the export spans multiple geographies): VM count per region,
  signal type used for each, and any VMs with no geographic signal (fallback flag)
- **OS/SQL profile**: Windows Server count, ESU-eligible count, SQL VMs detected, production split
- **Utilisation telemetry**: CPU P95, memory P95, VM count contributing

---

---

# Layer 2 — Rightsizing

## Purpose

Layer 2 takes the `RVToolsInventory` from Layer 1 and produces a **`ConsumptionPlan`** —
an estimate of the annual Azure IaaS cost (compute + storage) the customer will incur
after migration, plus a **`RightsizingValidation`** checkpoint with quality signals.

The core loop iterates over every powered-on, non-template VM record, computes a rightsized
Azure target, matches the least-cost Azure SKU, and prices it at PAYG rates.

## 2.1 Utilisation Resolution

For each VM, the engine resolves the best available utilisation signal:

```
Always returns (0.0, 0.0, "fallback")
```

**Current state:** Per-VM telemetry (vCPU/vMemory tab) and host-proxy paths were
deliberately disabled (v1.3.0, M1 fix). The vCPU/vMemory tab uses obfuscated VM names in
some exports (e.g. `vm100`…`vm2661`), which caused partial-match host-proxy logic to fire
for hundreds of VMs, producing anomalous +25% Azure vCPU outliers. The fallback benchmark
factors produce consistent, auditable results that match the BA spreadsheet.

**What "fallback" means:** no per-VM utilisation measurement is used. The engine applies
the benchmark's `cpu_util_fallback_factor` (default: **0.60** — i.e. assume 60% average
CPU utilisation) and `mem_util_fallback_factor` (default: **0.80** — assume 80% average
memory utilisation). These are conservative industry-standard assumptions for VMware
environments where telemetry is absent.

## 2.2 Rightsizing Formula

Given a VM with `vcpu` vCPUs and `memory_mib` MiB of memory:

### Fallback mode (always active)

```
target_vcpu    = max(1, ceil( vcpu × cpu_fallback × (1 + cpu_headroom) ))
target_mem_gib = max(1, ceil( (memory_mib ÷ 1024) × mem_fallback × (1 + mem_headroom) ))
```

Default benchmark values:
- `cpu_util_fallback_factor` = 0.60
- `cpu_rightsizing_headroom_factor` = 0.20 (20% headroom above measured demand)
- `mem_util_fallback_factor` = 0.80
- `memory_rightsizing_headroom_factor` = 0.20

So a VM with 16 vCPUs at defaults: `ceil(16 × 0.60 × 1.20)` = `ceil(11.52)` = **12 target vCPUs**.

### Source-Size Ceiling (critical design constraint, v1.2.3)

After computing the raw target, the engine applies:

```
target_vcpu    = min(vm.vcpu,          raw_target_vcpu)
target_mem_gib = min(vm.memory_mib/1024, raw_target_mem_gib)
```

**Why this cap exists:** A VM running at ≥83% utilisation would produce a raw target that
exceeds its own allocation after adding 20% headroom. Azure SKU matching would then snap
up to the next discrete tier (e.g. a 9-vCPU target → D16s = 16 vCPUs), compounding across
a large fleet to produce 2–3× the on-prem vCPU total, dramatically overstating Azure cost.
The cap enforces the design intent: this tool sizes for **migration**, not for upgrades.
A VM at capacity is already the right size; Azure SKU-tier rounding provides the small
remaining adjustment.

**Guarantee:** Azure vCPU total ≤ On-Prem vCPU total (before SKU snapping). Some
uplift from discrete SKU tiers is unavoidable and expected; the net result is always close
to or below on-prem provisioned counts.

### Utilisation Cap (0.95)

Even when telemetry is available (future), utilisation fractions are capped at 0.95 before
applying headroom. VMware's `Consumed/Size MiB` ratio can legally exceed 1.0 due to memory
ballooning, TPS (Transparent Page Sharing), and swap reclaim — metrics that reflect
VMware's memory management, not actual application demand. The 0.95 cap prevents these
artefacts from producing Azure targets above the on-prem provisioned size.

## 2.3 Azure VM Family Selection

After computing `(target_vcpu, target_mem_gib)`, the engine selects an Azure VM family
based on memory density and workload-type keywords:

| Rule (checked in order) | Family |
|------------------------|--------|
| VM name or Application contains `sap` or `oracle` AND `mem_gib / vcpu ≥ M-threshold` | M-series (memory-optimised, very high density) |
| VM name or Application contains `sap` or `oracle` (not M-threshold) | E-series |
| VM name or Application contains `hpc` | F-series (compute-optimised) |
| `vm.is_sql == True` OR name/app contains `database`, `db`, `cache`, `redis`, `mongo`, `elastic` | E-series |
| `mem_gib / vcpu ≥ E-series threshold` (default: 6.0 GiB/vCPU) | E-series |
| `mem_gib / vcpu ≥ M-series threshold` (default: 13.0 GiB/vCPU) | M-series |
| Default | D-series (general purpose) |

**Why these rules:** Azure's D-series is the most economical general-purpose family. E-series
is memory-optimised and appropriate for databases and cache workloads. F-series is
compute-optimised (high vCPU, low memory). M-series handles extreme memory requirements
(SAP HANA, large Oracle databases). Automatic keyword detection means the seller does not
need to manually classify each VM.

## 2.4 SKU Matching Algorithm

For each VM the engine calls `match_sku(target_vcpu, target_mem_gib, vm_catalog, family)`.

### Algorithm
1. Filter the catalog to the chosen family (D, E, F, or M).
2. Filter to SKUs where `sku.vcpu ≥ target_vcpu` AND `sku.memory_gib ≥ target_mem_gib`.
3. Among qualifying SKUs, select the one with the **lowest annual cost** (PAYG price × 8760 hrs).

### Asymmetric SKU Matching (v1.2.2)
SKUs where the matched vCPU count exceeds `target_vcpu × sku_match_secondary_tolerance`
(default: 2.0×) are penalised. This prevents a VM requiring 6 vCPUs from matching a
32-vCPU SKU just because it has the lowest price-per-unit. The result is a "best fit"
rather than a "cheapest large SKU" match.

### Minimum vCPU Floor
The minimum matched vCPU is **8** (`Standard_D8s_v5` is the absolute fallback). This
prevents micro-SKUs from producing unrealistically low per-VM costs for VMs that,
in practice, will require at least a baseline-capable Azure VM.

### No-Catalog Fallback
If no live catalog is available, the engine falls back to:
`price = vm_ref_vcpu_rate × target_vcpu × 8760`

## 2.5 Per-VM Storage Costing

Storage pricing uses a cascade of sources in priority order, reflecting accuracy:

| Priority | Source | Why preferred |
|----------|--------|--------------|
| 1 | vPartition `Capacity MiB` (provisioned filesystem capacity) | Most accurate — reflects what the guest OS has allocated for filesystems |
| 2 | vInfo `In Use MiB` | Hypervisor-level in-use bytes — actual data size |
| 3 | vDisk `Capacity MiB` × `(1 − reduction factor)` | Provisioned disk size, derated for typical over-allocation |
| 4 | vInfo provisioned MiB × `(1 − reduction factor)` | Last resort |

**Why in-use/consumed over provisioned:** Azure managed disks are billed on the provisioned
tier size selected, but the selection should be based on **needed** capacity — not the
VMware-allocated size. On-prem VMware disks are commonly over-allocated by 2–5×; using
raw provisioned sizes inflates Azure storage costs by the same factor. Using actual data
size (vPartition consumed or vInfo in-use) as the sizing basis is conservative and
customer-accurate.

Each VM's per-disk storage cost is computed by `disk_tier_map.vm_annual_storage_cost_usd()`,
which assigns each disk to the cheapest Azure managed disk tier that covers its size
(P-series Premium or Pv2-series).

### Aggregate Storage Mode (alternative)
An "aggregate" mode is available that computes `fleet_total_provisioned_gb × blended_rate/GB`
instead of per-VM disk costs. This is less accurate but faster and can be toggled in the
Layer 2 override panel.

## 2.6 Azure Consumption Formula (Y10 Full Run Rate)

Once all per-VM compute and storage costs are summed:

```
annual_compute_lc_y10 = Σ(sku.price_per_hour_usd × 8760) × usd_to_local
annual_storage_lc_y10 = Σ(vm_annual_storage_usd) × usd_to_local
```

This is the **Year 10 anchor** — the full steady-state annual Azure cost at 100% migration
penetration, in local currency. The migration ramp (see §2.7) scales this down for earlier
years. ACD (Azure Consumption Discount) is **not** applied here; it is applied in the
financial model (Layer 3) when the seller has agreed a discount level with the customer.

## 2.7 Migration Ramp Presets

The ramp defines how quickly workloads migrate. It is stored as a 10-element list (Y1–Y10)
representing the fraction of workloads migrated by end of each year.

| Preset | Y1 | Y2 | Y3 | Notes |
|--------|----|----|-----|-------|
| Fast (100% by Y2) | 0.50 | 1.00 | 1.00 | Aggressive migration |
| **Extended (100% by Y3)** (default) | 0.25 | 0.75 | 1.00 | Typical enterprise migration |
| Gradual (100% by Y5) | 0.10 | 0.30 | 0.60 | Conservative phased migration |

The ramp is used in two ways:
1. **Azure consumption** — scaled by `(ramp_y + ramp_{y-1}) / 2` (half-year convention,
   see §3.3) to model the average run rate within each year.
2. **Retained on-prem costs** — scaled by `(1 − ramp)` to model the shrinking on-prem
   tail as VMs leave.

## 2.8 Per-Region Pricing in Mixed-Geography Exports

When a merged RVTools export contains hosts from multiple countries (each with its own
inferred Azure region), the engine builds a per-region pricing and catalog cache:
- `_region_pricing_cache`: fetches `AzurePricing` once per distinct region.
- `_region_catalog_cache`: fetches the VM SKU catalog once per distinct region.

Each VM is priced against **its own region's live PAYG rates**, not the fleet-level
average. A UK VM is priced against `uksouth`; an India VM against `centralindia`. This
produces an accurate blended Azure cost for multi-geography migrations.

## 2.9 RightsizingValidation Checkpoint

At the end of the per-VM loop, the engine produces a `RightsizingValidation` with:

| Field | Description |
|-------|-------------|
| `on_prem_vcpu` / `on_prem_memory_gb` | Source fleet totals (powered-on) |
| `azure_vcpu` / `azure_memory_gb` | Rightsized Azure totals after SKU matching |
| `telemetry_vm_count` | VMs with per-VM utilisation telemetry (currently 0) |
| `host_proxy_vm_count` | VMs sized via host-level proxy (currently 0) |
| `fallback_vm_count` | VMs sized via fallback factors (currently all VMs) |
| `telemetry_coverage_pct` | Fraction of VMs with any telemetry signal |
| `vcpu_reduction_pct` | Signed %; positive = Azure < on-prem (expected) |
| `anomaly_vm_count` | VMs where matched SKU vCPU > 2× source vCPU |
| `anomaly_vms` | Top anomalous VMs (name, source vCPU, matched vCPU) |
| `vcpu_increased` / `memory_increased` | Warning flags |
| `warnings` | List of quality warnings |

The UI displays this checkpoint so the user can verify that the rightsizing is directionally
correct before trusting the financial model.

---

---

# Layer 3 — Financial Model

## Purpose

Layer 3 assembles the full **11-year (Y0–Y10) financial P&L matrix** for both the Status
Quo (stay on-premises) and the Azure Case (migrate), computes cash-flow variants, and
derives the headline financial metrics: **NPV, ROI, payback period**.

The engine replicates the Excel workbook sheets: `Status Quo Estimation`,
`Depreciation Schedule`, `Retained Costs Estimation`, `Detailed Financial Case`, and
`Summary Financial Case / 5Y CF with Payback`.

## 3.1 Input Categories

Before computing costs, the engine assembles a `BusinessCaseInputs` Pydantic model with:

| Category | Key fields |
|----------|-----------|
| `WorkloadInventory` | `total_vms_and_physical`, `est_allocated_pcores_incl_hosts`, `pcores_with_windows_server`, `pcores_with_sql_server`, `allocated_storage_gb` |
| `ConsumptionPlan` | Y10 Azure compute/storage run rate, migration ramp, ACD, migration cost per VM |
| `BenchmarkConfig` | Unit cost rates (all in YAML), WACC, growth rate |
| `HardwareAssumptions` | Depreciation life, actual usage life, refresh rate, growth rate |
| `PricingAssumptions` | Windows/SQL licence price levels (Standard, Discount) |
| `DatacenterInfo` | DCs to exit, exit type (Static/Proportional), interconnects |

## 3.2 Status Quo Costs (`status_quo.py`)

Computes the full 10-year on-prem cost profile if the customer **does not migrate**.

All cost lines grow at `expected_future_growth_rate` (default 3%) per year. Index 0 = Y0
baseline (current state); indices 1–10 = projection.

### Hardware CAPEX (Acquisition)

```
server_acq_yr = (pcores × cost_per_core + pmem_gb × cost_per_gb_mem) × (1+g)^yr × (depr_life / actual_life) / depr_life
storage_acq_yr = max(0, allocated_gb − bundled_gb) × cost_per_gb × refresh_factor
nw_acq_yr = (num_cabinets × cabinet_cost + router/switch costs) × refresh_factor
```

The `depr_life / actual_life` ratio adjusts for real-world refresh cycles that differ
from accounting depreciation periods. If hardware is depreciated over 5 years but only
physically replaced every 7 years, the annual acquisition spending is lower than pure
depreciation would suggest.

### Hardware OPEX (Maintenance)

```
server_maintenance_yr = base_server_acq × (1+g)^yr × server_hw_maintenance_pct (5%)
storage_maintenance_yr = base_storage_acq × (1+g)^yr × storage_hw_maintenance_pct (10%)
network_maintenance_yr = base_nw_acq × (1+g)^yr × network_hw_maintenance_pct (10%)
```

### Datacenter Facilities

```
dc_power_kw = (pcores × TDP_watt / load_factor × PUE + storage_kw) / (1 − overhead_pct)
dc_space_cost_yr = dc_power_kw × space_cost_per_kw_month × 12 × (1+g)^yr
dc_power_cost_yr = dc_power_kw × power_cost_per_kw_month × 12 × (1+g)^yr
bandwidth_cost_yr = num_interconnects × interconnect_cost_per_yr × (1+g)^yr
```

**Power model note:** The datacenter power estimate uses a **PUE** (Power Usage
Effectiveness) factor to account for cooling, UPS losses, and lighting overhead on top of
raw IT load. `on_prem_pue = 1.5` means the datacenter draws 1.5 kW for every 1 kW of IT
work. The `overhead_pct` (30% by default) represents unused/reserve power capacity in the
facility.

### Licence Costs

```
virtualization_licenses_yr = pcores_virt × virtualization_license_per_core_yr × (1+g)^yr
windows_licenses_yr = pcores_windows × windows_license_per_core_yr(price_level) × (1+g)^yr
sql_licenses_yr = pcores_sql × sql_license_per_core_yr(price_level) × (1+g)^yr
windows_esu_yr = pcores_windows_esu × windows_esu_per_core_yr(price_level) × (1+g)^yr
sql_esu_yr = pcores_sql_esu × sql_esu_per_core_yr(price_level) × (1+g)^yr
```

**Price levels:** Standard (`_b`) and Discount (`_d`). Discount typically applies when
the customer has volume licensing agreements. ESU costs escalate by year (2×, 4× for
Windows/SQL respectively) to model the real Microsoft ESU pricing schedule.

### IT Administration Staff

```
num_admins_yr = round(total_vms × (1+g)^yr / vms_per_sysadmin)
system_admin_cost_yr = num_admins_yr × sysadmin_fully_loaded_cost_yr
```

The VM-count growth drives headcount growth: as the estate expands, more administrators
are needed. This avoids the unrealistic assumption that IT staff costs remain flat while
the environment they manage grows.

## 3.3 Depreciation Schedule (`depreciation.py`)

The depreciation engine produces a **7-year lookback + 10-year forward** schedule for
three asset classes: servers, storage, and network/fitout.

### Why a Lookback Is Needed
The P&L view of hardware costs uses depreciation (the amortisation of existing assets),
not cash acquisition. Assets purchased in years Y-7 through Y0 are still on the books and
generating depreciation charges in the forward projection period. Without the lookback,
Y1 depreciation would appear artificially low (only Y0 acquisitions contributing) until
the schedule "fills in" over time.

### Schedule Mechanics
```
For Y-7 to Y0 (historical):
  acquisition[i] = baseline_acq / depr_life   (even spread of existing asset base)
  depreciation[i] = annual_baseline_depr × (1+g)^max(0, year_offset)

For Y1 to Y10 (forward):
  acquisition[i] = baseline_acq × (1+g)^yr × (depr_life / actual_life) / depr_life
  depreciation[i] = same formula (simplified straight-line, 1-year recognition)
```

The `net_book_value` is tracked cumulatively as `cumulative_acq − cumulative_depr`.

## 3.4 Retained Costs (`retained_costs.py`)

When the customer migrates, they don't immediately stop paying all on-premises costs.
The retained costs engine computes the **on-prem cost tail** in the Azure case — what
the customer still pays while migration is in progress (and briefly after).

### Core Concept: The Ramp-Fraction

```
hw_fraction  = 1 − ramp_this_year          (hardware: terminates as VMs migrate)
lagged_fraction = 1 − ramp_prior_year      (everything else: 1-year billing lag)
```

### Billing Model Per Cost Line

| Cost line | Ramp treatment | Rationale |
|-----------|----------------|-----------|
| Server/storage/network maintenance | `hw_fraction` (current year) | Maintenance terminates the moment servers are decommissioned |
| DC space, DC power, bandwidth | `lagged_fraction` (prior year) | Physical space cannot be vacated the same day VMs migrate; leases and power circuits have notice periods |
| Virtualisation licences | `lagged_fraction × prior-year SQ cost` | Annual subscription; cancellation takes effect at next renewal |
| Windows/SQL licences | `prior-year SQ cost` (no ramp reduction) | BYOL/Azure Hybrid Benefit: SA obligations persist in Azure — the customer still pays Software Assurance to use AHB; licence cost doesn't disappear |
| Windows/SQL ESU | `lagged_fraction × prior-year SQ cost` | Drops to $0 once the VM is in Azure (AHB provides free ESU coverage) |
| Backup/DR software | `lagged_fraction × prior-year SQ cost` | Annual subscription; Azure Backup takes over after the transition year |
| IT admin staff | `lagged_fraction + productivity floor` | A productivity floor (D31) prevents IT admin cost from dropping to $0; some staff remains for residual on-prem operations and cloud management |

**DC exit type:** The user can set `dc_exit_type` to:
- **Proportional** — DC facility costs decline proportionally with migration ramp (the customer vacates space progressively as racks are removed).
- **Static** — DC facility costs are 100% until migration is complete, then drop to $0 (the customer has a committed lease they cannot exit early).

## 3.5 Azure Consumption in the Financial Model (`financial_case.py`)

The Azure consumption cost for year `yr` is:

```
consumption_yr = avg_ramp_yr × full_run_rate × (1 + g) × (1 − ACD)

where:
  avg_ramp_yr  = (ramp_yr + ramp_{yr−1}) / 2     [half-year convention]
  full_run_rate = compute_lc_y10 + storage_lc_y10 + other_lc_y10
  g            = expected_future_growth_rate       [flat single-period uplift]
  ACD          = azure_consumption_discount        [negotiated discount, 0–1]
```

**Half-year convention:** Migrations don't happen instantaneously on January 1st. The
average of `ramp_yr` and `ramp_{yr−1}` reflects that, within a year, some VMs migrated
at the start and some at the end — the average run rate for that year is the midpoint.

**Growth rate as flat uplift:** The `(1 + g)` factor is applied as a one-time single-period
cost-growth adjustment across all years (not compounded annually). This reflects the
assumption that Azure pricing may increase relative to today's rates, but the exact year
of the increase is uncertain.

**ACD applied here (not in Layer 2):** PAYG rates from Layer 2 are intentionally gross.
The discount is applied here so the seller can iterate the ACD slider in Layer 3 without
re-running the entire rightsizing computation.

## 3.6 Migration Costs

```
gross_migration_cost_yr = newly_migrated_vms_yr × migration_cost_per_vm_lc
net_migration_cost_yr = gross_migration_cost_yr − ACO_yr − ECIF_yr
```

- **Newly migrated VMs** = `(ramp_yr − ramp_{yr−1}) × total_vms`
- **ACO / ECIF** = Microsoft funding programs (Azure Credits, Engineering Credits) that
  offset migration costs. These are entered as year-by-year amounts.

## 3.7 The Full P&L Matrix

`financial_case.compute()` assembles a 34-row × 11-column matrix for both scenarios:

**Status Quo rows** (Y0–Y10): server depreciation, server maintenance, storage depreciation,
storage maintenance, backup storage, DR storage, network depreciation, network maintenance,
bandwidth, DC space, DC power, virtualisation licences, Windows licences, SQL licences,
Windows ESU, SQL ESU, backup software, DR software, IT admin.

**Azure Case rows** (same structure for retained on-prem costs, plus): Azure consumption,
migration costs, Microsoft funding, existing Azure run rate.

The `savings()` series = `sq_total[yr] − az_total[yr]` for each year. Positive savings
mean Azure is cheaper.

### Dual View: P&L vs. Cash Flow

The engine computes two parallel financial views:

| View | Hardware treatment | Purpose |
|------|--------------------|---------|
| **P&L (depreciation)** | Straight-line depreciation of existing + future assets | GAAP-style income statement; shows smooth annual expense |
| **Cash Flow (acquisition)** | Actual cash outlay when hardware is purchased | True cash impact; shows the lumpy CAPEX refreshes the customer avoids by migrating |

The cash-flow view uses `forward_acquisition` from the depreciation schedule instead of
`forward_depreciation`. For the Azure case, retained CAPEX is scaled by
`hardware_renewal_during_migration_pct` (default 10%) — customers defer hardware refreshes
while migrating, so only ~10% of normally-due hardware is actually purchased.

## 3.8 NPV Calculation

```
NPV = Σ [savings_yr / (1 + WACC)^yr]   for yr = 1 to N
```

WACC (Weighted Average Cost of Capital, default **7%**) represents the customer's hurdle
rate — the minimum return required to justify the investment. Discounting reflects the
time value of money: a dollar saved in Year 5 is worth less than a dollar saved today.

Two NPV variants are computed:
- **10-Year NPV**: all 10 years of savings discounted.
- **5-Year NPV**: first 5 years only (more conservative, preferred for shorter planning horizons).

### Terminal Value (Gordon Growth Model)

Beyond Year 10, the model applies a terminal value to capture the perpetual value of
the Azure cost advantage:

```
TV = savings_Y10 × (1 + g_perpetual) / (WACC − g_perpetual)
TV_discounted = TV / (1 + WACC)^10
```

`g_perpetual` (default 2%) is the assumed perpetual growth rate of savings. The terminal
value is added to produce `npv_with_terminal_value` — the theoretically complete NPV.

## 3.9 ROI Calculation

The P&L-based ROI measures the return multiple on the present-valued Azure investment:

```
ROI_10yr = (NPV_10yr_with_TV) / NPV_of_Azure_costs_10yr
```

This answers: "For every dollar I spend on Azure (present-valued), how much value do I
get back?"

## 3.10 Primary ROI and Payback — 5Y Cash Flow Method

The **displayed** ROI and payback metrics use the `5Y CF with Payback` method, which
matches the Excel template's summary sheet. This method separates one-time migration
investment from ongoing savings:

```
investment_NPV = Σ [migration_costs_yr / (1 + WACC)^yr]   for yr = 1 to 5

run_savings_yr = SQ_total_yr − (Azure_total_yr − migration_costs_yr)
  [i.e. Azure ongoing P&L excluding migration spend]

cumulative_discounted_savings = Σ [run_savings_yr / (1 + WACC)^yr]   for yr = 1 to 5

ROI_CF = (cumulative_discounted_savings − investment_NPV) / investment_NPV

payback = fractional year when cumulative_discounted_savings ≥ investment_NPV
```

**Why this method:** It isolates the migration investment as the "cost" and the ongoing
run savings as the "return". ROI answers "did the migration investment pay off within
5 years?" Payback answers "how long until the migration pays for itself?"

## 3.11 IT Productivity Benefit

A separate `productivity.py` module computes the annual IT staff-hours freed up by moving
to a managed cloud platform. The benefit is calculated as:

```
admin_hours_saved_yr = vms_migrated × hours_saved_per_vm_per_yr
productivity_benefit_lc_yr = admin_hours_saved_yr × fully_loaded_hourly_rate
```

This is reported as a supplementary benefit — it is **not included** in the primary savings
calculation (conservative stance) but is displayed as a separate line item.

## 3.12 Net Interest Income (NII)

`net_interest_income.py` computes the interest the customer earns (or avoids paying) on
the capital freed up by not purchasing hardware. If the customer has a cost of capital > 0,
avoiding a large CAPEX hardware purchase has a financing benefit. This is also reported
as supplementary.

## 3.13 Waterfall Chart Decomposition

To explain **where** the savings come from, the engine computes average annual savings
broken down by cost category (Y1–Y10 average):

```
Hardware Costs Reduction   = avg(SQ hardware) − avg(Azure retained hardware)
Facilities Costs Reduction = avg(SQ DC costs) − avg(Azure retained DC costs)
Licenses Costs Reduction   = avg(SQ licences) − avg(Azure retained licences)
IT Operations Reduction    = avg(SQ IT admin) − avg(Azure IT admin)
Azure Consumption Increase = −avg(Azure consumption)   [cost, shown negative]
```

The waterfall shows the seller exactly which cost categories drive the financial case,
and which is offset by Azure consumption spend.

---

---

## Appendix A: Key Benchmark Defaults

All defaults are in `data/benchmarks_default.yaml` and can be overridden in the UI.

| Parameter | Default | Notes |
|-----------|---------|-------|
| WACC | 7% | Customer hurdle rate for NPV discounting |
| `cpu_util_fallback_factor` | 0.60 | Assumed CPU utilisation when telemetry absent |
| `mem_util_fallback_factor` | 0.80 | Assumed memory utilisation when telemetry absent |
| `cpu_rightsizing_headroom_factor` | 0.20 | 20% buffer above demand for Azure target |
| `memory_rightsizing_headroom_factor` | 0.20 | 20% buffer above demand for Azure target |
| `server_cost_per_core` | $147 | Hardware acquisition cost per physical core |
| `server_hw_maintenance_pct` | 5% | Annual maintenance as % of acquisition cost |
| `storage_hw_maintenance_pct` | 10% | Annual maintenance as % of storage acquisition |
| `vm_to_physical_server_ratio` | 12 | VMs per physical host (virtualisation density) |
| `vcpu_to_pcores_ratio` | 1.97 | vCPU-to-pCore ratio for Windows licence estimation |
| `windows_esu_per_core_yr_b` | $343.73 | Windows ESU cost per core per year (standard) |
| `sql_esu_per_core_yr_b` | $6,598.34 | SQL ESU cost per core per year (standard) |
| `perpetual_growth_rate` | 2% | Gordon Growth Model terminal value growth rate |
| `vms_per_sysadmin` | 50 | VMs per full-time IT administrator |
| `sysadmin_fully_loaded_cost_yr` | $120,000 | Fully-loaded annual cost per IT admin |

---

## Appendix B: Layer Revision and State Management

Each layer stores its outputs in Streamlit session state:

| Key | Contents |
|-----|---------|
| `_l1_result` | `inv` (RVToolsInventory), `region`, `pricing`, `vm_catalog`, `client_name`, `currency` |
| `_l2_result` | `cp` (ConsumptionPlan), `validation` (RightsizingValidation) |
| `_l3_result` | `summary` (BusinessCaseSummary), `fc` (FinancialCase) |

When a user clicks **← Revise** on Layer N, `_clear_from(N)` deletes all keys for layers
N and above, and the wizard step reverts to N. No work below the revised layer is lost.

Scenarios added in Layer 3 are stored in `_l3_scenarios` as a list of
`(label, summary, fc)` tuples and displayed side-by-side in the financial comparison table.

---

## Appendix C: Module-to-Layer Mapping

| Module | Layer | Role |
|--------|-------|------|
| `engine/rvtools_parser.py` | 1 | Parse RVTools export into `RVToolsInventory` |
| `engine/region_guesser.py` | 1 | Infer Azure region from geographic signals |
| `engine/azure_sku_matcher.py` | 1 + 2 | Fetch live pricing (L1); SKU matching (L2) |
| `engine/vm_rightsizer.py` | 2 | Per-VM rightsizing and family selection |
| `engine/consumption_builder.py` | 2 | Orchestrate per-VM loop; produce ConsumptionPlan |
| `engine/disk_tier_map.py` | 2 | Map per-VM disk sizes to Azure managed disk tiers |
| `engine/status_quo.py` | 3 | 10-year on-prem cost baseline |
| `engine/depreciation.py` | 3 | 7-lookback + 10-forward depreciation schedule |
| `engine/retained_costs.py` | 3 | On-prem cost tail under Azure migration ramp |
| `engine/financial_case.py` | 3 | Full P&L + cash-flow matrix assembly |
| `engine/outputs.py` | 3 | NPV, ROI, payback, waterfall computation |
| `engine/productivity.py` | 3 | IT productivity supplementary benefit |
| `engine/net_interest_income.py` | 3 | NII supplementary benefit |
| `engine/models.py` | all | Pydantic data models shared across all layers |
| `app/pages/agent_intake.py` | all | Three-layer wizard UI |
