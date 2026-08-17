---
artifact: L2-concept-design (project.md)
project: ecommerce-backend — Payments vertical slice
level: L2 (Project Architect)
c4_altitude: container/component (system → containers → components; stops above code)
status: concept-design (Phase 2 output; pre concept-validation, pre planning-cascade)
input: dry-run/intent-spec.md (signed-brief-draft, reflect-back PENDING)
created: 2026-06-02
owner: L2 — owner-only
---

# Concept Design — E-Commerce Backend (Payments Slice)

## 0. What this project is (the thing made visible)

An e-commerce backend whose **first vertical slice is Payments**: it accepts an
order that is ready to be charged, charges it via Stripe, and records the order
as paid — and it does this such that **a customer is never double-charged**
(R-007, the must-never-fail). Everything else in "an e-commerce backend"
(catalog, cart, accounts, fulfillment — R-009..R-012) is acknowledged but
**out of this slice**.

Success for the slice, concretely:
- An order ready-to-charge can be submitted and results in **exactly one** Stripe
  charge and **exactly one** order→paid transition, under retry (R-007.1),
  webhook redelivery (R-007.2), and concurrent submit (R-007.3).
- When the outcome is unknown/failed, the system **fails toward not charging
  twice** (T-3 safe-failure direction) and never leaves money/order divergence
  (R-008).
- It runs cheap (R-003/O-2) and uses Stripe (R-002).

**Top constraint:** R-007 (never double-charge), with the T-3 safe-failure
direction (prefer false-reject over double-charge).
**Biggest risk:** the three R-007 sub-obligations are *independent* failure
modes defeated by *three different mechanisms* (request idempotency, event
dedup, concurrency uniqueness) — getting one right does not get the others
right. The architecture must place each mechanism deliberately.

---

## 1. The Substrate (established first — B14, the sun)

Per DECOMPOSITION-METHODOLOGY Part I.8 and PROJECT-PLANNING Phase 2, the
cross-cutting stable core is named explicitly and built first via the walking
skeleton. The feature areas orbit it (dependencies point inward toward it).
Nothing volatile sits at the center.

The substrate is a **platform/foundation context**, NOT a peer feature module.
It owns these stable primitives:

| Substrate primitive | What it is | Why it's substrate (cross-cutting / stable) | Intent IDs |
|---|---|---|---|
| **Money** | Value type: integer minor units + ISO-4217 currency. No floats. Arithmetic, equality, formatting. | Every area that touches price/charge needs it; it is stable and shared. Wrong money handling silently corrupts charges. | R-005, R-007, R-008 |
| **IDs** | Typed identifiers (`OrderId`, `ChargeId`, `PaymentIntentId`, `IdempotencyKey`, `EventId`). Generation + parsing. | Identity is the spine the whole slice + the one-spine scheme rest on. | R-007.1/.2/.3 |
| **DomainEvent + audit log** | Append-only, immutable record of state transitions (order-paid, charge-created, webhook-consumed) with timestamps and actor. | Auditability of "exactly once" is itself a requirement of proving R-007; needed by every area. | R-007, R-008 |
| **IdempotencyPrimitive** | The *generic* "do this side-effecting operation at most once for a given key" mechanism: a uniqueness-guaranteeing store + claim/commit protocol. | This is the single mechanism R-007.1 and R-007.2 are both *built on*; concurrency (R-007.3) leans on the same uniqueness store. Lifting it here (vs. duplicating in each area) is the whole B14 point. | R-007.1, R-007.2, R-007.3 |
| **Clock** | Injectable time source. | Needed for audit, for webhook-staleness, for testability of the above. Trivial but shared. | R-007.2, R-008 |

**Substrate ports (sockets the features plug into) — provisional, see contracts:**
- `IdempotencyStore` — claim(key) → {FRESH | DUPLICATE(prior_result)}, commit(key, result).
- `EventLog` — append(event), read(stream).
- `Money`, `Id*` — pure value types (no port; imported).

The substrate is a **deep module**: large hidden complexity (uniqueness under
concurrency, durable audit) behind a small contract (claim/commit, append/read).

---

## 2. Feature-area carving (DDD seams)

Carving the *Payments slice* (not the whole store — the rest is deferred). The
seam test (DECOMPOSITION Part I.3): carve where the **ubiquitous language
shifts** and group what **co-changes**.

Three feature areas inside the slice, plus the substrate they orbit and one
**Orders touchpoint** at the slice edge:

```
                         ┌─────────────────────────────┐
                         │        SUBSTRATE            │  ← the sun (stable)
                         │ Money · IDs · Events/Audit  │
                         │ IdempotencyPrimitive · Clock│
                         └─────────────┬───────────────┘
                  depends inward       │      depends inward
        ┌───────────────┬─────────────┼──────────────┬────────────────┐
        │               │             │              │                │
  ┌─────▼─────┐   ┌──────▼──────┐  ┌───▼─────────┐  ┌─▼──────────────┐
  │ payments/ │   │  payments/  │  │  payments/  │  │   orders       │
  │  intake   │   │   gateway   │  │  webhooks   │  │ (touchpoint)   │
  │ (submit + │   │  (Stripe    │  │ (Stripe     │  │  order state:  │
  │  request  │   │   charge    │  │  event      │  │  ready→paid    │
  │  idempot- │──▶│   adapter)  │◀─│  dedup +    │─▶│  /failed       │
  │  ency)    │   │             │  │  reconcile) │  │                │
  └───────────┘   └─────────────┘  └─────────────┘  └────────────────┘
   R-007.1,.3       R-002,R-005      R-006,R-007.2     R-005,R-008
```

### 2.1 `payments/intake` — submit boundary & request idempotency
- **Language seam:** "submit", "idempotency key", "attempt". The caller's
  request, not Stripe's domain.
- **Owns:** the submit endpoint contract; request-level idempotency (R-007.1);
  the concurrency-uniqueness guard on purchase identity (R-007.3); deciding
  whether a submit is a fresh attempt or a replay/race; the safe-failure
  rejection path (T-3).
- **Co-change:** retry semantics and concurrency guard change together (both are
  "is this the same attempt?").
- **Depends on:** substrate `IdempotencyStore`, `IDs`, `EventLog`; calls
  `gateway` to actually charge; tells `orders` the result.

### 2.2 `payments/gateway` — Stripe charge adapter
- **Language seam:** Stripe's domain — "PaymentIntent", "charge", "client
  secret", Stripe idempotency-key header. The hexagonal **adapter** to the
  external processor.
- **Owns:** translating a domain charge request into a Stripe API call;
  passing a Stripe idempotency key (Stripe's own at-most-once on the API);
  mapping Stripe outcomes back to a domain result; tokenized card handling so no
  raw PAN is stored (R-014).
- **Co-change:** everything that changes when Stripe's API changes lives here and
  stops here (membrane).
- **Depends on:** substrate `Money`, `IDs`; external Stripe API (adapter edge).

### 2.3 `payments/webhooks` — Stripe event dedup & reconciliation
- **Language seam:** Stripe *events* — "event id", "redelivery", "at-least-once
  delivery", "signature". Distinct language from a synchronous charge.
- **Owns:** receiving Stripe webhooks; verifying signature; **event-level dedup
  on Stripe event id** (R-007.2); reconciling async outcome with the
  synchronous result (R-006); driving the order→paid transition on confirmed
  payment; honoring no-divergence (R-008).
- **Co-change:** webhook handling and event-dedup change together; orthogonal to
  request retry (different delivery channel, Stripe's retry not the client's).
- **Depends on:** substrate `IdempotencyStore` (event-id as key), `EventLog`;
  reads gateway/intake state; tells `orders` the confirmed result.

### 2.4 `orders` — the touchpoint (slice edge)
- **Language seam:** "order", "ready-to-charge", "paid", "failed". The order
  lifecycle. In the full store this is a large area; **for this slice it is a
  thin touchpoint**: the slice assumes an order arrives *ready-to-charge*
  (R-010 cart/checkout is deferred) and the slice's job ends at *paid/failed*
  (R-012 fulfillment deferred).
- **Owns (in-slice):** the order's payment-relevant state machine
  (`READY → PAID | FAILED`), the single-source-of-truth for "is this order
  paid?", and the integrity invariant R-008 (no paid-but-uncharged /
  charged-but-unpaid).
- **Provisional:** in-slice we model just enough of `orders` to host the state
  transition and the R-008 invariant. The full Orders area is deferred; this is
  the **minimal touchpoint** the Payments slice needs.

**Why not fewer areas (glob check):** intake/gateway/webhooks sit on three
*different language seams* and three *different R-007 mechanisms*; folding them
into one "payments" module would put request-retry, Stripe-API, and
event-redelivery — three independent change axes — behind one fat interface.
**Why not more (confetti check):** each area hides a real, independent change
axis behind a thin contract; none is a pass-through.

---

## 3. Component map (zoom into the slice)

