# AI Architecture — Project Planning Process

How work flows from intent to built software. The process is derived from real-world professional services — architecture firms, management consulting, software solution design. These disciplines independently converged on the same pattern: intent is captured, a concept is designed, the concept is validated, the concept is detailed, the details are executed. The pattern is universal because the problem is universal: turning an idea into a built thing through coordinated expert effort.

This document defines the planning spine — how intent becomes a *validated plan*, and how that plan becomes built software. It is deliberately altitude-stable: the per-unit operating mechanics live in the level docs and the methodology docs, and are cross-referenced here.

**V1 scope:** the process described here is for **building software**. General task-type breadth (ML pipelines, market studies, design-led products) is the long-term destination, not the V1 beachhead. Where this doc says "the thing being built," read "software."

**Two anchors this doc points to, never duplicates:**
- `DECOMPOSITION-METHODOLOGY.md` — *how* L2 and the planning-L3s (L3&) carve the system (C4 + DDD + SDD + hexagonal ports as the backbone; deep-modules as the rubric that pressure-tests a carving, not the backbone itself).
- `PLAN-ALIGNMENT-GATE.md` — the single hard checkpoint between designing and building, and the requirements-traceability spine that makes it work.

---

## The Shape of the Process

The whole process is **two nested `Plan → Execute → Review` cycles joined at one boundary** — not a single global waterfall:

```
  INTAKE                 ── structured intent capture (L1 + parallel grilling session)
    │
  DESIGN CYCLE           ── L2 architect process → planning cascade → validated plan
    │
  ══ PLAN-ALIGNMENT GATE ══  ← the one hard checkpoint; the vertex of the V
    │
  BUILD CYCLE            ── fresh execution-L3s → L4 → L5 write code against the frozen plan
```

The design cycle produces a *validated plan* and never a line of code. The build cycle produces validated code and never re-opens the architecture. The **plan-alignment gate is the only boundary between them** (full design in `PLAN-ALIGNMENT-GATE.md`).

This is **front-loaded design with rolling per-phase execution-planning**, not a waterfall. The architecturally-significant decisions are made up front, before building starts, because concept errors cascade and are cheap to fix only at plan-time. But execution-planning is *rolling*: each unit's detailed plan is produced just before that unit is built, so real information from earlier phases feeds the later ones. Architecture is committed early; task decomposition stays just-in-time.

Underneath, every unit of work runs its own small `Plan → Execute → Review` cycle, separated from its neighbors by clean context (see the per-unit operating cycle in `DESIGN-PRINCIPLES.md` and the level docs). The big-picture cascade below is how those per-unit cycles compose into a project.

---

## Phase 1: Structured Intake (L1 + User)

The user owns the idea — what, why, for whom, the problem being solved. L1 (System Orchestrator) runs a **structured intake** to turn that idea into a precise, tagged intent spec. This is not requirements-gathering by checklist; it is an opinionated elicitation process whose goal is to find exactly where the user is opinionated and exactly how technically fluent they are in each area, so the rest of the system knows what to decide *for* the user and what to bring *back* to the user.

### The intake method

- **Outcomes-first.** Start from what success looks like in the user's own terms — the outcomes and the must-never-fails — not from a feature list or a tech stack.
- **Tradeoff-probing to detect opinionated vs delegated.** People reveal their real opinions when shown a *fork*, not when asked "do you care about X?". L1 surfaces concrete tradeoffs ("A biases toward cost, B toward latency — which way?") and reads which forks the user has a stake in. The ones they engage with are *opinionated*; the ones they wave off are *delegated*.
- **Variable-depth drilling.** Drill **deep** on opinionated and risky areas; stay **shallow** on delegated ones. Depth is spent where the user has a stake or where getting it wrong is expensive — not uniformly.
- **Capture technical fluency per area, alongside opinionated/delegated.** For each area, L1 records not just *does the user have an opinion* but *how technically fluent are they here*. This second axis is load-bearing downstream: it determines whether the plan-alignment gate later surfaces something to the user as a technical claim or as a plain-language implication (intake-calibrated render-depth — see `PLAN-ALIGNMENT-GATE.md`).
- **A tagged living spec.** Every requirement is tagged **`decided`** (the user resolved it), **`delegated`** (left to professional judgment below), or **`deferred`** (resolved later, at the last responsible moment). The spec is living during intake and frozen as the signed brief at the end. Must-never-fail requirements are flagged and **decomposed to atomic, individually-testable obligations** at intake — a compound must-never-fail minted whole is the highest-stakes place for silent loss.
- **Reflect-back.** L1 plays the captured intent back to the user — including the tags and the must-never-fail decomposition — and the user confirms or corrects.

