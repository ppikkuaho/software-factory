---
artifact: frozen-interface-contract (L2 compatibility-review output → LOCK)
scope: Payments slice (intake · gateway · webhooks) + substrate ports consumed + orders touchpoint
status: FROZEN — these are the contracts the execution-L3/L4/L5 build against. Read-only to executors.
supersedes: contracts/payments-charge.md (PROVISIONAL), the "provisional/open" bullets in contracts/substrate-ports.md and contracts/orders-touchpoint.md
authored_by: L2 Project Architect (compatibility review of L2/plan/area-payments.md)
review_inputs: L2/plan/area-payments.md (planning-L3 design + 5 renegotiations), skeleton/FINDING.md, contracts/*, decisions/001-012
intent_ids: R-002, R-005, R-006, R-007.1, R-007.2, R-007.3, R-008, R-014, T-3
created: 2026-06-02
---

# Compatibility Review + Interface Lock — Payments Slice

This is the **L2 compatibility-review** output (PROJECT-PLANNING Phase 4, "L2
reviews them as a set" → "Lock the interfaces"). The planning-L3 for the Payments
area submitted `plan/area-payments.md` with 5 proposed renegotiations (3 from the
skeleton + 2 sibling-area ripples). L2 reviews the area design as a set against the
other areas' contracts, accepts/counters the renegotiations, resolves the
cross-area ripples, and **freezes** the result.

Only **one** payments area was designed in this dry-run; the other areas
(substrate, orders) exist only as provisional contracts, not as submitted
planning-L3 designs. The compatibility review is therefore **partial** — see
SHORTCOMING-LOCK-1. Where a ripple lands on an un-designed area, L2 records the
**required** contract change as a constraint that the (future) substrate/orders
planning-L3 inherits; it is locked from payments' side but **unconfirmed from the
counterparty's side**. Those are flagged PROVISIONALLY-LOCKED below.

---

## Part A — Compatibility review findings

### A.1 Does the renegotiated interface ripple to Orders? — YES, two ripples.

- **Ripple O-1 (mark_paid driver):** the design resolves the orders-contract open
  bullet ("called by intake (sync) or only by webhooks") in favor of
  **webhooks-only** (D-P5). This is *compatible* with the orders contract as
  written (OI-1 idempotent transition already supports redelivery-driven calls);
  it removes an ambiguity rather than breaking anything. **Resolved, no orders
  contract change needed.** ACCEPTED.
- **Ripple O-2 (mark_failed reversibility, §7 #5):** D-P6 has intake call
  `mark_failed` synchronously on a genuine decline, but C-W3 requires a late
  success webhook to be able to drive the order to PAID afterward. The orders
  contract models `READY → PAID | FAILED` with **no `FAILED → PAID` edge** and
  `mark_failed(order_id, reason) -> void`. This is a **genuine conflict**: the
  payments design needs `mark_failed` to be non-terminal, the orders contract
  makes it terminal. **This ripples into Orders and Orders has not been designed.**
  See LOCK decision L-7 (counter) and SHORTCOMING-LOCK-2.

### A.2 Does it ripple to the Substrate? — YES, two ripples.

- **Ripple S-1 (purchase keyspace atomicity, §7 #4a):** R-007.3 leans on SI-1
  (atomic claim) being extended to a new `purchase:` keyspace. The substrate
  contract names `req:`/`evt:` keyspaces explicitly and says claim is atomic, but
  does **not** name `purchase:`. SI-1's guarantee is keyspace-agnostic as written
  ("two concurrent `claim(k)` cannot both get FRESH"), so extending it to
  `purchase:` is **additive and compatible** — but it must be made explicit so the
  substrate L3 builds the `purchase:` namespace with the same unique constraint.
  PROVISIONALLY-LOCKED (substrate L3 must confirm).
- **Ripple S-2 (ChargeRecord host + TTL, §7 #4b):** D-P3 places `ChargeRecord`
  ownership in **payments**, not substrate — so the substrate only needs to (a)
  confirm the store supports TTL/reaping for `req:`/`evt:` keys (D-P4), and (b)
  NOT host ChargeRecord. The design's §7 #4b ("the store or a sibling table must
  host ChargeRecord") **contradicts** D-P3 ("ChargeRecord owned by payments").
  This is an **internal contradiction in the area design** — see
  SHORTCOMING-LOCK-3. L2 resolves it: **ChargeRecord is payments-owned** (L-3);
  the substrate ripple reduces to TTL-support confirmation only.

### A.3 Gaps — work no area claims.

- **GAP-1 (dead-letter / orphan-webhook path, RISK-2):** the design requires a
  webhook for an unknown `intent_id` to "park/alert, never auto-PAID" but says
  "no upstream doc specifies one." No area owns the dead-letter sink. L2 assigns
  it to **payments/webhooks** as part of the Reconciler scope (it is the component
  that detects the orphan), and freezes a minimal contract for it (L-6). Flagged
  because the *destination* of a parked event (ops queue? table?) is still
  unspecified — SHORTCOMING-LOCK-4.
- **GAP-2 (TTL-vs-Stripe-key misalignment, RISK-3):** the 24h Stripe idempotency
  key expiry vs the 7-day store TTL leaves a 24h–7day window where only our store
  guards R-007.1. This is not a missing *owner* but a missing *decision*: it is an
  accepted residual risk, but no doc records the acceptance. L2 records it as an
  explicit accepted-risk in L-5; it is NOT closed, it is consciously tolerated.
- **GAP-3 (concurrency never validated, RISK-8):** R-007.3 has zero end-to-end
  validation (skeleton couldn't run it). This is a *test/sequencing* gap, not an
  interface gap — it does not block the lock, but the acceptance tests (Part 2)
  must encode the requirement so the build is held to it against a real store.

### A.4 Conflicting decisions across areas / against the concept.

- **CONFLICT-1:** §7 #4b vs D-P3 (ChargeRecord home) — resolved in A.2/L-3.
- **CONFLICT-2:** D-P6 (sync mark_failed) vs orders `FAILED` terminality —
  resolved in A.1/L-7.
- No conflict found between the renegotiated `ChargeResult` and the substrate or
  the concept: the additions (`ACCEPTED_PENDING`, `intent_id`) are additive and
  consistent with ADR-005 (sync provisional) and ADR-007 (three outcomes).

### A.5 Does the combination still serve the concept?

Yes, with one **load-bearing externally-visible consequence that L2 cannot
unilaterally accept:** D-P5/RISK-5 — the synchronous `submit` caller never gets a
"paid" answer, only "pending." The concept (project.md §0) promised "submitted and
results in exactly one charge … and exactly one order→paid transition" but did not
say the caller learns of PAID synchronously. This is consistent with the concept
but is a product-visible behavior change. **L2 freezes the interface as async-only
but escalates RISK-5 to L1** as a reflect-back item (it cannot be closed at L2).
The whole webhooks branch also rests on R-006/R-008 being reflect-back-PENDING
(RISK-7) — the lock is therefore **conditional on reflect-back** (see Status).

---

## Part B — The LOCKED interfaces

> Edit policy: **FROZEN.** Execution-L3/L4/L5 build against exactly these shapes.
> Changes require a new numbered decision record + a re-lock, not an in-place edit.
> Types in CAPS are tagged-union variants. Field types reference substrate value
> types (`Money`, `OrderId`, `ChargeId`, `PaymentIntentId`, `IdempotencyKey`,
> `EventId`) unchanged from contracts/substrate-ports.md.

### L-1 — `ChargeService` (gateway exposes; intake consumes) — LOCKED

```
charge(req: ChargeRequest) -> ChargeResult
```

**ChargeRequest** (unchanged from provisional):
| field | type | intent |
|---|---|---|
| order_id | OrderId | R-005 |
| amount | Money | R-002, R-008 |
| domain_idempotency_key | IdempotencyKey | R-007.1 |
| payment_token | PaymentMethodRef (token; never PAN) | R-014 |

**ChargeResult** (RENEGOTIATION #1 ACCEPTED — `ACCEPTED_PENDING` added, `intent_id`
threaded onto every positive outcome):
| outcome | fields | meaning | caller obligation |
|---|---|---|---|
| `ACCEPTED_PENDING` | `{intent_id: PaymentIntentId, charge_id: ChargeId}` | Stripe PaymentIntent created and is `processing`/`requires_action`; NOT yet confirmed | record ChargeRecord; await webhook; return SubmitResult.ACCEPTED_PENDING |
| `ALREADY_CHARGED` | `{intent_id: PaymentIntentId, charge_id: ChargeId}` | this exact attempt was already created (our key or Stripe key deduped) | treat as success no-op; do NOT charge again |
| `REFUSED_FOR_SAFETY` | `{reason: str}` | outcome ambiguous; refused rather than risk a 2nd charge (T-3) | safe-reject; never retry into a charge |
| `FAILED` | `{reason: str, retriable: bool}` | genuine decline; no money taken | leave order not-PAID; intake may mark_failed (provisional) |

> **Lock note:** the provisional `CHARGED{charge_id, amount}` outcome is
> **removed** and replaced by `ACCEPTED_PENDING` as the sole positive
> create-outcome. Rationale: ADR-005 makes the sync result provisional *by
> construction*, so there is no honest "charged-and-confirmed-now" sync outcome —
> confirmation is always the webhook's. Keeping a `CHARGED` that means "confirmed"
> would be a lie at the sync boundary (the skeleton's exact forced overload). A
> charge is only ever CONFIRMED asynchronously. `intent_id` is **mandatory** on
> both positive outcomes — it is the only bridge to the async leg.

**Locked invariants (carry forward CI-1..CI-4, restated against the new enum):**
- **CI-1 (R-007.1):** two `charge()` with the same `domain_idempotency_key` →
  at most one `ACCEPTED_PENDING`; the second returns `ALREADY_CHARGED` with the
  **same** `intent_id`.
- **CI-2 (ADR-009):** Stripe Idempotency-Key is a deterministic function of
  `domain_idempotency_key` (same attempt → same Stripe key).
- **CI-3 (R-008/ADR-012.4):** no positive outcome is returned without a durable
  ChargeRecord(PENDING) existing first (intake's write-point, L-3).
- **CI-4 (T-3):** when the gateway cannot determine whether a prior charge
  exists, it returns `REFUSED_FOR_SAFETY`, never a positive outcome.

### L-2 — `SubmitResult` (intake exposes to the external caller) — LOCKED (NEW, renegotiation #3)

```
submit(order_id, amount, domain_idempotency_key, payment_token) -> SubmitResult
```
| outcome | fields | meaning |
|---|---|---|
| `ACCEPTED_PENDING` | `{intent_id: PaymentIntentId}` | fresh attempt accepted; charge in flight; **order is still READY, not PAID** — caller must poll order state or await notification |
| `ALREADY_ACCEPTED` | `{intent_id: PaymentIntentId}` | replay of an already-accepted attempt (same request key); idempotent no-op |
| `REFUSED` | `{reason: str}` | concurrency loser (R-007.3) or safety refusal (T-3) |
| `FAILED` | `{reason: str}` | genuine decline (gateway FAILED) |

> **Lock note (RISK-5, escalated):** there is deliberately **no** synchronous
> `PAID` outcome. PAID is reachable only via the webhook leg. This is the
> async-only consequence L2 escalates to L1.

### L-3 — `ChargeRecord` port (NEW, renegotiation #2) — LOCKED, **owner = payments area**

The durable spine that bridges the synchronous attempt to the asynchronous Stripe
confirmation. **Owned by the payments area** (D-P3 ACCEPTED; §7 #4b's
"substrate-or-sibling-table" alternative is REJECTED — see CONFLICT-1). It is a
payments-area-level shared component: **intake writes, webhooks reads/updates.**

```
record(intent_id: PaymentIntentId, order_id: OrderId, charge_id: ChargeId,
       amount: Money, idem_key: IdempotencyKey) -> void          # intake, pre-confirm
lookup_by_intent(intent_id: PaymentIntentId) -> ChargeRecord | NONE   # webhooks
mark_confirmed(intent_id: PaymentIntentId) -> void                # webhooks, on confirm
```

**State machine (monotonic):** `PENDING → CONFIRMED | FAILED | REFUSED`.
- **Write-point (intake):** writes `PENDING` *before* calling `ChargeService`
  (CI-3 / ADR-012.4). The `charge_id` and `intent_id` may be back-filled on the
  positive `ChargeResult` (intake writes the record keyed by the attempt, then
  updates with the intent_id the gateway returns — see SHORTCOMING-LOCK-5 on the
  ordering ambiguity).
- **Write-point (webhooks):** `mark_confirmed(intent_id)` on a verified, deduped,
  successful event; this is what makes the order→PAID drive possible.
- **RR-1:** `lookup_by_intent` returning `NONE` for an event's `intent_id` MUST
  route to the dead-letter path (L-6), never fabricate a record (R-008).
- **RR-2:** state transitions are idempotent and monotonic (a CONFIRMED record
  re-confirmed is a no-op) — supports webhook redelivery + crash recovery
  (RISK-1).

### L-4 — `IdempotencyStore` (substrate exposes) — LOCKED (provisionally, S-1/S-2)

Unchanged claim/commit shape from contracts/substrate-ports.md. **Locked
additions** (PROVISIONALLY — substrate L3 must confirm):
- **Keyspaces locked to three namespaces:** `req:` (R-007.1), `evt:` (R-007.2),
  `purchase:` (R-007.3). SI-1 atomic-claim guarantee **applies to all three**
  (S-1).
- **TTL:** the store MUST support per-key TTL or scheduled reaping; `req:` and
  `evt:` keys retained ≥ 7 days (D-P4). If the store cannot, the substrate L3
  escalates (this co-constrains ADR-010). (S-2)
- ChargeRecord is **NOT** hosted by the substrate (CONFLICT-1 resolution).

### L-5 — Idempotency key policy — LOCKED

- **Request key (`req:`):** **client-supplied** `Idempotency-Key` header (D-P1
  ACCEPTED). Intake validates presence/format; does not mint. Client contract:
  the client MUST reuse the same key on retry of the same logical attempt.
  Escalated as a client-contract note (it pushes a correctness obligation onto the
  caller).
- **Purchase-identity key (`purchase:`):** deterministic hash of
  `(order_id, amount.minor_units, amount.currency)` (D-P2 ACCEPTED). MUST NOT
  include the request key. Two concurrent submits for the same purchase derive the
  identical `purchase:` key and collide on one atomic claim (R-007.3).
- **GAP-2 / RISK-3 accepted residual:** request keys retained 7 days; Stripe's own
  idempotency key expires at 24h. A retry in the 24h–7day window is guarded by
  **our store alone**. This is a **consciously accepted residual R-007.1 gap**,
  not a defect — recorded here so it is a decision, not an accident. Escalated to
  L1 alongside RISK-5 (it is a product-risk acceptance, not an L2-closable one).

### L-6 — Dead-letter / orphan-webhook path — LOCKED (GAP-1 resolution)

Assigned to **payments/webhooks (Reconciler)**.
- When `lookup_by_intent(intent_id)` returns `NONE` for a verified, deduped event:
  the event is **parked** (recorded to a dead-letter sink + an alert emitted via
  EventLog) and the order is **never** transitioned to PAID (R-008 / RR-1).
- The dead-letter *sink mechanism* (table vs ops queue vs EventLog stream) is
  **left to the substrate/ops layer and is UNSPECIFIED upstream** —
  SHORTCOMING-LOCK-4. The *behavioral* contract (park, alert, never-PAID) is
  locked; the *destination* is not.

### L-7 — `OrderPaymentState.mark_failed` — COUNTER (Ripple O-2)

The payments design (D-P6) needs `mark_failed` to be non-terminal so a late
success webhook can still reach PAID (C-W3). The orders contract makes FAILED
terminal. **L2 cannot unilaterally rewrite the orders contract from inside the
payments review** (orders is a sibling area with its own future planning-L3).
L2's resolution, locked from payments' side:
- **mark_failed is locked as PROVISIONAL/reversible** for the payments slice:
  `mark_failed(order_id, reason)` marks the order FAILED but **does not close it
  to a later PAID transition**; a subsequent `mark_paid` for the same order with a
  confirmed charge MUST succeed (adds a `FAILED → PAID` reconciliation edge).
- This is a **required orders-contract change** that the orders planning-L3
  inherits as a constraint. **PROVISIONALLY-LOCKED** (orders L3 must confirm /
  may counter). Until orders confirms, **WS-P3's sync-mark_failed (D-P6) is
  blocked on this edge existing** — recorded as a build precondition.

---

## Part C — Lock verdict

- **LOCKED (no counterparty needed):** L-1, L-2, L-3, L-5 (within payments),
  L-6 (behavioral contract).
- **PROVISIONALLY-LOCKED (counterparty area must confirm):** L-4 (substrate:
  purchase keyspace + TTL), L-7 (orders: FAILED→PAID edge), L-6 destination
  (substrate/ops sink).
- **ESCALATED TO L1 (cannot close at L2):** RISK-5 (async-only submit), GAP-2
  (24h–7day R-007.1 residual), and the reflect-back-PENDING status of R-006/R-008
  (RISK-7) — the entire webhooks branch and the R-008 invariant are conditional on
  these being confirmed.

**Net:** the lock is **conditional, not clean.** The three additive renegotiations
(#1/#2/#3) lock cleanly within payments. The two sibling ripples (#4/#5) lock only
from payments' side and create constraints two un-designed areas must accept. This
is the honest state: in a full run, the substrate and orders planning-L3s would be
in this same compatibility round and would confirm or counter *before* freeze. Here
they are absent, so the freeze is partial. See SHORTCOMING-LOCK-1.

The frozen contracts above are what the execution build (and the Part-2 acceptance
tests) are written against.
