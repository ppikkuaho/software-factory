# Higher-Level Gate Contracts

*Created: 2026-06-15. Status: active design contract for L4/L3/L2 review gates.
This document specializes the gate-owned-forwarding spine in `GATE-LIFECYCLE.md`
for the portfolio/composition gates above L5+.*

## Current packet and arbitration contract — 2026-07-24

Every higher-gate packet names the current frozen-contract versions and receipts as well as the
candidate snapshot identity. A reviewer checks both: the receipt pins the governing contract; the
snapshot pins the submitted object. Ordinary messages cannot change the rubric or contract. After
an owner-home immutable revision, an amendment message requires the review holder to re-read and
explicitly rebind; stale receipt enforcement is visible but non-blocking pending owner ruling.

When the gate cannot judge an issue at its altitude, it writes an arbitration-tagged open question
to the direct parent and parks on that ledger-derived wait. The answer is a canonical message.
Individual review-check completions stay quiet; one `review_check` cohort barrier wakes the
orchestrator after the selected reviewer set is terminal.

## Purpose

L5/L5+ is the local code-quality loop. L5 runs deterministic checks before
submission; L5+ independently reviews what automation cannot settle.

L4, L3, and L2 gates are different. They are portfolio-level composition gates:
they decide whether a bundle of already-gated lower work can safely move upward.
They do not casually redo lower review. They consume lower verdicts by pointer
and review the composition produced at their own altitude.

The load-bearing rule is unchanged:

- the producing `#exec` seat submits a candidate;
- the `#review` gate owns PASS, BOUNCE, or ESCALATE;
- only PASS crosses the parent boundary.

The gate artifact records the review judgment, evidence, and rationale. The parent-visible
authority is the harness-routed gate state: `gate_passed`, `gate_bounced`, `gate_failed`, or
`gate_escalated` in the parent inbox or the child binding. A parent may use the artifact to
understand a routed outcome, but it waits for the harness route before treating a child output as
available for dependent work. A visible ACCEPT in a review artifact is not enough if the harness
subsequently fails the gate on artifact drift, malformed output, stale identity, or review-substrate
failure.

## Shared Gate Model

Each higher-level gate has one `#review` seat that acts as the gate orchestrator
and verdict owner. The producing `#exec` seat cannot mark the work ready for the
parent. It can only submit a candidate. The gate orchestrator decides whether
the candidate can move upward.

The orchestrator writes `reviews/<gate-id>/review-plan.md`, dispatches selected
check reviewers with bounded briefs, waits for their reports, reads their
findings, and then renders the final gate judgment. Check reviewers produce
findings only. They do not vote, repair, negotiate, author the final gate
artifact, or sign the terminal verdict.

Check reviewers are instantiated per gate candidate. A later candidate may read
prior findings as artifacts, but it does not reuse the same reviewer context.

The agent-facing operating contract for this topology is
`operational/shared/review-handbook.md`.

The runtime assignment for higher review seats is intentionally undecided.
L4+, L3+, and L2+ may run on Codex, Opus, or a mixed assignment. Prompts and
artifacts should describe the review task in runtime-neutral language so the
model/runtime choice can be made later from behavioural evidence.

FULL mode for L4+ and L3+ uses four first-class review-check seats (auxiliary sub-seats of the owning Ln+ gate — no notation of their own; ruled 2026-07-12):

- **Fidelity and coverage:** checks the candidate against the frozen parent
  brief/spec, requirement IDs, ADRs, rubrics, and delegated constraints.
- **Composition and interface integrity:** checks whether lower outputs form the
  whole this level was assigned to produce and whether internal, exposed, and
  cross-boundary contracts fit.
- **Evidence credibility:** checks that lower verdicts, deterministic evidence,
  expected direct lower execution evidence, and submitted claims are credible
  enough to rely on by pointer.
- **Risk, substrate, and handoff/readiness:** checks material risks,
  deviations, ownership/substrate concerns, and whether the parent/requester can
  consume the package without reconstructing the gate.

The four required L3+/L4+ report paths are:

- `reviewers/fidelity-coverage/report.md`
- `reviewers/composition-interface/report.md`
- `reviewers/evidence-credibility/report.md`
- `reviewers/risk-readiness/report.md`

