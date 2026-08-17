# Hypothesis Tree Research System — Seam Formats (Design Area A3)

> **Status:** Working note; design converged 2026-07-07.
> **Parents:** `RESEARCH-SYSTEM-CONCEPT-2026-07-07.md`, `RESEARCH-SYSTEM-TREE-SCHEMA-2026-07-07.md`
> (A1 v2), `RESEARCH-SYSTEM-PHYSICAL-LAYOUT-2026-07-07.md` (A2). Same directory.
> **Scope:** item 2 of the concept §13 queue — the dispatch and report as actual
> documents, the reference map, interrupt/steer message shapes, liveness mechanics,
> and a full worked example. Borrows deliberately from the L1-L5 launch-packet
> substrate (`l1-l5-agent-harness/design/working-notes/ROLE-LAUNCH-PACKET-SUBSTRATE-2026-06-18.md`
> and `factory/design/ARCHITECTURE.md` §2/§4); every import is critically reviewed in §7 —
> **imported ≠ trusted**.

Tags: ✅ committed · 🟡 committed concept, mechanics open · 🔲 open.

---

## 1. Seam principles ✅

1. **Self-contained.** A fresh unit with zero system knowledge executes from its
   launch surface alone. No unit ever needs to have read the concept note.
2. **Thin but decision-complete** (imported): the dispatch carries every decision
   the unit is not allowed to make, and nothing else.
3. **Pointer, not payload** (imported): reference material is *addressed with a
   use-when reason*, never inlined "because it's important somewhere."
4. **Only what returns through the seam exists** (concept §8.3) — unchanged.

---

## 2. The dispatch: two layers ✅

Matching the L1-L5 substrate split, a unit's starting surface has two independently
evolving layers:

