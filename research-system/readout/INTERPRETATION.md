---
provenance: director-provisional-2026-07-07
version: v0
---

# Readout — interpretation rules (v0)

Co-located interpretation rules for the observatory's v1 mechanical statistics
(user ruling, A2 §2: the legend travels with the numbers). **Signals are symptoms,
not verdicts.** Every statistic below is a mechanical screen (C1 §3.1, no LLM);
each entry states *what the number is*, *what it may mean*, and *what it must NOT
be read as*. A flagged number is a reason to look, never a finding on its own —
diagnosis is the triage + deep-dive layers' job, judged from what the agent saw
(local-surface discipline), never from omniscient hindsight.

## Behavioral screens (keyed to the spine, `system/observatory/spine.v0.md`)

### time-to-first-relevant-read  (SP-1 orientation)
- **Is:** actions taken before the agent's first read of a task-relevant file.
- **May mean:** high count → orientation waste, the L4 pain (reading unrelated
  material before the task's own files).
- **NOT:** a low count is not proof of good orientation — the agent may open the
  right file and still misread it; and "relevant" is a heuristic classification,
  not ground truth.

### off-task read count  (SP-1 / SP-6)
- **Is:** reads of files outside the assigned task's scope over the run.
- **May mean:** exploration waste or scope drift.
- **NOT:** not automatically waste — some off-task reads are legitimate context-
  building or ruling-out; read it against the brief's scope, not in isolation.

### repeated-identical-call count  (SP-2 tool economy)
- **Is:** count of identical tool calls (same tool + args) with no intervening
  state change — repeated reads, re-listing unchanged dirs, re-derived facts.
- **May mean:** amnesia / context loss, or churn under a stuck sub-goal.
- **NOT:** not always redundant — a legitimate re-read after a write, or polling a
  changing resource, can look identical; confirm nothing changed between calls.

### token spend  (cost anchor)
- **Is:** tokens consumed, attributable per node/episode.
- **May mean:** a cost anchor for impact qualification (cycles/tokens/delay), and
  concentration where effort pooled.
- **NOT:** not a quality or a lock-in verdict — deep-exploitation and thrash both
  spend; spend is the symptom, standing movement + decision_log give the reading.

### errored-call count  (reliability screen)
- **Is:** tool calls returning an error/failure over the run.
- **May mean:** environment friction, malformed calls, or a plan fighting reality.
- **NOT:** not agent fault by default — errors can be the environment's; a low
  count is not health if the agent avoided hard actions to keep it low.

## Defect-flow matrix  (C1 §7 — lives here, valuable beyond the observatory)

The **caught-at × introduced-at** matrix. Caught-at is mechanical (gate events);
introduced-at is a classified attribution. Aggregate hot cells are actionable even
when single attributions are noisy; hot cells are direction candidates for the
tree, and a cell's rate is a free baseline metric.

**Caveats built in (verbatim, C1 §7):**

- **(a) gate-quality confound** — a level that catches nothing looks clean;
  "found at X" ≠ "caused by X"; the two axes exist to keep those apart.
- **(b) Censoring** — the matrix sees only detected failures; escaped defects
  arrive via post-merge watches and user reports and are recorded as
  **caught-at: production** cells, not lost.

Read the matrix as *where defects are found and (noisily) where they were born* —
not as a scorecard of which gate is "good." An empty cell can mean "nothing
happens here" or "nothing is caught here"; only the two axes together tell them
apart.