That four-seat roster remains the default. A commissioning operator may use
repeated `harnessctl start --review-panel-arm
'<L3-module-address-glob>=<ordered-axis-list>'` declarations for the ruled
panel-width experiment. The first case-sensitive match against the canonical
L3 producer address wins. A selected roster may contain the four axes above
and/or `broad`; the broad seat writes `reviewers/broad/report.md` and reviews
without an axis lane for anything that would make acceptance wrong. This
parameter applies only to L3 module candidates. It does not alter L4+
workstream panels or the separately calibrated L2+ product/probe roster.

At L2+ product altitude, FULL mode instead uses five first-class seats:
fidelity-coverage, composition-interface, risk-readiness, user-simulation, and
performance-robustness. Evidence-credibility retires as a separate L2+ seat.
The product-probe seats each receive the same content-addressed journey/MNF
roster and candidate identity, but a separate manifest-verified writable
disposable candidate copy. Their required paths are:

- `reviewers/fidelity-coverage/report.md`
- `reviewers/composition-interface/report.md`
- `reviewers/risk-readiness/report.md`
- `reviewers/user-simulation/report.md`
- `reviewers/performance-robustness/report.md`

The product probes may block only against the exact anchor rule in their
charters. A missing artifact-declared invocation is the typed blockable
artifact-face finding `FACE-NO-INVOCATION`; it never licenses an improvised
command or silent skip. Without a specified quantitative threshold,
performance measurements are non-blocking inventory unless the same observation
independently violates an MNF failure path or explicit artifact-face promise.

The older six/seven report names remain as subchecks under the level-specific axes, not as
required report files. Coverage and architecture coverage map to fidelity and
coverage; task/internal/cross-area interface, exposed contract, integration,
product integration, end-to-end flows, and boundary quality map to composition
and interface integrity; lower/evidence quality maps to evidence credibility at
L3+/L4+ and to the owning axis plus lead synthesis at L2+;
risk/deviation, shared ownership/substrate, parent/requester handoff, and
consumability map to risk, substrate, and handoff/readiness.

Situational roles include security/privacy, data/state, performance/scale,
operations/observability, migration/compatibility, UX/workflow, domain/policy,
and deep-module/API-design reviewers.

The orchestrator uses FULL mode by default. SHORT mode is the unusual
small-candidate exception for a genuinely simple gate where one right-sized
judgment task can cover the material questions without diluting the review.
Missing reviewer substrate is not a SHORT reason; it is a review-substrate
failure or escalation condition.

The gate artifact records the review mode, reports read, material findings, and
why the verdict follows. In FULL mode the lead does not author the required
level-specific check reports; it synthesizes them and signs the verdict.

## Review Packet Contract

The packet is pointer-not-payload. It tells reviewers what to read, not what to
believe.

Every higher-level review packet has two generated spec slices:

- **Trace slice:** the inherited requirement IDs, parent requirement text,
  traced design/test/rubric elements, lower outputs, lower verdicts, and trace
  or coverage map for the candidate subtree.
- **Governing context slice:** the ADRs, decision records, substrate decisions,
  interface contracts, conventions, and boundary constraints that apply to the
  candidate by path scope or boundary intersection, even when they do not map
  neatly to one requirement ID.

ADRs and interface contracts are part of the spec. Requirement IDs are the
coverage spine, not the whole standard. A broad ADR should not be forced under a
single requirement ID merely so a gate can find it. The review packet includes
governing ADRs and contracts through three routes:

1. **Trace reachability:** decisions and contract clauses whose trace stanzas
   overlap the candidate's inherited IDs.
2. **Path scope:** decisions and contracts authored at the candidate node or an
   ancestor/substrate node whose governance metadata covers the candidate path.
3. **Boundary intersection:** interface contracts the candidate exposes,
   consumes, changes, or relies on across a parent/sibling boundary.

Every higher-level review packet also carries:

- parent brief/spec and inherited requirement IDs;
- frozen rubric or acceptance criteria for this boundary;
- trace-slice pointers;
- governing-context pointers;
- producer `report.md`;
- lower gate verdict pointers;
- expected direct lower execution child evidence for ACCEPT (`L2 -> L3`, `L3 -> L4`, `L4 -> L5`);
- verification-runtime pointers when the harness can discover them, with commands taken only from
  explicit `Verification Commands` sections and local runtime probes framed as bounded review hints;
- deterministic evidence pointers, where applicable;
- open escalations, deviations, and unresolved concerns;
- candidate artifact manifest and immutable candidate snapshot directory;
- candidate artifact, branch, or workspace pointer;
- known risks and prior bounce history.

The harness should reject or fail the gate before model judgment is spent when
mechanical prerequisites are missing:

