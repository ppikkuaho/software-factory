# ADR-004 — Three R-007 sub-obligations get three distinct guards, separately tested

- **Status:** decided
- **Intent IDs:** R-007.1, R-007.2, R-007.3
- **C4 altitude:** system

## Decision
- R-007.1 (retry-safe) → **request idempotency guard** in `payments/intake`,
  keyed by submit idempotency key.
- R-007.2 (webhook-dedup) → **event dedup guard** in `payments/webhooks`, keyed
  by Stripe event id.
- R-007.3 (concurrency-safe) → **uniqueness/locking guard** on purchase identity
  in `payments/intake`.

Each guard is a separate component with its own negative/failure-path
acceptance test (replay N times; redeliver same event id; fire two concurrent
submits). The shared substrate primitive (ADR-003) does not collapse the three
tests into one.

## Rationale
The intent spec (§MNF Decomposition) establishes the three are **independent
failure modes** — each can fail while the others hold. The gate (Check 4b) will
require the three negative tests. Placing each guard in the area whose language
it speaks (request / event / purchase-identity) keeps membranes honest.

## Consequences
- Three acceptance tests are authored at planning time, before code, one per
  obligation (PROJECT-PLANNING Phase 4 anti-theater rule).
- A change to one guard must not silently weaken another.