### The parallel grilling session

The deep elicitation is heavy work that would clog L1's context. So the drilling runs in a **separate, parallel session**: L1 dispatches the user (and/or a dedicated intake session) to do the heavy lifting of producing the spec artifacts (SDD or whatever artifacts fit, in the right order), and **only the finished spec returns to L1.** L1 ingests the spec with a clean context. This is the same clean-context discipline the whole system uses — the producer of a heavy artifact and the steward of the thin distilled result are separated.

### L1 as intent guardian

L1 does not just capture intent and hand it off. L1 **writes it down** (the tagged spec becomes the signed brief) and then **guards** it for the life of the project: when L2 proposes a concept, L1 checks it against the captured intent *before* surfacing anything to the user, and frames any divergence concretely ("you said X; the concept does Y instead, because Z"). The **user remains the ultimate reviewer of intent fidelity** — L1 is the guardian who keeps the bar honest and keeps noise off the user's desk, not the final judge.

**Output:** the **tagged intent spec** (the signed brief). Each requirement gets a stable hierarchical ID at intake (`R-001`, `R-002`, …) carrying its tag, priority, parent outcome, must-never-fail flag, a verbatim **ID→intent-span map** so the prose→ID minting is inspectable downstream, and a **`reflect-back` status** (`pending` until the user confirms it at the Phase 1 reflect-back — or at Phase 3 concept validation for L1-derived requirements — then `confirmed`). The `reflect-back` field is the producer of the stamp the plan-alignment gate's Check 1 reads to forbid freezing on unconfirmed foundations. IDs are minted **only** here; everything below either traces to an intake ID or is sanctioned scope (see Requirements Traceability in `PLAN-ALIGNMENT-GATE.md`). This ID spine is one and the same as the agent-address spine and the workspace-path spine (see "One Spine," below).

---

## Phase 2: The L2 Architect Process (Concept Design)

L2 (Project Architect) receives the tagged intent spec and runs the **real architect's decision-process** — the actual workflow a senior architect uses, copied deliberately. L2 produces the fundamental shape of the solution as a *design artifact* (a coherent picture of how the thing works), not a list of options.

### What L2 actually does

- **Identify the architecturally-significant decisions.** Not every decision — the ones that are expensive to reverse, cross module boundaries, or constrain everything below. Those are L2's to make.
- **Decompose to components + responsibilities + interfaces, then stop at sufficient resolution to delegate.** L2 carves down only until each piece is well-enough specified to hand to an L3 — and no further. Over-resolving robs the L3 of the decisions that are properly theirs.
- **Last Responsible Moment + subsidiarity.** Decide cross-module and expensive-to-reverse things **now**; **defer module-internal and domain-deep decisions downward, with constraints.** A deferred decision is not a gap — it is delegated *with the constraints that bound it*, and those constraints are exactly the rubric the L3 is later held to (D26). Subsidiarity: the decision is made at the lowest level that has the context to make it well.
- **Recognize and apply known patterns.** Where the problem matches a known shape, name and reuse the pattern instead of re-deriving it.
- **De-risk with spikes.** Where a decision turns on something unknown, run a spike to learn before committing. The **walking skeleton is the first and largest such spike** — an ungated, early end-to-end thread that proves the connections before the gated build starts (see "Walking Skeleton," below).
- **DDD is the carving sub-method inside this**, not a replacement for it. Domain-driven design is how L2 (and the planning-L3s) *find the seams* — bounded contexts, aggregates, where to cut. It sits inside the architect process, not above it. The full carving methodology is `DECOMPOSITION-METHODOLOGY.md`.

