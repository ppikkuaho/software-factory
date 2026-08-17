---
artifact: task-brief (L4 → L5) — cross-runtime task contract (E32)
task_address: payments#exec   (workspace node: build/payments/)
reviewer_seat: payments#review (L5+, Opus/Claude Code — different runtime, M52)
executor_runtime: Codex harness / GPT-5.5 (per runtime-and-model-map.md assignment table)
authored_by: L4 Workstream Coordinator (Opus/Claude Code)
edit_policy: IMMUTABLE once sent. brief.md is pointer-not-payload; raw upstream intent is referenced, not copied.
frozen_anchor: build/payments/acceptance.md + the 4 test_acceptance_*.py files (READ-ONLY to you)
locked_spec: ../../L2/decisions/interfaces-locked.md  (the FROZEN contract — L-1..L-7, CI-*, SI-1, OI-1, RR-*)
created: 2026-06-02
---

# Task Brief — Implement the Payments slice (R-007 exactly-once)

> **Read order (do this first):** (1) the four `test_acceptance_*.py` files and
> `acceptance.md` in THIS directory — they are the contract; (2) this brief for
> the decisions already made for you; (3) `interfaces-locked.md` (referenced, pull
> on demand) only where this brief points you at a specific L-n / CI-n clause.
> **The tests are the definition of done. This prose is context.**

## §0. Runtime-neutral contract vs. adapter (E32 — what is and isn't in this brief)

This brief is the **neutral contract**: it is identical regardless of which runtime
executes it. It carries identity (address), spec, frozen acceptance anchor,
interface contracts, constraints, workspace location, reporting expectations.

The **adapter** (injected at spawn, NOT written here) owns the three
runtime-specific things: your **tool manifest** (the Codex tool surface), the
**harness invocation**, and your **output format**. If anything in this brief
seems to assume a Claude-specific tool or affordance, that is a brief defect —
escalate it (see §7); do not silently adapt.

## §1. Identity & workspace

- **You are** the executor seat at node `payments` (`build/payments/`).
- **Work only inside** `build/payments/`. Produce `payments_impl.py` (and any
  internal modules you choose, e.g. `payments_impl/` package) here. Append a line
  to the project `log.md` when done. Touch nothing else.
- An independent reviewer (L5+, `payments#review`, a *different* model+runtime)
  will read the frozen tests and your output and either accept or bounce. Build to
  the spec; do not try to anticipate or pre-empt the review.

## §2. The task (one sentence)

Author `payments_impl.build_system(**kwargs)` returning a wired `PaymentsSystem`
that makes **all four frozen acceptance files pass** under `python -m unittest`,
faithfully implementing R-007 (never double-charge) across its three independent
vectors plus the supporting safety invariants.

## §3. The seam you implement (from acceptance.md — non-negotiable surface)

`build_system(**kwargs)` returns an object exposing **exactly**:

- `submit(order_id, amount, idem_key, token) -> SubmitResult`
- `deliver_webhook(raw_event, signature) -> WebhookResult`
- `order_state(order_id) -> "READY" | "PAID" | "FAILED"`
- `stripe` — the in-memory fake gateway double, exposing:
  - `intent_count() -> int` (distinct Stripe PaymentIntents actually created)
  - `set_next_intent_status(status)` — drive next charge outcome (e.g. `"failed"`)
  - `set_ambiguous(True)` — drive the ambiguous-error path (CI-4)
  - `seen_tokens` — list of tokens the gateway saw (R-014 assertion reads this)
- `store` — the IdempotencyStore; for the concurrency test it MUST expose
  `is_atomic_concurrent == True` (see §5.C, the load-bearing one)
- `charge_records` — exposes `lookup_by_intent(intent_id) -> record | NONE`,
  where a record exposes `.state` or `["state"]` ∈ {`PENDING`,`CONFIRMED`,`FAILED`,`REFUSED`}
- `last_intent_id(order_id) -> intent_id` — used by the safety test to recover the
  intent id of a sync-FAILED attempt (the FAILED SubmitResult need not carry it).

**Result variant tags** (the tests read `.tag` or `["tag"]`):
- SubmitResult: `ACCEPTED_PENDING{intent_id}` · `ALREADY_ACCEPTED{intent_id}` ·
  `REFUSED{reason}` · `FAILED{reason}`
- WebhookResult: `CONFIRMED` · `DUPLICATE_NOOP` · `REJECTED_SIGNATURE` · `PARKED_ORPHAN`

You may model the tagged union however you like (dataclass with `.tag`, or dict
with `["tag"]`); `acceptance_harness.tag()`/`field()` accept either. Spec-
faithfulness first, elegance second.

