# Area Composition Review — `<area>#review` → `<requester>`

**Candidate:** `<area candidate pointer>`
**Verdict:** `<ACCEPT | BOUNCE | ESCALATE>`

## Outcome

<One paragraph explaining the verdict and what the requester can rely on.>

## Inputs Reviewed

<Exact pointers read: requester brief, frozen design, reports, review reports,
interface contracts, evidence, risk/deviation notes.>

## Review Plan

<Review checks used and reviewer reports read.>

In FULL mode, name the four reviewer report files and the `review_check` cohort barrier for the
current gate. Individual terminal rows append silently; report files alone are not terminal evidence.

### Short Review Exception Checklist

<Fill only if using the short review; otherwise write "not used". Every row must
be YES with an evidence pointer, or FULL mode with the four V1 review-check
reports plus the cohort barrier is required.>

| Condition | Yes/No | Evidence Pointer | Rationale |
|---|---|---|---|
| Workstream report count is 1-2 | <YES/NO> | <pointer> | <short rationale> |
| No shared state/data/API/migration/async/ownership/sequencing dependency is named | <YES/NO> | <pointer> | <short rationale> |
| Every requester requirement maps to one output, deferral, or escalation | <YES/NO> | <pointer> | <short rationale> |
| Each local review has verdict plus evidence pointer | <YES/NO> | <pointer> | <short rationale> |
| Exposed contract is unchanged or directly evidenced | <YES/NO> | <pointer> | <short rationale> |
| Handoff names covered requirements, exposed contract, evidence, and unresolved risks | <YES/NO> | <pointer> | <short rationale> |

## Material Findings

| ID | Check | Severity | Finding | Evidence Pointer | Authority Owner | Routing | Requested Action |
|---|---|---|---|---|---|---|---|
| <F-001> | <fidelity-coverage/composition-interface/evidence-credibility/risk-readiness> | <blocking/material/minor/note> | <finding> | <pointer> | <owner> | <accept-note/bounce/escalate> | <action or decision> |

## Coverage Summary

| Requirement | Source | Status | Output / Deferral / Escalation | Evidence |
|---|---|---|---|---|
| <R-...> | <brief/design> | <covered/deferred/missing/escalated> | <named item> | <pointer> |

## Internal Interface Summary

<Whether workstream outputs connect through compatible internal contracts.>

## Area Integration Judgment

<Whether the workstreams form one coherent area from the requester point of
view.>

## Exposed Contract Summary

<What other areas can rely on, and any exposed-contract risk.>

## Evidence Gaps

<Missing or weak evidence that affects confidence.>

## Risk And Deviation Summary

<Accepted, bounced, or escalated risks/deviations.>

## Requested Action

<Exactly one: ACCEPT next step; BOUNCE repairs with owner and artifact to
change; or ESCALATE question with options and evidence.>

## Requester Handoff Note

<What the requester can rely on and what remains unresolved.>