### Why L2 delegates (and why it isn't about capability)

L2 delegates downward for **role separation and context/bandwidth preservation, not capability.** The model below is the same model; an L2 *could* do an L3's detailed design, just as a director *can* do an associate's work — it's simply not the director's job, and doing it would clog the bandwidth the role exists to protect. **Domain expertise loads per-domain into the owning L3's loadset**, not into L2. L2 holds the cross-cutting architectural picture; each L3 holds the deep domain knowledge for its area.

### The substrate is established first

Before the feature areas, L2 establishes the **substrate**: the cross-cutting stable core that every feature area depends on — Money/value types, IDs, events, audit, the idempotency primitive, the base data model. This is **not a peer feature module**; it is the foundation the rest is built on, and it is **built first via the walking skeleton** (resolution B14). Dependencies point toward this stable core; nothing volatile sits at the center. The substrate's interfaces are the sockets the feature areas plug into.

### L2's output: ADR-style

L2's concept is delivered as:
- a **component map** (the major areas of work and how they connect),
- **interface contracts** between areas (the sockets, defined by the core),
- **ADRs** — one per architecturally-significant decision: the decision, its rationale, and a status (`decided` / `deferred`), and
- **per-module specs** in which the deferred decisions appear as **constraints** — i.e., the frozen rubric each L3 is held to (D26).

ADRs pull quadruple duty: they are the **handoff contract** to the level below, the **anti-drift anchor** the gate and reviewers check against, the **audit/optimizer substrate** (the optimizer-L1 reads decision history), and the **statelessness rationale-preservation** layer (a fresh instance recovers *why* from the ADRs, not just *what*). Each ADR and module is tagged with the intent IDs it serves (the trace-block obligation; see `PLAN-ALIGNMENT-GATE.md`).

### Provisional interfaces, progressive hardening

L2 is not the domain expert for every area, and upfront interface design is fragile — one mechanism resolves both. L2 proposes **coarse, provisional interfaces.** The domain-deep planning-L3 pressure-tests them against the area's real constraints and **renegotiates upward** where the coarse interface won't hold. The **walking skeleton runs early on these provisional interfaces** (not on negotiated/frozen ones) and feeds its findings back into the cascade — reopening the relevant ADR and the compatibility review where reality contradicts the concept (see "The Walking Skeleton," below). Interfaces are therefore **FLUID during the planning cascade and FROZEN only after the plan-alignment gate PASSes** — fluid exactly long enough to get them right, candidate-locked at the compatibility review, and frozen by the gate before any code is written against them.

**Pressure-test before freeze (not just prose).** A provisional interface is not made safe by labelling it "provisional." Before it can be frozen, the contract's **enums and ports must survive a pressure-test against execution reality** — the async flows and real keyspaces the build will actually exercise — performed by the planning-L3 renegotiation and the walking skeleton. A checker can observe this: the candidate contract carries evidence that each enum value and each port was exercised (a skeleton thread that traversed it, or a renegotiation note that revised it), not merely a "provisional" tag. The sim's lesson was concrete: an enum that could not express "accepted, pending confirmation" and a missing intent→order port both surfaced only under build pressure, after a premature freeze. Full statement in `DECOMPOSITION-METHODOLOGY.md` (Contract-first / provisional-interface hardening).

---

## Phase 3: Concept Validation (L2 → L1 → User)

L2 sends the concept back to L1. L1 evaluates it for **fidelity to intent** — does this serve what the user described? — not for technical quality, which is L2's domain. L1 guards: it checks the concept against the captured intent before anything reaches the user, and frames divergences concretely ("you said X; the concept does Y, because Z").

