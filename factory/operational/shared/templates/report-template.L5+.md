# Review Report — `<node-address>#review` → `<parent-address>`

**From:** `<node-address>#review` (L5+ Independent Reviewer) · **To:** `<parent-address>`
**Type:** review-verdict
**Status:** <DONE> — **VERDICT: <ACCEPT | BOUNCE — n defects>** <one line; restate the verdict in your terminal signal's `evidence.notes`>

> L5+ ADAPTATION of `report-template.md` (registered — see `operational/shared/blocks/registry.json`):
> a reviewer does not DISCHARGE requirements — it VERIFIES them. The per-criterion verdict table
> below IS your gate artifact (QUALITY-GATE M52) when written as
> `reviews/<gate-id>/gate-report.md`. The producer's node-root `report.md` remains candidate
> evidence. One page, pointer-not-payload. Delete this note and every <angle-bracket> prompt when filling.

## Outcome
<One short paragraph: the verdict and what it rests on — what you ran yourself, what you read.>

## What was done
<Your OWN testing pass (the frozen suite re-run, the executor's unit tests, the edges you probed)
and your read of the work against the full frozen constraint set. Pointers to outputs.>

## Requirement IDs verified (per-criterion verdicts)
<BARE references only (`R-003.2.1`) — never re-declared trace stanzas. The per-criterion verdict
table against the gate rubric / frozen acceptance — every given criterion: PASS / FAIL (named
defect: file, behavior, violated requirement ID). This table is the gate artifact.>

| Criterion / ID | Verdict | Evidence (pointer) |
|---|---|---|
| <R-…> | <PASS/FAIL> | <where you saw it> |

## Verification evidence
<Pointers, not payload: which suites YOU ran (results), frozen artifacts confirmed unmodified,
what you probed beyond the suites. If you cite an exit code, use the command run that preserved the
original command status directly. Piped or truncated output excerpts are fine for readability, but do
not use them as exit-code evidence unless you separately captured the original status.>

## Deviations & concerns
<On BOUNCE: every named defect. On ACCEPT: the honest concerns that remain — a review reporting
zero concerns either didn't look or didn't think.>

## Sign-off checklist
- [ ] Every `plan.md` task checked, or explicitly deferred with reason
- [ ] Anything reviewed beyond the brief is listed above
- [ ] Requirement IDs cited as bare references (no re-declared stanzas)
- [ ] Verdict restated in the terminal signal's `evidence.notes`
- [ ] Current `gate_id`, `producer_artifact`, and `gate_artifact` included in the terminal signal
      when the sign-off brief provides them
- [ ] No ancestor/project `log.md` was edited; required review evidence is in this gate report and terminal signal

*Template: `operational/shared/templates/report-template.L5+.md` (doc-system; adapts `report-template.md` — see `design/DOC-SYSTEM.md`).*
