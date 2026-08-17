## Task List First — Durable `plan.md` Next

Once your launch packet is in context, your FIRST operational act in a fresh or respawned session is
to create or refresh the native task list for your runtime (Claude Code todo list / Codex
`update_plan`). Keep it high-level and role-appropriate: the list should steer the work at your
altitude without scripting individual commands, searches, or implementation choices.

If the task-list tool is deferred or not yet callable, use only the runtime's tool-discovery action
to expose that task-list tool family first. Create the initial list from the launch seed before any
file reads, shell commands, workspace inspection, or reference lookup. The first list can be
provisional; you will refine it after bounded orientation.

Then do the bounded orientation needed to make the plan real: orient on the inbox rows, brief,
acceptance, and any immediately named task package files. Use optional reference material only for a
concrete question raised by those files. After that orientation, write or update `plan.md` in your
node: the goal in one line, then the durable checklist (template:
`operational/shared/templates/plan-template.md`). For preinstantiated forms such as `plan.md` or
`report.md`, first open the form through the runtime's file-read tool before editing so the editor
has the current file state. The final three items are ALWAYS:

1. fill `report.md` per its template — `operational/shared/templates/report-template.md`
   (an L5+ review seat uses the registered `report-template.L5+.md` adaptation)
2. verify the report cites the requirement IDs you were given (bare references)
3. sign off (write your terminal signal — `comms-protocol.md`, Terminal Signal)

Keep the runtime task list and `plan.md` aligned as you work — the file is the durable copy, the tool
is the working view.
Docs are truth: session state dies, files survive. A respawned successor inherits `plan.md` and
continues mid-list instead of re-deriving your intent (statelessness is the backstop,
`agent-lifecycle.md`). The fixed final items exist because completion bias eats end-of-work duties
stated only as prose (Run-2: seven seats signed DONE without reports and were bounced) — a
checklist whose last unchecked item is "fill report.md" structurally cannot read as done.