L1 surfaces to the user what the user asked to see (calibrated by the per-area fluency captured at intake — technical detail for fluent/opinionated areas, plain-language implications elsewhere) plus any genuine divergence from stated intent. The user approves, corrects, or redirects. This validation gate exists because a flawed concept produces flawed details at every level below, and no excellence in execution rescues a misconceived design.

This is a *concept-level* check on intent fidelity — it is **not** the plan-alignment gate. The plan-alignment gate (Phase 5) validates the fully assembled, distributed plan as a whole, after the planning cascade has detailed every area.

---

## Phase 4: The Planning Cascade (a coordinated round)

The approved concept is detailed by the L3 layer in a **single coordinated round**, not an open-ended fan-out. This is the design cycle's *Execute* phase: it produces the detailed plan, never code.

### Planning-L3s run as a coordinated round

L2 spawns a **temporary planning-L3** per area. Each planning-L3 takes its area assignment (scope, constraints, the provisional interfaces, the resolved decisions that bound it) plus the full concept for context, and produces the area's detailed design as a `plan/area-{name}.md` in L2's planning workspace. It carries the domain-deep expertise for its area — this is where per-domain knowledge loads. The planning-L3 pressure-tests L2's provisional interfaces and renegotiates upward where needed.

The planning cascade is a coordinated design round, not an unconstrained live fanout. L2 may register
the known planning-L3 siblings as visible planned nodes, then review their outputs together in the
compatibility review. Runtime admission is **serial by sibling order** for all L2-owned L3 children,
including planning-L3s and execution-L3s (L3): L3.1 is admitted first; L3.2 waits on L3.1; L3.3 waits on
L3.2. The next L3 starts only when its predecessor has passed its own gate. This is process
structure, not an infrastructure throttle: the admitted L3 may still decompose downward normally, and
lower L4/L5 fanout is not capped by this rule.

> **AMENDED 2026-06-16 (user ruling — zero-L3 retired):** a project admitted into the L1-L5 build
> harness always passes product execution through an L3 area/module owner. For a trivial area, L2
> may use one recorded L3 instance to carry both area design and execution-management responsibility,
> but that L3 still delegates implementation through L4/L5. L2 does not spawn L4 directly and L3 does
> not write product code inline. Non-collapsible at any scale: the frozen intent anchor, the L3 owner,
> acceptance-before-executor (M51), the independent L5+ review, and L2's composition judgment.

**Threshold-gated split.** A *substantial* area warrants the planning-L3 / execution-L3 split — the planning-L3 collapses after producing the design, and a **fresh execution-L3** is spawned later to own and build it (clean-context separation of planning from execution). A *trivial* area may use one L3 owner for both area design and execution management. That is a planning-layer simplification only; implementation still runs through the L4/L5 harness spine. Depth is variable at the planning layer too (resolution M53; the split mechanics are C21).

### L2 compatibility review (the round closes here)

The planning-L3s submit their detailed designs together, and **L2 reviews them as a set** — this is the step that catches what parallel design cannot:
- Do interface contracts **match across areas**? If area A emits X and area B expects Y, it is caught here.
- Are there **gaps** — work no area claims?
- Are there **conflicting decisions** — area-level choices that contradict each other or the concept?
- Does the combination still **serve the concept** — do internally-sound areas collectively drift?

Cross-module interface ripples and renegotiations surface in this review. (This is the `parallel design → L2 compatibility review → lock` pattern confirmed in `ARCHITECTURE.md` §"Execution Strategy.")

If the compatibility review finds a repairable child-owned issue in an area that has already passed
its area gate, L2 records the finding in the compatibility artifact and opens a fresh same-address
area incarnation with a delta repair brief. The repaired area must pass its gate again before it can
be used for candidate-lock or forwarded into the plan-alignment package. Cross-area contract changes
still renegotiate at L2 before repair work is spawned.

