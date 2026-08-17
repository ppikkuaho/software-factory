# L3+ Area Composition Review Spawn Template

Filled by the harness when opening an area review.

---

## Load These Documents

- `operational/L3+/role.md`
- `operational/L3+/config.md`
- `operational/shared/review-handbook.md`
- `operational/shared/templates/higher-gate-review-plan-template.md`
- `operational/shared/templates/check-review-report-template.md`
- `operational/shared/templates/area-composition-review-template.L3+.md`
- `operational/shared/comms-protocol.md`
- `operational/shared/agent-lifecycle.md`
- `operational/shared/runtime-and-model-map.md`
- `design/GATE-LIFECYCLE.md`
- `design/HIGHER-LEVEL-GATES.md`
- `design/QUALITY-GATE.md`

## Runtime

Use the assigned runtime. Runtime choice does not change the review standard,
required artifacts, or verdict meanings.

## Assignment

Review the submitted area candidate at:

`{{AREA_CANDIDATE_POINTER}}`

Requester brief:

`{{REQUESTER_BRIEF_POINTER}}`

Frozen area design:

`{{AREA_DESIGN_POINTER}}`

Review packet:

`{{REVIEW_PACKET_POINTER}}`

Author:

- `reviews/{{GATE_ID}}/review-plan.md`
- `reviews/{{GATE_ID}}/gate-area-composition-review.md`

Wait for these harness-created check reports and the `review_check` cohort barrier before synthesis
in FULL mode:

- `reviews/{{GATE_ID}}/reviewers/fidelity-coverage/report.md`
- `reviews/{{GATE_ID}}/reviewers/composition-interface/report.md`
- `reviews/{{GATE_ID}}/reviewers/evidence-credibility/report.md`
- `reviews/{{GATE_ID}}/reviewers/risk-readiness/report.md`

In FULL mode, you write the plan, the daemon opens the four check-reviewer seats, you wait until
each selected check has a report and the cohort barrier has crossed, synthesize the final gate
artifact, and sign the verdict. Individual child rows append silently; the barrier is the one
synthesis wake. Do not author the four check reports yourself or use native Agent/Task sidechains for
them. SHORT mode is only for the documented small-candidate exception; missing reviewer substrate
is not a SHORT reason.
If any selected check remains open, end the current turn in a ledger-derived wait. Do not poll.

The final report must carry `VERDICT: ACCEPT`, `VERDICT: BOUNCE`, or
`VERDICT: ESCALATE`.

## Inputs To Read

- requester brief and inherited requirements;
- frozen area design;
- submitted area report;
- workstream reports and composition review reports;
- internal interface contracts;
- exposed interface contracts;
- integration evidence;
- risk/deviation notes;
- prior bounce or escalation notes, if present.

## Completion

When `reviews/{{GATE_ID}}/gate-area-composition-review.md` is complete, write the review seat's terminal
signal as `DONE` with `evidence.verdict` set to `ACCEPT`, `BOUNCE`, or
`ESCALATE` and `evidence.gate_id` set to `{{GATE_ID}}`. Use
`evidence.gate_artifact` for `reviews/{{GATE_ID}}/gate-area-composition-review.md`
and `evidence.producer_artifact` for the submitted producer artifact(s). Read
`.sign-off.review.json` immediately before signing and copy its `owner_token`
verbatim into `.signal.review.json`.
