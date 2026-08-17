# Hypothesis Tree Research System — Concept & Architecture

> **Name:** Hypothesis Tree Research System (ruled 2026-07-07). Recognized variants:
> "Hypothesis Tree", "Research System" — default to the full name.
> **Status:** Working note; concept-level design, converged 2026-07-06/07.
> **Supersedes:** the research-unit and navigation portions of `relay-research/DESIGN.md`
> **for this function** (autonomous harness-improvement research). Provisional per user
> ruling 2026-07-07; final documentation placement deliberately deferred (doc overhaul
> in progress).
> **Purpose of this note:** freeze the converged concept so a future instance can
> continue the design without re-deriving it. This is NOT a buildable spec — §13 is the
> work queue for the next altitude down.

---

## 0. How to read this document

Same convention as `relay-research/DESIGN.md`:

- ✅ **COMMITTED** — converged and ruled; safe to build on.
- 🟡 **COMMITTED CONCEPT / DESIGN OPEN** — the idea is settled; mechanics are not.
- 🔲 **OPEN** — not designed. Do not assume.

The Decision Log (§14) separates drafter judgment calls from pending user rulings.
A glossary of coined terms is at §15 — forward references are safe to park until then.

---

## 1. The problem ✅

Build an autonomous self-improvement research system around a SWE agent harness
(the L1–L5 system — a multi-agent harness whose levels L1–L5 denote agent
altitudes/roles; e.g. L4 = an executing agent). Instantiated per main component. Work is **mostly sequential**
(one active dispatch at a time), potentially hundreds of research iterations over time.
The primary product is **merged harness changes** validated against evals/benchmarks;
durable learnings are the residue, not the main output.

The failure modes this design exists to prevent (all observed or anticipated from
experience with long-horizon agent research):

1. **Claim inflation** — the system makes generalized claims that were never proven;
   single observations silently become rules.
2. **Documentation dysfunction** — either drowning in transcripts (can't find
   anything) or only high-level docs full of unsupported claims. Both poles are the
   same disease: **compression that loses its link to evidence**.
3. **Navigation failure / path dependency** — the system stops evaluating main
   directions and just moves down a single one; lock-in; poor exploration.
4. **Amnesia** — learnings stop cumulating; research goes in circles; rediscovery.
5. **Wrong-task drift** — units spend resources on self-invented work (observed
   case: an agent spending ~40 iterations hardening its own contract).
6. **Premature stopping** — completion bias; agents under-explore and declare
   victory. Empirically more common than runaway overspend.

**Unifying view:** the system runs on a compressed map of itself — every "what next"
decision is made from that map, never from raw material. Failures 1–4 are corruptions
of the map. Therefore the epistemic state object (the tree, §3) is the control plane
and is engineered hardest; every role touches it through gates.

The system must also be **self-healing**: its health must be measurable from its own
artifacts (§11) so it can be re-steered when it degrades.

---

## 2. Architecture at a glance ✅

```
production runs ──► OBSERVATORY (field) ──► verified observations ──► LEDGER ◄── user ideas
      ▲                                                                 │    ◄── research spin-offs
      │                                                                 ▼
      │                                                              DIRECTOR ──► HYPOTHESIS TREE
      │                                                                 │        (cursor, epochs)
      │                                                          dispatch (one leaf)
      │                                                                 ▼
      │                                                        RESEARCH UNIT
      │                                                 (senior + junior + checker)
      │                                                                 │ report
      │                                                                 ▼
      └────── merge into trunk ◄── settlement/gates ◄──────────── SEAM VERIFIER
          (+ post-merge watch obligation → observatory)          (owns all writes)
```

One cycle in prose: ideas enter the **ledger** from three provenances (user,
research spin-offs, observatory findings). The **director** — whose only surface is
the tree, the ledger, and statistics — mints ledger entries into tree nodes, moves
the **cursor**, and dispatches one leaf at a time to a **research unit**. The unit
works on a git branch, drafts claims; the **seam verifier** adjudicates every claim —
nothing enters the tree's epistemic content except through it (write authority is
split precisely in §4.1). Verified improvements **merge into
trunk** (the promotion event), which settles parked siblings and advances the
baseline **epoch**. Merged changes carry predictions; the **observatory** watches
production for confirmation or disconfirmation, feeding the ledger again.

Two halves, one boundary object: the **lab** (tree + units: controlled experiments
settle hypotheses) and the **field** (observatory: live-system observation generates
them). They never talk directly; they meet in the ledger.

---

## 3. The hypothesis tree ✅

The core epistemic artifact and the director's entire decision surface.

**The tree is over harness variants, not knowledge.** A node = a premise/hypothesis
+ (usually) an actual change on a git branch + the measured response. Learnings are
attached residue.

### 3.1 Specificity is depth

Each level answers its parent's question one degree more concretely:

- Root/top: an open quality dimension ("improve code adherence").
- Next: rival **intervention families** (prompt shaping, context management, review
  gating…).
