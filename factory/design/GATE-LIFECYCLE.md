# Gate Lifecycle and Behavioural Iteration Plan

*Created: 2026-06-15. Status: active planning/spec bridge. This document turns the
quality-gate doctrine into an implementation plan and names the remaining design decisions before
behavioural tuning becomes the main loop.*

## Current frozen-input and communication contract — 2026-07-24

Every review packet identifies the current frozen contracts by notary stamp and holder receipt,
alongside the distinct candidate manifest/hash and immutable candidate snapshot. A contract receipt
says which contract version the review seat was given; the candidate identity says which submitted
bytes it judges. Do not conflate them.

If a contract owner amends a governing contract, the immutable revision lineage precedes an
ordinary `contract_amendment` message. The review seat re-reads and explicitly rebinds before
claiming the new version. Stale receipts remain advisory during ordinary work, turn end, and the
live owed checklist. At the candidate-submission choke point only, a producer holding a stale
receipt is refused with `STALE-CONTRACT-RECEIPT`, the stale contract path, the current revision
record, and the explicit re-read → contract-rebind marker → fresh-signal resubmit recovery.

Normal nonterminal coordination uses canonical direct-edge messages and open questions. An
arbitration-tagged question routes altitude uncertainty to the owning parent. Legacy coordination
handoffs, answer-down, and special `ESCALATED` routing are compatibility/history, not gate
authority. Review-check completion rows accumulate silently; one `review_check` cohort barrier
wakes the gate lead for synthesis.

## Load-Bearing Invariant

Executor output is only a candidate. The review gate owns forward motion.

A producing seat (`#exec`) can submit work for review. It cannot mark a boundary complete, wake the
parent as ready, merge upward, or promote the product. Only the review gate can pass work forward.

This preserves the central separation of concerns:

- the executor produces;
- the reviewer verifies against the frozen standard;
- the gate synthesizes the verdict;
- the parent consumes gate-cleared work at its own altitude;
- the behavioural lab observes without changing the process.

The producer never signs off on its own work. A parent-visible completion without a gate PASS is
drift.

This invariant governs completion, not all communication. Normal direct-edge messages allow a
running child to ask its parent about a nonterminal plan/interface/approval issue and a parent to
send clarified guidance. Those events are routing evidence only; they do not PASS work, collapse a
node, amend a frozen contract, or replace the review gate.

Gate authority is the committed harness state, not the review artifact by itself. The review
artifact is the reviewer's evidence and rationale; it becomes parent-consumable only when the
watchdog accepts the review signal, commits `gate_state=gate_passed`, and appends a `gate_passed`
pointer to the parent inbox. A parent waiting on child work reads the `gate_passed`, `gate_failed`,
or `gate_escalated` routing line, or the equivalent child binding state, as the status source.
`gate_bounced` is normally the producer's repair loop rather than a parent inbox route; if the child
binding shows `gate_bounced`, dependent parent work remains held until a later route lands or an
altitude issue is explicitly escalated. If a review artifact says ACCEPT but the harness routes
`gate_failed`, the candidate has not passed; the parent owns the recovery decision before dependent
work proceeds.

## Scope

This document sits below `design/QUALITY-GATE.md` and above code implementation. It does not replace
the quality-gate philosophy; it makes the operational lifecycle explicit enough to build and test.

It covers:

- the gate state machine;
- what each gate reviews against;
- how review work decomposes;
- required gate artifacts;
- the harness enforcement spine;
- the build order;
- passive observability;
- the behavioural tuning loop after the system is ready.

It does not cover the plan-alignment gate in detail. `design/PLAN-ALIGNMENT-GATE.md` remains the
design-cycle to build-cycle checkpoint. This document covers the build-cycle gates that decide
whether completed work can move upward.

The behavioral contracts for the L4/L3/L2 portfolio gates live in
`design/HIGHER-LEVEL-GATES.md`; this document owns the lifecycle mechanics those contracts ride.

## State Model

The work-node lifecycle needs a first-class gate state, separate from the executor lifecycle.

```text
exec_running
-> candidate_submitted
-> gate_open
-> gate_reviewing
-> gate_passed | gate_bounced | gate_escalated | gate_failed
```

Implementation status: the current bridge has the binding-level spine for
`candidate_submitted`, `gate_passed`, `gate_bounced`, `gate_escalated`, and
`gate_failed`. `gate_open` and `gate_reviewing` remain design states for a
future first-class reviewer-seat runtime. The implemented bridge enforces the
closed `gate_state` enum and event-level transition table in `harnessd.validate`.

Meanings:

- `exec_running`: the producing seat is working.
- `candidate_submitted`: the producing seat has produced a candidate and its report.
- `gate_open`: the harness has accepted the candidate for review and made the review packet
  available.
- `gate_reviewing`: one or more review seats are active.
- `gate_passed`: the gate has accepted the work. This is the only parent-forwarding state.
- `gate_bounced`: the gate found typed defects and returned work to the producer. Every bounce is
  also a durable LOOK-HERE audit signal—something probably went wrong and must be inspected—even
  if a later candidate passes.
- `gate_escalated`: the gate cannot decide at its altitude and asks the parent.
- `gate_failed`: the gate cannot proceed because the review substrate, artifacts, or ownership
  contract is broken. Recovery is parent/operator-owned: repair the substrate, then call
  `gate-retry <producer>` to resubmit the held candidate.

Implemented bridge transition table:

| Event | Previous `gate_state` | Next `gate_state` | Executor lifecycle | Owner |
|---|---|---|---|---|
| `gate_candidate_submitted` | none, `gate_bounced`, or `gate_failed` after parent/operator retry | `candidate_submitted` | `running -> running` | producer `#exec` DONE interpreted as candidate submission, or repaired failed gate resubmitted |
| `gate_bounced` | `candidate_submitted`, `gate_bounced`, or parent-returned `gate_escalated` | `gate_bounced` | `running -> running` | review gate returns typed defects, or the parent returns its ruling canonically, while the producer keeps context |
| `gate_escalated` | `candidate_submitted`, `gate_bounced`, or `gate_escalated` | `gate_escalated` | `running -> running` | review gate asks parent altitude; duplicate escalations are suppressed while pending |
| `gate_failed` | none, `candidate_submitted`, `gate_bounced`, `gate_escalated`, or `gate_failed` | `gate_failed` | `running -> running` | harness reports review-substrate or ownership failure to parent |
| `gate_passed` | `candidate_submitted`, `gate_bounced`, or parent-accepted `gate_escalated` | `gate_passed` | `running -> done` | review gate accepts, or parent accepts an escalated gate; this is the only parent-forwarding completion |

Every gate event must write the expected `gate_state` in the transition delta, and the
validator rejects unknown gate-state strings or mismatched event/state pairs before commit.

Open signal decision:

- Preferred: introduce `SUBMITTED`, `PASS`, and `BOUNCE` terminal/control signals.
- Acceptable bridge: keep `DONE` for producing seats but reinterpret `#exec DONE` as candidate
  submission when a gate is required. Parent wake remains gated on `#review PASS`.

The preferred form is clearer because it makes the completion boundary explicit in the agent's
language.

## Review Packet

The gate reviews against standards that were set before execution. The executor does not define the
standard at submission time.

A review packet should contain pointers to:

- the parent brief or spec;
- inherited requirement IDs and the trace map;
- frozen acceptance criteria or rubric;
- ADRs and interface contracts in scope;
- lower-level gate verdicts;
- expected direct lower execution child evidence for ACCEPT (`L2 -> L3`, `L3 -> L4`, `L4 -> L5`);
- deterministic test or CI evidence where applicable;
- verification-runtime pointers where the harness can discover them, with artifact-declared
  commands parsed only from explicit `Verification Commands` sections and all local probes treated
  as review hints rather than daemon-certified execution authority. Shell/plain fenced blocks,
  explicit `Command:` lines, and inline command references in those sections may be promoted; fenced
  result blocks such as `text` are not commands;
- the produced artifact, branch, or diff;
- the producer's `report.md`;
- the harness-owned `reviews/<gate-id>/candidate-artifacts.json` manifest that freezes the producer
  artifact set and hashes at candidate submission, plus immutable snapshot copies under
  `reviews/<gate-id>/candidate-snapshot/` for parent/audit proof;
- known escalations, decisions, deviations, and unresolved concerns.

The packet is pointer-not-payload. It tells the review seat what to read, not what to believe. The
live node artifacts are the candidate workspace during review. The candidate artifact manifest is
the routing identity for that mutable producer output: a review verdict routes only while current
producer artifacts still match the submitted manifest. The snapshot directory is the historical
proof copy for what the gate accepted. This matters because accepted candidates may later be
reworked into the next phase's workspace, such as plan-alignment PASS freezing an area design and
replacing a child task package for execution. Node-root `plan.md` is living process bookkeeping and
is not part of that frozen candidate identity; it remains available to reviewers as context, but
checklist updates after candidate submission do not invalidate the submitted deliverable.

Rubric pointers in the packet must resolve to existing artifacts. For product-level L2 candidates,
the frozen rubric source may be the intent/spec, validated plan package, or plan-alignment decision
material rather than a node-local `acceptance.md`.

## Deterministic Floors Versus Judgment Gates

Automatable checks belong to the producing seat before submission. If a check is mechanical,
objective, and available in the task environment, the executor runs it as part of producing the
candidate. This includes frozen acceptance tests, unit tests, linters, formatters, type checkers,
builds, and configured CI/security scans.

The review gate does not exist to replace deterministic tooling. It exists to spend independent
judgment on what deterministic tools cannot settle: spec fidelity beyond asserted tests, code or
artifact quality, minimalism, readability, maintainability, interface fit, overengineering,
unresolved ambiguity, and composition at the reviewer's altitude.

For the L5/L5+ pair this split is load-bearing:

- `L5#exec` owns implementation and the deterministic quality floor. A candidate with red or
  unrun mechanical checks is not ready for review.
- `L5+#review` owns the independent non-deterministic review. It may re-run tests to validate the
  evidence and probe untested edges, but its primary authority is judgment against the full frozen
  spec and rubric.

Higher-level gates consume this lower evidence by pointer. L4, L3, and L2 may question missing,
contradictory, or non-credible lower evidence, but they do not casually redo leaf-level code review.
Their main work is portfolio-level composition.

## Review Decomposition

Every `#review` seat is the gate orchestrator for its node. It owns
`review-plan.md`, review-check dispatch, synthesis, and the terminal verdict.
The selected check reviewers provide findings only. They do not vote, repair
the candidate, author the final gate artifact, or sign the boundary-crossing
signal.

The agent-facing operating contract is `operational/shared/review-handbook.md`.

For L4+ and L3+, FULL mode decomposes into four first-class review-check seats:

