---
artifact: tagged-intent-spec
project: ecommerce-backend (Payments vertical slice)
owner: L1 (System Orchestrator)
status: signed-brief-draft (pending reflect-back confirmation)
edit_policy: immutable once settled; intent revisions only via explicit revision record
created: 2026-06-02
intake_method: M50/K45 (outcomes-first → tradeoff-probing → variable-depth → fluency capture → tagged living spec → MNF decomposition → reflect-back)
---

# Intent Spec — E-Commerce Backend (Payments Slice)

> Scope note: the user asked for "an e-commerce backend." This dry-run builds and gates ONE
> vertical slice — **Payments** — first (walking-skeleton-first). This spec captures the full
> intent envelope the user expressed, tags which parts are in-slice vs out-of-slice, and
> decomposes the must-never-fail (R-007) the Payments slice exists to guarantee.

---

## How to read this spec

- **IDs are minted only here** (intake). Everything downstream traces to an ID or is sanctioned scope.
- **Tags:** `decided` (user resolved it) · `delegated` (left to professional judgment below) · `deferred` (resolved at the last responsible moment).
- **MNF** = must-never-fail flag. MNF requirements are decomposed to atomic, individually-testable obligations below (§ Must-Never-Fail Decomposition).
- **Fluency** per opinionated area drives gate render-depth (M58): `technical` = show the user the technical claim; `plain` = show the plain-language implication.
- The **ID→intent-span map** (§ at bottom) gives the verbatim user span(s) each ID claims to carry, so the prose→ID minting is inspectable at the gate (Check 0).

---

## Outcomes (what success looks like, in the user's terms)

- **O-1.** The user can sell products online and reliably collect money for orders, with Stripe as the payment processor. ("an e-commerce backend"; "must use Stripe")
- **O-2.** Running it stays cheap — hosting cost is a standing priority, not an afterthought. ("keeping hosting cost low")
- **O-3.** A customer is **never charged twice** for one purchase. This is the line that must never be crossed. ("never double-charge a customer")

Slice framing: O-1 and O-3 are what the **Payments slice** delivers and guarantees. O-2 is a cross-cutting constraint that shapes every area, including this slice. The rest of "an e-commerce backend" (catalog, cart, fulfillment, accounts) is acknowledged scope but **out of this slice** and tagged `deferred`/out-of-slice below.

---

## Requirements

