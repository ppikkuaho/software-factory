# ADR-001 — Establish a named substrate context, built first

- **Status:** decided
- **Intent IDs:** R-007, R-007.1/.2/.3, R-003
- **C4 altitude:** system

## Decision
Establish a platform/foundation **substrate** context (Money, IDs,
DomainEvent/audit log, IdempotencyPrimitive, Clock) explicitly, and build it
first via the walking skeleton. The substrate is the stable core every feature
area depends inward toward; it is **not** a peer feature module.

## Rationale
The R-007 obligations and money handling cut across intake, gateway, and
webhooks. Smearing idempotency/money/IDs through each feature area would
duplicate the very mechanism the MNF rests on and create divergent
implementations of "exactly once." DECOMPOSITION-METHODOLOGY Part I.8 (B14)
mandates naming a cross-cutting concern as a substrate and building it first.
Dependencies then point toward a stable sun; nothing volatile sits at the
center.

## Consequences
- All areas import substrate value types and depend on substrate ports.
- The walking skeleton stands the substrate up first (PROJECT-PLANNING Phase 2).
- A wrong substrate interface is expensive — so its ports are pressure-tested by
  planning-L3s before freeze.
