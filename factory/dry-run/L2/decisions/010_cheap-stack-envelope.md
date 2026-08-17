# ADR-010 — Cheap-stack envelope: one relational DB + one service (exact choice deferred)

- **Status:** deferred (envelope decided; concrete choice is a constraint to L3)
- **Intent IDs:** R-003, R-004, R-002
- **C4 altitude:** container

## Decision (the envelope, not the product)
The slice runs as a **single service backed by a single relational datastore that
supports atomic uniqueness constraints and transactions**. The exact
language/framework/datastore/host is **delegated downward** (R-004) subject to:
- R-003: cheap to run (favor a managed-but-free-tier or single-box relational DB
  over a multi-service / always-on cluster);
- the uniqueness-guarantee requirement (ADR-003/006) — the datastore MUST be able
  to enforce a uniqueness constraint atomically;
- R-002: Stripe SDK availability in the chosen language.

## The constraint the L3 is held to (D26)
1. Pick the cheapest stack that still gives **atomic uniqueness + transactions**
   (needed for R-007.3 and R-008). A datastore without this is disqualified
   regardless of cost.
2. If cost and the uniqueness guarantee conflict, **escalate to L1** — do not
   silently trade away the MNF for cost.
3. Single deployable unit preferred (cost + simplicity) unless a seam forces
   otherwise.

## Rationale
R-004 delegates the stack with R-003 (cheap) and R-002 (Stripe) as the bounding
rubric. The architecturally-significant part is not *which* DB but that the slice
needs **one** stable relational store with atomic uniqueness — that property is
what R-007.3/R-008 depend on and is decided now; the brand is deferred.

## Tension flagged
R-003 (cheap) vs. the uniqueness/transaction requirement can conflict on the
cheapest possible infra. Priority applied: correctness of R-007 > cost. Surfaced
to L1 in project.md §7.
