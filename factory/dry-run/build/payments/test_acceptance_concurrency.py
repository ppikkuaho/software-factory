"""R-007.3 (c) — two CONCURRENT submits for the SAME purchase → charged ONCE.

FROZEN, read-only to the executor. Authored from L2/decisions/interfaces-locked.md
(L-5 purchase-key, L-4 SI-1 atomic claim across the `purchase:` keyspace, ADR-006)
before any build code exists.

This is the SCARIEST must-never-fail case (RISK-8): the walking skeleton could NOT
exercise it (single-threaded in-memory store). The executor MUST wire a genuinely
atomic store for this test — a real DB unique constraint or a correctly-locked
structure. A green run against a single-threaded fake is TEST THEATER and fails
the gate even if the assertions pass (see test_concurrency_used_real_store).

Mechanism under test: two concurrent submits that derive the SAME purchase-identity
key (deterministic hash of order_id+amount+currency, D-P2) collide on ONE atomic
claim. Exactly one wins (ACCEPTED_PENDING); the loser is REFUSED (T-3 safe-failure,
never a second charge).
"""
import threading
import unittest

from acceptance_harness import make_system, tag, ORDER_A, AMOUNT, TOKEN


class ConcurrentSubmitChargedOnce(unittest.TestCase):
    def setUp(self):
        # Request a system wired with a REAL concurrent/atomic store. The executor
        # must honor this flag; if it cannot provide one, build_system must raise
        # (escalate), NOT silently hand back a single-threaded fake.
        self.sys = make_system(concurrent_store=True)

    def test_concurrency_used_real_store(self):
        # Fidelity guard: the wired store must declare itself concurrency-safe.
        # The executor exposes system.store.is_atomic_concurrent == True only for a
        # store that actually enforces an atomic unique claim across threads.
        self.assertTrue(
            getattr(self.sys.store, "is_atomic_concurrent", False),
            "concurrency test must run against a real atomic store, not a "
            "single-threaded fake (RISK-8 / SI-1). A green bar on a fake is theater.",
        )

    def test_two_concurrent_same_purchase_one_charge(self):
        results = []
        barrier = threading.Barrier(2)

        def submit(req_key):
            # DIFFERENT request keys (so the request guard does NOT dedup them) but
            # SAME order+amount (so the PURCHASE guard must). This isolates R-007.3
            # from R-007.1 — exactly ADR-004's three-distinct-guards point.
            barrier.wait()  # maximize the race window
            results.append(self.sys.submit(ORDER_A, AMOUNT, req_key, TOKEN))

        t1 = threading.Thread(target=submit, args=("req-key-1",))
        t2 = threading.Thread(target=submit, args=("req-key-2",))
        t1.start(); t2.start(); t1.join(); t2.join()

        tags = sorted(tag(r) for r in results)
        # Exactly one winner, exactly one safe-refusal — never two acceptances.
        self.assertEqual(
            tags, ["ACCEPTED_PENDING", "REFUSED"],
            f"expected one ACCEPTED_PENDING and one REFUSED, got {tags} "
            "(R-007.3: concurrent submit must charge once, loser refused)",
        )
        self.assertEqual(
            self.sys.stripe.intent_count(), 1,
            "two concurrent submits for the same purchase must create exactly one "
            "Stripe PaymentIntent (R-007.3)",
        )
        self.assertEqual(self.sys.order_state(ORDER_A), "READY")  # still async to PAID

    def test_loser_is_refused_not_failed_not_charged(self):
        # T-3 direction: the contention loser is REFUSED (safe), never FAILED-as-if-
        # -declined and never silently charged. Re-run the race and inspect the loser.
        results = []
        barrier = threading.Barrier(2)

        def submit(req_key):
            barrier.wait()
            results.append(self.sys.submit(ORDER_A, AMOUNT, req_key, TOKEN))

        ts = [threading.Thread(target=submit, args=(f"k{i}",)) for i in range(2)]
        for t in ts: t.start()
        for t in ts: t.join()

        losers = [r for r in results if tag(r) == "REFUSED"]
        self.assertEqual(len(losers), 1)
        # A REFUSED loser must not have created a charge.
        self.assertEqual(self.sys.stripe.intent_count(), 1)


if __name__ == "__main__":
    unittest.main()
