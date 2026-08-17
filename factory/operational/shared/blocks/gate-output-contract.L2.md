## The Product Gate Owns The Composition Verdict

When your areas/modules report complete, your `#exec` seat submits a candidate product
composition by filling `report.md` and pointing at the lower gate verdicts. That submission is
not parent-visible completion. The `L2#review` gate reviews THE PRODUCT COMPOSITION — never the
units (they passed the L5 gate), never workstream internals (they passed L4's), and never area
internals (they passed L3's).

Required gate artifact: `reviews/<gate-id>/gate-composition-review.md`, produced by the review
gate without overwriting the producer's node-root artifacts. It carries: do the areas/modules
connect (interfaces honored as frozen); does the
assembled product cohere with the architecture L2 laid down; cross-module conflicts; the
requirement IDs the product composition discharges; verdict + concerns. **Do not re-run
lower-level test suites** — cite their gated results by reference. The gate may PASS the
composition upward to L1, BOUNCE it back to `L2#exec` with typed defects, or ESCALATE to L1
when the decision exceeds product-composition authority.

## L3 Execution Spine (non-collapsible)

The project-build cascade always passes through an L3 area/module owner. For a trivial area, L2 may
use one recorded L3 instance to carry both area design and execution-management responsibility when
that is recorded in an ADR (`DD-...`, `status: decided`). That L3 still drives product execution
through the harness layer below it.

L2 does not spawn L4 directly for a small project, and it does not ask an L3 to write product code
inline. If the work needs product execution, L2 prepares the L3 child node and asks the harness to
spawn it; the L3 owns the area/workstream handoff below.
Non-collapsible at any scale: the frozen intent anchor, acceptance-before-executor (M51), the
independent L5+ review, the L3 area owner, and the L2 review gate's composition verdict.