- **Fidelity and coverage:** does the output satisfy the frozen spec,
  decisions, rubrics, and requirement IDs?
- **Composition and interface integrity:** do the produced parts compose at this
  level's altitude, and are contracts honored across boundaries?
- **Evidence credibility:** were lower verdicts and promised checks produced,
  and are the results credible enough to rely on by pointer?
- **Risk, substrate, and handoff/readiness:** are material risks,
  ownership/substrate issues, deviations, and parent/requester handoff gaps
  visible and dispositioned?

Four remains the unconfigured roster. For a commissioned panel-width
experiment, repeated `harnessctl start --review-panel-arm` declarations may
replace an L3 module candidate's roster with an ordered subset of those axes
or the registered `broad` unscoped check; declaration order is authoritative
and the first matching canonical L3 producer-address pattern wins. The same
required-spec seam controls role-selection validation, report enumeration,
completion provenance, and seat dispatch. L4 workstream panels and the L2+
product/probe cell ignore this module-only parameter.

For L2+, FULL mode decomposes into five first-class review-check seats:
fidelity-coverage, composition-interface, risk-readiness, user-simulation, and
performance-robustness. Evidence-credibility retires as a separate L2+ seat.
Risk-readiness absorbs the exact three security basics; the two product probes
drive a content-addressed journey/MNF roster against separate writable
disposable candidate copies.

The older six/seven check names are subchecks under these axes, not required
report files. SHORT mode is only for a genuinely small candidate where one
right-sized review task covers the material questions; missing reviewer
substrate is not a SHORT reason.

```text
gate lead / verdict owner
  <- fidelity-coverage reviewer
  <- composition-interface reviewer
  <- risk-readiness reviewer
  <- evidence-credibility reviewer (L3+/L4+)
  <- user-simulation reviewer (L2+)
  <- performance-robustness reviewer (L2+)
```

Rules:

- one reviewer should receive one right-sized judgment task;
- reviewers judge against the frozen standard, not against producer intent;
- reviewers produce findings, not fixes;
- tentative findings are first-class and must not be collapsed into confident pass/fail language;
- the gate lead turns findings into `PASS`, `BOUNCE`, or `ESCALATE`;
- the gate lead does not produce or repair the work;
- in FULL mode the gate lead waits for the complete level-specific roster and does not
  author them.

Review-check decomposition is normal for upper gates. SHORT mode is the narrow
gate sizing exception and must be recorded with evidence in `review-plan.md`.

## Per-Level Gate Contracts

### L5 Unit Gate

Producing seat: `L5#exec`.

Review seat: `L5+#review`.

Review altitude: one unit of implementation.

Producing-seat responsibility: write the implementation, run the frozen acceptance tests, run
internal tests, run the configured deterministic code-quality floor, and report exact evidence.

Gate artifact: L5+ `report.md` verdict table. No separate verdict file unless later evidence shows
the report artifact is too overloaded.

Reviews against:

- frozen `acceptance.md`;
- L5 brief and requirement IDs;
- relevant ADRs and interface clauses;
- code diff or produced artifact;
- L5 report and deterministic evidence;
- non-deterministic quality criteria: spec fidelity beyond test assertions, correctness risks,
  minimalism, readability, maintainability, convention fit, overengineering, and unowned ambiguity.

PASS wakes L4. BOUNCE wakes L5 and preserves the producer's L5 context. The reviewer turn closes;
a later candidate gets a fresh reviewer incarnation at the same logical `#review` address. Repeated
bounce past the configured cap escalates to L4.

### L4 Workstream Gate

Producing seat: `L4#exec`.

Review seat: `L4#review`.

Review altitude: workstream composition. The gate does not re-review every L5 line. It checks
whether accepted task outputs compose into the workstream and honor the workstream spec.
Because deep modules are the rubric, interface integrity is a primary review surface here, not an
optional checklist item.

Gate artifact: `composition-report.md`, produced by the review seat. The producing L4's own
candidate summary remains `report.md`.

Reviews against:

- L3 workstream brief;
- workstream requirements and trace IDs;
- frozen task acceptance criteria;
- L5+ verdicts;
- integration evidence;
- task interface contracts and cross-task assumptions.

It consumes L5/L5+ evidence rather than redoing it. A lower-level quality problem is in scope only
when it is visible as a composition failure, missing or non-credible evidence, or a materially
missed L5+ finding.

PASS wakes L3. BOUNCE wakes L4. Escalation goes to L3.

### L3 Area Gate

Producing seat: `L3#exec`.

Review seat: `L3#review`.

Review altitude: area/module composition. The gate checks whether workstreams compose into a coherent
area and whether the area exposes the interfaces L2 expected.

Gate artifact: `area-composition-review.md`, produced by the review seat. The producing L3's own
candidate summary remains `report.md`.

Reviews against:

- L2 area/module spec;
- L3 design;
- workstream reports and L4 gate verdicts;
- internal and exposed interface contracts;
- cross-workstream integration evidence;
- area requirement coverage and trace IDs.

It consumes workstream verdicts rather than relitigating individual task implementation quality.
Its question is whether the workstreams form the area/module L2 asked for.

PASS wakes L2. BOUNCE wakes L3. Escalation goes to L2.

### L2 Product Composition Gate

Producing seat: `L2#exec`.

Review seat: `L2#review`.

Review altitude: product/system composition. The gate checks whether areas/modules compose into the
product architecture and honor the intent-spec constraints L2 was responsible for translating.

