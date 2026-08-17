---
artifact: provisional-interface-contract
contract: Substrate ports (IdempotencyStore, EventLog)
status: PROVISIONAL (coarse; frozen at L2 compatibility review)
intent_ids: R-007.1, R-007.2, R-007.3, R-008
created: 2026-06-02
---

# Substrate Ports (provisional)

The sockets the feature areas plug into. The substrate is the deep module; these
are its thin contracts.

## Port: `IdempotencyStore` (ADR-003)
```
claim(key: IdempotencyKey) -> FRESH | DUPLICATE(prior_result)
commit(key: IdempotencyKey, result) -> void
```
- **SI-1:** `claim` is atomic — two concurrent `claim(k)` cannot both get `FRESH`
  (this is the uniqueness guarantee R-007.3 leans on).
- **SI-2:** after `commit(k, r)`, every later `claim(k)` returns `DUPLICATE(r)`.
- **SI-3:** the claim+side-effect+commit must be safe across a crash between
  claim and commit (recovery is a constraint to the substrate L3 — ADR-012).
- Keyspaces are namespaced: request keys (R-007.1) and Stripe-event-id keys
  (R-007.2) do not collide.

## Port: `EventLog` (ADR-001)
```
append(event: DomainEvent) -> void   # immutable, append-only
read(stream) -> [DomainEvent]
```
- **SE-1:** append-only; events are immutable once written (audit substrate for
  proving exactly-once — R-007/R-008).
- **SE-2:** every charge-created, order-paid, webhook-consumed transition emits
  an event.

## Value types (imported, no port)
- `Money` (ADR-002), `OrderId`/`ChargeId`/`PaymentIntentId`/`IdempotencyKey`/
  `EventId`, `Clock`.

## Provisional / open
- Whether `IdempotencyStore` and the concurrency-uniqueness guard (R-007.3) are
  literally the same table/constraint or two facets — deferred to substrate L3
  (ADR-006).