## §4. Decisions already made for you (decision-complete — do NOT re-derive these)

Every item below is a decision you would otherwise have to invent. GPT-5.5 will not
backfill these with good architecture, and it must not: they are locked upstream.

- **D1 — Request key is CLIENT-SUPPLIED** (`idem_key` arg). Intake dedups on it in
  the `req:` keyspace. Same key replayed → `ALREADY_ACCEPTED` with the **same
  intent_id**; exactly one Stripe intent. (L-5 req key; CI-1; test_retry)
- **D2 — Purchase-identity key = deterministic hash of
  `(order_id, amount.minor_units, amount.currency)`**, in the `purchase:` keyspace.
  It MUST NOT include `idem_key`. Two concurrent submits with DIFFERENT `idem_key`
  but SAME `(order_id, amount)` collide on ONE atomic claim. Winner →
  `ACCEPTED_PENDING`; loser → `REFUSED`. (L-5 purchase key; ADR-006; test_concurrency)
- **D3 — Amount is `(minor_units:int, currency:str)`** — a 2-tuple, exactly as the
  harness passes `AMOUNT = (4999, "USD")`. Integer minor units (R-002). No floats.
- **D4 — Record-before-charge (CI-3 / ADR-012.4):** intake writes a ChargeRecord in
  state `PENDING` **before** calling the gateway. No positive outcome may be
  returned without that PENDING record existing first. Back-fill `intent_id` onto
  the record after the gateway returns it. (test_safety record-before-charge)
- **D5 — Order→PAID is driven by the WEBHOOK ONLY (D-P5 / ADR-005).** `submit`
  NEVER returns a synchronous PAID and NEVER transitions the order to PAID. After a
  successful `submit` the order stays `READY`. PAID happens only in
  `deliver_webhook`. (L-2 lock note; test_retry, test_webhook_dup, test_safety)
- **D6 — On gateway `FAILED` (genuine decline), intake marks the order `FAILED`
  synchronously** (D-P6). `mark_failed` is **NON-TERMINAL/REVERSIBLE** (L-7
  counter): a later success webhook for that same intent reconciles `FAILED → PAID`.
  Drive the decline via `stripe.set_next_intent_status("failed")`. (test_safety
  sync_failed_then_late_success)
- **D7 — Ambiguous gateway outcome → `REFUSED` (CI-4 / T-3).** When
  `stripe.set_ambiguous(True)`, the gateway cannot tell if a prior charge exists;
  the adapter returns `REFUSED_FOR_SAFETY` and intake surfaces `REFUSED`. Never a
  positive outcome, never a retry-into-charge. Order stays `READY`. (test_safety)
- **D8 — Stripe-side idempotency key is a deterministic function of the domain
  key (CI-2 / ADR-009).** Same domain key → same Stripe key → the fake gateway
  returns the SAME PaymentIntent (it does NOT create a second). This is the
  defense-in-depth fourth layer beneath the request guard.
- **D9 — Webhook dedup (R-007.2):** `deliver_webhook` claims on the Stripe
  `event["id"]` in the `evt:` keyspace. First valid success event → `CONFIRMED`
  + order PAID + `mark_confirmed` on the record. Same event id again →
  `DUPLICATE_NOOP`, order stays PAID, no second charge. (C-W1; OI-1; RR-2; test_webhook_dup)
- **D10 — Two DISTINCT event ids for the SAME intent (test_webhook_dup last case):**
  the ChargeRecord state machine is **monotonic** (RR-2): once CONFIRMED, a second
  distinct event re-confirms idempotently → return `CONFIRMED` or `DUPLICATE_NOOP`,
  order PAID exactly once. Either tag is accepted; one transition only.
- **D11 — Signature verification BEFORE any state change (C-W2):** `signature ==
  "valid"` passes; anything else → `REJECTED_SIGNATURE`, no state change. The fake
  verifier is a literal string compare against `"valid"` — that is the whole
  contract the test exercises; do not invent crypto.
- **D12 — Orphan webhook (RR-1 / L-6):** a verified, signed, deduped event whose
  `intent_id` has NO ChargeRecord → `PARKED_ORPHAN`. NEVER fabricate a record,
  NEVER transition any order to PAID. (test_safety orphan)
- **D13 — Webhook event shape** is a dict: `{"id", "type", "intent_id", "status"}`.
  The reconciler needs `id` (dedup), `intent_id` (record lookup), `status`
  (`"succeeded"` for the success path). `signature` is a separate positional/kw arg
  to `deliver_webhook`, not a field of the event.
