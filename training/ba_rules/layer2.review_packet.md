# Layer 2 Rule Book — Review Packet

**For:** Business Analyst (Ozzie)
**Prepared by:** Engineering, 2026-04-27
**Sources cited (DUAL):**
1. **Microsoft Azure R&D** — "VMware/OnPrem to Azure Mappings" deck.
   Verbatim formulas captured at
   [training/baseline_workflow/azure_rd_rightsizing/canonical_formulas.md](../baseline_workflow/azure_rd_rightsizing/canonical_formulas.md).
2. **BA Layer 2 transcript** —
   [training/baseline_workflow/layer2_azure_match/transcript.vtt](../baseline_workflow/layer2_azure_match/transcript.vtt).

**Rule book:** [training/ba_rules/layer2.yaml](layer2.yaml)

---

## TL;DR

15 rules in 7 sections, 6 cross-cutting key principles. **Every rule cites at least one source.** 5 rules cite the R&D deck verbatim by anchor (e.g. `R&D.SLIDE7.CPU.DEFAULT`); the remaining rules either cite the BA transcript only (BA-specific carve-outs) or both.

| Status | Count | Meaning |
|--------|-------|---------|
| ✅ MATCH | 1 | Engine implements correctly today |
| ⚠️ PARTIAL | 7 | Engine implements part of it; gap noted |
| 🔨 UNIMPLEMENTED | 7 | Net-new for Layer 2 work |
| ❌ DELTA | 0 | (no engine deviations from canonical) |

The 7 UNIMPLEMENTED rules are the substance of the Layer 2 convergence work. None of them are surprises — they're the R&D formulas + BA carve-outs the engine hasn't yet adopted.

---

## What's new vs Layer 1

### 1. R&D-sanctioned rightsizing — every CPU/Memory/Storage decision cites the slide deck

Per your instruction:
> ensure to annotate everywhere we are following the R&D-sanctioned rightsizing calculation … as a form of traceability and source citation.

Every rightsizing rule has a `citations[]` entry pointing to the canonical formula in `canonical_formulas.md`:

