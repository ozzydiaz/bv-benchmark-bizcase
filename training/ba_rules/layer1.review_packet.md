# Layer 1 Rule Book — Review Packet

**For:** Business Analyst (Ozzie)  
**Prepared by:** Engineering, 2026-04-27  
**Source video:** `training/baseline_workflow/layer1_ingest/recording.mp4` (~12 min)  
**Source transcript:** `training/baseline_workflow/layer1_ingest/transcript.vtt`  
**Rule book:** [`training/ba_rules/layer1.yaml`](../ba_rules/layer1.yaml)

---

## TL;DR

I extracted **22 rules** from the Layer 1 recording, organised into 9 sections.
Against the current engine:

| Status | Count | Meaning |
|--------|-------|---------|
| ✅ MATCH | 14 | Engine implements the rule correctly today |
| ⚠️ PARTIAL | 6 | Engine implements part of it; gap noted |
| ❌ DELTA | 2 | Engine deviates from your workflow |

**You need to confirm or correct the 6 PARTIAL + 2 DELTA rules** plus 4 rules
flagged with `[BA review needed]` in the YAML. The remaining 10 MATCH rules
just need a sign-off glance.

---

## How to review

Option 1 — **read the YAML directly** (recommended):
[training/ba_rules/layer1.yaml](../ba_rules/layer1.yaml).
Every rule has a verbatim transcript excerpt with timestamp.

Option 2 — read the summary table below; jump to the YAML only for the
rules you want to change. When you sign off a rule, either reply with
its `rule_id` or set `reviewed: true` in the YAML.

---

## The 8 rules that NEED your eyes

These are sorted by impact on the Customer A deltas you flagged.

### 1. ❌ `L1.STORAGE_PROV.001` — On-prem TCO storage source

**My understanding from the transcript at 00:06:06:**
> "for the on-premise counts, we use the **provision MIB** metadata per VM in vInfo"

**Engine today:** uses **In Use MiB** for the on-prem TCO storage summary.

**Impact:** Customer A expected 4,389,810 GB vs engine 4,034,346 GB — 8% under.
This is also the rule you wrote up in `customer_a-analysis-feedback.md`.

**Question for you:**
> Is "vInfo Provisioned MiB" the canonical source for the on-prem TCO storage
> number? Or does Layer 3 actually pull from vPartition Capacity? (vPartition
> shows 4,572,825 GB in your feedback, which is *closer to but not equal to*
> the 4,389,810 you cited.)

---

### 2. ⚠️ `L1.PARTITION.001` — vPartition aggregate scope

**Transcript at 00:10:39** describes vPartition as having all volume data
for managed-disk sizing.

**Engine today:** aggregates `total_partition_capacity_gb` for **powered-on
VMs only**.

**Impact:** Customer A expected 4,572,825 GB vs engine 3,081,602 GB — 33% under.

**Questions for you:**
1. Should the fleet aggregate include powered-off VMs too (matching your
   "they paid for it, it depreciates" rationale)?
2. Is vPartition Capacity the source for the Layer 3 on-prem TCO storage,
   or only for Layer 2 Azure managed-disk sizing?

---

### 3. ❌ `L1.INPUT.006` — Column-name fuzzy matching

**Transcript at 00:07:48 + 00:08:11:**
> "It might say provisioned, it might say capacity, it may or may not include MIB
> in the title... Maybe it might say utilized. So we have to use similar keywords
> or analogous keywords to find the right columns"

**Engine today:** matches column headers by **exact string only** (e.g.
must literally say `"Provisioned MiB"`).

**Impact:** silent failure on customer files with abbreviated headers; another
"all zeros" failure mode.

**Questions for you:**
1. What synonym sets do you actually use? My best guesses:
   - `Provisioned MiB` ↔ `Provisioned`, `Capacity`, `Provisioned (MiB)`
   - `In Use MiB` ↔ `In Use`, `Utilized`, `Utilised`, `Used`
   - `Memory` ↔ `Memory MiB`, `Memory (MiB)`, `RAM`, `RAM MiB`
2. Are there any cases where the wrong-column match would be worse than
   no match (e.g. picking `Capacity GiB` and treating it as MiB)?

---

### 4. ⚠️ `L1.UTIL.001` — Per-VM utilisation embedded in vInfo

**Transcript at 00:03:14** says utilisation can come from **vinfo and/or
vhost and/or vpartition**.

**Engine today:** scans separate vCPU and vMemory tabs (per-VM) and vHost
(per-host). It does **not** look at vInfo for embedded "CPU usage %" or
"Memory usage %" columns.

