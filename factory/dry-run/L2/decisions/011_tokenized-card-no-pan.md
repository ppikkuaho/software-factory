# ADR-011 — Stripe-hosted card capture / tokenization; backend stores no raw PAN

- **Status:** decided
- **Intent IDs:** R-014, R-002
- **C4 altitude:** container

## Decision
Card data is captured via Stripe-hosted mechanisms (Stripe Elements / Checkout /
PaymentMethod tokens). The backend handles only Stripe tokens / PaymentIntent
references — it never receives, processes, or stores a raw PAN.

## Rationale
R-014 (L1-derived from "use Stripe correctly") — keeping raw card data out of the
backend collapses PCI scope, which also serves R-003 (a system in PCI scope is not
cheap to run). Correct Stripe use makes this the default.

## Status caveat
R-014 is reflect-back PENDING (L1-derived). Low risk of rejection (it is standard
correct-Stripe practice) but flagged for completeness.

## Consequences
- `payments/gateway` operates on tokens/PaymentIntents, never card numbers.
- The submit payload (intake) carries a Stripe token reference, not card data —
  reflected in the charge interface.