- producer report missing or unfilled;
- required gate artifact from a lower boundary missing;
- an ACCEPT lacks the expected direct lower execution child with `gate_state=gate_passed`
  (planning-only and test-author children are supporting evidence, not implementation evidence);
- lower child still unresolved, bounced, failed, or escalated without a parent
  answer;
- inherited requirement IDs missing from the trace/coverage map;
- a governing ADR/decision or interface contract that applies by path scope or
  boundary intersection cannot be resolved into the packet;
- deterministic evidence required by the rubric missing or red;
- review packet points at non-existent artifacts;
- current producer artifacts no longer match the submitted candidate manifest
  before verdict routing;
- candidate snapshot artifacts named by the manifest are missing or no longer
  match the submitted hashes.

## Common Artifact Schema

Each higher-level gate artifact is level-specific, but the shape is common:

1. **Header:** gate address, producer address, reviewer address, timestamp,
   candidate pointer, verdict.
2. **Inputs Reviewed:** exact artifact pointers read, including lower verdicts.
3. **Scope Boundary:** what this gate reviewed and what it intentionally did
   not re-review.
4. **Coverage:** requirement IDs and child outputs accounted for.
5. **Findings:** typed findings with severity, confidence, evidence pointer,
   and violated requirement/contract when applicable.
6. **Composition Judgment:** whether the pieces form the assigned whole.
7. **Interface Judgment:** whether contracts hold across boundaries.
8. **Risk Judgment:** risks accepted, bounced, or escalated.
9. **Verdict Rationale:** why PASS, BOUNCE, or ESCALATE follows from the
   findings.
10. **Bounce Packet, if BOUNCE:** typed repair requests addressed to the
    producing `#exec` seat, not to lower children directly.
11. **Escalation Packet, if ESCALATE:** the parent-altitude question and the
    options/evidence needed to answer it.

## Verdict Semantics

### PASS

PASS means the work is safe to consume at the parent altitude. The gate has
checked its altitude, verified required lower evidence exists, and found no
material unresolved defects.

PASS does not mean the parent abdicates judgment. The parent may still evaluate
approach, priority, and strategic fit at its own altitude.

The parent consumes PASS through the harness `gate_passed` pointer or binding
state. The gate artifact explains the PASS; it does not itself move the work
upward before the harness commits the route.

### BOUNCE

BOUNCE means the candidate can be repaired by the producing seat within its
authority. A bounce always goes to the producing `#exec` seat for this boundary.
The producer may then repair directly or route work to lower children, but the
gate does not bypass the producer's ownership.

Bounce defects must be typed and actionable:

- missing required child output or verdict;
- coverage gap against inherited IDs;
- internal contradiction between lower outputs;
- interface mismatch inside the produced whole;
- non-credible lower evidence;
- report too vague for parent consumption;
- composition does not satisfy the parent brief;
- risk visible at this altitude and repairable by the producer.

### ESCALATE

ESCALATE means the gate cannot decide without a parent-altitude decision.
Escalation is not a stricter bounce. It is used when the issue exceeds the
producer's authority or the gate's rubric:

- parent spec ambiguity;
- conflict between parent-level requirements;
- decomposition appears wrong rather than merely incomplete;
- acceptable tradeoff requires parent/client values;
- repeated bounces hit the cap;
- lower evidence raises a systemic process concern the producer cannot repair
  locally.

## L4 Workstream Gate

Producing seat: `L4#exec`.

Review seat: `L4#review`.

Gate artifact: `composition-report.md`.

Review altitude: workstream composition. The workstream gate reviews the parts
L5 and L5+ cannot see: whether accepted L5 units compose into the workstream L3
assigned, whether their interfaces fit, and whether the lower verdicts are
credible enough for L3 to rely on.

Primary question:

> Does this workstream fulfill its L3 brief, with all L5 task outputs integrated
> through clean interfaces and enough evidence for L3 to consume it?

### L4 Inputs

- L3 workstream brief and inherited IDs;
- workstream rubric;
- L4 `plan.md` and `report.md`;
- each L5 task brief/acceptance pointer;
- each L5+ verdict/report;
- task interface contracts and cross-task assumptions;
- deterministic evidence summaries from L5/L5+;
- integration evidence for the workstream;
- open L5 escalations or bounces.

### L4 V1 Review Axes

- **Fidelity and coverage:** every workstream obligation is assigned to a lower
  task, accepted deferral, or escalation. Legacy subcheck: task coverage.