**Role layer — the role packet.** Senior/junior/checker identity, claim discipline
and tier definitions, report contract, scope rules ("no meta-work; the interrupt is
the only escape"), ex-ante plan obligation. Sourced from canonical role files in
`system/` (methodology lane), assembled into a generated packet with a recorded
**version + hash**. Stable across dispatches; tuned behaviorally like any L1-L5
role surface.

- 🟡 v1 adopts the *shape* (canonical file → generated packet + version + hash) but
  **not** the inline surface-tag projection machinery — with only 3–4 research role
  surfaces, whole-file assembly suffices; tags are justified when role-surface count
  grows (§7, import I-1).
- Role-packet version is recorded on every dispatch record (A1 §3 gains
  `role_packet: {version, hash}`). Packet changes are methodology-lane changes:
  behavior comparisons across packet versions must be labeled — the instrument
  discipline applies to the seam itself.

**Task layer — the task package.** Generated per-leaf by `ht dispatch create`,
mechanically compiled from tree state (DP-A3-1, ruled) plus one optional free-text
director's note.

---

## 3. Task package contents ✅

### 3.1 In-packet (injected)

```
d-2.3.1-1.md
├─ Header       — dispatch id, node, epoch stamp, deliverable type (structure|verdict),
│                 role_packet {version, hash}
├─ Lineage      — premise chain root→here, one line each + standing
├─ Question     — what this dispatch asks
├─ Done         — done-definition (achievement-based; senior operationalizes ex-ante)
├─ Constraints  — relevant global learnings (verified, user-ratified, small by design)
├─ Instruments  — available suites + versions, noise floors, invocation commands
├─ Workspace    — worktree path, branch name, report/archive paths
└─ Director's note (optional, free text — the one non-mechanical section)
```

### 3.2 Reference map (available, not injected)

Generated alongside the packet; each entry carries a **use-when** reason:

```
.reference-map.md
├─ subtree claim history      — "use when building on or contradicting prior results here"
├─ dead ends (closed/refuted siblings + reasons)
│                             — "check BEFORE proposing follow-up hypotheses or
│                                when your plan approaches sibling territory"
├─ unit handbook@vN           — "use when claim tiers, report format, or plan
│                                obligations are unclear"
├─ instrument docs            — "use when an eval/rubric invocation fails or needs options"
└─ archive of this node's prior dispatches — "use when continuing prior work on this leaf"
```

**Honesty note (from import review I-2):** the reference map is *ergonomics*, not a
guarantee. Anti-repeat is **guaranteed** by D8 proposal-time dedup at the ledger and
checked by the checker at plan-QA ("dead ends consulted before follow-ups?" is a
checklist item); the reference map just makes the right behavior cheap. Anchoring
avoidance (dead ends out of the injected packet) is likewise best-effort by
placement, not enforced.

---

## 4. The report ✅

**Living-then-frozen** (imported): the senior maintains `report.md` in the node
workspace throughout the dispatch; `ht report submit` **freezes it mechanically**
(hash recorded on the dispatch record; post-submission edits impossible via
tooling — D7, not prose). The director's mid-dispatch trace-reader overview may read
the draft state — logged as part of the overview, per observer-effect discipline.

Format: Markdown narrative with **fenced structured blocks** for machine-validated
parts (DP-A3-2, ruled). `ht report submit` rejects structurally invalid reports
before the verifier ever sees them.

```
report.md
├─ 1. Header        — dispatch id, outcome, metering echo
├─ 2. Dispatch echo — one paragraph: what was asked, as understood
├─ 3. Plan & deviations — ex-ante plan ref + every deviation, declared
│                         (undeclared deviation found in archive = adjudication
│                          failure; DP-A3-3, ruled)
├─ 4. What was done — brief narrative
├─ 5. Measurements  — fenced block: metric, value, epoch, instrument@v, noise_flag, anchors
├─ 6. Claims        — fenced block: text, proposed_tier, anchors, supporting measurements
├─ 7. Done-definition assessment — coverage vs. ex-ante exhaustion criteria
├─ 8. Proposed hypotheses — ledger candidates: text, rationale, anchors
├─ 9. Off-frame observations — surprises, recorded not investigated
├─ 10. Operational notes — infra/tooling friction (feeds methodology lane)
└─ 11. Archive manifest — archive/ contents and anchor-resolution map
```

---

## 5. Mid-dispatch messages ✅

**Interrupt (unit → director)** — upgraded to the escalation-payload shape
(imported, with a scope constraint):

```
interrupt
├─ when
├─ what happened      — why the dispatch cannot be completed as posed
├─ what was tried
├─ evidence           — anchors into the workspace/archive
├─ options            — SCOPED TO THE DISPATCH: re-pose as X / provide Y / drop
│                       (never tree navigation — "explore 2.4 instead" is the
│                        director's decision space, not the unit's)
└─ recommendation
```

The blocked unit **keeps its context and waits** (imported). The director answers
(re-pose / provide / recall); "the subordinate provides the information; the parent
owns the decision."

**Steer (director → unit)** — unchanged from A1 §3: `{when, message}`,
goal-referenced, never budget-referenced, logged.

---

## 6. Liveness ✅

Import the L1-L5 **journaled terminal-signal + idle-detection** infrastructure;
**do not import its termination policy** (I-5). Differences:

- Dispatch outcomes (`completed | blocked | recalled | retry-operational`) map onto
  journaled terminal signals — the fact-of-signing-off is durable.
- Idle-without-signal triggers a **prod** (their watchdog) and a signal to the
  director (our tripwire) — but never an automatic FAILED. **Termination is the
  director's recall decision**, except for mechanical crash → `retry-operational`.
- The hidden director-side timer (concept §8.4) rides the same idle/elapsed
  detection — no new liveness machinery is built.

---

## 7. Critical import review ✅ (per user instruction: imported ≠ quality)

| # | Import | Verdict | Reservation / adaptation |
|---|---|---|---|
| I-1 | Launch-packet/task-package split; packet version+hash | **Adopt shape** | Surface-tag projection machinery is a *pilot* in L1-L5 (only L5/L5+ migrated) and its "editing tagged prose = editing the packet" rule is discipline-dependent — a prose rule, which our D7 posture distrusts. v1: whole-file role packets + hash; tags only if role-surface count grows. Packet hashes must be validated at spawn, not just recorded |
| I-2 | Reference map with use-when notes | **Adopt, demoted to ergonomics** | Their own telemetry note admits reference maps can invite wandering, and "frequent reading" is ambiguous signal. We do not rely on it: D8 dedup is the anti-repeat guarantee; the checker checklist is the enforcement; the map is convenience |
| I-3 | Escalation payload (happened/tried/evidence/options/recommendation) | **Adopt with scope fence** | "Options" invites mini-navigation; options are fenced to dispatch-repair space, never tree space |
| I-4 | Living-then-frozen report | **Adopt with two guards** | (a) Freeze must be mechanical (hash at submit) or "immutable" is prose. (b) A director-readable draft creates a mild performative-drafting incentive (senior writing for the observer); overview reads are logged, and the verifier adjudicates against the *archive*, not the prose — which bounds the damage |
| I-5 | Watchdog / terminal signals | **Adopt detection, reject policy** | Prod-then-auto-FAIL is right for task hierarchies, wrong here: our director owns termination (recall). Auto-transition only for mechanical crash → retry-operational |
| I-6 | Semantic brief / runtime envelope split | **Adopt as-is** | Low risk; forward-compatible for mixed-runtime units (e.g. GPT-5.5 junior). Envelope stays out of the semantic dispatch |
| I-7 | Sign-off/gate handshake machinery (`.sign-off.<seat>.json`, gate routes) | **Reject** | Our seam verifier (Phase B) is the gate; importing gate semantics would create two adjudication systems at one boundary |
| I-8 | File-read telemetry | **Adopt later, via trace reader** | It *is* the action stream (concept §10); belongs to the trace-reader build, not the seam — noted for the evidence-plane convergence (concept §13.11) |

---

## 8. Worked example ✅ (format test — L4 pinned-contract case)

### 8.1 Dispatch `d-2.3.1-1.md` (task package; role packet `senior@v1` injected separately)

```markdown
# Dispatch d-2.3.1-1
node: 2.3.1 · epoch: trunk@7 · deliverable: verdict · role_packet: senior@v1 (hash 3f2a…)

## Lineage
- 2: context shaping reduces L4 drift [supported]
- 2.3: contract placement matters [supported]
- 2.3.1: ← this dispatch

## Question
Test: pinning the task contract as first user message (vs. system prompt) reduces
L4 instruction drift in long sessions.

## Done
Verdict-grade evidence produced: tier-1 where instruments allow, tier-2 rubric
comparison for adherence. Done when the ex-ante plan's declared coverage is met or
a blocking interrupt is raised. Not done at "promising early results."

## Constraints
- G-4: eval suite S is flaky — always 3 seeds.
- G-7: never edit eval configs inside a dispatch (instrument changes are
  epoch-gated).

## Instruments
- suite-S@v3 (task completion; noise floor ±1.5pp; `ht eval run suite-S`)
- tool-call counter (mechanical; `ht eval run toolcount`)
- adherence: no eval exists — tier-2 rubric path (declare rubric ex-ante).

## Workspace
- worktree: worktrees/2.3.1/ · branch: ht/2.3.1-pinned-contract (forks trunk@7)
- report: nodes/2.3.1/reports/d-2.3.1-1-report.md · archive: nodes/2.3.1/archive/

## Director's note
2.2 established placement matters for short sessions; long-session behavior is the
open half. Prioritize session length > 50 turns if sampling forces a choice.
```

*(Reference map generated alongside: subtree claims of 2.x, dead ends incl. 2.1
"system-prompt-only strengthening — refuted", handbook@v1, instrument docs,
this node's prior dispatches: none.)*

### 8.2 Report `d-2.3.1-1-report.md` (frozen at submit; abbreviated narrative)

```markdown
# Report d-2.3.1-1 · outcome: completed · tokens 412k · wall 3h10m

## Dispatch echo
Asked: test whether pinning the task contract as first user message reduces L4
instruction drift in long sessions, verdict-grade.

## Plan & deviations
Ex-ante plan: plans/d-2.3.1-1-plan.md (declared before execution: implementation
approach; 3-seed suite-S runs; toolcount; adherence rubric R2 on 10 paired long
sessions ≥50 turns; exhaustion = all three instruments run + 10 pairs judged).
Deviation: 1 of 10 paired sessions crashed (infra); replaced with a fresh pair —
declared here, pair list in archive/pairs.md.

## What was done
Implemented contract pinning on branch (diff in archive/diff.patch). Ran declared
instruments. Collected paired transcripts to archive/sessions/.

## Measurements
```yaml
- {metric: task_completion, value: +1.2pp, epoch: trunk@7,
   instrument: suite-S@v3, noise_flag: true, anchors: [archive/evals/suite-S/]}
- {metric: tool_calls, value: -18%, epoch: trunk@7,
   instrument: toolcount@v1, noise_flag: false, anchors: [archive/evals/toolcount/]}
```

## Claims
```yaml
- {text: "Pinned contract reduced tool calls 18% on suite S at trunk@7",
   proposed_tier: 1, anchors: [archive/evals/toolcount/summary.json]}
- {text: "Adherence improved in 8/10 paired long sessions (rubric R2@v1, this component)",
   proposed_tier: 2, anchors: [archive/rubric/R2-judgments/, archive/sessions/]}
```

## Done-definition assessment
Declared: 3 instruments + 10 pairs. Delivered: 3 instruments + 10 pairs (1 replaced,
declared). Coverage complete.

## Proposed hypotheses
- "Pinned-contract effect generalizes across components" — rationale: mechanism is
  attention position, not L4-specific. anchors: [archive/rubric/R2-judgments/]
- "MCP-tool sessions respond differently to contract placement" — see off-frame.

## Off-frame observations
Sessions using MCP tools showed reversed drift pattern in 3/3 such pairs — not
investigated (off-dispatch). anchors: [archive/sessions/pairs-07,09,10]

## Operational notes
Suite-S seed-2 runner needed manual retry twice (G-4 confirmed; runner flakiness
worse on long sessions — methodology-lane candidate).

## Archive manifest
diff.patch · evals/{suite-S,toolcount}/ · rubric/R2-judgments/ · sessions/ (10 pairs
+ 1 crashed) · pairs.md (pairing protocol) · plan snapshot.
```

**Format-test result:** every report section lands in a defined A1 field or ledger
path; the crashed-pair case exercised deviation declaration; the noise-flagged
+1.2pp exercised measurement-without-claim; no schema gaps surfaced. (The verifier
walkthrough of this example — demotion of claim 2 if the ex-ante rubric check
fails, spin-off of hypothesis 1 — is Phase B material.)

---

## 9. Open within this area 🔲

1. Role packet contents per seat (senior/junior/checker) — first drafts belong with
   the handbook; behavioral tuning is ongoing methodology-lane work.
2. Unit handbook@v1 authoring (tier definitions, worked examples).
3. `ht dispatch create` / `ht report submit` command details — Phase B with the
   rest of the tool.
4. Runtime adapter reuse from L1-L5 (`factory/harnessd/spawn/adapters/`) vs. thin fork —
   decide when building; depends on evidence-plane alignment (concept §13.11).
5. Surface-tag machinery adoption trigger (I-1): revisit when role surfaces > ~5.

## 10. Decision log

| Ruling | Decision | Date |
|---|---|---|
| DP-A3-1 | Task package mechanically compiled + optional director's note | 2026-07-07 |
| DP-A3-2 | Report = Markdown + fenced structured blocks, tool-validated at submit | 2026-07-07 |
| DP-A3-3 | Deviations from ex-ante plan: mandatory declaration; undeclared = adjudication failure | 2026-07-07 |
| DP-A3-4 | Dead ends via reference map (ergonomics) + D8/checker (guarantee); learnings in-packet | 2026-07-07 |
| Imports | I-1…I-8 verdicts as §7 (user instruction: critically reviewed, not trusted) | 2026-07-07 |
| Two-layer dispatch | Role packet (versioned+hashed) + generated task package | 2026-07-07 |
| Interrupt | Escalation-payload shape, options fenced to dispatch-repair space | 2026-07-07 |
| Report | Living-then-frozen, mechanical freeze at submit | 2026-07-07 |
| Liveness | Import detection/journaling; termination stays with director | 2026-07-07 |
