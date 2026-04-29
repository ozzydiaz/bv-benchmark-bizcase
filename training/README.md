# BV Benchmark — BA Training Corpus

This directory is the **single source of truth** for the human business analyst
(BA) workflow that the engine in `engine/` is being trained to reproduce.

It exists because reverse-engineering `Template_BV Benchmark Business Case v6.xlsm`
gave us *outputs* but not the BA's *decision sequence and fallback rules*.
The transcripts here capture that decision sequence verbatim. Each engine
behaviour must be traceable to a numbered rule in the rule book, and each
rule must be traceable to a transcript timestamp.

---

## Customer-data privacy policy (READ FIRST)

The training corpus and parity baselines reference real customer
engagements. Customer-identifying tokens are STRICTLY FORBIDDEN in
tracked source. The repository uses anonymised aliases throughout:

| Anonymised alias | Used for |
|---|---|
| `customer_a` (folder/code) / `Customer A` (prose) | First reference engagement (large US enterprise) |
| `customer_b` … | Subsequent engagements |
| `<primary-datacenter>`, `<secondary-datacenter>`, … | Datacenter labels |
| `<host>`, `<cluster-name>` | Host / cluster labels |
| `customer_a_rvtools_2024-10-29.xlsx` | Reference RVTools file (gitignored) |
| `reference_BV_Benchmark_Business_Case_v6.xlsm` | Reference workbook (gitignored) |

Concrete safeguards:

- All `*.xlsx` / `*.xlsm` / `*.xlsb` files are **gitignored** (only the
  `Template_BV Benchmark Business Case v6.xlsm` template is tracked).
- The per-VM replica dump (`replica_per_vm.yaml`) and parity reports are
  **gitignored** because they include real hostnames and datacenter
  labels surfaced from the input file. Always regenerated locally.
- `tests/test_privacy_guard.py` is a CI gate that **fails the build**
  if any forbidden token (real customer name / datacenter / acronym)
  appears in tracked source. Extend `FORBIDDEN_PATTERNS` whenever a new
  engagement starts.
- Local paths to the gitignored input file are passed via
  `BV_PARITY_INPUT=<path>` env var (or the default
  `customer_a_rvtools_2024-10-29.xlsx` on disk if present).

If you need to onboard a new customer engagement, **rename the input
file to `customer_<letter>_rvtools_<YYYY-MM-DD>.xlsx` BEFORE you place
it anywhere near a tracked file**, and run `pytest tests/test_privacy_guard.py`
locally before any `git commit`.

---

## Layout

```
training/
├── baseline_workflow/          # raw BA training material
│   ├── layer1_ingest/
│   │   ├── transcript.vtt      # WebVTT, used for citations + grep
│   │   ├── transcript.docx     # original Teams export
│   │   └── recording.mp4.txt   # hint file — full mp4 lives outside the repo
│   ├── layer2_azure_match/
│   └── layer3_financials/
├── ba_rules/                   # extracted, human-reviewed rule books
│   ├── layer1.yaml             # ← deliverable of Phase 1, Layer 1
│   ├── layer2.yaml             # (Phase 1, Layer 2 — TBD)
│   └── layer3.yaml             # (Phase 1, Layer 3 — TBD)
├── replicas/                   # standalone Python oracles per Phase 2
│   ├── layer1_ba_replica.py    # (Phase 2, Layer 1 — TBD)
│   └── …
└── baselines/                  # known-good per-customer expected outputs
    └── customer_a_2024_10/
        ├── ba_expected.yaml         # values from BA's actual spreadsheet (TRACKED)
        ├── replica_outputs.yaml     # FYI summary (gitignored — fleet aggregates safe but path policy is exclude)
        ├── replica_per_vm.yaml      # per-VM dump (gitignored — contains hostnames)
        └── parity_report.md         # diff harness output (gitignored)
```

---

## Workflow

1. **New BA session?** Drop `.vtt`/`.docx`/`.mp4` into a new
   `baseline_workflow/<topic>/` folder.
2. **Extract rules** — author a YAML rule book under `ba_rules/`. Every entry
   must cite a transcript timestamp and (where applicable) name the engine
   field it governs and the BA's expected value on the Customer A reference sample.
3. **Author replica** — a self-contained Python script under `replicas/` that
   mechanically follows the rule book. **No imports from `engine/`.**
4. **Diff** — `tests/test_baseline_parity.py` runs both the engine and the
   replica on `baselines/customer_a_2024_10/rvtools.xlsx` and writes a diff report.
5. **Converge** — close the largest deltas first, commit, repeat.

See `../docs/theory-of-operation.md` for how this fits into the rest of the
codebase, and `../version-history.md` for what the engine changed in response.

---

## Rule book schema

Every entry in `ba_rules/layer*.yaml` has this shape:

```yaml
- rule_id: L1.STORAGE_PROV.001       # stable identifier — referenced by tests
  layer: 1                            # 1, 2, or 3
  topic: "On-prem provisioned storage"
  ba_action: |
    Sum vInfo Provisioned MiB across all VMs (powered-on + powered-off,
    including templates) and convert to decimal GB (÷ 953.674).
  inputs:                             # which RVTools tabs/columns are read
    - sheet: vInfo
      column_keywords: ["Provisioned MiB", "Provisioned", "Capacity"]
  output:
    engine_field: RVToolsInventory.total_storage_provisioned_gb
    excel_cell: "1-Client Variables!D42"
  fallbacks:
    - condition: "vInfo Provisioned column absent"
      action: "Fall back to In Use MiB / 953.674; emit parse_warnings entry."
  citations:
    - transcript: training/baseline_workflow/layer1_ingest/transcript.vtt
      timestamp: "00:06:06.527"
      excerpt: "for the on-premise counts, we use the provision MIB metadata per VM in vInfo"
  reference_expected: 4389810              # value from BA's spreadsheet (decimal GB)
  engine_status: DELTA                # one of MATCH | DELTA | UNIMPLEMENTED | NA
  engine_status_note: |
    Engine currently uses In Use MiB (not Provisioned) for total_storage_in_use_gb.
    Tracked as fix candidate.
```

`engine_status` is the **only field updated by the engine team** — everything
else is owned by the BA and frozen by review. When a rule's status flips from
`DELTA` to `MATCH`, the corresponding parity test must pin the value.

---

## Why videos matter (don't skip them)

The `.vtt` files capture *what was said*, but the videos capture *what was
clicked* — which tab, which filter, which cell range. The BA frequently
changes context without naming it ("wrong tab again, sorry, I was on the
V host tab"). When in doubt about a rule, watch the video at the timestamp
cited in the rule. The hint file in each folder points to the source mp4.
