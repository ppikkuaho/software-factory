---
artifact: provisional-interface-contract
contract: Orders touchpoint interface
status: PROVISIONAL (coarse; frozen at L2 compatibility review)
intent_ids: R-005, R-008
created: 2026-06-02
---

# Orders Touchpoint Interface (provisional)

`orders` owns the single source of truth for order payment state (ADR-008).
`payments/*` drive transitions through this port; they never write paid-state
locally.

## Port: `OrderPaymentState`
```
get_state(order_id) -> READY | PAID | FAILED
mark_paid(order_id, charge_id, amount, idempotency_key) -> Transitioned | AlreadyPaid | Rejected
mark_failed(order_id, reason) -> void
```
- **OI-1 (idempotent transition):** `mark_paid` with the same
  (order_id, idempotency_key) twice → one transition; second returns
  `AlreadyPaid` (supports R-007.2 webhook redelivery driving the transition).
- **OI-2 (R-008 invariant):** `mark_paid` succeeds only against a recorded
  successful charge of matching `amount`; an order cannot become PAID without
  one. Conversely a recorded successful charge must have a path to PAID.
- **OI-3:** `READY` is the only legal pre-state for `mark_paid` (the slice
  assumes orders arrive ready-to-charge — R-010 deferred).

## Provisional / open
- Whether `mark_paid` is called by intake (sync) or only by webhooks
  (authoritative) — ties to ADR-005 reconciliation; resolved at compatibility
  review.
- The amount-match check (OI-2) presumes the order carries a total; the order
  model is minimal in-slice (touchpoint only).
