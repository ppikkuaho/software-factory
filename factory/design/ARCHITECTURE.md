# AI Architecture -- Architecture Document

Structural design for the L1–L5 build-software agent system. Section 1 (levels, operating cycle, models, communication/visibility) was consolidated 2026-06-02 from the design conversation and is at build-resolution; later sections are coarser and carry remaining `[PLACEHOLDER]` work. Sibling process-design docs hold the detail: `PLAN-ALIGNMENT-GATE.md`, `DECOMPOSITION-METHODOLOGY.md`, `QUALITY-GATE.md`, `WORKSPACE-SCHEMA.md`, `COMMUNICATION.md`, and `operational/shared/runtime-and-model-map.md` / `agent-definition-principles.md`.

> **CURRENT SUBSTRATE RECONCILIATION — 2026-07-24.** `SUBSTRATE-PHYSICS.md` is the one-sitting
> model. Where this older semantic spine says best-effort bus, per-child completion nudge,
> evidence-primary liveness, or special escalation/answer-down, read the current mechanism as:
> durable sender-owned messages/open questions; daemon-verified event wakes; quiet child rows plus
> cohort barriers; hook-primary turn state with evidence fallback; and ordinary answer messages.

### Document Hierarchy

This project's design documents follow a five-level hierarchy, from intent to implementation:

| Level | Purpose | Document |
|-------|---------|----------|
| 1. Vision | Why this exists, what success looks like | `VISION.md` |
| 2. Principles | Philosophical constraints governing all decisions | `DESIGN-PRINCIPLES.md` |
| 3. Architecture | Structural design — components, boundaries, relationships | `ARCHITECTURE.md` (this document) |
| 4. Process Design | Protocols, schemas, contracts, workflows | `QUALITY-GATE.md`, `PLAN-ALIGNMENT-GATE.md`, `DECOMPOSITION-METHODOLOGY.md`, `WORKSPACE-SCHEMA.md`, `COMMUNICATION.md`, `operational/shared/git-protocol.md`, `GUI-DESIGN.md` |
| 5. Implementation | Actual code, configs, prompts, infrastructure | `operational/` (configs, loadsets, runtime map) |

**Per-level documents** sit alongside the architecture as parallel artifact types:
- **Soul documents** (`operational/L1/soul.md` through `operational/L5/soul.md`) — fundamental identity, drives, orientation. Seeds from which behavior grows. Rarely change. Deprioritized for V1 (see `operational/shared/agent-definition-principles.md`): per that principle, soul docs are now **frozen one-line status pointers**, not first-class Identity artifacts — behavior is framed primarily through positive role boundaries and the brief, not a heavy identity layer. They remain listed below for completeness but are no longer the primary investment.
- **Role documents** (`operational/L1/role.md` through `operational/L5/role.md`) — job description, responsibilities, boundaries, interfaces. Change as the system design evolves.
- **Operational configs** (`operational/L1`…`L5`, `operational/shared/`) — behavioral defaults, tooling, communication patterns, inspection criteria, runtime/model assignment. Tuned frequently from experience. The cross-cutting configs live in `operational/shared/` (`runtime-and-model-map.md`, `agent-definition-principles.md`, `agent-lifecycle.md`, `comms-protocol.md`, `git-protocol.md`).

Each level is constrained by the levels above it. Specific designs in this document should be traceable to the principles by number.

---

## 1. The Five Levels

The hierarchy separates five genuinely distinct cognitive modes (principle 3). Each level thinks differently, sees differently, and produces different artifacts. They are not granularity layers of the same thinking -- they are different kinds of minds.

**Levels are a separation by *kind of thinking*, not a permission ladder (A2).** The primary axis is cognitive mode: intent-capture, project judgment, area design, workstream operations, craft execution are genuinely different jobs. Permissions, scope, and visibility are *consequences* of the cognitive role — you hold authority over the decisions that belong to your kind of thinking and nothing else (see Autonomy and Permissions). Reading this hierarchy as "higher = more powerful" inverts the design; read it as "each level owns a distinct decision class." Notably, the model is the *same* across the upper seats (see Model and Runtime) — a Project Architect delegates to a Module Designer not because it *can't* do the area design, but because doing so would clog its bandwidth and collapse the role separation that keeps context clean. Delegation is role + bandwidth, not capability.

**Seat notation (user ruling 2026-07-12):** **Ln** = execution seat at level n (e.g. L4); **Ln+** = review/verification seat at level n (e.g. L5+, L3+); **Ln&** = planning seat at level n (e.g. L2&); planning seats exist at L1–L3. In this corpus's established vocabulary that means planning-L3 = **L3&** and execution-L3 = **L3** (the C21 split). **L/+** = the transition/cross-level review seat — the plan-alignment gate (**LE+** is the sanctioned substitute wherever the slash would collide with the address-path separator). L1& = the intake/intent-spec production function; L2& = the design-cycle architect function (both ruled real, threshold-gated separate agents — design pass owed, see `working-notes/DEFERRED-REGISTER.md`). Review gates and every function they spawn fall under that gate's seat; auxiliaries stay named, never notated. Established seat names stay; the notation is annotated alongside them at first use per doc. Ruling source: `RESEARCH-SYSTEM-MACRO-ARCHITECTURE-2026-07-12.md` §2 in the repository's research-system corpus.

**V1 scope: software-building.** V1 targets one task type — building software — as the beachhead (A1). The architecture is *designed* to generalize to other portfolios (ML-research pipelines, market studies with code deliverables, etc.), and the level structure, operating cycle, and gate are deliberately task-agnostic in shape. But "handles any task type / flexibility is a core V1 requirement" is **retired as a V1 claim**: portfolio breadth is the long-term destination, not the first deliverable. Where this document or its siblings describe domain-flexible configuration, read it as *forward-compatible structure*, not a V1 commitment. The walking-skeleton and dry-run validation target a software project (the Payments slice of an e-commerce backend).

### L1 -- System Orchestrator

Receives user intent. Clarifies through conversation until delegatable. Routes to the right project's Project Architect. Confirms to user that the task is handled — closes the loop so the user can let go. When results return, packages them for the user — right detail level, right framing, gates deliverables before they reach the user. Monitors cross-project issues: resource conflicts, dependencies, overlapping work. Does not do project-level work.

