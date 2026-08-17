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
