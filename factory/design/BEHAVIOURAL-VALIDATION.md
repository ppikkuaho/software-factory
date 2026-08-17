# Behavioural Validation — the trace-through (Phase 6)

> **What this phase is.** The substrate build (Phase 5, increments 0–17) proves the harness is
> mechanically correct — the stage works: the ledger survives a crash, fencing rejects a stale token,
> the chokepoint claims before it spawns. This phase proves something the mechanical tests **cannot
> reach**: that when a real build flows through the system from L1 downward, **the design carries it
> without leaking.**
>
> **Verification (Phase 5) = "did we build the machine right?"** — the mechanism computes correctly.
> **Validation (this phase) = "does the system DO what's desired?"** — the agents, running in-situ, do
> what they're meant to and have the pieces they need.

## The mental model (the user's lens)

Pour real intent in at L1 and watch it flow down through L2 → L3 → L4 → L5 and back up. The **agents
are the medium the water flows through, not the subject** — we trust a strong agent to faithfully
execute its instructions. **The design is what's under test**: the briefs, the handoff contracts, what
each level passes down, whether the pieces a level needs are present. A faithful agent is therefore a
**probe for design gaps** — it does exactly what the design says, so wherever the design is incomplete
or off, the flow breaks *right there*.

### A leak is a flow that ROUTES AROUND a gap, not one that stops at it

The dangerous failure is not the cascade stalling (that is visible and a gift). It is the cascade
**silently routing around a missing piece**: a faithful agent handed an under-specified brief fills the
hole with a plausible default, the water keeps flowing, and the gap surfaces three levels down as work
that is subtly wrong. A leak is: a step skipped, **a system it is meant to leverage not in place**, a
field silently defaulted, a handoff that drops something.

So the load-bearing question of the trace-through is **not** "did water reach the bottom?" It is:
**when under-specified intent is poured in, does the system escalate / bounce (loud), or smoothly
produce plausible-but-wrong work (silent)?** Nearly everything in the L1–L5 design exists to convert a
silent gap into a loud stop — escalate-don't-decide, thin-but-decision-complete briefs, the
plan-alignment gate, trace-blocks, the frozen rubric. The trace-through is what tells us that machinery
actually *fires*.

## The collaboration contract for this phase

The user is **opinionated on the behaviour (the "what")** and delegates the **mechanism (the "how")** —
with the standing condition that *the how must produce exactly that what.* So in this phase the
**behavioural contract** (which behaviours count as correct at each joint) is the user's to set; the
test harness that checks it is the builder's. The behavioural contracts below are **drafts for the user
to be opinionated on**, not decided facts.

## Two instruments, two timings (front-load the cheap one)

The whole point of doing this early is **issue isolation**: a missing piece caught at the L2 brief is
one bug; caught three levels down in the trace it is a forensic hunt. So the cheap, deterministic layer
is front-loaded.

| Instrument | What it catches | Needs | When |
|---|---|---|---|
| **(P) Pieces-present** — deterministic, no agent | a missing field / a dangling ref / a system-it-leverages not in place in the brief+load-manifest a level is handed | `brief.py` (built, Inc 10) + the role docs | **NOW** (front-load) |
| **(B) Behaviour-per-joint** — real agent, rubric-scored | silent gap-filling: an under-spec input → plausible-but-wrong work instead of an escalation | a real in-role boot (Inc 16) | after Inc 16 |
| **(T) Full trace** — the real job, L1-down | emergent leaks across the whole cascade | full substrate + real agents | commissioning |

