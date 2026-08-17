# Hypothesis Tree Research System — repository instructions

This directory is the factory's research root. Its subject is the sibling `../factory/`
directory; units may touch that subject only on `ht/*` branches.

## Orient and layout

- **Canonical design layer:** `system/notes/RESEARCH-SYSTEM-*.md`; the reading order is in
  `system/notes/README.md`. The notes are authoritative: where code and notes disagree,
  the notes win.
- Repository machinery lives under `system/`: notes, schemas, instruments, roles, and the
  `ht` write gate. Reusable interpretation rules live at `readout/INTERPRETATION.md`.
- `ht root init` creates runtime state directories such as `trees/`, `ledger/`, `readout/`,
  `worktrees/`, and `var/`. They are not source contents. Private transcript `fixtures/`
  are also absent from this public snapshot.

## Write rules

- **State mutations go through `ht` only** (D7: invariants live in machinery).
  Never hand-edit JSON under `trees/`, `ledger/`, or `readout/` — the pre-commit
  hook rejects out-of-band writes.
- Role identity via `HT_ROLE` env (`director` | `verifier` | `unit` | `user` |
  `harness`); the tool enforces the A1 §10 field-level authority table.
- Archives (`trees/*/nodes/*/archive/`) are write-once via the tool and live
  outside git.
- Code changes (`system/`) are ordinary development — run `uv run pytest` before
  committing. Once the research system is live, instrument changes are
  epoch-gated (C6 §6).
