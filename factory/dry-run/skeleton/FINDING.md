---
artifact: walking-skeleton-finding
spike: Walking Skeleton (ungated de-risking spike, Architect-owned, THROWAWAY)
status: ran green; provisional interface PARTIALLY HOLDS — two seams need renegotiation
ran: 2026-06-02
build_order_ref: project.md §6 step 2
covers_r007: (a) retry-once R-007.1, (b) duplicate-webhook-once R-007.2 — at skeleton depth
does_not_cover: (c) concurrent-submit R-007.3 (in-memory single-threaded store cannot honestly exercise SI-1)
---

# Walking Skeleton — Finding

## What was built
Thinnest throwaway end-to-end thread (stdlib Python, `unittest`, no deps):
`substrate → payments/intake → payments/gateway → (async Stripe webhook) →
payments/webhooks → orders touchpoint → PAID`. 3 tests, all green.

## Does the provisional interface HOLD? — Mostly, with two real renegotiations

### HOLDS
- `ChargeService.charge() -> {CHARGED|ALREADY_CHARGED|REFUSED_FOR_SAFETY|FAILED}`
  three-outcome shape is workable for the sync leg. CI-1 (same domain key → one
  CHARGED) and CI-2 (derived Stripe key) express cleanly.
- `IdempotencyStore.claim/commit` carries both the request-idem (R-007.1) and the
  webhook-event-dedup (R-007.2) flows. Both single-threaded cases pass.
- `OrderPaymentState.mark_paid` idempotent transition (OI-1) carries webhook
  redelivery: second identical event → `DUPLICATE_NOOP`, order PAID once.

### NEEDS RENEGOTIATION (the spike's payload)

**1. The sync `ChargeResult` cannot express "accepted, confirmation pending."**
ADR-005 makes the webhook authoritative and the sync response provisional — but
the provisional `ChargeResult` has only `CHARGED` as its positive outcome, and
its stated meaning is "a new charge was created now." For a Stripe PaymentIntent
that is `processing` (not yet `succeeded`), the gateway must return something that
is neither "charged-and-confirmed" nor "failed." The skeleton was *forced* to
overload `CHARGED` to mean "intent created, not yet confirmed," which contradicts
the contract's own gloss. **Renegotiation:** `ChargeResult` needs an explicit
`ACCEPTED_PENDING{intent_id}` outcome (or `CHARGED` must be redefined as
provisional-by-construction). The contract's own "Provisional/open" bullet flags
this ("sync vs webhook authoritative") but leaves the OUTCOME ENUM unchanged,
which is the actual hole. `intent_id` is also absent from `Charged` yet is the
only key that ties the sync charge to the async webhook (see #2).

**2. No port maps `intent_id → (order_id, charge_id, idem_key)`.** The webhook
event carries Stripe's `intent_id`; `orders.mark_paid` demands
`(order_id, charge_id, amount, idempotency_key)`. NOTHING in `contracts/` bridges
the two. The skeleton had to invent a `ChargeLedger` and could not even decide
which area owns it — the test harness, not any component, populates it (see
test `wire()` / `_submit_and_record`). **Renegotiation:** the substrate or a
payments-owned "charge record" port must be a first-class contract, with a
defined owner and a defined write-point (ties to ADR-012, which decided the
*property* of no-divergence but deferred the *record* that makes reconciliation
possible — the deferral dropped the lookup port, not just the write ordering).

**3. `ACCEPTED_PENDING` has no home in the submit contract.** intake's
`SubmitResult` is undefined in any provisional doc; the skeleton invented it.
Because the order is authoritatively PAID only by the webhook, the synchronous
caller of `submit` gets back "pending," and the submit interface must say so.
This is the intake-side mirror of #1.

## Must-never-fail status at skeleton depth
- R-007.1 retry-once: PASS (request-key claim dedups; one Stripe intent).
- R-007.2 duplicate-webhook-once: PASS (event-id claim dedups; one transition).
- R-007.3 concurrent-submit: **NOT EXERCISED** — see Shortcomings. The skeleton
  can mimic it but cannot prove SI-1 atomicity in-memory; an honest concurrency
  spike needs a real store with a unique constraint. This is the *scariest* R-007
  case (project.md §6 risk note) and the skeleton CANNOT de-risk it. The spike
  that matters most is the one the walking skeleton can't run.

## Throwaway status
This code is throwaway per the "Walking Skeleton" section ("ungated, early
de-risking spike … informs the plan"). It MUST NOT be promoted into the gated
build. No doc states a discard/quarantine step — recorded as a shortcoming.
