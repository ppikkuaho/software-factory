---
artifact: per-area-spec (planning-L3 brief)
area: payments/gateway
status: provisional brief for planning-L3
intent_ids: R-002, R-005, R-007.1, R-014
interfaces: contracts/payments-charge.md
adrs: 002, 007, 009, 011
created: 2026-06-02
---

# Area Spec — payments/gateway

## Scope
The hexagonal adapter to Stripe. Translate a domain `ChargeRequest` into a Stripe
PaymentIntent/charge; pass a derived Stripe Idempotency-Key; map the outcome to
the three-outcome `ChargeResult`; operate only on tokens (no PAN).

## Provisional interfaces
Implements `ChargeService` (contracts/payments-charge.md). External edge: Stripe API.

## Constraints (D26 rubric)
- **C-G1 (ADR-009, R-007.1):** Stripe Idempotency-Key = deterministic function of
  `domain_idempotency_key`. Same attempt → same Stripe key → Stripe dedupes.
  A fresh random key per retry is a defect.
- **C-G2 (ADR-007/CI-4):** if you cannot determine whether a prior charge exists
  (e.g. ambiguous Stripe error), return REFUSED_FOR_SAFETY, never a fresh CHARGED.
- **C-G3 (ADR-011, R-014):** operate on Stripe tokens/PaymentMethod refs only;
  never receive or store raw PAN.
- **C-G4 (ADR-002):** amount is Money (integer minor units); pass to Stripe as
  minor units; reject currency mismatch.
- **C-G5 (ADR-005):** treat the sync Stripe response as PROVISIONAL; the
  authoritative confirmation arrives via webhooks. Do not assume sync = final.

## Acceptance (negative tests authored before build)
- Same domain key → same Stripe Idempotency-Key (assert determinism).
- Two charges same domain key → Stripe creates one PaymentIntent.
- Ambiguous Stripe error → REFUSED_FOR_SAFETY, no second charge.

## Spike (de-risk early)
Sandbox-Stripe spike validating the idempotency-key behavior and the
ambiguous-error → refuse path, before the contract freezes.
