# Getting Started — BV Benchmark Business Case (BV BC Agent)

## What this tool does

Given an **RVTools export** and a **customer name**, it automatically:

1. Parses the on-prem inventory (VMs, vCPUs, memory, storage, OS, SQL)
2. Infers the Azure region from datacenter metadata
3. Fetches live PAYG pricing from the Azure Retail Prices API
4. Right-sizes the estate to Azure VM SKUs using P95 utilisation telemetry
5. Runs the full 10-year TCO / cashflow / P&L financial model
6. Produces KPI cards, cost comparison charts, a waterfall, and export-ready
   PowerPoint + Excel outputs

**No manual inventory entry required.**

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11 or later | Streamlit Cloud runs 3.11; local dev tested on 3.11+ |
| Git | To clone the repo |
| Internet access | To fetch Azure Retail Prices (falls back to benchmarks if offline) |
| RVTools export (.xlsx) | Standard RVTools "All to xlsx" export |
| Excel template (.xlsm) | `Template_BV Benchmark Business Case v6.xlsm` (place in project root) |

The Excel template and RVTools exports are **not committed** to the repo (large binaries).
Obtain them separately and drop them into the project root directory before running.

---

## 1. Clone the repo

```bash
git clone https://github.com/ozzydiaz/bv-benchmark-bizcase.git
cd bv-benchmark-bizcase
```

---

## 2. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

**Optional — install kaleido for chart images in PowerPoint exports:**

```bash
pip install kaleido
```

---

## 3. Add required data files

Place these files in the **project root** (same directory as `app/`):

```
bv-benchmark-bizcase/
├── RVTools_export_<customer>_<date>.xlsx    ← your RVTools export
├── Template_BV Benchmark Business Case v6.xlsm  ← Excel template
├── app/
├── engine/
└── ...
```

Both file types are gitignored — they are never committed to the repo.

---

## 4. Run the Streamlit app

```bash
# From the project root, with the venv active:
streamlit run app/main.py
```

The app opens at **http://localhost:8501** in your browser.

---

## 5. Two ways to use the app

### Option A — Agent Intake (recommended, automated)

The Agent Intake page runs as a **3-layer checkpoint wizard**. The engine stops after each layer, shows you the full results, and waits for your approval before proceeding. You can review, override, and re-run each layer independently without losing downstream work.

#### Step bar

```
Upload  →  Inventory ✅  →  Rightsizing ✅  →  Financial ✅  →  Export
```

Approved layers collapse into compact green summary banners with a **← Revise** button if you need to re-open them.

---

#### Layer 1 — Inventory checkpoint

**What happens:** The engine parses the RVTools export, infers the Azure region (TLD → DC consensus → GMT → keyword), and fetches live PAYG pricing from the Azure Retail Prices API.

**What to review:**
- VM count, powered-on subset, vCPU, memory, storage
- OS profile: Windows, ESU-eligible, SQL Prod/Non-Prod
- Inferred Azure region and pricing source (live API vs benchmark fallback)
- ESU / SQL caveats (amber banners when data is inferred)

**Override options** (expand ⚙️ Layer 1 Overrides):
| Override | Effect |
|---|---|
| Force Azure Region | Override the inferred region and re-fetch live prices |
| Rename Client | Update the client name shown in outputs |
| Change Currency | Switch the display currency |

**Pre-flight before approving:** Choose **Migration Horizon** (5 or 10 years) and **Storage Mode** (`Per-VM disk tiers` or `Fleet aggregate`). These flow into Layer 2.

Click **✅ Approve Inventory — Proceed to Rightsizing** to continue.

---

#### Layer 2 — Rightsizing checkpoint

**What happens:** Per-VM rightsizing runs using P95 utilisation telemetry (with headroom), plus a `RightsizingValidation` checkpoint that audits signal quality.

**Source-size ceiling** — The rightsized target (vCPU and memory) is always capped at the source VM's own allocation. This tool sizes for *migration*, not upgrades: if `utilisation × headroom` would push the Azure target above what the VM currently has, the source allocation is already the right size. Without this cap, high-utilisation VMs (running at ≥ 83% with default 20% headroom) would produce targets above the source, causing large snap-ups to the next Azure SKU tier and dramatically inflating cost estimates.

