# Higher Gate Review Plan — `reviews/<gate-id>/review-plan.md`

**Gate:** `<review-seat address>`
**Candidate:** `<producer-seat address>`
**Review Packet:** `<reviews/<gate-id>/review-packet.md>`

Review Mode: `<FULL | SHORT>`
Short Review Exception: `<YES only when Review Mode is SHORT; otherwise write NO>`

## Candidate Reading

<Pointers read before choosing the review plan: review packet, parent brief,
rubric, producer report, lower verdicts, evidence summaries, open decisions, and
known risks.>

## Role Selection

<Explain the review mode. In FULL mode, name the exact level roster:

- L2+ five-seat roster: fidelity-coverage, composition-interface,
  risk-readiness, user-simulation, and performance-robustness.
- L3+/L4+ four-seat roster: fidelity-coverage, composition-interface,
  evidence-credibility, and risk-readiness.

In SHORT mode, explain why one right-sized review task can cover the material
questions. Missing reviewer substrate is not a SHORT reason.>

## Reviewer Dispatch

Use this table for FULL mode. Keep exactly the rows required by the gate level;
each report path is relative to this review directory.

| Review Check | Reviewer Task | Report Path | Grounding Material |
|---|---|---|---|
| Fidelity and coverage | `<one narrow review task>` | `reviewers/fidelity-coverage/report.md` | `<packet/spec/evidence pointers>` |
| Composition and interface integrity | `<one narrow review task>` | `reviewers/composition-interface/report.md` | `<packet/spec/evidence pointers>` |
| Risk, substrate, and handoff/readiness | `<one narrow review task>` | `reviewers/risk-readiness/report.md` | `<packet/spec/evidence pointers>` |
| Evidence credibility (L3+/L4+ only) | `<one narrow review task>` | `reviewers/evidence-credibility/report.md` | `<packet/spec/evidence pointers>` |
| User simulation (L2+ only) | `<one narrow product-face probe>` | `reviewers/user-simulation/report.md` | `<probe roster/disposable-instance pointers>` |
| Performance and robustness (L2+ only) | `<one narrow product-face probe>` | `reviewers/performance-robustness/report.md` | `<probe roster/disposable-instance pointers>` |

The gate lead writes this plan, then the harness daemon dispatches the
review-check seats from it. You may read reports as they appear, but synthesize
only from the completed reviewer set. Individual terminal rows append silently; synthesize only
after every report exists and the address-owned `review_check` cohort barrier crosses. Report-file
presence alone is not terminal evidence. In FULL mode, the lead
does not author these check reports and does not use native Agent/Task/subagent
sidechains for these gate reviewers.

If any selected check remains open, end the turn in a ledger-derived wait. Do not hold the pane in
a polling loop; the barrier transition supplies the single wake.

## Short Review Exception

Use this section only for SHORT mode. Each row must carry an explicit `YES`,
an evidence pointer, and a rationale. If any row is not clearly YES, switch to
FULL mode.

| Condition | YES | Evidence Pointer | Rationale |
|---|---|---|---|
| `<short exception condition>` | `YES` | `<pointer>` | `<why it is clearly satisfied>` |

## Added Reviewers

<Record any extra reviewer spawned after the first reports, the uncovered risk
that required it, and the report path. Write "none" if no extra reviewer was
needed.>

## Completion Evidence To Read Before Synthesis

<List the reviewer report files and the cohort barrier the gate lead must read
before writing the gate artifact. In FULL mode this must include the exact
level-specific roster named above: five L2+ reports or four L3+/L4+ reports,
plus the `review_check` cohort barrier for the current gate. State that report
files alone were not treated as completion evidence.>
