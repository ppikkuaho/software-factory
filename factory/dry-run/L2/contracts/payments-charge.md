---
artifact: provisional-interface-contract
contract: Payments charge interface (intake → gateway)
status: PROVISIONAL (coarse; fluid during planning cascade, frozen at L2 compatibility review)
owner: defined by the core (gateway is the port; intake adapts to it)
intent_ids: R-005, R-007.1, R-007.2, R-009-derived(safe-failure), R-014
created: 2026-06-02
---

# Provisional Payments Charge Interface

This is the socket `payments/gateway` exposes and `payments/intake` plugs into.
**Coarse and provisional** — planning-L3s pressure-test and may renegotiate it
upward. Frozen at the L2 compatibility review.

## Port: `ChargeService`

```
charge(req: ChargeRequest) -> ChargeResult
```

### ChargeRequest
| field | type | notes | intent |
|---|---|---|---|
| order_id | OrderId (substrate) | which order this charges | R-005 |
| amount | Money (substrate) | integer minor units + currency; must equal order total | R-002, R-008 |
| domain_idempotency_key | IdempotencyKey (substrate) | THE attempt identity; gateway derives the Stripe Idempotency-Key from it (ADR-009) | R-007.1 |
| payment_token | StripeToken/PaymentMethodRef | tokenized card ref — never raw PAN (ADR-011) | R-014 |

### ChargeResult (three outcomes, not a boolean — ADR-007)
| outcome | meaning | caller obligation |
|---|---|---|
| `CHARGED{charge_id, amount}` | a new charge was created now | record + drive order→PAID via webhook reconciliation |
| `ALREADY_CHARGED{charge_id}` | this exact attempt was already charged (idempotent no-op; Stripe or our key deduped) | treat as success, do NOT charge again |
| `REFUSED_FOR_SAFETY{reason}` | outcome ambiguous; system refused rather than risk a second charge (T-3) | safe-reject the submit; never retry into a charge |
| `FAILED{reason, retriable}` | genuine charge failure (declined etc.) | leave order not-PAID; no money taken |

### Contract invariants (load-bearing)
- **CI-1 (R-007.1):** Two `charge()` calls with the *same* `domain_idempotency_key`
  produce **at most one** `CHARGED`; the second returns `ALREADY_CHARGED`.
- **CI-2 (ADR-009):** gateway passes a Stripe Idempotency-Key deterministically
  derived from `domain_idempotency_key`.
- **CI-3 (R-008):** `CHARGED` is never returned without a durable charge record
  existing first (intent recorded before the Stripe call — ADR-012 constraint 4).
- **CI-4 (T-3):** when gateway cannot determine whether a prior charge exists, it
  returns `REFUSED_FOR_SAFETY`, never a fresh `CHARGED`.

### Provisional / open (for planning-L3 to pressure-test)
- Whether the *authoritative* success is signalled here (sync) or only via the
  webhook channel (ADR-005) — the sync result may be provisional and the
  `orders` transition driven only by webhooks. **Flagged: intake/gateway/webhooks
  L3s must reconcile this at the compatibility review.**
- Whether `domain_idempotency_key` is minted by intake or supplied by the client.
