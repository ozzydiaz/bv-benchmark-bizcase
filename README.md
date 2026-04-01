# BV Benchmark Business Case Engine

Automates the BV Benchmark Business Case Excel workbook (v6) as a Python engine + Streamlit application. Upload an RVtools export and the engine **automatically derives** on-premises inventory, right-sizes Azure targets, infers the deployment region, fetches live Azure Retail Prices API pricing, and produces a validated on-premises TCO vs. Azure migration financial case: NPV, ROI, payback, and full 10-year P&L.

The only inputs a seller needs to provide are:
- RVtools `.xlsx` export  
- Client name + currency  
- Azure Consumption Discount (ACD) once agreed  

All other inputs (vCPU, memory, storage, right-sized Azure targets, region, PAYG pricing) are auto-derived from the RVtools file.

---

## Requirements

| Dependency | Version |
|---|---|
| Python | 3.12+ |
| openpyxl | ≥ 3.1 |
| pydantic | ≥ 2.0 |
| streamlit | ≥ 1.32 |
| plotly | ≥ 5.0 |
| pandas | ≥ 2.0 |
| pytest | ≥ 8.0 |

Install:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## Quick Start

### Launch the Streamlit App

```bash
streamlit run app/main.py
```

Navigate to `http://localhost:8501`.  The app walks through four steps:

1. **Step 1 — Intake** Upload an RVtools `.xlsx` export *or* enter VM inventory manually.  
2. **Step 2 — Consumption Plan** Enter (or have auto-estimated) Azure PAYG run-rate and migration schedule.  
3. **Step 3 — Results** Financial case summary: NPV, ROI, payback, 10-year P&L waterfall.  
4. **Step 4 — Export** Download a formatted PowerPoint / Excel output.

### Run the Engine directly

```python
from engine.models import BusinessCaseInputs, BenchmarkConfig
from engine import status_quo, retained_costs, depreciation, financial_case, outputs

benchmarks = BenchmarkConfig.from_yaml("data/benchmarks_default.yaml")
# ... populate inputs ...
fc = financial_case.compute(inputs, benchmarks, sq, rc, depr)
summary = outputs.compute(inputs, benchmarks, fc)
outputs.print_summary(summary)
```

### Validate against reference workbook

```bash
python scripts/validate_vs_reference.py
```

Runs two tracks:
- **Track A** — parser accuracy vs. manually-completed reference workbook inputs.
- **Track B** — engine output accuracy vs. reference workbook financial outputs.

---

## Workflow & Data Flow

```
RVtools .xlsx export
        │
        ▼
engine/rvtools_parser.py
  ├─ vInfo   → VM counts, vCPU, memory, storage in-use, Windows/ESU pCores
  ├─ vCPU   → per-VM CPU utilisation P95 (Overall MHz / Max MHz)
  ├─ vMemory→ per-VM memory utilisation P95 (Consumed / Size MiB)
  ├─ vDisk  → per-disk provisioned capacity, per-VM disk inventory
  ├─ vHost  → pCore counts, datacenter names (with host counts), GMT offset
  └─ vMetaData → vCenter FQDNs
        │
        ▼
engine/region_guesser.py  (priority: TLD → DC consensus → GMT offset → keyword)
        │
        ▼
engine/azure_sku_matcher.py  (Azure Retail Prices API, 24-hour disk cache)
        │
        ▼
engine/consumption_builder.py  (right-size + build ConsumptionPlan)
  ├─ CPU:     P95 utilisation × headroom  OR  fallback −40% if vCPU tab absent
  ├─ Memory:  P95 utilisation × headroom  OR  fallback −20% if vMemory tab absent
  └─ Storage: aggregate (fleet total × $0.018/GB) OR per_vm (per-disk tier map)
        │
        ▼
app/pages/intake.py  (user reviews / overrides; stores region, storage_mode)
        │
        ▼
BusinessCaseInputs (Pydantic model)
┌────────┬────────┬──────────┬──────────┐
│Workload│Consump-│Benchmark │Engagement│
│Inventor│tion    │Config    │Info      │
│  y     │Plan    │(YAML)    │          │
└────────┴────────┴──────────┴──────────┘
                   │
    ┌──────────────┼──────────────────┐
    ▼              ▼                  ▼
status_quo.py   retained_costs.py  depreciation.py
    │              │                  │
    └──────────────┼──────────────────┘
                   ▼
          financial_case.py
          (full 11-year P&L matrix)
                   │
          ┌────────┼────────┐
          ▼        ▼        ▼
     outputs.py  productivity.py  net_interest_income.py
          │
          ▼
    BusinessCaseSummary
    (NPV, ROI, payback, waterfall, IT productivity, NII)
```