Gate artifact: `composition-review.md`.

Reviews against:

- frozen intent-spec and requirement map;
- L2 architecture and ADRs;
- area/module specs;
- L3 gate verdicts;
- cross-module interface contracts;
- system-level integration evidence;
- known deviations and open risks.

It consumes area/module verdicts rather than relitigating workstream or task details. Its question
is whether the portfolio composes into the product/system L1 intended.

PASS wakes L1. BOUNCE wakes L2. Escalation goes to L1.

### L1 Intent-Fidelity Gate

Producing seat: the cascade below L1 has delivered gate-cleared product output.

Review owner: L1 for the preliminary judgment; the owner for final accept. A commissioning
operator is valid only under the explicit run-scoped delegate binding and is never labelled owner.

Review altitude: client intent. The question is not "did lower levels pass their checks?" but
"does the delivered product satisfy what the client actually asked for?"

Gate artifacts: `fidelity-judgment.md`, one content-addressed owner-question package, and its
immutable answer.

Reviews against:

- frozen intent-spec;
- delivery destination contract;
- product as the user experiences it;
- lower gate verdicts;
- material deviations.

L1 preliminary accept posts the owner playback question. Owner CONFIRM authorizes—but does not
side-effect—the deliberate control-plane promotion command. REJECT wakes L1 and sends the owner's
exact reason through one canonical direct-edge repair message to the live L2 project child.

Implementation status: the owner-final playback edge is built. `harnessd.promote` requires
`client-brief/fidelity-judgment.md` (or the single project-subtree equivalent) with
`Preliminary Verdict: accept`, one complete exact-evidence row per frozen outcome and MNF, a
current content-addressed playback question, and a notary-current CONFIRM answer carrying allowed
authority. Missing, ambiguous, stale, wrong-authority, or rejecting surfaces hold the gate and
leave `deliverable_state` untouched.
Promotion normalizes §8 filesystem destinations before copying and, when the frozen spec carries
§8, uses that spec row over a stale cached binding destination. If the deliverable product surface
is below the project node root, the promote request carries `delivery_source` / `--delivery-source`;
the daemon resolves it inside the node workspace and ships that product surface instead of a node
archive.

## Harness Enforcement

The harness must make the invariant mechanically true.

Required enforcement:

- a producing `#exec` seat cannot wake the parent as complete;
- `#exec` candidate submission opens or queues the gate;
- an L2-owned L3 `#exec` child, including planning-L3 (L3&) and execution-L3 (L3) seats, may be registered as
  `planned` but held at `admission_state=waiting_on_sibling` until its immediate predecessor
  reaches `gate_state=gate_passed`;
- if that predecessor reaches a non-passed terminal or parent-visible non-passed gate state, the
  successor becomes `admission_state=blocked_on_sibling` and L2 receives a
  `serial_admission_blocked` pointer; L2 owns retry, resequencing, cancellation, or escalation;
- only `#review PASS` writes the parent-facing completion/wake;
- `#review BOUNCE` writes typed defects to the producer inbox;
- the producer context survives bounce;
- the review context does not survive a verdict turn: PASS, BOUNCE, and ESCALATE close the reviewer
  and best-effort kill the review pane, and a later candidate opens a fresh reviewer incarnation;
- bounce count is tracked and capped;
- cap exhaustion escalates to the parent;
- gate PASS is refused if required gate artifacts are missing or unfilled;
- gate PASS is refused if required lower verdicts are missing;
- parent promotion or merge paths require gate PASS, not producer DONE.

The terminal-signal path is the right enforcement point. A dashboard warning is not enforcement.

## Build Plan

### Post-Night-Loop Status Note (2026-06-17)

The `night-loop-2026-06-16` branch completed a real hierarchy run through promotion, which validates
the broad shape of Increments 1-5. The branch review of HEAD `7fddedd` adds four plan-level
constraints before further unattended runs are treated as reliable behavioural evidence:

- wake/inbox ack is still structurally unsafe on provider-error transcript growth (WD-1 / LR-130);
- candidate output artifacts now have manifest identity at submission (LR-131); validate the
  implementation under the next clean run;
- observability must separate neutral review-routing attention from failure/runtime contamination
  before dashboards become review surfaces;
- test-author doctrine has been superseded by Plan 8's two-artifact split: planning-L3 owns
  falsifiable criteria/rubrics, while execution-L4 normally uses an L5 `test_author` plus L5+
  package review to produce executable acceptance before implementation L5 opens; the next run
  should validate that L4s follow that sequence and bind accepted package identity before
  implementation.

These do not invalidate the gate lifecycle spine. They become the next hardening slices around the
spine before Increment 6/7 behavioural iteration is relied on.

### Increment 1: Spec Canonicalization

- Update `QUALITY-GATE.md` with the gate-owned-forwarding invariant.
- Update `DAEMON.md` / `WATCHDOG.md` / `TRANSPORTS.md` with gate lifecycle routing.
- Update role docs so producing seats submit candidates and review seats pass/bounce.
- Add the missing L3 gate artifact to the doc-system block set.
- Decide signal vocabulary: explicit `SUBMITTED/PASS/BOUNCE` versus reinterpretation of `DONE`.

Done when the docs answer who owns every boundary transition.

### Increment 2: Gate Data Model

- Add gate state to binding/WAL state.
- Add gate identity keyed by node path plus boundary level.
- Record gate packet pointers.
- Record reviewer seat bindings.
- Record bounce count, last verdict, and parent-forwarded-at timestamp.