- Deeper: variants within a family.
- Deep leaves: crisp testable changes ("pin task contract as first user message").

**Siblings are rival answers; depth is commitment to refining one.** Upper nodes are
directions/premises; hypotheses proper only exist deep — and a branch that never
reaches verdict-depth is still legitimate: it narrowed the space, and that is
recorded. The tree manufactures its own specificity by descent.

### 3.2 Premise standing and self-summarization

Every internal node states the premise its subtree explores and carries a **verified
standing** (untested / supported / weakened / refuted / contested), updated from its
children's outcomes. Standing updates are verifier-gated writes like any other — typed
propagation, not lossy LLM condensation (contrast: `propagate_insights()` in Arbor,
an external hypothesis-tree research system this design leans on — see §12).

**The navigability invariant:** reading a node at depth *k* tells the truth about
everything below it at coarser grain. The director navigates by reading top levels
and descending only where standing is uncertain or contested. Navigability is an
invariant the verifier maintains, not an indexing problem.

### 3.3 Lineage vs. scope

Tree position is **provenance, not scope**. A lesson learned in 2.3.x usually
applies elsewhere. Cross-cutting learnings promote out of their branch into a small
**global learnings layer** attached to the tree — verified-only, deliberately small,
visible to all dispatches and proposals. (Pattern-scope promotion is gated: §5.)

### 3.4 One tree per component; recursion upward 🟡

Each main component of the SWE system gets its own tree. The same discipline extends
recursively: components form top-level branches of one system-wide tree, and **there
is only one cursor in the whole system** — so trunk epochs are automatically global,
and even urgent cross-component interrupts are just a top-level swap with the
standard park/settle machinery. Committed as concept; mechanics undesigned.

---

## 4. Navigation: director, cursor, epochs ✅

### 4.1 The director

One role whose **only job is to direct**: which branch to explore, where to move,
what to spend attention on. It runs no experiments, writes no summaries, reads no
primary material — its surface is **compressed state only** (enumerated at the end
of this section). Rationale: (a) forcing function — anything
the director needs must be in the tree, so reporting quality is structural; (b)
context-budget immunity at iteration 400; (c) path dependency becomes one auditable
policy in one place instead of being smeared invisibly across hundreds of local
choices.

Navigation decisions are themselves **claims** — recorded with rationale, evidence-
linked, contradictable later.

