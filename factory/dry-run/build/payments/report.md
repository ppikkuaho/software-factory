# Payments Build Report

## What Was Done

- Added `payments_impl.py` exposing `build_system(**kwargs)` and the required `PaymentsSystem` seam.
- Implemented `submit(order_id, amount, idem_key, token)` with:
  - client request-key replay in the `req:` keyspace;
  - deterministic purchase identity from `(order_id, minor_units, currency)`;
  - atomic `purchase:` claim before gateway charge creation;
  - ChargeRecord `PENDING` creation before gateway call;
  - webhook-only `PAID` transition;
  - synchronous reversible `FAILED` for genuine gateway decline;
  - `REFUSED` safe-failure for ambiguous gateway outcome.
- Implemented `deliver_webhook(raw_event, signature)` with signature-first rejection, `evt:` event deduplication, orphan parking, and monotonic confirmation of charge records.
- Implemented the in-memory fake Stripe gateway with deterministic Stripe idempotency-key dedup, intent counting, status override, ambiguous outcome switch, and token-only observation through `seen_tokens`.
- Implemented `charge_records.lookup_by_intent(intent_id)`, `last_intent_id(order_id)`, and default `READY`/`FAILED`/`PAID` order state behavior.

## How Verified

The requested bare `python` command is not available in this shell:

```text
$ python -m unittest
zsh:1: command not found: python
```

The available Python 3 interpreter is:

```text
$ command -v python3
/opt/homebrew/bin/python3

$ python3 --version
Python 3.14.3
```

Full frozen suite result:

```text
$ python3 -m unittest
.................
----------------------------------------------------------------------
Ran 17 tests in 0.006s

OK
```

Verbose per-file result:

```text
$ python3 -m unittest -v
test_concurrency_used_real_store (test_acceptance_concurrency.ConcurrentSubmitChargedOnce.test_concurrency_used_real_store) ... ok
test_loser_is_refused_not_failed_not_charged (test_acceptance_concurrency.ConcurrentSubmitChargedOnce.test_loser_is_refused_not_failed_not_charged) ... ok
test_two_concurrent_same_purchase_one_charge (test_acceptance_concurrency.ConcurrentSubmitChargedOnce.test_two_concurrent_same_purchase_one_charge) ... ok
test_distinct_request_keys_are_distinct_attempts (test_acceptance_retry.RetryChargedOnce.test_distinct_request_keys_are_distinct_attempts) ... ok
test_first_submit_is_accepted_pending_not_paid (test_acceptance_retry.RetryChargedOnce.test_first_submit_is_accepted_pending_not_paid) ... ok
test_retry_same_key_returns_already_accepted_one_intent (test_acceptance_retry.RetryChargedOnce.test_retry_same_key_returns_already_accepted_one_intent) ... ok
test_token_only_no_raw_pan_reaches_gateway (test_acceptance_retry.RetryChargedOnce.test_token_only_no_raw_pan_reaches_gateway) ... ok
test_ambiguous_gateway_outcome_refused_for_safety (test_acceptance_safety.SafeFailureAndNoDivergence.test_ambiguous_gateway_outcome_refused_for_safety) ... ok
test_charge_record_pending_exists_before_any_positive_outcome (test_acceptance_safety.SafeFailureAndNoDivergence.test_charge_record_pending_exists_before_any_positive_outcome) ... ok
test_orphan_webhook_parked_never_paid (test_acceptance_safety.SafeFailureAndNoDivergence.test_orphan_webhook_parked_never_paid) ... ok
test_paid_always_traces_to_confirmed_charge (test_acceptance_safety.SafeFailureAndNoDivergence.test_paid_always_traces_to_confirmed_charge) ... ok
test_submit_never_returns_synchronous_paid (test_acceptance_safety.SafeFailureAndNoDivergence.test_submit_never_returns_synchronous_paid) ... ok
test_sync_failed_then_late_success_webhook_reaches_paid (test_acceptance_safety.SafeFailureAndNoDivergence.test_sync_failed_then_late_success_webhook_reaches_paid) ... ok
test_duplicate_event_id_paid_exactly_once (test_acceptance_webhook_dup.WebhookDedup.test_duplicate_event_id_paid_exactly_once) ... ok
test_forged_or_unsigned_event_rejected_before_state_change (test_acceptance_webhook_dup.WebhookDedup.test_forged_or_unsigned_event_rejected_before_state_change) ... ok
test_single_success_event_transitions_to_paid (test_acceptance_webhook_dup.WebhookDedup.test_single_success_event_transitions_to_paid) ... ok
test_two_distinct_events_same_intent_still_paid_once (test_acceptance_webhook_dup.WebhookDedup.test_two_distinct_events_same_intent_still_paid_once) ... ok

----------------------------------------------------------------------
Ran 17 tests in 0.004s

OK
```

Per-file pass counts:

- `test_acceptance_concurrency.py`: 3/3 passed.
- `test_acceptance_retry.py`: 4/4 passed.
- `test_acceptance_safety.py`: 6/6 passed.
- `test_acceptance_webhook_dup.py`: 4/4 passed.

Total: 17 passed, 0 failed, 0 errors under `/opt/homebrew/bin/python3`.

## Atomic Store Mechanism

`AtomicIdempotencyStore` is the store wired by `build_system()`. Its `claim(key, value)` method guards the complete check-and-set with a single `threading.Lock` critical section:

```text
with self._lock:
    if key in self._claims:
        return False, self._claims[key]
    self._claims[key] = value
    return True, value
```

That same atomic claim is used for `req:`, `purchase:`, and `evt:` keyspaces. Because the purchase claim is protected by this lock, two concurrent `claim("purchase:...")` calls cannot both observe the key as fresh. The store exposes `is_atomic_concurrent = True` because it genuinely enforces this atomic unique claim across threads.

## Concerns / Open Questions

- The shell has no bare `python` executable, so the exact requested command cannot run here. The same unittest suite passes under the available Python 3 interpreter at `/opt/homebrew/bin/python3`.
- The orphan webhook sink is represented only by the contracted `PARKED_ORPHAN` result; no durable dead-letter destination is specified or implemented in this in-memory slice.
- The in-memory store has no TTL behavior. The frozen tests and brief for this build do not require TTL.
- Best-effort bus nudge was not delivered. `bus status` and `bus whoami` failed with `EPERM` opening the Codex Desktop bus control log, and `bus send payments ...` failed with `no_such_participant`.

## Escalations

None. I found no contradiction between the frozen tests, `brief.md`, and `acceptance.md`.
