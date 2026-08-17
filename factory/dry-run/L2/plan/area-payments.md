---
artifact: area-design (planning-L3 output)
area: payments (intake + gateway + webhooks)
level: L3 (planning-L3, temporary)
role_identity: payments area lead — idempotency/exactly-once specialist
status: design submitted for L2 compatibility review
intent_ids: R-002, R-005, R-006, R-007.1, R-007.2, R-007.3, R-008, R-014, T-3
input_concept: dry-run/L2/project.md
input_briefs: L2/briefs/payments-intake.md, payments-gateway.md, payments-webhooks.md
input_contracts: L2/contracts/payments-charge.md, substrate-ports.md, orders-touchpoint.md
input_skeleton: dry-run/skeleton/FINDING.md
adrs_inherited: 002,003,004,005,006,007,008,009,010,011,012
created: 2026-06-02
owner: planning-L3 (this area) — submitted to L2; becomes design.md for execution-L3
---

# Area Design — Payments (intake · gateway · webhooks)

This is the detailed design for the **Payments area** of the e-commerce slice. It
covers three sibling sub-areas that L2 carved (`payments/intake`,
`payments/gateway`, `payments/webhooks`). The **substrate** and the **orders
touchpoint** are *sibling areas owned by other planning-L3s* — I consume their
provisional ports and renegotiate two of them (§7), but I do not design them.

> **Scope note on the task framing vs. L2's carving.** My spawn task named the
> submodules "PaymentIntent core, GatewayAdapter, IdempotencyStore,
> WebhookReconciler." L2's `project.md` carves at a coarser grain
> (intake / gateway / webhooks sub-areas) and puts `IdempotencyStore` in the
> **substrate**, not in payments. I follow L2's carving (it is the authority;
> §0.1 of role.md forbids me re-carving the architecture) and map the task's
> names onto it: PaymentIntent-core ≙ intake's ChargeOrchestrator + the new
> ChargeRecord, GatewayAdapter ≙ gateway, WebhookReconciler ≙ webhooks,
> IdempotencyStore ≙ the substrate port I consume (NOT mine to design). The
> mismatch between the task framing and the concept is logged as a shortcoming
> (SC-1) because it forced an interpretation call that a real planning-L3 would
> have had to escalate.

---

## 1. How the Payments area works (coherence before decomposition)

The area's whole job is **exactly-once charging** under three independent attack
vectors. The single organising idea: **every side-effecting step is gated by a
claim on the substrate `IdempotencyStore`, and the durable `ChargeRecord` is the
spine that ties the synchronous attempt to the asynchronous Stripe confirmation.**

One charge attempt flows like this:

```
 caller
   │ submit(order_id, amount, idem_key, token)
   ▼
[intake.SubmitHandler]
   │ 1. claim(request-key)            ── R-007.1 request idempotency
   │ 2. claim(purchase-identity-key)  ── R-007.3 concurrency uniqueness   *NEW key*
   │ 3. write ChargeRecord{PENDING}   ── ADR-012.4 record-before-charge    *NEW record*
   │ 4. charge(ChargeRequest)─────────────────────────┐
   │                                                   ▼
   │                                          [gateway.StripeChargeAdapter]
   │                                            derive Stripe idem-key (ADR-009)
   │                                            create PaymentIntent (async at Stripe)
   │                                            map → ChargeResult
   │ 5. on ACCEPTED_PENDING:                    ◄──────┘
   │    update ChargeRecord{intent_id}    *the lookup key that bridges to async*
   │    commit(request-key)
   │    return SubmitResult{ACCEPTED_PENDING, intent_id}
   ▼
 caller holds "pending"; order is STILL `READY`, not PAID
                              ⋮  (time passes; Stripe processes)
 Stripe ──webhook(event_id, intent_id, succeeded)──▶ [webhooks.WebhookReceiver]
                                                       verify signature (C-W2)
                                                       claim(event-id-key) ── R-007.2
                                                       lookup ChargeRecord by intent_id
                                                       OrderPaymentState.mark_paid(...) ── ADR-008
                                                       mark ChargeRecord{CONFIRMED}
                                                       order → PAID exactly once
```

The three R-007 mechanisms are **deliberately three different claims on the same
store**, exactly as ADR-004 demands — they are not one guard reused three times:

| R-007 sub-obligation | Mechanism | Keyspace | Owner submodule |
|---|---|---|---|
| R-007.1 retry → once | claim on **request idempotency key** | `req:` | intake |
| R-007.2 dup webhook → once | claim on **Stripe event id** | `evt:` | webhooks |
| R-007.3 concurrent submit → once | claim on **purchase-identity key** (durable unique) | `purchase:` | intake |

Defense-in-depth fourth layer: the **Stripe-side idempotency key** (ADR-009) sits
*underneath* R-007.1 inside the gateway, so even a bypass of intake's request
guard cannot create two Stripe PaymentIntents.

This is the coherent unit. The decomposition below falls out of it.

---

## 2. Workstreams

Five workstreams. WS-1 and WS-2 (the renegotiated contracts) gate everything
else and must be approved by L2 at the compatibility review before WS-3..5 build.

### WS-P1 — Charge contract & ChargeRecord port (the renegotiated interface)
- **Scope:** Produce the *frozen* interface artifacts this area builds against:
  the revised `ChargeService.charge()` contract with the new `ACCEPTED_PENDING`
  outcome and `intent_id` on the positive result (§7 renegotiation #1/#3), and
  the new **`ChargeRecord` port** (§7 renegotiation #2) — the
  `intent_id → (order_id, charge_id, amount, idem_key, state)` lookup that bridges
  sync→async. Define owner, write-points, and states (`PENDING → CONFIRMED |
  FAILED | REFUSED`). Define intake's `SubmitResult` shape.
- **Acceptance criteria:**
  - `ChargeResult` enum contains `CHARGED`(redefined provisional) **or**
    `ACCEPTED_PENDING{intent_id, charge_id}`, `ALREADY_CHARGED{charge_id}`,
    `REFUSED_FOR_SAFETY{reason}`, `FAILED{reason, retriable}` — and `intent_id`
    is present on every positive outcome.
  - `ChargeRecord` port specifies: `record(intent_id, order_id, charge_id, amount,
    idem_key) -> void`, `lookup_by_intent(intent_id) -> ChargeRecord | NONE`,
    `mark_confirmed(intent_id)`, with the **owner named** (decision D-P3 below)
    and the **write-point named** (intake writes PENDING pre-charge; webhooks
    writes CONFIRMED).
  - `SubmitResult` enum defined: `ACCEPTED_PENDING{intent_id}` /
    `ALREADY_ACCEPTED{intent_id}` / `REFUSED{reason}` / `FAILED{reason}`.
  - L2 has accepted the renegotiation (or countered) — this WS is **blocked on
    the compatibility review**.
- **Constraints:** must stay consistent with substrate `IdempotencyStore`,
  `OrderPaymentState.mark_paid` signature (needs `charge_id` + `amount` →
  `ChargeRecord` must carry both), ADR-005 (webhook authoritative), ADR-007
  (safe-failure outcomes), ADR-012 (record-before-charge).
- **Context needed:** all three payments briefs, all three contracts, the
  skeleton FINDING, ADR-005/007/009/012.

### WS-P2 — Idempotency keying & concurrency guard resolution (ADR-006)
- **Scope:** Resolve the deferred ADR-006 (concurrency mechanism) and the
  idempotency-key storage/TTL question (the §"deferred constraints" the task
  asks me to resolve). Define: (a) how the **request idempotency key** is
  obtained (minted by intake vs client-supplied — provisional contract leaves
  this open), its **format**, its **storage** (the substrate store), and its
  **TTL/retention**; (b) the **purchase-identity key** derivation for R-007.3 and
  its enforcement as a *durable uniqueness claim* (not check-then-act); (c) the
  keyspace namespacing (`req:` / `evt:` / `purchase:`) so the three never collide.
- **Acceptance criteria:**
  - Request-key origin decided and justified (D-P1).
  - Purchase-identity key derivation specified deterministically from
    `(order_id, amount, ...)` and documented (D-P2); two concurrent submits for
    the same purchase provably claim the same key.
  - Idempotency-key TTL/retention rule specified with reasoning (D-P4), and the
    interaction with replay-after-expiry called out (a replay after TTL must NOT
    silently become a second charge — see RISK-3).
  - The guard is expressible on the ADR-010 datastore (single relational DB w/
    atomic unique constraint); if not, escalation drafted.
- **Constraints:** ADR-006 (durable uniqueness, no check-then-act, loser refused),
  ADR-010 (datastore envelope, atomic unique constraint available), ADR-007
  (T-3 loser is REFUSED not charged), substrate SI-1 (claim atomic).
- **Context needed:** ADR-006, ADR-010, substrate-ports contract, intake brief.
- **Note:** the *generic* atomic-claim primitive is the **substrate L3's** to
  build (ADR-003/SI-1). This WS specifies the **keys and policy** payments
  layers on top — and feeds a renegotiation note back to the substrate L3 if the
  store's claim semantics are insufficient for the purchase keyspace.

### WS-P3 — intake: SubmitHandler + orchestration + record-before-charge
- **Scope:** Build `payments/intake`: `SubmitHandler` (request entry),
  `IdempotencyGuard` (request-key claim), `ConcurrencyGuard` (purchase-key claim),
  `ChargeOrchestrator` (write ChargeRecord PENDING → call ChargeService → update
  record → commit → emit events → return SubmitResult). Drives **no** order
  transition (ADR-005: that is webhooks' job — D-P5).
- **Acceptance criteria** (negative tests, authored by the L4-tester lateral at
  execution plan-time — not here; these are the *targets*):
  - Replay same request key ×N → one ACCEPTED_PENDING, rest ALREADY_ACCEPTED;
    exactly one Stripe intent.
  - Two concurrent submits same purchase → one ACCEPTED_PENDING, other REFUSED;
    exactly one charge (R-007.3).
  - Crash between ChargeRecord{PENDING} write and ChargeService return →
    reconciliation (webhook) reaches exactly one charge, no second (ADR-012.5).
  - Any ambiguous ChargeResult → REFUSED, never a retry-into-charge (C-I4/T-3).
  - intake never calls a local paid-flag write (C-I5).
- **Constraints:** C-I1..C-I5 from the intake brief; depends on WS-P1 (contract)
  and WS-P2 (keys) being frozen.
- **Context needed:** intake brief, frozen WS-P1/WS-P2 outputs, substrate ports.

### WS-P4 — gateway: StripeChargeAdapter
- **Scope:** Build `payments/gateway`: `StripeChargeAdapter` implementing the
  frozen `ChargeService`; `StripeIdempotencyMap` (deterministic Stripe key from
  domain key, ADR-009); `OutcomeMapper` (Stripe PaymentIntent status → revised
  `ChargeResult`, including the `processing` → `ACCEPTED_PENDING` mapping that the
  skeleton proved is missing). Tokenized cards only (ADR-011). Treat sync Stripe
  response as provisional (ADR-005/C-G5).
- **Acceptance criteria (targets):**
  - Same domain key → identical Stripe Idempotency-Key (determinism asserted).
  - Two charges same domain key → Stripe creates one PaymentIntent; adapter
    returns ALREADY_CHARGED on the second.
  - PaymentIntent `processing`/`requires_action` → `ACCEPTED_PENDING{intent_id}`
    (NOT a confirmed CHARGED) — the skeleton finding #1 fix.
  - Ambiguous Stripe error / "deduped intent we have no local record of" →
    REFUSED_FOR_SAFETY, no second charge (C-G2).
  - PAN never received/stored (C-G3); currency mismatch rejected (C-G4).
- **Constraints:** C-G1..C-G5; depends on WS-P1 frozen contract. **Spike**
  (per gateway brief): sandbox-Stripe spike validating idempotency-key behavior
  and the ambiguous-error→refuse path before contract freeze.
- **Context needed:** gateway brief, ADR-009/011/005, frozen WS-P1.

### WS-P5 — webhooks: WebhookReceiver + EventDedup + Reconciler
- **Scope:** Build `payments/webhooks`: `WebhookReceiver` (HTTP in + signature
  verify), `EventDedup` (claim on Stripe event-id, R-007.2), `Reconciler`
  (lookup ChargeRecord by `intent_id`, drive `OrderPaymentState.mark_paid`,
  mark ChargeRecord CONFIRMED, hold R-008 no-divergence). This submodule **owns
  the authoritative order→PAID transition** (D-P5).
- **Acceptance criteria (targets):**
  - Same event id ×2 → one transition, second DUPLICATE_NOOP, order PAID once
    (R-007.2).
  - Forged/unsigned event → rejected before any state change (C-W2).
  - Webhook arrives for an `intent_id` with no ChargeRecord → does NOT silently
    create one; resolves safe (logged/parked, never an unbacked PAID) — R-008
    direction (RISK-2).
  - Sync said FAILED but webhook confirms success → reconcile to one charge,
    PAID once (C-W3 disagreement case).
  - Confirmed charge with no transition is recoverable to PAID; PAID always
    traces to a confirmed charge (C-W5/R-008).
- **Constraints:** C-W1..C-W5; depends on WS-P1 (ChargeRecord lookup port),
  substrate `IdempotencyStore` event keyspace, `OrderPaymentState`.
- **Context needed:** webhooks brief, ADR-005/008/012, frozen WS-P1, orders
  touchpoint contract.

---

## 3. Interface contracts

### 3.1 Cross-area (consumed / renegotiated) — see §7 for the renegotiations
- **Inbound from substrate:** `IdempotencyStore.claim/commit` (SI-1..SI-3),
  `EventLog.append/read`, `Money`, `Id*`, `Clock`. **Consumed as specified**,
  with one renegotiation request to substrate (§7 #4: the purchase-identity
  keyspace needs the same atomic-claim guarantee, and the store must be able to
  carry the `ChargeRecord` or a sibling table must — ownership decision D-P3).
- **Outbound to orders:** `OrderPaymentState.mark_paid(order_id, charge_id,
  amount, idempotency_key)`. **Consumed as specified.** Note: `mark_paid` is
  driven by **webhooks only** (D-P5), resolving the orders-contract "provisional/
  open" bullet ("called by intake (sync) or only by webhooks").
- **`ChargeService`** (gateway exposes; intake consumes): **RENEGOTIATED** — see
  §7 #1/#3.
- **`ChargeRecord`** (NEW port): **RENEGOTIATED into existence** — see §7 #2.

### 3.2 Cross-workstream (internal to payments)
| Producer | Consumer | Contract | Coupling |
|---|---|---|---|
| WS-P1 | WS-P3,P4,P5 | frozen `ChargeService` + `ChargeRecord` + `SubmitResult` | tight — all build against it; must freeze first |
| WS-P2 | WS-P3,P5 | keyspace namespacing + key derivation rules + TTL | tight on intake; webhooks uses `evt:` only |
| WS-P3 (intake) | WS-P4 (gateway) | `ChargeService.charge()` call | the frozen WS-P1 contract |
| WS-P3 (intake) | WS-P5 (webhooks) | `ChargeRecord{PENDING, intent_id}` written by intake, read by webhooks | **decoupled in time** — async; the record IS the only coupling |
| WS-P5 (webhooks) | orders | `OrderPaymentState.mark_paid` | the orders contract |

The intake↔webhooks coupling is **entirely through the durable `ChargeRecord`**,
not a call. This is the design's load-bearing seam: sync and async never call
each other; they meet at the record keyed by `intent_id`.

---

## 4. Decisions at this level (with reasoning)

- **D-P1 — Request idempotency key is CLIENT-SUPPLIED, intake validates.**
  *Why:* R-007.1 is about the *client's* retry of the *same* logical request; only
  the client knows two HTTP calls are "the same attempt" (a server-minted key
  would differ per call and defeat dedup). Standard Stripe-style
  `Idempotency-Key` header semantics. *Alternative considered:* server-minted from
  `(order_id, amount)` — rejected because it conflates request-retry (R-007.1)
  with purchase-uniqueness (R-007.3), collapsing two of ADR-004's three guards
  into one (a violation of ADR-004). *L2 should validate:* this pushes a
  correctness obligation onto the client (must reuse the key on retry); acceptable
  for a backend slice but worth confirming the client contract.

- **D-P2 — Purchase-identity key (R-007.3) = deterministic hash of
  `(order_id, amount, currency)`.** *Why:* "the same purchase" must be derivable
  *server-side* and identically by two concurrent requests so they collide on one
  claim. It must NOT include the request idem-key (that would let two genuinely-
  duplicate submits with different request keys both pass — the exact R-007.3 hole).
  *Open:* if a legitimate re-order of the identical cart at the identical price is
  possible, this key would false-reject it (T-3 tolerated direction — acceptable
  per ADR-007, but flagged as RISK-4).

- **D-P3 — `ChargeRecord` is owned by `payments` (a new payments-internal
  persistence component), NOT the substrate.** *Why:* it is payments-domain data
  (charge attempts), not a cross-cutting primitive; the substrate stays the
  stable generic core (ADR-001). It is written by intake and read/updated by
  webhooks — both payments submodules — so it lives at the **area** level, shared
  by the two workstreams. *Alternative:* push it into substrate as a generic
  "operation record" — rejected as premature generalization (confetti of a
  primitive nothing else uses yet). *L2 must validate:* this creates a small
  payments-area shared-persistence component that both intake and webhooks
  depend on; it is the one place the area is not cleanly tree-shaped (two
  workstreams share mutable state). See RISK-1.

- **D-P4 — Idempotency-key retention: request keys retained ≥ the Stripe
  webhook-delivery + retry window (Stripe retries webhooks up to ~3 days), with a
  conservative TTL of 7 days; ChargeRecord retained for audit indefinitely (it is
  the R-008 audit spine, append-only via EventLog).** *Why:* a request key must
  outlive any in-flight retry/redelivery, or a late retry after expiry becomes a
  fresh charge (RISK-3). Stripe's event-id keys (`evt:`) follow the same ≥7-day
  floor. *L2/substrate validate:* the substrate store must support TTL or
  scheduled reaping; if it cannot, escalate (ties to ADR-010).

- **D-P5 — The order→PAID transition is driven by webhooks ONLY (authoritative
  channel); intake returns `ACCEPTED_PENDING` and never transitions.** *Why:*
  ADR-005 makes the webhook authoritative; the skeleton proved intake was being
  *forced* to either lie (mark paid on a provisional sync result) or invent
  `ACCEPTED_PENDING`. Choosing webhook-only resolves the cross-area renegotiation
  flag in the webhooks brief and the "provisional/open" bullets in the charge and
  orders contracts. *Consequence:* the synchronous caller NEVER gets a "paid"
  answer; it gets "pending" and must poll/await the order state or a later
  notification. *L2 must validate this is acceptable to the product* (the caller's
  UX is now inherently async) — flagged as RISK-5 and to L1 via L2.

- **D-P6 — On `FAILED`, intake calls `OrderPaymentState.mark_failed` synchronously
  (failure is safe to act on sync; only *success* must wait for the webhook).**
  *Why:* a genuine decline takes no money and creating no divergence risk;
  failing the order promptly is good UX and safe. *Asymmetry is deliberate:*
  success is webhook-authoritative, failure is sync-actionable. *L2 validate:*
  the edge case where intake sees FAILED but a late webhook later reports success
  (Stripe race) — handled by C-W3 reconciliation (webhook wins, re-opens to PAID),
  but this means `mark_failed` must itself be reversible/non-terminal. Flagged as
  RISK-6 and as a renegotiation pressure on the orders `mark_failed` contract
  (§7 #5).

---

## 5. Internal dependency map & sequencing

```
WS-P1 (contract+ChargeRecord) ─┐  BLOCKED on L2 compatibility review (freeze)
WS-P2 (keys+concurrency+TTL) ──┤  partly parallel with P1; both must freeze
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                         ▼
   WS-P4 (gateway)         WS-P3 (intake)           WS-P5 (webhooks)
   needs P1                needs P1+P2              needs P1 (+ orders, substrate)
        └───────────┬───────────┴────────────┬───────────┘
                    ▼                          ▼
              integration: intake→gateway   intake→(ChargeRecord)→webhooks→orders
```

**Suggested execution order (for the fresh execution-L3):**
1. **Freeze phase (gated on L2):** WS-P1 + WS-P2 in parallel — they are the
   renegotiated contracts and key policy; nothing should be coded against an
   unfrozen interface. Natural phase boundary: L2 compatibility review.
2. **WS-P4 (gateway)** next — it has a Stripe spike that de-risks the contract
   itself; running it early can still bounce a correction into WS-P1 before the
   freeze hardens (progressive hardening). Genuinely independent of intake.
3. **WS-P3 (intake)** — depends on both frozen contracts and on gateway's real
   `ChargeService` for integration.
4. **WS-P5 (webhooks)** — last; it depends on ChargeRecord being written by
   intake and on the orders touchpoint being ready. It closes the loop.
5. **Integration check** (execution-L3 responsibility): the async seam
   (intake writes ChargeRecord → webhook reads it) and the R-007.3 concurrency
   test — *the skeleton could not exercise R-007.3*; it MUST be exercised here
   against the real datastore unique constraint (this is the scariest case per
   project.md §6 and the skeleton's own gap).

Phase boundary worth pausing at: after WS-P4's Stripe spike and before WS-P3/P5,
re-confirm the frozen contract still holds (the spike may surface a correction).

---

## 6. Risks and concerns

- **RISK-1 (shared mutable state):** `ChargeRecord` (D-P3) is written by intake
  and updated by webhooks — the one place the area is not a clean tree. A
  write-ordering or visibility bug here is an R-008 divergence. Mitigation: the
  record's state machine (`PENDING→CONFIRMED`) is monotonic and webhook-side
  transitions are idempotent (claim-gated); ADR-012's crash-safety constraint
  applies. Must be integration-tested, not unit-tested only.
- **RISK-2 (orphan webhook):** a webhook for an `intent_id` with no ChargeRecord
  (e.g. record write lost, or webhook for a charge created out-of-band). Design:
  webhooks must NOT fabricate a PAID with no backing record (R-008). Resolution:
  park/alert, never auto-PAID. Needs a defined dead-letter path — flagged because
  no upstream doc specifies one.
- **RISK-3 (replay after TTL):** a client retry arriving after the request-key
  TTL expired would be seen as fresh → second charge. D-P4 sets TTL ≥ Stripe
  retry window, but a pathologically late retry is still a hole. The Stripe-side
  idempotency key (ADR-009) is the backstop *only if Stripe's own key has not also
  expired* (Stripe idempotency keys expire after 24h). **This is a genuine residual
  R-007.1 gap and I am surfacing it to L2** — the two TTLs (our store vs Stripe's
  24h) are not aligned, so a retry in the 24h–7day window relies on OUR store
  alone. Acceptable but must be a conscious decision, not an accident.
- **RISK-4 (false-reject of legitimate re-purchase):** D-P2's purchase key would
  reject a genuine identical re-order at identical price within the key's life.
  Per T-3 this is the tolerated direction, but the product should know.
- **RISK-5 (async-only success):** D-P5 means the submit caller never gets "paid"
  synchronously — only "pending." This is an externally visible behavior change
  the user/product must accept. Surfaced to L1 via L2.
- **RISK-6 (mark_failed reversibility):** D-P6's sync-fail + late-success race
  requires `mark_failed` to be non-terminal/reversible — a pressure on the orders
  contract (§7 #5).
- **RISK-7 (R-006/R-008 reflect-back PENDING):** the entire webhooks submodule and
  the R-008 invariant rest on R-006/R-008, which `project.md §7` flags as
  L1-derived and **not user-confirmed**. If the user rejects webhook
  reconciliation at reflect-back, WS-P5 and much of this design are reopened. I am
  designing on an unconfirmed foundation because L2 told me to; flagged loudly.
- **RISK-8 (R-007.3 never validated upstream):** the skeleton explicitly could
  NOT exercise R-007.3 (in-memory store). This design pushes the first real proof
  of the scariest must-never-fail case all the way to execution integration. The
  concurrency mechanism (D-P2 + substrate atomic claim) has had **zero** end-to-end
  validation. Recommend a dedicated concurrency spike in WS-P2 against the real
  ADR-010 datastore *before* WS-P3 hardens.

---

## 7. Interface Renegotiation (proposed to L2)

The skeleton FINDING surfaced three holes; domain analysis confirms them plus two
more. I propose the following corrections to L2's provisional contracts. **WS-P1
is blocked until L2 accepts or counters.**

**#1 — `ChargeResult` needs `ACCEPTED_PENDING{intent_id, charge_id}`** (charge
contract). *Reason:* ADR-005 makes the sync result provisional, but the enum's
only positive outcome (`CHARGED` = "a new charge was created now") cannot express
a Stripe PaymentIntent in `processing`. The skeleton was forced to overload
`CHARGED`, contradicting its own gloss. *Correction:* add `ACCEPTED_PENDING`, and
add `intent_id` to it. (The contract's "provisional/open" bullet flagged the
sync-vs-webhook question but left the enum unchanged — that enum is the real hole.)

**#2 — A `ChargeRecord` lookup port must become a first-class contract with a
named owner and write-points** (currently absent from `contracts/`). *Reason:* the
webhook carries Stripe `intent_id`; `OrderPaymentState.mark_paid` demands
`(order_id, charge_id, amount, idempotency_key)`. Nothing bridges the two. ADR-012
decided the *property* (no divergence) and the *write ordering* but **dropped the
lookup record** that makes reconciliation possible. *Correction:* add the
`ChargeRecord` port (§WS-P1 acceptance); **owner = payments area** (D-P3);
write-points = intake writes PENDING pre-charge, webhooks writes CONFIRMED.

**#3 — intake's `SubmitResult` must be a defined contract** (currently undefined
anywhere; skeleton invented it). *Reason:* because success is webhook-authoritative
(D-P5), the sync caller gets "pending," and the submit interface must say so.
*Correction:* define `SubmitResult{ACCEPTED_PENDING | ALREADY_ACCEPTED | REFUSED |
FAILED}` as part of the intake area contract.

**#4 — to the SUBSTRATE L3 (sibling-area renegotiation):** the `IdempotencyStore`
must (a) support the `purchase:` keyspace with the **same SI-1 atomic-claim
guarantee** the request/event keyspaces get (R-007.3 leans on it), and (b) the
store or a sibling table must be able to host the `ChargeRecord` durably with
TTL/retention (D-P4). The substrate-ports contract's own "provisional/open" bullet
("whether IdempotencyStore and the concurrency guard are the same table") is the
hook; I am asking for the atomic-claim guarantee to be explicitly extended to the
purchase keyspace, and for TTL support to be confirmed. *This is a cross-sibling
contract ripple — surfaced to L2 (common ancestor) to coordinate, per the
visibility rules (I cannot reach into the substrate subtree).*

**#5 — to the ORDERS L3 (sibling-area renegotiation):** `OrderPaymentState.
mark_failed` must be **non-terminal/reversible** — a sync FAILED (D-P6) followed
by a late Stripe success webhook (C-W3) must be able to drive the order back to
PAID. The orders contract currently models `FAILED` as a sink state via the
`READY → PAID | FAILED` machine with no FAILED→PAID edge. *Correction:* either
add a `FAILED → PAID` reconciliation edge or make `mark_failed` provisional until
a terminal timeout. *Surfaced to L2 to coordinate with orders L3.*

**Confirmation of what HOLDS (no change needed):** `IdempotencyStore.claim/commit`
shape carries both request and event dedup (validated by skeleton); the
three-outcome shape of `ChargeResult` is otherwise sound; `OrderPaymentState.
mark_paid` idempotency (OI-1) carries webhook redelivery; `ADR-009` Stripe-key
derivation is expressible. The renegotiations are **additive** (new outcomes, new
port, two reversibility/atomicity guarantees) — they do not contradict L2's
concept, they complete it.

---

## 8. Handoff to the fresh execution-L3

- This file becomes your `design.md`. The **frozen** inputs you build against are
  WS-P1 + WS-P2 outputs — but **they are not frozen until L2 accepts the §7
  renegotiations.** Do not spawn L4s for WS-P3/P4/P5 until the compatibility
  review clears and the interfaces lock.
- The **scariest, least-validated** thing is R-007.3 (RISK-8). Sequence a real
  concurrency spike early.
- The whole webhooks submodule rests on R-006/R-008 being confirmed at reflect-back
  (RISK-7). Confirm that status before building WS-P5.
- Acceptance tests for WS-P3/P4/P5 are authored by the L4-tester lateral at
  execution plan-time from these scopes — the criteria in §2 are the targets, not
  the tests.

*Planning-L3 collapses after this submission. Bus nudge to L2: "payments area
design at L2/plan/area-payments.md — 5 renegotiations proposed (3 from skeleton +
2 sibling-area ripples to substrate and orders); WS-P1 blocked on compatibility
review."*
