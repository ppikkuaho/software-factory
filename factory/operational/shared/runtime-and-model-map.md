# Runtime & Model Map — Operational Reference

Which model, runtime, and reasoning effort each level runs on, and how a parent on one runtime briefs a child on another. Loaded at boot for all levels; consulted by the spawn machinery at every spawn.

The short version: **model + runtime + reasoning effort is a per-level, config-time, swappable dimension** — it is not something an agent picks at runtime, and it is not baked into a level's identity. The spawn contract abstracts the runtime; the adapter turns those configured values into the runtime's native launch surface. This is what makes the assignment table below a *configuration*, not an architecture.

*Decisions: E31 (config-time swappable dimension), E32 (cross-runtime brief). Siblings: `agent-definition-principles.md`, `agent-lifecycle.md`, `comms-protocol.md`, `git-protocol.md`. Upstream: `PLAN-ALIGNMENT-GATE.md`, `QUALITY-GATE.md`, `COMMUNICATION.md`.*

---

## Terminology — Codex is a harness, GPT-5.6 Sol is a model

These are two separate dimensions and the docs keep them separate:

- **Runtime** = the *harness* an agent runs inside — its tool surface, its spawn/invocation mechanics, its output format. Today the registry deliberately uses both **Claude Code** and the proven native **Codex** adapter.
- **Model** = the LLM doing the thinking inside that harness — today **Opus 5.0** or **GPT-5.6 Sol**.
- **Reasoning effort** = the model's configured reasoning posture for that seat — `xhigh` or `high` in the current calibration, never an unmanaged runtime default.

"Codex" names the harness, never a model — OpenAI no longer ships models called "codex." When a level is described as "Codex + GPT-5.6 Sol," that is *harness + model*, two choices, not one. Keep the words straight: a sentence about tool manifests or spawn mechanics is about the runtime; a sentence about reasoning style or where work is generative-vs-pedantic is about the model.

---

## Model + Runtime + Effort Is a Config-Time Dimension (E31/T-23)

Three properties define how model/runtime/effort is wired in:

1. **Per-level.** Each level has an assigned model, runtime, and reasoning effort (table below). Specialized semantic-cell and product-probe seats have explicit calibrated rows of their own.
2. **Config-time, not run-time.** The assignment lives in the level config and is read by the spawn machinery when a parent spawns a child. **No agent selects its own or its child's model/runtime/effort mid-task.** A parent does not reason about "should this child be Opus or GPT-5.6 Sol" or choose an effort tier; it spawns the child for its level, and the level's config supplies all three values. This keeps calibration out of the per-task decision surface — one less thing for an agent to get wrong, and a knob the system tunes globally rather than ad hoc.
3. **Swappable.** Because the assignment is config and the brief is runtime-neutral (next section), changing a level's runtime is a config edit plus an adapter swap — not a rewrite of the level's role, soul, or brief. The whole reason the brief is split into a neutral contract + a thin adapter is to make this swap cheap. If GPT-5.6 Sol turns out to suit L4 better than Opus, that's a one-line config change and the L4 adapter; nothing about how L4 is briefed changes.

The spawn contract is the abstraction boundary: a parent emits a **runtime-neutral task contract**, and the adapter for the child's runtime turns it into a concrete spawn. The parent never writes harness-specific spawn code by hand.

---

## The Assignment Table (E32)