| ID | Requirement | Tag | MNF | Parent | Fluency | Notes |
|----|-------------|-----|-----|--------|---------|-------|
| R-001 | System is an e-commerce backend: it accepts orders and collects payment for them. | decided | — | O-1 | plain | The product framing. Payments slice realizes the payment-collection half. |
| R-002 | Stripe is the payment processor; payment integration is built against Stripe. | decided | — | O-1 | technical | Hard user constraint. Not an option to weigh — fixed. User fluent at flow level. |
| R-003 | Hosting/running cost is kept low; cost is a first-class design constraint, weighed against other -ilities. | decided | — | O-2 | plain | User opinionated on the *goal* (cheap), delegated on the *means* (which infra). See tradeoff probe T-1. |
| R-004 | Architecture/stack choices (language, framework, datastore, deploy target) are left to professional judgment, subject to R-003 (cheap) and R-002 (Stripe). | delegated | — | O-2 | plain | User: "use your best judgment." Delegated WITH the two constraints above as the bounding rubric. |
| R-005 | Payment flow: customer submits an order → backend creates a charge via Stripe → order is marked paid on success. | decided | — | O-1 | technical | User fluent at this altitude; co-described the flow. |
| R-006 | Stripe payment outcomes are reconciled via Stripe's webhooks (async confirmation), not solely the synchronous API response. | delegated | — | R-005 | technical | Implied by R-002+R-007(b). User did not specify webhooks by name; this is professional judgment that the user's "never double-charge" obligation forces a webhook-aware design. Flagged for reflect-back. |
| **R-007** | **A customer is never double-charged for a single purchase.** | **decided** | **YES** | **O-3** | **plain** | **THE must-never-fail.** Decomposed to atomic obligations R-007.1 / R-007.2 / R-007.3 below. User confirms the *decomposition*, not just the outcome. |
| R-007.1 | A network/app-level retry of the same submit results in **exactly one** charge. | decided | YES | R-007 | plain | Sub-obligation (a). Idempotent submit. |
| R-007.2 | A duplicate gateway webhook (Stripe redelivers the same event) results in **exactly one** charge / one state transition. | decided | YES | R-007 | technical | Sub-obligation (b). Webhook idempotency / event-dedup. Render technical: tied to Stripe event IDs. |
| R-007.3 | Two concurrent submits of the same purchase result in **exactly one** charge. | decided | YES | R-007 | plain | Sub-obligation (c). Concurrency control / uniqueness under race. |
| R-008 | On a charge failure or ambiguous outcome, the system never silently leaves the customer charged-but-order-unpaid or paid-but-uncharged (no money/order divergence). | delegated | — | O-3 | plain | Adjacent integrity obligation surfaced while decomposing R-007. NOT what the user literally said; raised at reflect-back. Tagged delegated because user delegated "everything else," but flagged because it is in the blast radius of "never double-charge." |
| R-009 | Catalog / product management. | deferred | — | O-1 | plain | Out of Payments slice. Part of "an e-commerce backend." Revisit when slice gates. |
| R-010 | Cart / checkout assembly (pre-payment). | deferred | — | O-1 | plain | Out of slice. The Payments slice assumes an order arrives ready-to-charge. |
| R-011 | Customer accounts / auth. | deferred | — | O-1 | plain | Out of slice. |
| R-012 | Fulfillment / order lifecycle past "paid." | deferred | — | O-1 | plain | Out of slice. |
| R-013 | Refunds / disputes / partial captures. | deferred | — | O-3 | technical | Out of slice but adjacent to R-007 — a refund path can re-open double-charge risk. Explicitly parked, not dropped. |
| R-014 | Security/PCI posture: card data handled so the backend does not store raw PANs (Stripe-hosted card capture / tokenization). | delegated | — | R-002 | plain | Implied by using Stripe correctly. User did not raise it; professional default. Flagged so it is not silently assumed. |

Tags legend: `decided` · `delegated` · `deferred`.

---

## Opinionated Areas + Technical Fluency (per area)

Captured via tradeoff-probing (the forks below), not a survey. Drives gate render-depth (M58).

| Area | Opinionated? | User fluency here | Render at gate as | Evidence (which fork they engaged) |
|------|--------------|-------------------|-------------------|------------------------------------|
| Payment processor choice | **Yes — fixed** (Stripe) | Fluent at *flow* level; NOT fluent on idempotency/transaction internals | technical for the flow; plain for the internals | Declared Stripe up front, unprompted; engaged on flow when probed. |
| Hosting / running cost | **Yes — directional** (keep it cheap) | Plain-language; cares about the outcome (cost), not the mechanism | plain-language cost implications | Engaged fork T-1 (cheap vs. managed-but-pricier); chose cheap. |
| Double-charge safety (R-007) | **Yes — absolute** | Fluent on *what* must not happen; NOT fluent on *how* (idempotency keys, dedup, locking) | plain for the guarantee; technical detail surfaced only as plain-language implications | Named it unprompted as the must-never-fail. Did not engage idempotency mechanics — delegated the *how*. |
| Stack / framework / datastore | **No — delegated** | n/a | plain (just confirm it matches "cheap" + "Stripe") | Waved off fork T-2 ("use your best judgment"). |
| Webhook handling, concurrency model, PCI handling | **No — delegated**, but in R-007's blast radius | NOT fluent | plain-language implications, with the *guarantee* (not the mechanism) surfaced | Did not raise; delegated. Surfaced by L1 because they bear on the MNF. |

