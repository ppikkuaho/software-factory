---
artifact: per-area-spec (planning-L3 brief)
area: payments/webhooks
status: provisional brief for planning-L3
intent_ids: R-006, R-007.2, R-008
interfaces: contracts/substrate-ports.md, contracts/orders-touchpoint.md
adrs: 001, 003, 004, 005, 007, 008, 012
created: 2026-06-02
caveat: R-006 and R-008 are reflect-back PENDING (L1-derived). If rejected, this area is reopened.
---

# Area Spec — payments/webhooks

## Scope
Receive Stripe webhooks; verify signature; dedup on Stripe event id (R-007.2);
reconcile the async authoritative outcome with the provisional sync result
(R-006); drive the order→PAID/FAILED transition (ADR-008); hold no-divergence
(R-008).

## Provisional interfaces
Consumes `IdempotencyStore` (event-id keyspace), `EventLog`, `OrderPaymentState`.
Exposes a webhook endpoint (transport TBD by L3).

## Constraints (D26 rubric)
- **C-W1 (R-007.2):** the same Stripe event id delivered twice → exactly one
  state transition; the second is an idempotent no-op. Use `IdempotencyStore.claim`
  keyed by Stripe event id.
- **C-W2 (signature):** verify Stripe webhook signature before acting; reject
  unsigned/forged events.
- **C-W3 (ADR-005/006):** the webhook is the AUTHORITATIVE outcome; reconcile it
  with the provisional sync result. If they disagree, resolve in the safe-failure
  direction (ADR-007) and never double-charge/double-credit.
- **C-W4 (ADR-008):** drive the transition via `OrderPaymentState.mark_paid`
  (idempotent OI-1); never write paid-state locally.
- **C-W5 (ADR-012, R-008):** a charge confirmed by webhook with no order
  transition must be recoverable to PAID; an order PAID must trace to a confirmed
  charge. No divergence.

## Acceptance (negative tests authored before build)
- Same event id ×2 → one transition, second no-op.
- Forged/unsigned event → rejected.
- Sync said failed but webhook confirms success → reconcile to one charge, PAID once.

## RENEGOTIATION FLAG
Whether the order transition is driven HERE (webhook, authoritative) or in intake
(sync) is unresolved across intake/gateway/webhooks. **Resolve at the L2
compatibility review** — this is a known cross-area interface ripple.