- **Composition and interface integrity:** task outputs connect through declared
  workstream interfaces and form one operational slice, not a pile of accepted
  tasks. Legacy subchecks: task-interface integrity, workstream integration,
  and deep-module/boundary quality at workstream scale.
- **Evidence credibility:** L5+ verdicts, expected direct implementation child
  evidence, and deterministic evidence are present and specific enough to rely
  on by pointer. Legacy subcheck: lower-verdict credibility.
- **Risk, substrate, and handoff/readiness:** residual risks, deviations, and
  handoff gaps are visible, and L3 can understand what exists, what it
  discharges, and what remains risky by reading the report and pointers. Legacy
  subcheck: parent consumability.

### L4 Must Not Re-Review

- raw L5 code line-by-line;
- acceptance tests already gated by L5+;
- L5 implementation style unless it creates a visible composition defect or
  proves the L5+ verdict non-credible.

The L4 gate spends its judgment only where the bigger picture matters: task
coverage, task-to-task interfaces, integration into the workstream, lower-verdict
credibility, and parent consumability. If an issue is fully visible to L5 or
L5+, it belongs there unless the missed issue makes the lower verdict itself
non-credible.

### L4 PASS Criteria

- every inherited requirement ID is accounted for;
- every lower task is passed, intentionally deferred, or escalated with a
  resolved answer;
- task outputs integrate through declared interfaces;
- no cross-task contradiction remains;
- L5+ verdicts and deterministic evidence are present and credible;
- `composition-report.md` is specific enough for L3 to consume by reference.

### L4 BOUNCE Criteria

- missing L5+ verdict or unclear lower evidence;
- workstream output incomplete against the brief;
- task outputs conflict or do not connect;
- workstream interface not honored;
- task decomposition left a gap or duplicated ownership;
- report hides material concerns from L3.

### L4 ESCALATE Criteria

- L3 brief is ambiguous or contradictory;
- task decomposition reveals the workstream was mis-sized or mis-scoped;
- interface expectation between sibling workstreams cannot be settled locally;
- bounce cap is exhausted.

## L3 Area Gate

Producing seat: `L3#exec`.

Review seat: `L3#review`.

Gate artifact: `area-composition-review.md`.

Review altitude: area/module composition. The area gate reviews whether accepted
L4 workstreams compose into the coherent area/module L2 asked for, and whether
the area exposes the interface L2 expected.

Primary question:

> Does this area realize its frozen design as one coherent module, with internal
> workstreams integrated and external contracts fit for L2 composition?

### L3 Inputs

- L2 area/module spec and inherited IDs;
- frozen `design.md`;
- L3 `plan.md` and `report.md`;
- L4 workstream reports and `composition-report.md` verdicts;
- internal interface contracts;
- exposed interface contracts promised to L2;
- cross-workstream integration evidence;
- area requirement coverage map;
- unresolved workstream deviations and risks.

### L3 V1 Review Axes

- **Fidelity and coverage:** area output matches the frozen L2/L3 design,
  inherited IDs, and delegated constraints. Legacy subcheck: design fidelity and
  area coverage.
- **Composition and interface integrity:** workstreams form one module, internal
  contracts line up across data/lifecycle/error/ownership boundaries, and the
  module exposes the ports, invariants, and dependency direction L2 expected.
  Legacy subchecks: cross-workstream composition, internal interfaces, exposed
  interfaces, and deep-module/boundary quality at area scale.
- **Evidence credibility:** L4+ verdicts, expected direct workstream child
  evidence, and integration evidence are present and credible enough to rely on
  by pointer. Legacy subcheck: evidence quality.
- **Risk, substrate, and handoff/readiness:** material risks, deviations,
  ownership/substrate concerns, and requester handoff are visible and either
  accepted, bounced, or escalated. Legacy subcheck: risk at module scale.

### L3 Must Not Re-Review

- L5 code quality;
- individual L5 acceptance suites;
- L4 workstream internals except where they fail to compose into the area.

### L3 PASS Criteria

- all workstreams are gate-cleared or resolved;
- internal interfaces are coherent;
- exposed interfaces match L2 contracts;
- requirement coverage has no silent drops or unsanctioned additions;
- area-level risks are recorded with disposition;
- `area-composition-review.md` is usable by L2 as the area verdict.

### L3 BOUNCE Criteria

- workstreams do not compose into the area design;
- internal interface mismatch;
- exposed interface mismatch that L3 can repair within area authority;
- missing or non-credible L4 verdicts;
- area report fails to surface material deviations or risks.

