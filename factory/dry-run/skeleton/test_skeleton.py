"""Walking-skeleton end-to-end test (stdlib unittest only).

THROWAWAY SPIKE. Proves ONE thin charge path connects end-to-end across:
  substrate -> payments/intake -> payments/gateway -> (async webhook) ->
  payments/webhooks -> orders touchpoint -> PAID

It also exercises the must-never-fail R-007 sub-cases at skeleton depth:
  (a) network/app retry -> charged once   (R-007.1)
  (b) duplicate gateway webhook -> once    (R-007.2)
It deliberately does NOT cover (c) concurrent submit (R-007.3) — the in-memory
single-threaded skeleton store cannot honestly exercise SI-1 atomicity. That gap
is a FINDING, not an omission to hide.

Run: python3 -m unittest -v   (from the skeleton/ dir)
"""

import unittest
from substrate import Money, IdempotencyStore, EventLog, Clock
from orders import OrderPaymentState, READY, PAID
from gateway import FakeStripe, StripeChargeAdapter, Charged
from webhooks import WebhookReceiver, ChargeLedger
from intake import SubmitHandler, SubmitRequest


def wire():
    """Assemble the whole slice. NOTE: the test harness is forced to decide WHO
    populates the ChargeLedger (intake? gateway? a new component?). The contracts
    are silent; the skeleton wires intake to record it, which is an architectural
    choice no doc sanctions. (Finding.)"""
    idem = IdempotencyStore()
    events = EventLog()
    clock = Clock()
    orders = OrderPaymentState()
    stripe = FakeStripe()
    gateway = StripeChargeAdapter(stripe)
    ledger = ChargeLedger()
    intake = SubmitHandler(idem, events, gateway, orders, clock)
    webhook = WebhookReceiver(idem, events, orders, ledger, clock)
    return locals()


class WalkingSkeleton(unittest.TestCase):

    def _submit_and_record(self, w, req):
        """Helper that does what no contract assigns an owner to: after intake
        charges, somebody must write the intent_id->order mapping into the ledger
        so the async webhook can reconcile. We reach into gateway state to get the
        intent_id, which proves the seam is missing from the contract."""
        before = len(w["stripe"].webhooks_to_deliver)
        res = w["intake"].submit(req)
        # Pull the freshly created intent (only if a real charge happened).
        if res.status in ("ACCEPTED_PENDING",) and len(w["stripe"].webhooks_to_deliver) > before:
            evt = w["stripe"].webhooks_to_deliver[-1]
            # charge_id is res.detail for ACCEPTED_PENDING
            w["ledger"].record(evt["intent_id"], req.order_id, res.detail,
                               req.amount, req.idempotency_key)
        return res

    def test_single_charge_path_e2e(self):
        w = wire()
        amt = Money(4999, "EUR")
        w["orders"].seed("order-1", amt)

        # 1. submit (sync): charge created, order NOT yet paid (ADR-005 async).
        res = self._submit_and_record(w, SubmitRequest("order-1", amt, "idem-1", "tok_visa"))
        self.assertEqual(res.status, "ACCEPTED_PENDING")
        self.assertEqual(w["orders"].get_state("order-1"), READY,
                         "order must NOT be PAID on sync path; webhook is authoritative")

        # 2. async: Stripe delivers the confirmation webhook -> order PAID.
        evt = w["stripe"].webhooks_to_deliver[0]
        out = w["webhook"].receive(evt)
        self.assertEqual(out, "Transitioned")
        self.assertEqual(w["orders"].get_state("order-1"), PAID)

    def test_retry_charges_once(self):
        # R-007.1 (a): app/network retry of the SAME submit -> one charge.
        w = wire()
        amt = Money(4999, "EUR")
        w["orders"].seed("order-2", amt)

        r1 = self._submit_and_record(w, SubmitRequest("order-2", amt, "idem-2", "tok_visa"))
        r2 = self._submit_and_record(w, SubmitRequest("order-2", amt, "idem-2", "tok_visa"))
        self.assertEqual(r1.status, "ACCEPTED_PENDING")
        self.assertEqual(r2.status, "ALREADY_ACCEPTED")
        # exactly one stripe intent created
        self.assertEqual(len(w["stripe"].webhooks_to_deliver), 1)

    def test_duplicate_webhook_transitions_once(self):
        # R-007.2 (b): Stripe redelivers the SAME event id -> one transition.
        w = wire()
        amt = Money(4999, "EUR")
        w["orders"].seed("order-3", amt)
        self._submit_and_record(w, SubmitRequest("order-3", amt, "idem-3", "tok_visa"))

        evt = w["stripe"].webhooks_to_deliver[0]
        out1 = w["webhook"].receive(evt)
        out2 = w["webhook"].receive(evt)  # redelivery, identical event id
        self.assertEqual(out1, "Transitioned")
        self.assertEqual(out2, "DUPLICATE_NOOP")
        self.assertEqual(w["orders"].get_state("order-3"), PAID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
