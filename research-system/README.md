# The factory's research system

The **Hypothesis Tree Research System** is Software Factory's autonomous research loop.
Its subject is the L1–L5 factory in `../factory/`: it proposes changes, runs bounded
research dispatches, verifies every claim, and records only what survives adjudication.

A hypothesis tree is the control plane. A director chooses one leaf from compressed state;
a senior, junior, and checker execute one bounded unit on its own branch; and an independent
verifier alone may write epistemic state through the `ht` CLI. Verified child outcomes
propagate upward so each parent remains a truthful summary of the evidence beneath it.

## Map

- `system/notes/` — the authoritative design layer; start with `README.md` there
- `system/ht/` — the write-gate CLI and runtime
- `system/roles/` — versioned role packets and shared rendered blocks
- `system/schemas/` — tree, dispatch, claim, ledger, report, and merge schemas
- `system/instruments/` — trace reader, observatory, and composition gate
- `system/observatory/` — versioned behavioral spines screened by the observatory
- `readout/INTERPRETATION.md` — rules for interpreting generated statistics
- `tests/` — the publishable deterministic test suite

Runtime state such as `trees/`, `ledger/`, `var/`, `worktrees/`, and generated readouts is
created by `ht root init`; it is not repository content.

The factory coupling is intentional: L1–L5 seat names and factory audit-log searches refer
to the sibling system this research loop exists to improve.

This is a curated public snapshot. Dated session, ruling, and review notes and the raw
transcript fixtures under `fixtures/` are not part of it; citations to those notes or
fixtures in the design notes are provenance markers, not links to follow. Because the raw
fixtures are private, the trace-reader and observatory tests that consume them are excluded;
the instruments themselves are included.

Run the published suite with `uv run pytest`.
