"""R-007.1 (a) — network/app retry of the SAME logical request → charged ONCE.

FROZEN, read-only to the executor. Authored from L2/decisions/interfaces-locked.md
(L-1 CI-1, L-2, L-5 request-key policy) before any build code exists.

The obligation: a client that retries the same submit with the SAME
client-supplied request idempotency key must produce exactly one Stripe
PaymentIntent and one accepted attempt; every replay returns ALREADY_ACCEPTED with
the same intent_id.
"""
import unittest

from acceptance_harness import make_system, tag, field, ORDER_A, AMOUNT, TOKEN


class RetryChargedOnce(unittest.TestCase):
    def setUp(self):
        self.sys = make_system()

    def test_first_submit_is_accepted_pending_not_paid(self):
        # L-2: a fresh attempt is ACCEPTED_PENDING; the order is NOT synchronously PAID.
        r = self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
        self.assertEqual(tag(r), "ACCEPTED_PENDING")
        self.assertIsNotNone(field(r, "intent_id"))
        self.assertEqual(self.sys.order_state(ORDER_A), "READY")  # RISK-5: not PAID yet

    def test_retry_same_key_returns_already_accepted_one_intent(self):
        # CI-1 / C-I1: replay with the SAME request key N times → one intent total.
        first = self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
        intent_id = field(first, "intent_id")
        for _ in range(4):
            r = self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
            self.assertEqual(tag(r), "ALREADY_ACCEPTED")
            # Same attempt → same intent_id bridges back to the one in-flight charge.
            self.assertEqual(field(r, "intent_id"), intent_id)
        self.assertEqual(
            self.sys.stripe.intent_count(), 1,
            "retry must not create a second Stripe PaymentIntent (R-007.1)",
        )

    def test_distinct_request_keys_are_distinct_attempts(self):
        # Negative control: two genuinely different request keys are NOT deduped by
        # the request guard. (The purchase guard, R-007.3, is a separate mechanism —
        # ADR-004's three-guards rule. This test pins that req-key dedup alone does
        # not collapse distinct keys.) NOTE: if order_id+amount are identical the
        # purchase guard MAY still refuse the second — see test_acceptance_concurrency.
        self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
        r2 = self.sys.submit(ORDER_A, AMOUNT, "req-key-2", TOKEN)
        # Either it is a fresh attempt (if purchase guard scope differs) or a
        # safety REFUSED by the purchase guard — but it must NEVER be silently
        # treated as the SAME accepted attempt as req-key-1 (that would be the
        # collapse-two-guards-into-one bug, D-P1 rationale).
        self.assertIn(tag(r2), ("ACCEPTED_PENDING", "REFUSED"))

    def test_token_only_no_raw_pan_reaches_gateway(self):
        # R-014 / C-G3: gateway operates on tokens only.
        self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
        self.assertNotIn(
            "4242424242424242", repr(getattr(self.sys.stripe, "seen_tokens", [])),
            "raw PAN must never reach the Stripe gateway (R-014)",
        )


if __name__ == "__main__":
    unittest.main()