Done when replay can reconstruct gate state from WAL alone.

### Increment 3: L5/L5+ Vertical Slice

Start with the leaf pair because seat-qualified addresses and L5+ already exist.

Scope note: Increment 3 built the binding-level spine and watchdog/chokepoint behavior. Increment 4
has now started wiring that spine into production child registration: L2/L3/L4/L5 producer spawns
are stamped with `gate_required` and `gate_review_address`, and the paired planned `#review` binding
is stamped with `gate_for`. The remaining production work is the fuller gate-state enum, review-seat
open/failure handling, and merge/promotion integration.

Tests first:

- L5 candidate submission does not wake L4. **Implemented at the binding-level spine:** gated producer `DONE` now records
  `gate_state=candidate_submitted`, keeps the producer running, and appends one
  `candidate_submitted` pointer line to the paired `#review` inbox. Submission first joins the
  producer's contract receipts to current owner versions and refuses stale receipts without
  changing the gate state or opening a review packet.
- L5+ PASS wakes L4. **Implemented at the binding-level spine:** review `DONE` with `VERDICT: ACCEPT` now finalizes the
  held producer, finalizes the review seat, and appends one `gate_passed` pointer line to the
  parent inbox.
- L5+ BOUNCE wakes L5 and not L4. **Implemented at the binding-level spine:** review `DONE` with `VERDICT: BOUNCE`
  records `gate_state=gate_bounced`, increments the producer's bounce count, and appends one
  `gate_bounced` pointer line to the producer inbox without waking the parent. The review seat is
  finalized and its pane is best-effort killed after the verdict. The same transition writes the
  loud gate-bounce audit signal used by live rows and run reports.
- L5+ cannot PASS without the verdict artifact. **Implemented:** L5+ `DONE` now fails the
  return-contract floor unless `report.md` contains `VERDICT: ACCEPT`, `VERDICT: BOUNCE`, or
  `VERDICT: ESCALATE`; the review seat signs `DONE` with `evidence.verdict` carrying the same
  routing value, and signal/report mismatches are refused.
- L5+ cannot PASS without citing given requirement IDs. **Implemented:** the existing L5-class
  requirement-citation floor is now pinned on the review PASS path.
- bounce preserves L5 producer context. **Implemented at the binding-level spine:** the producer
  remains running on BOUNCE, while the reviewer closes and its pane is best-effort killed; a later
  producer resubmission reopens the paired `#review` binding with a fresh lease/owner token before
  review.
- loop-cap exhaustion escalates to L4. **Implemented at the binding-level spine:** the producer binding's
  `gate_bounce_cap` bounds local BOUNCE loops; when exhausted, a later BOUNCE records
  `gate_state=gate_escalated` and appends one `gate_escalated` pointer line to the parent inbox.
  The bridge default is `3` when no binding-specific cap is set. The review seat is finalized and
  its pane is best-effort killed after the escalation. Once the producer is `gate_escalated`, later
  fresh BOUNCE/ESCALATE signals from an already-closed or stale review seat do not emit another
  parent escalation. Later producer DONE re-polls, including rewritten DONE artifacts, also do not
  re-enter candidate submission or fail on the now-terminal single-use review seat; the pending
  parent-altitude decision owns the next move.
- parent reads only gate-cleared work. **Implemented at the binding-level spine:** the submit→PASS regression test pins that
  the parent receives no completion line on candidate submission and later receives exactly one
  `gate_passed` line from the review gate.
- parent treats review artifacts as evidence only. **Prompt/contract hardened after LR-151:** parent
  role and spawn surfaces now instruct L2/L3/L4 to wait for the harness `gate_passed` pointer or
  binding state before consuming a child as complete, and to stop dependent work on `gate_failed`
  even when a review artifact contains an ACCEPT rationale.

Done when the L5/L5+ lifecycle is enforced in the harness for real spawn-created bindings, not only
test-seeded bindings.

### Increment 4: Higher-Level Gates

Before broadening the code path, design the reviewer roles for L4, L3, and L2 explicitly. These
are portfolio/composition gates, not scaled-up L5+ code reviews.

**Designed:** `design/HIGHER-LEVEL-GATES.md` now owns the per-level packet, artifact, reviewer
topology, PASS/BOUNCE/ESCALATE, and bounce-scope contracts.

For each higher-level gate, specify:

- the review altitude and what it must not re-review;
- the review packet inputs and evidence pointers;
- FULL mode's level-specific review-check roster and the SHORT exception conditions;
- the required nested review-check report paths;
- the exact gate artifact schema;
- PASS, BOUNCE, and ESCALATE criteria;
- bounce target and scope;
- deterministic prerequisites before judgment is spent.

Then apply the same lifecycle mechanics to L4, L3, and L2:

- derive gatedness fail-safe from role/config at spawn time, write `gate_required` / `gate_for`,
  preserve gate fields across respawn, and hold/refuse on ambiguity rather than failing open.
  **Partially implemented:** `register_and_spawn_child` now derives gatedness for L2/L3/L4/L5
  producer seats, stamps `gate_required` + `gate_review_address`, and creates the paired planned
  `#review` binding with `gate_for`;
