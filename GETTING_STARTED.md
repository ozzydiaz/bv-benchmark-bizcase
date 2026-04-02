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
| Python 3.11 or later | Tested on 3.14.2 |
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

1. Click **⚡ Agent Intake** in the sidebar
2. Enter the **customer name** and **currency**
3. Upload the **RVTools .xlsx** export
4. Optionally expand *"⚙️ Optional Parameters"* to set migration horizon, ACO/ECIF credits, or number of DCs to exit
5. Click **⚡ Parse & Build Business Case**

The engine runs automatically. You will see:
- Parsed inventory summary (VMs, hosts, vCPUs, memory, storage)
- OS and license profile (Windows, ESU, SQL with Prod/Non-Prod classification — Production assumed when no environment tags present)
- Azure right-sizing (P95 utilisation-based or fallback)
- Business case KPI preview + 5-Year cost comparison chart

Then navigate directly to:
- **4 · Results** — full interactive analysis (5 tabs)
- **5 · Export** — download PowerPoint or pre-filled Excel

### Option B — Manual Intake (step-by-step)

Walk through the numbered steps in order:

| Step | Purpose |
|---|---|
| 1 · Client Intake | Enter customer name, currency, VM/server inventory |
| 2 · Consumption Plan | Set migration horizon, Azure sizing, ACO/ECIF |
| 3 · Benchmarks | Review / override all 57 cost assumptions |
| 4 · Results | View full financial analysis |
| 5 · Export | Download PPTX / Excel |

---

## 6. Verify the installation (run tests)

```bash
# From the project root, with the venv active:
python -m pytest tests/ -q
```

Expected output: **51 passed** (with RVTools file present in root).

Without the RVTools file, 21 tests are skipped and 30 pass.

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

## 10. Troubleshooting

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
