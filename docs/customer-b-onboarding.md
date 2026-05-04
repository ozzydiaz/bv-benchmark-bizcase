# Customer B Onboarding — 3-Way Audit Procedure

> **Purpose:** When the BA delivers a fully completed second customer workbook,
> follow this procedure to add it to the Layer 3 parity ratchet without
> destabilising Customer A's zero-drift guarantee. Total time: hours, not days.
>
> **Pre-requisites:** v1.5.0-layer3-zero-drift on `main`; pre-Step-15 work
> already merged (pmem invariant test, v1.6/v1.7 scaffolds, threat model).

---

## The 5-Step Procedure

### Step 1 — Place the file

The customer workbook is **gitignored** (it contains real customer data). Drop
it at the repo root with a deterministic name:

```bash
# Customer A is at:    customer_a_BV_Benchmark_Business_Case_v6.xlsm
# Customer B goes at:  customer_b_BV_Benchmark_Business_Case_v6.xlsm
cp ~/Downloads/<received-file>.xlsm \
   /Users/ozdiaz/dev/bv-benchmark-bizcase/customer_b_BV_Benchmark_Business_Case_v6.xlsm
```

Verify the `.gitignore` already excludes the new path (it does — the rule
`*.xlsm` excludes all xlsm files with a single `!Template_BV Benchmark Business Case v6.xlsm`
allow-list exception). Quick sanity check:

```bash
git check-ignore customer_b_BV_Benchmark_Business_Case_v6.xlsm
# Expected output: customer_b_BV_Benchmark_Business_Case_v6.xlsm
```

If the command prints nothing, **stop and add the path to `.gitignore` BEFORE
any `git status`** — Customer data must never enter Git history.

---

### Step 2 — Run the replica oracle, dry

The first sanity-check is whether the BA workbook can be parsed at all by the
golden extractor. This is the same oracle Customer A uses; if Customer B's BA
filled cells differently or used merged cells in unusual places, the extractor
will surface that here before parity is even attempted.

```bash
python - <<'PY'
from training.replicas.layer3_golden_extractor import extract_layer3_golden, flatten_golden
g = extract_layer3_golden("customer_b_BV_Benchmark_Business_Case_v6.xlsm")
flat = flatten_golden(g)
print(f"Extracted {len(flat)} oracle cells from Customer B")
print(f"First 5: {list(flat.items())[:5]}")
PY
```

**Expected:** ~395 cells (matches Customer A coverage).
**Failure modes:**
- `KeyError` on a sheet name → BA renamed a sheet; update the extractor.
- `TypeError: NoneType` on a cell read → BA left a yellow cell empty; update
  the workbook OR add a defensive default in the extractor.
- Cell count drifts by >5% → BA added/removed scenarios; investigate before
  proceeding.

---

### Step 3 — Capture the parity baseline

Run the existing 29-test Layer 3 parity suite against Customer B by
parameterizing the workbook fixture. The recommended path is a **single-line
edit** to `tests/test_layer3_parity.py` — add Customer B as a second
parametrized fixture:

```python
# tests/test_layer3_parity.py — find the existing fixture:
@pytest.fixture(scope="module")
def golden():
    if not CUSTOMER_A_WORKBOOK.exists():
        pytest.skip(f"Customer A workbook not found at {CUSTOMER_A_WORKBOOK}")
    return extract_layer3_golden(str(CUSTOMER_A_WORKBOOK))

# REPLACE WITH:
CUSTOMER_B_WORKBOOK = REPO_ROOT / "customer_b_BV_Benchmark_Business_Case_v6.xlsm"

@pytest.fixture(
    scope="module",
    params=[
        pytest.param(CUSTOMER_A_WORKBOOK, id="customer_a"),
        pytest.param(CUSTOMER_B_WORKBOOK, id="customer_b"),
    ],
)
def golden(request):
    if not request.param.exists():
        pytest.skip(f"Workbook not found at {request.param}")
    return extract_layer3_golden(str(request.param))
```

Then run **with verbose IDs** so you can see which customer fails which test:

```bash
python -m pytest tests/test_layer3_parity.py -v 2>&1 | tee /tmp/customer_b_baseline.txt
```

**Expected:** 58 tests collected (29 × 2 customers); ideally 58 pass.

**If Customer B drifts:** the diff is the contract gap. Save the output;
proceed to Step 4.

---

### Step 4 — Diff Customer B vs Customer A

The 3-way auditor (`training/replicas/layer3_judge.py`) emits a structured
report. Capture both customers' reports and diff:

```bash
python scripts/_smoke_layer3_judge.py customer_a_BV_Benchmark_Business_Case_v6.xlsm \
    > /tmp/audit_a.txt
python scripts/_smoke_layer3_judge.py customer_b_BV_Benchmark_Business_Case_v6.xlsm \
    > /tmp/audit_b.txt
diff /tmp/audit_a.txt /tmp/audit_b.txt | less
```

> **Note:** `scripts/_smoke_layer3_judge.py` currently hard-codes Customer A
> at line 13. Genericise it as part of Step 15 — accept the workbook path as
> `sys.argv[1]`, default to Customer A.

**Categorise each Customer-B-only failure into one of three buckets:**

| Bucket | Symptom | Action |
|---|---|---|
| **(a) Customer A bias** | A heuristic was tuned to Customer A's specific values (e.g., a hard-coded BU count, a mix percentage). | Refactor the heuristic; **do not** widen tolerance. Re-run both customers; both must stay at zero drift. |
| **(b) Genuine Customer B variance** | BA legitimately filled a cell differently (e.g., different `wacc`, different consumption mix). | This is fine — the engine should recompute and match. If it doesn't, the engine has a bug. Fix and re-run. |
| **(c) BA workbook deviation** | Customer B's BA used a non-template formula, added a custom row, or skipped a section. | Out-of-scope for the engine. Document in `version-history.md` as a known limitation; do **not** raise `MAX_ENGINE_DRIFT`. |

