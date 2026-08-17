# Report — `<node-address>#<seat>` → `<parent-address>`

**From:** `<node-address>#<seat>` (<level> <role>) · **To:** `<parent-address>`
**Type:** <task-complete | workstream-complete | area-complete | project-complete | review-verdict>
**Status:** <DONE | FAILED> — <one line: the terminal state of the work as the parent will experience it>

> One page. Pointer-not-payload (`operational/shared/comms-protocol.md`): the detail lives in the
> artifacts this report points at — never pasted in. Delete this line and every <angle-bracket>
> prompt when filling.

## Outcome
<One short paragraph: what exists now, where it is, what verdict/state it carries.>

## What was done
<Compressed narrative at your altitude — what, not how. Point at the artifacts that hold the
detail (`plan.md`, child reports, design docs, code paths).>

## Requirement IDs discharged
<BARE references only (`R-003.2.1`) — never re-declared trace stanzas. Every ID given in your
brief.md/acceptance.md accounted for: discharged / deferred (reason) / escalated.>

## Verification evidence
<Pointers, not payload: which suite/check ran, where its output lives, which reviewer verdict
covers it. Cite gated results by reference — do not re-run lower-level verification.>

## Verification Commands
<For L5 implementation or test-author work, list the exact operative commands here, one command per
line or in a fenced command block, and state the expected collection/result shape in Verification
evidence above. For higher-level reports, include commands only when you personally ran bounded
altitude-appropriate smoke checks. If you say "I ran" a command, list the exact command actually run
or cite the transcript/probe event that preserves it; do not rewrite a representative command as if
it were the evidence. Every command you list must be runnable verbatim from the working directory you
state; if it needs a directory or environment, include that in the command itself, such as
`cd child/path && ...` or `PYTHONPATH=child/path ...`. If no direct command applies, write none with
the reason.>

## Deviations & concerns
<Every divergence from the brief, tagged material/cosmetic, with its requirement ID. Zero concerns
means you either didn't look or didn't think — there are always edges where judgment was required.>

## Sign-off checklist
- [ ] Every item in the durable file `plan.md` is checked or explicitly deferred in that file
- [ ] Anything done beyond the brief is listed above
- [ ] Requirement IDs cited as bare references (no re-declared stanzas)
- [ ] No ancestor/project `log.md` was edited; required handoff evidence is in this report and terminal signal

*Template: `operational/shared/templates/report-template.md` (doc-system; see `design/DOC-SYSTEM.md`).*
