# BV Benchmark Business Case — Demo Cheat Sheet

## Starting the App

```bash
cd /path/to/bv-benchmark-bizcase
source .venv/bin/activate
streamlit run app/main.py
```

Opens at **http://localhost:8501** in your browser.

---

## Step-by-Step Demo Flow

### Step 1 · Client Intake
**What to fill in:**
- Client Name (e.g. `Contoso Corp`)
- Currency (leave `USD`)
- VMs to migrate (e.g. `500`)
- Physical servers (e.g. `50`)
- vCPU total (e.g. `4000`)
- Memory GB total (e.g. `12000`)
- Storage TB total (e.g. `500`)
- DC locations (e.g. `2`)

**What you can skip for a quick demo:** software stack ratios, ESU/SQL fields — defaults cover a typical mixed Windows estate.

Click **"💾 Save"** at the bottom.

---

### Step 2 · Consumption Plan
**What to fill in:**
- Migration horizon (stick with 36 months for most demos)
- Ramp: leave defaults or match a known migration schedule
- ACO / ECIF: enter if you have real numbers; 0 is fine for a first pass

Click **"💾 Save"**.

---

### Step 3 · Benchmarks *(usually skip in live demos)*
Leave all defaults unless the customer has challenged a specific assumption (e.g. "our hardware lifecycle is 7 years, not 5").

If you do change something, the **"↺ Reset to Defaults"** button is at the top to undo everything at once.

---

### Step 4 · Results
**This is the money slide.** Walk through the tabs in order:

| Tab | Talking point |
|-----|---------------|
| **📊 Exec Summary** | Lead with **CF NPV (10-Year)** and **Payback**. Use the dual 5Y / 10Y charts to show cost crossover. Waterfall shows *where* the savings come from. |
| **💰 Cash Flow** | If pressed on methodology — CAPEX is actual spend year of purchase, not depreciated. Toggle 5Y / 10Y. |
| **📋 P&L** | Use only if the customer's finance team wants the depreciation view. |
| **🔍 Fact Check** | If you've pre-filled the Excel template and saved it, upload it here to prove the engine matches the spreadsheet. |
| **📽️ Present** | Switch here for screen-share. Press **F11** in browser for full-screen. All charts and KPIs, no app chrome. |

---

### Step 5 · Export
- **Download PowerPoint** → hand-off deck, dark theme, 2 slides: KPI cards + both charts (Slide 1), annual cashflow table (Slide 2).
- **Download Excel** → pre-filled template; customer's finance team can run the macros themselves.

---

## Common Objections & Quick Answers

| Objection | Where to point |
|-----------|---------------|
| "Your hardware cost assumptions are wrong for us" | Step 3 · Benchmarks → Server & Storage Costs expander |
| "We have a longer refresh cycle" | Step 3 · Hardware Lifecycle & Sizing |
| "Our Azure pricing is different" | Step 3 · Azure Pricing Fallbacks |
| "Show me the math on P&L vs. Cash" | Results → toggle between Cash Flow and P&L tabs |
| "Can I get this in a file?" | Step 5 → Export as PPTX or Excel |

---

## Things That Look Like Bugs But Aren't

- **Retained CAPEX drops to zero mid-table** — that's correct; once migration completes, no new on-prem hardware purchases.
- **Migration cost only appears in early years** — one-time ramp cost, expected.
- **P&L NPV is lower than CF NPV** — depreciation spreads the hardware cost over multiple years, which reduces apparent early-year savings; both views are correct.
- **Fact Check shows SKIP for some rows** — those cells weren't cached in the uploaded workbook. Open the file in Excel, press Ctrl+Alt+F9 (full recalc), save, then re-upload.

---

## Data to Have Ready Before a Live Customer Demo

1. Rough VM count (or server count if VMs unknown)
2. Approximate vCPU and memory totals — or just a "VMs per physical" ratio
3. Storage footprint in TB
4. Number of datacenter locations
5. Microsoft contract info (if ACO / ECIF credits apply)

Everything else can default.
