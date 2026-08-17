# ADR-002 — Money is integer minor units + ISO-4217 currency

- **Status:** decided
- **Intent IDs:** R-005, R-007, R-008
- **C4 altitude:** component (substrate value type)

## Decision
Represent money as a value type carrying an integer count of minor units (e.g.
cents) plus an ISO-4217 currency code. No floating point anywhere in the charge
path. Arithmetic, equality, and formatting live on the type.

## Rationale
Stripe charge amounts are integer minor units; float money introduces silent
rounding divergence that, in the R-007/R-008 blast radius, can manifest as a
charge that doesn't equal the order total (a flavour of money/order divergence).
Cross-module: every area that names an amount uses the same type, so this is an
L2-level decision, not a per-area one.

## Consequences
- Amount mismatches become type-level / equality-level, testable.
- Currency mixing is caught at the type boundary.