### Candidate-lock the interfaces (freeze is a post-gate act)

Once the compatibility review clears, the interfaces are **candidate-locked, not frozen.** The compatibility review produces *candidate* interfaces — negotiated and cross-area-checked, but not yet contracts execution is held to. The **freeze is a distinct, later act that happens only after the plan-alignment gate (Phase 5) emits PASS.** A checker can tell the two states apart: a candidate interface lives in L2's planning workspace and is still editable by renegotiation; a frozen interface is the read-only `design.md` contract inherited by the fresh execution-L3, written only once the gate has PASSed. Sequencing is strict: **reflect-back confirmation (Phase 3) and the plan-alignment gate (Phase 5) both precede freeze.**

The post-PASS handoff must be visible to the next acting agent. When L2 copies each accepted area
design into the execution-L3 workspace, `design.md` is stamped as frozen for execution and points at
the plan-alignment PASS verdict / validated-plan package that authorized the freeze. This stamp does
not rewrite the design; it prevents a fresh execution-L3 or L4 from seeing a planning-era candidate
header and treating the design as still open.

**Prohibition (no building on unconfirmed foundations).** Do **not** freeze, and do **not** let any area build on, a requirement that is still **reflect-back-pending** — i.e., not yet confirmed by the user in the Phase 3 reflect-back. A requirement in reflect-back-pending state is observable in the trace (it carries no `confirmed` mark); any candidate interface that depends on such a requirement is blocked from freeze until the dependency clears. This is the ordering bug the sim surfaced: interfaces were frozen before the gate ran, on unconfirmed foundations. The correct order is **reflect-back confirm → plan-alignment gate PASS → freeze.**

### Acceptance criteria are authored here, before any code

The planning cascade is also where the **anti-theater temporal rule** is enforced, but the design cycle distinguishes two artifacts. During planning, each level authors the **falsifiable acceptance criteria and review rubric** for the level below — at planning time, before the work, from the spec, by an agent that is not the worker. A level's Plan phase is not "done" until it has emitted spec + acceptance criteria + gate rubric (the **Plan-phase output contract**). This anchors the work to a pre-authored standard without pretending that the design cycle must produce every executable leaf suite.

For L5 implementation tasks, the executable acceptance tests are a build-cycle operationalization of those frozen criteria. Execution-L4 performs rolling task decomposition, then opens a dedicated L5 `test_author` child to turn the frozen criteria and task spec into the executable `acceptance.md` / test package before the implementation L5 is spawned. That package is reviewed by L5+ and approved by L3 for spec-faithfulness before implementation can rely on it. A future first-class `#test` seat can replace the L5 `test_author` mechanism without changing the invariant.

The cascade's output is the **candidate validated-plan package**: the candidate architecture doc + the
N area designs + workstream definitions/plans + falsifiable acceptance criteria + the rubrics + any
risk/deep-probe evidence. It is not required to contain every executable leaf test suite before the
build cycle opens. Design-cycle falsifiable criteria use `kind: test`, the same structural class the
later executable tests use.

L2 submits that package with a minimal machine sidecar: each artifact row gives its node-relative
path plus the trace IDs actually in that artifact, and every MNF ID gets a failure-path mapping to
an in-package `kind: test`. A second semantic manifest physically partitions verification files
from construction modules. `plan-alignment-ready.json` points to the package and both sidecars. The
harness reads tags/MNF only from the current frozen intent receipt, builds the trace graph and
coverage report, and refuses gaps/creep to L2. A passing Q3 bundle parks `semantic_cell_pending`
without waking L1; the daemon runs the exact-read blind semantic cell and wakes L1 only when its
evidence index is complete. L2 then waits for L1's `plan_alignment_decision`. The package becomes
the frozen validated plan only after the
plan-alignment gate emits PASS.

---

## Phase 5: The Plan-Alignment Gate

