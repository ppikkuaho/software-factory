# ADR-005 — Webhook-aware design; async outcomes reconciled, not sync-only

- **Status:** decided
- **Intent IDs:** R-006, R-007.2, R-005
- **C4 altitude:** system

## Decision
The authoritative payment outcome is reconciled from **Stripe webhooks**
(asynchronous confirmation), not solely from the synchronous charge-API
response. `payments/webhooks` owns this reconciliation; the synchronous response
from `payments/gateway` is treated as provisional.

## Rationale
R-006 (L1-derived) holds that "never double-charge" forces a webhook-aware
design: the synchronous API response can be lost (timeout) while the charge
succeeded, which is exactly the retry double-charge risk. The durable truth of
"did this charge succeed" is the webhook event. R-007.2 only exists because
Stripe delivers those events at-least-once.

## Status caveat (carried to L1)
R-006 is **L1-derived and reflect-back PENDING**. If the user rejects webhook
reconciliation, this ADR and the entire `payments/webhooks` area are reopened.
The concept currently assumes confirmation.

## Consequences
- Two outcome channels (sync provisional + async authoritative) must agree;
  reconciliation logic (R-008) handles disagreement.
- The order→paid transition is driven by the authoritative (webhook) channel.
