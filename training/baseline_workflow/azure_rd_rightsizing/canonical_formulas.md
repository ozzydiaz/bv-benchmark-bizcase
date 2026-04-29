# Microsoft Azure R&D — VMware/On-Prem to Azure Mappings (canonical source)

**Source**: PDF deck "VMWare / OnPrem to Azure Mappings" (AI Assistant Copilot),
shared by Microsoft R&D 2026-04-27. The deck defines the canonical Azure
right-sizing formulas adopted across multiple Microsoft Copilot agent
implementations.

**Ownership / authority**: Microsoft Azure R&D. This document captures the
slide content verbatim for citable reference. When the deck is updated,
update both this file and the rule entries that cite it.

**Why this file exists**: rule entries in `../ba_rules/layer2.yaml` cite
specific formulas (e.g. `R&D.SLIDE7.CPU.DEFAULT`) which resolve to specific
sections of this document. Reviewers can verify the engine implementation
against the canonical formulas without having to open the PDF.

---

## R&D.SUPPORTED_PROMPTS  (slide 2)

Primary prompt: **"Get VMWare to Azure mappings"**.
Purpose: identify Azure SKUs that correspond to VMware / On-Premises
configurations.

## R&D.OPTIONAL.REGION  (slide 3)

| Mode | Prompt | Behaviour |
|---|---|---|
| Map | `Map region to <azure_region_code>` | Apply the region only when vInfo Site/Datacenter is missing. |
| Force | `Force region to <azure_region_code>` | Override any vInfo Site/Datacenter; use the user value. |

## R&D.OPTIONAL.REGION_CODES  (slide 4)

The complete Azure public + sovereign region code list as of the PDF
revision. Reproduced verbatim:

```
australiacentral, australiacentral2, australiaeast, australiasoutheast,
austriaeast, belgiumcentral, brazilsouth, brazilsoutheast, canadacentral,
canadaeast, centralindia, centralus, centraluseuap, chilecentral, chinaeast,
chinaeast2, chinaeast3, chinanorth, chinanorth2, chinanorth3, eastasia,
eastus, eastus2, eastus2euap, eastusstg, francecentral, francesouth,
germanynorth, germanywestcentral, indonesiacentral, israelcentral, italynorth,
japaneast, japanwest, jioindiacentral, jioindiawest, koreacentral, koreasouth,
malaysiawest, mexicocentral, newzealandnorth, northcentralus, northeurope,
norwayeast, norwaywest, polandcentral, qatarcentral, southafricanorth,
southafricawest, southcentralus, southcentralusstg, southeastasia, southindia,
spaincentral, swedencentral, switzerlandnorth, switzerlandwest, uaecentral,
uaenorth, uksouth, ukwest, westcentralus, westeurope, westindia, westus,
westus2, westus3
```

## R&D.OPTIONAL.FAMILY_PROCESSOR  (slide 5)

| Prompt | Allowed values |
|---|---|
| `Map family to <X>` | `GeneralPurpose`, `ComputeOptimized`, `MemoryOptimized`, `HighPerformanceCompute`, `StorageOptimized`, `GPU`, `FPGAInstances` |
| `Map processor to <X>` | `Intel`, `AMD`, `ARM` |

Applied as a SKU-selection filter AFTER the rightsized triple
(rs_vcpus, rs_mem_gb, rs_disk_gib) is computed.

## R&D.OPTIONAL.RIGHT_SIZING  (slide 6)

| Prompt | Range |
|---|---|
| `CPU reduction% to <N>` | 1–100 |
| `Memory reduction% to <N>` and `Memory buffer% to <N>` | each 1–100 |
| `Storage reduction% to <N>` and `Storage buffer% to <N>` | each 1–100 |

Reductions COMPRESS the source value; buffers ADD margin above the
required capacity.

---

## R&D.SLIDE7.CPU — Right Sizing Logic (CPU)

### R&D.SLIDE7.CPU.DEFAULT  (no user CPU reduction%)

```
rs_vcpus = snapCPU( max( min_vcpus,
                          ceil( vInfo[CPUs] × max(vHost[CPU Usage %], 1) / 100 ) ) )
```

Notes:
- vHost[CPU Usage %] is read from the host that vInfo[Host] points to
  (per-VM, NOT a fleetwide average).
- The `max(…, 1)` floor means that when vHost[CPU Usage %] is missing,
  zero, or negative, the formula treats it as 1% — collapsing every VM
  on that host to `min_vcpus`. (See guard `KP.HOSTPROXY_GATE` in the
  Layer 2 rule book.)

