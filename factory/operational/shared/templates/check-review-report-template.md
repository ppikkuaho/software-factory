# Check Review Report — `<review check>`

**Gate:** `<review-seat address>`
**Candidate:** `<producer-seat address>`
**Review Packet:** `<reviews/<gate-id>/review-packet.md>`
**Review Check:** `<check name>`
**Report Path:** `<reviews/<gate-id>/reviewers/<axis>/report.md>`

Recommended Routing: `<accept-note | bounce | escalate>`

Write exactly one `Recommended Routing:` line near the top. Do not include
multiple alternative routing lines.

Allowed checks and paths:

- Fidelity and coverage: `reviewers/fidelity-coverage/report.md`
- Composition and interface integrity: `reviewers/composition-interface/report.md`
- Risk, substrate, and handoff/readiness: `reviewers/risk-readiness/report.md`
- Evidence credibility (L3+/L4+ only): `reviewers/evidence-credibility/report.md`
- Broad acceptance review (commissioned L3 module panel only): `reviewers/broad/report.md`
- User simulation (L2+ only): `reviewers/user-simulation/report.md`
- Performance and robustness (L2+ only): `reviewers/performance-robustness/report.md`

## Scope Read

<Exact pointers read for this check.>

## Finding Summary

<One short paragraph. If there are no material findings, say what was checked and
why the check supports accept-note routing.>

## Findings

| ID | Severity | Confidence | Finding | Evidence Pointer | Criterion / Contract | Recommended Routing | Rationale |
|---|---|---|---|---|---|---|---|
| `<F-001>` | `<blocking/material/minor/note>` | `<high/medium/low>` | `<finding>` | `<pointer>` | `<criterion>` | `<accept-note/bounce/escalate>` | `<short rationale>` |

## Required Accounting

For Evidence Credibility reports, include a table:

| Lower Child / Output | Verdict Pointer | Currency / Stamp Checked | Credible Enough? | Rationale |
|---|---|---|---|---|
| `<child-or-output>` | `<pointer>` | `<hash/ts/gate-id/currentness check>` | `<YES/NO>` | `<why>` |

For Risk, Substrate, and Handoff/Readiness reports, include a table:

| Trigger | Applies? | Evidence Pointer / Rationale |
|---|---|---|
| Security/privacy | `<applies or N/A because...>` | `<pointer or rationale>` |
| Data/state | `<applies or N/A because...>` | `<pointer or rationale>` |
| Migration/compatibility | `<applies or N/A because...>` | `<pointer or rationale>` |
| Performance/scale | `<applies or N/A because...>` | `<pointer or rationale>` |
| Operations/observability | `<applies or N/A because...>` | `<pointer or rationale>` |
| Domain/policy | `<applies or N/A because...>` | `<pointer or rationale>` |
| Substrate/ownership | `<applies or N/A because...>` | `<pointer or rationale>` |
| Handoff/readiness | `<applies or N/A because...>` | `<pointer or rationale>` |

## Notes For Gate Lead

<Any uncertainty, dependency on another report, or reason an extra reviewer may
be needed.>

## Boundary Statement

<Confirm that this report contains findings only, not the final gate verdict,
and that any lower-level probe was bounded with a stated upper-altitude reason.>
