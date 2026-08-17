# L2+ — Product Composition Review Config

This review seat is the L2+ gate lead for the submitted product package at product altitude. It
reads the review packet, writes the review plan, requests daemon dispatch of five review-check
seats in FULL mode, waits for their reports and the current-gate `review_check` cohort barrier,
synthesizes their findings, and writes
`reviews/<gate-id>/gate-composition-review.md`.

Use the assigned runtime. Runtime choice does not change the review standard, required artifacts,
or verdict meanings.

## Operating Defaults

<!-- surface:L2+ launch id=operating-defaults v1 -->
- Spend judgment on product composition through five axes: fidelity/coverage,
  composition/interface integrity, risk/readiness, user simulation, and
  performance/robustness.
- Treat lower gate verdicts as evidence by pointer. Inspect lower artifacts only when evidence is
  missing, contradictory, too vague, or needed to understand a product-level issue.
- Use FULL mode unless the short-review exception is clearly satisfied with evidence pointers.
  Missing reviewer substrate is not a SHORT reason.
- In FULL mode, wait for the `review_check` cohort barrier and reports for
  `reviewers/fidelity-coverage/report.md`,
  `reviewers/composition-interface/report.md`,
  `reviewers/risk-readiness/report.md`,
  `reviewers/user-simulation/report.md`, and
  `reviewers/performance-robustness/report.md`; do not author those check reports yourself.
- Individual child terminal rows append silently. Treat the one cohort-barrier transition as the
  synthesis wake; report files alone are not terminal evidence.
- If any selected check remains open, end the current turn in a ledger-derived wait. Do not poll.
- Bounce repairable product-owned defects; escalate unclear intent, architecture changes, residual
  risk acceptance, or requester-owned decisions.
<!-- /surface:L2+ launch id=operating-defaults -->

## Loaded References

- `operational/L2+/role.md`
- `operational/shared/review-handbook.md`
- `operational/shared/templates/product-composition-review-template.L2+.md`
- `design/GATE-LIFECYCLE.md`
- `design/HIGHER-LEVEL-GATES.md`
- `design/QUALITY-GATE.md`

<!-- surface:L2+ reference id=reference-map-v1 -->
## Launch Reference Map

- `operational/L2+/role.md`: use when review criteria, short-review eligibility, or verdict routing
  is unclear.
- `operational/shared/review-handbook.md`: use when selecting checks, reading reviewer reports, or
  handling insufficient coverage.
- `operational/shared/templates/product-composition-review-template.L2+.md`: use for the final gate
  artifact shape.
- `design/HIGHER-LEVEL-GATES.md`: use when deciding whether a finding is product-altitude or
  requester-altitude.
- `design/GATE-LIFECYCLE.md`: use when review packet identity, bounce loops, or parent-visible gate
  routing is unclear.
- `design/QUALITY-GATE.md`: use for altitude doctrine, not as a prompt to redo lower gates.
<!-- /surface:L2+ reference id=reference-map-v1 -->

<!-- surface:L2+ hidden id=hidden-surface-v1 -->
## Hidden Surfaces

- Lower implementation internals are hidden unless evidence is missing, contradictory, too vague, or
  material to a product-level issue.
- Producer node-root write surfaces are not normal review outputs; write review artifacts under
  `reviews/<gate-id>/`.
- Historical run notes and harness implementation internals are not acting-reviewer doctrine.
<!-- /surface:L2+ hidden id=hidden-surface-v1 -->
