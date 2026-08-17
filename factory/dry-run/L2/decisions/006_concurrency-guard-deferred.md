# ADR-006 — Concurrency guard mechanism: DB uniqueness constraint (deferred to L3 as constraint)

- **Status:** deferred (decided downward as a constraint; the D26 rubric L3 is held to)
- **Intent IDs:** R-007.3
- **C4 altitude:** component (intake-internal)

## Decision (the constraint, not the implementation)
R-007.3 (two concurrent submits → exactly one charge) is realized by a
**uniqueness guarantee on the purchase identity**, enforced at the durable-store
level (e.g. a unique constraint / conditional write / `SELECT ... FOR UPDATE`),
NOT by application-level check-then-act. The exact mechanism is **deferred to the
substrate/intake planning-L3** because it depends on the concrete datastore
(ADR-010, deferred). 

## The constraint the L3 is held to (D26)
1. The guard must be **race-correct under true concurrency** — no
   check-then-act window where two requests both pass the check.
2. On contention, the loser is **rejected or coalesced, never charged a second
   time** (T-3 safe-failure direction; ADR-007).
3. The mechanism must be expressible on the chosen cheap datastore (ADR-010) —
   if it cannot be, escalate upward (the datastore choice and this guard
   co-constrain each other).
4. Negative test required: two concurrent submits for the same purchase →
   exactly one charge.

## Rationale
Last-Responsible-Moment + subsidiarity: the concrete locking primitive is
domain/datastore-deep and cheap to defer, but the *property* (durable uniqueness,
safe-failure on contention) is cross-cutting and decided now. Application-level
check-then-act is pre-emptively forbidden because it is the classic race bug.

## Open dependency
This decision is entangled with ADR-010 (datastore choice). If the cheap
datastore cannot give a uniqueness guarantee cheaply, R-007.3 vs R-003 (cost)
trade off — escalate to L1.