**Intake methodology (K45 / M50).** L1 elicits intent outcomes-first, then probes *tradeoffs* to discover where the user is genuinely opinionated versus content to delegate — people reveal opinions when shown a fork, not when asked "do you care." It drills deep on opinionated/risky areas, shallow on delegated ones, and captures two dimensions per area: **opinion** (opinionated → ask later; delegated → don't) and **technical fluency** (technical → technical render; non-technical → plain-language implications). Both feed the plan-alignment gate's human-render calibration (M58). The output is a **tagged living spec** — each requirement marked `decided` / `delegated` / `deferred` — reflected back to the user. The heavy work of producing the spec docs (SDD / whatever artifacts fit, in the right order) is done by **dispatching the user to a separate parallel session**; only the finished spec returns, so L1's context stays clean.

**Intent guardian.** L1 captures intent, writes it down as the intent spec, and then *guards* it — checking whether what L2 proposes aligns with captured intent before surfacing to the user. The **user is the ultimate reviewer of client-intent fidelity** (this is L1's seat at the plan-alignment gate; see `PLAN-ALIGNMENT-GATE.md`).

> **Ruled 2026-07-12 (design pass owed):** the intake/intent-spec production function is **L1&** — a real, threshold-gated separate planning agent (formalizing the K45/M50 parallel-session offload); L1 remains the persistent orchestrator/intent guardian, guarding from the frozen spec. See `working-notes/OWNER-RULINGS-NOTATION-TESTER-SPLIT-MODEL-2026-07-12.md` §C.

**Identity:** `operational/L1/soul.md` — drives, orientation, relationship to the work.
**Role:** `operational/L1/role.md` — responsibilities, boundaries, workspace, escalation triggers.
**Operational config:** `operational/L1/` — intake methodology, guardian checks, portfolio-monitoring defaults, communication patterns, tooling, inspection criteria. Runtime/model: Opus 5.0 (generative/intent seat; see Model and Runtime).

### L2 -- Project Architect

Owns one project. Holds the project's history, architecture, constraints, current state. Receives tasks from L1, determines the approach, frames work for Module Designers. Evaluates results against intent — catches drift, kicks back with corrections when needed. The quality gate between what was asked and what was delivered.

**Architect process (M49).** L2's methodology is the real-architect decision-process, not a decomposition algorithm: identify the *architecturally-significant* decisions; decompose to "components + responsibilities + interfaces" and **stop at sufficient resolution to delegate**; apply **Last Responsible Moment + subsidiarity** — decide the cross-module/expensive things *now*, defer module-internal/domain-deep things *downward with constraints*; recognize and apply known patterns; de-risk with spikes. **DDD is the carving sub-method inside this**, not a replacement for it (full method in `DECOMPOSITION-METHODOLOGY.md`). L2 establishes a **substrate first** (B14) — the cross-cutting core (money, IDs, events, audit, idempotency primitive, base data model) built ahead of the feature areas via the walking skeleton — not as a peer feature module.

**Output = ADR-style (M49).** L2 produces a component map + interface contracts + **ADRs** (decision + rationale + status `decided`/`deferred`) + per-module specs where deferred decisions appear as **constraints** — which become the frozen rubric each L3 is held to (D26). ADRs do quadruple duty: handoff contract, anti-drift anchor, audit/optimizer substrate, and statelessness rationale-preservation. Interfaces are **fluid during the planning cascade, frozen for execution**: L2 proposes coarse interfaces, the domain-deep planning-L3 pressure-tests and renegotiates upward, the walking skeleton validates, then they lock (M49 / §5).

> **Ruled 2026-07-12 (design pass owed):** the design-cycle architect function is **L2&** — planning cascade through gate-PASS, then collapses; a fresh build-cycle **L2** takes the frozen plan + ADRs + RTM. Threshold-gated. Same note §C.

Domain expertise loads **per-domain into the owning L3's loadset**, not into L2 — L2 is the same model as its L3s and delegates for role-separation and bandwidth, not capability.

**Identity:** `operational/L2/soul.md` — drives, orientation, relationship to the work.
**Role:** `operational/L2/role.md` — responsibilities, boundaries, workspace, escalation triggers.
**Operational config:** `operational/L2/` — architect-process defaults, ADR templates, per-project tuning, tooling, inspection criteria. Runtime/model: Opus 5.0 (architecture seat; see Model and Runtime).

### L3 -- Module Designer

Owns an area of the project across two phases — deep area design, then execution of that design. The two phases are **not the same agent** (C21). They are split because clean-context planning and clean-context execution are different cognitive jobs (C19/C20):

- **Planning-L3 (temporary).** Spawned during the design cycle to produce the area/module design. It thinks deeply about its area — L2 sets direction + coarse interfaces + constraints, the planning-L3 works out the specifics, *pressure-tests the interfaces and renegotiates them upward*. It writes its output as `plan/area-{name}.md` in **L2's planning workspace**, then **collapses**. This is the planning-L3 → execution-L3 handoff: its design is an *output*, not a live context.
- **Execution-L3 (fresh).** Spawned later during the build cycle, after the plan-alignment gate has passed. It **owns that frozen design as `design.md`** in `proj/{area}/`, adds `plan.md` as the living execution layer, decomposes the design into workstreams, assigns them to L4 Workstream Coordinators, manages the gap between plan and reality, reads L4 reports, and handles operational surprises. It inherits only the frozen plan + RTM — never the planning conversation.

The split is **threshold-gated (M53)**: it fires only when a module's design is substantial. Trivial modules may use one L3 owner for both phases — variable depth at the planning layer, not a license to skip the L4/L5 implementation spine. Both level templates must state the handoff explicitly (planning-L3 output → execution-L3 input). This dual-phase nature is what makes L3 a genuinely distinct cognitive mode (see Orientation).

> **Ruled 2026-07-12:** the C21 split generalizes upward — L1& and L2& are ruled real threshold-gated agents (design pass owed; same note §C).

**Identity:** `operational/L3/soul.md` — drives, orientation, relationship to the work.
**Role:** `operational/L3/role.md` — responsibilities, boundaries, workspace, escalation triggers. Carries both the planning-L3 and execution-L3 variants and the handoff contract between them.
**Operational config:** `operational/L3/` — design-review patterns, verification patterns, the planning→execution handoff, tooling, inspection criteria. Runtime/model: Opus 5.0 (area-design seat; see Model and Runtime).

### L4 -- Workstream Coordinator

Receives a workstream from the Module Designer. Decomposes into concrete tasks, sequences them, assigns to Task Executors. Reads Task Executor reports — evaluates process quality, coverage, flagged concerns. Does not inspect raw work. Handles operational surprises within its workstream — adjusts, retries, or escalates when something breaks. Manages the gap between the workstream plan and reality.

**L4 owns the L5 acceptance path (M51).** The design cycle gives L4 frozen workstream criteria and rubrics; execution-L4 decomposes the workstream into task nodes and uses a dedicated L5 `test_author` child to turn those criteria into executable acceptance before the implementation L5 writes anything. The test package passes L5+ review, then lands as a **frozen, read-only-to-the-executor `acceptance.md`** artifact in the work node (D26). This is the anti-test-theater core: the work is anchored to a standard the implementation worker cannot move, never to tests reverse-engineered from code. A future first-class `#test` lateral can replace the L5 `test_author` mechanism. (See `QUALITY-GATE.md`; the same temporal rule generalizes up the cascade — see Operating Cycle.)

**Identity:** `operational/L4/soul.md` — drives, orientation, relationship to the work.
**Role:** `operational/L4/role.md` — responsibilities, boundaries, workspace, escalation triggers. Includes the normal M51 L5 test-author path and the post-design refresh variant.
**Operational config:** `operational/L4/` — verification patterns, test-authoring discipline, tooling, inspection criteria. Runtime/model: open — Opus 5.0 for the coordinator/spec-judgment seat; test-authoring and pedantic checks may move after evaluation (see Model and Runtime).

### L5 -- Task Executor

Executes bounded tasks. Writes code, runs the **frozen, pre-written acceptance tests** (read-only to it; D26) plus its own unit tests plus **CI (the automated floor)**, verifies within its scope, and reports back: what was done, how it was verified, what concerns remain. What it works on and how the approach is framed arrive as givens; within those boundaries, it has full craft autonomy and genuine aesthetic judgment about how the thing is made.

**L5 is an execute-review pair (M52).** **L5** (the executor) writes code and makes the pre-written tests pass. **L5+** (a *separate* reviewer agent) does its own testing and reviews against spec, then either **accepts** (→ forward; both collapse) or **bounces** (L5 keeps its context and continues; bounded loop, see §7 and `QUALITY-GATE.md`). L5+ runs on a *different runtime* (Opus) from a GPT-5.6 Sol L5 for judgment diversity (E32). The executor cannot edit its own acceptance tests — immutability is the anti-test-theater enforcement made physical.

**Identity:** `operational/L5/soul.md` — drives, orientation, relationship to the work.
**Role:** `operational/L5/role.md` — responsibilities, boundaries, workspace, escalation triggers. Includes the L5 executor and L5+ reviewer variants.
**Operational config:** `operational/L5/` — self-verification patterns, the run-tests-then-bounce loop, tooling, reporting templates. Runtime/model: L5 executor = **GPT-5.6 Sol on the Claude Code harness** (standing session, Sol main loop — unified 2026-07-13; execution seat); L5+ reviewer = Opus (judgment seat). Judgment diversity lives in the model (see Model and Runtime).

### Autonomy and Permissions

Each level has autonomy within its scope but cannot reach outside it. Autonomy narrows downward; each level operates only within the scope given by the level above.

| Level | Can Spawn | Scope | Kind of Autonomy |
|-------|-----------|-------|-----------------|
| L1 — System Orchestrator | L2, L3, L4, L5 | Cross-project | Organizational — routing, resource allocation, creating projects |
| L2 — Project Architect | L3, L4, L5 (own project) | Own project only | Judgment — approach selection, problem framing, catching drift |
| L3 — Module Designer | L4, L5 (own area) | Own area of project | Design — detailed architecture and execution management within area |
| L4 — Workstream Coordinator | L5 (own workstream) | Own workstream | Operational — decomposition, sequencing, verification |
| L5 — Task Executor | Nothing | Bounded task | Craft — implementation choices within clear boundaries |

L2 can't decide to work on a different project. L3 can't change L2's direction. L4 can't change L3's design. L5 can't expand its own scope. Each level's permissions are a consequence of its cognitive role — you have authority over the decisions that belong to your kind of thinking, and nothing else.

**Edge behavior:** When a level encounters something outside its scope, it escalates with a complete report — what happened, what was tried, evidence, options, and recommendation — over the bus, with the report itself written to the work node (transport vs. truth, see Communication and Visibility). The receiving level critically evaluates independently. This is a low-trust decision environment: the subordinate's ground-level view is useful signal, not trusted directive. The receiving level evaluates against its own plan, framework, and knowledge, and does not default-bias toward the recommendation. The subordinate provides the information; the parent owns the decision.

**Addressing (F35).** Every agent's address is its **workspace node path plus a role-variant suffix**: `proj/payments`, `proj/payments/gateway`, `proj/payments/gateway/stripe-client#exec` / `#review`. Addresses are **semantic** (area names, not numeric `L3.1`) and **stable across respawn** — bound to the work node, not the instance — so a collapsed-then-resurrected agent keeps its address. This one path scheme is the system's single spine: **requirement-IDs, agent-addresses, workspace-paths, git-branches, rubric-file locations, and the visibility graph are all the same hierarchical-path/prefix scheme** (the UNIFICATION decision). A requirement `R-003.2.1`, the node `proj/payments/gateway/stripe-client`, that node's branch, its `acceptance.md`, and its visibility neighborhood all key off the same prefix.

**Workspace permissions — write (nested tree):**

| Level | Writes |
|-------|--------|
| L1 | `L1/` portfolio workspace |
| L2 | own project root + creates `{area}/` nodes + the substrate |
| planning-L3 | `plan/area-{name}.md` in L2's planning workspace (then collapses) |
| execution-L3 | `{area}/` (owns `design.md`, `plan.md`) + creates `{area}/{workstream}/` |
| L4 | `{area}/{workstream}/` + creates `{workstream}/{task}/` (+ frozen `acceptance.md`) |
| L5 | `{area}/{workstream}/{task}/` only — and **never** its own `acceptance.md` |

Write access follows the nested tree — each level writes within its own workspace and creates child workspaces within it. The `acceptance.md` rubric is written before executor admission by the acceptance-authoring path and is **read-only to the executor** thereafter (D26). File locking handled by infrastructure. Append-only files (logs, status.md) use an append queue.

**Read access is need-to-know, not broad** — see Communication and Visibility for the visibility graph that supersedes the old "everything within scope" read model.

### Orientation

In the four-level design, levels alternated cleanly between breadth and depth: L1 breadth (portfolio), L2 depth (single project), L3 breadth (multiple tasks), L4 depth (single task). The five-level hierarchy breaks this strict alternation. L3 is a hybrid — deep during its design phase (producing detailed architecture for its area), broad during its execution phase (managing multiple workstreams through L4s). This dual-phase nature is what makes L3 a genuinely distinct cognitive mode rather than a redundant layer: it adds a kind of thinking (detailed design ownership) that neither L2 nor L4 provides. If L3 were purely operational, it would collapse into L4; if it were purely strategic, it would collapse into L2. The two-phase structure justifies the boundary (principle 3). The planning-L3 / execution-L3 split (C21) is the operational realization of this duality: the "deep" and the "broad" halves are literally different agents, separated by a clean-context boundary.

### Operating Cycle

The system runs a **per-unit `Plan → Execute → Review` cycle**, applied recursively at every level — **not** a single global waterfall (C19/C20). Each unit of work, at each level, is planned, then executed, then reviewed before its result is accepted upward. The phases are **separated by clean context**: planning and execution are different cognitive jobs and, where it matters (most sharply at L3), different agents, so execution never inherits the planning conversation and cannot quietly drift it.

**Architecture front-loaded, execution-planning rolling (C19).** The high-level architecture is decided once, up front (L2's substrate-first design). But detailed execution-planning is *rolling*, increment by increment: after each phase completes, real information — not assumptions — feeds back, and later designs may be revised. This is agile-with-upfront-architecture: design broadly, execute in phases, revise as real information emerges. Designs are expected to evolve; frozen interfaces are expected to hold.

**Dual nested cycles, one shared boundary.** The operating cycle is actually two nested `Plan → Execute → Review` loops that meet at exactly one point:

- **Design cycle** — produces a *validated plan*, never code. Plan: L1 intake → tagged intent spec; L2 → module portfolio + interface contracts. Execute: planning-L3s design each module and produce workstream definitions, interfaces, risks, falsifiable acceptance criteria, and rubrics. Review: **the plan-alignment gate.**
- **Build cycle** — produces validated code. A *fresh* execution-L3 takes its frozen, gate-approved design and drives L4 → L5 to write code. Execution-L4 performs rolling task decomposition and uses the normal M51 L5 `test_author` path to turn frozen criteria into executable acceptance before implementation L5 opens.

> **Ruled 2026-07-12 (founding design pass owed):** test authorship promotes from the L5 `purpose: "test_author"` errand to a first-class tester seat (GPT-5.6 Sol on the unified Claude Code pattern — harness half superseded 2026-07-13); its notation is decided in the founding pass. Same note §B + `working-notes/DEFERRED-REGISTER.md`.

**The plan-alignment gate is the boundary that unlocks the build cycle.** It is the single hard
checkpoint between design and build. No execution-L3 opens before PASS. The deterministic spine
checks forward/backward/MNF coverage, then a daemon-owned exact-read cell runs once-per-intent
atomization, two blind reconstructions, adversarial comparison, and portfolio coherence. L1
elevates only machine-required drift, disagreement, contradiction, atomization, or MNF-adequacy
findings; with none, L1 PASS stands alone. Full mechanics live in `PLAN-ALIGNMENT-GATE.md`.

**The Plan-phase output contract (M51, generalized).** A level's Plan phase is not "done" until it has emitted **spec + acceptance criteria + gate rubric** for the level below — authored *before* that level executes, *from* the spec, by a seat other than the worker. This is the anti-theater temporal rule applied up the whole cascade: every delegating level authors the pass-conditions for the level below at its own altitude (D26), so work is always anchored to criteria, never criteria retrofitted to work. At the L4→L5 boundary, the executable suite is produced in the build cycle by the normal `test_author` path from the frozen criteria.

### Model and Runtime

Model and runtime are assigned **per-level, at config time, and are swappable** (E31). The spawn mechanism is **runtime-abstracting**: the spawning level composes a runtime-neutral task contract; a thin per-runtime adapter turns it into a concrete harness invocation (see Invocation). Full assignment map and the cross-runtime contract live in `runtime-and-model-map.md`; the load-bearing rule:

- **Established generative / architecture seats → Opus 5.0.** L1 (intent), L2 (architecture), L3 (area design), L4 (planning), and L5+ (review judgment) — work that needs greenfield synthesis, broad intent understanding, and the user's own language. Semantic-cell and probe-seat assignments live in their calibrated registries rather than inheriting this rule.
- **Pedantic / adversarial / checking / execution seats → GPT-5.6 Sol (Claude Code harness, Sol main loop).** L5 (code execution), test-authoring/checking seats when assigned there, the gate's atomization auditor and adversarial comparator — literal, engineering-brained, pedantic completeness; weak at greenfield/architecture, which is exactly why it is not seated there.

**Cross-runtime briefing (E32).** When an Opus level briefs a GPT-5.6 Sol level, the **semantic brief is identical across runtimes** (identity + spec + frozen acceptance tests + interface contracts + constraints + workspace + reporting); only the tool-manifest, harness-invocation, and output-format are runtime-specific, injected by the adapter at spawn (hexagonal: brief = core, runtime envelope = adapter). **Result-flow is runtime-neutral for free** — both runtimes write files (docs-as-truth) and post to the bus. GPT-5.6 Sol implementation briefs carry extra discipline: **maximally decision-complete** (it will not fill gaps with good architecture), **accepted executable acceptance as the primary anchor**, and an explicit **escalate-ambiguity-don't-decide** rule (the L5→L4 channel is load-bearing). The L5+ reviewer sits on a *different* runtime (Opus) for judgment diversity. Swapping a runtime is swapping an adapter — that is what delivers E31's swappability.

### Communication and Visibility

**Transport is durable messages plus verified wakes; product truth remains in owned artifacts
(F33).** The old filesystem-inbox and ephemeral-bus models are superseded. A sender-owned message
durably records the direct edge and points to the substantive artifact; the recipient inbox is a
replayable delivery pointer. Open questions and their answers use the same primitive. The daemon
verifies wake delivery and retries it, so a missed keystroke costs latency without losing either
the message or the product fact.

**Visibility is need-to-know, not broad project-wide read (F34).** The earlier "L1–L3 read the whole project" model is replaced by a **visibility graph** derived directly from the address path (F35):

- **Subtree** — a node sees everything beneath it (paths under its own).
- **Siblings** — a node sees its same-parent siblings (for lateral coordination without escalation).
- **Parent** — a node sees its parent (path minus its last segment) for the contract it was given.
- **God-view (read-only)** — L1 and the system-improvement function (and, in the future, an optimizer-L1 capability — name retired 2026-07-12: the function now lives with the improvement harness's **principal coordinator**, hypothesis-tree project) can read across the whole portfolio, for routing, drift-monitoring, and recurring-issue detection. This is the deliberate exception, and it is read-only.

Cross-parent coordination that the sibling/subtree view does not cover escalates to the common ancestor rather than reaching laterally across the tree. The graph is **derived, not separately maintained** — it falls straight out of the path scheme (subtree = prefixes-under, siblings = same-parent, parent = prefix-minus-one), so it shares the single spine with addressing, requirement-IDs, and git branches. Downward communication remains unrestricted (a parent may always brief its child); the visibility graph constrains *reads*, while the bus handles *transport* — keep transport and policy separate (F36). Full protocol in `COMMUNICATION.md`; the durable-truth schema in `WORKSPACE-SCHEMA.md`.

---

## 2. Inter-Level Relationships

### Delegation (Downward)

Direction flows down as minimal prompts -- the "short email" model (principle 6). L1 tells L2 what is needed, not how. L2 tells L3 the area, direction, and key constraints — enough for L3 to produce a detailed design. L3 tells L4 the workstream scope and plan — decomposed until all design decisions are made, leaving tactical decisions to L4. L4 tells L5 the bounded task, not the implementation strategy.

Each delegating level spends tokens only on its own cognitive task. The invocation infrastructure handles context loading, role definition, file discovery, and bootstrapping. The delegating level composes *what*, the system handles *how to spin up* (see section 4, Invocation).

### Reporting (Upward)

Results flow up as compressed accounts -- summaries of what happened, not raw artifacts (principles 4, 5). Each boundary compresses differently because the receiving level needs a different kind of information:

- **L5 to L4:** Execution compressed into verification signals. "What was done, what was tested, what concerns were flagged."
- **L4 to L3:** Workstream operations compressed into area progress. "Tasks complete, workstream on track or adjusted, blockers identified."
- **L3 to L2:** Area state compressed into project progress. "Design complete, execution phase status, blockers identified, design revisions needed."
- **L2 to L1:** Project state compressed into portfolio status. "On track, deliverable ready, decision needed, blocked on X."

No level re-examines the work of the level below as default behavior. Inspection is the exception, triggered when something in a report doesn't add up (principle 4).

**Reporting protocol: event-driven, not periodic.** Each level reports when something meaningful happens — completion, escalation, significant status change. No scheduled check-ins. Between events, the parent can read the child's living docs (plan.md, report.md) for situational awareness — these *are* the truth; the bus message is only the nudge that they changed. No communication for communication's sake.

The pattern at every boundary is the same: **the durable account lives in an owned artifact and a
durable direct-edge message points at it** (F33). The daemon delivers and replays the wake. Ordinary
child completion rows stay quiet until the owner's product or review-check cohort barrier crosses.

**L5 → L4:** `report.md` in the task node serves as both progress tracker and final candidate
account. Questions use canonical `needs_answer` messages. Accepted completion reaches L4 through
the gate route, not a producer-authored completion nudge.

**L4 → L3:** `plan.md` stays current. Meaningful questions/guidance use canonical messages; the
product cohort barrier and gate routes carry readiness.

**L3 → L2:** `plan.md` stays current. Design readiness uses its specialized plan-alignment edge;
other questions/guidance use canonical messages and accepted execution uses gate routes.

**L2 → L1:** Same event-driven pattern. The specialized plan-alignment decision and product gate
routes remain distinct; other decision needs use canonical messages/open questions.

**Historical bus message format (superseded by `messages/<message-id>.json`):**
```
---
type: phase-complete | escalation | deliverable | status-change
from: [address — e.g. proj/payments/gateway#exec]
re: [work-node path]
urgency: routine | needs-attention | blocking
node: [path to the doc carrying the durable account]
---
[Short nudge text — the substance lives in the node doc.]
```

Frontmatter matches node metadata — the recipient can triage without opening, and follow `node` to the durable truth. Full protocol in `COMMUNICATION.md`.

**Inspection triggers** are a calibration problem that emerges from use. With souls deprioritized for V1 (see `agent-definition-principles.md`), the instinct ("anything out of place is felt before it is found") is carried mainly by the role boundary and the operational config rather than a heavy identity layer; specific trigger criteria are tuned iteratively in operational configuration. The structural backstop is not instinct but the gate and the frozen rubrics — drift is caught by checking against pre-authored criteria, not by feel alone.

### Escalation

Each level handles what it can and escalates what it cannot. Escalation flows upward through the same boundaries as reporting, but carries a different signal: "I need a decision or resource I don't have."

The pattern: a level encounters something outside its scope or authority, packages the situation into a compressed account with a clear ask, and sends it up. The receiving level either resolves it or escalates further.

L1 has a high threshold for escalating to the user. Most things should resolve within the hierarchy. When something does reach the user, L1 packages it -- the right context, the right framing, a clear ask (principle 1).

**Escalation triggers by level:**

- **L5 escalates to L4:** Task as specified can't be done (dependency missing, API doesn't match description, constraint conflicts). Better approach found but it changes scope. Work reveals something affecting sibling tasks.
- **L4 escalates to L3:** Workstream plan hits a wall — operational surprise that changes the shape of the work (not just a task failure). Cross-task dependencies not in the workstream plan.
- **L3 escalates to L2:** L2's direction appears wrong or suboptimal. Design reveals constraints or tradeoffs that require project-level decisions. Cross-area dependencies not in the project plan.
- **L2 escalates to L1:** Cross-project issue (resource conflict, shared dependency). Project scope needs to change. Decision requires user input that L1 should mediate.
- **L1 escalates to user:** High threshold. Only genuine decisions or blockers requiring owner judgment.

**Question payload:** A canonical `needs_answer` message points at the decision-ready artifact: what
happened, what was tried, evidence, options, and recommendation. The blocked agent keeps context
and parks on the open question.

**Attention:** Event type and open-question state determine daemon wake behavior; senders do not
select urgency/wake knobs.

---

## 3. Artifact Model

The system designs for statelessness (principle 2). Documentation is the primary memory. Every level reads its state from artifacts and writes its state back to artifacts. If all processes are killed and restarted from documentation alone, the system reconstructs fully.

### Artifact Categories

The system maintains several categories of artifacts, organized by what they serve:

**Portfolio-level artifacts** -- The view across all projects. What exists, what's active, what's the status. L1's primary reading material.

**Project-level artifacts** -- The deep knowledge of each project. Conceptual model, architecture, decisions made, history, current state. L2's primary reading material. This is the most critical artifact category -- L2's effectiveness is directly determined by the quality of project documentation (principle 12).

**Area design artifacts** -- Detailed designs, interface specifications, implementation plans for project areas. L3's primary output during design phase.

**Operational artifacts** -- Workstream records, decomposition plans, progress tracking, verification results. L4's working documents.

**Execution artifacts** -- The actual work product plus implementation reports. Code, research, analysis, alongside structured accounts of what was done and how it was verified.

**System configuration artifacts** -- Level definitions, invocation protocols, L2 configurations, documentation schemas. The system's own blueprint.

Artifacts are living documents — refined, added to, edited, and kept current by each instance that touches them. They reflect current project reality, not a log of everything that ever happened. Each level maintains the artifacts it owns for its own future instances, so the next instance of that level bootstraps from an accurate, up-to-date picture.

The workspace is a shared file space organized by level, designed for ephemeral agents — like an open source project where contributors come and go but the system survives through artifact quality and conventions.

Each level owns specific document types: L2 owns project state (`project.md`), decision records, and conventions. L3 owns area-level design documents and area plans (`plan.md`). L4 owns workstream plans (`plan.md`) and workstream READMEs. L5 owns execution reports and task artifacts. A single project-level log (`log.md`) provides chronological context, append-only.

Living documents (`project.md`, `plan.md`) serve as the navigation layer — active items in full detail, completed items collapsed. No archive system, no file moves. Files stay where they are; paths stay stable.

Each document carries its editing rules in frontmatter (owner, edit policy). A mandatory shutdown handoff protocol ensures departing agents update living docs, append to the log, and update READMEs before shutdown completes.

Full design in `WORKSPACE-SCHEMA.md`.

---

## 4. Invocation

### The "Short Email" Model

When a level spawns the level below, it sends a minimal brief -- the equivalent of a short email in a real organization (principle 6). The spawning level does not spend tokens on bootstrapping, context loading, or role definition. That is infrastructure.

The invocation protocol handles bootstrapping in layers:
- **Soul** — the level's fundamental identity (universal per level, never changes)
- **Loadset** — skills, tools, file discovery guides, reference material, handbook (configurable per project)
- **Task brief** — the minimal prompt from the spawning level (task-specific)
- Setting up reporting channels back to the spawner

L1 composes something like: "game project: implement dialogue branching." The protocol turns that into a fully bootstrapped L2 with project context loaded, role active, and task understood. L2 composes something like: "audio area: design and implement the spatial audio system." The protocol turns that into a fully bootstrapped L3 with area context loaded and design mandate clear.

### Configuration and Flexibility

**V1 builds software (A1).** The structure is *designed* to generalize across task types — coding, research, report writing, any problem-solving at sufficient depth — and that generality is a real long-term goal. But for V1, "handles any task type / flexibility is a core requirement" is **not** a commitment to honor now; V1 targets software-building, and the flexibility described here is forward-compatible structure, validated against one task type first. Domain-flexible configuration is a slot the architecture leaves open, not a V1 deliverable.

Each project has a predefined L2 configuration -- an artifact that defines how to spin up an L2 for that project. L1 doesn't build L2s from scratch; it invokes them. For new projects, L1 has a skill or guide for creating a new L2 configuration, but this is setup work, not normal operation. The same principle extends downward: L2 has predefined patterns for invoking L3, L3 for L4, and L4 for L5. Each level's invocation configuration is itself a versioned artifact (principle 16).

The key customization surface is what skills, files, and context the lower level loads, plus its runtime/model assignment (see Model and Runtime; map in `runtime-and-model-map.md`). Each project configuration defines a default loadset, but there is an inventory of everything available, and the loadset is easily modified per project. The same loadset mechanism is what *would* let the architecture host a non-software project later — different loadsets, same structure — but V1 only exercises the software case.

Configurations are not rigid presets. The system uses configuration "archetypes" -- well-crafted example configs that teach the structure and thinking behind good configuration. These serve as reference material, not templates to copy. A handbook on how to write and adapt configs for different task types complements the archetypes. The LLM's intelligence handles adaptation -- the common shortcoming is not lack of intelligence but wrong metacognition, insufficient instructions, or underestimated difficulty. Good archetypes and a clear handbook address all three.

Predefined configs are the default, not a constraint. Each level can customize invocations at runtime — adding context, adjusting scope, overriding defaults — when the standard config doesn't fit the specific task. The configs handle the common case; the spawning level adapts for the specific case.

Each level is exposed a set of prewritten tool calls / functions it can invoke programmatically — spawning, reporting, querying status, etc. These are the standard operations. But the interface is designed so levels can easily customize or compose these at runtime when they need something the prewritten tools don't cover. Think of it as a tool-calling interface with sensible defaults and runtime extensibility.

### Variable Depth

The five-level hierarchy is the maximum, not the minimum (principle 15). L1 routes to the appropriate depth based on the task. Simple operational work goes directly to L4. A complex creative problem deploys the full stack. The test: does the task require a different kind of thinking at each level it passes through? If a level would just pass-through without adding judgment, skip it.

For trivial tasks (fix a typo, change a comma to a semicolon), L1 sends a subagent directly -- the "assistant" pattern. A System Orchestrator doesn't route a trivial fix through five levels. L1's judgment on routing depth is trusted; the architecture provides guidance on when each depth is appropriate, but doesn't enforce mandatory gates.

Once a project is admitted into the full L1-L5 product-build cascade, variable-depth routing no
longer means zero-L3 execution. L2 product work passes through an L3 area/module owner. For a
trivial area, L2 may use one L3 instead of separate planning/execution L3 instances, but that L3
still drives implementation through L4/L5 and L2 does not spawn L4 directly for product work.

### Spawning Mechanism

Each agent is a full agent session on some runtime, spawned as an independent process. **The spawn mechanism is runtime-abstracting (E31/E32):** the spawning level composes a **runtime-neutral task contract**; a deterministic spawn script wraps it in a **thin per-runtime adapter** and launches the concrete harness. The spawning level never hard-codes which harness runs underneath — that is read from the target's config (see Model and Runtime; map in `runtime-and-model-map.md`).

The spawn script:

1. Reads the target level's config (loadset, project config, **runtime/model assignment**, optional role-variant identity)
2. Assembles the **semantic brief** — identity + spec + frozen acceptance tests/rubric + interface contracts + constraints + workspace + reporting — *identical across runtimes*
3. Picks the **runtime adapter** for the assigned model and injects the runtime-specific envelope: tool manifest, harness invocation, output format
4. Launches the harness (the pinned Claude Code for every seat — Opus or Sol — since 2026-07-13; a non-Claude harness re-enters via the adapter registry) with the assembled context as the initial prompt

The brief content is the hexagonal *core*; the runtime envelope is the *adapter*. Swapping a level's runtime = swapping the adapter, with the semantic brief unchanged — that is what makes the runtime swappable per E31. The agent never bootstraps itself — by the time it receives its first context, everything is already loaded. The script guarantees completeness, similar to the existing `agent-activate.py` pattern.

**Brief discipline (H41 / E32).** Briefs are **thin but decision-complete** — the spawning level distills the brief (spec + constraints + interface + ADRs) and *references* raw upstream intent rather than carrying it (pointer-not-payload, M54); ADRs are the rationale bridge. For a GPT-5.6 Sol implementation seat the discipline tightens: **maximally decision-complete** (it will not fill gaps with good architecture), the accepted executable **acceptance tests are the primary anchor**, and the brief explicitly instructs **escalate ambiguity, do not decide it** (the upward channel is load-bearing).

**Session lifecycle:**
- Each spawned session is a full agent instance with full tools, full context window, full autonomy
- Sessions naturally halt when the agent finishes its work (no more tool calls)
- Agents should always aim to be in a "ready to be shut down" state — work captured in artifacts, living docs updated
- Only the level above can collapse a session — agents never kill their own session
- A **blocked** agent keeps its context and waits, surfacing options upward — it does not collapse itself (G37)
- Clean shutdown: agent finishes naturally, parent collapses the session
- Interrupted shutdown: parent sends "prepare for shutdown, finish protocol and report when ready", agent wraps up, parent collapses
- Collapsed sessions can be resumed with full context preserved — collapsing is low-risk

**Communication between sessions:**
- **Transport = the bus** (real-time nudges); **truth = the docs** in the work nodes (F33)
- A message is a pointer/nudge, not the payload; delivery is best-effort because truth lives in the docs
- The workspace, the bus, and the visibility graph (sections 1–3) are the inter-session communication layer — both Opus and GPT-5.6 Sol seats write files and post to the bus, so result-flow is runtime-neutral for free

**Brief structure template (runtime-neutral contract):**
```
1. Identity — "You are the {area} Module Designer under {project}" / address e.g. proj/payments/gateway#exec
2. Bootstrap — loadset (assembled by script; soul deprioritized — role + brief carry behavior)
3. Workspace — "Your node is {path}. Read README.md to orient. Your acceptance.md is read-only."
4. Brief — distilled spec + constraints + interface contracts + ADRs (raw intent referenced, not carried)
5. Reporting — "Write report.md / plan.md in your node. Nudge {parent address} on the bus on events."
6. Runtime envelope — tool manifest + harness invocation + output format (injected per-runtime by the adapter)
```

The spawning level writes only the semantic brief (parts 1, 3–5 content). The runtime envelope (part 6) and bootstrap (part 2) are assembled deterministically by the spawn script from config.

---

## 5. Concurrency

### Async by Default

The system is fundamentally asynchronous (principle 14). When a level delegates downward, it fires and continues -- it does not block waiting for results. Results flow back up when they're ready. This means L1 can manage multiple active projects simultaneously without being blocked by any of them.

The only synchronous interactions are user-to-level conversations -- the user talking to L1, or the user dropping into a lower level directly.

### Resource Awareness

The primary resource constraint is the number of parallel instances. Compute is prepaid (Claude Code subscription) — the limits are session-based, not token-based in practice. The system needs to track active parallel instances and cap them at a defined limit (likely 20–64, to be determined empirically). Claude Code exposes usage data to the user; the system needs a mechanism to expose it to L1/L2 for allocation decisions.

No hard concurrency limits have been found on Claude Code Max. The practical constraint is operational, not technical — how many parallel agents can be meaningfully coordinated without quality degrading. The status board (infrastructure-tracked agent states) provides the visibility needed: L1/L2 can see how many sessions are active and make allocation decisions accordingly. A configurable soft cap (starting around 20-64, tuned empirically) prevents runaway spawning, but the system is not expected to hit hard technical limits.

### Agent Lifecycle

L1 is the only truly persistent agent in the system. It is the longest-lived process and the user's primary interface. Like all levels, L1 maintains documentation discipline -- it can reconstruct from artifacts after any context reset. But L1 is also the level where active context window management pays off the most. It lives long enough that smart decisions about what to keep in context, what to offload to artifacts, and how to handle compacts gracefully compound over time. This makes L1's context management a unique design problem -- not because it's fragile without it (documentation handles that), but because robust context management makes it significantly better.

L2 and L3 agents are persistent but not in the same way as L1 -- their context stays alive after completing a task, enabling follow-up messages to the same agent rather than spawning fresh. L4 agents are persistent within their workstream's lifetime — they stay alive to manage multiple task cycles but collapse when the workstream completes. L5 agents are task-scoped and collapse after completion. "Persistent" for L2/L3 means reachable for follow-up, not proactive -- a persistent L3 sits idle until its parent sends a new message.

Each level manages the lifecycle of the agents it spawned. Agent management -- knowing what's active, routing messages, deciding when to collapse -- is an explicit responsibility of each level (L1 through L4).

**Shutdown protocol:** Cascading shutdown goes bottom-up. If L3 tries to shut down an L4 that still has active L5s, the command is deterministically blocked -- hard block, not a soft suggestion. L3 is prompted to have L4 clean up its L5s first. L4 then shuts down its L5s and confirms readiness before L3 collapses it. The same pattern applies at every boundary. All levels (L1-L4) are aware of this protocol.

**L1 override:** L1 can force-kill agents at any depth, bypassing the bottom-up protocol. This requires explicit confirmation: "This is a destructive action that will kill active agents and their context, and could interrupt critical work. Are you sure?" The override exists for emergencies, not convenience.

Persistence is an optimization, not a dependency. The system is designed for statelessness (principle 2) -- artifacts must be good enough that a fresh instance can pick up if a persistent one dies. But in normal operation, persistent agents benefit from their live context window, and the system leverages this.

Persistence is implemented through agent sessions that naturally halt when work is done and can be resumed with full context preserved. Message routing to persistent agents uses the **bus** (transport); the durable account lives in the work-node docs (truth). Context window management for long-lived agents (especially L1) is an open optimization — active context management is a post-V1 research direction (see NOTES.md, "LLM Self-Managed Context"). Maximum lifetime before forced refresh is an empirical question to be determined during implementation.

**Collapse + 2w resurrect/audit window (G37/G38).** Statelessness is the backstop, not the operating mode — persistence is an optimization layered on top of artifact discipline. When an agent collapses (clean completion, or parent-initiated after a blocked agent surfaced its options), its session is not destroyed: it enters a **2-week window** during which it remains **resurrectable with full context** and **auditable**. Within the window the parent — or the system-improvement function (and, in the future, an optimizer-L1 capability that feeds on it) — can re-spawn the exact agent to continue, replay its reasoning, or audit what it did against its frozen rubric. After the window, the session is reaped; reconstruction then falls back to the stateless path (a fresh instance reads the work node). This makes collapse low-risk and gives the audit layer a live forensic window without making the system *depend* on live context. The concrete window mechanics (read-only replay vs. live re-spawn vs. re-run, and who triggers the reap) are an operational free-parameter being settled in the dry-run; the audit layer's use of the window is described in `OBSERVABILITY.md`.

**Persistence by level:**
- **L1:** Truly persistent. Longest-lived process, user's primary interface.
- **L2:** Persistent within project lifetime. Stays alive for follow-up after task completion.
- **L3:** Persistent within area lifetime. Stays alive through design and execution phases — the continuity between designing and overseeing execution is valuable.
- **L4:** Persistent within workstream lifetime. Stays alive to manage multiple task cycles, collapses when workstream completes.
- **L5:** Task-scoped. Collapses after task completion.

### Execution Strategy

Default pattern: parallel design → L2 compatibility review → sequential execution.

L2 spawns **planning-L3s** to design in parallel — a **coordinated planning round** (M55), not independent silos. Each planning-L3 produces a detailed design for its area and **renegotiates interfaces upward** where L2's coarse contracts don't hold. Designs come back; **L2 runs a compatibility review** — catching incompatibilities, interface mismatches, conflicting assumptions, and cross-module interface ripples — then locks the interfaces. L2 defines the contracts between areas upfront; planning-L3s design the *how* within (and push back on) those contracts. Interfaces are **fluid during this cascade, frozen after the lock.**

**Two distinct things must not be conflated:** the **walking skeleton is an ungated de-risking spike** — built early, before the gate, to prove the substrate's connections wire up (B15/C22/M55). **Gated execution is different**: it happens *after* the plan-alignment gate passes. The skeleton proves the shape; the gate authorizes the build.

Once the planning round is complete and the **plan-alignment gate has passed** (see Operating Cycle and `PLAN-ALIGNMENT-GATE.md`), **fresh execution-L3s** are spawned (C21): each takes its frozen design and spawns L4 Workstream Coordinators to execute it. Execution proceeds sequentially by default — one phase at a time, in dependency order. Sequential execution is simpler, easier to debug, produces clearer audit trails, and avoids hitting session usage caps. Parallel execution is available when genuinely needed, but not the default.

After each execution phase completes, real information (not assumptions) feeds back. Later designs may need revision — L2 and L3 should expect this. This is agile with upfront architecture: design broadly, execute in phases, revise as real information emerges. Designs are expected to evolve; interfaces are expected to hold.

L2 chooses the right strategy per project. Some work is highly parallel (independent areas with clean interfaces), some deeply sequential (each piece depends on the last). The architecture supports both; the strategy is an L2 decision, not a system constraint.

### Failure and Timeout

Normal liveness is observed through runtime hooks, not a wall-clock timeout. At each observed turn
end the existing return-contract projection finds the owed product, derives a legitimate wait
(including live children, a pending gate verdict, or an open question), or reminds the seat to
produce/explain. Process death, a stuck in-flight tool, and missing/hook-less runtime edges retain
the pane/transcript/file/CPU evidence floor. Full mechanics live in `WATCHDOG.md`,
`operational/shared/comms-protocol.md`, and `operational/shared/agent-lifecycle.md`.

Thresholds are empirical — they depend on task type, not level. A research task legitimately takes longer than a code fix. The spawning level sets expectations per task, not per level.

**Failure recovery:** A stuck or crashed session is recoverable by design. Artifacts capture work in progress (principle 2). The parent level can collapse the failed session and spawn a fresh one that reads the workspace to pick up where the previous one left off. No special recovery protocol needed — the stateless design makes respawning the default recovery mechanism.

**Cascading failure prevention:** Each level only monitors its direct children. If an L5 is stuck, L4 handles it (inquiry → respawn or escalate). L4 doesn't escalate to L3 unless it can't resolve the issue itself. This natural containment prevents a single stuck L5 from cascading upward through the entire hierarchy. L1's force-kill override exists for the case where containment fails.

---

## 6. User Interface

### L1 as Primary Interface

The user's default interaction is with L1. The user arrives, shares ideas, gives direction, asks questions. L1 has the conversation that turns fuzzy intent into clear delegation. Once aligned, L1 delegates and the user gets confirmation that the thing is "handled" -- the loop is closed (principle 1).

When results come back, L1 decides how to present them: the right amount of context, the right framing, packaged for the user's decision-making needs. The user sees deliverables and decisions that need input, not process (principle 1).

### Direct Access to Lower Levels

The user is not locked out of any level (principle 11). They can drop into L2, L3, L4, or L5 directly -- to observe, to provide input, to work hands-on. This does not supersede the level's normal process. The level continues operating as designed; the user's input is additional context, not a process override.

The analogy: a watchmaker peering at the gears. Observation and participation without disruption.

### Multiple L1s

The current design covers the project-work L1. Separate L1s are anticipated for:

- **Personal/life domains** -- Different function, different structure. Health, life admin, and other personal matters operate differently than project work.
- **Improvement Workspace** -- System observation, process improvement, meta-analysis. Its "projects" are the system itself. (A separate, future optimizer-L1 capability may someday run out of this workspace — not V1.)

These are separate design tasks. They may not need the same L1-L5 structure -- their cognitive requirements may call for different shapes entirely.

**Decision (2026-03-25):** Life-OS and the L1-L5 agent architecture are confirmed as separate systems — not integrating. Life-OS is a single-agent assistant with persistent memory (capture, consolidation, domain-based knowledge). L1-L5 is a multi-agent delegation hierarchy for project work. They solve different problems with different architectures: Life-OS makes one agent better through memory; L1-L5 makes work better through delegation. They may coexist in the same repository but do not share state, infrastructure, or runtime.

`[PLACEHOLDER: User interface design -- how the user navigates between levels, how status is presented, how the "drop in" experience works. The GUI/visual front-end is a separate workstream but shapes the architecture.]`

---

## 7. Failure and Recovery

### The Escalation Chain

The primary failure-handling pattern is upward escalation through the existing level boundaries. Each level attempts to handle problems within its scope. When it cannot, it packages the situation and sends it up.

- **L5 fails** -- Reports failure to L4 with what happened and what was tried. L4 decides: retry with different parameters, reassign, adjust the task, or escalate to L3 if the workstream plan itself seems wrong.
- **L4 fails** -- Reports to L3 that the workstream plan hit a wall. L3 evaluates: is this a decomposition problem (L4's scope), a design problem (L3's scope), or a direction problem (escalate to L2)?
- **L3 fails** -- Reports to L2 that the area design or execution is blocked. L2 evaluates: is this a design problem (L3's scope), an approach problem (L2's scope), or a framing problem (escalate to L1)?
- **L2 fails** -- Reports to L1 that the project is blocked on something outside its authority or understanding. L1 evaluates: resolve it, reprioritize, or surface it to the user.
- **L1 surfaces to user** -- Only for genuine decisions or blockers that require owner judgment. High threshold (principle 1).

### Drift Detection

Drift — when work subtly diverges from what was intended — is a central risk in a stateless system. The primary cause is context loss from compacts: an instance reconstructs imperfectly from artifacts and shifts direction without realizing it. This makes drift both a context management problem (better artifacts, active context management) and a gatekeeping problem (explicit alignment checks at each boundary).

Drift is fought structurally, not by vigilance alone. Two mechanisms carry the load:

- **Per-level gatekeeping against a frozen rubric.** L3, L4, and L2 are the primary per-level gatekeepers; each anchors on the *pre-authored, frozen* spec/rubric for the level below (D26) and checks the result against it. L4 checks L5's output against the frozen acceptance tests. L3 checks L4's output against its design. L2 checks L3's output against the approach. Because the criteria were authored *before* the work and are read-only to the worker, this is a check against a fixed anchor, not a negotiation with the output (the anti-theater temporal rule; see Operating Cycle).
- **The plan-alignment gate against intent.** Per-level fidelity does *not* compose into global fidelity (the telephone problem). The plan-alignment gate reads the assembled *whole* plan against the user's tagged intent once, before any code, catching gaps, scope creep, and semantic drift that survive local review — including drift introduced at the prose→ID minting seam. It is the system's #1 anti-drift mechanism; full design in `PLAN-ALIGNMENT-GATE.md`.

L1 catching drift should be very rare — if it happens, something got past both the per-level rubrics and the gate. When drift *is* detected, the detecting level kicks the work back down with corrections (bounded bounce-backs; see `QUALITY-GATE.md`). This is normal operation, not failure — it's the quality-assurance loop that makes trust-based delegation safe. Spec-faithfulness/drift is the system's #1 verification target; execution-quality optimization is deliberately deferred behind it (J43).

### Reconstruction from Artifacts

Because the system designs for statelessness (principle 2), any crashed or stalled process can be replaced by a new instance that reads the same artifacts. The documentation schemas must be sufficient for a fresh instance to pick up where the previous one left off. This is the deepest form of fault tolerance: the system's resilience is equal to the quality of its documentation.

---

## Open Design Work

The build philosophy is infrastructure first, specifics emerge. Build the skeleton that things plug into -- the shapes, interfaces, and slots -- then fill in the specifics as you lay track.

### Architecture (structural decisions that shape everything else)

~~1. **Permission boundaries** -- DONE. Workspace permissions, edge behavior, escalation dynamics. *(Section 1)*~~
~~2. **Reporting contracts** -- DONE. Event-driven protocol, per-boundary mechanics, bus message format (pointer/nudge). *(Section 2)*~~
~~3. **Escalation criteria** -- DONE. Per-level triggers, payload format, urgency signaling. *(Section 2)*~~
~~4. **Documentation schemas** -- DONE. Workspace structure, document types, edit policies, naming conventions. *(Section 3, details in NOTES.md)*~~
5. **Invocation protocol + spawn mechanism** -- Runtime-abstracting spawn (semantic brief + per-runtime adapter), brief structure/discipline, bootstrapping. *Largely resolved (Section 4 + `runtime-and-model-map.md`); documentation-scaffolding-at-spawn-time detail remains (NOTES.md).*
6. **Concurrency mechanics** -- Instance caps, tracking, throttling, L1 allocation decisions. *(Section 5)*
7. **Agent lifecycle** -- Persistence, bus routing, collapse + 2w resurrect/audit window, context management, lifetime limits. *Resolved at the architecture level (Section 5); 2w-window mechanics (replay vs. re-spawn vs. re-run, reap trigger) being settled in the dry-run.*
8. **Timeout and failure design** -- Timeout values, cascading failure prevention, circuit breakers. *(Section 5)*
~~9. **Git integration** -- RESOLVED. Branch strategy, PR-as-review, merge conflict protocol. *(Covered by `operational/shared/git-protocol.md`.)*~~
10. **L1 portfolio documentation schema** -- L1's workspace is portfolio-level, not project-scoped. *(TODO in NOTES.md)*

### Configuration (per-level specifics that plug into the architecture)

These fill in once the architecture is set. Highly iterative.

11. **L1-L5 level configs** -- Behavioral design, tooling, metacognition schema, runtime/model assignment per level. *Souls are deprioritized for V1 — behavior is framed primarily through positive role boundaries + the brief (see `agent-definition-principles.md`); soul.md files can stay one-line pointers. Operational configs (`operational/L1`…`L5`) now have their architecture slots.*
12. **Metacognition schemas per level** -- Mental models, skills, rubrics. Upper levels need strategy and communication; lower levels need verification and craft skills.
13. **Time awareness** -- Can L2 schedule and pace work across days?
14. **User profile document** -- Context the levels need to orient around the user; the intake methodology (K45/M50) grounds elicitation in it. *(TODO in NOTES.md)*
~~15. **Connect generative skeleton to architecture** -- Resolved/evolved. The decomposition methodology (C4 + DDD + SDD + hexagonal ports; deep-modules as *rubric*, not backbone) is captured in `DECOMPOSITION-METHODOLOGY.md`; L2's architect-process applies it (see L2 above). *(Supersedes the earlier pointer to `PROJECT-PLANNING.md`, which did not contain the method.)*~~

### Product / UX (separate workstream, downstream of architecture)

16. **User interface / navigation** -- How the user moves between levels, observes, participates. Terminal works to start.
17. **Multiple L1 design** -- Personal/life L1, Improvement Workspace L1. May need different structures.
18. **Benchmarking** -- How to measure whether the system works better than flat dispatch.

---

*Created: 2026-03-12*
*Last consolidated: 2026-06-02 (5-level migration; operating cycle + dual design/build cycles + plan-alignment gate boundary; per-level model/runtime; bus+docs comms; need-to-know visibility graph; path-spine addressing; collapse + 2w resurrect/audit; V1 = software-building). Source: `working-notes/consolidation-plan-2026-06-02.md`.*
*Status: Section 1 at build-resolution; later sections coarser. Process detail in the sibling docs named above.*
*Governing documents: VISION.md, DESIGN-PRINCIPLES.md*
