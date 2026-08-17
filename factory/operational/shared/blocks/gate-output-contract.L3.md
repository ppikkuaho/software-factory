## The Area Gate Owns The Integration Verdict

When your workstreams report complete, your `#exec` seat submits a candidate area by filling
`report.md` and pointing at the lower gate verdicts. That submission is not parent-visible
completion. The `L3#review` gate reviews THE AREA COMPOSITION — never task internals (they
passed L5/L5+) and never workstream internals (they passed L4).

Required gate artifact: `reviews/<gate-id>/gate-area-composition-review.md`, produced by the
review gate without overwriting the producer's node-root artifacts. It carries: do the workstreams
compose into the area design; do internal interfaces
match; do exposed interfaces honor the L2 area/module spec; are cross-workstream assumptions
compatible; which requirement IDs the area composition discharges; verdict + concerns. **Do not
re-run lower-level test suites** — cite their gated results by reference. The gate may PASS the
area upward to L2, BOUNCE it back to `L3#exec` with typed defects, or ESCALATE to L2 when the
decision exceeds area-integration authority.

An ACCEPT needs direct L4 child execution evidence: the L3's submitted area must point at the
workstream child bindings and their L4+ gate passes. A planning-only L3 is marked separately as
`child_purpose: planning`; an execution L3 cannot substitute its own inline code or report for the
L4 workstream spine.
