# ADR-012 — Charge↔order-state write protocol for R-008 (deferred to L3 as constraint)

- **Status:** deferred (the no-divergence property decided; the protocol deferred)
- **Intent IDs:** R-008, R-007
- **C4 altitude:** component (cross intake/gateway/webhooks/orders)

## Decision (the property, not the protocol)
The system must never end in a state where money and order disagree:
charged-but-order-unpaid, or paid-but-uncharged (R-008). The **concrete write
protocol** that guarantees this — the ordering of (create-charge-record,
call-Stripe, transition-order) and the recovery path when a step fails between
them — is **deferred** to the planning-L3s for intake/webhooks/orders, because it
is domain-deep and depends on the datastore's transaction model (ADR-010).

## The constraint the L3 is held to (D26)
1. There must be **no committed state** in which a Stripe charge succeeded but no
   path will ever transition the order to PAID, and none in which an order is
   PAID with no recorded successful charge.
2. The protocol must be **crash-safe**: a crash between any two steps leaves a
   state from which reconciliation (the webhook channel, ADR-005) can recover to
   a consistent end state.
3. Ambiguous/in-flight states resolve in the **safe-failure direction**
   (ADR-007): never resolve an ambiguity by charging again.
4. The intent record (a charge attempt) is **durably recorded before** the Stripe
   call, so a lost sync response can be reconciled (this is what makes R-007.1 +
   R-006 work together).
5. Negative tests required: kill the process between charge-success and
   order-transition → reconciliation reaches PAID exactly once, no second charge.

## Rationale
R-008 is the integrity invariant in R-007's blast radius. *That* it must hold is
an L2 decision (it crosses gateway/webhooks/orders). *How* (exact transaction
boundaries) is datastore-deep — deferred with the constraints above so it is
delegated, not dropped.

## Status caveat
R-008 is reflect-back PENDING (L1-derived). Flagged to L1.