**SKU matching methodology** — Azure VM SKUs come in fixed vCPU/memory tiers. When a rightsized target lands between tiers, a naive "must cover both dimensions strictly" approach forces a snap-up on *both* dimensions simultaneously, inflating the matched SKU (and its cost) far beyond what the workload needs. The engine uses an **asymmetric 3-pass cascade** instead:

1. **Pass 1 — Relaxed secondary dimension:** Each VM is classified as CPU-skewed (high vCPU, low memory — e.g. web/app servers) or memory-skewed (high memory, low vCPU — e.g. databases). The *primary* dimension is always covered in full; the *secondary* dimension is allowed to be up to `SKU tolerance %` below the rightsized target. This mimics the manual Xa2 analysis approach: try a slightly lower value on the non-bottleneck resource first to find a cheaper tier.
2. **Pass 2 — Strict both dimensions:** Standard match where both vCPU ≥ target and memory ≥ target.
3. **Final pick:** Cheapest result across Pass 1 and Pass 2. Pass 1 can only reduce or hold cost, never inflate it.

The trade-off is intentional and transparent: CPU-skewed VMs may carry slightly more memory than needed on the matched SKU; memory-skewed VMs may carry slightly more vCPU. This mirrors the "imprecise but least-cost" outcome you'd get from manual Xa2 analysis.

**What to review:**
- **Telemetry coverage** — % of VMs with per-VM CPU/memory telemetry vs host-proxy vs benchmark fallback
- **vCPU delta** — how much the rightsized vCPU count changed vs on-prem
- **Memory delta** — same for memory
- **Anomaly list** — VMs where the rightsized vCPU is more than 2× the source vCPU (likely balloon artefacts); review before approving
- **SKU tolerance** shown in the results caption — confirms what matching mode was used
- Cost preview: per-VM/hr rate, estimated Azure run-rate

**Override options** (expand ⚙️ Layer 2 Overrides):
| Override | Effect |
|---|---|
| CPU Headroom % | Headroom added above P95 CPU (default 20%) |
| Memory Headroom % | Headroom added above P95 memory (default 20%) |
| Fallback % | CPU/memory retention when telemetry absent |
| Storage Mode | Switch between Per-VM and Fleet-aggregate |
| **SKU match tolerance %** | **How far below rightsized target the secondary dimension may be in Pass 1 (default 20%). Set to 0% for original strict matching.** |

Click **✅ Approve Rightsizing — Proceed to Financial Model** to continue.

---

#### Layer 3 — Financial model checkpoint

**What happens:** The full 10-year (or 5-year) P&L and cash-flow financial model runs, including status quo TCO, retained costs, depreciation, migration ramp, IT productivity, and NII.

**What to review:**
- **Headline KPIs**: Project NPV, ROI (5Y CF), Payback period, Azure cost/VM/yr
- **Cost comparison chart**: On-Prem vs Azure annual costs with CF savings annotation
- **Sanity checks**: sign consistency, payback within horizon, Azure < On-Prem per VM

**Override options** (expand ⚙️ Layer 3 Overrides):
| Override | Effect |
|---|---|
| WACC % | Discount rate for NPV (default 7%) |
| DCs to Exit | Number of datacentres decommissioned |
| Horizon | Switch between 5-year and 10-year analysis |
| ACO / ECIF Credits | Year-by-year Azure funding credits |

**Scenario comparison:** Use the **Add Scenario** button to run an alternative set of Layer 3 overrides side-by-side. Named scenarios appear as additional columns in the KPI table and additional lines on the chart.

Click **✅ Approve Financial Model — Proceed to Export** to continue.

---

#### Layer 4 — Export

Download a formatted **PowerPoint** (dark-theme deck with KPI cards + 5Y/10Y charts) or a pre-filled **Excel** (Template v6 yellow cells populated; user recalculates macros in Excel).

### Option B — Manual Intake (step-by-step)

Walk through the numbered steps in order:

