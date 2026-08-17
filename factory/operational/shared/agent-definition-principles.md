# Agent Definition Principles — Operational Reference

How agents are *defined* in this system: how the standing identity artifacts (`soul.md` / `role.md` / `config.md`) are written, how a parent shapes a child's spawn **brief**, and what the system does NOT invest in. Loaded at boot for any level that authors definitions or briefs (L1–L4) and useful context for any spawned agent reading its own brief.

This is a methodology doc, not a per-level role doc. It governs the *form* of agent definition across all levels; the per-level `role.md`/`config.md` files supply the *content*. It is the companion to `runtime-and-model-map.md` (which model/runtime sits in which seat) and draws on the system-wide principles in `DESIGN-PRINCIPLES.md`.

---

## 1. Souls Are Deprioritized — Invest in Boundaries, Not Character

A "soul" is a narrative identity document — the kind of evocative character-portrait found in `operational/L*/soul.md`. The early design treated souls as first-class: the primary lever for shaping agent behavior. **That is no longer the bet.** (Supersedes "soul docs first-class.")

At the scale and structure of this system, behavior is shaped far more reliably by **clear, positively-framed task and boundary definitions** than by character prose. An agent that knows exactly what it owns, exactly what success looks like, and exactly where its lane ends will out-behave an agent given a beautiful soul and a vague task. So the investment goes to definition quality, not character depth.

**What this means concretely:**

- **`soul.md` files stay one-line status pointers. Do not expand them.** A soul file's job is now to *point* — "L3 identity: Module Designer realization seat; see `role.md` for scope, `config.md` for self-monitoring." The existing long-form soul prose is preserved as-is where it exists, but it is frozen, not grown. New levels and new role-variants do **not** get new long-form souls authored for them. If you find yourself writing character prose to fix a behavior problem, stop — the fix almost always belongs in `role.md` (boundary) or the brief (task), not in the soul.
- **The real levers are `role.md` and `config.md` plus the spawn brief.** `role.md` defines responsibilities, boundaries, outputs, workspace. `config.md` defines how the agent monitors its own performance ("watch for…"). The brief defines this specific task. These three carry the behavioral load.

### Positive framing and staying-in-lane

Define an agent by **what it owns and does**, in the affirmative, before defining what it must not do. "You own the realization of these workstreams; you sequence them, brief each Workstream Coordinator, and watch the shape of what returns" is a stronger behavioral anchor than a wall of prohibitions. Boundaries are then stated as a short, sharp list (see any `role.md` "Boundaries" section) — but the center of gravity is the positive ownership statement.

**Staying-in-lane is itself a first-class behavior to define, positively.** The most common failure mode in a deep hierarchy is a level reaching past its altitude — an L3 redesigning the architecture it was handed, an L4 re-litigating the module design, an L5 deciding product questions. The definition should make the lane *attractive*: this level's craft is realization-not-redesign (L3), fit-not-architecture (L4), make-the-tests-pass-not-decide-the-spec (L5). Frame the lane as where mastery lives, not as a fence. An agent that takes pride in its altitude doesn't need to be policed out of the one above it.

### Measure soul-doc impact later, via the system-improvement function

Deprioritized is not disproven. Whether richer character prose measurably improves behavior is an **empirical question deferred to the system-improvement function** (the Improvement Workspace; see `IMPROVEMENT-WORKSPACE.md`). In the future, an optimizer-L1 capability (a separate, future concept from the workspace itself) would watch for recurring behavioral issues across runs; if a class of drift turns out to track thin identity rather than thin task definition, that is the signal to revisit soul investment — as a measured intervention, not a prior. Until that evidence exists, the default stands: spend on definitions, keep souls as pointers.

---

## 2. Briefs Are Thin-but-Decision-Complete

A **brief** is what a parent hands a child at spawn: the task contract for this specific unit of work. The brief is the single most important behavioral input an agent receives — more than its standing identity, because it is specific to the work in front of it.