Type-B and Type-T burn the OAuth subscription window — explicitly **fine** (user, 2026-06-06): the
behaviour is the product, and these are the only way to validate it. They are **evals, not unit tests**:
non-deterministic, scored against an independently-authored rubric (the user's `llm-design-principles`
discipline — independent rubric, no test theater, evaluate-don't-assert-exact-output).

## The observation product

The output of a full trace is not only a pass/fail score. The main human-facing artifact is the
**behavioural run log** defined in `BEHAVIOURAL-RUN-LOG.md`.

That log sits above the deterministic evidence index. It uses ledgers, transcripts, visible reasoning
summaries, inboxes, signals, reports, and review artifacts to explain the run in behavioural terms:
what each agent seemed to believe its job was, why that belief was locally reasonable, where it
matched the intended architecture, where it drifted, and what kind of intervention would steer the
next run better.

This matters because the system is opinionated about behaviour, not merely runtime completion. A
runtime gate can catch an unsafe transition, but it cannot by itself explain why an L3 tried to do
L4/L5 work or why an L4 treated local test-authoring as the normal path. Those are behavioural
questions first. The run log is the artifact where those questions are reconstructed and judged.

## The joints (where the design can leak)

Top-down, so a leak isolates to its joint:

1. **user request → intent-spec** (L1 intake / the grilling session)
2. **intent-spec → L2 brief → concept design** (L2)
3. **concept design → L3 area brief → area design** (L3)
4. **area design → L4 workstream brief → tasks** (L4)
5. **task brief + frozen acceptance → L5 execution → L5+ review** (L5 / L5+)
6. **report-up at every boundary; gate-bounce loop; escalation up + answer down; coordinator-collapse cascade** (the cascade dynamics)
7. **L1 preliminary fidelity playback → owner-final CONFIRM → deliberate control-plane promotion**
   (delivery; a commissioned operator delegate is explicitly labelled and never presented as owner)

## The L1 intake rubric (Inc 19) — DRAFT for the user's behavioural opinion

Applied to a real multi-turn L1 intake run (the agent-under-test driven against the counterpart-sim).
Each criterion is judged against the captured transcript + the produced `intent-spec.md`, scored
pass / partial / fail with evidence (an eval judgment, not a string-match). **The user sets/edits these.**

1. **PIECES-FIRST BOOT** — L1 reads its role docs (the 9-doc load-manifest) BEFORE acting, not after.
2. **STAYS IN LANE** — L1 does INTAKE only: interviews + produces an intent-spec; does NOT start
   architecting, designing components, or writing build code.
3. **OUTCOMES-FIRST QUESTIONING** — its questions probe the user's OUTCOME / tradeoffs (what's it for, who
   reads it, what matters), not implementation trivia first.
4. **THE LEAK TEST (load-bearing)** — on the under-spec scenario, where the human WITHHOLDS a real
   constraint (the manual file-ordering / the image-resolution requirement) unless asked: L1 **ASKS for
   it** (or surfaces the assumption explicitly for confirmation) rather than SILENTLY inventing a default.
   A silent invention = a leak = fail. (On the complete scenario, it should still confirm, not assume blind.)
5. **CALIBRATED ASK-vs-ASSUME** — asks about genuine forks; folds low-risk defaults as FLAGGED assumptions
   (reflected back), not asking everything or assuming everything.
6. **CONTRACT-COMPLETE INTENT-SPEC** — the produced `intent-spec.md` carries the required fields (the
   intake contract per `operational/L1/intake-session-template.md`) — decision-complete for L2, no silent gaps.
7. **REFLECT-BACK** — before finalizing, L1 reflects the understanding back for confirmation (the
   grilling-session close), rather than unilaterally declaring done.

> First (one-shot, pre-counterpart-sim) run showed 1, 2, 3, 5 and the leak-instinct (4) STRONGLY
> POSITIVE. The counterpart-sim run tests them across a full multi-turn intake incl. 6 + 7.

## The L2 architect rubric (Inc 20) — DRAFT for the user's behavioural opinion

A core eval criterion (user, 2026-06-06) is **not** "is the output plausible architecture?" but **"did
L2 do everything it should and FOLLOW ITS PRESCRIBED WORKFLOW?"** — output-plausibility ≠ process-
adherence. Scored against the produced `architecture.md` (eval judgment + adversarial verify), in two
bands:

**A. Workflow-adherence (the M49 Real-Architect Process, `operational/L2/role.md`):**
1. **Decision-surface** — did it identify the architecturally-SIGNIFICANT decisions (expensive-to-reverse
   / cross-module / constraining), not every decision?
2. **Decompose-then-STOP** — components + responsibilities + interfaces at delegate-resolution; NOT a task
   list, NOT detailed design, NOT code (the in-lane test).
3. **Last-Responsible-Moment + subsidiarity** — cross-module/expensive decisions made NOW; domain-deep
   ones deferred DOWNWARD as explicit **constraints** in the per-module specs, not blank TODOs.
4. **Known patterns** — recognized the problem shape, reached for established patterns (hexagonal/ports,
   walking-skeleton, stability-dependency), didn't reinvent.
