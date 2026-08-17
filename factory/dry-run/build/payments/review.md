---
artifact: L5+ review report (M52) — independent second reading against frozen spec
seat: payments#review (Opus / Claude Code — different runtime from the L5 executor, per judgment-diversity)
reviews: payments_impl.py (authored by L5 = real Codex/GPT-5.5 via mcp__codex__codex)
against: build/payments/acceptance.md gate rubric + the 4 frozen test files + interfaces-locked.md (L-1..L-7)
verdict: ACCEPT WITH NOTED CONCERNS (R-007 faithfully implemented; two safe-direction fidelity gaps the frozen tests do not catch)
created: 2026-06-02
---

# L5+ Review — Payments slice (R-007 exactly-once)

## Method
Did NOT trust the executor's green bar or its report. Re-ran `python3 -m unittest`
(17/17 pass, confirmed independently). Then inspected the wiring against the gate
rubric in `acceptance.md`, and ran three adversarial probes beyond the frozen
suite (non-succeeded webhook, ambiguous-then-retry, distinct-event reconfirm).

## Gate rubric verdict (acceptance.md §"Gate rubric")
1. **All four files pass** — ✓ confirmed independently (17/17).
2. **Concurrency store genuinely atomic** (the load-bearing one) — ✓ **PASS, not
   theater.** `AtomicIdempotencyStore.claim()` (payments_impl.py:85-90) guards the
   full check-and-set in a single `threading.Lock`. Two racing
   `claim("purchase:...")` cannot both get FRESH. `is_atomic_concurrent=True` is set
   only on this real store. The test's `test_concurrency_used_real_store` guard is
   honestly satisfied.
3. **No positive outcome without prior ChargeRecord(PENDING) (CI-3)** — ✓ verified
   by reading the orchestration path: `create_pending()` (line 231) precedes
   `create_payment_intent()` (line 238) unconditionally.
4. **submit never returns synchronous PAID (L-2/RISK-5)** — ✓ submit only ever
   returns ACCEPTED_PENDING/ALREADY_ACCEPTED/REFUSED/FAILED; PAID is set only in
   `deliver_webhook` (line 278).
5. **PAN/token (R-014)** — ✓ gateway records only the supplied token; tests never
   pass a PAN and none can reach `seen_tokens`.
6. **Tentative findings first-class (D29)** — honored: my two findings below are
   stated with confidence, not collapsed to a pass.

## R-007 faithfulness (the must-never-fail)
- **R-007.1 retry → once:** ✓ `req:` claim replays ALREADY_ACCEPTED with same
  intent_id; one Stripe intent. Defense-in-depth Stripe-key dedup present.
- **R-007.2 dup webhook → once:** ✓ `evt:` claim; second delivery DUPLICATE_NOOP;
  PAID once; signature verified before any state change.
- **R-007.3 concurrent → once:** ✓ atomic `purchase:` claim; winner ACCEPTED_PENDING,
  loser REFUSED; exactly one intent. **This is the scariest case (RISK-8) and it is
  honestly exercised against a real atomic store here for the first time in the run.**
- Cross-cutting: ambiguous → REFUSED (never positive); orphan → PARKED_ORPHAN
  (never fabricated PAID); sync FAILED reversible to PAID by late webhook; PAID
  always traces to a CONFIRMED record. All ✓.

**Conclusion: R-007 is faithfully implemented.** In every path I probed,
`stripe.intent_count()` never exceeds 1 and no order reaches PAID without a backing
CONFIRMED record.

## Concerns the frozen tests do NOT catch (recorded; both fail SAFE)

- **REV-1 (medium) — Stripe idempotency key derived from purchase_key, not the
  domain/request key.** `_stripe_idempotency_key(purchase_key)` (line 241) keys the
  gateway dedup on `(order_id, amount)`, whereas locked CI-2 / brief D8 say the
  Stripe key is a deterministic function of the **domain_idempotency_key**. In this
  slice the request-guard already dedups same-key retries before the gateway, so the
  divergence is masked and the tests pass. But it conflates R-007.1's dedup axis with
  R-007.3's purchase axis at the Stripe layer — exactly the ADR-004
  "three-distinct-guards" smell. Errs safe (never *more* than one intent), but it is
  a spec deviation the executor reported as "no contradiction."
- **REV-2 (low/medium) — non-succeeded webhook consumes the `evt:` key before the
  status check.** `deliver_webhook` claims `evt:{id}` (line 268) prior to the
  `status=="succeeded"` branch (line 276). A `processing`/`requires_action` event
  returns DUPLICATE_NOOP after burning the event id; if Stripe later **redelivers
  the same event id** with a terminal status, it is swallowed as a duplicate. Real
  Stripe redelivers the same event id, so this is a genuine reconciliation gap. It
  fails SAFE (under-confirms, never double-charges) and the frozen tests never send a
  non-succeeded status, so it is invisible to the gate.

## Note on the executor's report (L4-relevant signal)
The L5 report says **"Escalations: None … no contradiction between the frozen tests,
brief, and acceptance.md."** Independent review found one spec deviation (REV-1) and
one robustness gap (REV-2). Neither was surfaced. Per the L4/L5 role docs, a report
claiming zero concerns beyond housekeeping is itself a signal — the executor built a
correct-to-the-tests artifact but did not self-detect where it diverged from the
locked contract. This is the predicted GPT-5.5 failure mode in miniform: it made the
frozen tests pass faithfully and literally, but did not flag that its Stripe-key
derivation contradicts D8/CI-2 because nothing in the *tests* forced it to. The
tests anchored the work; they did not anchor contract-fidelity beyond what they
assert. That is the real finding of this dry run.

## Verdict
**ACCEPT** for the R-007 must-never-fail (faithfully implemented, concurrency proven
against a real atomic store). **REV-1 should bounce** in a non-dry-run: align the
Stripe key with the domain key per CI-2/D8, or amend the contract if the
purchase-key derivation is actually intended (escalate-don't-decide — it is a
contract question, not L5's call). REV-2 is a follow-up hardening item.