- spawn or open review seats for each gate, and write `gate_failed` when the review substrate cannot
  be opened. **Implemented for the binding-level spine:** the paired review slot is registered as planned, redrive
  opens it only after `candidate_submitted`, and a failed redrive open marks the producer
  `gate_failed` plus wakes the parent. Candidate submission also refuses to enter
  `candidate_submitted` when the `#review` binding is absent, terminal, or bound to a different
  producer; that case marks the producer `gate_failed` and wakes the parent immediately. A repaired
  failed gate is recovered only through the parent/operator control-plane command
  `gate-retry <producer>`, which validates that the review slot is present, non-terminal, and bound
  to the producer before moving the producer back to `candidate_submitted` and replaying the review
  pointer. A review slot that becomes missing, terminal, or misbound after `candidate_submitted`
  is detected during the daemon recovery sweep and fails the producer closed into `gate_failed`;
- create review packets from node artifacts;
- create `reviews/<gate-id>/review-packet.md` when a gated producer submits a candidate.
  **Implemented for the binding-level spine:** candidate submission writes a harness-owned pointer
  packet plus `reviews/<gate-id>/candidate-artifacts.json` and immutable submitted bytes under
  `reviews/<gate-id>/candidate-snapshot/`, records `gate_id`, `gate_review_dir`,
  `gate_review_packet`, `gate_candidate_artifact_manifest`, and
  `gate_candidate_artifact_manifest_sha256` plus `gate_candidate_artifact_snapshot_dir` on the
  producer binding, and includes the packet pointer in the review-seat inbox line. Later producer
  DONE re-polls while the producer is already
  `candidate_submitted` are idempotent and do not create new packet directories unless an explicit
  retry/repair transition commits a new candidate;
- enforce candidate output immutability during review. **Implemented:** the watchdog checks the
  producer's current artifact hashes against the submitted `candidate-artifacts.json` before routing
  a review verdict. If producer-owned artifacts were added, removed, or changed after submission, the
  review signal is refused with a typed defect and the producer gate moves to
  `gate_failed` / `failure_class=candidate_artifact_drift`; recovery requires a fresh candidate
  submission or parent/operator retry of an explicitly accepted current artifact set. The
  reviewer-facing return-contract line says to stop the current verdict attempt and wait for a new
  `candidate_submitted` pointer or explicit retry; it does not invite the reviewer to repair and
  re-sign an already-invalid candidate identity. Node-root `plan.md` is excluded from this manifest
  because it is living process bookkeeping, not candidate deliverable identity;
- enforce per-level gate artifacts. **Implemented:** the return-contract walker now requires review
  seats to produce their review-owned artifact under `reviews/<gate-id>/`
  (`gate-report.md`, `gate-composition-report.md`, `gate-area-composition-review.md`, or
  `gate-composition-review.md`) with an explicit `VERDICT: ACCEPT`, `VERDICT: BOUNCE`, or
  `VERDICT: ESCALATE`, and the watchdog compares the terminal signal against that artifact verdict;
- keep return-contract refusals sticky by signal identity. **Implemented:** once a DONE signal
  artifact has failed the return contract, that harness-observed signal identity remains refused
  even if the agent edits mutable artifacts underneath it. The defect row/inbox nudge remain
  edge-triggered once per signal identity; the repaired candidate is admitted only after the agent
  rewrites the signal file and produces a fresh artifact identity;
- enforce v1 dispatch artifacts. **Implemented for upper-gate decomposed
  review:** `review-plan.md` must include exactly one plain `Review Mode: FULL`
  or `Review Mode: SHORT` line and a non-empty `## Role Selection` section.
  `FULL` requires the four L3+/L4+ reports or five L2+ reports under
  `reviews/<gate-id>/`. The shared paths are
  `reviewers/fidelity-coverage/report.md`,
  `reviewers/composition-interface/report.md`, and
  `reviewers/risk-readiness/report.md`; L3+/L4+ additionally requires
  `reviewers/evidence-credibility/report.md`, while L2+ additionally requires
  `reviewers/user-simulation/report.md` and
  `reviewers/performance-robustness/report.md`. Each check report must include a plain
  `Recommended Routing: ...` line. `SHORT` requires an explicit
  `Short Review Exception: YES` in `review-plan.md`; missing reviewer substrate
  is not a SHORT reason;
- let upper-gate review leads wait through harness wakes. **Implemented:** after writing a FULL
  `review-plan.md`, individual review-check terminal rows append silently. An L4+/L3+/L2+ lead
  waits for all selected reports and the address-owned `review_check` cohort barrier before
  synthesis. Report files alone are not terminal evidence. If a selected check remains open, the
  lead ends the turn in a ledger-derived wait rather than polling;
- keep the review-seat write surface explicit. **Implemented for review-seat kickoff:** higher
  review seats still share the node workspace with the producer, but their startup nudge and
  candidate pointer name `.inbox.review.jsonl`, `reviews/<gate-id>/review-packet.md`,
  `reviews/<gate-id>/review-plan.md`, the level-specific nested check-report paths, and
  the review-owned gate artifact path under that review directory. The
  producer's node-root artifacts are evidence inputs, not the review plan or
  review deliverable;
- route review-lead death to the held producer gate. **Implemented:** if reconcile detects a
  `coordinator_died` event for a gate `#review` lead while its producer is still
  `candidate_submitted`, the daemon also marks the producer `gate_failed` with
  `failure_class=review_lead_died` and wakes the parent. Generic coordinator deaths keep their
  existing reconcile-escalation visibility path;