- **D14 — Tokenized cards only (R-014 / C-G3):** the gateway records `token` into
  `stripe.seen_tokens`; a raw PAN must never appear there. The tests only ever pass
  `TOKEN`; you do not need PAN-stripping logic, but you MUST NOT echo anything other
  than the token the caller supplied into the gateway.

## §5. Acceptance anchors (test → obligation → what wires it)

| File | Obligation | Your wiring |
|---|---|---|
| `test_acceptance_retry.py` | R-007.1 retry→once | `req:` claim (D1), Stripe-key dedup (D8), ACCEPTED_PENDING not PAID (D5) |
| `test_acceptance_webhook_dup.py` | R-007.2 dup webhook→once | `evt:` claim (D9), sig verify (D11), monotonic record (D10) |
| `test_acceptance_concurrency.py` | **R-007.3 concurrent→once** | atomic `purchase:` claim across threads (D2) + `store.is_atomic_concurrent` |
| `test_acceptance_safety.py` | T-3/R-008 safety net | record-before-charge (D4), ambiguous→refuse (D7), orphan park (D12), reversible FAILED (D6), PAID⇒CONFIRMED (R-008) |

### §5.C — THE LOAD-BEARING ONE (read this twice)

`test_acceptance_concurrency.py` calls `make_system(concurrent_store=True)` and
then **asserts `system.store.is_atomic_concurrent is True`** *before* racing two
threads. This is the anti-theater fidelity guard (RISK-8 / SI-1):

- You MUST provide a store that enforces an **atomic unique claim across threads**
  — a `threading.Lock`-guarded check-and-set, or equivalent, such that two
  concurrent `claim(purchase_key)` calls cannot both get FRESH. Only such a store
  may set `is_atomic_concurrent = True`.
- A single-threaded dict that merely *passes the assertions by luck of scheduling*
  is **TEST THEATER and fails the gate** even if green. Do not set the flag on a
  store that isn't genuinely atomic.
- **If you cannot provide a genuinely atomic store, `build_system(concurrent_store=
  True)` MUST raise (escalate) — do NOT silently return a single-threaded fake.**
  (This is the one place the spec orders you to fail loudly rather than guess.)

## §6. Constraints

- **Python 3, stdlib only.** No third-party deps. `unittest`, `threading`,
  `hashlib`, `dataclasses`, `enum` are all fine.
- **Do NOT edit any `test_acceptance_*.py`, `acceptance_harness.py`, or
  `acceptance.md`.** They are frozen and read-only to you. You make them pass; you
  do not move the goalposts. (D26)
- The fake gateway is in-memory and deterministic — no network, no sleeps, no real
  Stripe. It models exactly what the tests drive: status override, ambiguous flag,
  intent counting, idempotency-key dedup, seen_tokens.
- Spec-faithfulness is the #1 self-verification axis. Run `python -m unittest` from
  `build/payments/` and confirm all four files pass before you report.

## §7. Escalate-don't-decide (the relief valve — USE IT)

You are a literal executor. Where this brief or the frozen tests are ambiguous or
contradictory, **do not fill the gap with a reasonable-sounding default — stop and
surface it.** Specifically escalate (do not decide) if:

- the frozen tests and this brief disagree on a tag, field, or behavior;
- a test imports a method/attribute this brief did not name on the seam (§3);
- you cannot provide a genuinely atomic concurrent store (§5.C) — raise, don't stub;
- a test appears to require a synchronous PAID (it must not — if you read one that
  way, you've misread; flag it);
- the runtime adapter exposes a tool surface that doesn't match a brief reference (§0).

Raise it by writing the blocker into `report.md` under "Escalations" with: what you
found, why it blocks, and the options you see — then proceed on the unblocked parts.

## §8. Reporting expectations (your primary deliverable alongside the code)

Fill `build/payments/report.md`:
- **What was done** — components built, the seam wired.
- **How verified — specifically.** Paste the `python -m unittest` summary (counts,
  per-file). State explicitly that the concurrency store is genuinely atomic and
  HOW (the mechanism), since that is the load-bearing fidelity claim.
- **Concerns / open questions** — there are always some. (E.g. the orphan
  dead-letter *destination* is unspecified upstream — L-6 / SHORTCOMING-LOCK-4; you
  implement the behavior `PARKED_ORPHAN`, not a real sink.)
- **Escalations** — anything from §7.

Truth lives in the docs you write into this node (`payments_impl.*`, `report.md`,
test output). Post a bus nudge to `payments` parent when done; the parent re-reads
the node (best-effort transport, F33).
