## The L5+ Gate Report Contract — `reviews/<gate-id>/gate-report.md`

Your `reviews/<gate-id>/gate-report.md` is the parent-facing review deliverable. The producer's
node-root `report.md` remains candidate evidence and must not be overwritten. The runtime
return-contract gate (E2) REFUSES a DONE sign-off whose review gate lacks a non-empty gate artifact:
the signal stays on disk, a typed defect lands in your inbox, and you must fix and re-signal. Do not
discover this at sign-off — the report is work, not paperwork; write it before your terminal signal.

- **Follow the L5+ template:** `operational/shared/templates/report-template.L5+.md` — the
  registered reviewer adaptation of the shared report template (typed header, one page,
  pointer-not-payload, `comms-protocol.md`). Your per-criterion verdict table IS the gate
  artifact (M52); the verdict is restated in your terminal signal's `evidence.notes`, with
  `evidence.gate_artifact` and `evidence.producer_artifact` pointers.
- **Cite the requirement IDs you VERIFIED as BARE references** (`R-003.2.1`) — a reviewer does
  not discharge requirements, it verifies them; the IDs come from the same frozen
  `brief.md`/`acceptance.md` the executor was held to. Never re-declare trace stanzas (see the
  trace-discipline block). The E2 gate enforces the citation mechanically for L5-class seats —
  YOURS INCLUDED: both Run-2 L5+ reviewers tripped this check because no reviewer-facing doc
  carried the duty. A review naming no ID it verified is unverifiable itself.
- **Describe trace syntax in words if needed.** Gate reports cite IDs; they do not contain trace
  comment examples, including in prose or code fences. Keep trace declarations in the declaring
  artifacts.
- **Account for every given criterion:** PASS, or FAIL with the named defect (file, behavior,
  violated requirement ID) — a vague bounce ("needs polish") is worse than no bounce.
