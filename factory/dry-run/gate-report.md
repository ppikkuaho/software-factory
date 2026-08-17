---
artifact: plan-alignment-gate-report (dry-run)
gate: Plan-Alignment Gate (design → build vertex of the V)
slice: Payments (e-commerce backend vertical slice)
must-never-fail: R-007 (never double-charge), decomposed R-007.1 / R-007.2 / R-007.3
ran_against: dry-run/intent-spec.md, L2/project.md, L2/briefs/*, L2/contracts/*,
  L2/decisions/001-012 + interfaces-locked.md, L2/plan/area-payments.md,
  build/payments/acceptance.md + test_acceptance_*.py
gate_design: design/PLAN-ALIGNMENT-GATE.md
run_by: Plan-Alignment Gate role (dry-run simulation)
created: 2026-06-02
verdict: CONDITIONAL FAIL → routes to HUMAN (cannot PASS; structural + MNF defects open)
---

# Plan-Alignment Gate Report — Payments Slice

## 0. Headline verdict

**CONDITIONAL FAIL — does not PASS to the build cycle.** The plan is unusually
strong on *content* (the three R-007 mechanisms are correctly separated, the
safe-failure direction is honored, ChargeRecord bridges sync→async coherently).
But the gate **cannot emit PASS** for three independent reasons, any one of which
is sufficient:

1. **The plan did not carry the trace-block instrumentation the gate's RTM-builder
   is specified to consume** (Check 1, hard-gating). The RTM was reconstructable by
   hand only, not "generated, never maintained." This is a *gate-can't-run* defect,
   not just a finding — see §2 and SC-G1.
2. **Two reflect-back-PENDING confirmations (R-006, R-008) are load-bearing for the
   entire webhooks branch and the R-008 invariant** — and one of them (R-007's own
   decomposition + the async-only consequence) must reach the human by construction.
   The gate's human sign-off (Check 8) is the irreducible lock and it is unsatisfied.
3. **A must-never-fail obligation (R-007.1) has a consciously-accepted residual hole
   (GAP-2, the 24h–7day TTL window)** that the gate's MNF rule forbids L1 from
   clearing. It must surface to the human force-expanded.

The detailed checks follow in gate order (0 → 7); the human package (Check 8 input)
is §9; shortcomings of the *plan-as-handed-to-the-gate* are §10.

---

## 1. Check 0 — Atomization completeness (prose → ID seam)

Re-read the intent prose (the verbatim spans in intent-spec.md §ID→Intent-Span Map
and the Outcomes) against the minted ID list. Question per the gate: *name every
testable obligation in the prose that is NOT cleanly carried by some ID.*

**Verdict: PASS with two routed-to-human notes.** The minting is unusually
disciplined — the ID→intent-span map exists (the artifact Check 0 needs is present,
which is itself the thing most plans omit), and R-007 is decomposed to three atomic,
individually-testable obligations exactly as the gate's MNF rule demands.

Findings (typed `UNMINTED-*` / `THIN-*`), routed to human (cannot be cleared by L1):

- **UNMINTED-0 (none, hard).** No testable obligation in the verbatim prose is
  wholly dropped. "never double-charge" → R-007.{1,2,3} is a clean, exhaustive-looking
  decomposition of the three independent failure modes.
- **THIN-1 (R-006, R-008, R-013, R-014 are L1-DERIVED, not user-said).** Four
  requirements carry `[L1-derived]` spans — they are professional-judgment additions,
  not minted from user words. The intent-spec is *honest* about this (it flags them),
  but per Check 0 they are **thin/ambiguous spans** and the requirements resting on
  them (esp. R-006/R-008, which the *whole webhooks submodule* depends on) must be
  **force-expanded to the human**, not collapsed. The atomization auditor cannot
  clear them; the human confirms or rejects.
- **THIN-2 (R-007 decomposition completeness is asserted, not confirmed).** The spec
  argues the three sub-obligations are exhaustive ("independent failure modes") but
  the **user has not confirmed the decomposition** (Confirmation status: PENDING).
  The gate's MNF rule requires the *user* to confirm the decomposition itself. Until
  then R-007.{1,2,3} are L1's decomposition, force-expanded to the human.
- **THIN-3 (decomposition may be under-complete — adversarial note).** Two failure
  modes of "double-charge" are arguably in the prose envelope but absent from the
  three IDs: **(i) refund-then-recharge** (R-013, explicitly parked out-of-slice — OK
  but named); **(ii) partial-capture / multi-capture of one PaymentIntent** charging
  twice — not mentioned anywhere. For *this slice* (single full charge) (ii) is out
  of scope, but the auditor flags it because "never double-charge" in the user's plain
  language is not obviously limited to the three minted vectors. Route to human as a
  scope-boundary confirm.

---

## 2. Check 1 — Tag well-formedness (RTM-builder, hard gate)

**Verdict: FAIL (structural) — the trace-blocks the RTM-builder consumes do not
exist at the granularity the gate specifies. The RTM is not generatable; it was
hand-reconstructed.**

The gate design (Requirements Traceability §"Flow-down is a hard authoring rule")
mandates that **each translation step emit a machine-readable trace-block tagging
each element it created** with the parent IDs it serves — "L2 tags each module and
interface; planning-L3 tags each design element; L4 tags each workstream and task;
the L4-tester tags each acceptance test and rubric line." The RTM-builder then
*harvests* these mechanically. What the plan actually carries:

| Level | What the gate spec requires | What the plan actually has | Gap |
|---|---|---|---|
| Intake (L1) | ID→intent-span map | **PRESENT** (intent-spec.md) — good | none |
| L2 module/interface | per-module, per-interface ID tag | a **per-file front-matter `intent_ids:` list** (whole-file, not per-element) | coarse: a module's interface line is not individually tagged |
| Planning-L3 design element | per design-element ID tag | front-matter `intent_ids:` on area-payments.md; per-element IDs appear in **prose tables** (e.g. §1 R-007 mechanism table) but not as a parseable block | not machine-harvestable |
| L4 workstream/task | per-WS, per-task ID tag | WS-P1..P5 reference IDs **in prose acceptance criteria**, no trace-block | not machine-harvestable; no L4 artifact exists separately at all |
| L4-tester acceptance test | per-test + per-rubric-line ID tag | `acceptance.md` has a **coverage map table** (test → obligation) + per-file front-matter `intent_ids:` | **closest to compliant**; the only level that tagged at element granularity |

Per the gate's own rule — *"A missing trace-block from any level is a FAIL — prevents
'coverage looks fine because nobody tagged that module.'"* — **this is a hard FAIL of
Check 1.** The RTM below was assembled by a human (me) reading prose, which is exactly
the "RTM tax" the gate says it avoids and exactly the manual step that makes the gate
economically irrational and skippable. **This is the single highest-value finding of
the run: the gate's load-bearing input contract (machine-readable per-element
trace-blocks) was never produced by the upstream levels.** (SC-G1.)

Sub-findings:
- **No `D-` derived-requirement records exist in the sanctioned form.** The plan has
  `D-P1..D-P6` records — but these are **design *decisions*** (resolving deferred
  ADRs), not the gate's `D-<intent-id>` **derived-requirement** records that name the
  intent ID(s) they *serve* and get a "serves" RTM row. New design elements
  (`ChargeRecord`, `ACCEPTED_PENDING`, the `purchase:` key, the dead-letter sink) were
  **born without a sanctioned `D-` birth record**. By the gate's rule ("Sanctioned ID
  birth, everywhere else is creep") these are *un-birthed elements* → Check 3
  scope-creep candidates that only escape because a human can see they serve R-007.
  A deterministic backward-coverage script would flag every one of them. (SC-G2.)
- **ID-prefix hierarchy is shallow.** The gate assumes dotted child IDs
  (`R-007.1 → R-007.1.4`) so checks can re-run over a subtree. The plan stops at
  `R-007.1` and never mints L2/L3/L4 children — every level reuses the **same**
  `R-007.1` tag. The dotted-prefix subtree-keying that Re-Gating depends on therefore
  **does not exist**; a FAIL cannot be scoped to a subtree because there are no
  subtrees in the ID namespace. (SC-G3.)

---

## 3. The RTM (hand-reconstructed — see Check 1 FAIL)

One row per leaf requirement in slice scope. `deferred`/out-of-slice IDs listed but
not built-out. Columns abbreviated. **This matrix is the gate's evidence that
coverage was checked; it is NOT a generated artifact (Check 1 FAIL).**

| Req-ID | tag | MNF | L2 element | L3 design element | L4 workstream | acceptance test | neg/fail-path test | status |
|---|---|---|---|---|---|---|---|---|
| R-001 | decided | — | concept §0 | (product framing) | — | — | — | NO TEST (decided, untested) ⚠ |
| R-002 | decided | — | gateway area | StripeChargeAdapter | WS-P4 | (implicit via charge tests) | — | thin: no test asserts "Stripe is the processor" |
| R-003 | decided | — | ADR-010 cheap-stack | (deferred to L3) | — | — | — | NO TEST; deferred infra choice ⚠ |
| R-004 | delegated | — | ADR-010 | (deferred) | — | — | — | delegated, exempt |
| R-005 | decided | — | intake+orders | SubmitHandler/OrderState | WS-P3,P5 | retry test (submit→pending→paid path) | — | covered (flow) |
| R-006 | delegated | — | webhooks area | WebhookReceiver/Reconciler | WS-P5 | webhook_dup tests | n/a | **covered BUT reflect-back PENDING** ⚠ |
| R-007 | decided | **YES** | (parent) | (parent) | (parent) | (via children) | (via children) | covered through children |
| R-007.1 | decided | **YES** | intake | IdempotencyGuard | WS-P3 | test_acceptance_retry.py | retry-replay neg test ✓ | covered; **GAP-2 residual hole** ⚠ |
| R-007.2 | decided | **YES** | webhooks | EventDedup | WS-P5 | test_acceptance_webhook_dup.py | dup-event neg test ✓ | covered |
| R-007.3 | decided | **YES** | intake | ConcurrencyGuard | WS-P3/P2 | test_acceptance_concurrency.py | concurrent-race neg test ✓ | covered; **zero end-to-end validation (RISK-8)** ⚠ |
| R-008 | delegated | — | orders+webhooks | Reconciler/ChargeRecord | WS-P5 | test_acceptance_safety.py | orphan/divergence neg tests ✓ | **covered BUT reflect-back PENDING** ⚠ |
| R-014 | delegated | — | gateway | tokenization | WS-P4 | safety rubric #5 (PAN never reaches gateway) | — | covered (asserted in harness, weakly) |
| T-3 | (probe→R-008) | — | ADR-007 | safe-failure outcomes | WS-P3/P4/P5 | safety tests (ambiguous→REFUSED) | ✓ | covered |
| R-009..R-012 | deferred | — | — | — | — | — | — | out-of-slice, exempt (listed) |
| R-013 | deferred | — | — | — | — | — | — | out-of-slice; **adjacent to R-007** (parked, named) |

---

## 4. Check 2 — Forward coverage / gap scan (hard gate)

Walk down: every decided/delegated (non-deferred) ID must trace to ≥1 design element
AND ≥1 acceptance test; every MNF additionally requires a negative/failure-path test.

**Verdict: FAIL — two decided requirements have no acceptance test.**

- **GAP-FWD-1 (R-001, decided, untested).** "System is an e-commerce backend: accepts
  orders and collects payment." No acceptance test asserts the end-to-end "order
  collected payment" outcome distinctly; it is only implicit in the retry/webhook
  paths. Decided-but-untested = hard GAP by Check 2.
- **GAP-FWD-2 (R-003, decided, untested).** "Hosting cost kept low" is `decided` and
  has **no design element and no test** — it is deferred to L3 infra choice (ADR-010)
  that does not exist yet. The gate counts a decided requirement with an empty
  design+test column as a GAP. (Mitigant: cost is arguably a non-functional constraint
  not unit-testable — but the gate spec makes no such exemption, so it gates. SC-G4:
  the gate has no story for non-functional/`-ility` requirements.)
- **MNF coverage — PASS.** All three R-007 sub-obligations trace to a design element
  AND a dedicated negative/failure-path test (retry-replay, dup-event, concurrent-race).
  Presence is satisfied (adequacy is Check 4b).
- **R-002 thin.** "Stripe is the processor" has no test that would fail if a non-Stripe
  gateway were swapped in — it is structurally assumed, not asserted. Flagged, not
  hard-failed (it is realized, just not pinned by a test).

**L1 may NOT clear the MNF items; GAP-FWD-1/2 are routable to the owning level.**

---

## 5. Check 3 — Backward coverage / scope-creep scan (hard gate)

Walk up: every design element / task / test must cite ≥1 live requirement ID or a
sanctioned `D-` record.

**Verdict: FAIL — multiple design elements born without sanctioned `D-` records
(see Check 1 sub-finding). Coverage is human-adjudicable but not machine-clean.**

- **ORPHAN-1 — `ChargeRecord` port (L-3).** A whole new first-class persistence
  component, introduced by renegotiation #2 / D-P3. It clearly *serves* R-008 + R-007
  (the sync→async bridge), but there is **no `D-008a`-style derived-requirement record
  declaring that service**; it was born inside a *decision* record. A deterministic
  backward scan tags it ORPHAN. Human adjudication: legitimate derivation → should be
  promoted to an approved `D-` record. (This is the gate working — but only because a
  human read prose; the script would have failed it.)
- **ORPHAN-2 — `ACCEPTED_PENDING` outcome + async-only behavior (L-1/L-2, D-P5).**
  A new externally-visible behavior (caller never gets sync PAID). Serves R-005/R-007
  but is a **product-visible scope-shift** the user never asked for. Correctly
  escalated by L2 as RISK-5 — but in RTM terms it is an `ORPHAN/SCOPE-SHIFT` that
  needs a `D-` record and a human confirm. **Routes to human.**
- **ORPHAN-3 — dead-letter / orphan-webhook sink (L-6, GAP-1).** Net-new scope
  ("park, alert, never auto-PAID") assigned by L2 to webhooks. Serves R-008. No `D-`
  record; destination unspecified (SHORTCOMING-LOCK-4). Backward-scan ORPHAN +
  forward-incomplete (behavioral contract locked, mechanism absent).
- **ORPHAN-4 — `purchase:` keyspace + deterministic purchase-identity key (D-P2).**
  Serves R-007.3, legitimate, but again born in a decision record, not a `D-` record.
- **No pure scope-creep found** (nothing built that serves *no* requirement). Every
  orphan resolves to a real R-007/R-008 service. The defect is **process** (un-birthed
  derivations), not **bloat**. That is itself the finding: the gate's backward check
  is structurally blind here because the `D-` discipline was never followed (SC-G2).

---

## 6. Check 4a/4b — Two-window reconstruction + adversarial comparison

The gate spec mandates two fresh clean-context agents (verification-window vs
construction-window) plus a separate adversarial comparator. **In this dry-run a
single role is running the gate, so true two-window blind independence is not
achievable** — recorded as SC-G5 (the gate design assumes spawn infra that does not
exist yet; a single agent cannot honestly run a *blind* reconstruction of artifacts
it has already read). I run a best-effort single-window reconstruction and flag the
independence gap rather than fake it.

**Reconstruction (construction-window, behavior in user language):** "A system built
to this plan will, when an order is submitted to be charged: create exactly one
Stripe charge even if the submit is retried, sent twice concurrently, or if Stripe
re-notifies us twice. It will NOT tell the caller 'paid' immediately — it says
'pending' and only marks the order paid when Stripe's webhook confirms. If anything
is ambiguous, it refuses rather than risk a second charge. A genuine decline fails
the order, but a later success can still flip it to paid."

**Reconstruction (verification-window, from tests only):** matches on the three
exactly-once guarantees, the no-sync-PAID rule, ambiguous→REFUSED, orphan→park,
FAILED→PAID reversibility. **A↔B agree** — no `TEST-DESIGN-SPLIT` surfaced (caveat:
same agent read both, so a correlated blind spot would NOT surface; this is the
weakness SC-G5 names).

**Adversarial comparison to intent (4b) — wrong-but-plausible realizations per MNF:**

- **R-007.1 adversarial:** *"A realization that passes test_acceptance_retry.py but
  double-charges in reality": a client that does NOT reuse the idempotency key on
  retry.* The plan's D-P1/L-5 **pushes the dedup-correctness obligation onto the
  client** ("client MUST reuse the key on retry"). The acceptance test always reuses
  `"req-key-1"`, so it **passes by construction while the real-world failure mode (a
  client that forgets) is untested and unguarded by the server.** Combined with GAP-2
  (TTL 24h–7day window relies on our store alone), **R-007.1's MNF adequacy is NOT
  fully pinned.** `DRIFT-R-007.1` → force-expand to human. This is the strongest
  semantic-drift finding: the test green-lights a guarantee the *server alone* does
  not provide.
- **R-007.2 adversarial:** a realization that dedups on event-id but acts before the
  signature check, or that treats two *distinct* event-ids for one intent as two
  transitions. The tests cover both (forged→rejected; two-distinct-events→once). **MNF
  adequately pinned.** ✓
- **R-007.3 adversarial:** the dread case — a realization that passes against a
  *single-threaded fake store* and double-charges under real concurrency. The test
  authors anticipated exactly this (`test_concurrency_used_real_store` asserts
  `store.is_atomic_concurrent`). **BUT** the plan itself states R-007.3 has had
  **zero end-to-end validation** (RISK-8); the guard is a contract, not a proven fact,
  and the test will SKIP until an executor wires a real store. **MNF mechanism is
  *specified* adequately but *unproven*.** `SILENT-ASSUMPTION-R-007.3` → force-expand.

**Per-MNF adequacy statements (Check 4b mandatory output):**
- R-007.1: failure-path test replays the same key N times, asserts `intent_count()==1`.
  Mechanism: request-key claim on `req:`. **Adequacy: PARTIAL** — server-side guard is
  real, but the guarantee leaks to a client obligation + a TTL window (DRIFT above).
- R-007.2: delivers same event-id twice, asserts one transition + `DUPLICATE_NOOP`.
  Mechanism: claim on `evt:`. **Adequacy: SOUND.**
- R-007.3: fires two concurrent submits same purchase, asserts one ACCEPTED + one
  REFUSED + `intent_count()==1`, guarded by a real-store assertion. Mechanism: atomic
  claim on `purchase:`. **Adequacy: SOUND BY DESIGN, UNVALIDATED IN FACT (RISK-8).**

---

## 7. Check 5 — Evidence-specificity

Each MNF/outcome above carries a concrete, falsifiable behavioral claim (an
input→output pair: "same key ×N → intent_count==1"), not a topic label. **PASS.**
The acceptance tests themselves are the falsifiable contract and they are specific.
(One weakness: R-014 "no raw PAN" is asserted only as a harness convention, not a
hard test that drives RAW_PAN through and asserts rejection — thin but present.)

---

## 8. Check 6 — Coherence / cross-module shared-assumption scan

Whole-portfolio read for cross-module assumptions that conflict with NO linking ID.

**Verdict: two real contradiction-pairs surfaced (both already caught by L2 — good,
but they confirm the check earns its place).**

- **COH-1 (intake ↔ orders): `mark_failed` terminality.** intake (D-P6) assumes it
  can call `mark_failed` synchronously AND that a late webhook can still drive PAID.
  orders' contract models FAILED as **terminal** (no FAILED→PAID edge). These two
  modules independently assume incompatible state machines. L2 caught it (L-7 counter)
  but it is **only PROVISIONALLY-LOCKED — orders is an un-designed area that has not
  confirmed.** Live contradiction until orders L3 exists. (Confirms the gate's "no
  shared ID" point — the conflict is between two modules, mediated by no requirement.)
- **COH-2 (payments ↔ substrate): TTL alignment.** payments assumes the store holds
  `req:` keys ≥7 days; Stripe's own idempotency key expires at 24h. Between 24h and
  7 days **only our store guards R-007.1** — a cross-layer shared-assumption gap
  (GAP-2). Conscious-accepted by L2 (L-5) but it is a coherence hole between two
  layers' retention assumptions. Surfaces here as well as in §4.
- **COH-3 (payments internal): ChargeRecord shared mutable state (RISK-1).** intake
  writes, webhooks updates — the one place the area is not tree-shaped. A
  write-ordering/visibility bug = R-008 divergence. Assumption "PENDING→CONFIRMED is
  monotonic & claim-gated" is load-bearing and asserted, not proven. Flag.

---

## 9. Check 7/8 — L1 triage + the HUMAN sign-off package

**L1 triage routing (with the conflict-of-interest fences applied):**

| Finding | Type | Disposition | Can L1 clear? |
|---|---|---|---|
| THIN-1 (R-006/R-008 L1-derived) | atomization | **surface to human** | **NO — intake/atomization, un-clearable** |
| THIN-2 (R-007 decomp unconfirmed) | atomization/MNF | **surface to human** | **NO — MNF, un-clearable** |
| GAP-2 / DRIFT-R-007.1 (TTL + client-key) | MNF residual | **surface to human, force-expand** | **NO — MNF defect** |
| RISK-5 / ORPHAN-2 (async-only) | scope-shift | confirmable question to human | needs parallel-L1 co-sign (product-visible) |
| RISK-8 / R-007.3 unvalidated | MNF adequacy | **surface to human + block build until validated** | **NO — MNF** |
| GAP-FWD-1/2 (R-001/R-003 untested) | coverage gap | route to owning level (L2/L3) | yes (not MNF) |
| ORPHAN-1/3/4 (un-birthed `D-`) | scope/process | promote to approved `D-` records, route | yes, mechanical |
| COH-1 (mark_failed) | coherence | block on orders confirm | needs counterparty area |
| Check 1 FAIL (no trace-blocks) | structural | **gate cannot generate RTM** | **NO — gate input defect** |

**The single digestible human package (the warm diff against the signed brief):**

1. **Playback (three-column, MNF force-expanded):**
   - *R-007.1 "retry → once":* plan claims it; test asserts `intent_count==1` on key
     replay; intent span "never double-charge / network retry → once." **But the
     guarantee depends on the client reusing its key and on our 7-day store (Stripe's
     own key dies at 24h). Confirm you accept that the *server alone* does not catch a
     client that forgets its key.** (DRIFT-R-007.1)
   - *R-007.2 "dup webhook → once":* claim/test/intent all align. SOUND.
   - *R-007.3 "concurrent → once":* claim/test/intent align **but the mechanism has
     never been run end-to-end; first real proof is deferred to build.** Confirm you
     accept building on an unvalidated concurrency guard, with a mandated real-store
     test as the gate.
2. **Findings ledger (deltas, one line each, with proposed disposition):** as the table
   above — each phrased as a confirmable question ("plan would do X for R-007.1; you
   asked for Y — confirm?").
3. **MNF roster (force-expanded, never collapsed):** R-007.1 (PARTIAL adequacy),
   R-007.2 (SOUND), R-007.3 (SOUND-by-design / UNVALIDATED) with the one-line adequacy
   statements from §6.
4. **Residual (point the human at the fuzzy zones):** R-006/R-008 are L1-derived and
   unconfirmed; async-only submit is a product-visible behavior change; the
   decomposition itself is unconfirmed; orders + substrate are un-designed so two locks
   are provisional; GAP-2 is an accepted-but-unconfirmed residual.

**Green-collapse:** NOTHING is collapsed. All MNF rows force-expanded; every
remaining decided requirement either has an open gap (R-001, R-003) or rests on an
unconfirmed L1-derived/provisional foundation. No requirement met the bar
("fully covered AND a falsifiable reconstruction claim the adversarial comparator
tried to break and could not") — R-007.2 is the closest and even it sits under the
unconfirmed R-006 webhooks foundation.

---

## 10. SHORTCOMINGS — where the gate lacked what it needed (highest-value section)

These are defects in the **gate design + the plan-as-handed-to-the-gate**, recorded
per the dry-run's primary purpose. Format: severity / blocks-build / where.

- **SC-G1 — [BLOCKER] The plan did not carry per-element machine-readable
  trace-blocks; the RTM is not generatable.** *(where: all of L2/L3/L4 + the gate's
  Requirements-Traceability section).* The gate's #1 mechanical input — "RTM-builder
  harvests every trace-block and joins them mechanically" — has nothing to harvest at
  element granularity. Levels emitted whole-file `intent_ids:` front-matter and prose
  tables instead of the mandated per-module / per-design-element / per-task /
  per-test trace-blocks. The "generated, never maintained, complete-by-construction"
  property fails; I hand-built the RTM, which is the exact RTM-tax + skippability the
  gate claims to design away. **This is the design gap the run exists to find: the
  gate specifies a return-contract hook to enforce trace-blocks, but the upstream
  role docs do not show the levels HOW to emit them, so they didn't.**
- **SC-G2 — [BLOCKER] `D-` derived-requirement discipline was never followed; the
  backward-coverage check is structurally blind.** *(where: L3 area-design D-P*
  records + the gate's "Sanctioned ID birth" rule).* The plan's `D-P1..6` are design
  *decisions*, not the gate's `D-<intent-id>` *derived-requirement* records. Every
  net-new element (ChargeRecord, ACCEPTED_PENDING, purchase-key, dead-letter sink) was
  born without a sanctioned record. A deterministic Check 3 would flag all of them as
  ORPHAN/scope-creep; only human prose-reading rescues them. The gate and the
  operational docs **collide on the meaning of `D-`** (decision-record `D-Pn` vs
  derived-requirement `D-<id>`) — a naming/namespace clash that must be resolved
  before build, or backward-coverage cannot run mechanically.
- **SC-G3 — [HIGH] Dotted-ID hierarchy stops at intake; subtree re-gating is
  impossible.** *(where: ID minting + Re-Gating section).* No L2/L3/L4 child IDs are
  minted (`R-007.1` is reused verbatim at every level). The gate's entire
  incremental-re-gating mechanism ("all checks keyed by dotted-ID prefix",
  "re-run over the touched subtree") has no subtrees to key on. A FAIL here can only
  re-gate the whole plan — the exact cost-blowup the gate says makes it skippable.
- **SC-G4 — [MEDIUM] The gate has no story for non-functional / `-ility`
  requirements.** *(where: Check 2 forward-coverage).* R-003 ("keep it cheap") is
  `decided` but un-testable as written; Check 2 hard-fails any decided req with an
  empty test column, so a legitimate cost constraint forces a FAIL or an awkward
  exemption the spec never grants. R-002 ("Stripe") is similar — realized but not
  test-pinnable. The gate needs a typed category for constraints that are *honored by
  construction* vs *verified by test*.
- **SC-G5 — [HIGH] Two-window blind reconstruction is un-runnable by a single agent;
  the dry-run cannot exercise the gate's central anti-theater property.** *(where:
  Check 4a/4b + the "structural independence" claim).* The gate's core defense against
  correlated blind spots is two clean-context agents that never share lineage plus a
  separate adversarial comparator — i.e. real spawn infra. In this finishing-pass
  simulation one agent read every artifact, so the A↔B "agreement" I report is
  worthless as independence evidence. The design is sound; the **dry-run cannot
  validate it**, which means the property is unproven until the spawn infra exists.
  Named, not faked.
- **SC-G6 — [HIGH] The gate's hardest gating items are PENDING-on-human-confirmation
  that the simulation cannot produce — so the gate cannot actually terminate.**
  *(where: Check 0 THIN-1/2, Check 8, intent-spec reflect-back status).* R-006, R-008,
  the R-007 decomposition, the async-only consequence, and GAP-2 all require the
  *user* to confirm, and the spec's reflect-back is explicitly PENDING ("no live user
  turn"). The gate is *correctly* refusing to PASS — but it means the entire plan was
  assembled and frozen (interfaces-locked.md) on top of **unconfirmed L1-derived
  foundations**. The build cycle would inherit a frozen contract whose webhooks branch
  could be invalidated by a single "no" at reflect-back. The gate caught it; the
  process gap is that freezing happened *before* the gate, not after.
- **SC-G7 — [MEDIUM] MNF "presence of a negative test" passes Check 2 even when the
  test SKIPs.** *(where: Check 2 + build/payments).* All 17 acceptance tests currently
  SKIP (no `payments_impl`). Check 2 counts a test as "present" structurally, and the
  MNF adequacy in Check 4b reasons about the test *text*, not a green run — so a slice
  with zero passing tests can satisfy the design-cycle gate. That is arguably correct
  (the gate is pre-build), but the gate spec never says "tests are authored-and-frozen
  but not-yet-green at gate time," leaving ambiguous whether a SKIP-ing suite is
  acceptable evidence. It should be made explicit.
- **SC-G8 — [MEDIUM] The gate inherits a PARTIAL lock and has no rule for gating an
  incompletely-reviewed plan.** *(where: interfaces-locked.md Part C + the gate's PASS
  definition).* Two interfaces (L-4 substrate, L-7 orders) are only
  PROVISIONALLY-LOCKED because their counterparty areas are un-designed. The gate
  design assumes a fully-assembled plan; it has no defined verdict for "the plan is
  internally coherent but half its interfaces await counterparty confirmation." The
  honest verdict is FAIL/BLOCK, but the gate spec doesn't enumerate this state.
- **SC-G9 — [LOW] No coverage check on the ID→intent-span map's own completeness.**
  *(where: Check 0).* Check 0 audits prose→ID (did every obligation get minted) but
  nothing audits ID→span (does every minted ID actually have a non-empty verbatim
  span). Here the map is good, but the check is one-directional by spec.
- **SC-G10 — [LOW] R-013 (refund) is parked but is in R-007's blast radius with no
  gate tripwire.** *(where: Check 0 THIN-3).* "Never double-charge" plain-language
  arguably covers refund-then-recharge; the slice correctly defers it, but nothing in
  the gate flags that a *future* slice re-opening R-013 must re-enter the human MNF
  gate. Post-PASS-changes covers element edits, not deferred-requirement activation.

---

## 11. Gate verdict (final)

**FAIL — routed, batched into one verdict, one round of kickbacks:**

- **To L2/L3/L4 (mechanical/structural):** emit the missing per-element trace-blocks
  (SC-G1); convert `D-Pn` decisions into sanctioned `D-<id>` derived-requirement
  records or rename to resolve the namespace clash (SC-G2); mint dotted child IDs so
  re-gating can scope (SC-G3); add tests or a typed exemption for R-001/R-002/R-003
  (GAP-FWD-1/2, SC-G4).
- **To the HUMAN (un-clearable, force-expanded):** confirm/reject R-006 & R-008;
  confirm the R-007 decomposition; accept/reject async-only submit (RISK-5); accept
  the R-007.1 client-key + TTL residual (GAP-2/DRIFT-R-007.1); accept building on the
  unvalidated R-007.3 guard with the real-store test as the build gate (RISK-8).
- **To counterparty areas (block):** orders must confirm FAILED→PAID (L-7/COH-1);
  substrate must confirm purchase-keyspace atomicity + TTL (L-4/COH-2).
- **Gate-self (blocks the gate itself):** SC-G5 (two-window independence) and SC-G6
  (human confirmations) mean *this* gate run is itself partial — it cannot emit a true
  PASS even if all routed defects were fixed, because the irreducible human turn and
  the independent-reconstruction infra do not exist in the simulation.

**No PASS. The build cycle is NOT unlocked.** The plan's *content* is strong; the
plan's *gate-instrumentation* (trace-blocks, `D-` discipline, dotted IDs) and its
*confirmation state* (reflect-back PENDING, partial lock) are what fail the gate —
which is precisely the class of defect this finishing pass existed to surface.