The single hard checkpoint between designing and building. It reads the **assembled plan as a whole** against the user's tagged intent and asks the one question per-level reviews structurally cannot: *does local fidelity at every step compose into global fidelity to intent?* It catches the three drift classes that survive local review — **gaps** (dropped requirements), **scope creep** (unrequested additions), and **semantic drift** (requirements traced but subtly wrong) — and it inspects its own first translation (intent prose → requirement IDs) rather than treating it as axiomatic.

Its success output is a **signed validated-plan artifact**. L1 triages the completed semantic
evidence and elevates only required drift, window disagreement, contradiction, atomization, or
MNF-adequacy findings, one confirmable owner question each. If nothing elevates, L1 PASS stands
alone. No execution-L3 is spawned and no build harness is unlocked until PASS. **The interface
freeze is one of the acts the PASS authorizes:** the candidate interfaces from Phase 4 become the
frozen, read-only `design.md` contracts only on PASS.

The handoff into this gate is nonterminal. L2 does not sign `DONE`, does not use the product
`L2#review` gate, and does not open a blocking question for the normal phase transition. It writes
`plan-alignment-ready.json`; L1 returns PASS or FAIL with `harnessctl plan-alignment-decision`.

The full design — deterministic coverage, physical blind windows, output-contract specificity,
adversarial comparison, portfolio coherence, once-per-intent atomization, elevate-only L1 triage,
and trace-graph incremental re-gating — lives in `PLAN-ALIGNMENT-GATE.md`.

---

## Phase 6: The Build Cycle (Execution)

On PASS, the **build cycle** begins. A **fresh execution-L3** (a different agent from the planning-L3) takes its frozen, gate-approved area design and drives the build down through L4 and L5. When it reuses the planning-L3's logical address, the harness gives it a fresh active work surface: current `plan.md`, `report.md`, and seat inbox/sign-off files belong to the execution incarnation. Prior planning forms are preserved as control-plane provenance under `<runtime>/.harnessd/incarnation-archives/`, outside the agent workspace; gate-id review directories remain durable gate evidence.

- **L3** owns the area: `design.md` is the frozen contract, `plan.md` is the living execution layer. It sequences workstreams by dependency and risk, mostly sequentially (2–4 L4s active), parallel only where areas are genuinely independent. Later work benefits from earlier results.
- **L4** decomposes its workstream into tasks against the area design. The validated plan supplies frozen workstream criteria and rubrics; L4 performs rolling task decomposition and uses the normal M51 L5 `test_author` path to turn those criteria into executable task acceptance before an implementation L5 opens. If a spec change forces a post-design refresh, the same path is used with `test_refresh: true`; L3 approves the refreshed package for spec-faithfulness after L5+ accepts it, and the implementation target must bind the approved refreshed `tests/` package before L5 opens.
- **L5** is an **execute–review pair.** The executor (Codex *harness* + GPT-5.6 Sol *model*) writes the code and runs the pre-written acceptance tests + its own unit tests + **CI (the automated floor).** **L5+** — a *separate* reviewer agent on a **different runtime** (Opus) for judgment diversity — does its own testing and reviews against spec, then **accepts** (forward; both collapse) or **bounces** (the executor keeps its context and continues; bounded loop). Reviewers verify code against the *same* acceptance tests and rubrics frozen during the design cycle — they cannot be edited to match the code.

Cross-runtime briefing follows the runtime-neutral task contract + thin runtime adapter pattern: the semantic brief is identical across runtimes; only the tool manifest, harness invocation, and output format are runtime-specific, injected by the adapter at spawn. GPT-5.6 Sol seats get maximally decision-complete briefs (they will not fill gaps with good architecture), acceptance tests as the primary anchor, and an explicit **escalate-ambiguity-don't-decide** rule. Full treatment in `runtime-and-model-map.md`. The model rule throughout: **Opus 5.0 for generative/architecture seats; GPT-5.6 Sol (Codex harness) for pedantic/checking/execution seats.**