---

## Assumptions

### RVtools Parser — TCO Baseline Scope

The parser produces two distinct VM counts:

| Scope | Fields | When Used |
|---|---|---|
| **TCO Baseline** | `num_vms`, `total_vcpu`, `total_vmemory_gb`, `total_storage_in_use_gb`, `pcores_with_windows_server`, `pcores_with_windows_esu` | On-prem cost sizing |
| **Azure Migration Target** | `num_vms_poweredon`, `total_vcpu_poweredon`, `total_vmemory_gb_poweredon`, `total_storage_poweredon_gb` | Azure consumption estimation |

**Auto-detection rule:**

| vHost tab | Default TCO scope | Rationale |
|---|---|---|
| **Present** | Powered-on VMs only | vHost confirms actual host inventory; powered-off VMs represent idle/decommissioned capacity not driving active hardware costs |
| **Absent** | All VMs (on + off) | Without host data the full inventory may be under-counted; including all VMs avoids understating the baseline |

Override via `parse(path, include_powered_off=True/False)`.

### OS Detection (Windows / ESU)

- Both `OS according to the configuration file` and `OS according to the VMware Tools` columns are checked.  
- The configuration-file column is the **primary source** — it more frequently contains explicit version strings.  
- ESU-eligible versions: **2003**, **2008** (incl. R2), **2012** (incl. R2).  
- Pre-2016 VMs often have a generic string (`Windows Server 2016 or later`) in both columns. These are counted as Windows Server (for licensing) but **cannot** be reliably auto-classified as ESU. The parser emits a warning and sets `esu_count_may_be_understated = True`.  
- **Best practice:** Run a separate OS audit (MAP Toolkit, Azure Migrate, or SCCM/Intune inventory) and override the auto-detected ESU count in the intake form.

### Azure Consumption — Auto-Derivation from RVtools

When an RVtools file is uploaded, the engine runs a full auto-derivation pipeline before the user sees Step 2:

#### 1. Region inference (`engine/region_guesser.py`)

Checks signals in priority order:

| Priority | Signal | Source | Notes |
|---|---|---|---|
| 1 | Country-code TLD | `vHost.Domain`, `vMetaData.Server` | `.uk` → `uksouth`; `.de` → `germanywestcentral` etc. |
| 2 | **Datacenter consensus** | `vHost.Datacenter` (host counts) | If ≥50% of hosts share a named DC with a keyword match, that DC wins. More reliable than timezone as enterprises often set all servers to UTC. |
| 3 | GMT offset | `vHost.GMT Offset` | UTC (offset=0) maps to `uksouth` — used only when no consensus DC exists. |
| 4 | Datacenter keyword | `vHost.Datacenter` | Any match, no quorum required. |
| 5 | Fallback | — | `eastus` |

Example: a fleet with 88% of hosts in a datacenter named "Phoenix" → `westus3` (Phoenix, AZ), even though all servers are configured to UTC.

#### 2. Live PAYG pricing (`engine/azure_sku_matcher.py`)