| Step | Purpose |
|---|---|
| 1 · Client Intake | Enter customer name, currency, VM/server inventory |
| 2 · Consumption Plan | Set migration horizon, Azure sizing, ACO/ECIF |
| 3 · Benchmarks | Review / override all 57+ cost assumptions |
| 4 · Results | View full financial analysis |
| 5 · Export | Download PPTX / Excel |
| Fact Checker | Upload saved workbook for engine ↔ Excel parity check |

---

## 6. Verify the installation (run tests)

```bash
# From the project root, with the venv active:
python -m pytest tests/ -q
```

Expected output: **64 passed** (with RVTools file present in root).

Without the RVTools file, RVTools-dependent tests are skipped.

---

## 7. Benchmark overrides

All 57 cost benchmark assumptions are editable in **3 · Benchmarks** without touching code.
They persist in session state for the duration of the browser session.

To make overrides permanent across sessions, edit:
```
data/benchmarks_default.yaml
```

---

## 8. Azure Pricing

The engine fetches live PAYG pricing from the **Azure Retail Prices API** for the
inferred region and caches results for 24 hours under `.cache/azure_prices/`.

If the API is unreachable (offline, rate-limited), the engine falls back to the
benchmark defaults from `data/benchmarks_default.yaml`. The pricing source is
shown on the Agent Intake results page under "Pricing source."

---

## 9. Quick reference — what RVTools columns are used

| Sheet | Columns used |
|---|---|
| vInfo | VM, Powerstate, Template, CPUs, Memory, In Use MiB, OS (config), OS (VMware Tools), Application, Environment |
| vHost | Host, Datacenter, # Cores, # Memory, vCPUs per Core, Time Zone Name, GMT Offset, Domain |
| vCPU | Powerstate, Max, Overall (→ CPU P95 utilisation) |
| vMemory | Powerstate, Size MiB, Consumed (→ memory P95 utilisation) |
| vDisk | VM, Powerstate, Template, Capacity MiB (→ per-VM provisioned disk for tier costing) |
| vMetaData | Server (→ vCenter FQDN for region inference) |

All other sheets are ignored.

---

## 10. Sharing with colleagues (Streamlit Community Cloud)

The app is deployed at **[bv-benchmark-bizcase.streamlit.app](https://bv-benchmark-bizcase.streamlit.app)** — no install required.

Anyone with the link and a browser can use the full 3-layer checkout wizard. No Python, no Git, no local setup.

### What colleagues need
- The URL above
- An RVTools `.xlsx` export for their customer
- Nothing else — the app runs entirely in the cloud

### Re-deploying after code changes
Pushes to `main` trigger an automatic redeploy on Streamlit Cloud (usually 2–3 minutes). No action needed.

### Running the app locally instead
If Streamlit Cloud is unavailable or you want to use a local RVTools file without uploading it, run locally:

```bash
streamlit run app/main.py
```

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| `Module not found` error on startup | Ensure venv is active: `source .venv/bin/activate` |
| `streamlit: command not found` | Run `pip install -r requirements.txt` in the active venv |
| Azure pricing shows "benchmark" source | API unreachable or rate-limited — results are still valid using benchmark defaults |
| ESU pCores show ⚠ warning | RVTools OS column lacks version strings for some Windows VMs. Override the ESU pCore count in Step 3 · Benchmarks if you have a separate OS audit. |
| SQL count shows "estimated (10% default)" | No `Application` custom attribute data in the RVTools export. Override SQL pCores in Step 3 · Benchmarks. |
| SQL shows "assumed production — no env tags" | No `Environment` tags found in the inventory. **All Windows Server and SQL VMs are treated as Production by default.** This is the correct assumption when tagging is absent. If the estate includes Dev/Test workloads, tag VMs in vCenter (Environment = "Dev" / "Test"), re-export RVTools, and re-upload. |
| PowerPoint charts are text placeholders | Install `kaleido`: `pip install kaleido` then re-export |
| Excel export opens without macros | By design — openpyxl strips VBA. Use as a data reference; re-run macros in the original template if needed. |
| `Template not found` on Excel export | Place `Template_BV Benchmark Business Case v6.xlsm` in the project root |
