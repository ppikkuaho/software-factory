---
artifact: per-area-spec (planning-L3 brief)
area: payments/intake
status: provisional brief for planning-L3
intent_ids: R-005, R-007.1, R-007.3, R-008
interfaces: contracts/payments-charge.md, contracts/orders-touchpoint.md, contracts/substrate-ports.md
adrs: 003, 004, 006, 007, 009, 012
created: 2026-06-02
---

# Area Spec — payments/intake

## Scope
The submit boundary. Accept an order-ready-to-charge submission; enforce
request-level idempotency (R-007.1) and the concurrency-uniqueness guard
(R-007.3); orchestrate the charge via `ChargeService`; record the attempt
durably before charging; drive (or hand to webhooks) the order transition.

## Provisional interfaces
Consumes `ChargeService` (contracts/payments-charge.md), `IdempotencyStore`
(substrate), `OrderPaymentState` (orders). Exposes a submit endpoint (shape TBD
by L3 — REST/RPC, transport-not-contract).

## Constraints (D26 rubric)
- **C-I1 (R-007.1):** a replayed submit with the same idempotency key → exactly
  one CHARGED across the system. Use `IdempotencyStore.claim` on the request key.
- **C-I2 (R-007.3, ADR-006):** two concurrent submits for the same purchase →
  exactly one charge; loser REFUSED/coalesced (T-3), never a 2nd charge. Use the
  durable uniqueness guard, not check-then-act.
- **C-I3 (ADR-012):** durably record the charge-attempt intent BEFORE calling
  `ChargeService`, so a lost response is reconcilable. No CHARGED without a prior
  record.
- **C-I4 (ADR-007):** on any ambiguous outcome, REFUSE_FOR_SAFETY; never retry
  into a second charge.
- **C-I5 (ADR-008):** never write order paid-state locally; go through
  `OrderPaymentState`.
- **DEFERRED-to-you:** the exact uniqueness mechanism (ADR-006) and the
  charge↔order write protocol (ADR-012) — resolve within the constraints above,
  escalate if the datastore can't support them.

## Acceptance (negative tests authored before build)
- Replay same idempotency key ×N → one CHARGED, rest ALREADY_CHARGED.
- Two concurrent submits same purchase → one CHARGED, other REFUSED/coalesced.
- Kill between attempt-record and charge → reconciliation reaches exactly one charge.
