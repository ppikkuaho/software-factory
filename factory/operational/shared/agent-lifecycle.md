# Agent Lifecycle — Operational Reference

How agent sessions are spawned, how they end, what happens to their state after they end, and how to recover. Loaded at boot for all levels.

The short version: an agent runs its unit of work, then **collapses on finish** to free context. A collapsed node is not gone — its full state stays **resurrectable for 2 weeks** (G37), feeding the audit layer. When an agent is *blocked* or fails review it does **not** dead-end: it keeps its context and **escalates options to its parent** (G37). Underneath all of it: **statelessness is the backstop, persistence is the optimization** (G38) — every level must survive a cold respawn from artifacts alone, and persistence only makes that cheaper.

*Decisions: G37 (collapse-on-finish + 2w resurrection/audit window; keep-context-on-block + escalate-options), G38 (statelessness backstop / persistence optimization). Siblings: `runtime-and-model-map.md`, `agent-definition-principles.md`, `comms-protocol.md`, `git-protocol.md`. Upstream: `QUALITY-GATE.md` (the bounce/escalation loop), `OBSERVABILITY.md` (the 2w window's audit/optimizer-L1 contract), `WORKSPACE-SCHEMA.md` (addresses, the work node), `COMMUNICATION.md` (the bus + docs transport).*

---

## Session Basics

Each agent is a full session on its assigned runtime — independent process, full tools, full context window, full autonomy. Most levels are **Claude Code / Opus 5.0**; **L5 is the Codex harness / GPT-5.6 Sol** (see `runtime-and-model-map.md`). You are spawned by your parent level. By the time you receive your first context everything is already loaded — you never bootstrap yourself.

Your identity is your **address**: a workspace node path plus a role-variant suffix (`proj/payments/gateway#exec`, `…#review`), per F35. The address is bound to the **work node**, not to the instance — so it is stable across respawn, and it survives collapse and resurrection. The same hierarchical-path spine that names your address also names your workspace path, your git branch, your rubric file, your requirement-IDs, and your slice of the visibility graph (see `WORKSPACE-SCHEMA.md`). One spine, six uses.

---

## How You Spawn a Child

You decompose your unit and delegate sub-units to children (an L2 spawns planning/execution-L3s (L3& / L3), an L3 spawns L4s, an L4 spawns L5s). The **work** is the point; spawning the worker is a thin administrative act on top of it. The real work is your decomposition and the artifacts you author **into each child's node** — its brief and its frozen acceptance. By the time you spawn, the child's node is already prepared; the spawn just brings it online. You do **not** create a child session directly, and you do **not** touch the ledger or the control plane — those belong to the harness daemon (the single writer).

Runtime-native background-agent tools are not harness child spawns. If you need lower-level
product work, use the two-step node + `.harness-outbox` path below; that is how the child enters
the supervision tree, receives its role, gets gated, and leaves durable evidence. If a native
`Agent` path is unavailable or denied, continue by preparing the child node and writing the outbox
request.

**Two steps: PREPARE the node, then ask the harness to spawn it.**

**Step 1 — prepare the child's node (this is the work).** Each child gets a node that is a **subdirectory of your own workspace**, named for the child. Because your write-jail covers your whole subtree, you author the child's files there directly:
- `<<child_name>>/brief.md` — the child's task, **pointer-not-payload**: the requirement IDs it owns (its responsible-ID-set, with trace-blocks), the interface contract it must honor, the constraints, and the ADRs that bridge the rationale — *referencing* the upstream design rather than copying it. Thin-but-decision-complete: enough that the worker never has to ask before starting.
- `<<child_name>>/acceptance.md` — the **frozen** acceptance tests / gate rubric, authored from the spec *before* the worker starts. For an implementation L5 task, L4 first turns the frozen criteria into an executable package through a dedicated L5 `test_author` child; that package passes L5+ review before the implementation task uses it. Post-design refresh uses the same path with `test_refresh: true` and L3 approval. Read-only to the executor once set.

**Step 2 — drop a one-line spawn-request into your own `.harness-outbox/`.** A small JSON file, one per child:

```json
{ "child_name": "parser", "child_level": "L4" }
```

That's the whole request in the normal case — no brief text. The harness brings the child online pointed at the node you prepared: it derives the child's `spec_pointer` → its `brief.md` and `frozen_acceptance_ref` → its `acceptance.md` from the node's own files.

For an L2 planning round, mark temporary planning-L3 seats explicitly:

```json
{ "child_name": "parser-plan", "child_level": "L3", "purpose": "planning" }
```

- **`child_name`** — a **leaf-name only**, not a full address. The harness composes the child's real address by appending this leaf under *your* address (`proj/widget#exec` + `parser` → `proj/widget/parser#exec`), and the child's node is `…/parser/` under your workspace. You cannot name a child anywhere except inside your own subtree — a name carrying a `/`, `..`, `#`, or whitespace is **refused**. Use a short lowercase slug (`a-z 0-9 . _ -`).
- **`child_level`** — the level the child runs at (`L2`…`L5`), and it must be **deeper than your own** (an L2 spawns L3s, an L3 spawns L4s, an L4 spawns L5s). A same-level or up-level request — or an attempt to spawn an `L1` (the root is genesis-only) — is **refused**. The level selects the child's model + runtime + role (see `runtime-and-model-map.md`); you never pick a child's model directly.
- **`brief` (optional — the exception, not the default)** — an inline task string. Use it ONLY for a throwaway child you didn't pre-author a `brief.md` for; it writes the child's `brief.md` for you. The faithful default is to pre-author the node (Step 1) and omit `brief`. A pre-authored `brief.md` is **never overwritten** by a no-`brief` request.
- **`purpose` (optional)** — a process-purpose marker for special sanctioned child work. Current values: `"planning"` for an L2-spawned planning-L3 design child; `"test_author"` for an L4-spawned L5 child whose job is to author an executable acceptance package from frozen criteria rather than product code.
- **`test_refresh` (optional boolean)** — use `true` only with `purpose: "test_author"` when a post-design spec change requires a refreshed acceptance package.
- **`test_refresh_for` (optional)** — names the implementation task or requirement the refresh applies to. A refreshed package must pass L5+ review and then receive L3 approval before implementation children proceed against it.
- **`accepted_test_package` (required for L4 → implementation L5)** — names the same-parent L5 `test_author` child whose package has already passed L5+ review. The harness binds that accepted `tests/` package into the implementation child node; do not copy it manually. Use the leaf child name (`"tests"`, `"parser-tests"`), not the gate id and not a file path. Example:

```json
{
  "child_name": "impl",
  "child_level": "L5",
  "accepted_test_package": "tests"
}
```

- **`no_executable_tests_exception_ref` (rare alternative for L4 → implementation L5)** — a parent-node-relative pointer to an approved no-executable-tests exception artifact. Use this only for the unusual approved no-tests path; it is mutually exclusive with `accepted_test_package`.

**What happens next (observable).** On its next sweep the daemon reads your request, registers the child as a planned node **under your address** (the supervision-tree edge points back at you), derives its spec/acceptance pointers from the node you prepared (or writes `brief.md` from an inline `brief`), and spawns it through the one chokepoint — under *your* identity, so a request in your outbox can only ever spawn *your* child. A serviced request is renamed `…​.done`; it is never spawned twice.

**Rejection is visible, never silent.** If a request is malformed (bad JSON, an unsafe `child_name`, an unknown or non-descending `child_level`) the harness renames it `…​.rejected` and drops a `…​.rejected.reason` file next to it explaining why. Read the reason, fix the request, drop a new one. The flow never silently skips a child — a missing child is always either a `.rejected` you can see or a spawn-failure escalation (below), not a quiet gap.

**A spawn that cannot honor its contract escalates** — it does not silently substitute or degrade (see *Spawn-time failure is an escalation* below). The request channel is how you *ask*; the contract guarantees are enforced by the harness on your behalf.

---

## How You Submit to a Review Gate

At gated boundaries, finishing your producer work means submitting a candidate to the co-located
review gate. It does not mean waking the parent as complete.

For L4, L3, and L2 producer seats:

1. Fill `report.md` in your node. Point to the lower gate verdicts, evidence, coverage map,
   integration notes, deviations, and unresolved risks the review seat needs.
2. Make sure the review packet pointers in your node are current. The packet is pointer-not-payload:
   it tells the review seat what to read.
3. Write your terminal signal as `DONE`.

When a gate is required, the harness interprets producer `DONE` as `candidate_submitted`: the
producer stays running, the co-located `#review` seat (the node's Ln+ function-owner) receives a `candidate_submitted` pointer, and
the parent is not woken. Only the review gate's `ACCEPT` becomes parent-visible completion.

The review seat may `BOUNCE` typed repair requests back to the producer, `ESCALATE` a decision to
the parent, or `ACCEPT` the candidate and pass it upward. A producer that receives a bounce keeps
its context, repairs within its authority, updates the candidate artifacts, and submits again.

This is a review-gate submission, not ordinary child delegation. The `#review` seat is co-located
at the same work node and structurally independent from the producing `#exec` seat.

---

## How Sessions End

**Natural completion — collapse on finish (G37).** You finish your unit of work, your output is accepted at the gate above you, and your parent **collapses** the session to free context. Collapse is the default end-state, not a failure: holding a finished agent's context open costs window for no benefit once its result is durably written to the work node. Collapse is what makes statelessness the backstop and persistence merely an optimization (below).

**Interrupted shutdown.** Your parent sends "prepare for shutdown." You wrap up: complete the handoff protocol below. Then your parent collapses the session.

**Block / review-failure — you do NOT end (G37).** If you hit a blocker you cannot resolve at your altitude, or your output fails the gate above you, you do **not** dead-end and you do **not** collapse. You **keep your context** and **escalate options to your parent** — see *Block & Review-Failure Handling* below. Collapse is reserved for *accepted* completion and for parent-ordered shutdown.

**Key rule: your parent (or the harness on its behalf) collapses your session — you never kill your own.** You also never collapse a child that still has live descendants — shutdown cascades bottom-up. The harness watchdog may collapse an **ephemeral leaf** (L5/L5+) that has gone idle without writing its **terminal signal artifact** (`.signal.<seat>.json` — `comms-protocol.md`, Terminal Signal): a bounded, evidence-based reap of a non-signing leaf, recorded as `FAILED` — never a blind kill of a live, working session. It does not auto-collapse persistent coordinators; a dead coordinator is *recovered*, not reaped (see Liveness below).

---

## Block & Review-Failure Handling (G37) — Escalate Options, Don't Dead-End

A blocked or bounced agent is the most dangerous moment in the cascade: it is exactly where a literal executor (especially a GPT-5.6 Sol L5, which won't paper over a gap with good architecture — see `runtime-and-model-map.md`) is tempted to either guess silently or stall. The lifecycle rule forbids both.

When you are **blocked** (ambiguity, a contradiction in your spec, a problem you cannot resolve at your altitude) or your output is **bounced** by the gate above you:

1. **Keep your context.** You are not respawned cold for a bounce or a block. Your loaded brief, your frozen acceptance artifact, your work-in-progress, and your reasoning so far stay live. Throwing that away on every bounce would burn the very context that makes the next attempt better.
2. **Escalate *options*, not a dead-end.** Surface the blocker to your parent framed as **decidable options with tradeoffs**, not as "I'm stuck." Name what you know, name the fork, name what you'd need to proceed. A bare "blocked" is a dead-end; "blocked, and here are the two ways forward and what each costs" is a decision the parent can actually make. (This mirrors the human-facing neutral-tradeoff-framing rule M57 — give the decider a real choice, not a wall.)
3. **Do not silently decide.** The escalation channel exists precisely so ambiguity goes *up* instead of being filled in place. For a GPT-5.6 Sol L5 this is load-bearing and is briefed explicitly as **escalate ambiguity, don't decide it** (E32) — the L5→L4 escalation is the relief valve that keeps literalness from becoming silent wrong guesses.
4. **Iterate within the bounded loop.** On a review bounce you keep iterating against the same frozen acceptance artifact (`QUALITY-GATE.md`, D29). The bounce loop is **bounded** (loop-cap N): if it doesn't converge, the issue escalates to the parent rather than thrashing — repeated non-convergence is itself information that the spec, the decomposition, or the unit sizing is wrong, which is a parent-altitude call, not yours.

Transport for all of this is **durable artifact plus canonical question** (`COMMUNICATION.md`,
`comms-protocol.md`). Write the evidence/options artifact, then a direct-edge message with
`needs_answer: true`; the open question makes your wait ledger-derivable. The parent's answer is
another canonical message naming the question; answer commit closes it and wakes you. The full
bounce/escalation mechanics live in `QUALITY-GATE.md`. Older signal and downward-answer records
remain readable as compatibility history; do not author them for new communication.

### Normal coordination while staying alive

Some parent/child communication is neither completion nor a held question. Use the same canonical
message record with `needs_answer: false`. Write the artifact first, then the message. For a
clarified constraint that changes a frozen contract, an ordinary message is insufficient: the
contract owner writes an immutable revision, the daemon sends `contract_amendment`, and the holder
re-reads and explicitly rebinds.

The pattern applies to every direct execution edge below L1: L2/L3, L3/L4, and L4/L5. Use it to keep
the live child pointed at the right altitude without turning ordinary coordination into completion or
failure.

### Spawn-time failure is an escalation, never a silent substitution

The escalate-options rule applies not just to a *running* agent that hits a block, but to a **spawn that cannot be completed as configured**. The general rule: **a spawn that cannot honor its contract escalates up the chain — it never silently substitutes or silently degrades.** A child must run on the unit, brief, and (model + runtime) its config specifies; if the spawn machinery cannot deliver that, the failure travels upward as a framed decision, exactly like a block.

The load-bearing case is **model/runtime pinning** (`runtime-and-model-map.md`, E32): if the adapter cannot pin the configured model + runtime — model unavailable, override rejected (e.g. a ChatGPT-account Codex silently substituting a different model for a requested `gpt-5.x` pin), or the runtime is down — it does **not** quietly run the child on a substitute. Observable behavior:

- The spawn does **not** proceed on a fallback model/runtime. No work runs on an unrecorded substitute.
- A **spawn-failure escalation** is emitted with the child's address, the configured vs. actual model/runtime, and the failure class. Because model/runtime is a config-time, system-level concern that no intermediate level may override, this escalation terminates at **L1**, who alerts the user — rather than stopping one level up like an ordinary block.
- The **actual model/runtime used is always recorded** in the child's work node (`model-used`) and is never silently assumed from config. A child running on a model that differs from its config with no corresponding L1 escalation in the trace is a contract violation a checker can flag.

This keeps spawn failures consistent with the rest of the lifecycle: nothing fails silently, every degradation surfaces as a decision someone with the authority to make it actually sees. The detailed model/runtime contract — the three failure classes and the user-facing alert text — lives in `runtime-and-model-map.md`; this section is the *lifecycle* facet: a spawn that can't honor its contract escalates instead of degrading.

---

## Shutdown Handoff Protocol

Mandatory before a session ends (whether by accepted-collapse or interrupted shutdown). Shutdown doesn't complete until these are done — because the next thing that reads this node may be a *cold* instance, the optimizer-L1 audit layer, or a 2w resurrection:

1. Update your living docs (`design.md`, `plan.md`, or `project.md` as your level owns) to reflect current state.
2. If your node has a runtime-provided `status.md`, update it with the current state of your scope.
3. If your node has a runtime-provided `log.md`, append what was done. Do not create or append ancestor/project logs as a sign-off ritual.
4. Update the node README with anything the next instance needs to know.
5. Ensure your result and evidence are written into the **work node** itself (`report.md`, the code, test results against the frozen acceptance artifact) — docs are the durable truth; a collapse must leave the node self-describing.

The **frozen acceptance/rubric artifact** (D26; e.g. `…/stripe-client/acceptance.md`) is read-only to you and is never rewritten at shutdown — it was authored before executor admission by a different seat and stays immutable so the work stays anchored to the tests, never the tests to the work.

If you crash without completing handoff — degraded but recoverable. Living docs, log, and any results written before the crash still exist in the node. The next instance reconstructs from them. The quality of that left-behind state is the whole ballgame for recovery.

---

## Persistence vs. Statelessness (G38)

**Statelessness is the backstop; persistence is the optimization.** The system is *designed* so that a fresh instance, spawned cold at a node's address, can pick up from the node's artifacts alone. Persistence — keeping a session warm after it finishes its immediate work — only saves the cost of that cold reconstruction. It is never load-bearing for correctness. If a persistent agent dies, a cold respawn must be able to continue; if it can't, the failure is in the artifacts, not in the lost session.

Per-level persistence profile (an optimization layer, not a dependency):

- **L1** — longest-lived. Continuous conversation with the user; intent guardian. Benefits most from active context management, but its captured intent spec is what actually persists — a cold L1 reloads the spec and continues.
- **L2 / L3** — persistent across their phase: sessions stay warm after completing immediate work so a parent can send a follow-up without a cold respawn. A persistent L3 sits idle until its parent messages it. (Note the C21 split: a *planning-L3* produces the area design then collapses; a *fresh execution-L3* owns it later — see `WORKSPACE-SCHEMA.md` / decomposition docs. Persistence applies within each, not across the planning→execution seam.)
- **L4** — manages a workstream across multiple L5 spawns; persists for the workstream's duration.
- **L5 / L5+** — task-scoped. The L5/L5+ execute-review pair runs the unit; on **accept**, both collapse and forward upward; on **bounce**, L5 keeps context and iterates (above). L5 is the shortest-lived seat and the one most often cold-respawned.

Across every level the invariant holds: **artifacts must be good enough that a fresh instance can take over.** Persistence is how the system is fast; statelessness is how it is correct.

---

## The 2-Week Resurrection / Audit Window (G37)

When a node **collapses on finish**, it is *not* immediately reaped. For **2 weeks** its full state — frozen brief, frozen acceptance artifact (D26), `report.md`, transcript, edit-manifest slice, and trace-blocks — is held **resurrectable** in the work node, keyed by its **stable address** (which survives collapse, F35). This window is the lifecycle hook that the audit layer hangs off of; the window's *audit/optimizer contract* is owned by `OBSERVABILITY.md` and is only summarized here.

The window serves the **audit and improvement layer, not the live run**:

- **Read-only replay (the default).** The narrative timeline, diagram replay, and drift metrics for a now-collapsed node are reconstructable from the held state without re-spawning anything. This is what the user and optimizer-L1 do most of the time — look back at how a collapsed node worked.
- **Live re-spawn (when needed).** A collapsed node can be brought back at its address with its exact frozen context — to interrogate it ("why did `proj/payments/stripe-client#exec` choose this?") or to re-run the unit after an upstream fix without re-planning from scratch. Re-spawn lands at the *same address* because the address is bound to the work node, not the dead instance.

**Reaping.** After 2w the **lifecycle reaper** (infrastructure, not an agent) garbage-collects the resurrectable state. By then whatever the audit layer needed has been distilled into the durable narrative + drift metrics, which persist permanently. **optimizer-L1 (god-view) or the user can pin a node to extend its window** when a run is under active investigation.

**The window feeds the system-improvement audit function.** This 2-week buffer is the live, highest-fidelity feed into the audit layer (I42): the freshest material for drift analysis is whatever collapsed in the last 2 weeks. The audit function reads the held state to spot the recurring, cross-run patterns the per-run plan-alignment gate structurally cannot see (a single defect is the gate's job; the same defect recurring across runs is the audit function's). In V1 this is done by the user working within the Improvement Workspace (IMPROVEMENT-WORKSPACE.md); a future optimizer-L1 capability (a separate concept from the workspace itself) would automate this systematically. The full 2w-window contract — read-only god-view, drift-first targeting, structural-intervention-with-human-disposition — lives in `OBSERVABILITY.md`; this section just establishes that collapse leaves a 2w trail and who consumes it.

---

## Recovery

If an agent is stuck or crashed and cannot be salvaged by escalation:

1. Parent collapses (or force-reaps) the failed session.
2. Parent spawns a **fresh instance at the same address** (addresses are stable across respawn, F35).
3. The fresh instance reads the work node — living docs, `status.md`, `log.md`, README, the frozen brief and acceptance artifact — to pick up where the previous one left off.
4. No special recovery protocol: stateless design (G38) makes cold respawn the default recovery mechanism. If a unit collapsed recently, its 2w-held state can also be resurrected to recover richer context than the durable docs alone.

The quality of your documentation directly determines how well recovery works. A well-documented work node means seamless recovery; a poorly documented one means the new instance starts half-blind.

---

## Liveness — turn state and the return contract

Runtime hooks are primary. Claude seats durably record turn running, tool-in-flight/tool-complete,
and turn end; Codex's sanctioned notify equivalent records turn end, with unavailable start/tool
edges explicitly covered by fallback evidence. The harness seeds a live owed checklist at spawn and
refreshes it as submissions and obligations land.

At every observed turn end the shared return-contract projection yields exactly one result:

1. the owed product is present;
2. the ledger proves a legitimate wait — live children, pending gate verdict, or held open
   question; or
3. neither, so the seat is reminded to produce what it owes or write an explanation/question.

Process death, a hook-observed tool stuck in flight, malformed/missing hook state, and hook-less
runtimes retain the transcript/pane/file/CPU evidence floor. Elapsed time alone never proves
failure. Ordinary child completion does not wake a waiting coordinator one child at a time; terminal
rows accumulate silently and the address-owned product/review-check cohort barrier produces one
wake when the cohort becomes ready.

---

## Cascading Failure Prevention

A level **acts on its direct children** (if an L5 is stuck, L4 handles it — prod → respawn or escalate; L4 escalates to L3 only when it cannot resolve it itself). This natural containment keeps one stuck L5 from cascading upward, and composes with the escalate-options rule above: a block travels exactly one level up. But **liveness and quiescence are read over the whole subtree, not just direct children** — the harness maintains a **live-descendant roll-up** per node so a coordinator is never judged idle (or collapsed) while live work exists two levels down. Direct-children is the *action* scope; the subtree roll-up is the *visibility* the harness needs to keep the accountability invariant.

**Emergency override:** L1 can force-reap agents at any depth, bypassing the bottom-up protocol — destructive, requires explicit confirmation. Routine reaps are the watchdog's bounded, evidence-based reap of non-signing leaves (above), never a blind kill. (The system-improvement god-view is read-only — it observes and proposes, it cannot kill sessions.)

---

*Operational reference — loaded at boot for all levels.*
*Created: 2026-03-29 · Consolidated: 2026-06-02 (G37 collapse-on-finish + 2w window + escalate-options-on-block; G38 statelessness-backstop / persistence-optimization).*
