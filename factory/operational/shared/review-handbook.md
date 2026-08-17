# Review Handbook

*Created: 2026-06-15. Status: agent-facing operating contract for review gates.*

## Purpose

Review gates exist to keep ownership outside the producer. A producing `#exec`
seat submits a candidate. The co-located `#review` gate lead decides whether
that candidate can move upward, must return for repair, or needs parent
arbitration.

For L5+, the gate is local code-quality review. For L4+, L3+, and L2+, the gate
lead is an orchestrator and verdict owner, not a monolithic reviewer. Higher
gates consume lower verdicts by pointer and review whether the produced bundle
composes at their altitude.

## Contract receipts, amendments, and arbitration

Before judgment, read the packet's candidate manifest/snapshot identity and each current
frozen-contract stamp/receipt. They prove different things: submitted bytes versus the governing
contract version. A normal message cannot change the rubric. If a `contract_amendment` message
arrives, re-read the owner-home revision and use the explicit rebind path before claiming the new
version. A stale receipt wakes you but is not currently a PASS blocker unless the owner later
ratifies that rule.

If a decision exceeds this gate's altitude, write an arbitration-tagged `needs_answer` message to
the direct parent and park. Do not invent the answer or encode it as a contract edit.

<!-- block:review-by-driving v1 -->
## Derive the Verdict by Driving the Frozen Claim

The producer's report is a map, not a verdict. Meet the submitted artifact through its actual face
on a **fresh disposable instance**, run the documented live scenario and the altitude-appropriate
probes yourself, and observe the recipient-visible behavior. Separate what you drove and watched
from what you accept only as cited lower evidence.

Derive the verdict against the frozen contract only. Do not add desirable-but-unasked behavior,
move the goalposts, or treat author self-certification and green automation as proof of the claim.
Fresh review ownership is intentional: the written, frozen contract supplies stable acceptance;
the warm-verifier clause from the source doctrine is not part of this system.
<!-- /block:review-by-driving -->

## Gate Orchestrator

The `#review` seat — the node's Ln+ function-owner — is the gate orchestrator and verdict owner. Its job is to:

1. read the review packet and frozen criteria;
2. verify mechanical prerequisites before spending judgment;
3. choose the reviewer roles needed for this packet;
4. write `reviews/<gate-id>/review-plan.md`;
5. request reviewer dispatch by recording FULL mode and check-specific coverage
   in `review-plan.md`;
6. wait for the required check reports and the `review_check` cohort barrier;
7. read their findings without treating any one reviewer as authoritative;
8. produce the gate artifact with `VERDICT: ACCEPT`, `VERDICT: BOUNCE`, or
   `VERDICT: ESCALATE`;
9. sign the terminal signal that routes the verdict.

The orchestrator does not repair the work. It may ask for a missing review,
spawn an additional reviewer, or escalate ambiguity, but it must not rewrite the
candidate to make it pass. In FULL mode for L4+, L3+, and L2+, the orchestrator
does not author the required level-specific check reports; those reports are produced by
separate review-check seats.

In the harness runtime, `Review Mode: FULL` in `review-plan.md` is the dispatch
request. After the plan is durable, the daemon opens the level-specific review-check roster
with their own briefs and launch packets. The gate lead may read report files as they appear, but
final synthesis starts from the completed reviewer set. Each selected check still requires its
report and terminal child row, but those individual rows append silently. The daemon wakes the
lead once when the address-owned `review_check` cohort barrier crosses from nonterminal members to
none. If any selected check remains open, the lead ends the turn in a ledger-derived wait and does
not poll. The lead does not use Claude Code Agent, Task, or other native sidechain
tools for these gate reviewers; those sidechains are not harness seats and are
not valid gate evidence.

## Check Reviewers

Check reviewers are independent judgment agents — auxiliary sub-seats of the owning Ln+ gate, with no notation of their own (ruled 2026-07-12). Each reviewer owns one review
check and produces findings only. They do not render the final gate verdict,
modify the candidate, negotiate with the producer, or invent new success criteria.

Check reviewers are instantiated per gate candidate. A later candidate gets new
reviewer contexts and new reports; prior findings are read as artifacts.

Every check reviewer receives:

- the review packet;
- the frozen parent brief/spec and inherited IDs;
- the trace slice: requirement IDs, traced elements, rubrics, lower outputs, and
  lower verdicts;
- the governing context slice: ADRs, interface contracts, substrate decisions,
  conventions, and boundary constraints that apply by scope;