| Level | Model | Runtime | Effort | Why |
|-------|-------|---------|--------|-----|
| **L1** (client interface / intent guardian) | Opus 5.0 | Claude Code | **xhigh** | Generative, conversational, judgment-heavy: intake, tradeoff-framing, intent guarding, gate triage. |
| **L2** (architect) | Opus 5.0 | Claude Code | **xhigh** | The most generative seat in the system — greenfield architecture, ADRs, carving (`DECOMPOSITION-METHODOLOGY.md`). |
| **L3** (area / module designer) | Opus 5.0 | Claude Code | **xhigh** | Domain-deep design, interface renegotiation, pattern application — still generative. |
| **L4** (workstream planner + acceptance owner) | **Opus 5.0** | Claude Code | **high** | Planning/decomposition is the primary seat; Opus fits. L4 receives frozen criteria from the validated plan, then normally spawns a dedicated L5 `test_author` child to turn those criteria into executable acceptance before implementation L5 opens. That package is reviewed by L5+ and approved by L3 when the package changes L3-owned criteria, refreshes after a spec change, or records a no-executable-tests exception. A future first-class tester seat may later move to GPT-5.6 Sol based on evals. |
| **L5** (executor) | **GPT-5.6 Sol** | **Codex** | **high** | Execution: literal, spec-anchored coding against frozen acceptance tests. The proven native Codex path replaced the removed Sol/CLIProxy transport on 2026-07-16 while retaining the standing-seat lifecycle. |
| **L2+ / L3+ / L4+** (composition reviewers) | **Opus 5.0** | Claude Code | **high** | Independent review at the owning level's composition altitude. |
| **L5+** (reviewer, lateral to L5) | **Opus 5.0** (different model from L5) | Claude Code | **high** | Independent second reading against spec; placed on a different model and runtime from L5 on purpose — see judgment diversity below. |

**L1–L4 and the established review seats are Opus 5.0 on Claude Code.** These are the
generative/architecture/planning and independent-judgment seats. The harness config key
`opus-5.0` maps explicitly to the released model ID `claude-opus-5`. **L5 remains GPT-5.6 Sol.**
The current V1 acceptance-authoring mechanism is an L4-spawned L5 `test_author` child plus L5+
package review, with L3 approval where required. The implementation path consumes the accepted
executable package produced by that path. A future first-class tester seat may later be reassigned
to GPT-5.6 Sol based on evals.

Reasoning effort is explicit on every production config row. L1/L2/L3 execution seats use
`xhigh`; L4, L5, every review/review-check seat, semantic-cell seat, and product-probe seat use
`high`. The `LevelConfig` safe floor is also `high`, so a sparse config cannot silently fall to a
runtime's low default. Claude Code receives the value through its native `--effort` flag; Codex
receives it as `model_reasoning_effort` in the generated per-seat `config.toml`.

Claude Code remains pinned at **2.1.152**. On 2026-07-24 a real OAuth-only turn on that pin,
requested with `--model claude-opus-5`, returned concordant `claude-opus-5` attribution in the init
row, assistant `message.model`, and `modelUsage` key. Claude Code 2.1.219 is the first release whose
own model-aware changelog names Opus 5 and whose result schema adds richer
`canonicalModel`/`provider` fields; those fields are not required by the harness's current
message-model evidence path. `PINNED-CC.md` carries the full pin rationale.

### Calibrated semantic-cell and product-probe assignments

| Seat | Model | Runtime | Effort |
|---|---|---|---|
| reconstruction-verification | GPT-5.6 Sol | Codex | high |
| reconstruction-construction | GPT-5.6 Sol | Codex | high |
| adversarial comparator | Opus 5.0 (`claude-opus-5`) | Claude Code | high |
| coherence | Opus 5.0 (`claude-opus-5`) | Claude Code | high |
| atomization | Opus 5.0 (`claude-opus-5`) | Claude Code | high |
| user-simulation probe | GPT-5.6 Sol | Codex | high |
| performance/robustness probe | GPT-5.6 Sol | Codex | high |

These rows are exact owner+director-calibrated registry entries, not per-run choices. Literal and
driving seats use Sol; judgment seats use Opus. The joint calibration session's Sol/Claude-Code
serving column was narrowly reconciled by director ruling to the native Codex adapter because the
Sol proxy transport was removed on 2026-07-16. Restoring it would be a different design pass;
recording Sol on an unflagged Claude pane would be false.