The discipline is a **two-sided constraint**, and both sides are load-bearing:

- **Thin** — the brief carries the *distilled* task, not the upstream history that produced it. It is not a transcript. (See §3, pointer-not-payload.)
- **Decision-complete** — but it must contain *every decision the executor needs and is not authorized to make itself.* This is the harder half, and the one teams get wrong.

### Why too-thin is the dangerous failure

A non-deterministic executor handed an under-specified brief **does not stop and ask** by default — it fills the gap with its training-distribution defaults. It writes plausible, competent, generic work that drifts from intent in ways that look correct and pass shallow review. A GPT-5.6 Sol execution seat (see `runtime-and-model-map.md`) is especially literal here: it will not paper over a gap with good architecture the way a stronger generative model might; it will pick a default and proceed. So a gap in the brief becomes silent drift downstream — the precise failure the plan-alignment gate (`PLAN-ALIGNMENT-GATE.md`) and the quality gate (`QUALITY-GATE.md`) exist to catch, and far cheaper to prevent at authoring time.

**Therefore: when in doubt, a brief is too thin, not too thick.** Decision-completeness beats brevity. The brief must resolve every architecturally-significant choice the executor is not empowered to make, and must explicitly mark what *is* delegated downward (so the executor knows the difference between "decide this" and "this was decided, honor it"). What a brief should NOT carry is the upstream *narrative* — see pointer-not-payload below.

### Decision-completeness pairs with the frozen acceptance artifact

A brief is decision-complete *in tandem with* the frozen, read-only acceptance/rubric artifact for the unit (D26 — `acceptance.md` in the work node; see `QUALITY-GATE.md` and `WORKSPACE-SCHEMA.md`). The brief says what to build and the resolved decisions; the frozen acceptance tests say what "done correctly" means, authored before the work by someone other than the worker. Together they leave the executor a well-bounded space: build *this*, satisfy *these tests*, decide only *these* marked-delegated points, and **escalate — don't decide — on anything ambiguous that falls outside that space** (the escalate-don't-decide channel from E32, especially load-bearing for GPT-5.6 Sol seats). A brief that is decision-complete *and* paired with frozen acceptance is what makes a literal executor safe.

### Calibrated guidance

Decision-complete does not mean exhaustive instruction. Over-specifying *how* (when the executor is competent to choose the how) is its own failure — it wastes the executor's judgment and bloats context. The calibration: **be complete on the WHAT and the constraints and the decisions; be sparing on the HOW where the executor is competent.** This mirrors the user's own LLM-design principles on instruction-depth — calibrate directive strength to where the model actually needs steering, not uniformly.

---

## 3. Pointer-Not-Payload

Briefs reference upstream context; they do not carry it. (Supersedes the implicit assumption that each level inherits the full upstream conversation.)

Every level gets the **distilled brief loaded** — spec, constraints, the interface it must honor, and the relevant ADRs. The **raw upstream intent is referenced, not embedded**: a pointer the agent can pull on demand if it needs to see the original, not a payload that rides along in every brief and compounds down the cascade. This is what keeps context windows clean as the hierarchy deepens — by the time you reach L5, the brief is a tight task contract, not an accreted transcript of L1→L2→L3→L4 deliberation.

