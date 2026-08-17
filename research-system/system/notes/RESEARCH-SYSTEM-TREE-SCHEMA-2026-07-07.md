# Hypothesis Tree Research System — Tree Schema (Design Area A1)

> **Status:** Working note; design converged 2026-07-07; **v2** after three-reviewer
> pass (see §12 — review response and triage).
> **Parent document:** `RESEARCH-SYSTEM-CONCEPT-2026-07-07.md` (same directory) — the
> ratified concept this schema implements. Read that first; this note quotes its
> rulings (D7–D10) and section numbers without re-deriving them.
> **Scope:** item 6 of the concept note's §13 work queue — concrete node/claim/
> dispatch/tree/ledger schemas, the index/detail split, the visual-view requirement,
> and the field-level write-authority map. Serialization file formats and validation
> machinery are **not** here (they belong to physical layout A2 and the D7 gate work
> in Phase B).

Tags: ✅ committed · 🟡 committed concept, mechanics open · 🔲 open.

---

## 1. Organizing principle ✅

**The schema is partitioned by author.** Every field belongs to exactly one write
domain (concept D10): navigation state is director-authored, epistemic state is
verifier-authored, git bindings and lifecycle mechanics are harness-written,
user-section ledger entries are user-authored. The schema is therefore also the
enforcement map for the D7 write gate: a write is valid iff the writer's role owns
the field.

**Trigger/write split (v2):** some director *acts* entail writes the director does
not own (settlement-demotion creates a ledger entry; entry-merge creates a new
entry; merge advances the epoch). Rule: **the director's act queues the write; the
owning author executes it.** Same proposes/disposes shape as everywhere else. The
trigger is recorded in the director's domain (decision_log / settlement field); the
resulting artifact is authored by its owner (verifier / merge gate).

---

## 2. Node ✅

```
node
├─ NAVIGATION DOMAIN (director-authored)
│  ├─ id: "2.3.1"                       # positional (DP-1)
│  ├─ parent: "2.3"
│  ├─ premise: one paragraph            # the question/bet this node explores
│  ├─ minted_from: ledger#L-014 | user-direct | settlement#node-2.2
│  ├─ supersedes: node#id | null        # v2: mint-to-replace lineage (see DP-1 note)
│  ├─ superseded_by_node: node#id | null
│  ├─ status: unexplored | worked | parked | closed | merged     # v2: no `active`
│  ├─ status_reason: text + refs        # e.g. closed citing standing
│  └─ conflicts: [ {parked_at_epoch, superseded_by: "2.4" | null,
│                   settlement: pending | closed | revived | demoted→ledger#L-031} ]
│                                       # v2: list — park/revive/re-park cycles
│
├─ GIT BINDING (harness-written; created at FIRST DISPATCH, not at mint)   # v2
│  ├─ branch: "ht/2.3.1-pinned-contract"
│  └─ fork: [{epoch: 7, reason: original} , {epoch: 9, reason: revived}]  # v2: history
│
└─ EPISTEMIC DOMAIN (verifier-authored)
   ├─ standing: untested | supported | weakened | refuted | contested   # (DP-3)
   ├─ standing_note: rationale citing the claims/children that drive it;
   │                 also carries post-merge watch annotations ("under-delivered
   │                 in production — see watch#W-4")                     # v2
   ├─ claims: [claim…]                  # §4
   ├─ measurements: [measurement…]      # §4
   └─ learnings_out: [refs to global-layer entries that originated here]
```

Ruled decisions:

- **DP-1 ✅ — positional IDs + no-reparenting rule.** IDs like "2.3.1" are stable
  because nodes are never moved: restructuring = mint new nodes + close old ones.
  Sibling indices append monotonically; nothing is renumbered or deleted.
  **v2:** mint-to-replace records `supersedes`/`superseded_by_node`, so evidence on
  a closed node is followable to where the idea now lives (review finding: without
  the link, months of restructuring scatter evidence across ghost nodes).
- **DP-2 ✅ — no `kind` field.** Direction-vs-hypothesis is carried by depth and by
  what the dispatch asks. Premise *writing quality* is a methodology-lane concern
  (minting guidance), deliberately **not** a schema template (§11.6).
- **DP-3 ✅ — five standing states.** `contested` = children/claims genuinely
  conflict; `standing_note` must explain. (User: the 4-vs-5 *choice* is tunable and
  not load-bearing; standing itself is load-bearing — the director navigates by it.)
  Concept note §3.2/§15 updated to five states (was four).