- lower gate verdict pointers and deterministic evidence pointers;
- expected direct lower execution child evidence for ACCEPT at this altitude;
- the check brief defining its scope and non-scope;
- the required output contract.

Every finding must include:

- review check;
- severity: `blocking`, `material`, `minor`, or `note`;
- confidence: `high`, `medium`, or `low`;
- evidence pointer;
- criterion or contract implicated;
- recommended routing: `PASS-note`, `BOUNCE`, or `ESCALATE`;
- concise rationale.

Tentative findings are allowed. A low-confidence material concern is not a PASS
and not automatically a BOUNCE; it is evidence for the orchestrator to weigh,
re-check, or escalate.

## Role Inventory

Core roles:

- **Fidelity reviewer:** checks the candidate against the frozen parent brief,
  requirement IDs, ADRs, and delegated constraints.
- **Composition reviewer:** checks whether lower outputs form the whole this
  level was assigned to produce.
- **Interface reviewer:** checks ports, data contracts, lifecycle expectations,
  dependency direction, and cross-boundary assumptions.
- **Verification-evidence reviewer:** checks that lower verdicts, deterministic
  evidence, and required reports exist and are credible enough to rely on.
- **Risk reviewer:** checks material security, data, migration, performance,
  operational, UX, or domain risks at this altitude.
- **Reporting reviewer:** checks whether the candidate is packaged so the parent
  can consume it without spelunking or redoing the gate.

Situational roles:

- **Security/privacy reviewer:** material when credentials, permissions,
  personal data, tenant boundaries, payment flows, or adversarial inputs are in
  scope.
- **Data/state reviewer:** material when the work changes persistence,
  migrations, schemas, state machines, idempotency, or reconciliation.
- **Performance/scale reviewer:** material when latency, throughput, admission,
  concurrency, resource ceilings, or cost are material.
- **Operations/observability reviewer:** material when the work changes alerts,
  logs, dashboards, runbooks, lifecycle recovery, or debugging surfaces.
- **Migration/compatibility reviewer:** material when existing users, data,
  APIs, formats, or integrations must continue to work.
- **UX/workflow reviewer:** material when user-facing flows, operator workflows,
  or human-in-the-loop handoffs are material.
- **Domain/policy reviewer:** material when correctness depends on domain rules,
  policy, regulation, or business semantics outside ordinary engineering.
- **Deep-module/API-design reviewer:** material when the gate depends on
  boundary quality, abstraction depth, dependency direction, or API stability.

The inventory is not exhaustive. The orchestrator may define a project-specific
role when the packet needs a perspective not covered here, but the role must
have a clear review check, scope, grounding material, and output contract.

## Role Selection

The orchestrator chooses roles from the packet, not from habit. It should select
the smallest reviewer set that can independently cover the material risks and
composition questions.

L4+/L3+ FULL mode uses exactly four first-class review-check seats:

- **Fidelity and coverage:** whether the accepted bundle satisfies the frozen
  brief/spec, inherited IDs, rubrics, ADRs, and delegated constraints.
- **Composition and interface integrity:** whether lower outputs compose into
  the assigned whole and whether internal/exposed/cross-boundary contracts fit.
- **Evidence credibility:** whether lower verdicts, direct lower execution
  child evidence, deterministic evidence, and submitted claims are current,
  specific, and credible enough to rely on by pointer.
- **Risk, substrate, and handoff/readiness:** whether residual risks,
  deviations, ownership/substrate concerns, and parent/requester handoff are
  visible and dispositioned at this altitude.

L2+ FULL mode uses exactly five first-class review-check seats:

- **Fidelity and coverage** and **Composition and interface integrity** retain
  the definitions above at product altitude.
- **Risk, substrate, and handoff/readiness** also owns lower-evidence
  accounting and the exact L2+ security basics: secrets absent, no accidental
  network exposure, and input sanity at trust boundaries.
- **User simulation:** drives every confirmed positive, negative, and MNF
  journey through the artifact-declared recipient face on a dedicated
  disposable instance.
- **Performance and robustness:** drives specified thresholds and bounded
  concurrency, interruption/recovery, lifecycle, capacity, timing, and MNF
  probes on a separate disposable instance.

Evidence credibility retires as a separate axis at L2+. The current Q3
plan-alignment coverage report, intent-spec receipt, candidate manifest, and
probe roster provide the deterministic input spine; fidelity, composition,
risk, and the gate lead still account for evidence where it supports their
claims.

The older six/seven check names remain subchecks under the level-specific axes.
They are not additional required report files:

- coverage and architecture coverage map to fidelity and coverage;
- task/internal/cross-area interface, exposed contract, integration, product
  integration, end-to-end flows, and boundary quality map to composition and
  interface integrity;
