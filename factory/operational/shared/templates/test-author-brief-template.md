# Test-Author Brief — `<node-address>#exec`

**For implementation task:** `<target-task-address-or-slug>`

**Purpose:** author the frozen executable acceptance package for one implementation L5 task from
the task spec and frozen criteria, or refresh an existing package after an authorized spec change.

## Inputs

- Target task spec / brief: `<path>`
- Frozen workstream criteria / rubric: `<path>`
- Parent workstream design or constraints: `<path>`
- Relevant requirement IDs: `<bare IDs>`
- Existing acceptance package, if this is a refresh: `<path or none>`
- Reason for authoring: `<initial package / spec change / clarified requirement / gate bounce / other>`
- Approval source, when required: `<L3 decision / parent-authorized clarification / not required for ordinary initial package>`

## Work

Read the target task spec and write the acceptance package the implementation L5 will be held to.
Use the spec as the authority. Write tests and rubric lines that a competent implementer can run or
apply without needing your context.

For an initial package, keep the output tightly bound to the target task spec and frozen criteria.
For a refresh, preserve valid existing coverage and change only what the spec change requires.
Call out what changed and why.

<!-- block:acceptance-package-proof v1 -->
## Acceptance Package = Executable Checks + Live Scenario + Red-Run Log

In `test_author` mode, the bindable top-level `tests/` home is one package with three required
parts:

1. executable acceptance checks, fixtures, helpers, and exact command wrappers;
2. a runnable live-scenario spine, with its exact working directory, setup, invocation, and
   recipient-visible observation documented in `tests/live-scenario.md`;
3. a non-empty `tests/red-run-log.md` recording the failing run observed for **each new check**
   before that check's later green is trusted.

The red-run log records what actually ran and failed, with command, failure/result, and the claim
surface it exercises. A hypothetical failure description is not a red run. The live scenario must
drive the artifact as its recipient will touch it; a command that exits successfully without
exercising the intended claim is not a scenario.

Take the claim only from the frozen task spec and criteria. When the claim is ambiguous, send L4 a
direct-edge message with `needs_answer: true`; do not choose the missing behavior yourself. Put the
exact operative verification commands and expected result shape under `## Verification Commands`
in `report.md`. L5+ judges whether the scenario is real and whether every new check was genuinely
observed red; the runtime walker checks only that the red-run log is present and non-empty.
<!-- /block:acceptance-package-proof -->

## Output Contract

Write the package into this node:

- `tests/` — executable tests, fixtures, helpers, or exact test-command wrappers the implementation
  L5 can run after this package passes L5+ review
- `report.md` — short pointer report naming what was authored, which IDs are covered, and any gaps

Each authored test item carries a `kind: test` trace stanza keyed to the requirement ID it
verifies. You may mint test-artifact IDs for the tests you author, such as `TST-<task>-001`; do
not mint, rename, or renumber requirement IDs. Requirement IDs from the brief are cited verbatim in
`serves: [...]`.

Use the canonical syntax from `design/PLAN-ALIGNMENT-GATE.md`. For Python tests, put the HTML
comment inside a Python comment adjacent to the test it tags, for example:

`# <!-- trace: { id: TST-<task>-001, serves: [R-001], kind: test, level: L5, node: <node>#exec } -->`

Do not implement product code. Do not rewrite the target implementation. Your result becomes usable
only after your L5+ review gate accepts it. For a post-design refresh, no-executable-tests
exception, or spec-faithfulness dispute, L3 also approves before implementation uses the package.