### L3 ESCALATE Criteria

- L2 area spec or architecture contains a contradiction;
- correct fix changes an L2-owned contract;
- sibling-area conflict requires L2 arbitration;
- area boundary appears mis-carved;
- bounce cap is exhausted.

## L2 Product Composition Gate

Producing seat: `L2#exec`.

Review seat: `L2#review`.

Gate artifact: `composition-review.md`.

Review altitude: product/system composition. The product gate reviews whether
accepted L3 areas/modules compose into the architecture L2 promised L1, and
whether the system is ready for L1 intent-fidelity review.

Primary question:

> Do the areas compose into the product/system architecture, with cross-area
> interfaces, end-to-end flows, deviations, and risks explicit enough for L1 to
> judge intent fidelity?

### L2 Inputs

- frozen intent-spec and requirement map;
- L2 architecture, ADRs, component map, and conventions;
- area/module specs;
- L3 `area-composition-review.md` verdicts;
- cross-module interface contracts;
- system-level integration evidence;
- end-to-end flow evidence;
- deviation/risk ledger;
- delivery destination contract.

### L2 V1 Review Axes

- **Fidelity and coverage:** built areas match the frozen intent/spec, L2
  architecture, ADRs, and requirement map. Legacy subcheck: architecture
  coverage/fidelity.
- **Composition and interface integrity:** modules form one system, ports and
  contracts work across area boundaries, dependency arrows point toward
  stability, and primary user/system flows traverse the composed modules without
  unowned gaps. Legacy subchecks: cross-area composition, system interfaces,
  product integration, and end-to-end behavior.
- **Evidence credibility:** L3+ verdicts, expected direct area child evidence,
  system integration evidence, and flow evidence are present and credible enough
  to rely on by pointer. Legacy subcheck: evidence quality.
- **Risk, substrate, and handoff/readiness:** cross-cutting substrate decisions
  remain single-owned, deviations are named and dispositioned, and the package
  is framed for L1/user intent-fidelity review rather than as raw technical
  detail. Legacy subchecks: substrate/shared ownership, deviation discipline,
  and L1/requester consumability.

### L2 Must Not Re-Review

- task code;
- workstream internals;
- area internals except where they break product composition;
- L1 intent acceptance, which belongs to L1/user final review.

### L2 PASS Criteria

- every area/module is gate-cleared or explicitly out of scope by approved
  decision;
- cross-area contracts are coherent and exercised by evidence;
- end-to-end flows cover the frozen intent requirements the architecture claims
  to serve;
- deviations and risks are explicit;
- delivery destination contract is present;
- `composition-review.md` gives L1 a clean product-level packet.

### L2 BOUNCE Criteria

- area outputs do not compose into the architecture;
- cross-area contract mismatch repairable by L2;
- missing L3 gate verdicts or non-credible area evidence;
- system-level flow gap;
- duplicated ownership of substrate/shared state;
- product report obscures deviations or risks.

### L2 ESCALATE Criteria

- intent/spec conflict requires L1 or user arbitration;
- architecture must change in a way that alters the approved concept;
- delivery destination or acceptance authority is ambiguous;
- user-value tradeoff is required;
- bounce cap is exhausted.

## Gate-Agent Operating Rules

Review agents are not producers. They do not repair, rewrite, or negotiate
mid-review. Their job is to read the frozen standard, read the candidate and
evidence, and act according to seat authority. The gate lead is the orchestrator
and verdict owner. Review-check seats produce findings only.

The orchestrator prompt should steer posture as:

> Evaluate this candidate against the stated gate criteria. Focus on material
> defects, unsupported evidence, composition failures, interface mismatches, and
> tradeoffs that would change the verdict. Treat acceptable choices as
> acceptable. Write the review plan, dispatch the required level-specific review-check seats in
> FULL mode, wait for their reports, read findings as evidence rather than
> votes, and render the verdict that follows from the criteria. Do not search
> for objections for their own sake.

Check reviewers receive a narrower version of the same contract:

- input packet plus check-specific criteria;
- no authority to change the work;
- output findings only;
- each finding carries evidence, severity, confidence, and routing
  recommendation: PASS-note, BOUNCE, or ESCALATE;
- one assigned report path under `reviewers/<axis>/report.md`.

The orchestrator may spawn an additional reviewer when findings conflict or a
review check comes back under-supported. It must record that choice in the gate
artifact rather than silently smoothing uncertainty into a confident verdict.

