"""payments/webhooks stub — Stripe event dedup + reconcile (drives order->PAID).

THROWAWAY SPIKE. Receives a Stripe webhook event, dedups on event id (R-007.2 /
C-W1) via IdempotencyStore, and drives the AUTHORITATIVE order->PAID transition
(ADR-005 / C-W4). Signature verify (C-W2) is stubbed to "ok".

FINDING SURFACE: to call orders.mark_paid (OI-2 requires charge_id + amount +
idem_key) the webhook handler needs to map a Stripe intent_id -> (order_id,
charge_id, domain_idem_key). The provisional contracts provide NO port for that
lookup. The webhook event carries intent_id; orders.mark_paid wants order_id +
idempotency_key. Nothing in contracts/ bridges intent_id -> order_id. The
skeleton is forced to invent a ChargeLedger lookup that no contract defines.
"""

from substrate import IdempotencyStore, EventLog, DomainEvent, FRESH, Duplicate, Clock
from orders import OrderPaymentState, TRANSITIONED, ALREADY_PAID, REJECTED


class ChargeLedger:
    """INVENTED BY SKELETON — not in any provisional contract.
    Maps intent_id -> (order_id, charge_id, amount, domain_idem_key) so the
    webhook can reconcile. Who owns this? Unspecified. (Finding.)"""
    def __init__(self):
        self._by_intent = {}

    def record(self, intent_id, order_id, charge_id, amount, domain_idem_key):
        self._by_intent[intent_id] = (order_id, charge_id, amount, domain_idem_key)

    def lookup(self, intent_id):
        return self._by_intent.get(intent_id)


class WebhookReceiver:
    def __init__(self, idem: IdempotencyStore, events: EventLog,
                 orders: OrderPaymentState, ledger: ChargeLedger, clock: Clock):
        self.idem = idem
        self.events = events
        self.orders = orders
        self.ledger = ledger
        self.clock = clock

    def verify_signature(self, raw_event) -> bool:
        return True  # C-W2 stubbed

    def receive(self, event: dict):
        if not self.verify_signature(event):
            return "REJECTED_SIGNATURE"

        # C-W1 / R-007.2: dedup on Stripe event id. NOTE: same IdempotencyStore as
        # request keys -> needs namespacing the port does not express. Skeleton
        # prefixes manually.
        ns_key = "evt:" + event["event_id"]
        claim = self.idem.claim(ns_key)
        if isinstance(claim, Duplicate):
            return "DUPLICATE_NOOP"

        if event["type"] != "payment_intent.succeeded":
            self.idem.commit(ns_key, "ignored")
            return "IGNORED"

        rec = self.ledger.lookup(event["intent_id"])
        if rec is None:
            # confirmed charge with no local record -> C-W5/R-008 divergence risk.
            self.idem.commit(ns_key, "orphan")
            return "ORPHAN_CHARGE"  # contract has no defined handling for this

        order_id, charge_id, amount, domain_idem_key = rec
        outcome = self.orders.mark_paid(order_id, charge_id, amount, domain_idem_key)
        self.events.append(DomainEvent(
            "webhook-consumed",
            {"event_id": event["event_id"], "outcome": outcome},
            self.clock.now(),
        ))
        self.idem.commit(ns_key, outcome)
        return outcome