### 2.1 Status definitions and transitions ✅ (v2 — reviewer-driven rewrite)

`active` is **removed**: it conflated "cursor is here" with "dispatch running,"
duplicating `tree.cursor` inside node status and creating stuck states after
recall/block/scout. **Cursor position lives only in `tree.cursor`.** Definitions:

- `unexplored` — minted; no adjudicated report yet. No git binding exists yet.
- `worked` — has ≥1 **adjudicated report** (complete or partial); open for further
  dispatches. A scout's verified report legally moves `unexplored → worked`.
- `parked` — swapped away with an unsettled conflict entry (settlement pending).
  Settlement resolution moves it: `closed` (settled: closed **or** demoted — the
  demotion ref lives in the conflict entry) or `worked` (settled: revived, with a
  new `fork` entry stamped at the current epoch).
- `closed` — no further work intended (director act; `status_reason` cites grounds,
  often standing).
- `merged` — landed on trunk. Navigationally terminal; **standing stays live**
  (post-merge watch results can still degrade it).

Dispatch outcomes and epistemics (v2 — fixes a review-caught contradiction):

- `blocked` and `retry-operational` are **operational**: no report is adjudicated,
  no status or standing change; the node stays as it was; the director re-poses via
  a fresh dispatch record (`d-X-2`) or moves on.