- lower/evidence quality maps to evidence credibility at L3+/L4+ and to the
  owning L2+ axis plus gate-lead synthesis at product altitude;
- risk/deviation, shared ownership/substrate, parent/requester handoff, and
  consumability map to risk, substrate, and handoff/readiness.

Add situational roles when packet evidence or project context makes the
check material. For higher gates, additional specialists supplement the
level-specific roster; they do not replace the required reports in FULL mode. SHORT mode is
the unusual exception for a genuinely small candidate where one right-sized
review task can cover the material questions without diluting judgment. Missing
reviewer substrate is not a SHORT reason; it is a review-substrate failure or
escalation condition. The orchestrator records the exception and why coverage
remains sufficient in the gate artifact.

The richer specialist inventory is a source of future behavioural-tuning
experiments, not a command to spawn every plausible reviewer. L5+ stays focused
on the independent local review that automation cannot perform. L2+ product
probes are fixed roster members. Their exact version-2 instructions are
owner+director calibrated and notary-receipted; both run GPT-5.6 Sol through the
proven native Codex adapter. Production dispatch still fails closed on any
instruction-byte drift or absent model row.

Higher review prompts must be runtime-neutral. L4+, L3+, and L2+ review seats
may run on Codex, Opus, or a mixed runtime assignment; that choice is deferred
until the review behaviour and runtime tradeoffs are evaluated.

<!-- block:composition-proof v1 -->
## Drive Composition at This Gate's Altitude

Use accepted lower verdicts for unit facts; do not re-run lower-level line or unit review. Drive the
composed artifact itself on a fresh disposable instance and cover the failure modes that only exist
after assembly:

- seams exercised in vivo, not inferred from matching interface prose;
- cross-unit failure paths and recovery behavior;
- end-to-end journeys, explicitly including **negative and must-never-fail journeys**;
- emergent properties that no child could observe alone;
- real wiring, configuration, and runtime connectivity;
- drift between the assembled behavior and the frozen plan/contract.

Your gate artifact names what you drove, what you observed, and which lower verdicts you cited. A
paper compatibility read or an aggregate green suite cannot substitute for composition evidence.
<!-- /block:composition-proof -->

## Higher-Gate Altitude

Upper gates rely on lower gates as competent by default. They check whether
lower verdicts are present, current, credible, and appropriate for the upper
claim. Directly driving the composed artifact at the current altitude is the
required composition oracle, not an optional sanity probe and not a rerun of
lower review. They do not rerun lower suites as a generic confidence ritual.
Bounded lower-level probes are allowed only with a stated reason: missing,
contradictory, stale, or too-vague evidence, or a concrete credibility concern.

## Orchestrator Verdict

The orchestrator reads reviewer reports as evidence, not as votes. It must
identify the material findings, resolve duplicate or contradictory findings, and
explain why the final verdict follows.

`ACCEPT` means no blocking or material unresolved finding remains at this
altitude, required evidence exists, expected lower execution child gate-pass
evidence is present when this is an execution candidate, and the parent can
consume the package.

Before ACCEPT, the lead explicitly accounts for every `blocking` or `material`
finding and every check report whose recommended routing is not `accept-note`.
The synthesis either routes the finding to BOUNCE/ESCALATE or records a concrete
dismissal rationale with an evidence pointer. Four `accept-note` lines are not a
vote; material findings still decide the verdict.

`BOUNCE` means the producer can repair the issue within its authority. The
bounce packet names the defects, evidence, criteria, and expected repair scope.

`ESCALATE` means the gate cannot decide because the issue belongs to the parent
altitude or requires a human/client tradeoff. The escalation packet names the
question, options, evidence, and why bounce would be the wrong route.

## Required Artifacts

The orchestrator writes the gate artifact for its level:

- L5+: `reviews/<gate-id>/gate-report.md`;
- L4: `reviews/<gate-id>/gate-composition-report.md`;
- L3: `reviews/<gate-id>/gate-area-composition-review.md`;
- L2: `reviews/<gate-id>/gate-composition-review.md`.

Higher-level gate artifacts must include:

- review plan: selected roles and why;
- reviewer reports read;
- inputs reviewed;
- scope boundary;
- coverage and lower-verdict accounting;
- expected lower execution child evidence or recorded design-only exception;
- material findings by review check;
- composition judgment;
- interface judgment;
- risk judgment;
- verdict rationale;
- bounce packet or escalation packet when applicable.

The terminal signal carries the routing verdict, but the gate artifact is
authoritative.