```
SUBSTRATE (built first, walking skeleton stands on it)
  Money            value type
  IDs              typed ids + IdempotencyKey
  EventLog         append-only audit (port: EventLog)
  IdempotencyStore claim/commit uniqueness (port: IdempotencyStore)  ← R-007 backbone
  Clock            injectable time

payments/intake
  SubmitHandler          request entry; mints/accepts IdempotencyKey
  IdempotencyGuard       claim() on key → FRESH vs DUPLICATE (R-007.1)
  ConcurrencyGuard       uniqueness on purchase identity (R-007.3)  *
  ChargeOrchestrator     calls gateway, records result, notifies orders

payments/gateway
  StripeChargeAdapter    domain ChargeRequest → Stripe PaymentIntent
  StripeIdempotencyMap   domain key → Stripe idempotency-key header
  OutcomeMapper          Stripe response → domain ChargeResult

payments/webhooks
  WebhookReceiver        HTTP in; signature verify
  EventDedup             claim() on Stripe event id (R-007.2)
  Reconciler             confirmed event → order transition (R-006, R-008)

orders (touchpoint)
  OrderState             READY → PAID | FAILED  (single source of truth)
  PaidTransition         idempotent transition; R-008 invariant holder

* ConcurrencyGuard and IdempotencyGuard may be the same primitive applied to
  two different keys — see ADR-006 and the D26 constraint in the intake spec.
```

---

## 4. Architecturally-significant decisions (index)

Full records in `decisions/`. Significance = expensive to reverse / crosses
module boundaries / constrains every level below.

| ADR | Decision | Status | Intent IDs |
|----|----|----|----|
| 001 | Substrate-first; named foundation context | decided | R-007, R-003 |
| 002 | Money as integer minor units + currency value type | decided | R-005, R-007 |
| 003 | One generic IdempotencyPrimitive in substrate, reused by intake + webhooks | decided | R-007.1, R-007.2 |
| 004 | Three R-007 sub-obligations get three distinct, separately-tested guards | decided | R-007.1/.2/.3 |
| 005 | Webhook-aware design; Stripe events reconciled (not sync-only) | decided | R-006, R-007.2 |
| 006 | Concurrency guard = DB uniqueness constraint on purchase identity | deferred (constraint to L3) | R-007.3 |
| 007 | Safe-failure direction baked into contracts (fail toward not-charging) | decided | R-007, R-008, T-3 |
| 008 | Order paid-state is single-source-of-truth in `orders`, not duplicated | decided | R-008, R-005 |
| 009 | Stripe idempotency key derived from the domain idempotency key | decided | R-007.1, R-002 |
| 010 | Cheap-stack envelope: single relational DB + single service (defer exact choice) | deferred (constraint to L3) | R-003, R-004 |
| 011 | Stripe-hosted card capture / tokenization; backend stores no raw PAN | decided | R-014, R-002 |
| 012 | Charge-before-or-after order-state ordering & the write protocol for R-008 | deferred (constraint to L3) | R-008 |

---

## 5. Per-area specs (briefs) — pointers

Each area's spec (scope + provisional interface + constraints/D26 rubric) is in
`briefs/`. Detail is focused on Payments + substrate + the Orders touchpoint,
per assignment. Areas:
- `briefs/substrate.md`
- `briefs/payments-intake.md`
- `briefs/payments-gateway.md`
- `briefs/payments-webhooks.md`
- `briefs/orders-touchpoint.md`

Provisional interface contracts (the sockets) are in `contracts/`.

---

## 6. Build order (informs the walking skeleton; not gated execution)

1. **Substrate first** (Money, IDs, EventLog, IdempotencyStore, Clock).
2. **Walking skeleton:** one thin thread — submit → gateway (stubbed/sandbox
   Stripe) → order PAID — exercising every boundary, proving the connections
   and validating the provisional interfaces (progressive hardening). Ungated
   spike.
3. Core-out by dependency/risk: intake idempotency (R-007.1) → gateway
   (R-002) → webhooks dedup (R-007.2) → concurrency guard (R-007.3) →
   R-008 reconciliation hardening.

Risk-first note: R-007.3 (concurrency) and R-008 (no divergence) are the
scariest; spike the uniqueness-under-race mechanism early (ADR-006).

---

## 7. Open / carried-forward (surfaced to L1 at concept validation)

- The intent spec's reflect-back is **PENDING** — R-006, R-008, R-013, R-014 are
  L1-derived, not user-confirmed. The concept *depends on* R-006 and R-008
  (webhooks + no-divergence) being real. If the user rejects them at reflect-back,
  `payments/webhooks` and the R-008 invariant change materially. **Flagged to L1.**
- Default priorities this concept applies (surfaced per config.md): correctness
  of R-007 is weighted **above** cost (R-003) wherever they trade off — e.g., a
  uniqueness constraint / durable idempotency store costs a little but is
  non-negotiable for the MNF. If the user wants cost to win that trade, escalate.