- `recalled` is **partial-but-epistemic**: the partial report goes through the seam
  normally (concept §8.4); granted claims land and may update standing. What recall
  does *not* do is close the leaf's question — the node returns to `worked` /
  `unexplored`-with-report→`worked` like any adjudicated outcome. ("Recalled doesn't
  change standing" in v1 was wrong; verified partial results always count.)

Status × standing remain different axes: `closed`+`supported` (fine, deprioritized),
`merged`+later-`weakened` (post-merge degradation), `parked`+`contested` (swapped
away mid-controversy) are all legal and all visually distinguishable (§8).

---

## 3. Dispatch record ✅ (leaf-attached; one per dispatch)

```
dispatch
├─ id: "d-2.3.1-1"                      # node + ordinal
├─ node: "2.3.1"
├─ question: what this dispatch asks
├─ done_definition: achievement-based end condition (concept §8.2)
├─ plan_ref: ex-ante plan — rubric + exhaustion criteria, declared before work
├─ steers: [{when, message}]            # director→unit, logged (concept §8.4)
├─ interrupt: {when, reason} | null     # v2: unit→director blocking interrupt,
│                                       #     logged incl. the "why ill-posed" text
├─ outcome: completed | blocked | recalled | retry-operational
├─ metering: {tokens, wall_clock}       # passive, never enforced
├─ epoch: trunk@7
├─ role_packet: {version, hash}         # v2.1 (A3): seam packet provenance
├─ adjudications: [refs]                # v2.1 (B4): bounce attempts derivable
└─ report_ref, archive_ref
```

Authors: harness writes mechanical fields (metering, outcome, timestamps); director
writes question/done_definition at mint and appends steers; **unit writes the
interrupt** (its one legal upward message); plan_ref points at senior-authored
ex-ante content. A recall is additionally recorded as a **decision_log move**
(type `recall`, with `target` and rationale) — the liveness valve is an auditable
director act, not just a mechanical outcome (v2).

---

## 4. Claim and measurement ✅

```
claim
├─ id: "c-2.3.1-3"
├─ node, source_dispatch
├─ text: scope-explicit statement
├─ proposed_tier: 2                     # what the senior asked for
├─ granted_tier: 2                      # what adjudication awarded
├─ anchors: [archive spans]             # mandatory (concept §9 citation discipline)
├─ epoch: trunk@7
├─ instruments: [suite-S@v3, rubric-R2@v1]
└─ status: granted | superseded | contradicted    # post-merge watch can contradict

measurement
└─ metric, value, epoch, instrument@version, noise_flag, anchors
```

- `proposed_tier` vs `granted_tier` makes the **demotion rate** computable from two
  fields.
- Below-noise measurements carry `noise_flag` and license no claims; recorded, not
  discarded.
- All epistemic-domain: verifier-authored only.

---

## 5. Tree-level state ✅

```
tree
├─ root_question, component
├─ epoch: 9                             # advanced MECHANICALLY by the merge gate (v2)
├─ epoch_history: [{epoch, merged_node, date,
│                   user_ratified: true | false | n/a}]
│                  # v2: ratification recorded for merges that promote guidance
│                  #     into actor-visible surfaces (concept §4.3); n/a otherwise
├─ cursor: [{node: "2.3.1", allocation: "a1"}]
│          # v2: list-with-capacity-1 — parallelism later = capacity change,
│          #     honoring concept §4.2's no-schema-change promise
├─ decision_log: [{move, from, to, target, allocation, rationale, refs, epoch, date}]
│          # v2: `target` for moves that don't move the cursor (scout, recall,
│          #     mint, ledger ops); `allocation` ties moves to a cursor slot;
│          #     `refs` = evidence links (navigation claims are evidence-linked,
│          #     concept §4.1)
├─ global_learnings: [{id, text, tier3_claim_ref, origin_nodes,
│                      director_approved, user_ratified}]   # v2: both gates recorded
└─ watch_queue: [{id: "W-4", merged_node, prediction_claim,
                  observed, verdict, severity,
                  status: open | resolved}]
     # v2: rows are created MECHANICALLY at merge (one per prediction-bearing
     #     claim, observed/verdict null); verifier fills observed + verdict;
     #     resolution folds into the node's standing_note and the row closes —
     #     the queue holds only open watches (no unbounded growth)
     # verdict: confirmed | under-delivered | regressed
     # regressions surface at the director's next review
```

- `decision_log` and other logs are **queried mechanically** — statistics are
  computed by code and presented via the readout; no LLM ever linear-reads
  thousands of raw rows (that would recreate the concept-§10 failure). Raw logs are
  cheap storage off the hot path; growth replay keeps them complete by design.
- **Watch verdict → standing (guidance; full rubric in B4):** `regressed` →
  verifier typically moves standing to `weakened`/`refuted` and sets the
  prediction claim `contradicted`; `under-delivered` → claim scope was inflated:
  standing_note annotated, standing adjusted at verifier judgment, demotion
  recorded against the original claim where warranted.

---

## 6. Index / detail split ✅ (ratified 2026-07-07; v2 adds lifecycle)

Two layers, one source of truth each way:

- **Index** — per node: structure, premise one-liner, status, standing,
  latest fork epoch, unsettled-conflict flag (+ tree-level cursor and epoch).
  (v2: fork-epoch and conflict flag added — they are navigation-relevant and the
  visual needs them; spend and scout markers are **not** in the index, see §8.)
- **Node detail** — per-node: full premise, claims, measurements, dispatch records,
  conflict entries, report(s), `archive/`.

**Index lifecycle (v2 — the scale fix):** the index is **depth-ordered, so a
partial top-down read is a valid read** (the §3.2 navigability invariant made
physical), and **fully-dead subtrees collapse**: a subtree in which every node is
`closed` or `merged` is represented in the default read by its root's line only
(premise, standing, rollup marker). The **live view** — open/parked/worked nodes,
their ancestors, and dead-subtree rollup lines — is what the director reads each
turn; it is canonical (not a generated view), maintained mechanically from status.
The full uncollapsed index remains canonical and addressable underneath. Without
this, dead nodes dominate the index by ~iteration 400 and the director's
context-immunity claim fails (review finding #1).

Canonical form is validated structured data (JSON or equivalent — exact
serialization: A2); human-readable Markdown views are **generated and uncitable**.

---

## 7. Ledger entry ✅ (DP-4, ratified 2026-07-07)

```
ledger_entry
├─ id: "L-014"
├─ section: user | research | observatory
├─ text: the hypothesis/idea, scope-explicit
├─ proposed_by: user | report#d-2.3.1-1 | claim#c-2.3.1-3
│               | observation#O-38 | settlement#node-2.2
│               # v2: claim# lineage distinguishes demotion-spawned
│               #     generalizations (feeds the §9 refutation-rate statistic)
├─ support_count: 3
├─ echoes: [{source_ref, epoch}]        # every increment traceable — never a bare counter
├─ cross_refs: [entry ids]              # cross-section convergence links
├─ dedup_log: [{matched, resolution: distinct | merged}]   # D8 events, recorded
├─ status: open | minted→node#2.3.1 | retired(reason) | merged-into→L-020
└─ anchors: [evidence refs]             # especially observatory-sourced entries
```

Field-level write partition (D10 extended to the ledger):

| Field group | Author | Rationale |
|---|---|---|
| Entry creation, user section | user, direct | override authority — no gate between the user and their own intake |
| Entry creation, research + observatory sections | verifier only | spin-offs land via adjudication; observations land when verified |
| Entry creation **on director trigger** (settlement-demotion → research section; entry-merge target) | **verifier executes** | v2: §1 trigger/write split — director's act queues it; verifier authors text, unions `echoes` **with dedup** (never naive sums), carries anchors, internalizes prior cross_refs |
| `support_count`, `echoes`, `dedup_log` | verifier | an echo is logged as a byproduct of adjudication already underway — zero *marginal* cost, but never un-gated (aligned wording in concept §6) |
| `status` transitions (mint / merge-entries / retire) | director | triage = attention allocation; retirement must cite grounds |

Settlement-demoted entries: created in the **research** section,
`proposed_by: settlement#node-X`, `support_count` seeded from the demoted branch's
verified claims (each seeding echo = one adjudicated claim ref — measurements die,
verified support survives as traceable echoes).

A `support_count` of 7 is seven inspectable references. Cross-section convergence is
checkable, not a vibe.

---

## 8. Visual view 🟡 (requirement ratified; shape language deferred)

A **generated view**, part of the monitor readout. Ruled 2026-07-07: readability
first; concrete visual language deferred. **v2 data-source correction:** renders
from the **index + the computed statistics readout** (spend per node, scout
markers) — both canonical; the v1 "from the index alone" claim contradicted the
index's own "nothing else" rule (review finding). The index alone still fully
determines structure, status, standing, strata, and badges.

Requirements (unchanged from v1):

- **Roots-down orientation**: trunk/product at top; research digs downward; **epochs
  as soil strata** (a node's stratum = its fork epoch — now in the index);
  stale-baseline debt visible as roots ending in old soil; merges = nutrients
  flowing up.
- **Fill color = standing** (5 states; contested distinct, e.g. split fill).
- **Outline = status** (5 states — no `active`; the cursor has its own marker).
- **Node size = spend** (from the statistics readout).
- Cursor marker; unsettled-conflict badges (from index flag); scouts as whiskers
  (from readout).
- **Legend = the co-located interpretation rules** (user ruling).
- **Growth replay** — derived from decision_log + epoch_history; nothing extra
  recorded.
- Renderer → self-contained static SVG/HTML, regenerated on index change. 🔲 shape
  language deferred.
- Collapsed dead subtrees (§6) render collapsed by default — the picture and the
  director read the same tree.

---

## 9. Statistics computability map ✅

| Signal | Source fields |
|---|---|
| Verifier demotion rate | claim.proposed_tier vs granted_tier |
| Tier mix over time | claim.granted_tier × epoch |
| Rediscovery rate | ledger_entry.dedup_log |
| Spend concentration | dispatch.metering × node position |
| Thrash / lock-in | tree.decision_log (+ standing movement for interpretation) |
| Unsettled debt | node.conflicts[].settlement = pending |
| Refutation rate of spun-off generalizations | ledger entries with claim# lineage × final standing |
| Scout rate | decision_log moves of type scout (target field) |

All statistics are computed mechanically from these fields; the readout presents
aggregates **with co-located interpretation rules** (user ruling; lock-in vs
deep-exploitation is the canonical example). A soft-cap alert on
`global_learnings` size belongs in the readout (its smallness is a norm — the alert
is the backstop).

---

## 10. Consolidated write-authority table ✅ (v2 — completed per review)

| Artifact / field group | Director | Verifier | Unit | User | Harness (mechanical) |
|---|---|---|---|---|---|
| Node: id, premise, status, status_reason, conflicts, supersedes | ✍ | | | override | |
| Node: standing, standing_note, claims, measurements, learnings_out | | ✍ | | ratify tier-3 | |
| Git binding (branch, fork history) | | | | | ✍ (at first dispatch; refork at revive) |
| Dispatch: question, done_definition, steers | ✍ | | | | |
| Dispatch: interrupt | | | ✍ | | |
| Dispatch: plan (ex-ante), report draft, archive | | | ✍ (senior/junior) | | |
| Dispatch: metering, outcome, timestamps | | | | | ✍ |
| Tree: cursor, decision_log | ✍ | | | override | |
| Tree: **epoch, epoch_history** | | | | ratify actor-visible promotions | ✍ (merge gate) |
| Tree: global_learnings | proposes + approves | ✍ | | ratifies | |
| Tree: watch_queue — row creation | | | | | ✍ (at merge, from prediction claims) |
| Tree: watch_queue — observed, verdict, resolution | | ✍ (from observatory input) | | | |
| Ledger: user section creation | | | | ✍ | |
| Ledger: research/observatory creation (incl. on director trigger), support, echoes, dedup | | ✍ | proposes via report | | |
| Ledger: status transitions | ✍ | | | override | |

The unit **never writes tree or ledger** — only its own workspace, report draft,
archive, and the dispatch interrupt field. Everything else it wants remembered
crosses the seam.

---

## 11. Open within this area 🔲

1. Exact serialization (file formats, one-file-per-node vs. hybrid; live-view
   maintenance mechanics) — **A2 physical layout**, where D7 enforcement hooks land.
2. ~~Validation rules~~ — closed 2026-07-07 by B4 §9
   (`RESEARCH-SYSTEM-VERIFIER-PROTOCOL-2026-07-07.md`).
3. ~~Standing aggregation + watch-verdict rubric~~ — closed 2026-07-07 by B4 §5–§7
   (rubric; watch mapping; merge-time fresh re-derivation as the v1 lying-map
   guard, per-N spot-audits growth-path).
4. Visual shape language (deferred by ruling).
5. Archive internal structure (span addressing for anchors) — A2, with the trace
   reader's typed-trace format in mind.
6. Premise-writing guidance (what a good premise contains) — methodology lane, on
   purpose not a schema template (DP-2 instinct; scale review #5 acknowledged).
7. Dedup matcher quality at corpus scale — Phase B with D8 machinery (the amnesia
   detector is powered by the operation that degrades as the corpus grows; scale
   review #9).

---

## 12. Review response — 2026-07-07 v2 triage ✅

Three independent Opus reviews (consistency, scale stress-test, scenario
walkthrough) ran against v1; per user instruction they were treated as cheap issue
checkers, not canon. Disposition:

**Accepted (folded into v2 above):** director-triggered writes get the
trigger/write split (§1, §7); epoch writer assigned to the merge gate (§5, §10);
`active` removed from status enum — cursor is single-sourced, fixing stuck-states
after recall/block and the scout transition (§2.1); blocked/retry vs recalled
epistemics separated — verified partial results count (§2.1); dispatch gains
`interrupt` (§3); recall recorded as decision_log move (§3, §5); decision_log gains
`target`/`allocation`/`refs` (§5); cursor is a capacity-1 list (§5); `fork` gains
history for revive (§2); `conflicts` is a list (§2); `supersedes` links for
mint-to-replace (§2); watch_queue rows created mechanically at merge, get
open/resolved lifecycle, verdict→standing guidance (§5); index gains fork-epoch +
conflict flag, depth-ordering, dead-subtree collapse, and the live view (§6);
visual renders from index + statistics readout (§8); ledger `proposed_by` may cite
claim# (§7); settlement-demotion section/support seeding defined (§7); entry-merge
echo union with dedup (§7); status definitions written out (§2.1); global_learnings
records director approval; epoch_history records user ratification (§5); concept
note aligned on five standing states and support-count wording.

**Rejected or deferred, with reasons:** premise templating (methodology-lane
guidance, not schema — premature taxonomy; §11.6); decision_log "uncompactable"
alarm (conflated raw logs with the readout — logs are mechanically aggregated,
never linear-read; §5); echoes/dedup_log growth (bounded by entry lifetime;
no action); standing-aggregation trust and dedup-at-scale (real, already owned by
B4/Phase B; §11.3, §11.7); global_learnings hard cap (soft-cap alert in readout
instead; §9).

---

## 13. Decision log for this note

| Ruling | Decision | Date |
|---|---|---|
| DP-1 | Positional node IDs + no-reparenting rule (+ v2 supersedes links) | 2026-07-07 |
| DP-2 | No `kind` field on nodes; premise guidance → methodology lane | 2026-07-07 |
| DP-3 | Five standing states incl. `contested` (concept note aligned); the 4-vs-5 choice tunable, standing itself load-bearing | 2026-07-07 |
| DP-4 | Ledger entry schema as §7, incl. field-level write partition + trigger/write split | 2026-07-07 |
| Index/detail split | Ratified; index = director surface; v2: depth-ordered, dead-subtree collapse, live view | 2026-07-07 |
| Visual view | Requirement ratified; roots-down; readability first; renders from index + stats readout; shape language deferred; growth replay derived | 2026-07-07 |
| v2 revision | Three-reviewer triage applied (§12); status enum drops `active`; trigger/write split adopted | 2026-07-07 |
