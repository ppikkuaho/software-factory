## Build to the Claim — Fix It, Drive It, Account for It, Stop

For implementation work, your claim is the smallest recipient-visible increment named by your
decision-complete brief. Do not silently “fix the claim yourself.” If the claim, its boundary, or
its acceptance is ambiguous, send L4 a direct-edge message with `needs_answer: true` and hold the
affected work until the answer lands.

The artifact's **face** is part of the claim: commands, options, documented examples, emitted
fields, errors, and existing entry points are promises the recipient will act on. Keep that face no
larger than the claim you can prove completely.

Prove the claim where it lives. Drive the real artifact through the package's live-scenario
invocation on a disposable instance and observe the recipient-visible result. Green tests,
compilation, types, and code reading are evidence inputs; they are not substitutes for watching the
artifact do the promised thing. Re-walk existing entry points against every new state this
increment creates.

Stop when the claim is proven. Reopen it only when a promise on the artifact's face is broken or an
existing public entry point misbehaves on a state this increment created. Every other finding is
**inventory owned by L4**: record it; do not fix it in this diff. In `report.md`, keep the account
explicit under the exact headings `## Drove and Watched`, `## Inferred`,
`## Residual Uncertainty`, and `## Inventory`.
