---
artifact: per-area-spec (planning-L3 brief)
area: orders (touchpoint only — full Orders area is deferred/out-of-slice)
status: provisional brief for planning-L3
intent_ids: R-005, R-008
interfaces: contracts/orders-touchpoint.md
adrs: 008, 012
created: 2026-06-02
---

# Area Spec — orders (slice touchpoint)

## Scope (deliberately minimal)
Model ONLY what the Payments slice needs: the order payment state machine
`READY → PAID | FAILED`, the single source of truth for "is this order paid?"
(ADR-008), and the R-008 integrity invariant. The full Orders area (creation,
line items, cart assembly, fulfillment — R-010/R-012) is **deferred / out of
slice**; do not build it.

## Provisional interface
Exposes `OrderPaymentState` (contracts/orders-touchpoint.md).

## Constraints (D26 rubric)
- **C-O1 (ADR-008):** sole owner of order paid-state; payments areas transition
  through this port, never write a parallel flag.
- **C-O2 (OI-1):** `mark_paid` is idempotent on (order_id, idempotency_key) —
  supports webhook redelivery (R-007.2) driving the transition.
- **C-O3 (OI-2, R-008):** an order becomes PAID only against a recorded successful
  charge of matching amount; no paid-but-uncharged, no charged-but-unpaid.
- **C-O4 (OI-3):** READY is the only legal pre-state for mark_paid (orders arrive
  ready-to-charge; cart/checkout deferred).
- **ASSUMPTION FLAGGED:** "order arrives ready-to-charge with a known total" — the
  cart/checkout that produces this is out of slice. The touchpoint must define a
  minimal seam where a ready order enters; how orders are *created* is NOT in scope.

## Acceptance (negative tests authored before build)
- mark_paid twice same key → one transition, AlreadyPaid second time.
- mark_paid with amount ≠ recorded charge amount → rejected (R-008).
- mark_paid on a non-READY order → rejected.
