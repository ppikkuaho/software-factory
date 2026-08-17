# Hypothesis Tree Research System — Observatory (Design Area C1)

> **Status:** Working note; design converged 2026-07-07.
> **Parents:** `RESEARCH-SYSTEM-CONCEPT-2026-07-07.md` + A1/A2/A3/B4 notes (same
> directory). Design trail: the 2026-06-21 behavioral-research working notes
> (`l1-l5-agent-harness/design/working-notes/`) — standards spine, case review,
> evidence ledger, synthesis ritual; that corpus right-sized to one organ.
> **Scope:** item 1 of concept §13 — the field organ's trigger model, per-run pass,
> spine, admission rules, defect-flow analytics, outputs.

Tags: ✅ committed · 🟡 committed concept, mechanics open · 🔲 open.

---

## 1. Station (recap of ratified position) ✅

The observatory watches **the subject system only** (L1-L5 production runs) — the
three-watcher division: observatory → subject; verifier → research units; monitor →
director. Field evidence **proposes; lab evidence disposes**: observational claims
license hypotheses and priorities, almost never merges. It writes nothing directly —
observations enter the ledger's observatory section through the verifier's gate.
Must not: write the tree, run experiments, decide direction, watch research units.
Primary instrument: the trace reader's **Behavioral Record** (evidence-grade,
post-hoc); the L1-L5 audit event log supplies between-session structure
(`session_id` joins them).

## 2. Trigger model ✅ (DP-O1 ruled)

- **Per-run, automatic:** run completes → hook fires → observatory session spawns,
  analyzes that run, files outputs, exits. No persistent daemon — "constant
  self-monitoring" is an emergent property of trigger cadence, not a resident
  agent (the anti-"roundness" structure).
- **Directed watches:** on-demand bounded assignments ("watch feature X"). Boundary
  vs. research: a watch observes production passively; anything touching a branch,
  change, or instrument run is a research dispatch.
- **Statistics refreshed after every run** (ruled).
- **Synthesis every 3 runs** (ruled): reads accumulated report cards, surfaces
  cross-run patterns, proposes spine candidates and sub-threshold promotions,
  emits the statistics report.
- 🔲 One-time bootstrap backfill over historical runs — decide at build time.

## 3. The per-run pass: three layers ✅

Coverage without reading everything (the detection/diagnosis split as anatomy):

1. **Mechanical screens** — no LLM, 100% of every run: spend per node,
   bounce/revision counts per gate, escalations, retries, watchdog prods,
   tool-call anomalies, time-to-first-relevant-action, file-read patterns.
2. **Triage** — one LLM pass over the behavioral digest + screen flags + spine
   checklist; ranks findings and selects descent targets. Never reads raw
   transcripts.
3. **Deep dives** — subagents on flagged episodes only: empathetic replay from the
   reasoning + experience streams at the flagged spans (local-surface discipline —
   judge from what the agent saw, never omniscient hindsight).

## 4. Depth and impact ✅ (DP-O4, user-corrected)

**The risk is under-exploration, not overspend** (user ruling; second occurrence of
the budget→done-definition inversion — see Decision Log, principle P-1). Therefore
no descent caps in v1. Instead:

- **Done-definition for the pass:** every flag above the impact threshold is
  diagnosed or explicitly deferred with reason; goal-function-enforced against
  premature wrap-up, like any dispatch.
- **Impact qualification** on every finding, from four handles:
  - **measured cost anchors** (from screens: cycles lost, tokens burned, delay
    caused);
  - **recurrence** (echo counts across runs);
  - **within-run comparative ranking** (findings ranked against each other —
    relative judgment over absolute scores);
  - **expected-impact-if-fixed** on ledger candidates — the director's triage
    currency.
  Qualitative tiers (trivial / minor / notable / severe) + cost anchors where
  measurable; estimates labeled as estimates.
- **Escalation = diagnosability boundary, not budget:** a finding whose diagnosis
  exceeds one run's evidence (needs cross-run data, a controlled experiment, or
  sustained watching) becomes a ledger entry proposing a directed watch or
  analyze-dispatch. Attention allocation beyond the pass is the director's job.
- Growth-path: **impact-estimate calibration** — when a fix for a finding later
  merges, realized improvement vs. estimated impact is checkable via the normal
  post-merge watch machinery (predicted-vs-observed, reused).

## 5. The spine ✅ (DP-O2 ruled)

A small, versioned, **user-ratified** list of observable behavioral expectations —
what "good" means, per level/role. It is what makes every pass well-defined instead
of "round": observations are claims about *named expectations*, comparable across
hundreds of runs.