### R&D.SLIDE7.CPU.USER  (user CPU reduction% supplied)

```
rs_vcpus = snapCPU( max( min_vcpus,
                          ceil( vInfo[CPUs] × (1 − cpu_reduction_pct/100) ) ) )
```

### R&D.SLIDE7.SNAPCPU

`snapCPU(n)` rounds `n` UP to the next value in Azure's standard vCPU ladder:

```
1, 2, 4, 8, 16, 20, 32, 48, 64, 80, 96, 104, 128, 192
```

Values above 192 stay as the next defined ladder value (extension is the
caller's responsibility — current ladder covers all general/compute/memory
optimised SKUs through 2026 catalogue revisions).

---

## R&D.SLIDE8.MEMORY — Right Sizing Logic (Memory)

### R&D.SLIDE8.MEMORY.DEFAULT  (no user reduction; buffer defaults to 0)

```
rs_mem_gb = snapMem( max( min_mem_gb,
                            (vMemory[Consumed] / 1024) × (1 + mem_buffer_pct/100) ) )
```

Source: vMemory tab `Consumed` column (per-VM telemetry, MiB).
Conversion: `/1024` (binary GiB).

### R&D.SLIDE8.MEMORY.USER  (user reduction% and/or buffer% supplied)

```
rs_mem_gb = snapMem( max( min_mem_gb,
                            (vInfo[Memory] / 1024)
                              × (1 − mem_reduction_pct/100)
                              × (1 + mem_buffer_pct/100) ) )
```

Source: vInfo `Memory` column (per-VM provisioned, MiB).

### R&D.SLIDE8.SNAPMEM

`snapMem(g)` rounds memory in GB UP to the next Azure memory tier:

```
4, 8, 16, 32, 48, 64, 96, 128, 160, 192, 256 …
```

The `…` indicates the ladder continues with Azure's published M-series
tiers (320, 384, 432, 512, 576, 672, 768, 1024, 1792, 2048, 3892, 5700,
11400, 23000) — caller extends as needed.

---

## R&D.SLIDE9.STORAGE — Right Sizing Logic (Storage)

Three-tier source chain. ALL branches snap UP to the nearest 128 GiB
boundary (P10 minimum) AND floor at 128 GiB.

### R&D.SLIDE9.STORAGE.DEFAULT  (vPartition present, Consumed MiB > 0)

```
rs_disk_gib = max( 128,
                   ceil( ( Σ vPartition[Consumed MiB] / 1024
                            × (1 + storage_buffer_pct/100) ) / 128 ) × 128 )
```

Buffer defaults to 0 when not supplied.

### R&D.SLIDE9.STORAGE.CAPACITY  (vPartition present, Consumed = 0/null OR user supplied reduction/buffer)

```
rs_disk_gib = max( 128,
                   ceil( ( Σ vPartition[Capacity MiB] / 1024
                            × (1 − storage_reduction_pct/100)
                            × (1 + storage_buffer_pct/100) ) / 128 ) × 128 )
```

### R&D.SLIDE9.STORAGE.VINFO  (vPartition tab absent entirely)

```
rs_disk_gib = max( 128,
                   ceil( ( vInfo[Total disk capacity MiB] / 1024
                            × (1 − storage_reduction_pct/100)
                            × (1 + storage_buffer_pct/100) ) / 128 ) × 128 )
```

Buffer defaults to 0 when not supplied.

---

## R&D.SLIDE10.MIN_FIELDS

Minimum vInfo columns required for any rightsizing to be possible:

- `Powerstate`
- `CPUs`
- `Memory`
- `In Use MiB`

## R&D.SLIDE11.IDEAL_FILE

An "ideal" file (every formula reachable, no fallbacks needed) contains:

- `vInfo`
- `vHost`
- `vPartition`
- `vMemory`

---

## How to cite this document in the rule book

Each `R&D.SLIDE7.CPU.DEFAULT`, `R&D.SLIDE8.MEMORY.USER`, etc. above is a
stable anchor. Layer 2 rule entries cite the anchor in their `citations[]`
list along with the BA transcript timestamp. Example:

```yaml
citations:
  - source: "Microsoft R&D — VMware/OnPrem to Azure Mappings"
    anchor: "R&D.SLIDE7.CPU.DEFAULT"
    file: training/baseline_workflow/azure_rd_rightsizing/canonical_formulas.md
  - source: "BA Layer 2 transcript"
    timestamp: "00:HH:MM:SS"
    excerpt: "..."
```
