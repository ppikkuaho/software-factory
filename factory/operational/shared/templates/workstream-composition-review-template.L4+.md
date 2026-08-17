# Workstream Composition Review — `<workstream>#review` → `<area>`

**Candidate:** `<workstream candidate pointer>`
**Verdict:** `<ACCEPT | BOUNCE | ESCALATE>`

## Outcome

<One paragraph explaining the verdict and what the area lead can rely on.>

## Inputs Reviewed

<Exact pointers read: L3 brief, rubric, task reports, local review reports,
interface contracts, deterministic evidence, integration evidence, risk/deviation
notes.>

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
| Task output count is 1-2 | <YES/NO> | <pointer> | <short rationale> |
| No shared state/data/API/migration/async/ownership/sequencing dependency is named | <YES/NO> | <pointer> | <short rationale> |
| Every workstream obligation maps to one output, deferral, or escalation | <YES/NO> | <pointer> | <short rationale> |
| Each local review has verdict plus evidence pointer | <YES/NO> | <pointer> | <short rationale> |
| Workstream interface is unchanged or directly evidenced | <YES/NO> | <pointer> | <short rationale> |
| Handoff names covered requirements, task outputs, evidence, and unresolved risks | <YES/NO> | <pointer> | <short rationale> |

## Material Findings

| ID | Check | Severity | Finding | Evidence Pointer | Authority Owner | Routing | Requested Action |
|---|---|---|---|---|---|---|---|
| <F-001> | <fidelity-coverage/composition-interface/evidence-credibility/risk-readiness> | <blocking/material/minor/note> | <finding> | <pointer> | <owner> | <accept-note/bounce/escalate> | <action or decision> |

## Coverage Summary

| Requirement / Obligation | Source | Status | Output / Deferral / Escalation | Evidence |
|---|---|---|---|---|
| <R-...> | <brief/rubric> | <covered/deferred/missing/escalated> | <named item> | <pointer> |

## Task Interface Summary

<Whether task outputs connect through compatible workstream contracts.>

## Workstream Integration Judgment

<Whether tasks form one coherent workstream from the area lead point of view.>

## Lower Evidence Gaps

<Missing or weak lower evidence that affects confidence.>

## Boundary And Consumability Summary

<What the area lead can rely on, what the workstream exposes, and any handoff
risk.>

## Requested Action

<Exactly one: ACCEPT next step; BOUNCE repairs with owner and artifact to
change; or ESCALATE question with options and evidence.>
