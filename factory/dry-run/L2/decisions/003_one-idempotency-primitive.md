# ADR-003 — One generic IdempotencyPrimitive in the substrate

- **Status:** decided
- **Intent IDs:** R-007.1, R-007.2, R-007.3
- **C4 altitude:** component (substrate port)

## Decision
Provide a single generic at-most-once primitive in the substrate — an
`IdempotencyStore` with a `claim(key) → {FRESH | DUPLICATE(prior_result)}` /
`commit(key, result)` protocol backed by a uniqueness-guaranteeing durable
store. Both request idempotency (R-007.1, keyed by the submit idempotency key)
and webhook dedup (R-007.2, keyed by the Stripe event id) are built on this one
primitive. The concurrency guard (R-007.3) leans on the same underlying
uniqueness store.

## Rationale
R-007.1 and R-007.2 are the same shape ("perform this side effect at most once
for this key") on different keys. One primitive, two keyspaces, prevents two
divergent dedup implementations. The uniqueness guarantee that defeats races
(R-007.3) is the same store property. This is the concrete realization of the
substrate (ADR-001).

## Consequences
- The store's uniqueness guarantee is the single most load-bearing property in
  the slice; its concrete tech is constrained but the choice is deferred
  (ADR-006, ADR-010).
- `claim/commit` must be atomic w.r.t. the side effect, or the guarantee leaks —
  this is a constraint passed to the substrate L3 (see brief).

## Note / risk
The three R-007 sub-obligations remain **independently tested** (ADR-004) even
though they share this primitive — sharing the mechanism must not collapse the
three tests into one.