- **Seed small** (≈5–8 entries) from already-observed pains: orientation waste,
  tool-call waste, adherence, escalation quality, acceptance integrity. "We don't
  really know what we should look for yet, and it will vary as the system grows"
  (user) — so the spine **learns**: growth only by promotion (recurring
  observation → ledger → pattern claim → spine entry, user-ratified), never by
  upfront authorship. Premature comprehensive specs shape what observers can see —
  the frame-challenge lesson.
- **Off-spine channel stays open:** surprises are recorded regardless; they are
  where new spine entries come from.
- Ossification guards: entries carry provenance + evidence and are revisitable;
  doctrine-grade blast radius = doctrine-grade authority (user ratification).

## 6. Ledger admission ✅ (DP-O3 ruled)

*Preserve broadly, admit narrowly* (Area 5 lineage). Report cards capture
everything; the ledger — the director's attention surface — admits only:

1. **Spine violations** (pre-agreed important; auto-admissible, verifier-gated);
2. **Recurrences** (an echo on the existing entry — zero ledger growth, rising
   support);
3. **High-severity novelties** (observatory judgment, verifier-gated).

Sub-threshold observations get their second chance at every synthesis, which reads
report cards in bulk and may promote accumulated weak signals. Residual risk is
delay, not loss.

## 7. Defect-flow analytics ✅ (valuable beyond the observatory — lives in the readout)

The **caught-at × introduced-at matrix**:

- **Caught-at is mechanical:** gate events carry gate ID, level, artifact, verdict —
  bounce/revision/loop rates per level are simple counts. "What's carrying its
  weight" costs nothing.
- **Introduced-at is classified:** a per-bounce LLM pass reads the gate's stated
  reason + bounced artifact and assigns failure class + introduced-at + confidence
  (HarnessFix-pattern typed attribution). The L1-L5 path spine (IDs = addresses =
  paths = branches) makes lineage *walkable*, not searched.
- **Aggregate is robust to noisy individual attributions** — hot cells are
  actionable even when single cases are misjudged.
- **Caveats built in:** (a) gate-quality confound — a level that catches nothing
  looks clean; "found at X" ≠ "caused by X"; the two axes exist to keep those
  apart. (b) Censoring — the matrix sees only detected failures; escaped defects
  arrive via post-merge watches and user reports and are recorded as
  **caught-at: production** cells, not lost.
- **Payoff loop:** hot cells are direction candidates for the tree, and the cell's
  rate is a free baseline metric — an eval minted from defect pressure.

## 8. Outputs ✅

| Output | Cadence | Consumer |
|---|---|---|
| Per-run **report card** (bounded format: screens summary, spine results, anchored findings + impact tiers, ledger candidates) | every run | archive; synthesis |
| **Ledger entries** (§6 classes, verifier-gated) | as warranted | director triage |
| **Statistics store** (screens aggregates + defect-flow matrix) | refreshed every run | readout (with co-located interpretation rules) |
| **Synthesis report** (cross-run patterns, spine candidates, promotions) | every 3 runs | user + director |

## 9. V1 / growth-path ✅

| V1 | Growth-path |
|---|---|
| Per-run trigger, directed watches, 3-run synthesis | Bootstrap backfill (build-time decision) |
| Three-layer pass; impact-driven depth, no caps | Spend fences only if overspend is ever observed (P-1) |
| Spine seed + promotion mechanics | Impact-estimate calibration via post-merge watches |
| Defect-flow matrix + caveats | Finer failure-class taxonomy (earned from data, not authored) |

## 10. Open within this area 🔲

1. Observation-record schema — the 2026-06-21 corpus (observation contexts,
   evidence anchors, case format) is the trail; finalize with the trace reader's
   Behavioral Record format.
2. Spine seed's concrete entries — author with the user at build time.
3. Run-completion hook mechanics — with the `ht`/harness build.
4. Synthesis session's own format and done-definition.
5. Evidence-plane contract with L1-L5's existing observability stack — parked
   (concept §13.11).

## 11. Decision log

| Ruling | Decision | Date |
|---|---|---|
| DP-O1 | Per-run auto trigger; stats every run; synthesis every 3 runs; no daemon | 2026-07-07 |
| DP-O2 | Spine: seed ≈5–8 from known pains, grow only by promotion, user-ratified | 2026-07-07 |
| DP-O3 | Ledger admission: spine violations + recurrences (echoes) + high-severity novelties; rest in report cards + synthesis | 2026-07-07 |
| DP-O4 | **User-corrected:** under-exploration is the risk — no caps; impact-driven depth (cost anchors, recurrence, comparative ranking, expected-impact-if-fixed) + pass done-definition; escalation at diagnosability boundary. Mechanism details = drafter proposal, flagged for veto | 2026-07-07 |
| **P-1 (principle)** | *Design against under-exploration; add spend fences only on observed overspend.* Second occurrence of the inversion (dispatch budgets → done-definitions; observatory caps → impact-driven depth) promotes it from dispatch-design fact to general design principle | 2026-07-07 |
