## The Report Contract — `report.md` Required at DONE, Every Level

Your `report.md` is the parent-facing deliverable, required at DONE at EVERY level — the root
included. The runtime return-contract gate (E2) REFUSES a DONE sign-off whose node lacks a
non-empty `report.md`: the signal stays on disk, a typed defect lands in your inbox, and you must
fix and re-signal. Do not discover this at sign-off — the report is work, not paperwork; write it
before your terminal signal.

- **Follow the shared template:** `operational/shared/templates/report-template.md` — typed header
  (From/To/Type/Status), one page, pointer-not-payload (`comms-protocol.md`). The detail lives in
  the artifacts the report points at, never pasted into it.
- **Cite the requirement IDs given in your `brief.md`/`acceptance.md` as BARE references**
  (`R-003.2.1`) — never as re-declared trace stanzas (see the trace-discipline block). A report
  naming no ID it discharged is incomplete: the level above you cannot confirm fidelity against an
  unstated target. For L5-class seats the E2 gate enforces the citation mechanically; at every
  level it is the contract. When your own artifact declares new artifact IDs, such as acceptance-test
  IDs, those IDs serve the given requirement IDs. Your `report.md` still cites the inherited
  requirement IDs bare.
- **Describe trace syntax in words if needed.** Reports cite IDs; they do not contain trace comment
  examples, including in prose or code fences. Keep trace declarations in the declaring artifacts.
- **Account for every given ID:** discharged, deferred (with reason), or escalated — a silently
  dropped ID resurfaces as an ownerless coverage gap.