- freeze child input packages across the gate boundary. **Implemented for spawned producer seats:**
  `brief.md` and `acceptance.md` are content-stamped onto the child binding when the child is
  briefed. Before a gated producer can submit a candidate, the harness compares the current stamps
  to those frozen inputs; drift fails the gate closed with `failure_class=frozen_input_drift` and
  wakes the parent instead of opening review on a silently mutated package;
- bind approved post-design acceptance refreshes by content. **Implemented for the V1
  test-author path:** when L3 approves a passed refreshed-test package, the L4 binding records
  the approved `tests/` package stamp. When L4 names that accepted package for the implementation
  child, the harness materializes a missing or empty implementation `tests/` tree from the approved
  package before the content check. The implementation target named by `test_refresh_for` may open
  only when its `tests/` package matches that approved stamp; an existing mismatched package still
  fails closed. The implementation child's own `acceptance.md` remains implementation-facing and
  does not have to byte-match the test-author rubric;
- route PASS upward, BOUNCE downward, ESCALATE upward;
- explicit review `ESCALATE` now routes upward to the parent as `gate_escalated`, separate from
  bounce-cap exhaustion;
- parent resolution of a gate escalation is explicit. **Implemented:** `harnessctl gate-accept
  <producer>` routes through daemon IPC to finalize a running `gate_escalated` producer as
  `gate_passed`, close the review seat, and unblock same-address next-phase work. While the
  producer remains `gate_escalated`, same-address outbox spawn requests are rejected with a visible
  reason instead of being consumed as idempotent already-live spawns. **Implemented for return:**
  `harnessctl gate-return <producer>` records the parent's ruling as one canonical parent-owned
  message to the producer and transitions `gate_escalated -> gate_bounced`. The old candidate stays
  unroutable; a fresh producer signal invokes the existing terminal-review-slot reopen path and
  creates a fresh review token, gate id, manifest, and panel. Neither bounce count nor bounce cap is
  reset, so a returned candidate that exhausts the cap again climbs back to parent judgment;
- parent-judgment convergence is bounded on the logical producer gate lineage. Every explicit or
  bounce-cap escalation increments `gate_escalation_count`, which survives gate-return and fresh
  review incarnations. At five, one typed WAL/L1-inbox notice states that parent judgment is not
  converging and leaves disposition to L1. At ten, the affected producer subtree receives the
  existing `paused_at` flag (in-flight turns still finish), a typed WAL row lands, and the direct
  L1 child carrying that subtree asks one canonical `needs_answer` question tagged
  `gate-convergence` to L1's owner surface. L1 itself remains live. Counts below five and counts six
  through nine have no threshold effect; if returned work re-caps, it re-escalates rather than
  spinning in the review panel;
- review terminal signals now name the current `gate_id` when the producer has a harness-created
  review packet, so a stale review verdict cannot route a newer candidate at the same producer
  address;
- planned `#review` bindings own no live process. Candidate submission and planned-spawn redrive
  clear any same-address stale review pane before a candidate pointer can be handled, and
  BOUNCE resubmission replaces the completed review binding with a fresh planned incarnation. Because
  a planned review binding is registered future work rather than an opened actor, the watchdog
  terminal/liveness body skips it until redrive opens the reviewer and records a transcript path;
- gate notification pointers are recoverable from committed binding state: if the best-effort inbox
  append for `candidate_submitted`, `gate_bounced`, `gate_failed`, `gate_escalated`, or
  `gate_passed` is lost, the daemon replay sweep reconstructs the missing line idempotently. Fresh
  `gate_failed` attempts after a retry carry `gate_failure_count`, so a repeated real failure is a
  new parent-visible pointer rather than a duplicate-suppressed old failure;
- post-PASS child repair is a same-address fresh incarnation. If a parent composition review finds a
  child-owned defect after that child has durable `gate_state=gate_passed`, the parent records the
  finding and submits a new outbox spawn request for the same child name with a delta repair brief.
  The prior candidate remains accepted history; the new incarnation gets a new owner token and a
  fresh gate cycle before its repaired output can move upward. Same-address fresh incarnations also
  get a fresh active work surface: prior `plan.md`, producer report artifacts, and seat transport
  files move under `<runtime>/.harnessd/incarnation-archives/` with an `incarnation-archive.json`
  provenance map, outside the agent workspace, while `reviews/<gate-id>/` directories remain in
  place as durable gate provenance;
- non-gate terminal relays are recoverable too: if the best-effort parent inbox append for
  `child_escalated` or `child_collapsed` is lost, the daemon replay sweep reconstructs the missing
  line idempotently from durable `terminal_signal` binding state. Review-check child completion
  recovery is fenced to the current running review lead and current producer `gate_id`, so old
  review-check completions cannot rehydrate into a future same-address review incarnation;
- prevent producer-only completion from crossing the boundary. **Implemented for production-spawned
  L5 producers:** a spawned L5 `DONE` submits a candidate to review and does not wake the parent;
  higher-level production-spawned producers carry the same derived gate fields.

Done when every L4/L3/L2 boundary requires gate PASS.

### Increment 5: Merge/Promotion Integration

