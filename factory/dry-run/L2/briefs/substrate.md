---
artifact: per-area-spec (planning-L3 brief)
area: substrate
status: provisional brief for planning-L3
intent_ids: R-007.1, R-007.2, R-007.3, R-008, R-002, R-003
interfaces: contracts/substrate-ports.md
adrs: 001, 002, 003, 006, 010
created: 2026-06-02
---

# Area Spec — Substrate (built first)

## Scope
Money value type; typed IDs incl. IdempotencyKey; append-only EventLog/audit;
the generic IdempotencyStore (claim/commit); Clock. This is the sun; build first
via the walking skeleton.

## Provisional interfaces
See `contracts/substrate-ports.md` (IdempotencyStore, EventLog) and ADR-002 (Money).

## Constraints (the D26 rubric — deferred decisions land here, not as open TODOs)
- **C-S1 (ADR-003/006, R-007.3):** `IdempotencyStore.claim` MUST be atomic under
  true concurrency — two concurrent claims on the same key cannot both return
  FRESH. Implement on a durable uniqueness primitive (DB unique constraint /
  conditional write), NOT application check-then-act.
- **C-S2 (ADR-003 SI-3):** claim→side-effect→commit must be crash-safe; define
  the recovery semantics for a crash between claim and commit.
- **C-S3 (ADR-002):** Money is integer minor units + ISO-4217; no floats; reject
  cross-currency arithmetic.
- **C-S4 (ADR-010):** runs on the cheap single relational store; if that store
  cannot give C-S1 atomically and cheaply, **escalate to L2** — do not weaken C-S1.
- **C-S5 (ADR-001 SE-1):** EventLog append-only and immutable.

## Acceptance (negative tests authored before build)
- Concurrent `claim(k)` ×N → exactly one FRESH.
- `commit` then `claim` → DUPLICATE with prior result.
- Crash between claim and commit → recoverable to a consistent state.

## Renegotiation latitude
You may renegotiate the port shapes upward if the atomic-claim guarantee can't be
expressed cleanly as claim/commit on the cheap store. This is expected.
