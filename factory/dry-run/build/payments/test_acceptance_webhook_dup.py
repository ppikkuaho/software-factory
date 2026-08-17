"""R-007.2 (b) — duplicate Stripe webhook (redelivery) → order PAID ONCE.

FROZEN, read-only to the executor. Authored from L2/decisions/interfaces-locked.md
(C-W1, OI-1, RR-2, L-3 mark_confirmed) before any build code exists.

The obligation: Stripe delivers webhooks at-least-once. The same event id delivered
twice must produce exactly ONE order->PAID transition; the second is an idempotent
no-op. A forged/unsigned event is rejected before any state change.
"""
import unittest

from acceptance_harness import make_system, tag, field, ORDER_A, AMOUNT, TOKEN


class WebhookDedup(unittest.TestCase):
    def setUp(self):
        self.sys = make_system()
        # Drive a charge into the in-flight (ACCEPTED_PENDING) state first.
        r = self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
        self.assertEqual(tag(r), "ACCEPTED_PENDING")
        self.intent_id = field(r, "intent_id")
        self.assertEqual(self.sys.order_state(ORDER_A), "READY")  # not PAID until webhook

    def _success_event(self, event_id):
        # The executor's webhook double must accept this shape (or adapt it). The
        # contract fields the Reconciler needs: event id + intent id + status.
        return {
            "id": event_id,
            "type": "payment_intent.succeeded",
            "intent_id": self.intent_id,
            "status": "succeeded",
        }

    def test_single_success_event_transitions_to_paid(self):
        res = self.sys.deliver_webhook(self._success_event("evt-1"),
                                       signature="valid")
        self.assertEqual(tag(res), "CONFIRMED")
        self.assertEqual(self.sys.order_state(ORDER_A), "PAID")

    def test_duplicate_event_id_paid_exactly_once(self):
        first = self.sys.deliver_webhook(self._success_event("evt-1"),
                                         signature="valid")
        self.assertEqual(tag(first), "CONFIRMED")
        # Redelivery of the SAME event id N times → DUPLICATE_NOOP, still PAID once.
        for _ in range(3):
            dup = self.sys.deliver_webhook(self._success_event("evt-1"),
                                           signature="valid")
            self.assertEqual(tag(dup), "DUPLICATE_NOOP")
        self.assertEqual(self.sys.order_state(ORDER_A), "PAID")
        # And exactly one underlying charge confirmation — no second charge ever.
        self.assertEqual(self.sys.stripe.intent_count(), 1)

    def test_forged_or_unsigned_event_rejected_before_state_change(self):
        # C-W2: signature verified before acting.
        res = self.sys.deliver_webhook(self._success_event("evt-forged"),
                                       signature="INVALID")
        self.assertEqual(tag(res), "REJECTED_SIGNATURE")
        self.assertEqual(self.sys.order_state(ORDER_A), "READY")  # unchanged

    def test_two_distinct_events_same_intent_still_paid_once(self):
        # Defense in depth: even if Stripe sent two DIFFERENT event ids for the same
        # intent, the ChargeRecord state machine (RR-2 monotonic CONFIRMED) keeps the
        # order PAID exactly once. The second distinct event re-confirms idempotently.
        self.sys.deliver_webhook(self._success_event("evt-1"), signature="valid")
        second = self.sys.deliver_webhook(self._success_event("evt-2"),
                                          signature="valid")
        # Either deduped at event level or absorbed by the monotonic record — never
        # a second transition.
        self.assertIn(tag(second), ("CONFIRMED", "DUPLICATE_NOOP"))
        self.assertEqual(self.sys.order_state(ORDER_A), "PAID")


if __name__ == "__main__":
    unittest.main()