- Calls the [Azure Retail Prices API](https://prices.azure.com/api/retail/prices) for the inferred region
- Reference SKUs: **Standard_D4s_v5** (Linux PAYG, 4 vCPU) → `price/vCPU/hr`; **Standard SSD E10 LRS** (128 GiB) → `price/GiB/mo`
- Results cached to `.cache/azure_prices/<region>.json` with a 24-hour TTL
- Falls back to benchmark defaults (`$0.048/vCPU/hr`, `$0.018/GB/mo`) if the API is unreachable

#### 3. Right-sizing (`engine/consumption_builder.py`)

| Dimension | With telemetry | Without telemetry (tab absent) |
|---|---|---|
| **CPU** | `ceil(vCPU_poweredon × P95_util × (1 + headroom))` | `ceil(vCPU_poweredon × (1 − 0.40))` default 40% reduction |
| **Memory** | `ceil(mem_gb_poweredon × P95_util × (1 + headroom))` | `ceil(mem_gb_poweredon × (1 − 0.20))` default 20% reduction |
| **Storage** | See modes below | — |

All reduction factors and percentile value (default P95) are configurable in `data/benchmarks_default.yaml`.

> **Important:** RVtools exports a single-point-in-time snapshot, not a time-series. P95 of a snapshot is a conservative estimate. For production sizing, supplement with 30/60/90-day performance history from Azure Migrate or vROps.

#### 4. Storage estimation modes

Selected in Step 1 under **"Azure Storage Estimation Mode"**:

| Mode | Formula | Best for |
|---|---|---|
| **`aggregate`** (default) | `ceil(vDisk_provisioned_gb × 1.20) × 12 × $0.018/GB` | Quick estimates; blended rate sufficient for fleet overview |
| **`per_vm`** | Each disk → `assign_tier(size_gib)` → tier monthly price; sum all disks | More accurate for mixed-size fleets; required for Premium SSD scenarios |

For `per_vm` mode, select the disk family:
- **`standard_ssd`** (default) — E-series tiers (E1–E80), suitable for general workloads
- **`premium_ssd`** — P-series tiers (P1–P80), for latency-sensitive / database workloads

Tier prices are stored in `engine/disk_tier_map.py` (East US LRS list prices, April 2025).

> **Aggregate vs per_vm difference:** On a typical 2,000 VM fleet, the aggregate blended rate ($0.018/GB) can **understate** Standard SSD cost by 2× because small disks (≤128 GB) have a higher effective $/GB rate at their tier. Use `per_vm` when the sales team wants to model realistic disk costs or Premium SSD scenarios.

#### ACR auto-estimate formula

```
compute_usd_yr = azure_vcpu × 8760 hr × price_per_vcpu_hour  (PAYG, before ACD)
storage_usd_yr = azure_stor_gb × 12 × price_per_gb_month      (aggregate)
             OR = Σ_disks assign_tier(disk_gib).price × 12     (per_vm)
```

### Azure Consumption — Manual / Override

**ACD — Azure Consumption Discount:**  
An optional percentage discount off PAYG, entered in the Consumption Plan step.  Common sources: Microsoft CSP discount, EA/MCA agreement, or individually negotiated terms.  
`effective_consumption = payg_estimate × (1 − ACD)`

**Azure Consumption Growth Formula:**  
The engine applies a cost-growth adjustment to Azure consumption to reflect the expectation that cloud pricing will be higher in future years:

```
consumption_year_y = avg_ramp_y × full_run_rate × (1 + growth_rate) × (1 − ACD)
```

Where:
- `full_run_rate` = `annual_compute + annual_storage + annual_other` from the Consumption Plan  
- `avg_ramp_y` = `(ramp_y + ramp_{y-1}) / 2` — half-year convention for each migration year  
- `growth_rate` = `expected_future_growth_rate` from the Hardware Lifecycle inputs (default 10%; reference workbook = 2%)  
- `ACD` = `azure_consumption_discount` (optional; default 0 = PAYG list price)  
- `(1 + growth_rate)` is applied as a **flat single-period uplift** to all years — matching the Excel formula, which treats this as a one-time cost-growth adjustment relative to today's prices, not as a year-by-year compound growth rate.

**Why this matters:** Without the growth multiplier, Azure consumption is understated by `growth_rate × 100%` relative to the workbook.  The reference workbook uses 2%; the engine default is 10%.  Always confirm the `expected_future_growth_rate` with the customer.

### Status Quo Cost Model

| Cost category | Key formula |
|---|---|
| Server acquisition | `pcores × cost_per_core + (pcores ÷ vcpu_to_pcores_ratio × vmem_to_pmem_ratio) × cost_per_gb_mem` |
| DC power (kW) | `TDP_W/core × pcores × watt_to_kW ÷ load_factor × PUE ÷ (1 − overhead)` |
| IT admin | `⌈total_vms ÷ vms_per_sysadmin⌉ × sysadmin_fully_loaded_cost` |
| Network fitout | Routers + switches only when `num_dcs > 0`; cabinet cost always |
| Virtualization | `pcores_with_virtualization × virtualization_license_per_core_yr` |
| Windows Server | `pcores_with_windows_server × license_rate_per_core_yr` (price level B or D) |
| ESU | `pcores_with_windows_esu × esu_rate_per_core_yr` |
| Backup/DR | `protected_vms × software_per_vm_yr + size_gb × storage_per_gb_yr` |

All CAPEX costs are spread over `depreciation_life_years` (default 5) using straight-line depreciation, with historical lookback of 7 years.

### IT Productivity Benefit

When `incorporate_productivity_benefit = Yes`, the engine computes a labour productivity saving from reduced IT operations overhead post-migration:

```
vms_y10 = total_vms × (1 + growth_rate)^10
on_prem_fte_y10 = vms_y10 / vms_per_sysadmin
productivity_gain_fte = on_prem_fte_y10 × productivity_reduction_after_migration × productivity_recapture_rate
annual_benefit = ⌊productivity_gain_fte⌋ × sysadmin_fully_loaded_cost_yr
```

Default benchmarks: `productivity_reduction_after_migration = 42%`, `productivity_recapture_rate = 95%`.  
The benefit ramps in with the migration schedule (proportional to `migration_ramp_pct`).

### Net Interest Income (NII)

The engine computes the interest earnings from the cash position differential between the SQ and Azure scenarios:

```
net_cash_outlay_y = SQ_cashflow_y − Azure_cashflow_y
ending_cash_y = ending_cash_{y-1} + net_cash_outlay_y
NII_y = max(0, ending_cash_{y-1}) × nii_interest_rate
discounted_NII_y = NII_y / (1 + wacc)^y
```

When the Azure case is cheaper than SQ (positive net outlay), the customer retains cash and earns interest.  During years where migration costs make Azure more expensive, the cash position is negative and no interest is earned.

Default `nii_interest_rate = 3%` (short-term deposit/treasury bill rate).

---

## Configuration

### User-Editable Fields (source workbook "1-Client Variables" + "2a-Consumption Plan")

All yellow-highlighted cells in the original Excel workbook are exposed as overridable inputs in the Streamlit app.  
**Step 1 = `app/pages/intake.py`** | **Step 2 = `app/pages/consumption.py`**

#### 1-Client Variables

| Excel Cell | Label | Model Field | Default | Step |
|---|---|---|---|---|
| D9 | Client Name | `EngagementInfo.client_name` | "Contoso" | 1 |
| D10 | Local Currency | `EngagementInfo.local_currency_name` | "USD" | 1 |
| D35 | Workload Name | `WorkloadInventory.workload_name` | "DC Move" | 1 |
| D39 | Nb of VMs | `WorkloadInventory.num_vms` | 0 | 1 |
| D40 | Physical servers (excl. VM hosts) | `WorkloadInventory.num_physical_servers_excl_hosts` | 0 | 1 *(Advanced)* |
| D44 | Allocated vCPU | `WorkloadInventory.allocated_vcpu` | 0 | 1 |
| D45 | Allocated pCores (excl. VM hosts) | `WorkloadInventory.allocated_pcores_excl_hosts` | 0 | 1 *(Advanced)* |
| D49 | Allocated vMemory GB | `WorkloadInventory.allocated_vmemory_gb` | 0 | 1 |
| D50 | Allocated pMemory GB (excl. VM hosts) | `WorkloadInventory.allocated_pmemory_gb_excl_hosts` | 0 | 1 *(Advanced)* |
| D54 | Allocated Storage GB | `WorkloadInventory.allocated_storage_gb` | 0 | 1 |
| D66 | vCPU / pCore ratio | `WorkloadInventory.vcpu_per_core_ratio` | 1.97 | 1 |
| D67 | pCores with Windows Server OS | `WorkloadInventory.pcores_with_windows_server` | 0 | 1 |
| D68 | pCores with Windows ESU | `WorkloadInventory.pcores_with_windows_esu` | 0 | 1 |
| *(derived)* | pCores with SQL Server | `WorkloadInventory.pcores_with_sql_server` | 10% of D67 | 1 *(SQL expander)* |
| *(derived)* | pCores with SQL ESU | `WorkloadInventory.pcores_with_sql_esu` | 10% of D68 | 1 *(SQL expander)* |
| D153 | Include existing run rate in business case | `AzureRunRate.include_in_business_case` | No | 1 *(Run Rate expander)* |
| D156 | Current ACD | `AzureRunRate.current_acd` | 0.0 | 1 *(Run Rate expander)* |
| D157 | New ACD | `AzureRunRate.new_acd` | 0.0 | 1 *(Run Rate expander)* |
| D160 | Monthly Spend (USD) | `AzureRunRate.monthly_spend_usd` | 0.0 | 1 *(Run Rate expander)* |
| D163–D166 | PayGo / RI / SP / SKU mix | `AzureRunRate.*_mix` | 1.0 / 0 / 0 / 0 | 1 *(Run Rate expander)* |
| *(hardware)* | Depreciation Life (years) | `HardwareLifecycle.depreciation_life_years` | 5 | 1 |
| *(hardware)* | Actual Usage Life (years) | `HardwareLifecycle.actual_usage_life_years` | 5 | 1 |
| D26 | Expected Future Growth Rate | `HardwareLifecycle.expected_future_growth_rate` | 10% | 1 |
| *(hardware)* | HW Renewal During Migration % | `HardwareLifecycle.hardware_renewal_during_migration_pct` | 10% | 1 |

> **Growth rate note:** D26 applies to **both** the on-premise SQ projections and the Azure consumption growth multiplier — keeping the comparison symmetric. The workbook default is 10%; conservative practice is 2–5%. Always confirm with the client.

#### 2a-Consumption Plan

| Excel Cell | Label | Model Field | Default | Step |
|---|---|---|---|---|
| D8 | Azure vCPU (right-sized) | `ConsumptionPlan.azure_vcpu` | 0 | 2 |
| D9 | Azure Memory GB | `ConsumptionPlan.azure_memory_gb` | 0 | 2 |
| D10 | Azure Storage GB | `ConsumptionPlan.azure_storage_gb` | 0 | 2 |
| E17–N17 | Migration ramp-up (EOY cumulative %) | `ConsumptionPlan.migration_ramp_pct` | [0.4, 0.8, 1, …] | 2 |
| E21–N21 | ACO per year | `ConsumptionPlan.aco_by_year` | all 0 | 2 *(Funding expander)* |
| E22–N22 | ECIF per year | `ConsumptionPlan.ecif_by_year` | all 0 | 2 *(Funding expander)* |
| E28–M28 | Compute consumption/year | derived from anchor × ramp | — | 2 |
| E29–M29 | Storage consumption/year | derived from anchor × ramp | — | 2 |
| E30–N30 | Other consumption/year | derived from anchor × ramp | — | 2 |
| *(bottom)* | ACD — Azure Consumption Discount | `ConsumptionPlan.azure_consumption_discount` | 0.0 | 2 |
| E35 | Activate Backup Option | `ConsumptionPlan.backup_activated` | No | 2 |
| E39 | Backup software in Azure consumption | `ConsumptionPlan.backup_software_in_consumption` | No | 2 *(shown when backup On)* |
| *(storage)* | Backup storage in Azure consumption | `ConsumptionPlan.backup_storage_in_consumption` | No | 2 *(shown when backup On)* |
| E42 | Activate DR Option | `ConsumptionPlan.dr_activated` | No | 2 |
| E46 | DR software in Azure consumption | `ConsumptionPlan.dr_software_in_consumption` | No | 2 *(shown when DR On)* |
| *(storage)* | DR storage in Azure consumption | `ConsumptionPlan.dr_storage_in_consumption` | No | 2 *(shown when DR On)* |

---

### Benchmark YAML (`data/benchmarks_default.yaml`)

All 51+ benchmark parameters can be overridden per engagement.  Key parameters:

| Parameter | Default | Description |
|---|---|---|
| `wacc` | 7% | Discount rate for NPV |
| `perpetual_growth_rate` | 3% | Terminal value growth |
| `expected_future_growth_rate` | 10% | Hardware and VM growth for SQ + Azure projections |
| `vms_per_sysadmin` | 1,200 | Used for IT headcount and productivity calculations |
| `payg_cost_per_vcpu_hour` | $0.048 | PAYG Azure compute fallback (D4s v5 East US ÷ 4 vCPU) |
| `payg_cost_per_gb_month` | $0.018 | PAYG Azure storage fallback (Standard SSD E10 LRS, aggregate) |
| `nii_interest_rate` | 3% | Short-term interest rate for NII calculation |
| `cpu_rightsizing_fallback_reduction` | 40% | CPU reduction when vCPU tab absent |
| `memory_rightsizing_fallback_reduction` | 20% | Memory reduction when vMemory tab absent |
| `cpu_rightsizing_headroom_factor` | 20% | Headroom above P95 CPU utilisation |
| `memory_rightsizing_headroom_factor` | 20% | Headroom above P95 memory utilisation |
| `storage_rightsizing_headroom_factor` | 1.20 | Provisioned storage × headroom (OS disk + temp overhead) |
| `utilization_percentile` | 95 | P-value used for CPU and memory utilisation analysis |

### Price Levels (Windows / SQL)

| Level | Description |
|---|---|
| B | Benchmark list price |
| D | Discounted (EA/MCA) — ~15% below B |

---

## Project Structure

```
bv-benchmark-bizcase/
├── app/
│   ├── main.py                  # Streamlit entry point (4 pages)
│   └── pages/
│       ├── intake.py            # Step 1: VM inventory, region, storage mode
│       ├── consumption.py       # Step 2: Azure PAYG estimate + migration ramp presets
│       ├── results.py           # Step 3: Financial case summary + charts
│       └── export.py            # Step 4: PowerPoint / Excel output
├── engine/
│   ├── models.py                # Pydantic input/config models + MIGRATION_RAMP_PRESETS
│   ├── rvtools_parser.py        # RVtools .xlsx parser (vInfo/vCPU/vMemory/vDisk/vHost/vMetaData)
│   ├── region_guesser.py        # Azure region inference (TLD → DC consensus → GMT → keyword)
│   ├── azure_sku_matcher.py     # Azure Retail Prices API client + 24h disk cache
│   ├── consumption_builder.py   # Right-size + ConsumptionPlan auto-build
│   ├── disk_tier_map.py         # Azure managed disk tier tables (E-series + P-series)
│   ├── status_quo.py            # On-prem 10-year cost baseline
│   ├── retained_costs.py        # Declining on-prem costs during migration
│   ├── depreciation.py          # CAPEX depreciation schedules (7yr lookback)
│   ├── financial_case.py        # Full 11-year P&L matrix (SQ + Azure)
│   ├── productivity.py          # IT Productivity benefit module
│   ├── net_interest_income.py   # Net Interest Income module
│   └── outputs.py               # NPV, ROI, payback, waterfall metrics
├── data/
│   ├── benchmarks_default.yaml  # 57+ benchmark + right-sizing parameters
│   └── region_map.yaml          # GMT offset / TLD / datacenter keyword → Azure region
├── scripts/
│   ├── validate_vs_reference.py # Track A (parser) + Track B (engine) validation
│   └── fact_check.py            # CLI: compare engine vs any saved client workbook
├── tests/
│   └── test_engine.py           # 30 unit tests
└── README.md
```

---

## Fact Checker

The fact checker compares the Python engine's computed outputs against any saved client workbook, giving practitioners and customers an objective parity score before presenting the business case.

### How it works

1. Reads the yellow input cells from the workbook's `1-Client Variables` and `2a-Consumption Plan Wk1` sheets
2. Reconstructs `BusinessCaseInputs` from those values
3. Runs the full Python engine pipeline
4. Compares every material output (NPV, ROI, payback, cost waterfall) against the Excel-cached values
5. Returns a `FactCheckReport` with per-metric delta %, pass/warn/fail status, and a weighted **Confidence Score (0–100%)**

> **Prerequisite:** The workbook must be **saved in Excel** after filling in all inputs — openpyxl reads formula values from the saved cache, not live formula execution.

### Confidence Score

$$\text{score} = \frac{\sum_i w_i \cdot \text{pass}_i}{\sum_i w_i} \times 100\%$$

High-stakes metrics (Project NPV, Payback, ROI) carry the most weight. A WARN counts as half credit.

| Score | Interpretation |
|---|---|
| ≥ 90% | High confidence — suitable for client presentation |
| 70–90% | Review WARN items before presenting |
| < 70% | One or more critical KPIs diverge — investigate before use |

### Tolerances

| Severity | Threshold | Action |
|---|---|---|
| PASS | ≤ 2% delta on critical metrics | No action needed |
| WARN | 2–5% delta | Review input assumptions |
| FAIL | > 5% delta | Investigate formula or input mismatch |

(Thresholds vary by metric; see `SEVERITY_CONFIG` in `engine/fact_checker.py`.)

### CLI usage

```bash
# Basic check — exit 0 if no FAIL
python scripts/fact_check.py --workbook path/to/client.xlsm

# Strict mode — exit 1 on WARN too
python scripts/fact_check.py --workbook path/to/client.xlsm --strict

# JSON output for CI pipelines
python scripts/fact_check.py --workbook path/to/client.xlsm --json
```

### Streamlit usage

In the **Step 4 Results** page, scroll to the **Fact Check** section and upload any saved `.xlsm` or `.xlsx` workbook.  The app displays:
- Confidence score gauge (green ≥ 90%, amber ≥ 70%, red < 70%)
- Per-metric comparison table with colour-coded rows
- Input mismatch warnings if the workbook's input cells differ from the engine's inputs

### Programmatic usage

```python
from engine.fact_checker import run, FactCheckReport

report = run(workbook_path="client.xlsm", inputs=my_inputs, benchmarks=my_bm)
report.print()
if report.confidence_score >= 90:
    print("Ready for client presentation.")
```

---

## Known Limitations

| Item | Status | Notes |
|---|---|---|
| ESU auto-detection | ⚑ Partial | Pre-2016 VMs show generic OS strings. Override in intake form using OS audit. |
| Region inference | ⚑ Heuristic | Priority: TLD → DC consensus (≥50% hosts) → GMT offset → keyword. Review inferred region in Step 1. |
| Azure SKU matching | ✓ Live API | D4s v5 Linux PAYG + E10 LRS disk via Azure Retail Prices API; 24h cache; benchmark fallback when offline. |
| Right-sizing (CPU) | ✓ Telemetry | P95 of vCPU.Overall/Max; falls back to −40% benchmark if vCPU tab absent. |
| Right-sizing (memory) | ✓ Telemetry | P95 of vMemory.Consumed/Size MiB; falls back to −20% benchmark if vMemory tab absent. |
| Storage — aggregate | ✓ Implemented | Fleet total × blended per-GB rate. Fast default. |
| Storage — per-VM tiers | ✓ Implemented | Per-disk tier assignment (Standard SSD E-series or Premium SSD P-series). |
| Azure consumption growth | ✓ Implemented | Flat `(1 + growth_rate)` uplift applied to all years (matches workbook) |
| Migration ramp presets | ✓ Implemented | Express (Y1) / Standard (Y2) / Extended (Y3) / Custom |
| IT Productivity | ✓ Implemented | Ramped with migration schedule |
| Net Interest Income | ✓ Implemented | Earned on positive cash differential position |
| AVS (Azure VMware Solution) | ⏳ Not yet | `byol_virtualization_for_avs` field reserved |
| PowerPoint export | ⏳ Not yet | Export page stub only |

---

## Testing

```bash
pytest tests/ -v
```

```bash
python scripts/validate_vs_reference.py
```

Expected output: `Track A: PASS ✓ | Track B: 30/30 PASS ✓ | 0 failures`

```bash
python scripts/_smoke_new_pipeline.py
```

Smoke test: region inference, right-sizing, and all storage modes (aggregate / per_vm Standard SSD / per_vm Premium SSD) run end-to-end on the sample RVtools export.

```bash
python3 scripts/_smoke_new_pipeline.py
```

Expected: region inference, right-sizing, and both storage modes run without error.