- Gate PASS becomes the merge prerequisite in git protocol.
  **Built as the sanctioned merge runner guard:** `harnessd.merge_gate.merge_branch`
  performs the real source->parent git merge only when the source binding is
  `state=done`, `gate_state=gate_passed`, and carries a `gate_id` proof pointer.
  `harnessd.merge_gate.auto_merge_after_gate_pass` invokes that same guard automatically after both
  direct review PASS and parent acceptance of an escalated gate. It derives the Git top level from
  the producer workspace and authorizes only repositories inside the runtime `nodes/` tree; a
  non-worktree is not applicable, while an outside/enclosing repository is a loud anomaly and is
  never touched. `harnessctl merge` still routes through daemon IPC to the same guard, but is now
  repair-only. Logical branch paths remain the node/address proof chain; the runner encodes them as
  `__self__` Git refs only at the Git command boundary so parent and child branches can coexist.
  Replay treats an already-merged source as no-op success. The binding `parent_address` must match the
  source path's structural parent, and merge conflicts return a typed refusal after a best-effort
  `git merge --abort`, leaving conflict resolution with the parent level that owns the target.
  Merge requests carry `requested_by`; normal parent-owned movement records the parent node address,
  while manual repair/commissioning records `operator`. Non-operator requesters must map to the
  target's parent branch, so the evidence label cannot silently name an unrelated requester.
  Preventing an agent from invoking raw
  `git merge` in its shell is a future hardening layer; the v1 sanctioned path is now gate-owned.
- Parent-forwarded work carries the gate verdict pointer.
  **Built as full PASS proof-chain routing:** the parent `gate_passed` inbox pointer carries the
  candidate, review seat, `gate_id`, producer artifacts, gate artifact, review packet, candidate
  artifact manifest, manifest hash, producer signal identity, and the automatic merge outcome.
  `merged` confirms branch movement, `not_applicable` says no Git merge occurred, and `failed`
  carries the repair command and explicitly forbids composition under an assumed merge. The parent
  can consume the accepted work without inferring what the gate judged or what Git did from loose
  node files.
- Promotion requires the top fidelity gate, not just a produced artifact. **Built for promotion:**
  `harnessd.promote` refuses L1 accept unless the preliminary fidelity artifact is complete and
  its current content-addressed playback question carries an immutable owner CONFIRM (or distinctly
  labelled, predeclared commissioning-delegate CONFIRM), then uses the normalized §8 destination
  from the frozen spec when present.
  If delivery fails, the failure records the failed target; a later control-plane retry may provide
  an explicit normalized destination override only while the binding is `delivery-failed`.

Done when code/product movement follows the gate spine.

### Increment 6: Passive Observability

Build read-only views from WAL, inboxes, signals, node artifacts, reports, and transcripts:

- live node tree with exec/review seats;
- gate state timeline;
- submit/pass/bounce/escalate trail;
- task and requirement coverage;
- decision index;
- gate verdict index;
- citation/bounce ledger;
- parent wake trail.

The observer must not add agent-visible duties unless those duties belong in the production process.
When an agent appears not to follow instructions, diagnose from the transcript and adjacent
artifacts before changing prompts: record the exact prompt/input surface, transcript-visible
rationale or action path, files read/written, validator feedback, state transitions, and resulting
artifact. This is the visible reasoning trace available to the harness; hidden chain-of-thought is
not an observable surface and must not be treated as required evidence.

### Increment 7: Repeatable Behavioural Runs

Add scenario infrastructure:

- run manifest;
- initial intake;
- model/runtime configuration;
- expected behavioural joints;
- passive capture bundle;
- post-run review packet;
- stable run IDs;
- replay/index scripts.

Start with small joints, then run the full trace-through.

### Increment 8: Behavioural Tuning Loop

Tune only after the gate spine is real enough to preserve the intended incentives.

Primary behaviours to tune:

- altitude discipline;
- executor submission versus gate forwarding;
- escalate-not-decide on ambiguity;
- review strictness and specificity;
- bounce quality;
- rubric completeness;
- parent trust in gate-cleared work without redoing lower-level review;
- L1 client-facing preliminary synthesis and the owner-final playback handoff.

Classify every observed problem before changing anything:

- substrate bug;
- spec gap;
- role/prompt issue;
- rubric issue;
- model/runtime issue;
- expected stochastic miss;
- observer effect.

Change one production surface at a time, then rerun a comparable scenario.

## Readiness Criteria

Behavioural iteration can become the main loop when:

- no executor can forward work without gate PASS;
- L4, L3, and L2 boundaries have explicit review gates;
- every gate knows the frozen standard it reviews against;
- bounces and escalations are deterministic and replayable;
- gate artifacts are enforced;
- runs are repeatable enough to compare;
- observability is passive and visual;
- task, decision, requirement, and gate histories are queryable;
- a full trace-through can be diagnosed without manually spelunking raw files.

## Open Design Decisions

1. Signal vocabulary: add `SUBMITTED/PASS/BOUNCE`, or bridge with seat-sensitive `DONE`.
2. Specialist reviewer expansion beyond the fixed level-specific review-check rosters and how
   project/risk profiles add them during behavioural tuning.
3. Bounce cap default by level. The bridge implementation defaults to `3` and allows a
   binding-specific `gate_bounce_cap`; the per-level policy can still tighten or vary this.
4. What deterministic evidence is required before a gate may spend judgment.
5. Runtime/model assignment for review-check seats and whether a mixed topology
   improves judgment independence.
6. **Resolved 2026-07-24:** L1 authors the preliminary per-outcome/per-MNF judgment; the owner
   renders final accept through the durable playback question. A commissioning delegate is valid
   only when explicitly bound at launch and remains distinctly labelled on every surface.

## Next Step

Launch a fresh current-HEAD behavioural run. The remaining work should be driven by observed gate
behaviour rather than further speculative gate mechanics.