**Seat notation (per user ruling 2026-07-12, `RESEARCH-SYSTEM-MACRO-ARCHITECTURE-2026-07-12.md` §2):** Ln = execution seat at level n; Ln+ = review/verification seat (e.g. L5+); Ln& = planning seat — planning seats exist at L1–L3; planning-L3 = L3&, execution-L3 = L3 (C21 split); L/+ = the plan-alignment gate (transition/cross-level review; LE+ where the slash would collide with address syntax); auxiliaries are named sub-seats of their owning gate. Key also in `design/ARCHITECTURE.md` §1.

This table is a config snapshot, not a law. It is expected to move as the system-improvement function (the Improvement Workspace; in the future, an optimizer-L1 capability (name retired 2026-07-12 → the principal coordinator, hypothesis-tree project) — a separate concept from that workspace — will perform this analysis systematically) learns where each model actually earns its seat.

---

## The Model-Perspective Rule (E31/E32)

The assignment isn't arbitrary preference — it follows one rule about where each model is strong:

- **Opus → generative / architecture seats.** Greenfield design, decomposition, ADRs, intent elicitation, semantic judgment, reconstructing "what would a system built like this actually do." Work that requires inventing structure from a fuzzy goal.
- **GPT-5.6 Sol → pedantic / adversarial / checking / execution seats.** Literal, engineering-brain, excellent at "name every obligation in this prose that no ID carries," "make these frozen tests pass," "find the case this assertion misses." **Weak at greenfield/architecture** — it will execute a spec faithfully but will *not* fill a gap in the spec with good architecture the way Opus tends to. That weakness is exactly why it's a good fit for seats where filling gaps silently is the failure mode you're trying to prevent.

This rule generalizes beyond the level table. Wherever the system needs a seat — including the **plan-alignment gate** (`PLAN-ALIGNMENT-GATE.md`) — apply it:

- The gate's literal **blind reconstruction** windows run GPT-5.6 Sol so absent behavior becomes an
  explicit `UNDETERMINED-GAP`, never a charitable inference.
- The **atomization auditor**, **adversarial comparator**, and **coherence** seats run Opus 5.0 for
  bounded semantic judgment under their calibrated output contracts.
- The product-altitude **user-simulation** and **performance/robustness** drivers run GPT-5.6 Sol.

The rule is the reusable thing; the level table and the gate seating are two applications of it.

---

## The Cross-Runtime Brief (E32)

The architecture uses both Claude Code and the native Codex adapter behind one standing-seat
lifecycle. The hexagonal boundary—runtime-neutral task contract plus thin adapter port—is therefore
live, not dormant. An Opus L4 briefing a Sol L5, or the daemon opening a Sol reconstruction/probe
seat, crosses both model and runtime without changing the semantic contract.

### Neutral contract (the core) + thin adapter (the port)

The **semantic brief is identical across runtimes**. It is the agent's actual task and is runtime-agnostic:

- identity (address — workspace node path + role-variant, per F35; see `WORKSPACE-SCHEMA.md`)
- spec (the distilled, pointer-not-payload brief — spec + constraints + interface contracts + the ADRs that carry rationale; raw upstream intent is *referenced*, pullable on demand, not carried)
- the **frozen acceptance/rubric artifact** — read-only to the executor (D26; see `QUALITY-GATE.md`)
- interface contracts
- constraints
- workspace location
- reporting expectations

Only three things are runtime-specific, and the **adapter injects them at spawn**:

- **tool manifest** — which tools this runtime exposes (the Codex tool surface vs. the Claude Code tool surface)
- **harness invocation** — how this runtime is actually spawned/driven
- **output format** — how this runtime returns its result

Swapping a level's runtime = swapping its adapter. The neutral contract — the part a human or a reviewer cares about — does not change. That is E31's swappability, delivered concretely.

### Spawn-failure contract: no silent fallback, deterministic escalation (E32)