**Tradeoff probes run (for the record):**
- **T-1 (cost vs. operational simplicity):** "Cheapest possible infra you babysit more, vs. a managed platform that costs more but runs itself — which way?" → User: cheap. ⇒ R-003 `decided`, means `delegated` (R-004).
- **T-2 (stack choice):** "Any preference on language/framework/database, or my call?" → "Your call." ⇒ R-004 `delegated`.
- **T-3 (double-charge strictness):** "If we had to choose: occasionally *reject a legitimate second purchase* to be safe, vs. occasionally risk a double-charge — which error do you prefer?" → User: never double-charge; a rejected/retried legitimate purchase is the acceptable failure direction. ⇒ confirms R-007 is the hard constraint and sets the **safe-failure direction** (fail toward *not charging twice*, even at the cost of a false reject) — load-bearing for R-008 and for how R-007 is realized.

---

## Must-Never-Fail Decomposition (R-007)

A compound MNF minted whole is the highest-stakes place for silent loss. R-007 is split into atomic, individually-testable obligations. **The user confirms this decomposition itself at reflect-back**, not just the headline.

| Obligation | ID | The concrete failure it forbids | Why it is its own obligation (distinct mechanism) | Testable as (negative/failure-path test the gate will require) |
|------------|----|----|----|----|
| (a) Retry-safe | R-007.1 | Customer/app retries the submit (timeout, double-click, network blip) and gets charged twice. | Defeated by request-level idempotency (idempotency key on submit). Different mechanism from (b) and (c). | Replay the *same* submit with the same idempotency key N times → assert exactly one Stripe charge and one order-paid transition. |
| (b) Webhook-dedup | R-007.2 | Stripe redelivers the same webhook event (at-least-once delivery) and the system charges/credits twice or double-transitions state. | Defeated by event-level dedup on Stripe event ID. Stripe's own retry semantics; orthogonal to client retries. | Deliver the same Stripe event ID twice → assert one state transition; second is a no-op (idempotent consume). |
| (c) Concurrency-safe | R-007.3 | Two requests for the same purchase race (two tabs, two app instances) and both create a charge. | Defeated by a uniqueness/locking guarantee on the purchase identity under concurrency. Race-specific; survives even if (a) and (b) hold. | Fire two concurrent submits for the same purchase → assert exactly one charge; the loser is rejected or coalesced, never a second charge. |

**Cross-cutting confirmation for the decomposition:**
- The three obligations are **independent failure modes** — each can fail while the others hold — which is exactly why R-007 cannot be a single test. (This is the argument the user is asked to confirm.)
- **Safe-failure direction (from T-3):** when any of the three guards triggers, the system fails *toward not charging twice* — a duplicate/concurrent/replayed attempt is rejected or coalesced, never charged again. A *false reject of a legitimate second, genuinely-distinct purchase* is the acceptable failure; a double-charge is not. The downstream design and the MNF adequacy check (gate Check 4b) must honor this direction.
- **Adjacent obligation R-008** (no charged-but-unpaid / paid-but-uncharged divergence) is in R-007's blast radius and is named so it is not silently dropped, but it is its own requirement, not a fourth sub-obligation of R-007.

---

## ID → Intent-Span Map (verbatim source spans)

Per minted ID, the verbatim user span(s) it claims to carry — so the prose→ID translation is inspectable at the gate (Check 0). Spans marked **[L1-derived]** are NOT verbatim user words; they are professional-judgment requirements L1 surfaced from the user's stated constraints, and are called out for reflect-back so the user can confirm or reject them.

