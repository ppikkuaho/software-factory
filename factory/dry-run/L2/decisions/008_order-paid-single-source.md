# ADR-008 — Order paid-state is single-source-of-truth in `orders`

- **Status:** decided
- **Intent IDs:** R-008, R-005
- **C4 altitude:** container

## Decision
The answer to "is this order paid?" lives in exactly one place — the `orders`
touchpoint's `OrderState`. `payments/*` areas do not keep their own parallel
"paid" flag; they drive the transition through the `orders` interface and read
paid-state from it. The order's payment state machine is `READY → PAID | FAILED`.

## Rationale
DECOMPOSITION-METHODOLOGY Part III.13: each piece of state has exactly one owning
module; a second copy of "paid" is a divergence waiting to happen and directly
threatens R-008 (paid-but-uncharged / charged-but-unpaid). Single source of truth
makes the R-008 invariant checkable in one place.

## Consequences
- `payments/webhooks` and `payments/intake` call an `orders` transition
  interface; they never write a paid flag locally.
- The R-008 invariant (money⇔order agreement) is asserted at the `orders`
  transition boundary.