Each level's right-arm review gate verifies what it received against the frozen design and rubric it set (review at altitude — composition + fidelity, not re-doing the level below). Independent review at each level is **in for V1**, not deferred — it is integrated into each level's process (the L5/L5+ pair, the L4 right-arm gate, the L3 gate rubric). Detailed mechanics live in `QUALITY-GATE.md`, the level docs (`operational/L3/`, `operational/L4/`, `operational/L5/` — their `role.md` / `config.md`), and `runtime-and-model-map.md`.

---

## Phase 7: Integration and Delivery

Results flow up through the frozen contracts. Each level evaluates what it received against the design it set; artifacts are written at each level; the visibility graph and the bus carry the notifications (a message is a pointer/nudge — durable truth lives in the docs; see `COMMUNICATION.md`). When a parent has received all results from its children, it collapses them and synthesizes.

L2 does the final cross-area integration review — the areas were designed to compose; this confirms they actually do. L2 reports to L1: what was built, how it maps to the original intent, where the implementation departed from the concept and why. L1 shapes the delivery for the user — they receive the result framed for their needs, not a project report. The loop that opened at intake closes here.

---

## The Walking Skeleton (an early de-risking spike, not gated execution)

The walking skeleton is an **ungated, early de-risking spike** that threads one thin path end-to-end — through the substrate and across the area interfaces — to prove the connections hold before the architecture is frozen. It is **distinct from gated execution**: it *informs* the plan; the build cycle is the *gated* construction that follows PASS.

**Owner.** The **Project Architect (L2) spawns it** as a dedicated **throwaway spike thread** — a short-lived agent whose only job is to run the skeleton and report back. It is not a planning-L3 and not an execution-L3 — the spike sits **outside the seat taxonomy** (ruled 2026-07-12): a throwaway thread with an address, not a seat; it has a named owner so the feedback path and the discard rule have someone accountable to them.

**Address and workspace.** The spike gets a real **one-spine address** (e.g. `proj/_skeleton`) and a **workspace path** under L2's planning tree (e.g. `plan/_skeleton/`), same as any other node — so its findings, threads, and discard are all observable in the tree, not ad-hoc.

**When it runs — EARLY, on provisional interfaces.** The skeleton runs **after the concept (Phase 2), against the *provisional* interfaces** — *not* after the cascade against negotiated/frozen ones. This corrects the earlier self-contradiction: the skeleton does not "validate the negotiated interfaces" after they settle; it runs *before* they settle and is one of the forces that makes them settle correctly. Running early is the whole point — it surfaces integration reality while the contracts are still cheap to change.

