# ADR-009 — Stripe idempotency key derived deterministically from the domain idempotency key

- **Status:** decided
- **Intent IDs:** R-007.1, R-002
- **C4 altitude:** component (gateway-internal contract obligation)

## Decision
When `payments/gateway` calls Stripe, it passes a Stripe `Idempotency-Key`
header that is a **deterministic function of the domain idempotency key** for
that charge attempt (e.g. a namespaced hash). The same domain attempt always
maps to the same Stripe key.

## Rationale
This stacks a second, Stripe-side at-most-once guarantee underneath our own
(ADR-003): even if our request guard is bypassed by a code path we missed, the
same domain attempt cannot create two Stripe charges, because Stripe itself
dedupes on its idempotency key. Defense in depth for R-007.1. Deterministic
derivation (not a fresh random key per call) is the load-bearing part —
a random key per retry would defeat Stripe's dedup.

## Consequences
- Gateway must receive (or be able to derive) the domain key; the charge
  interface carries it (see contracts/payments-charge.md).
- Retried gateway calls for the same attempt reuse the same Stripe key.