For v1, reviewer mechanics are first-class auxiliary review-check seats bound to
artifacts under `reviews/<gate-id>/`. The harness creates `review-packet.md`;
the gate lead creates `review-plan.md`; check reviewers create one report each
at the required level-specific paths. Runtime-native subagents may be useful for experiments,
but production gate evidence requires review-check reports tied to the current
gate identity. The final gate artifact reads check reports as evidence and owns
the verdict.

## Implementation Implications

Build order:

1. Teach the harness to create per-level review packets from existing node
   artifacts. **Implemented for the binding-level spine:** candidate submission writes
   `reviews/<gate-id>/review-packet.md` and records the packet pointer on the producer binding.
2. Enforce packet mechanical prerequisites before spawning reviewers.
3. Add artifact-contract parsers for `composition-report.md`,
   `area-composition-review.md`, and `composition-review.md`. **Implemented:** higher review
   seats cannot sign `DONE` without their level-specific gate artifact, and the terminal-signal
   verdict cannot override that artifact's verdict. The named gate artifact is the higher review
   deliverable; no additional review `report.md` is required for L4+/L3+/L2+ seats.
4. Generalize the L5/L5+ gate lifecycle to L4, then L3, then L2. **Partially implemented:**
   production child registration now stamps L4/L3/L2 producers with gate fields and creates their
   paired planned `#review` bindings; redrive opens review seats only after candidate submission,
   and failed review-seat opening marks the producer `gate_failed`.
5. Add tests that producer `DONE` never wakes the parent at those boundaries. **Partially
   implemented:** binding-level tests cover L4/L3/L2 candidate submission and a production-spawned
   L5 regression covers the real spawn path; higher-level production-spawned `DONE` regressions can
   be added as the opener/failure semantics settle.
6. Add tests for PASS upward, BOUNCE to producer, ESCALATE to parent, missing
   lower verdict refusal, and bounce-cap escalation at each level.
7. Complete the prompt/writing-level review-handbook design before coding the dispatch loop:
   role-selection policy, example role sets, review-check brief templates, reviewer report schema,
   orchestrator synthesis prompt, gate-artifact writing instructions, and calibration examples for
   PASS/BOUNCE/ESCALATE. **Implemented for v1:** role selection is recorded in
   `review-plan.md`; FULL mode requires the four L3+/L4+ or five L2+ nested check reports; SHORT mode requires an
   explicit short-review exception and is not allowed for missing reviewer substrate; review-seat
   kickoff names the review packet, review-plan path, check-report paths, and final gate artifact
   path; richer examples remain future behavioural tuning.
8. Implement the review-handbook dispatch loop: orchestrator writes the plan,
   dispatches the level-specific review-check briefs, waits for reviewer reports, and
   writes the synthesis into the gate artifact. **Implemented for v1:** the
   harness opens first-class auxiliary review-check seats with
   `review_check_for`, `review_check_axis`, and nested report paths; the lead
   owns the judgment and does not author check reports in FULL mode.
9. Add passive observability indexes for packet, verdict, findings, bounce, and
   escalation histories.
10. Add parent resolution for review-gate escalations. **Implemented:** `gate-accept` finalizes a
    parent-accepted `gate_escalated` producer as `gate_passed`; `gate-return` records the parent's
    ruling as a canonical message and moves the producer to `gate_bounced` so only a fresh candidate
    opens a fresh review incarnation. Return preserves the local bounce cap, so repeated failure
    climbs back to parent judgment. A sibling escalation counter warns L1 exactly at five; at ten it
    pauses the affected subtree and raises an owner-facing canonical question through L1.

Do not implement higher-level gates by copying L5+ code-review behavior upward.
Reuse lifecycle mechanics; specialize review behavior by altitude.

## Open Design Choices

- Specialist reviewer expansion beyond the fixed level-specific rosters and how project/risk
  profiles add them during behavioural tuning.
- Whether per-level gates share one artifact parser with level-specific required
  sections or use separate parsers.
- The default bounce cap per level.
- Which deterministic evidence is mandatory per project risk profile.
- Runtime/model assignment for L4+, L3+, and L2+ review seats, including
  whether a mixed Codex/Opus topology improves judgment independence.
- Further product-probe roster changes beyond the calibrated user-simulation and
  performance/robustness seats. Their exact version-2 instructions, notary records, and
  GPT-5.6-Sol/native-Codex rows are now production-active.
