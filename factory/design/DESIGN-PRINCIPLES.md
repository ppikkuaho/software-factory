# AI Architecture — Design Principles

Shared philosophy governing the design of a hierarchical AI agent architecture for managing multiple concurrent projects. Every structural decision should be traceable to these principles. If a design decision contradicts one, that's a signal to reconsider the decision.

The architecture is a five-level hierarchy (L1–L5) where each level handles a genuinely different kind of cognitive work. It is not an org chart roleplay — it's a structural separation of cognitive tasks, context management boundaries, and trust relationships, informed by organizational theory as a research corpus.

**V1 scope:** the first system builds *software*. General-purpose portfolio breadth — ML-research pipelines, market studies with code deliverables, arbitrary task types — is the long-term destination, but software-building is the V1 beachhead. "Handles any task type / flexibility is a core requirement" is **not** a V1 claim; it is retired as a stated requirement and re-earned later. These principles are written to be domain-general where they can be cheaply, but where a choice must be made, V1 optimizes for shipping correct software. (A1.)

**Meta-caveat — held softly at this scale.** Many of these principles descend from a body of LLM-and-agent design practice tuned for single prompts and small agent loops. At the scale of a five-level hierarchy of largely autonomous agents, the *mechanisms* transfer (decomposition, clean context, independent verification, design-from-the-receiving-end) but the specific *prescriptions* often must be re-expressed rather than copied. Treat the principles as load-bearing direction, not literal rules to apply verbatim; when one seems to fight the scale, re-express the mechanism for this altitude rather than discarding it or applying it mechanically. (L46; see agent-definition-principles.md.)

---

## 1. Owner, Not Operator

The architecture exists to move the user from operator to owner. AI execution became cheap — the bottleneck shifted from "can't build fast enough" to "can't lead that many projects simultaneously." The operator sits inside Claude Code watching output scroll, directing approach, checking work. The owner says "I need this" and receives "done, here's what happened." Every design choice should be evaluated against: does this move the user further from the terminal and closer to the results?

The user's biological context window is precious and finite. Every line of output, every status update, every intermediate result they see that they didn't need to see is cognitive waste. The system should protect the user's attention as aggressively as it protects LLM context windows. This is ultimately about empowerment — delegation frees the owner to be more ambitious, direct attention where it has the highest leverage, and take on more than any one person could operate directly.

**Implication:** Default to showing less, not more. Deliverables and decisions surface to the user; process does not. The user can always drill in when they want to — but the default path is results, not execution traces.

---

## 2. Design for Statelessness

The system is not truly stateless — agents have context windows and we leverage them. But we *design for* statelessness as a discipline, because the moment documentation practices slip, the next compact or crash creates real damage. If you delete all running processes and restart from documentation alone, the system should reconstruct itself fully. This must always be true, not as a fallback, but as a guarantee maintained by continuous practice.

Documentation is not a nice-to-have. It is the system's memory. If something isn't written down, it doesn't exist. Every action that changes state must update the relevant artifacts. The quality of the system equals the quality of the documentation practices. The context window is a performance benefit — it makes the current instance faster and more informed — but it is not a reason to document less. Documentation rigor runs at full regardless.

**Implication:** Every level must have defined documentation schemas — what it writes, where it writes it, in what format. A brilliant agent that doesn't document its work is worse than a mediocre one that documents everything, because the next instance inherits the artifacts, not the brilliance. Documentation practices are first-class architecture decisions, not afterthoughts.

---

## 3. Separate by Kind of Thinking, Not Granularity

Levels in the hierarchy exist because they correspond to genuinely different *kinds of thinking*, not different granularities of the same thinking. Strategy, architecture, operations, and craft are distinct cognitive modes. An executor trying to also be strategic does both badly. A leader managing task sequencing loses the strategic view.

Managing 20 projects across wildly different domains requires all of these cognitive modes happening in parallel. One mind — biological or LLM — can't hold portfolio strategy, project framing, task sequencing, and deep implementation simultaneously. The levels exist because the work demands it.

The number of levels is driven by the number of genuinely distinct cognitive modes the work requires. Each level should represent a different way of engaging with problems — different questions it asks, different skills it needs, different time horizons it operates on. If two levels ask the same kind of question at different scales, they should probably be one level.