| ID | Verbatim intent span(s) | Derived? |
|----|--------------------------|----------|
| R-001 | "an e-commerce backend" | verbatim |
| R-002 | "must use Stripe for payments" | verbatim |
| R-003 | "keeping hosting cost low" | verbatim |
| R-004 | "everything else: use your best judgment" (applied to stack) | verbatim (delegation) |
| R-005 | "fluent enough to discuss payment flow at a high level" + "an e-commerce backend" + "must use Stripe" → submit→charge→mark-paid flow | verbatim flow framing; specific steps **[L1-derived]** |
| R-006 | **[L1-derived]** from "must use Stripe" + "never double-charge" → Stripe webhooks must be reconciled | derived |
| R-007 | "never double-charge a customer" | verbatim |
| R-007.1 | "never double-charge a customer" + intake decomposition (a) "network/app retry → charged once" | verbatim outcome; obligation per intake MNF-decomposition mandate |
| R-007.2 | "never double-charge a customer" + intake decomposition (b) "duplicate gateway webhook → once" | verbatim outcome; obligation per intake MNF-decomposition mandate |
| R-007.3 | "never double-charge a customer" + intake decomposition (c) "concurrent submit → once" | verbatim outcome; obligation per intake MNF-decomposition mandate |
| R-008 | **[L1-derived]** from "never double-charge a customer" (blast radius: money/order integrity) | derived |
| R-009 | "an e-commerce backend" (catalog implied) | verbatim envelope; specific scope **[L1-derived]** out-of-slice |
| R-010 | "an e-commerce backend" (cart/checkout implied) | verbatim envelope; **[L1-derived]** out-of-slice |
| R-011 | "an e-commerce backend" (accounts implied) | verbatim envelope; **[L1-derived]** out-of-slice |
| R-012 | "an e-commerce backend" (fulfillment implied) | verbatim envelope; **[L1-derived]** out-of-slice |
| R-013 | **[L1-derived]** from "must use Stripe" + "never double-charge" (refund path re-opens charge risk) | derived |
| R-014 | **[L1-derived]** from "must use Stripe" (correct Stripe use ⇒ tokenized card capture, no raw PAN storage) | derived |

---

## Reflect-Back Script (played to the user; user confirms or corrects)

> Here is what I have. Correct me where I'm wrong.
>
> 1. **You're building an e-commerce backend, and we'll build the *payments* part first** — the part that takes an order and collects the money — because that's where your one hard rule lives. The rest (catalog, cart, accounts, fulfillment) is real but comes after. *(R-001, R-009–R-012)*
> 2. **Stripe is fixed.** Not something I'll weigh — it's decided. *(R-002)*
> 3. **Cheap to run is a standing rule.** I'll pick the stack and hosting to keep it cheap; that's my call, but "cheap" is yours. *(R-003, R-004)*
> 4. **Your one line that must never be crossed: a customer is never charged twice.** I've split that into the three distinct ways it could happen, because each needs a different guard: *(a)* you/the app retry and it charges twice; *(b)* Stripe sends us the same confirmation twice; *(c)* two submits race each other. **Do these three cover what you mean by "never double-charge"?** *(R-007 + R-007.1/.2/.3)*
> 5. **One direction I need you to confirm:** if we ever can't tell, I'll make the system **refuse/coalesce rather than risk a second charge** — meaning once in a blue moon it might reject a legitimate second attempt. You're telling me that's the safer error. **Right?** *(T-3, drives R-008)*
> 6. **Two things you didn't say but I'm assuming** — tell me if I'm wrong: I'll handle Stripe's confirmation webhooks (not just the immediate response), and I'll never leave you "charged but order shows unpaid." *(R-006, R-008)*
>
> Everything else, I run on my judgment under "cheap" and "Stripe."

**Confirmation status:** PENDING (simulation — no live user turn). Items 4, 5, 6 are the load-bearing confirmations; until confirmed they remain L1-derived assumptions, not user-decided fact.

---

## Open / To-Confirm at Reflect-Back

- R-006, R-008, R-013, R-014 are **L1-derived**, not user-stated. They must be confirmed (or rejected) before they harden from `delegated` to `decided`.
- The R-007 decomposition (three obligations) must be confirmed *as a decomposition* — the user agreeing the three cover their intent.
- The safe-failure direction (T-3 → R-008) must be confirmed; it changes how aggressively the system may reject.
