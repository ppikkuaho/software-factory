# Product Composition Review — `<product>#review` → `<requester>`

**Candidate:** `<product candidate pointer>`
**Verdict:** `<ACCEPT | BOUNCE | ESCALATE>`

## Outcome

<One paragraph explaining the verdict and what the requester can rely on.>

## Inputs Reviewed

<Exact pointers read: intent/spec, architecture, ADRs, area reports, area review
reports, cross-area contracts, flow evidence, deviation/risk ledger, delivery
handoff.>

## Review Plan

<Review checks used and reviewer reports read.>

In FULL mode, name the five reviewer report files and the `review_check` cohort barrier for the
current gate. Individual terminal rows append silently; report files alone are not terminal evidence.

### Short Review Exception Checklist

<Fill only if using the short review; otherwise write "not used". Every row must
be YES with an evidence pointer, or FULL mode with the five product-altitude review-check
reports plus the cohort barrier is required.>

The FULL roster is:
`reviewers/fidelity-coverage/report.md`,
`reviewers/composition-interface/report.md`,
`reviewers/risk-readiness/report.md`,
`reviewers/user-simulation/report.md`, and
`reviewers/performance-robustness/report.md`.

| Condition | Yes/No | Evidence Pointer | Rationale |
|---|---|---|---|
| Product contains one area/module or no cross-area interface beyond documented handoff | <YES/NO> | <pointer> | <short rationale> |
| No shared state/data/API/migration/async/ownership/sequencing dependency is named | <YES/NO> | <pointer> | <short rationale> |
| Every intent/spec requirement maps to one output, deferral, or escalation | <YES/NO> | <pointer> | <short rationale> |
| Each area review has verdict plus evidence pointer | <YES/NO> | <pointer> | <short rationale> |
| End-to-end behavior is not material or has named evidence | <YES/NO> | <pointer> | <short rationale> |
| Handoff names covered requirements, product contracts, evidence, and unresolved risks | <YES/NO> | <pointer> | <short rationale> |

## Material Findings

| ID | Check | Severity | Finding | Evidence Pointer | Authority Owner | Routing | Requested Action |
|---|---|---|---|---|---|---|---|
| <F-001> | <fidelity-coverage/composition-interface/risk-readiness/user-simulation/performance-robustness> | <blocking/material/minor/note> | <finding> | <pointer> | <owner> | <accept-note/bounce/escalate> | <action or decision> |

## Intent / Architecture Coverage Summary

| Requirement / Obligation | Source | Status | Output / Deferral / Escalation | Evidence |
|---|---|---|---|---|
| <R-...> | <intent/architecture/ADR> | <covered/deferred/missing/escalated> | <named item> | <pointer> |

## Cross-Area Interface Summary

<Whether areas connect through compatible product-level contracts.>

## Product Integration Judgment

<Whether areas form one coherent product from the requester point of view.>

## End-To-End Flow Summary

<Primary flows supported by evidence and gaps remaining.>

## Shared Ownership Summary

<Substrate ownership, duplicated responsibilities, and shared-state concerns.>

## Evidence Gaps

<Missing or weak evidence that affects confidence.>

## Deviation And Risk Summary

<Accepted, bounced, or escalated risks/deviations.>

## Requested Action

<Exactly one: ACCEPT next step; BOUNCE repairs with owner and artifact to
change; or ESCALATE question with options and evidence.>

## Requester Handoff Note

<What the requester can rely on and what remains unresolved.>
