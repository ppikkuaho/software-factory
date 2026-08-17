## The Workstream Gate Owns The Composition Verdict

When your L5/L5+ pairs complete, your `#exec` seat submits a candidate workstream by filling
`report.md` and pointing at the lower gate verdicts. That submission is not parent-visible
completion. The `L4#review` gate reviews THE WORKSTREAM COMPOSITION — never the L5 code lines
or acceptance suites the L5+ reviewers already gated.

Required gate artifact: `reviews/<gate-id>/gate-composition-report.md`, produced by the review
gate without overwriting the producer's node-root artifacts. It carries: do the units integrate
(interfaces between tasks hold); cross-task conflicts;
coverage of your decomposition (every task accounted for — passed / bounced / escalated, with
its requirement IDs); what the gate verified by REPORT-reading (cite the L5+ verdicts); verdict
and concerns. The gate may PASS the workstream upward to L3, BOUNCE it back to `L4#exec` with
typed defects, or ESCALATE to L3 when the decision exceeds workstream-composition authority.

An ACCEPT needs direct implementation L5 child evidence: the L4's submitted workstream must point at
the implementation child bindings and their L5+ gate passes. L5 `test_author` children may refresh
acceptance packages, but they are supporting evidence; they do not count as implementation work.
