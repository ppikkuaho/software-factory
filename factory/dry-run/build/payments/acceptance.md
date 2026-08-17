---
artifact: frozen-acceptance-tests (D26) — authored from spec BEFORE build
scope: Payments slice build tasks (intake · gateway · webhooks) — the R-007 must-never-fail
authored_by: L4-tester lateral (M51) — NOT the L4 coordinator, NOT the L5 executor
authored_from: L2/decisions/interfaces-locked.md (the FROZEN contract) + the area design L2/plan/area-payments.md §2 acceptance targets + the briefs' D26 constraints
edit_policy: FROZEN — READ-ONLY to the executor (L5). The executor makes these pass; it MUST NOT edit them. Changes require re-authoring at planning time, not at build time.
runtime: Python 3, stdlib `unittest` only (no third-party deps)
intent_ids: R-007.1, R-007.2, R-007.3, R-008, T-3, R-014
created: 2026-06-02
---

# Frozen Acceptance Tests — Payments Slice

These tests are authored **from the locked spec, before any build code exists**
(the anti-theater temporal rule, M51/D26). They encode the R-007 must-never-fail
obligations (a/b/c) and the supporting invariants. The executor (L5) implements
the contract surface so these pass; it may **read** these files and must **not**
edit them.

## How the executor plugs in (the seam the tests bind to)

The tests import a single module, `payments_impl`, which the executor authors. It
must expose a **factory** `build_system()` that returns a wired `PaymentsSystem`
exposing exactly the locked ports:

- `system.submit(order_id, amount, idem_key, token) -> SubmitResult`  (L-2)
- `system.deliver_webhook(raw_event, signature) -> WebhookResult`     (L-6 / C-W*)
- `system.order_state(order_id) -> "READY" | "PAID" | "FAILED"`       (orders touchpoint)
- `system.stripe` — a test double exposing `intent_count()` (how many distinct
  Stripe PaymentIntents were actually created) and `set_next_intent_status(...)`
  / `set_ambiguous(...)` to drive gateway-side outcomes deterministically.

Outcome tag strings the tests assert against (from L-1/L-2):
`ACCEPTED_PENDING`, `ALREADY_ACCEPTED`, `REFUSED`, `FAILED` (SubmitResult);
`CONFIRMED`, `DUPLICATE_NOOP`, `REJECTED_SIGNATURE`, `PARKED_ORPHAN` (WebhookResult).

The factory MUST wire a **real durable store** for the concurrency test
(`test_acceptance_concurrency.py`) — an in-memory single-threaded fake cannot
honestly exercise SI-1 (this is exactly the skeleton's gap, RISK-8). The store
used in that test must enforce an **atomic unique claim** across threads (a real
DB unique constraint, or a correctly-locked in-process structure). If the executor
cannot provide one, it MUST escalate, not stub it — a passing concurrency test
against a single-threaded fake is test theater.

## Coverage map (test → obligation)

| Test file | Obligation | Locked source |
|---|---|---|
| `test_acceptance_retry.py` | **R-007.1 (a)** retry → charged once | CI-1, L-5 req-key, C-I1 |
| `test_acceptance_webhook_dup.py` | **R-007.2 (b)** duplicate webhook → once | C-W1, OI-1, RR-2 |
| `test_acceptance_concurrency.py` | **R-007.3 (c)** concurrent submit → once | ADR-006, L-5 purchase-key, SI-1 |
| `test_acceptance_safety.py` | **T-3 / R-008** safe-failure, async-only, orphan, mark_failed-reversible | CI-4, L-1, L-6, RR-1, L-7 |

## What "pass" means (the gate)

Fidelity-dominant (D27): the build is faithful iff every test in all four files
passes **and** the concurrency test ran against a real atomic store (asserted by
`test_concurrency_used_real_store`). A green run on a single-threaded fake is a
**fidelity failure** even though the assertions pass — the gate rubric below makes
the reviewer check the store, not just the green bar.

## Gate rubric (read by the L5+ reviewer, against this frozen file)

1. All four acceptance files pass under `python -m unittest`. (fidelity)
2. The concurrency store is genuinely concurrent (reviewer inspects the wiring,
   not just the assertion). (fidelity — the load-bearing one)
3. No positive outcome is ever returned without a prior ChargeRecord(PENDING)
   (CI-3) — reviewer confirms by reading the orchestration path. (fidelity)
4. `submit` never returns a synchronous `PAID` tag (L-2 / RISK-5). (fidelity)
5. PAN/token: no raw PAN ever reaches the gateway double (R-014). (quality)
6. Tentative findings are first-class (D29) — a reviewer unsure whether the store
   is truly atomic says so with confidence level, does not collapse to pass.

The tests themselves are in this directory: `test_acceptance_retry.py`,
`test_acceptance_webhook_dup.py`, `test_acceptance_concurrency.py`,
`test_acceptance_safety.py`, plus the shared `acceptance_harness.py` (helpers only;
no implementation).
