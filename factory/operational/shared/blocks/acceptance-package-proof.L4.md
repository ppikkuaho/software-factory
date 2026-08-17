## Price the Proof and Hand Off One Complete Acceptance Package

Before spawning each test-author, write one stakes-pricing line in its brief: what shipping this
task's claim false would cost, who or what it would affect, and how hard the result would be to
reverse. Use that judgment to price package and review depth. A small reversible claim needs a
small complete proof; a high-consequence or hard-to-recall claim needs deeper scenarios, failure
probes, and review.

The bindable top-level `tests/` home is one contract with three required parts:

1. executable acceptance checks, fixtures, helpers, and exact command wrappers;
2. the runnable live-scenario spine, documented with exact setup/invocation/observation in
   `tests/live-scenario.md`;
3. a non-empty `tests/red-run-log.md` showing the observed failing run for **each new check**
   before its green is trusted.

Repeat this invariant in every test-author brief through the registered brief template. If the
claim is not decision-complete, resolve it before spawn or answer the child's direct-edge
`needs_answer` message; the test-author never supplies the missing product decision. L5+ owns the
substance judgment. Once accepted, all three package parts move together through the stable
L4-owned contract home into the implementation node.