## Dispatch Artifacts

For L5+, L4+, L3+, and L2+ gates, the review lead works inside
`reviews/<gate-id>/`. The harness creates `review-packet.md` there when the
producer submits a candidate. The packet is a pointer map, not evidence by
itself. It should expose both the trace slice and the governing context slice;
the producer's node-root artifacts are supporting candidate evidence, not the
review-owned gate artifact, and must not be overwritten.

L5+ is the local review gate for one L5 candidate. Its lead may write
`review-plan.md` as a working outline, but it does not use FULL/SHORT mode and
does not wait for auxiliary review-check seats. Its required artifact is
`gate-report.md`, written by the L5+ reviewer after its own independent local
review.

For L4+, L3+, and L2+, the review lead must write `review-plan.md` before
writing the final gate artifact. The plan records role selection and the review
mode. In the harness runtime, it is also the dispatch request for review-check
seats:

- include exactly one plain line, `Review Mode: FULL`, for the normal decomposed
  review;
- include exactly one plain line, `Review Mode: SHORT`, for the unusual
  short-review exception;
- include a non-empty `## Role Selection` section naming the selected checks
  and why that set is sufficient for the candidate.

In FULL mode, the exact level-specific reports must exist at nested paths
relative to the review directory and include exactly one plain
`Recommended Routing: accept-note`, `Recommended Routing: bounce`, or
`Recommended Routing: escalate` line near the top:

L3+/L4+:

- `reviewers/fidelity-coverage/report.md`
- `reviewers/composition-interface/report.md`
- `reviewers/evidence-credibility/report.md`
- `reviewers/risk-readiness/report.md`

L2+:

- `reviewers/fidelity-coverage/report.md`
- `reviewers/composition-interface/report.md`
- `reviewers/risk-readiness/report.md`
- `reviewers/user-simulation/report.md`
- `reviewers/performance-robustness/report.md`

The report template is
`operational/shared/templates/check-review-report-template.md`.

Each check is one narrow review task. The daemon opens the review-check seats
after the lead records FULL mode and the role-selection plan. The role-selection
section must name the exact level-specific slugs in FULL mode.
The check reviewer
receives:

- the review packet path;
- the parent brief/spec and relevant rubric pointers;
- the trace-slice and governing-context pointers relevant to the check;
- the exact check name and report path to write;
- the scope to judge and the neighboring checks to leave alone;
- the finding schema from the check-report template.

The check reviewer returns a report, not a verdict. In FULL mode, individual terminal rows append
silently and the lead waits for the reports plus the `review_check` cohort barrier before
synthesizing. If the runtime cannot open separate
reviewer contexts, record that substrate limitation in
`review-plan.md` and escalate or fail the review substrate rather than
pretending the decomposed review happened. Missing reviewer substrate is not a
reason to choose SHORT.

In SHORT mode, `review-plan.md` must contain `Short Review Exception: YES` and
each short-exception checklist row must contain an explicit YES, an evidence
pointer, and a rationale. SHORT mode is appropriate only when the candidate is
clearly simple enough for one right-sized review task to cover the material
questions.

The final gate artifact synthesizes the reports. It does not count votes.
Write it in the review directory with the level's `gate-*` basename, not in the
producer node root.

## Review-Check Launch Surfaces

<!-- surface:REVIEW-CHECK launch id=check-reviewer-role v1 -->
You are a review-check seat for one upper-gate candidate. You own only the
assigned check report. You do not render the final gate verdict, repair the
candidate, negotiate with the producer, or broaden your scope beyond the check
brief.
<!-- block:decision-delivery-signoff v5 -->
## Decision Home and Final Sign-Off

A generated document may project or summarize a decision, but it must never be
the decision's sole home. Record the decision in its owned canonical artifact
and deliver it through the recipient's normal surface; generation is not
delivery.

Once you submit a candidate, its artifacts are frozen until the gate verdict
returns. Do not edit reviewed bytes for any reason, including to fix a defect
you just found. Report the defect and let the verdict or a fresh submission
carry the repair.

Put disposable scratch only in the harness-provisioned `.tmp` tree. Leave it in
place and report if it blocks you; the harness never requires a seat to delete
anything, so never issue destructive filesystem commands.

When the same harness substrate command fails three consecutive times with the
same error class, stop retrying it. Three identical failures are the substrate
reporting that it is broken, not your invocation; a fourth attempt spends usage
against a control plane that cannot answer. Write a substrate-fault marker —
`substrate-fault.json` in your node, naming the command, the error class, the
three attempt timestamps, and what you were trying to do — and end the turn.
Ending the turn stops the meter, and the marker is what makes the fault legible
to whoever repairs the substrate; that repair is never your work.