5. **Spikes** — flagged high-uncertainty decisions for a spike rather than committing blind.

**B. Output-contract completeness (`operational/L2/role.md` → Output Format):**
6. **Component map** + **interface contracts** (coarse) + **ADRs** (`decision` + `rationale` +
   `status: decided | deferred`) + **per-module specs** (deferred = constraints).
7. **Trace-blocks on EVERY element** — each area/module (`kind: requirement`, dotted child id under its
   parent intent-ID), each ADR (`kind: decision`, `DD-NNN`), each derived requirement (`kind: derived`,
   `DR-…` with a live `serves`-link), each interface clause. (A missing/malformed trace-block is a hard
   preflight-hook REJECT — so this is load-bearing, not cosmetic.)
8. **MNF carry-through** — the two must-never-fails (no silent edit loss/corruption; no access-control
   leak) appear as first-class obligations/constraints flowed into the relevant components.

**C. THE LEAK TEST (load-bearing)** — the deliberately-OPEN offline-conflict-resolution policy:
9. L2 carries it as a `status: deferred` ADR (with its recommendation + tradeoffs), or escalates it —
   it does NOT silently invent a policy (last-writer-wins / CRDT / surface-conflict) and bake it in.
   A silent invention = a leak = fail.

> Scored via a multi-agent review of the produced `architecture.md` (independent dimensions + adversarial
> verification, grounded in the real L2 role docs). The user sets/edits these.

## The L3 (planning) design rubric (Inc 21) — DRAFT

Scored against the produced `plan/area-{name}.md` (multi-agent review vs the real L3 role docs):

1. **REALIZE-NOT-REDESIGN** — designs WITHIN the assigned area; does NOT redesign the architecture or
   change L2's concept/area assignment (role.md §Boundaries).
2. **PRESSURE-TEST L2's INTERFACE (the leak test, load-bearing)** — domain analysis catches the planted
   interface flaw (the Search Port has no delivery-time authz recheck → an MNF-2 leak risk) and
   RENEGOTIATES it UPWARD with a proposed correction; does NOT silently absorb the broken interface
   (role.md:20). Silent absorption = a leak = fail.
3. **ESCALATE CROSS-AREA DEPENDENCY** — for the reindex path's dependency on the sibling
   Change-Event Port (ordering / at-least-once unstated), the seat submits a canonical
   `needs_answer` question to L2 (the common ancestor) and parks rather than silently assuming
   (role.md §95, §Boundaries).
4. **DETAILED AREA DESIGN** — workstreams, interface contracts at this level, decisions, a dependency map,
   risks — at the right resolution (designs the area to delegate to L4 workstreams).
5. **TRACE-BLOCKS on every element** — `kind: requirement`, a dotted child id under the parent's prefix
   (`R-003.2` → `R-003.2.1`), `level: L3`; net-new elements only as `DR-` with a live `serves`-link;
   inherited IDs it can't place are escalated, not dropped (the hard preflight requirement).

## The L4 (coordinator) rubric (Inc 22) — DRAFT

Scored against the produced `plan.md` + `acceptance-plan.md` (vs the real L4 role docs):

1. **TASK DECOMPOSITION** — the frozen design is decomposed into executable, parallelizable tasks that
   cover every requirement without redundancy and sequence/recover sensibly; each task has a precise
   brief (scope, acceptance criteria, constraints, context).
2. **TEST-FIRST DISCIPLINE (the leak test, load-bearing)** — L4 recognizes that the design package
   supplies the task spec, falsifiable acceptance criteria, and gate rubric, and that executable
   acceptance is produced before any implementation L5 — never by the implementation executor (the
   anti-theater temporal rule, M51). Current V1 runtime behavior: L4 normally spawns a dedicated L5
   `test_author` child to turn frozen criteria into executable acceptance, waits for L5+ package
   review, binds the accepted package identity, then opens the implementation L5. L3 approval is
   required for post-design refreshes, no-executable-tests exceptions, and criteria/spec-faithfulness
   disputes.