The **governing test is a distinct kind of thinking** at each level (A2/L46) — not a permission ladder, not a granularity ladder. A breadth-vs-depth *alternation* between adjacent levels is a useful small-scale heuristic for spotting an artificial boundary (if two adjacent levels share an orientation, the seam between them may be fake), but it is only a heuristic, not the criterion. The criterion is whether the level engages problems in a genuinely different mode — different questions, different skills, different time horizon. A level can pass the kind-of-thinking test without cleanly inverting the one below it.

**Implication:** When designing capabilities for a level, ask: "Is this the right *kind* of thinking for this level?" The levels are cognitive boundaries, not just organizational convenience. Specific level definitions belong in the architecture document; this principle governs how those definitions are drawn.

---

## 4. Trust Intent, Verify Competence

Each level trusts that agents below report honestly — when they say they did X, they did. Management happens through *compressed accounts*, not direct inspection of work product. L4 doesn't read L5's code — it reads L5's report about the code (tests run, coverage, decisions made, concerns flagged). L3 doesn't review L4's task decomposition — it sees the summary of what was done and what's left.

But output quality is independently verified before it moves upward — not because agents lie, but because producers structurally cannot objectively evaluate their own work. LLMs systematically overestimate their own output quality, just as a junior developer honestly believes their code is solid. The gap between "I think this is good" and "this is actually good" is where quality escapes.

Every output passes through a quality gate — an independent reviewer at each level, structurally separated from the producing level. The gate evaluates the work against configured dimensions before clearing it upward. The producer never signs off on their own work.

Trust applies to intent and reporting. Verification of competence and quality is structural, not exceptional. The receiving level still evaluates — it inspects process, approach, steps taken, decisions made. It does not inspect raw work — the gate handles output quality verification so the receiving level doesn't need to, and context compression means the raw work isn't even available at the receiving level's altitude. The quality gate independently verifies output quality. Both happen. The goal is that each level's own QC is good enough that the gate rarely finds issues — but the gate exists because you can't guarantee that. The gate is a safety net, not a substitute for each level doing its job well.

**Implication:** Design quality gates at each level boundary. The gate is a parallel review function — with its own persistent context, configurable review dimensions, and right-sized reviewers. This review function is integrated into each level's process (the L5/L5+ pair, the L4 right-arm gate, the L3-gate rubric, etc.); it is not a separate organizational unit. Reporting contracts between levels remain thin (compressed accounts), but quality is guaranteed by the gate, not by the producing level's self-assessment. See NOTES.md, "Quality Gate System."

---

## 5. Context Isolation via Process Boundaries

Each level is a separate process. Context doesn't leak upward — it gets compressed into a return contract at every boundary. The hierarchy is a series of membranes that filter noise.

Context flows downward as *prompts* (with pointers, not contents). Context flows upward as *summaries* (compressed accounts of what happened, not raw traces). Everything in between — internal reasoning, failed attempts, tool traces, intermediate states — dies with the process that generated it.

The compression is structural, not algorithmic. You don't need a summarizer because the architecture forces brevity at every boundary. Each level composes what it sends and receives, which naturally compresses.