During ordinary work, read new inbox rows when the harness notification names
their sender and unread count. The final sign-off read below independently
covers messages that arrived mid-turn.

Before signing `DONE` or `FAILED`, read your current inbox as the
**second-to-last act**. Resolve or route anything that arrived while you were
working. Writing the terminal signal is the last act.
<!-- /block:decision-delivery-signoff -->

<!-- /surface:REVIEW-CHECK launch id=check-reviewer-role -->

<!-- surface:REVIEW-CHECK launch id=check-reviewer-method v1 -->
Read the review packet, the check brief, and the grounding pointers for your
axis. Judge at the gate altitude. Treat lower gates as competent by default:
verify presence, currency, credibility, and fit of lower verdicts rather than
redoing lower review. Use a bounded probe only when you state the upper-altitude
question or credibility concern it answers.
<!-- /surface:REVIEW-CHECK launch id=check-reviewer-method -->

<!-- surface:REVIEW-CHECK launch id=check-report-contract v1 -->
Write exactly the assigned report path using
`operational/shared/templates/check-review-report-template.md`. Include the gate,
candidate, review packet, review check, exactly one `Recommended Routing:
accept-note`, `Recommended Routing: bounce`, or `Recommended Routing: escalate`
line, scope read, finding summary, findings table, required accounting for the
assigned check, and notes for the gate lead. Sign completion only after that
report exists.
<!-- block:review-accountability v1 -->
## Assigned-Item Return Law

Return every item assigned to this review: enumerate each one and mark it
**Answered** with the supporting finding/evidence or **Declined** with a
reason. Silent omission is not an answer. Your report is the gate lead's
ground-truth record of what this seat checked; make every later claim about
your work derivable from it.
<!-- /block:review-accountability -->

<!-- /surface:REVIEW-CHECK launch id=check-report-contract -->

<!-- surface:REVIEW-CHECK launch id=check-boundaries v1 -->
You provide findings, not a verdict. Do not edit producer artifacts, the review
plan, sibling reviewer reports, or the final gate artifact. Cross-reference
neighboring checks when useful, but keep material findings under your assigned
axis.
<!-- /surface:REVIEW-CHECK launch id=check-boundaries -->

<!-- surface:REVIEW-CHECK reference id=reference-map-v1 -->
- `reviews/<gate-id>/review-packet.md` — pointer map for the candidate and gate.
- `reviews/<gate-id>/review-plan.md` — lead-authored plan naming this check.
- `operational/shared/templates/check-review-report-template.md` — required
  report shape.
- `operational/shared/review-handbook.md` — shared gate and review-check
  operating contract.
- `design/HIGHER-LEVEL-GATES.md` — altitude-specific higher-gate behavior.
<!-- /surface:REVIEW-CHECK reference id=reference-map-v1 -->

<!-- surface:REVIEW-CHECK hidden id=hidden-surface-v1 -->
- Producer write surfaces are hidden; the candidate is evidence, not your
  workspace.
- Lower-level implementation internals are hidden unless the submitted evidence
  is missing, contradictory, stale, too vague, or needed for a stated bounded
  upper-altitude probe.
- Harness implementation internals and historical notes are hidden unless the
  review packet explicitly names them as evidence.
<!-- /surface:REVIEW-CHECK hidden id=hidden-surface-v1 -->

When the gate artifact is complete, write the review seat's terminal signal as
`DONE` with `evidence.verdict` set to `ACCEPT`, `BOUNCE`, or `ESCALATE`. The
signal must also include `evidence.gate_id` copied from the current
`review-packet.md`, `evidence.gate_artifact` pointing at the review-owned gate
artifact, and `evidence.producer_artifact` pointing at the submitted producer
artifact(s). The gate artifact remains authoritative; the signal carries the
routing value and candidate identity the harness uses.

Before writing the terminal signal, read `.sign-off.review.json` in your node
directory and copy its `owner_token` verbatim into `.signal.review.json`. Use the
`signal_path` from that same handshake file as the write target.

## Future Behavioral Tuning

The v1 dispatch contract is intentionally narrow: packet, plan, required check
reports for FULL mode, explicit SHORT exception, and gate-artifact synthesis.
Future tuning can add calibrated examples of `ACCEPT`, `BOUNCE`, and
`ESCALATE`, project-specific role presets, richer specialist reviewer briefs,
and user-simulation or end-to-end browser review once live runs show where those
checks improve behavior.