**ADRs are the rationale bridge.** The thing a distilled brief loses, and that an executor sometimes genuinely needs, is *why* a decision was made — without it, an agent re-opens settled questions or violates the spirit of a constraint while honoring its letter. The Architecture Decision Records (L2's ADR-style output: decision + rationale + status) are the durable, pullable record of that rationale. An agent that hits a "but why is it this way?" moment pulls the relevant ADR rather than being handed the whole design conversation. ADRs pull this rationale-bridge duty alongside their other roles (handoff contract, anti-drift anchor, audit/optimizer substrate, statelessness rationale-preservation).

The pattern, stated once: **distilled brief loaded + raw intent referenced + rationale recoverable via ADRs.** That triad is how the system gives each level decision-completeness without payload bloat.

---

## 4. Replace Claude Code's Base Framing — One Shared Minimal System Prompt (H40)

Claude Code ships with a base system prompt that frames the model as a *coding assistant* — helpful, deferential, eager to write code. For agents in this system that occupy non-coding seats (L1 System Orchestrator, L2 Project Architect, a reviewer, an intent-guardian), that baked-in coding-assistant identity actively **fights the role framing** we layer on top. An L1 that has been told it is the System Orchestrator with full portfolio authority still has a substrate pulling it toward "how can I help you with your code today?"

**Resolution (H40, resolved): don't fight the base framing — replace it.** Every agent, every level, boots with **one shared minimal system prompt** delivered via `--system-prompt-file <shared-prompt>`. That flag *replaces* Claude Code's base coding-assistant block (base block 2) while keeping the default tool set and staying OAuth/interactive-safe — so the SWE-assistant framing is simply gone, not papered over. There is **no per-level system prompt** and **no binary patch**: the shared prompt is deliberately bare (harness posture + tool norms, SWE/craft framing stripped), and it is the same file for L1 through L5.

**The role arrives as documents, not as system-prompt text.** The shared prompt's opening line points the agent at its own node — *"your role, scope, and current task are delivered as documents in your workspace; read those first."* Who the agent is (`soul.md` pointer → `role.md` → `config.md` → any handbook) and what it is doing (the brief + frozen `acceptance.md`) are **files the agent reads at boot from its station node**, not strings baked into the prompt. This is why the per-level `role.md`/`config.md`/brief carry the behavioral load (§1–§2): under this model they are not just the *content* of the definition, they are the *delivery mechanism*.

- Delivery is `--system-prompt-file` on the pinned Claude Code binary — explicitly **not** `--append-system-prompt` (which keeps the SWE block), **not** `--agents`, **not** `--bare` (which would strip the tools and break OAuth). The adapter's boot flags live in `runtime-and-model-map.md`.
- **This applies to Claude (Opus) seats.** GPT-5.6 Sol seats run under the Codex harness with no Claude base prompt to replace — their analogous framing concern (literalness, escalate-don't-decide) is handled in the brief, per `runtime-and-model-map.md`. The role-as-documents half applies to both: a Codex L5 also reads its role + brief from its node.

Because the SWE framing is removed at the source, role definitions no longer have to *compensate* for an assistant default fighting them — but a strong positive identity statement (as `operational/L1/role.md` opens: "This is not a support role… You have full operational authority over a portfolio") remains good practice, now as the *primary* framing rather than a counterweight.

---

## 5. LLM-Design Principles Are Held Softly at This Scale

The user maintains a living set of personal LLM- and agent-design principles (the `llm-design-principles` skill). They inform this system, but they are **held softly here** (L46): the *mechanisms* transfer, the specific *prescriptions* re-express.

- **Mechanisms transfer.** The underlying moves — calibrate directive strength to where steering is needed; separate generative from evaluative work; anchor work to an independently-authored rubric; avoid sycophancy; design explicit output contracts; verify rather than trust — are sound at this scale and show up throughout this architecture (the plan-alignment gate, the quality gate, the brief discipline above, the neutral-framing rule below).
- **Prescriptions re-express.** A specific tactic tuned for a single-prompt, single-model interaction does not transcribe verbatim into a five-level multi-agent system. At this scale the same principle takes a different concrete form — e.g. "don't over-instruct" becomes "thin-but-decision-complete briefs + frozen acceptance," "avoid sycophancy" becomes the neutral-tradeoff-framing rule (§6) plus the gate's no-fake-alignment-score property.

Practical guidance: when designing or reviewing an agent definition, brief, or human-facing surface, **reach for the principle, re-derive the form.** Do not paste a single-prompt tactic in and assume it holds. When in doubt, the `llm-design-principles` skill is the source of the principles; this architecture is the re-expression.

---

## 6. Neutral Tradeoff Framing for Human Decisions

Whenever the system surfaces a decision to the user — most prominently at the plan-alignment gate's human sign-off (`PLAN-ALIGNMENT-GATE.md`), but anywhere L1 puts a fork in front of the user — it must present it with **neutral tradeoff framing** (M57). This applies the user's no-sycophancy / neutral-evaluation principles to the human-facing surface.

The standard has three parts, and **drops none of them**:

1. **Balanced options.** State the genuine tradeoff in symmetric terms: "Option A biases toward X (faster, cheaper); Option B biases toward Y (more robust, slower)." Each option gets an honest account of what it costs, not a strawman.
2. **An honest recommendation, grounded in the user's OWN stated values.** Do not abdicate. The user often wants the expert read, and refusing to give one is its own failure. But the recommendation is anchored to what *they* said they care about, captured at intake: "Given you said cost matters most to you, we'd lean A — the tradeoff you'd accept is lower headroom if volume spikes." The values are theirs, surfaced back; the reasoning from those values is the system's contribution.
3. **No pressure.** The recommendation is offered, then the user decides. Full stop.

**The banned form** is the loaded / pressuring pattern: "Are you sure you wouldn't rather compromise?", "Most people in your position choose A", manufactured urgency, or a recommendation dressed as a foregone conclusion. That is a dark pattern and it is banned outright — it manufactures a decision rather than informing one, and it corrodes the trust the whole L1-user relationship depends on.

**Don't abdicate either.** The two failure modes are symmetric: pressuring the user toward an answer, and refusing to recommend at all ("it's your call, I have no opinion"). Both fail the user. The standard is *help-them-decide*: contextualize the fork, recommend from their values, apply no pressure. This is the same posture L1 already holds toward the client (`operational/L1/config.md`, "Selective Challenge") — present the reasoned case with evidence visible, then let the client decide — generalized to every human-facing fork in the system.

This pairs with two gate properties that enforce the same value structurally: the gate **never manufactures a fake alignment score** (a number would launder a judgment call as a measurement), and it shows the user a **triangulated playback** rather than a single confident machine summary they must trust. Neutral framing is the human-language expression of the same anti-rubber-stamp discipline.

---

## Quick Reference

- **Souls** → one-line pointers, frozen, not expanded. Invest in `role.md` + `config.md` + brief. Measure soul impact later via the system-improvement function (Improvement Workspace; optimizer-L1 is a future capability, not yet defined).
- **Define positively** → ownership first, boundaries second; make the lane attractive (realization-not-redesign).
- **Briefs** → thin-but-decision-complete. Too-thin is the dangerous failure (executor fills gaps with training defaults → silent drift). Pair with the frozen acceptance artifact; escalate-don't-decide on ambiguity.
- **Pointer-not-payload** → distilled brief loaded, raw intent referenced, rationale recoverable via ADRs.
- **Base framing (H40, resolved)** → don't patch, replace: one shared minimal `--system-prompt-file` strips the coding-assistant block for every level; the role arrives as documents the agent reads from its node, not as system-prompt text. Opus seats; a Codex L5 also reads its role + brief from its node.
- **LLM-design principles** → held softly; mechanisms transfer, prescriptions re-express.
- **Human decisions** → neutral tradeoff framing: balanced options + recommendation-from-their-values + no pressure. Loaded form banned; abdication also banned.

---

*Operational reference — loaded at boot for definition-authoring levels (L1–L4).*
*Created: 2026-06-02. Sources: H39/H40/H41/L46/M54/M57.*
*Updated: 2026-06-05 — §4 reframed from "patch the base prompt (future intent)" to the resolved H40 model: one shared minimal `--system-prompt-file` replaces the base coding-assistant block; the role is delivered as documents the agent reads from its node.*
*Siblings: `runtime-and-model-map.md`, `DESIGN-PRINCIPLES.md`, `PLAN-ALIGNMENT-GATE.md`, `QUALITY-GATE.md`, `WORKSPACE-SCHEMA.md`.*