Isolation is also a **read** discipline, not only a flow discipline. The default is **need-to-know**, not broad project-wide read access. An agent sees only its own subtree, its same-parent siblings, and its parent — a **visibility graph** that derives directly from the one hierarchical-path spine (an agent's address *is* its workspace path, so "who can read what" is computed from path prefixes, not granted ad hoc). Cross-parent coordination escalates to the common ancestor rather than reaching sideways. Broad project-wide read is superseded; the narrow read-table is the rule. The single sanctioned exception is the god-view held by L1 and the system-improvement function (see principle 11 and F34). (F34/F35.)

**Implication:** Never pass raw conversation history between levels. Never pass file contents when a file path suffices. The inter-level contract is the compression mechanism — design it to carry exactly what the receiving level needs to make its decisions, and nothing more. And scope read access to the visibility graph by default; widening it is a decision, not a convenience. See WORKSPACE-SCHEMA.md and COMMUNICATION.md.

---

## 6. Protect Every Context Window

Every level should spend its tokens on *its cognitive task*, not on setup, routing, or boilerplate. Infrastructure between levels handles plumbing invisibly.

This applies most critically to L1, which is the longest-running and most context-sensitive level. L1 should be able to spawn an L2 with the token equivalent of a short email — "game project: implement dialogue branching." The invocation protocol, project context loading, role definition, file discovery — all of that is infrastructure, not L1's job.

But the principle applies at every level. L2 shouldn't spend tokens loading L3's tools. L3 shouldn't spend tokens loading L4's tools. L4 shouldn't spend tokens bootstrapping L5's environment. Each level's context should be maximally devoted to its actual cognitive work.

**Implication:** Design invocation protocols that minimize the spawning level's token expenditure. Predefined configurations, standardized boot sequences, infrastructure that handles the handoff. The cost of spawning should be borne by the infrastructure and the spawned instance, not by the spawner.

---

## 7. Documentation Accumulates Into Institutional Memory

Statelessness (principle 2) means every instance reads its state from artifacts. But there's a compounding property beyond that: documentation *accumulates*. Project framing documents, task records, implementation reports — these build a knowledge base that makes the system smarter over time, not just stateless.

Nothing gets lost. Standard documentation practices are stable and always followed. Each level knows where everything is and will be. New instances bootstrap faster because the artifacts they read are richer. The system's institutional memory grows independently of any individual session or context window.

This is distinct from statelessness: stateless means "can reconstruct from artifacts." Institutional memory means "the artifacts get better over time, so reconstruction gets better over time."

**Implication:** Documentation schemas per level are load-bearing architecture. Design them with the same care as the levels themselves. They determine not just what the current instance knows, but what all future instances will inherit.

---

## 8. Separated Problem Spaces for Optimization

By cleanly separating levels, problems become *diagnosable*. "Our decomposition is weak" is a different problem than "our implementation is sloppy" — and they live at different levels. You can improve L3 without touching L2 because they're architecturally separate.

Right now everything is mixed together and it's hard to even *see* where problems are. The hierarchy makes each level an independent optimization target. You can develop better playbooks for L2, better verification loops for L3, better planning skills for L4, better implementation skills for L5 — all independently.

This extends to process development: with a stable structure, you can do real, systematic improvement. Change one variable, measure the effect, keep or revert. Without structural separation, every change affects everything and nothing is attributable.

**Implication:** When something goes wrong, the first question is "which level owns this problem?" If you can't answer that, the level boundaries need to be clearer. If the answer is "multiple levels" — that's a coordination problem, which is the level above's responsibility.

---

## 9. The Architecture Serves Clarity

Three outcomes the architecture must produce, in order of importance:

1. **Cognitive offload.** The user presents problems and receives solutions. The loop closes when L1 confirms something is handled. The anxiety of "is this being worked on" dissolves because L1 holds the thread. This is the direct response to the leadership bottleneck — the system holds the portfolio so the user doesn't have to.

2. **Clarity of thinking.** The structure forces precision about *what kind of problem* you're dealing with. Is this a "which projects matter" problem (L1)? A "how to approach this" problem (L2)? An "area ownership" problem (L3)? An "execution sequence" problem (L4)? A "how to do this thing well" problem (L5)? Without the structure, these blur together.

3. **Context management.** Each level sees only what it needs. No context pollution — biological or LLM — from execution details when thinking strategically, or from strategic considerations when executing.

Performance improvement is a secondary benefit — better decomposition, right skills at right levels, verification built into the process. But the primary purpose is offload, clarity, and context management. If performance improves but things get messier, the architecture has failed.

**Implication:** When evaluating design choices, ask: "Does this make things clearer?" before "Does this make things faster?" Clarity compounds; speed optimizations often don't.

---

## 10. Organizational Theory as Research Corpus

The architecture draws on organizational theory — decision rights, information compression, span of control, natural boundaries in cognitive labor — as a research source, not a template. Decades of thinking about how to partition work, manage information flow between levels, and handle escalation are directly applicable.

What's relevant: the information architecture, the cognitive partitioning, the principles of delegation and trust, the patterns of when to escalate vs. handle locally.

What's not relevant: human management artifacts — bureaucracy, politics, status hierarchies, performance reviews. The risk is importing patterns that exist because of human social dynamics, not because they serve the cognitive task. Every borrowed concept must earn its place by serving the actual problem (cognitive separation, context management, clarity) rather than by analogy alone.

**Implication:** When reaching for org theory concepts, always ask: "Does this exist because it serves cognitive partitioning, or because it serves human social dynamics?" Only import the former.

---

## 11. Observability Without Disruption

The hierarchy is not a wall. Every level must be observable — by the user, by higher levels, by system-improvement processes — without being disrupted by observation. Like a watchmaker peering at the gears: you can see everything, touch what you need to, without stopping the mechanism.

This serves both immediate needs (the user wants to see or guide something) and systemic needs (process improvement requires being able to watch how things actually work). Observation is not inspection — it doesn't imply distrust. It's a different mode entirely.

**This is not in tension with need-to-know (principle 5).** Universal observability is a property of the **god-view exception**, not of every level. Ordinary agents do not get to watch each other — they are bound to the visibility graph. Cross-cutting observation is the bounded privilege of the user and the system-improvement function (read-only across the whole tree), which exist precisely to watch the mechanism without disrupting it. "Every level must be observable" means observable *to the god-view*, not laterally readable by peers. (F34; see principle 5.)

**Implication:** Design every level to be transparent to the god-view observers and graceful with unexpected input. External input is additional context, not a process override. The normal workflow continues; the observer participates without commandeering. Do not read general observability as a license to widen the per-agent visibility graph.

---

## 12. Upstream Quality Cascade

Quality problems propagate downstream. A badly framed task from the top produces garbage all the way down, no matter how good the execution. The most important quality to get right is at the highest level that touches the work.

This means the user's conversation with L1 — where intent gets clarified and framed — is the single highest-leverage interaction in the system. A brilliant L4 cannot rescue a task that was misconceived at L1. Investment in upstream quality (clear framing, good decomposition, appropriate approach selection) pays more than investment in downstream quality (better code, better testing).

The reverse also applies: when something goes wrong at the bottom, look up before looking down. The root cause is often a framing or decomposition error, not an execution error.

**Implication:** Weight investment toward higher levels. Design quality assurance that catches problems early. If downstream levels consistently discover problems that should have been caught upstream, fix the upstream process — don't add compensatory complexity downstream.

---

## 13. Documentation Is the Work (direction settled; calibration ongoing)

The system documents continuously, as part of its normal operation — not as a separate step after the work, and not as something the user manages. Like a consultant who takes notes throughout every conversation and writes up every decision: the documentation happens because the system is built to produce it, not because someone remembers to.

Statelessness (principle 2) creates the requirement. Institutional memory (principle 7) describes the benefit. This principle addresses the *mechanism*: documentation must be woven into every level's process so deeply that undocumented work is structurally difficult. The system captures what matters — decisions, reasoning, state changes, open questions — without requiring the user to direct it.

The hard design problem is calibration. Too much documentation becomes noise that drowns the next instance trying to bootstrap. Too little and statelessness becomes amnesia. Each level likely needs different documentation practices tuned to what the *receiving* audience (the next instance, the level above, the user) actually needs. The overhead must be low enough that it doesn't tax the work itself.

**Implication:** The direction is settled and the *what/where/format* is now largely landed — the per-node documentation schema (brief, frozen acceptance/rubric, report, ADRs, design/plan docs) lives in WORKSPACE-SCHEMA.md, and the trace-block obligation makes the upward-trace documentation a hard return-contract side-effect (PLAN-ALIGNMENT-GATE.md). The system handles this, not the user, and it's non-negotiable for statelessness to work. What remains genuinely open is **calibration**: the right granularity per level so documentation aids the next instance without drowning it. That tuning is an ongoing, optimizer-L1 (name retired 2026-07-12 → the principal coordinator, hypothesis-tree project)-informed refinement, not a blocker.

---

## 14. Asynchronous by Default

The system is fundamentally asynchronous. When a level delegates downward, it fires and continues — it does not block waiting for results. Results flow back up when they're ready. The only synchronous interactions are user-to-level conversations (user talking to L1, or user dropping into L2/L3/L4 directly).

This is not a preference — it's a reliability constraint driven by the LLM execution model. A foregrounded subagent that gets stuck without a preset timeout hangs the spawner, freezing everything above it. Backgrounded execution eliminates this risk. The system remains responsive at every level regardless of what's happening below.

The **turn-state/return-contract loop** is the safety net. Runtime hooks durably report turn
running, tool-in-flight, and turn end where the runtime exposes them. At turn end the shared
return-contract projection finds the product, derives a legitimate wait, or reminds the seat to
produce what it owes or explain/escalate. Process death, a long-stuck tool, and missing or hook-less
runtime edges retain the transcript/pane/file/CPU evidence floor. Wall-clock time alone never proves
failure. This is the one proactive behavior in an otherwise event-driven system. It is already
specified in `SUBSTRATE-PHYSICS.md`, `WATCHDOG.md`, `agent-lifecycle.md`, and
`comms-protocol.md`; do not recreate it here.

**Implication:** Default to backgrounded, async execution at every level. Only use synchronous (foregrounded) execution when you specifically need the response before continuing, or to conserve tokens on a known-fast operation. This determines the concurrency model: L1 can manage multiple projects simultaneously because it's never blocked waiting on any of them. A stuck L4 doesn't freeze the chain.

---

## 15. Variable Depth

The five-level hierarchy is the maximum, not the minimum. Not every task requires every level. "Fix this typo" doesn't need a project director to frame the approach. A personal-domain session routes directly to the specialist. L1 should dispatch to whatever level the task actually requires — directly to L4 for straightforward operational work, to L2 for something that needs problem framing, or hand the user off to an L2 for a deep design session that doesn't need L4/L5 at all.

This routing principle applies before a project is admitted into the full product-build cascade. Once
L2 owns a product build, the execution spine below L2 is not arbitrarily skipped: product execution
passes through an L3 area/module owner. A trivial area may use one recorded L3 for both area design
and execution management, but implementation still flows through L4/L5 and L2 does not spawn L4
directly.

The test is cognitive: does this task require a different *kind of thinking* at each level it passes through? If L2 would just pass it straight to L3 without adding judgment, skip L2. The levels exist to add value through their distinct cognitive modes (principle 3), not as mandatory gates.

**Implication:** Design each level's invocation to be independently addressable, not only reachable through the level above. L1 needs the judgment to route at the right depth. Over-processing simple tasks wastes tokens (violating principle 6) and adds latency for no cognitive benefit.

---

## 16. Designed for Evolution

The architecture anticipates its own improvement. Every level's processes — mental models, skillsets, rubrics, behavior design, verification loops — are explicit, versioned, and independently modifiable. Changing how L4 decomposes tasks should not require changes to L3 or L5. The system is built to be reconfigured, not just to function.

This is distinct from principle 8 (separated problem spaces), which says problems are diagnosable. This principle says the *solutions* are swappable. Each level's configuration is hot-swappable: you can change its rubrics, its playbooks, its documentation practices, and redeploy without affecting other levels. This is critical because the system will be highly iterative in its early stages — getting each level right requires rapid experimentation.

This is the infrastructure that enables system improvement processes (such as the Improvement Workspace) to be effective. Without it, improvement requires surgery; with it, improvement is configuration.

**Implication:** Design each level's behavior as explicit configuration, not implicit convention. Prompts, skills, rubrics, documentation schemas — all externalized, all independently versionable. The cost of changing a level's behavior should be low enough that experimentation is cheap.

---

## 17. Right-Size Every Cognitive Task

Every task — execution and review alike — is decomposed until each agent has a unit it can go deep on. Quality comes from decomposition depth. Three rules:

1. **Don't mix task types.** Coders code, reviewers review, planners plan. Each agent does one kind of cognitive work.
2. **Don't mix dimensions.** One reviewer evaluates one dimension. Not correctness *and* security *and* quality in one pass. Each dimension gets its own agent with a clean context.
3. **Smaller scope, deeper work.** Size is not just a capacity constraint — it's a quality variable. Smaller units of work enable deeper engagement even when larger ones would technically fit in the context window. An agent with less to hold can attend more fully to what it holds. The goal isn't "don't overload" — it's "right-size for the depth the work deserves." If a task could be smaller and benefit from it, decompose further.

A reviewer doing 200 lines on one dimension catches things a reviewer doing 2000 lines on three dimensions will miss. An executor building one well-scoped function produces better code than one building an entire module. This applies across the entire system — code, research, analysis, planning, documentation.

Decomposition is not a countermeasure against LLM limitations — it is the mechanism that produces quality. Depth comes from focus, and focus comes from scope. An agent given one well-bounded concern can engage with it fully — explore edges, catch subtleties, do the work justice. This is true regardless of model capability. The right level of decomposition is the minimum granularity where each agent can go genuinely deep.

**Implication:** Task decomposition is not just a planning concern — it's a quality concern. Every level that delegates (L1 through L4) must size tasks to the depth they deserve, not to the convenience of fewer agents. The independent review function applies the same principle: right-sized work units, single dimensions, individual reviewers.

---

## 18. Leads Direct, Never Execute

Level leads (L1, L2, L3, L4) never do raw work. Their cognitive task is: think, decompose, delegate, evaluate returns, decide. A lead that starts executing fills its context with execution artifacts, degrading its judgment on everything else — the altitude that makes coordination effective is lost the moment you descend into the work itself.

This is an operational rule, not just a preference. A lead's context window is reserved for direction and judgment. Execution artifacts — code, raw research, intermediate analysis — belong in the context windows of the agents doing that work. The lead sees synthesized returns, not the raw material.

**Implication:** Each lead level must have sufficient delegation infrastructure (downward to subordinate levels, laterally to its own department) so it never needs to do raw work itself. If a lead finds itself doing execution work, that's a signal that its delegation infrastructure or team is insufficient — fix the infrastructure, don't normalize the lead executing.

**Resolved for V1 — the producer never reviews itself, even at the bottom.** The rule does not literally extend to L5 (L5 *is* the execution seat; it writes code), but its spirit does: L5 is split into an execute-review pair. **L5** (Codex harness + GPT-5.6 Sol) writes the code, runs the pre-authored acceptance tests + unit tests + CI; a separate **L5+** reviewer (a different agent, on a different runtime for judgment diversity) tests and reviews against the frozen spec, then accepts (forward) or bounces (bounded loop). So the separation of "do" from "judge the doing" holds all the way down — the executor is never its own gate. See runtime-and-model-map.md and QUALITY-GATE.md.

---

## 19. Bias Toward Overshooting

The cost of undershooting is high: missed quality issues, lost ideas, incomplete work, defects that cascade through the system. The cost of overshooting is low: extra compute, extra agents, extra cycles. Always do too much.

An extra review dimension that finds nothing is a rounding error. A missing review dimension that would have caught a security hole is real damage. An extra lateral spawn that adds no new insight costs tokens. A missing one that would have surfaced a critical angle costs quality. A pre-submission checklist item that's always green costs a glance. A missing item that lets a defect through costs a gate rejection, a rework cycle, and a citation.

This applies across the system: task decomposition (decompose one level too deep rather than too shallow), review dimensions (check one too many rather than one too few), lateral depth (spawn one extra researcher rather than risk missing an angle), verification (run one extra check rather than ship with uncertainty).

**Implication:** When sizing effort — how many reviewers, how deep to decompose, how many angles to research — default to "one more than feels necessary." The system should feel slightly over-resourced, never slightly under-resourced. Efficiency is optimized after quality is guaranteed, not before.

---

## 20. Design from the Receiving End

Every agent-facing artifact — briefs, role docs, boot injections, report templates, quality gate criteria — has two sides: the sender who designs it and the agent who receives it. Design from the receiving end, not just the sending end.

The question is not "is this specification complete?" It's "would a capable person be able to do this task under these conditions, with only what they have?" That question requires empathy — the ability to sit in the agent's position and experience what they experience, without the context you hold as the designer.

This is the same discipline that makes delegation effective between people. A good manager asks: does this person have enough context? The right tools? A clear picture of success? A way to escalate? If a capable professional couldn't do good work with what you've given them, the problem is in the conditions, not in the professional.

Traditional software engineering skills — interfaces, contracts, specifications — remain important. But they are not sufficient. A specification can be technically complete and still fail because the *experience* of receiving it doesn't support the judgment the agent needs to exercise. Ordering, framing, and emphasis matter as much as content. Sometimes the fix is less information, better structured — not more instructions.

**Implication:** Before finalizing any agent-facing artifact, do the perspective test: become the receiving agent, with only their context. Can you do the work? What would make it impossible? When debugging agent behavior, ask first: "given only what the agent had, would I have done better?" See the `perspective-test` skill for the full exercise.

---

## 21. Decompose Where the System Wants to Be Cut

Right-Size (principle 17) governs *how small* a unit should be. This principle governs *where the cuts go* — and a wrong cut is not rescued by sizing the pieces well. Decomposition is a structural-design act, not a chopping act: the goal is to find the seams the problem already has and carve along them, so each unit can be reasoned about, changed, and verified in isolation.

The guiding rules:

- **Design the connections, not the boxes.** The hard part of decomposition is the interfaces between units, not the units. Carve where the interface is *thin* — few, stable, well-named things crossing the boundary. A boundary that needs a fat, chatty, constantly-renegotiated interface is in the wrong place. (B6.)
- **Carve by what changes together.** Group by co-change and coupling, not by superficial similarity. Things that change for the same reason belong in the same unit; things that change for different reasons belong apart. DDD is the **seam-finder** here — bounded contexts are a tool for locating these cuts. The target is to *isolate change*: a typical change should touch one unit, not ripple across many. (B7.)
- **Point dependencies toward stability.** Dependencies flow toward the stable core ("the sun"), never with a volatile thing at the center that everything else hangs off. What is most likely to change sits at the leaves; what is most depended-upon must be most stable. (B8.)
- **A boundary must pay for itself.** Each split has a cost (an interface to maintain, a contract to honor). A boundary is justified only when it buys more than it costs — real isolation of change or concern. The failure modes are symmetric: the **glob** (too few boundaries, everything tangled) and the **confetti** (too many boundaries, trivial fragments with more interface than substance). Cut until each piece earns its boundary, then stop. (B11.)

Crucially, **deep-modules vocabulary is kept but its role is corrected (L47):** "deep module" (rich behavior behind a thin interface) is a *quality rubric you hold a unit against* — does this boundary hide a lot behind a little? — **not** the decomposition backbone. The backbone is C4 + DDD + SDD + hexagonal ports. Don't use depth-ratio to *find* the cuts; use it to *check* the cuts you found by co-change and thin interfaces.

**Implication:** Decomposition is a first-class architectural skill that lives primarily at L2 (system carving) and L3 (module carving), with L4 slicing within a module. It has its own methodology document — see DECOMPOSITION-METHODOLOGY.md for the full method (carving heuristics, the backbone stack, the misfits-are-information taxonomy, router-vs-direct, walking-skeleton-first build order). This principle is the principle-altitude summary; the methodology doc is the working detail.

---

## 22. Fidelity Before Elegance

The system's **#1 target is faithfulness to intent** — that what gets built is what the user actually asked for — *before* any optimization of execution quality, elegance, or cleverness. A beautifully engineered system that solves the wrong problem is a total loss; a plain one that solves the right problem is a success. When the two compete, fidelity wins, every time. (J43.)

This orders the whole improvement program. The **verification loop that catches drift comes first**; optimizing the quality of execution comes second. Drift — a requirement silently dropped, subtly reinterpreted, or quietly expanded — is the dominant failure mode at this scale, because the plan is a chain of translations across many agents and local fidelity at each step does not compose into global fidelity to intent. So the first thing the system must do well is *prove it built the right thing*, and only then get better at building things beautifully.

This principle is why the **plan-alignment gate** exists as a hard checkpoint between the design and build cycles, why **acceptance tests and rubrics are authored from the spec, before the work, by someone other than the worker** (so the work is anchored to the tests, never the tests to the work), and why **requirement traceability runs along the single hierarchical-path spine** end to end. Those are the concrete fidelity instruments; this is the principle they serve.

**Implication:** When sequencing system-improvement effort, build and harden the anti-drift machinery before tuning execution quality. When a design choice trades a little elegance for a lot of demonstrable fidelity, take the trade. See PLAN-ALIGNMENT-GATE.md (the gate), QUALITY-GATE.md (the two axes: quality + fidelity, drift dominant), and DECOMPOSITION-METHODOLOGY.md.

---

## 23. Neutral Tradeoff Framing for Human Decisions

Where the system must put a decision to the human, it presents the choice **neutrally and helps them decide** — it does not steer, pressure, or manufacture consent. The standard form is: lay out the options as balanced ("A biases toward X, B biases toward Y; here is the tradeoff"), then offer an **honest recommendation grounded in the user's own stated values from intake** ("you told us cost matters most, so we'd lean A — the cost of that is slower delivery"). The loaded, pressuring form — "are you sure you wouldn't rather compromise on that?" — is a dark pattern and is banned. (M57.)

Two failure modes are both wrong, and the principle threads between them:

- **Steering** — framing options to push a preferred answer, burying the real tradeoff, or pressuring. Banned outright.
- **Abdication** — dumping raw options with no read, leaving a non-expert user to adjudicate a technical fork alone. Also wrong: the user usually *wants* the expert's honest read. Help them decide; don't hide behind false neutrality.

The recommendation is legitimate precisely because it is anchored to *their* values, not the system's preference — it contextualizes, recommends from what they told us they care about, and applies no pressure. This is principle 1 (Owner, Not Operator) and principle 9 (clarity) applied to the human-facing surface: protect the user's attention *and* their agency.

**Implication:** Every human-decision surface — the plan-alignment gate's findings ledger, intake forks, escalations — must use the balanced-options-plus-values-grounded-recommendation form, never the loaded form. How much and how technically a choice is rendered is **calibrated per-user, per-area from the intake** (opinion × fluency; see M58 in the intake design). See PLAN-ALIGNMENT-GATE.md (human sign-off) and COMMUNICATION.md.

---

## Resolved Since First Draft

The inter-level contract and documentation questions that this section once listed as open are now answered by the design corpus and no longer belong here as open principle-level questions:

- **Inter-level contracts (L1→L2 … L4→L5)** are settled around the **one hierarchical-path spine** and the **pointer-not-payload brief**: each delegating level emits a distilled brief (spec + constraints + interface + ADRs, with raw upstream intent referenced, not carried), and each level returns a compressed report. The L2 output is ADR-style (component map + interface contracts + ADRs + per-module constraint specs); the Plan-phase output contract requires spec + frozen acceptance tests + gate rubric before the level below executes. Escalation flows up the spine. See WORKSPACE-SCHEMA.md, COMMUNICATION.md, PLAN-ALIGNMENT-GATE.md, and the operational L1–L5 docs.
- **Documentation schemas per level** are owned by WORKSPACE-SCHEMA.md (the node layout: `brief.md`, `acceptance.md` frozen and read-only to the executor, `report.md`, ADRs, design/plan docs) rather than by this principles doc.
- **Multiple L1s / Improvement Workspace:** the Improvement Workspace is the system-improvement function with a god-view (principles 5 and 11); see IMPROVEMENT-WORKSPACE.md. An optimizer-L1 capability (a future agent that may operate out of that workspace) is a separate, future concept not yet defined for V1.

## Open Questions

- **Time awareness:** Can L2 schedule and pace work across days? Stretch tasks to fit timeframes, divide compute between sessions?
- **Metacognition per level (separate workstream):** Each level needs its own mental models, skillsets, rubrics, and behavior design — what "good judgment" looks like at that level. Upper levels need strategy/organization/communication skills. Lower levels need concrete LLM skills and verification loops. These need to be formalized, not left tacit. Designed to be highly iterative — the infrastructure must make reconfiguration easy (see principle 16). See agent-definition-principles.md.
- **Benchmarking:** How to measure whether the system actually works better than flat dispatch. Without measurement, improvements are guesswork. (Ties to the optimizer-L1 development methodology.)

---

*Created: 2026-03-10*
*Source: Design conversation — voice notes on hierarchical agent architecture + neoack's tortuise post research + prior LLM design-principles work as grounding*
*Last consolidated: 2026-06-02 — folded in the design-conversation decision-set (added principles 21 Decomposition, 22 Fidelity-Before-Elegance, 23 Neutral Tradeoff Framing; V1 = software-building; need-to-know visibility graph; god-view-bounded observability; kind-of-thinking as the governing level test; held-softly-at-scale caveat; resolved the inter-level-contract / documentation open questions). 23 principles.*
*Status: Living document — principles will be refined as the architecture develops*