**Feedback channel (defined, not implied).** Skeleton findings have a defined path back into the cascade: a finding **reopens the relevant ADR** and **feeds the L2 compatibility review / interface renegotiation** (Phase 4). A checker can observe the loop: a skeleton finding produces an ADR re-open entry and/or a renegotiation note on the affected candidate interface. The skeleton is a producer of pressure-test evidence for the enums/ports (see Phase 2's "Pressure-test before freeze").

**Throwaway / discard rule.** Skeleton code is a spike: **it is discarded and never enters the gated build.** No frozen `design.md`, no execution-L3, and no L5 commit inherits skeleton code; only its *findings* (ADR re-opens, renegotiation notes, de-risked decisions) survive. The workspace path is torn down or archived as throwaway after the cascade absorbs its findings.

**A separate store-backed spike for scary decisions.** The walking skeleton is typically **in-memory** and therefore cannot de-risk decisions that depend on real infrastructure behavior — concurrency, unique-constraint enforcement, transactional isolation, real keyspaces. For those, L2 spawns a **separate, store-backed spike** that exercises the real mechanism (e.g. a real DB enforcing a unique constraint under concurrent writes). Same owner/address/discard discipline; different fidelity. Do not assume the in-memory skeleton has cleared a decision that only the store-backed spike can clear.

(Building the AI-architecture system itself is being done walking-skeleton-first via the tabletop dry-run.)

---

## One Spine (requirement-IDs = addresses = paths = branches = rubrics = visibility)

A single hierarchical-path scheme runs through the entire process. The dotted requirement ID minted at intake (`R-003.2.1`), the agent's address (`proj/payments/gateway`), the workspace tree, the git branch, the rubric-file location, and the need-to-know visibility graph are **all the same hierarchical-path/prefix scheme.** A child ID is its parent dotted with a local index; a parent is recoverable by truncation; the visibility graph derives directly from the address (subtree = paths under; siblings = same-parent; parent = path minus last segment). Decided once, it serves the whole system. See `WORKSPACE-SCHEMA.md`, `PLAN-ALIGNMENT-GATE.md` (traceability), and `runtime-and-model-map.md` (addressing).

---

## Design Principles

### Each phase produces a design artifact, not a task list

Tasks emerge from designs. The tagged intent spec is an artifact. The L2 concept (component map + interface contracts + ADRs) is an artifact. Each area's detailed design is an artifact. Task decomposition happens at execution time, rolling, derived from the designs — not as the primary output of any planning phase. A task list with no design behind it has no coherence check; a design is a testable claim about how the thing works.

### Each artifact is validated before the next phase begins — and the whole plan once, as a unit

Intent is agreed before concept design. The concept is approved before the planning cascade. The detailed designs pass cross-area compatibility to produce *candidate* interfaces. And the assembled plan passes the **plan-alignment gate** as a whole before any code is written — and the **interface freeze happens only on that PASS**, never at the close of the compatibility review. Per-level fidelity does not compose into global fidelity, and catching drift at plan-time is the only point where the fix is cheap; freezing before the gate (as the sim did) locks in foundations the gate has not yet validated.

### Front-loaded design, rolling execution-planning — not a waterfall

The architecturally-significant decisions are made up front; the rest is deferred to the last responsible moment and planned just before it is built, so real information from earlier phases shapes later ones. Architecture is committed early and expected to *hold*; detailed plans are produced rolling and expected to *evolve*.

### Plan and execute are separated by clean context

Planning-L3 and execution-L3 are different agents; the parallel grilling session keeps L1's intake context clean; the gate is a contextual firewall the build cycle inherits the frozen plan through, never the design conversation. The producer of a heavy artifact and the steward of its distilled result are kept apart throughout (pointer-not-payload briefs: every level gets the distilled brief — spec + constraints + interface + ADRs — with raw upstream intent referenced, pullable on demand, not carried).

### Decisions are delegated, with constraints, to the lowest level that can make them well

Subsidiarity + Last Responsible Moment. A deferred decision travels downward as a *constraint*, and that constraint is the frozen rubric the level below is held to. Delegation is about role and bandwidth, not capability — the model below is the same model.

### Spawn-time role identity at every level

Every agent is spawned with a specific professional role identity, set by the level above — not to inject knowledge (the model already has it) but to inject a *lens*: which aspects to foreground, which best practices to default to, which risks to watch for. Positive boundary framing over persona ("soul") elaboration; the framing principles live in `agent-definition-principles.md`. Domain expertise carries default priorities that the user or the level above can override but need not specify.

### Derived from real-world professional services

Architecture firms, management consulting, software solution design — the source disciplines, not analogies. They independently evolved the same pattern because they solve the same problem: turning a client's intent into a built thing through coordinated expert effort. The agent hierarchy instantiates this pattern with LLM agents instead of human professionals; the cognitive sequence is the same.

---

*Created: 2026-03-29*
*Reframed: 2026-06-02 — intake / L2-architect-process / planning-cascade / dual-cycles + plan-alignment gate; 5-level model; front-loaded design with rolling execution-planning. See `working-notes/consolidation-plan-2026-06-02.md`.*
*Replaces: GENERATIVE-SKELETON.md*
*Anchors: `DECOMPOSITION-METHODOLOGY.md`, `PLAN-ALIGNMENT-GATE.md`.*
