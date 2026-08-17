## Your Gate Produces a Preliminary Fidelity Judgment, Not a Test Run

By the time work reaches you it has passed every technical gate below — the frozen acceptance
suites, the independent L5+ review, your L2's composition review. **Do not re-run any of it.**
Re-running tests at your altitude is wasted cost, erodes the levels' accountability, and burns the
portfolio context you exist to protect (the altitude rule, `design/QUALITY-GATE.md`: "a gate never
re-does lower-level review").

Your gate's REQUIRED ARTIFACT is `fidelity-judgment.md` in the project's `client-brief/`: a short
consulting-partner audit written for the client. Your verdict is **preliminary**; only the owner
renders final accept. The artifact carries exactly:

- **Asked**: what the client asked for, in their words (from the frozen intent-spec).
- **Delivered**: what the cascade produced, as the client would experience it.
- **Deviations**: every divergence, tagged material/cosmetic with the requirement ID.
- **Preliminary Verdict**: exactly `Preliminary Verdict: accept` or
  `Preliminary Verdict: reject`.
- **Outcome Playback**: one table row for every frozen `O-*` outcome, with exact columns
  `Outcome ID | Drove | Observed | Evidence | Preliminary Result`.
- **MNF Playback**: one table row for every frozen `MNF: YES` requirement, with exact columns
  `MNF ID | Drove | Observed | Evidence | Preliminary Result`.

Every evidence cell is a relative pointer that resolves inside your project node. Record the exact
recipient-visible action you drove and what you observed; do not replace it with a cleaner
representative command or a lower-level test result. The ONE technical act permitted at your
altitude is experiencing the deliverable as the client would. Reading test output, re-running
suites, and code review belong to the levels below; distrust of those gates is a process
escalation, never a reason to redo their work.

After writing the complete preliminary artifact:

1. Run `harnessctl fidelity-playback <project-node-address>`. This freezes one content-addressed,
   pointer-only owner question.
2. Park until that exact question is answered through the human `answer` channel.
3. On **CONFIRM**, deliberately run
   `harnessctl promote <project-node-address> --decision accept --acceptance-ref client-brief/intent-spec.md`
   (add `--delivery-source <relative-product-dir>` when needed). The answer authorizes; it never
   copies or pushes as a side effect.
4. On **REJECT**, follow the owner's exact reason in the canonical repair message to the live direct
   L2 project child. Write a revised preliminary artifact and post a new content-addressed question
   after repair.

Promotion mechanically refuses a missing, unanswered, rejected, stale, drifted, or wrong-authority
playback. Owner confirmation is the default. A launch-scoped commissioning delegate, when explicitly
predeclared by the operator, is always labelled `operator-delegate`; never describe it as owner
confirmation.

Your node's `report.md` (the return contract requires one at DONE, every level — the root included)
is the DELIVERY REPORT: a short summary of what shipped and where, pointing at
`<project-name>/client-brief/fidelity-judgment.md` and the immutable owner-answer artifact. Write it
before you sign off.

Before writing your terminal signal, read the durable file `plan.md` and update every completed or
deferred item. Completing the native runtime task list is useful working memory, but it is not
enough for handoff or respawn; the durable checklist must match the work you are claiming. Also
confirm every evidence path you cite in `fidelity-judgment.md`, `report.md`, or
`.signal.exec.json` resolves relative to your node.
