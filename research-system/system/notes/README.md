# Design notes — canonical home

**The canonical design layer lives here.** Single-source-of-truth is unchanged: one copy,
in-repo. The notes are authoritative; where code and notes disagree, the notes win.

Reading order:

1. `RESEARCH-SYSTEM-CONCEPT-2026-07-07.md` — ratified concept and system spine
2. The 2026-07-12 macro layer, read together:
   - `RESEARCH-SYSTEM-MACRO-ARCHITECTURE-2026-07-12.md`
   - `RESEARCH-SYSTEM-PRINCIPAL-COORDINATOR-2026-07-12.md`
   - `RESEARCH-SYSTEM-COMPOSITION-GATE-AND-RIDERS-2026-07-12.md`
   - `RESEARCH-SYSTEM-COHERENCE-AMENDMENTS-2026-07-12.md`
3. `RESEARCH-SYSTEM-BENCHMARK-EVAL-ARCHITECTURE-2026-07-13.md` — subject-system benchmark and evaluation architecture
4. `RESEARCH-SYSTEM-TREE-SCHEMA-2026-07-07.md` — node, dispatch, claim, tree, and ledger schemas
5. `RESEARCH-SYSTEM-PHYSICAL-LAYOUT-2026-07-07.md` — repository layout and git/epoch mechanics
6. `RESEARCH-SYSTEM-SEAM-FORMATS-2026-07-07.md` — dispatch, report, reference-map, and interrupt formats
7. `RESEARCH-SYSTEM-VERIFIER-PROTOCOL-2026-07-07.md` — adjudication pipeline and validation rules
8. `RESEARCH-SYSTEM-OBSERVATORY-2026-07-07.md` — per-run observatory design
9. `RESEARCH-SYSTEM-TRACE-READER-2026-07-07.md` — trace-reader design
10. `RESEARCH-SYSTEM-BUILD-PLAN-2026-07-07.md` — phased build sequence and checkpoints

The schemas in `../schemas/` are derived from the tree-schema note; at review gates they
are diffed field by field against the note text — **the note wins**.