3. **ESCALATE-DON'T-ABSORB (the second leak test)** — the under-framed workstream (W3 reindex, really a
   subsystem) is SURFACED to L3 ("this piece is bigger than the framing because X — here's how I'd
   adjust"), not silently absorbed by expanding what the executors do.
4. **REVIEW-THE-REPORT discipline** — the plan reflects that L4 reviews L5+ reports (not raw L5 code),
   checks coverage + verification claims, and treats "tested and it works" as a signal (role.md).
5. **TRACE-BLOCKS** — each task is a dotted child of its parent design-element id (author order),
   `kind: requirement`; each acceptance test `kind: test` keyed to the requirement it verifies; inherited
   IDs it can't place are escalated.

## The L5 / L5+ (execute–review pair) rubric (Inc 22b) — DRAFT

The eval must check BOTH halves of the M52 pair (user, 2026-06-06 — "check that the L5+ works too"):

**L5 (executor):**
1. **SPEC-FAITHFUL + tests-pass** — implements the task against the FROZEN acceptance tests (makes them
   pass), spec-anchored, literal.
2. **ESCALATE-DON'T-DECIDE** — on a genuine ambiguity in the brief, escalates rather than silently
   choosing (the executor's brief discipline).

**L5+ (independent reviewer — the load-bearing leak test):**
3. **REAL INDEPENDENT REVIEW, not theater** — given L5's output + the spec + the frozen acceptance, L5+
   does its OWN testing pass and writes a report on process quality + spec fidelity. **Plant a subtle
   defect** (code that passes the literal frozen tests but misses the spec intent, OR a real bug the
   tests don't cover): L5+ must CATCH it and BOUNCE — a rubber-stamp "looks good" = fail. (Judgment
   diversity is deliberate: L5 on Codex, L5+ on Claude; in the eval both run on the pinned Claude as a
   stand-in — note the runtime-difference caveat.)

## The full trace-through (Inc 24) — and what it actually validates: the REVIEW MACHINERY

The per-level evals above test each level's PRODUCTION behaviour. The full run tests the **other half —
the up-the-tree REVIEWS/GATES** (user, 2026-06-06): a minimal feature (the markdown-folder → PDF-with-TOC
tool) poured through the WHOLE cascade L1→L2→L3→L4→L5/L5+, small enough that the cascade actually
COMPLETES so we can watch each review fire and confirm it works:

- **L5+ reviews L5** (independent code review, accept/bounce).
- **L4 reviews the L5+ REPORT** (not raw L5 code) — coverage + verification-claim scrutiny.
- **L3 reviews workstream INTEGRATION** — do the L4 workstreams' outputs connect; does the area cohere.
- **L2 COMPATIBILITY REVIEW** — cross-module interface ripples across the planning-L3s (L3&); the freeze point.
- **The PLAN-ALIGNMENT GATE** — the big-bang gate on the assembled plan (the RTM + trace-block walk + the
  judgment checks); PASS is the freeze edge.

**Approach (until the live daemon cascade is commissioned):** a MANUAL trace-through — chain the real
level runs (each level's real output becomes the next level's real input) + fire each REVIEW as its own
real agent run + the counterpart-sim as the human for L1. The goal is NOT the artifact; it is to confirm
each review/gate genuinely fires and catches what it should (plant a defect at one boundary and confirm
the review above it bounces). This is the validation that the system's quality-control spine works, not
just that each level produces plausible output in isolation.

## Steering claims → observable run verification

This table is the canonical pairing between behavioral steering and the evidence an adherence audit
must inspect. The artifacts are observations, not proxy scores. Several claims are more strongly
falsifiable than provable: for example, absence of periodic chatter can falsify spam, but cannot
prove an internal motive. The audit must preserve that distinction instead of manufacturing
certainty.

| Behavioral steering claim | Evidence that proves or falsifies it in run artifacts | Where adherence audit reads it |
|---|---|---|
| Park, do not poll | Turn end has a ledger-derived wait; no repeated status/sleep tool loop; the resolving event later wakes the seat | turn events, WAL/inbox/message timeline, transcript tool digest |
| Escalate ambiguity, do not decide it | A `needs_answer` question precedes affected mutation; the seat parks; answer/arbitration precedes resumed work | messages/open questions, wait reasons, transcript/artifact chronology |
| Stop at the proven claim | L5 names one decision-complete face, drives its live scenario, and leaves unrelated inventory parent-owned | L5 report, accepted test package, live-scenario/red-run log, candidate manifest/diff |
| Boot-read discipline | Declared launch/manifest inputs are read before substantive mutation; no estate sweep or undeclared local-file read | launch manifest, blinder trace, transcript read order, first write/tool event |
| Silence is correct | No periodic “still working” message; every message maps to a durable event, question, verdict, or amendment | message rows, WAL/inbox timeline |
| Trust upward by citation | An upper seat cites current lower verdict/evidence and does not rerun lower work absent a stated credibility reason | parent/gate report pointers, review packet, transcript command history |
| Produce what you owe or explain | Every observed turn end is product-present, ledger-wait, or an explicit prod/checklist defect followed by product or explanation | turn events, owed checklist, return-contract defects, terminal artifact |
| Plan first, then work forward | Native task list and `plan.md` precede substantive mutation and stay aligned with the driven claim | transcript/tool order, `plan.md` snapshots, artifact times |
| Messages persuade; owners amend | Ordinary messages do not change contract fingerprints; revision lineage precedes amendment wake and holder rebind | messages, contract versions/lineage, receipts/rebind records |
| Review the frozen claim, not the author report | Reviewer drives a fresh instance and cites the frozen candidate/current contract fingerprints | review transcript, gate report, packet receipt/snapshot, live-scenario evidence |
| Ordinary completion is quiet until the cohort is ready | Individual terminal rows are silent; exactly one barrier transition wakes the waiting owner | child rows, barrier record, inbox ack/wake timeline |
| Manage the work, not agent activity | Coordinator's phase product follows child evidence; a child completion alone never becomes coordinator completion | parent product/report, gate state, cohort rows, turn-end checklist |

## The increments (Phase 6) — drafts for the user's behavioural opinion

Detailed in `harnessd/IMPLEMENTATION-PLAN.md` (Phase 6). Proposed behavioural contracts:

- **Inc 18 — Pieces-present harness (P, NOW).** For each spawn boundary, assemble the brief + load-manifest
  and assert: every manifest doc PATH resolves (present + readable under the read-allow graph); every
  cross-ref inside a manifest doc resolves (no dangling pointer); the brief carries every field the
  receiving level needs (decision-completeness — spec pointer, frozen acceptance for executor seats); the
  `role_variant` selects the CORRECT manifest (L5 gets swe-handbook, L3 the planning-template, #review the
  reviewer bundle, etc.). Deterministic, no model.
- **Inc 19 — L1 intake behavioural eval (B).** Real grilling-session, a representative request. **Contract
  (draft):** returns a contract-complete intent-spec (8 fields, valid, delivery destination captured); an
  **under-specified** request triggers a clarification/escalation, NOT a silently-invented spec.
- **Inc 20 — L2 concept-design eval (B). Contract (draft):** produces the concept-design artifacts
  (component map + interface contracts + ADRs + per-module specs); had its pieces present; stayed in
  lane (architected — did NOT code or re-open intent); on an injected ambiguity, submitted a
  canonical `needs_answer` question and parked rather than papering over it.
- **Inc 21 — L3 area-design eval (B).** Realization-not-redesign; mints requirement IDs + trace-blocks;
  escalates a cross-area dependency rather than absorbing it.
- **Inc 22 — L4/L5/L5+ execution eval (B).** L4 decomposes + ensures frozen acceptance is authored through the current acceptance-authoring path before L5; L5 is
  spec-faithful + escalates-don't-decides on an under-spec task; L5+ reviews against the frozen rubric and
  bounces a real defect.
- **Inc 23 — cascade-dynamics eval.** A durable direct-edge message reaches the parent and is acted
  on; ordinary child rows remain quiet until the cohort barrier; a gate-bounce loops back to L5; an
  open question parks, receives an answer, and wakes; collapsing a coordinator cascades to its
  subtree.
- **Inc 24 — the full trace-through (T, commissioning).** The real job the user designed: a non-trivial
  build it is *meant* for, poured in at L1, traced slowly L1-down, watching the named joints for leaks
  (esp. silent gap-routing). The goal is not the artifact — it is to watch how the water moves and find
  what we forgot.

---

*Created: 2026-06-06. The behavioural / trace-through phase: validation of the system's desired behaviour
(the agents, in-situ, doing what they're meant to with the pieces they need), distinct from the Phase-5
mechanical verification of the substrate. Behavioural contracts are the user's to set; drafted here for review.*