---

### Step 5 — Ratchet `MAX_ENGINE_DRIFT` only after both pass

The Layer 3 ratchet at `tests/test_layer3_parity.py` line 728 is locked:

```python
MAX_ENGINE_DRIFT = 0
```

**Do NOT raise this number to "make Customer B pass".** That defeats the entire
audit. The ratchet only ratchets DOWN. Either:

1. Customer B passes at `MAX_ENGINE_DRIFT = 0` → commit the parametrization
   change. Push. Done.
2. Customer B fails → fix the engine OR document the deviation as bucket (c)
   above and **skip the failing tests for Customer B specifically**:
   ```python
   @pytest.mark.parametrize("workbook_id", ["customer_a", "customer_b"])
   def test_engine_parity(workbook_id, ...):
       if workbook_id == "customer_b" and request.node.callspec.id == "<specific test>":
           pytest.skip("Customer B uses non-template <foo> formula — bucket (c)")
       ...
   ```

After both pass at zero drift, **tag the release**:

```bash
git tag -a v1.5.1-customer-b-onboarded -m "Customer B onboarded at zero engine drift"
git push origin v1.5.1-customer-b-onboarded
```

---

## Decision Tree: When to Trigger Each Backlog Item

The pre-Step-15 work was deliberate setup for these decisions:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Customer B 3-way audit complete — what next?                         │
└──────────────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────┴─────────────┐
                │                           │
        Zero engine drift              Bucket (a) or (b)
                │                       drift surfaced
                ▼                           │
   ┌──────────────────────┐                 ▼
   │ v1.6 + v1.7 are now  │     ┌────────────────────────────┐
   │ unblocked.           │     │ FIX the engine (NOT widen   │
   │                      │     │ tolerance). Re-run both     │
   │ Order:               │     │ customers. Re-baseline.     │
   │  1. v1.6 (TV) first  │     │                             │
   │     — config-only,   │     │ Then return to top of tree. │
   │     low risk         │     └────────────────────────────┘
   │  2. v1.7 (RI/SP)     │
   │     after v1.6 ships │
   │     and Customer A+B │
   │     re-baselined     │
   │                      │
   │ Use the scaffold     │
   │ tests in:            │
   │  tests/test_v16_*    │
   │  tests/test_v17_*    │
   │                      │
   │ Required reading:    │
   │  docs/v16-v17-       │
   │   threat-model.md    │
   └──────────────────────┘
```

---

## Files & Locations Quick Reference

| Asset | Location | Notes |
|---|---|---|
| Customer A workbook | `customer_a_BV_Benchmark_Business_Case_v6.xlsm` | Gitignored. |
| Customer B workbook | `customer_b_BV_Benchmark_Business_Case_v6.xlsm` | To be added. Must be in `.gitignore`. |
| Layer 3 ratchet | `tests/test_layer3_parity.py:728` | `MAX_ENGINE_DRIFT = 0` (locked). |
| Layer 3 fixture | `tests/test_layer3_parity.py:50-53` | Parameterize for Customer B in Step 3. |
| Golden extractor | `training/replicas/layer3_golden_extractor.py` | Sole pull from BA workbook. |
| 3-way auditor | `training/replicas/layer3_judge.py` | Tiered tolerance bands. |
| Smoke script | `scripts/_smoke_layer3_judge.py` | Currently hard-codes Customer A path; genericise. |
| pMemory invariant | `tests/test_engine.py::TestStatusQuo::test_pmemory_invariant_d49_d50_d52` | Pre-Step-15 regression guard. |
| v1.6 scaffold | `tests/test_v16_tv_method_scaffold.py` | 7 tests, all skipped today. |
| v1.7 scaffold | `tests/test_v17_ri_sp_blending_scaffold.py` | 5 tests, all skipped today. |
| Threat model | `docs/v16-v17-threat-model.md` | Required reading for v1.6/v1.7 PRs. |
| Roadmap | `version-history.md` (top section) | v1.6/v1.7/v2.0 backlog. |

---

## Failure-Mode Quick Reference

| Symptom | Most likely cause | First check |
|---|---|---|
| `KeyError` extracting Customer B | BA renamed a sheet | Open workbook; compare sheet tabs to Customer A |
| 5+ engine-drift cells | Engine has a Customer-A-only assumption | Check the failing cells against `docs/v16-v17-threat-model.md` Section F (hidden coupling) |
| Negative `terminal_value` for Customer B | Customer B's Y10 savings are negative | This is **expected**; verify it matches the BA workbook. Do NOT enable `tv_floor_at_zero` (that's a v1.6 opt-in only). |
| `acd > 1.0` validation error | BA hand-typed >100% discount | Cap at workbook source; do not relax the validator (see threat-model section E). |
| Customer A passes, Customer B does too, but `git diff` shows tolerance changes | Someone widened `MAX_ENGINE_DRIFT` | **Revert immediately.** The ratchet only goes down. |

---

## What This Procedure Deliberately Does NOT Do

- **Does not run v1.6 or v1.7 refactors.** Both are gated on Customer B
  passing at zero drift FIRST. See `version-history.md` Roadmap section.
- **Does not auto-merge.** Every step requires human review of the diff.
- **Does not extend the engine for Customer B specifically.** If Customer B
  needs a new feature, that's a separate PR with its own scaffold + tests.
- **Does not loosen tolerances.** The ratchet is one-directional.

---

*Last updated: 2026-05-04. Anchored to the May 2026 risk analysis and adversarial threat model.*