The adapter's job is to pin the **configured** model + runtime for the child's level. The failure mode this contract exists to forbid is **silent degradation** — the adapter quietly running the child on something other than what the config specifies and nobody noticing. This is not hypothetical: in the real dry-run a `gpt-5.x` model override was **silently rejected by a ChatGPT-account Codex and fell back** to whatever that account served, and the divergence surfaced only later. That is exactly the failure this contract makes impossible.

**The pinning obligation.** Before the child runs, the adapter must confirm it has pinned the configured model + runtime. The three ways this can fail:

1. **Model unavailable** — the configured model isn't served by the reachable endpoint/account.
2. **Override rejected** — the adapter requested a specific model (e.g. a `gpt-5.x` pin) and the runtime/account refused it or substituted another.
3. **Runtime down** — the harness itself (Codex, Claude Code) is unreachable or fails to spawn.

**Observable behavior on any of the three — deterministic escalation, never fallback:**

- The adapter does **not** spawn the child on a substitute model/runtime. It does **not** "best-effort" with whatever is available.
- It emits a **spawn-failure escalation to L1** carrying: the child's address, the *configured* model+runtime, the *actual* model+runtime the endpoint would have served (or "none — runtime down"), and which of the three failure classes fired. This rides the same escalate-options channel as any block (`agent-lifecycle.md`), but it terminates at **L1** because model/runtime is a config-time, system-level concern, not a per-task one — no intermediate level is authorized to pick a substitute.
- **L1 alerts the user.** The user sees: "could not run `<address>` on its configured `<model>/<runtime>` (reason: `<class>`); the endpoint would have served `<actual>` instead. No work was run on a substitute." L1 does not silently downgrade; the user decides (retry, re-config the level, or accept a different model explicitly).
- A checker can verify the contract held by asserting: for every spawned child, a **`model-used` record** exists in the work node and equals the configured model; any mismatch must have a corresponding L1 spawn-failure escalation and user alert in the trace. A child running on an unrecorded or mismatched model with no escalation is a contract violation.

**The actual model used is always recorded and surfaced.** Every spawn writes the actual model+runtime it ran on into the child's work node (a `model-used` field in the node's metadata/`status.md`), regardless of success or failure. The model in use is **never silently assumed** from the config — config is the *intent*, the recorded `model-used` is the *fact*, and the audit layer (`OBSERVABILITY.md`) can replay which model actually produced any artifact. When they match, that's the normal case; when they can't be made to match, the run does not proceed silently.

### Result-flow is runtime-neutral for free

Results do not need a runtime-specific return channel, because the system carries two
runtime-neutral surfaces (F33; see `COMMUNICATION.md`):

- **Docs are the durable truth.** Both runtimes write files into the work node (`report.md`, the code, test results). Truth lives in the docs, not in any message.
- **Messages and wakes are runtime-neutral.** Both runtimes write the same sender-owned direct-edge
  message/question records. The daemon derives inbox pointers, applies the same event wake table,
  verifies delivery, and retries from durable state.

Turn observation has one explicit runtime asymmetry: Claude hooks expose turn start, tool-in-flight,
tool completion, and turn end; Codex's sanctioned notify surface exposes turn end only. Missing
Codex start/tool edges use detector evidence fallback. Message, question, barrier, receipt, and gate
semantics remain identical regardless of runtime.

---

## GPT-5.6 Sol Brief Discipline

Briefing a GPT-5.6 Sol child is not the same as briefing an Opus child. GPT-5.6 Sol will faithfully execute what it's given and will **not** paper over an underspecified brief with good architecture. The brief discipline turns that property from a liability into the safety it's meant to be:

- **Maximally decision-complete.** Every decision the executor needs must be *in the brief*. A gap is not an invitation for GPT-5.6 Sol to invent a reasonable answer — it is a hole it will either escalate or stumble on. Brief it as if every unstated assumption is a defect. (This sharpens the general "thin-but-decision-complete" brief rule from `agent-definition-principles.md` for the GPT-5.6 Sol case specifically.)
- **Acceptance tests as the primary anchor.** The frozen acceptance artifact (D26) is the load-bearing definition of "done." For a GPT-5.6 Sol implementation executor, point it at the accepted executable tests first; the prose spec is context, the tests are the contract.
- **Escalate ambiguity, don't decide it.** The brief must explicitly instruct: when something is ambiguous or missing, **raise it upward, do not fill it**. This makes the L5→L4 escalation channel load-bearing — it's the relief valve that keeps GPT-5.6 Sol's literalness from turning into silent wrong guesses. (`COMMUNICATION.md` carries the escalation payload format.)

### Judgment diversity: L5+ on a different model

The **L5+ reviewer runs on Opus through Claude Code, a different model and runtime from the
Sol/native-Codex L5 it reviews.** This is deliberate: an independent reading against spec is worth
more when it does not share the producer's blind spots. The same calibrated model pattern separates
literal/driving semantic-cell and product-probe work from bounded judgment work above.

---

## L4/L5 Codex-Audit Checklist (neutralize Claude-isms)

L5 (and any other Codex-runtime level) runs on a harness that does **not** carry the Claude base prompt and was not written with Claude's instruction-following idioms in mind. Documents and briefs authored by Opus levels can leak "Claude-isms" — phrasings, conventions, and implicit harness assumptions that read fine to a Claude model and badly to GPT-5.6 Sol. The **codex audit** is the action of scrubbing those out of anything an L5 will consume.

> Status: the L4+L5 codex audit is **owed** — it is an action not yet performed against the current L5 docs (`operational/L5/role.md`, `config.md`, `soul.md`, `swe-handbook.md`, `spawn-template.md`). It is logged here so it isn't lost; perform it before the first real Codex L5 spawn.

When auditing an L5-facing doc or brief, neutralize:

1. **Claude-harness tool assumptions.** References to tools/affordances that exist on Claude Code but not on the Codex tool manifest. The brief's tool references must match the *runtime-specific tool manifest* the adapter injects, not the Claude default surface.
2. **Implicit base-prompt behavior.** Anything that relies on Claude's base prompt or default conventions to "just work" (tone defaults, refusal patterns, formatting habits, implicit safety scaffolding). Codex has no Claude base prompt — make the expectation explicit in the doc or it won't hold.
3. **Claude instruction-following idioms.** Phrasings tuned to how Opus reads directives (soft hedges, "you might consider," altitude cues). For GPT-5.6 Sol, convert to explicit, literal, decision-complete instructions — say exactly what to do and what to escalate.
4. **Filled gaps that assume good-architecture backfill.** Any place the doc leaves a decision implicit on the assumption the model will fill it well. GPT-5.6 Sol won't; make it explicit or mark it escalate-don't-decide.
5. **Cross-runtime brief conformance.** Confirm the doc cleanly separates neutral-contract content from runtime-specific content, so the adapter (not the prose) owns the runtime-specific parts.

The audit's output is a cleaned doc plus, where relevant, a note for `agent-definition-principles.md` (so the Claude-ism doesn't get re-authored next time) and `runtime/patches/claude-code/` if the fix belongs at the base-prompt patch layer (H40).

---

## Open Items

- **Future tester-seat model/runtime** — V1 uses an L5 `test_author` child plus L5+ review for acceptance-authoring and refreshes; if a first-class tester seat is introduced later, reassess its runtime after evals. Not blocking. (2026-07-13: the founding inherits the unified standing-seat pattern — see ruling note.)
- **L4+L5 codex audit** — owed; perform before the first real Codex L5 spawn (checklist above). — MOOTED 2026-07-13 (no Codex L5 spawns under the unification ruling; revives only with a Codex-harness consumer).
- **Claude base-prompt patch (H40)** — captured as intention; ties to `runtime/patches/claude-code/`. Codex levels have no Claude base prompt, so the patch is Claude-runtime-only by construction.

---

*Operational reference — loaded at boot for all levels; consulted by the spawn machinery at every spawn.*
*Created: 2026-06-02*