**Question for you:**
> Have you ever seen RVTools exports that put per-VM utilisation directly
> in vInfo? If yes, what column names? If no, we can downgrade this to NA.

---

### 5. ⚠️ `L1.UTIL.005` — Host CPU% as proxy for per-VM utilisation

**Transcript at 00:09:27:**
> "it's debatable as to whether we can apply the CPU usage percentage from
> the V host... to those VMs running on that host. That's one way to do it,
> and we should calculate it and present that out to the business analyst"

**Engine today:** captures `host_cpu_util` but the host-proxy *application*
path was disabled in v1.3.0 (see `version-history.md`). Layer 2 currently
falls through to a flat fallback factor.

**Question for you:**
> The host-proxy path was removed because of a bug. Do you want it
> re-introduced as a BA-visible **option** (off by default, with a UI
> toggle showing what % is being applied)? Or is it permanently retired?

---

### 6. ⚠️ `L1.UNITS.002` — Memory column units

**Transcript at 00:05:00** says memory is "MIB or MIB format".

**Engine today:** treats vInfo `Memory` column as **decimal MB** (÷ 1024)
rather than MiB (÷ 953.674). The output happens to match Customer A expectations
(67,989 GB), but the math is inconsistent with how storage is converted.

**Question for you:**
> Is the vInfo `Memory` column actually decimal MB (RVTools labels it as
> "MB" in some versions) or binary MiB? Customer A matches with the MB treatment.
> If you don't know, we can leave the engine alone — it's working — but
> document the inconsistency.

---

### 7. ⚠️ `L1.INPUT.001` — Non-RVTools format detection

**Transcript at 00:00:24** says we should adapt to many flavours.

**Engine today:** silently produces all zeros if the file isn't RVTools
(e.g. PowerCLI exports). No format detection, no "wrong file type" banner.

**Question for you:**
> When you receive a wrong-format file today, you spot it manually. Should
> the app pre-flight the file and refuse with a clear message ("This looks
> like a PowerCLI export, please re-export from RVTools")? Or should it
> attempt to parse alternate formats?

---

### 8. ⚠️ `L1.INPUT.003` — Below-minimum (vInfo only) tolerance

**Transcript at 00:02:28:**
> "sometimes customers will not send us even that. They may send us only the
> V info tab"

**Engine today:** parses what it can but does not emit a structured warning
listing which tabs were missing relative to your stated minimum (vInfo,
vHost, vPartition).

**Question for you:**
> When vHost is missing, do you want the UI to show an explicit "Missing
> tab — using benchmark fallback (vCPU/pCore = 1.97)" banner, or is the
> current quiet behaviour acceptable?

---

## The 4 rules I marked `[BA review]` because the transcript is silent

| Rule | What I assumed | Need confirmation on |
|------|----------------|----------------------|
| `L1.SCOPE.003` | Templates count as on-prem inventory | Is this what you do? Layer 1 transcript doesn't mention templates. |
| `L1.UTIL.004` | Fallback CPU/Mem util % codified in `Benchmark Assumptions` | Layer 1 says "codified in app" but not the exact value. |
| `L1.HOST.002` | `num_hosts` excludes hosts with no powered-on VMs | Engine v1.3.0 does this; not stated in Layer 1. |
| `L1.STORAGE_INUSE.001` | In-use storage is for Layer 2 managed-disk sizing only | Layer 2 transcript may clarify. |

---

## What's next

Once you sign off this rule book (replying with deltas/edits or just "approved"),
I'll move to **Phase 2 / Layer 1**:

1. Build `training/replicas/layer1_ba_replica.py` — a stand-alone Python
   script that reads the Customer A RVTools file and mechanically follows every
   rule above. **No imports from `engine/`** — it's an independent oracle.
2. Run it on Customer A, compare every output to your spreadsheet (line-by-line),
   and tune the rule book until the replica matches **within ±1% on every
   field** (the gate you set: stellar, not "kinda works").
3. Add `tests/test_baseline_parity.py` that runs both the engine and the
   replica on the same file and fails CI on any field over 5% apart.

Then we tackle Layer 2 + Layer 3 with the same recipe.

---

## Implementation notes (for context, not action)

- Phase 0 corpus is in [training/](../) — transcripts committed, mp4s left
  on local disk per the repo's binary policy.
- The rule book schema is documented in [training/README.md](../README.md).
- Convergence work will land as small focused PRs, each citing the
  `rule_id` it closes and the parity-test field it pins. No engine changes
  in this PR — only the corpus + rule book + this packet.
