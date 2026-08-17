# ADR-007 — Safe-failure direction baked into the contracts

- **Status:** decided
- **Intent IDs:** R-007, R-008, T-3
- **C4 altitude:** system

## Decision
Every guard and every ambiguous-outcome path **fails toward not charging twice**.
When the system cannot determine whether a charge already happened, it refuses or
coalesces rather than charging again — accepting a possible false-reject of a
genuinely-distinct second purchase as the tolerated error. This direction is
encoded in the interface contracts (the charge interface returns a
`DUPLICATE`/`ALREADY_CHARGED` outcome that is a success-shaped no-op, and the
submit contract may return a safe-reject).

## Rationale
T-3 established the user's tolerated error direction: reject a legitimate second
purchase before risking a double-charge. This is load-bearing for how aggressively
guards reject and for R-008. It is a cross-cutting invariant, so it is fixed at
L2 and threaded into every contract, not left to per-area taste.

## Status caveat
The T-3 direction and R-008 are reflect-back PENDING (L1-derived for R-008).
Flagged to L1; if the user wants the opposite bias, contracts change.

## Consequences
- Contracts distinguish "charged now", "already charged (no-op)", and
  "refused for safety" — three outcomes, not a boolean.
- A guard that is unsure defaults to refuse, never to charge.
