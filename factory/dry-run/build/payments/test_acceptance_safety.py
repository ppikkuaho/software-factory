"""Safe-failure (T-3) + no-divergence (R-008) supporting invariants.

FROZEN, read-only to the executor. Authored from L2/decisions/interfaces-locked.md
(CI-3 record-before-charge, CI-4 ambiguous->refuse, L-6 orphan/dead-letter,
RR-1 no-fabricated-record, L-7 mark_failed-reversible, L-2 async-only) before any
build code exists.

These are the cross-cutting safety obligations that make the three R-007 mechanisms
trustworthy: ambiguity refuses rather than double-charges, an order never becomes
PAID without a backing confirmed charge, an orphan webhook parks instead of
fabricating, and a sync FAILED is reversible by a late success webhook.
"""
import unittest

from acceptance_harness import make_system, tag, field, ORDER_A, AMOUNT, TOKEN


class SafeFailureAndNoDivergence(unittest.TestCase):
    def setUp(self):
        self.sys = make_system()

    # --- CI-3: record-before-charge ---------------------------------------
    def test_charge_record_pending_exists_before_any_positive_outcome(self):
        # CI-3 / ADR-012.4: no positive ChargeResult without a prior PENDING record.
        # The executor exposes system.charge_records.lookup_by_intent(intent_id).
        r = self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
        self.assertEqual(tag(r), "ACCEPTED_PENDING")
        rec = self.sys.charge_records.lookup_by_intent(field(r, "intent_id"))
        self.assertIsNotNone(rec, "a ChargeRecord must exist for an accepted attempt")
        self.assertIn(tag(rec) if hasattr(rec, "tag") else rec.get("state"),
                      ("PENDING", "CONFIRMED"))

    # --- CI-4 / T-3: ambiguous gateway outcome refuses, never charges twice ---
    def test_ambiguous_gateway_outcome_refused_for_safety(self):
        # Drive the gateway double to an ambiguous error (cannot tell if a prior
        # charge exists). The contract demands REFUSED, never a positive outcome.
        self.sys.stripe.set_ambiguous(True)
        r = self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
        self.assertEqual(tag(r), "REFUSED")
        self.assertEqual(self.sys.order_state(ORDER_A), "READY")

    # --- L-2 / RISK-5: there is no synchronous PAID ------------------------
    def test_submit_never_returns_synchronous_paid(self):
        r = self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
        self.assertNotEqual(tag(r), "PAID")
        self.assertIn(tag(r), ("ACCEPTED_PENDING", "ALREADY_ACCEPTED",
                               "REFUSED", "FAILED"))

    # --- L-6 / RR-1: orphan webhook parks, never fabricates a PAID ----------
    def test_orphan_webhook_parked_never_paid(self):
        orphan = {"id": "evt-orphan", "type": "payment_intent.succeeded",
                  "intent_id": "pi_never_seen", "status": "succeeded"}
        res = self.sys.deliver_webhook(orphan, signature="valid")
        self.assertEqual(tag(res), "PARKED_ORPHAN")
        # No order anywhere transitions to PAID off an unbacked event (R-008).
        self.assertEqual(self.sys.order_state(ORDER_A), "READY")

    # --- L-7 / C-W3: sync FAILED is reversible by a late success webhook ----
    def test_sync_failed_then_late_success_webhook_reaches_paid(self):
        # Genuine decline on the sync leg -> order FAILED (D-P6 sync mark_failed).
        self.sys.stripe.set_next_intent_status("failed")
        r = self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
        self.assertEqual(tag(r), "FAILED")
        self.assertEqual(self.sys.order_state(ORDER_A), "FAILED")
        # Stripe race: a LATER webhook reports the same intent actually succeeded.
        intent_id = field(r, "intent_id") if "intent_id" in _safe_fields(r) else \
            self.sys.last_intent_id(ORDER_A)
        late = {"id": "evt-late", "type": "payment_intent.succeeded",
                "intent_id": intent_id, "status": "succeeded"}
        res = self.sys.deliver_webhook(late, signature="valid")
        # Webhook is authoritative (ADR-005); reconcile FAILED -> PAID (L-7 edge).
        self.assertEqual(tag(res), "CONFIRMED")
        self.assertEqual(self.sys.order_state(ORDER_A), "PAID")
        self.assertEqual(self.sys.stripe.intent_count(), 1)  # never a second charge

    # --- R-008: a PAID order always traces to a confirmed charge -----------
    def test_paid_always_traces_to_confirmed_charge(self):
        r = self.sys.submit(ORDER_A, AMOUNT, "req-key-1", TOKEN)
        intent_id = field(r, "intent_id")
        self.sys.deliver_webhook(
            {"id": "evt-1", "type": "payment_intent.succeeded",
             "intent_id": intent_id, "status": "succeeded"}, signature="valid")
        self.assertEqual(self.sys.order_state(ORDER_A), "PAID")
        rec = self.sys.charge_records.lookup_by_intent(intent_id)
        state = tag(rec) if hasattr(rec, "tag") else rec.get("state")
        self.assertEqual(state, "CONFIRMED",
                         "a PAID order must trace to a CONFIRMED charge record (R-008)")


def _safe_fields(result):
    if hasattr(result, "__dict__"):
        return set(vars(result).keys())
    if isinstance(result, dict):
        return set(result.keys())
    return set()


if __name__ == "__main__":
    unittest.main()