| Rule | R&D anchor | BA carve-out (additive) |
|---|---|---|
| `L2.RIGHTSIZE.CPU.001` | `R&D.SLIDE7.CPU.DEFAULT`, `R&D.SLIDE7.CPU.USER` | host-proxy gated, BA reduction defaults |
| `L2.RIGHTSIZE.MEMORY.001` | `R&D.SLIDE8.MEMORY.DEFAULT`, `R&D.SLIDE8.MEMORY.USER` | vMemory-absent fallback (R&D doesn't cover) |
| `L2.RIGHTSIZE.STORAGE.001` | `R&D.SLIDE9.STORAGE.DEFAULT`, `.CAPACITY`, `.VINFO` | (none — R&D chain is complete) |
| `L2.REGION.001` | `R&D.OPTIONAL.REGION` (slide 3) | (none) |
| `L2.FAMILY_PIN.001` | `R&D.OPTIONAL.FAMILY_PROCESSOR` (slide 5) | (none) |

R&D anchors are STABLE — they don't change unless the deck is revised. If R&D ships v2 of the formulas, only the canonical doc + the affected `engine_status_note` need updating; rule semantics are versioned via the anchor name.

### 2. The 8-vCPU floor is now OS-conditional via flag — `KP.WIN_8VCPU_MIN`

Per your clarification (verbatim in `KP.WIN_8VCPU_MIN.statement`):

```
flag = enforce_8vcpu_min_for_windows_server  (default True)

flag=True,  OS matches "Windows Server"  →  min_vcpus = 8
flag=True,  OS empty / non-Windows       →  min_vcpus = 1   (Linux assumed)
flag=False, any OS                        →  min_vcpus = 1
```

Windows detection reuses the engine's existing regex
`re.compile(r"windows\s+server", re.IGNORECASE)` against vInfo OS columns —
so this rule is consistent with how Layer 1 already counts Windows pCores
for licensing. No new pattern.

The choice is recorded per VM as `vcpu_floor_source`:
`'windows_compliance' | 'linux_or_unknown' | 'flag_off'` — fully auditable.

This is rule **`L2.RIGHTSIZE.CPU.WINDOWS_FLOOR`** with `source_conflict`
explicitly noted: R&D deck specifies a generic `min_vcpus`; the BA carve-
out makes it OS-conditional. The R&D formula itself is unchanged.

### 3. The "host-proxy" trap from v1.3.0 is now properly gated

R&D's `R&D.SLIDE7.CPU.DEFAULT` IS the host-proxy formula we disabled in M1
because it produced 836 over-sized SKU matches in Customer A. Rather than drop
the R&D formula (which would conflict with our dual-citation principle),
the rule book gates it behind a BA decision: `KP.HOSTPROXY_GATE`.

```
ba_review_packet.utilisation_strategy ∈ {'host_proxy',
                                          'flat_reduction',
                                          'per_vm_telemetry'}
```

The L1 review packet already surfaces this choice (your prior `L1.UTIL.005`
clarification). Layer 2 just consumes the BA's selection — never silently
applies host proxy.

### 4. BA's manual XA2 retry behaviour is now codified

Your transcript at 00:06:10 / 00:13:00 / 00:08:46 describes the iterative
"decrement vCPU by 1 until match, then increment memory by 1 if needed,
never over-inflating" pattern (the green/yellow cells in your spreadsheet).
This is captured as `L2.RIGHTSIZE.CPU.RETRY` and feeds per-VM diagnostics
so the BA can audit which VMs needed adjustment.

### 5. The pricing offer matrix is explicit (`KP.PRICING_OFFER_MATRIX`)

Five offers per VM: PAYG, RI-1y, RI-3y, SP-1y, SP-3y. Missing offers are
flagged not faked. PAYG remains the headline. The four reserved/savings
totals shown alongside. Engine today only surfaces PAYG at the aggregate
level — the per-VM breakdown is `UNIMPLEMENTED`.

### 6. Multi-disk decomposition for large-storage VMs

Your Customer A example (~203 TB / 42 disks → 25× E60 LRS) is captured as
`L2.RIGHTSIZE.STORAGE.MULTI_DISK`. R&D's formula returns a single
`rs_disk_gib`; the BA workflow decomposes it across the largest practical
tier. LRS is the default redundancy (per BA at 00:19:30). This is an
engineering extension on top of R&D, explicitly flagged in `source_conflict`.

---

## Conflicts between R&D and BA, and how they were resolved

The rule book uses a `source_conflict` block whenever R&D and BA practice
diverge. Three conflicts captured in this draft:

| Rule | R&D says | BA says | Resolution |
|---|---|---|---|
| `L2.RIGHTSIZE.CPU.WINDOWS_FLOOR` | generic `min_vcpus` parameter | 8-vCPU floor only for Windows Server | BA carve-out is additive — sets the value of `min_vcpus` per VM before the R&D formula runs |
| `L2.RIGHTSIZE.MEMORY.001` | "default" branch reads `vMemory[Consumed]`; doesn't say what to do when vMemory tab missing | Use BA-approved memory reduction% on `vInfo[Memory]` | Engineering extension: vMemory-missing → fall through to `R&D.SLIDE8.MEMORY.USER`. Documented in `fallbacks[]` |
| `L2.RIGHTSIZE.STORAGE.MULTI_DISK` | single `rs_disk_gib` output | Decompose across multiple disks at largest practical tier | Engineering extension: post-process `rs_disk_gib` through tier decomposition. Recorded as a separate rule, not a modification to R&D's |

In every case, the R&D formula is preserved verbatim. BA practice is layered on top as an additive guard.

---

## Gaps that need your input before Phase 2 / Layer 2

✅ **All three gating questions answered by BA on 2026-04-27. Phase 2 / Layer 2 is unblocked.**

### A. Default reduction percentages — RESOLVED

> *"the 'ozzie's guess' RS factors would have been 40% of cpu count, 60% of memory count, 80% of storage count. these are totally wild guesses. I prefer using R&Ds formulas based on whatever scenarios with available or unavailable metadata and tabs in the rvtools. in a last-ditch fallback, the above ozzie guesses but to be made editable by the BA."*

**Codified as `KP.BA_FALLBACK_REDUCTIONS`:**

```
ba_fallback_cpu_retained_pct    = 40   →  cpu_reduction_pct    = 60
ba_fallback_mem_retained_pct    = 60   →  mem_reduction_pct    = 40
ba_fallback_storage_retained_pct = 80  →  storage_reduction_pct = 20
```

These apply ONLY when strategy resolves to `ba_fallback` (no telemetry,
no BA-specified reductions). BA-editable per engagement.

### B. Customer A rightsizing strategy — RESOLVED

> *"the training data is basically like-for-like Azure VM mapping apart from the several edge cases I voiced about needing to adjust cpu count up/down or memory up/down from the video and transcripts."*

**Codified as `KP.STRATEGY_PRECEDENCE`** with five strategies:
`per_vm_telemetry > flat_reduction > like_for_like > ba_fallback` (host_proxy
never auto-picked). Customer A replica baseline uses `like_for_like` + the manual
adjustments from `L2.RIGHTSIZE.CPU.RETRY`.

### C. Customer A 8-vCPU floor enforcement — RESOLVED

> *"for Customer A case, I did NOT follow '8 vcpu minimum' for Windows Server source VMs. So OFF. we'll want this as a approval option for the BA to see with and without strict enforcement."*

**`enforce_8vcpu_min_for_windows_server` default flipped to FALSE** in
`KP.WIN_8VCPU_MIN`. Customer A baseline records `flag = False` explicitly. BA
review packet exposes the toggle so customers can see both views.

---

## How this fits the Phase 2 / Layer 2 plan

Once you sign off this rule book and answer A/B/C above, the Phase 2
deliverables are:

1. `training/replicas/layer2_ba_replica.py` — implements every R&D formula
   as a separately named function (`rd_slide7_cpu_default`, etc.) plus the
   BA carve-outs. Engine-independent oracle.
2. `training/baselines/customer_a_2024_10/ba_expected.yaml` — extend with Layer 2
   targets: azure_vcpu 16,318 / RAM 76,068 GB / Storage 4,572,825 GB / PAYG
   $6,704,247 / Storage $4,115,545.
3. `training/parity/run_layer2_parity.py` — three-way diff (replica vs BA
   vs engine).
4. Iterate until the **replica matches the BA at 0.00%** on the per-VM and
   aggregate fields.
5. Then converge the engine through targeted fixes against the same
   parity test.

Same recipe as Layer 1, same "stellar not kinda works" gate.

---

## Where to find everything

- **R&D canonical formulas (verbatim)**: [`training/baseline_workflow/azure_rd_rightsizing/canonical_formulas.md`](../baseline_workflow/azure_rd_rightsizing/canonical_formulas.md) — every formula with stable `R&D.SLIDE*.*` anchors.
- **R&D source provenance**: [`training/baseline_workflow/azure_rd_rightsizing/source.txt`](../baseline_workflow/azure_rd_rightsizing/source.txt).
- **BA Layer 2 transcript**: [`training/baseline_workflow/layer2_azure_match/transcript.vtt`](../baseline_workflow/layer2_azure_match/transcript.vtt).
- **Rule book**: [`training/ba_rules/layer2.yaml`](layer2.yaml).
- **This packet**: [`training/ba_rules/layer2.review_packet.md`](layer2.review_packet.md).