**Write authority, precisely** (this resolves an apparent tension with §7): tree
state has two write domains. **Navigation state** — node existence, cursor position,
park/settle status — is director-authored; these writes are the recorded navigation
claims above. **Epistemic state** — measurements, claims, standing, learnings — is
seam-verifier-authored only. The §7 rule ("no role both authors epistemic content
and steers by it") thus means: the director can never author the evidence it steers
by, and the verifier never steers. The director shapes the map's *structure*; only
verified content fills it.

**The director's inputs, exhaustively:** the tree, the ledger, statistics (§11), and
on-signal **trace-reader digests** (§8.4, §10) — LLM-authored compressions. The
structural invariant: **no raw or primary material, ever** — archives and transcripts
have no path into its context.

### 4.2 Single canonical branch (the cursor)

At any moment one node is active — the cursor. Modeled as an **attention allocation
with capacity 1** (not a structural singleton), so later parallelism is a scheduler
change, not a schema change.

Move vocabulary (the director's outputs are legal cursor moves plus ledger/tree
operations):

- **descend** — open/mint a child; **widen** — mint a new sibling (rival answer);
- **swap** — park the current sub-branch, promote another to canonical; creates a
  **conflict entry** (unsettled-debt record) for the parked one;
- **pop** — close a subtree; **merge** — close a branch into trunk (§4.3);
- **scout** — a rationed off-path probe (measurement-safe under frozen trunk;
  costs attention only). Accounting: a scout is a normal dispatch in every counted
  sense — one leaf, one report, one verification — but does **not** move the cursor
  and can never merge; sequential execution is preserved (it runs between canonical
  dispatches). Scout rate is a monitor statistic (D9, ratified);
- **promote-from-ledger** — mint a ledger entry into a node;
- **recall** — terminate a running dispatch from above (§8.4); the liveness valve.

### 4.3 Frozen trunk, epochs, settlement

**Trunk moves only at merge.** While a branch is being explored, trunk is frozen, so
every measurement taken during that branch's lifetime — including parked siblings —
shares one baseline. Parking is free; **merging is what costs**: it devalues every
parked sibling's measurements. The open **conflict entries** — attached to parked
nodes in the *tree*; they are not part of the ledger (§6) — are the record of that
staleness debt, and merge forces explicit settlement per parked sibling:

- **close** (accept the loss), or
- **revive-and-remeasure** (pay the rebase cost), or
- **demote to ledger** — the measurements die, the *idea* survives as a supported
  hypothesis awaiting re-promotion.

**Merge authority is split:** the director *decides* to seek merge (a cursor move);
the merge *gate* disposes — mechanical eval checks, verifier adjudication of the
supporting claims, settlement completeness — and pattern-scope or actor-visible
promotions additionally require user ratification (§5, §7). The director proposes;
gates dispose.

Two separable commitments (kept separate deliberately): **trunk freezing** buys
measurement integrity (non-negotiable); **single canonical path** buys attention
integrity (relaxable later if parallelism arrives, without touching the first).

**Every measurement is stamped with its baseline epoch** even while that's constant
— it's what makes later parallelism and any cross-epoch comparison checkable.

---

## 5. Epistemology: the claim ladder ✅

Claims are typed by **scope**, and the tiers differ in *what evidence can license
them*, not merely in required rigor:

| Tier | Example | Licensed by |
|---|---|---|
| 1 — point fact | "tool calls −18% on suite S at trunk@7" | mechanical measurement; near-free verification |
| 2 — episode claim | "adherence improved in 8/10 sampled long sessions (rubric R2, this component)" | disciplined qualitative evidence + adversarial verification |
| 3 — pattern claim | "pinned contracts improve adherence" (population-scope) | **recurrence across varied contexts** (support counts); rare here; requires director approval + **user ratification** |

Core rules:

- **No single observation licenses a population claim, at any verification rigor.**
  Promotion up the ladder happens by accumulating instances, never by argument.
- **Generalization = hypothesis.** When a report over-claims scope, the verifier
  demotes the claim to what the evidence supports, and the generalization spins off
  to the **ledger** as an untested hypothesis with support_count = 1. The illegal
  epistemic move becomes a legal navigational move — and generalization candidates
  are precisely the director's best fuel.
- **Demote, don't reject** (where possible): claims land at the tier their evidence
  supports. Over-claiming is corrected, not punished into silence.
- **Non-quantifiable ≠ non-verifiable.** Qualitative changes are legitimate: paired
  episodes, fixed rubric, adversarial check — landing at honest (episode) scope.
- **Noise floor:** deltas inside the noise floor license nothing (recorded as
  measurements, not claims).
- **Eval-minting (EDD as asymptote):** when the same qualitative claim family keeps
  appearing in merge decisions, that recurrence is the trigger to build an eval for
  it. The instrument suite grows where claim pressure demands; eval-driven
  development is the direction of travel, not a prerequisite.

---

## 6. The ledger ✅

The intake buffer for **all** hypothesis sources, and the boundary object between
the field and the lab. Entries carry provenance, support counts, and cross-
references.

**Sections by provenance:** user / research spin-offs / observatory findings.
**No fixed priority rank between sections** (ruled 2026-07-07, superseding an
earlier user›research›observatory ordering): the director allocates attention by
judgment, informed by severity tags and cross-section convergence. Provenance never
confers epistemic weight in either direction — a user idea's claims are not
stronger for being user-sourced.

- **Cross-section convergence is a signal:** the same hypothesis arriving
  independently in two sections weighs more than either alone (cross-referenced, not
  duplicated).
- Support counts accumulate as a byproduct of adjudication already underway: each
  echo is logged by the verifier while it is adjudicating the source artifact — zero
  *marginal* cost, no separate pass, but never un-gated (every increment is a
  traceable, verified echo; see A1 schema note §7).
- The ledger also catches **demoted branches** at settlement (§4.3) — ideas survive
  their measurements.
- **Right-sizing happens at promotion:** the director mints nodes from entries, and
  may merge several too-atomic entries into one testable hypothesis or split a
  too-broad one. Sizing is a deliberate act by the role with budget authority.
- **Post-merge watch results do not enter the ledger** (ruled 2026-07-07): a
  disconfirmation is not a new hypothesis — it lands as an annotation on the merged
  node plus a distinct, severity-tagged director-facing category (§11). Only
  hypotheses it *spawns* ("fails under condition X") enter the ledger normally.
- Growth path: a dedicated **ledger-keeper** agent (the old "corpus health
  stewardship" function from the prior corpus) if entries ever accumulate faster
  than the director's reviews can triage them. Not v1.

---

## 7. Roles ✅

Design rule: **no role both authors the tree's epistemic content and steers by it**
(precise write-domain split: §4.1). Each role touches the tree differently;
separation kills self-confirmation.

| Role | Job | Must not |
|---|---|---|
| **Director** | Navigate: cursor moves, mint nodes from ledger, dispatch leaves, graded control of running dispatches | run experiments, write summaries, read primary material |
| **Senior** (unit) | Translate dispatch → plan (incl. **ex-ante** rubric and exhaustion criteria); supervise; absorb outcomes; draft the report with tier-proposed claims; guard scope | navigate, rescope the dispatch, verify its own claims, mint nodes |
| **Junior** (unit) | Execute bounded tasks: implement, run, record; surface surprises without investigating them | interpret beyond local surface, decide direction |
| **Checker** (in-unit) | In-loop QA — execution fidelity: did the run do what the plan said; numbers computed right; artifacts present. **Under senior authority**; serves the unit | gate the seam; adjudicate claims |
| **Seam verifier** | Independent. **Sole author of epistemic content in tree and ledger** (§4.1). Adjudicates claims (approve / demote / reject) against archives; maintains standing propagation and the self-summarization invariant; verification cost scales with tier; may fan out subagents | redirect research ("test X instead"), become a second director, review its own prior work |
| **Monitor** | Watches the **director's policy health** from tree statistics plus the director's decision log (§11): thrash, lock-in, debt accumulation. **V1 = instrument only**: signals collected at a known location, displayed with co-located interpretation rules, "take a look" alerts, zero authority — the judgment layer is the user (or an ad-hoc LLM pointed at the readout). Autonomous monitor-agent is growth-path, calibrated later from accumulated history | touch research content; hold authority over the director |
| **Observatory** | Field organ: watches production runs (ambient sweeps + directed watches); produces verified observations → ledger; executes post-merge watch obligations | write the tree; run experiments. 🔲 internals undesigned (§13) |
| **User** | Override anywhere; ratify pattern-scope claims and any promotion of guidance into the harness's actor-visible surfaces; re-steer at **frontier reviews** (recurring inspection of open directions + spend + standing — the designed human re-steer point); seed the ledger directly | — |

**Vocabulary rule:** "verifier" is reserved for the seam gate; the in-loop role is
the "checker." The two are completely unconnected — plan→execute→verify is simply
the correct shape of work inside the unit, while the seam gate decides what becomes
true. The gate never reviews its own prior work because it did none.

**Seam jurisdiction:** the verifier adjudicates the **report** (the epistemic
payload). Dispatches, blocking interrupts, and steers are direct director↔unit
messages — logged in the dispatch record, but not adjudicated.

Relationship to the older relay-research unit: see §12.

---

## 8. The dispatch ✅ (contract concept; formats 🔲)

### 8.1 The work atom

**One dispatch = one leaf = one report = one verification.** The dispatch is the
only work unit the architecture counts. ("Loop" is deleted from the vocabulary — it
conflated budget denominator, continuity boundary, and work atom. Budget is
resources, metered not capped; continuity is the unit's internal
resumable-workspace discipline; the atom is the dispatch.)

A dispatch inherits the specificity of its node: shallow = "explore this direction"
(deliverable: **structure** — characterizations, proposed sub-directions, ledger
candidates); deep = "test this hypothesis" (deliverable: **verdict**). Scoped ≠
narrow: bounded question + done-definition, open content.

**Epistemic vs. operational outcomes:** "hypothesis tested, negative" closes the
leaf; "harness crashed / eval didn't run" does not — same leaf, re-dispatch, no
standing update.

### 8.2 Dispatch contents (downward)

Premise chain; the question; **done-definition** (achievement-based end condition —
"explored until directions exhausted", operationalized **ex-ante** by the senior's
plan: what would count as exhausted, declared before work starts); relevant global
lessons; baseline epoch stamp. **No disclosed resource caps** — disclosed budgets
become anchors that agents pace under (observed: stated 30 min → agent spends 10 and
undershoots). Premature stopping, not overspend, is the empirically dominant risk;
the goal-function holds the done-definition against completion bias (mechanism: a
standing goal contract re-asserted whenever the agent moves to conclude, so stopping
requires demonstrably satisfying the declared end condition — Claude Code
`/goal` / `/autonomous-iteration`-style enforcement).

### 8.3 Report contents (upward — skeleton, sketch only)

Dispatch echo → measurements (epoch-stamped) → claims at proposed tiers **with
archive anchors** → proposed hypotheses (ledger candidates) → off-frame
observations → operational notes.

**Only what returns through the seam exists.** The unit may do anything internally;
internal exploration is ephemeral by design (units are disposable). If the unit
wants something remembered, it must be in the report and survive verification.
Follow-up ideas ("next we should try X") are ledger proposals — node-minting
authority never leaves the director.

### 8.4 Mid-dispatch channel and graded control

- **Unit → director:** blocking-only. One legal interrupt: "this dispatch cannot be
  completed meaningfully as posed." Everything else — including surprises — waits
  for the report. (A director↔unit *conversation* would create unwritten shared
  state; forbidden.)
- **Director → unit:** graded, never conversational:
  1. **Passive metering** — tokens/wall-clock recorded per dispatch (for statistics;
     never enforced);
  2. **Hidden tripwire** — a director-side timer; on trip, nothing happens to the
     unit; the director gets a signal (the tripwire is an attention scheduler for
     the director, not a budget);
  3. **Overview** — director requests a trace-reader digest of the live dispatch
     (external observation; the unit is not interrupted and does not self-report);
  4. **Steer** — a typed message ("converge on what you have and report"),
     goal-referenced, never budget-referenced. **Steers are logged in the dispatch
     record** — a truncated-by-direction dispatch adjudicates differently than a
     completed one (observer-effect discipline);
  5. **Recall** — terminate from above; partial report goes through the seam
     normally. Since an unterminated dispatch holds the *cursor* (the scarce
     resource in a sequential system), recall — not a token cap — is the liveness
     valve.

Premature stopping is thereby a **demotable over-claim**: "explored until exhausted"
is checked against the ex-ante declaration (checker mid-flight; verifier at the
seam) and demotes to "explored 3 of 5 declared families."

---

## 9. Records: report/archive split ✅

Per node: the **report** is the first-class document; **archive/** holds transcripts,
raw logs, scratch — the full record of what the unit did.

**Citation discipline (what keeps archives second-class without losing them):**
nothing in the system may cite a transcript directly as evidence; evidence enters
only through a verified claim in a report; **every claim must carry anchors into the
archive.** Archives are write-once, read-by-exception, with exactly two legitimate
readers: the **verifier now** (adjudication = checking drafted claims against the
raw record) and **re-analysis later** (when a framing dies or a dispute arises,
episodes are re-read under a new lens — observations survive interpretations).

The director never reads archives — structurally, not by policy.

---

## 10. Instruments: the trace reader 🟡

The load-bearing shared instrument. Four organs silently depend on "turn what an
agent did into something an LLM can reason over": the observatory, the director's
live overviews, the verifier's archive audits, and post-merge watches.

**Diagnosis of the naive approach:** raw transcripts are the wrong representation —
an LLM asked to analyze 100k+ tokens of interleaved log judges from vibes. (Observed
in L1–L5: analyses of e.g. "does L4 orient to the right task on spawn or waste
reads?" were weak; a human reading transcripts did better, which doesn't scale.)

**Three aligned streams:**

1. **Action stream** (mechanical, deterministic, no LLM): tool calls, files touched,
   tokens per phase, timestamps, spawn/report events — with pointers back to raw
   spans. Cheap enough to run over everything. Powers **detection**: something
   deviated, here, at step k. (Orientation studies become largely computable:
   first-k actions vs. task-relevant file set, time-to-first-relevant-read.)
2. **Reasoning stream** — the model's summarized thinking traces, **top of the
   diagnostic stack**: "I should figure out the folder structure" explains an
   index-read better than any outside reconstruction. Calibration (from the
   evidence-qualification area of the 2026-06-21 behavioral design — §12): thinking
   traces are strong evidence of *stated rationale*, weaker as
   proof of *mechanism* — they explain action choice; they don't silently upgrade
   into causal claims.
3. **Experience stream** — local-surface reconstruction: what the agent could see at
   each decision point (context assembly, active instructions). Diagnosis =
   **empathetic replay** ("given exactly this view, why was that the natural
   move?"), not hindsight scoring.

**Detection is mechanical and exhaustive; diagnosis is interpretive and targeted**
(spent where detection flags). Everything that reaches the director is LLM-authored
digest — raw tool-call feeds go nowhere near it.

**Why the streams matter — representation shapes the intervention space:** an
action-only instrument makes every imaginable fix an action-constraint (guardrails,
prohibitions — clamps that fight the model). Reasoning + experience streams make
*contextual* fixes visible (clarify the assignment, move the instruction, fix the
misleading layout) — fix the world, not the agent.

**Two products, two standings (amended 2026-07-07):** the instrument serves two
populations whose purposes must never blur. The **Behavioral Record**
(evidence-grade: post-hoc, anchored, observer-effect labeled; consumers =
observatory + verifier; enters the system only through adjudication) and the
**Progress Overview** (control-grade: live-read of a running session, labeled
"as of step N — still running," **uncitable as evidence** — same standing as
generated views; consumer = the director's §8.4 graded control, and the user).
Evidence-grade wants provenance and reproducibility; control-grade wants recency
and cheap compression. A progress digest must never anchor a claim. (Full build
design: `RESEARCH-SYSTEM-TRACE-READER-2026-07-07.md`.)

**Compression by naming:** digests name context components (system prompt vX, task
packet, N files) and descend into individual pieces only on demand — digest →
components → raw. (This **read-order gradient** — readers descend only as deep as
the question demands — recurs throughout the design: tree levels, report→archive,
and here.)

Governance: a **methodology-lane artifact**. (A **lane** is a persistent research
subject; component trees are subject lanes, and the *methodology lane* is the
cross-cutting one — how research itself is run: methods, instruments, this document.
Methodology artifacts are shared by every lane, hence highest blast radius when they
change.) Versioned; instruments change only at epoch boundaries.

**Build note:** this is the one component buildable now, standalone, with immediate
payoff in the existing L1–L5 system — and the best dry run, since the observatory
and the verifier are both roughly "trace reader + a stance." Recommended first
build.

---

## 11. Self-healing & health ✅ (concept; metric definitions 🔲)

Because everything is typed claims and recorded moves, health is **queryable**:

| Signal | Detects |
|---|---|
| Verifier demotion rate | claim inflation / degrading reporting culture |
| Tier mix over time (experiments up, pattern claims flat) | amnesia — learnings not cumulating |
| Rediscovery rate (proposals matching existing entries) | memory failure at proposal time |
| Spend concentration across subtrees (from passive metering) | lock-in |
| Refutation rate of spun-off generalizations | calibration of the system's own inference |
| Unsettled conflict entries piling up | navigation debt |
| Cursor dynamics: swaps without dispatches run (thrash); no swaps ever (lock-in) | director policy failure |

**Signals are symptoms, not verdicts** — interpretation is the point, and the
interpretation rules are **stored co-located with the statistics readout** (one
artifact, never separated), or misreadings recur. Canonical example: spend
concentration alone cannot distinguish lock-in from productive deep exploitation —
the differentiator is whether standing is *moving*. Concentration + advancing
standing = commitment paying off; concentration + flat standing = lock-in. Signals
are read jointly.

**V1 monitor = instrument** (ruled 2026-07-07): all signals recorded from day one at
a known collection point, displayed with their interpretation rules, alert-flavored
("take a look"), zero authority. The judgment layer is the user — or any LLM pointed
at the readout on demand. The autonomous monitor-agent is growth-path; because the
data is recorded from the start, it can be calibrated retroactively against full
history. It watches the director's policy health, never research content, and needs
only the tree + decision log to do it.

**Post-merge validation** closes the outer loop: a merged branch's claims become
predictions; the observatory carries a watch obligation (expected vs. observed
effect; confirm/disconfirm, null results first-class). Watch results do **not**
enter the ledger — they are not hypotheses. A disconfirmation **annotates the merged
node** ("merged, but under-delivered in production — see O-38") and lands in a
distinct, severity-tagged director-facing category: **regressions** (production made
worse) surface at the director's next review; **under-deliveries** (helped less than
claimed) just annotate and recalibrate. Severity will usually be a judged label
rather than a metric delta — fine, since it routes attention, not epistemic weight.
Deliberately light-touch — a field on the node plus one small queue, not a
subsystem.

Proposal-time dedup (D8, ratified): new hypotheses are checked against the
ledger/tree at proposal ("duplicates/contradicts known X") — inform-don't-block;
matches increment support counts; distinct-or-merge is decided by the normal
authority. Amnesia is caught where it happens, not by better filing.

---

## 12. Relationship to prior corpus ✅

### What this supersedes (for this function)

The relay-research unit (`DESIGN.md` §§4–7) is **decomposed, not discarded**:

- Senior's **Area A** (direct the research) → extracted, became the **director** —
  with a surface (the tree) DESIGN.md never specified.
- **Records** → split: evidence-adjudication half → **seam verifier**;
  watch-the-direction half → **monitor**.
- **Interface** → repositioned to the user↔director boundary (ratification,
  frontier re-steer, ledger seeding). 🔲 Day-to-day interaction surface undesigned.
- Senior **Area B** + Juniors → the **research unit** (+ checker). The unit's
  internal anatomy is now an implementation choice — nothing else depends on it.

### What survives from the older designs, transformed

- **Observation/interpretation split** ("the load-bearing idea") → archive/report
  split + citation discipline; observations survive interpretations.
- **Hot-swap / everything-in-writing** → "only what returns through the seam
  exists"; session-resumable unit workspaces; no conversational state anywhere.
- **Thin-skeleton rule**, reconciled with the typed-schema evidence from the
  literature: **type the frame-free layers hard (observations, decisions, claims);
  leave the frame-bound layers emergent** (directions condense by descent).
- **Methodology knowledge is the most durable and highest-blast-radius** → the
  methodology lane; trace-reader governance; instruments versioned at epochs.
- **Claim-type ladder & observer-effect discipline** (Area 5 of the behavioral
  design) → the scope-typed tiers (§5) and logged-steer rule (§8.4) — the surviving
  core of the "heavy" design, minus its machinery.
- The **behavioral-research working notes** (2026-06-21 series) become the design
  trail for the **observatory** layer — that corpus was a field-observation design
  that felt heavy only because it was trying to be the whole system.

### External precedents leaned on

Arbor (tree, coordinator/executor split, merge gates — and the lesson that free-text
`insight` under-types the durable layer); EurekAgent (evaluation authority outside
the acting agent; server-written results); SkillOpt/Trace2Skill (release-gated
documentation; rejected-candidate buffers); HarnessFix HTIR (typed traces incl.
context-assembly events); AutoScientists (dead-ends as first-class; noise floor;
champion discipline — and the warning that Markdown-protocol rules without
enforcement leak). Design rule ratified from the comparison (D7, 2026-07-07): **pin invariants in
machinery, not prose** — the seam, the write gate, and the promotion gate are
machine-checked, not role norms. Scope: ✅-tagged invariants only. Feasibility
caveat (user): if an invariant proves impractical to mechanize at build time, that
is surfaced as an explicit exception for ruling — never silently downgraded to
prose.

Source provenance for a future instance: the external-systems survey,
superseded design, and behavioral working notes came from a private
development workspace and are not included in this repository.

---

## 13. Open items 🔲 (the work queue for the next altitude)

1. ~~Observatory internals~~ — **designed 2026-07-07**: see
   `RESEARCH-SYSTEM-OBSERVATORY-2026-07-07.md` (same directory) — per-run trigger,
   three-layer pass, impact-driven depth (P-1 principle), spine mechanics, ledger
   admission, defect-flow matrix, outputs.
2. ~~Seam formats~~ — **designed 2026-07-07**: see
   `RESEARCH-SYSTEM-SEAM-FORMATS-2026-07-07.md` (same directory) — two-layer
   dispatch (role packet + task package), reference map, living-then-frozen
   report, escalation-shaped interrupt, liveness import, worked example. Borrows
   critically from the L1-L5 launch-packet substrate (import verdicts in its §7).
3. ~~Verifier protocol~~ — **designed 2026-07-07**: see
   `RESEARCH-SYSTEM-VERIFIER-PROTOCOL-2026-07-07.md` (same directory) —
   adjudication pipeline, tier-scaled checks (no default re-runs), bounce loop
   (cap 3 → director), standing rubric, watch-verdict mapping, merge gate,
   mechanical validation rules. Lean-first with explicit v1/growth split.
4. **Trace reader build design** — extraction schema, digest formats, live-tail
   mode. Recommended first build.
5. **Instruments registry** — ownership, versioning, noise-floor bookkeeping;
   rubric construction is run-time craft (senior, ex-ante) but instrument governance
   needs a home.
6. ~~Tree schema~~ — **designed 2026-07-07**: see
   `RESEARCH-SYSTEM-TREE-SCHEMA-2026-07-07.md` (same directory) — node/claim/
   dispatch/tree/ledger schemas, index/detail split, visual-view requirement,
   write-authority map.
7. **User↔director interaction surface** — the old Interface job, redesigned.
8. **Multi-component mechanics** — the one-cursor recursion (§3.4) concretely.
9. ~~Physical layout~~ — **designed 2026-07-07**: see
   `RESEARCH-SYSTEM-PHYSICAL-LAYOUT-2026-07-07.md` (same directory) — sidecar
   principle, research root `research-system/` (this repository's public sibling directory), git/epoch mechanics,
   D7 tooling, cross-tree v1 rules.
11. **L1-L5 evidence-plane alignment** — an architecture review names an emergent
    "evidence plane" (control / work / evidence planes); this system's observatory
    + trace reader + claims machinery is its natural implementation. Parked by
    user 2026-07-07; see A2 note §6. Handle when the L1-L5 ARCHITECTURE.md work
    resumes.
10. ~~Post-merge disconfirmation escalation~~ — resolved 2026-07-07 (§6, §11: outside
    the ledger; node annotation + severity-tagged director category).

---

## 14. Decision Log

### Drafter judgment calls (made for coherence; cheap to reverse)

| # | Decision | Note |
|---|---|---|
| D1 | Role/artifact names: director, senior, junior, checker, seam verifier, monitor, observatory, ledger, trace reader, global learnings layer | placeholders; rename freely |
| D2 | System name | **ruled 2026-07-07**: "Hypothesis Tree Research System"; variants "Hypothesis Tree" / "Research System" recognized; default full name |
| D3 | Report/dispatch skeletons (§8) presented as sketches | not contracts; §13.2 designs them |
| D4 | Supersession mapping (§12) stated as proposed | touches prior commitments; user may re-rule |
| D5 | Status-tag convention reused from DESIGN.md | continuity of reading habits |
| D6 | Claim tiers numbered 1/2/3 by scope | terminology, not new design |
| D7 | "Pin invariants in machinery, not prose" (§12) | **ratified 2026-07-07**; feasibility caveat: impractical cases surface for ruling, never silent downgrade |
| D8 | Proposal-time dedup (§11) | **ratified 2026-07-07** as-is: inform-don't-block, matches increment support counts |
| D9 | The scout move (§4.2) | **ratified 2026-07-07** as-is; scout rate watched via monitor statistics |
| D10 | Write-domain split: navigation state director-authored, epistemic state verifier-authored (§4.1) | **ratified 2026-07-07** |

### User rulings already given (recorded here so they don't re-litigate)

- Restructure around the hypothesis tree; director-level navigation is the hard
  problem being solved. Loops sequential for now; build parallel-aware.
- Third-party adversarial verification owns tree entries; researchers don't write
  their own results into the tree; senior retains authority over the in-loop
  checker, which is unconnected to the seam verifier.
- One dispatch = one leaf. Blocking-questions-only backchannel.
- Single canonical branch; parking creates a conflict state blocking merge/close
  without explicit decision. Same principle extends upward across components.
- Ledger sectioned by provenance; priority user › research › observatory; priority ≠
  epistemic weight.
- Pattern-scope claims: director approval + user ratification. (Expected rare.)
- Archives: transcripts kept but out of the way, never first-class evidence.
- No disclosed resource budgets; done-definitions + goal-function; hidden
  director-side tripwires; budget-exceed is a signal to the director
  (nudge/kill/let-finish, via trace-reader overview).
- Reasoning stream (summarized thinking traces) at the top of the diagnostic stack.
- This note supersedes DESIGN.md **for this function**; final placement TBD after
  the doc overhaul.
- 2026-07-07 ruling batch (walkthrough of all pending items): D7 (with feasibility
  caveat), D8, D9, D10 ratified. Monitor: **instrument from v1** — zero authority,
  signals at a known collection point with co-located interpretation rules;
  autonomous agent = growth-path. Ledger fixed priority ordering **removed**
  (supersedes the earlier user›research›observatory ranking; provenance sections
  remain). Post-merge watch results live **outside the ledger** (merged-node
  annotation + severity-tagged director category; regressions surface at next
  review; severity may be judged, not measured). Ledger-keeper agent noted as
  growth-path. Name: Hypothesis Tree Research System.

### Pending user rulings

- Final documentation placement after the overhaul. (All other rulings resolved in
  the 2026-07-07 walkthrough — see above.)

---

## 15. Glossary

- **Tree** — the hypothesis tree: premise-bearing nodes over harness variants;
  specificity = depth; the director's entire surface.
- **Node standing** — verified status of a premise (untested/supported/weakened/
  refuted/contested), propagated from children. (Fifth state added by A1 schema
  ruling DP-3, 2026-07-07.)
- **Cursor** — the single active node; capacity-1 attention allocation.
- **Epoch** — the interval between trunk movements; the baseline stamp on every
  measurement.
- **Park / conflict entry / settlement** — pausing a sibling; the recorded staleness
  debt (conflict entries are attached to parked nodes in the *tree* — they are not
  part of the ledger); its forced resolution at merge (close / revive-and-remeasure /
  demote to ledger).
- **Claim tiers** — point fact (1) / episode claim (2) / pattern claim (3); typed by
  scope; licensed by evidence type, not rhetoric.
- **Ledger** — provenance-sectioned intake buffer for all hypothesis candidates;
  boundary object between field and lab.
- **Dispatch** — the work atom: one leaf's question + done-definition; everything
  the architecture counts.
- **Seam** — the written boundary between director and unit: dispatch down, report
  up, blocking interrupt, typed steer. Only the report is verifier-adjudicated; the
  other message types are logged but direct.
- **Checker vs. verifier** — in-loop execution QA (serves the unit, under senior)
  vs. the independent seam gate (serves the tree, owns all writes).
- **Trace reader** — the shared instrument: action + reasoning + experience streams;
  detection mechanical, diagnosis interpretive.
- **Observatory** — the field organ watching production runs; feeds the ledger;
  carries post-merge watch obligations.
- **Global learnings layer** — small, verified-only, cross-branch knowledge attached
  to the tree.
- **Done-definition** — achievement-based end condition, operationalized ex-ante;
  the anti-completion-bias contract.
- **Lane** — a persistent research subject. Component trees are subject lanes; the
  methodology lane (methods, instruments, this document) cross-cuts them all.
- **Frontier review** — the recurring user inspection of open directions, spend, and
  standing; the designed human re-steer point.
- **Scout** — a rationed off-path dispatch: counted like any dispatch, moves no
  cursor, can never merge (D9, ratified; rate is a monitor statistic).
- **"Promotion" disambiguation** — four senses, always qualified in this document:
  merge-promotion (branch → trunk, §4.3); ledger-promotion (entry → node, §4.2/§6);
  tier-promotion (claim scope up the ladder, §5); guidance-promotion (into
  actor-visible surfaces — user-owned, §7).
- **Seat notation (Ln / Ln+ / Ln&)** — Ln = execution seat at level n (e.g. L4);
  Ln+ = review/verification seat at level n (e.g. L5+, L3+); Ln& = planning seat
  at level n (e.g. L2&); planning seats exist at L1–L3. C21-split mapping:
  planning-L3 = L3&, execution-L3 = L3. **L/+** = the transition/cross-level
  review seat (the plan-alignment gate; **LE+** is the sanctioned substitute
  wherever the slash would collide with address-path syntax). Gate auxiliaries
  and review-check seats are named sub-seats of their owning gate seat, never
  notated. Per user rulings 2026-07-12,
  `RESEARCH-SYSTEM-MACRO-ARCHITECTURE-2026-07-12.md` §2 + §13 addendum.
