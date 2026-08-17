# L5 Claim Account — `<node-address>#<seat>` → `<parent-address>`

**From:** `<node-address>#<seat>` (<level> <role>) · **To:** `<parent-address>`
**Type:** task-complete
**Status:** <DONE | FAILED> — <one line: the terminal state of the claim as the recipient experiences it>

> L5 ADAPTATION of `report-template.md` (registered — see
> `operational/shared/blocks/registry.json`). One page, pointer-not-payload. Delete this note and
> every <angle-bracket> prompt when filling.

## Outcome
<What recipient-visible claim exists now and where its artifact lives.>

## What was done
<The bounded implementation or acceptance-package work, with artifact pointers.>

## Requirement IDs discharged
<BARE references only. Account for every ID given in brief.md/acceptance.md as discharged,
deferred with reason, or escalated.>

## Verification evidence
<Pointers to preserved results, gate evidence, and the live scenario. Green checks are supporting
evidence; the drove-and-watched section below owns direct proof.>

## Verification Commands
<Exact commands actually run, one per line or in a fenced block, including working directory and
environment. State the expected collection/result shape. In test_author mode include the red and
scenario commands recorded under tests/.>

## Drove and Watched
<What real artifact/scenario you personally drove, on what disposable instance, and the exact
recipient-visible behavior you observed.>

## Inferred
<Claims supported by tests, types, code reading, or cited evidence but not directly watched.>

## Residual Uncertainty
<What was not exercised and where this result may still surprise the recipient.>

## Inventory
<Out-of-claim findings for L4 to triage. Do not fix them in this diff unless they break a promise
on the claim's face or an existing entry point on a state this increment created. Write `None`
only after looking.>

## Deviations & concerns
<Every authorized divergence from the brief, tagged material/cosmetic, with its requirement ID.>

## Sign-off checklist
- [ ] Every item in durable `plan.md` is checked or explicitly deferred
- [ ] The artifact's face contains no unproved promise
- [ ] Direct observation is separated from inference
- [ ] Inventory was filed for L4, not silently fixed
- [ ] Requirement IDs are bare references, never re-declared stanzas
- [ ] No ancestor/project `log.md` was edited

*Template: `operational/shared/templates/report-template.L5.md` (doc-system; adapts
`report-template.md` — see `design/DOC-SYSTEM.md`).*
