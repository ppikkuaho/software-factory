# Behavioural Run Log

Canonical template for observing full-system runs.

This document defines the human-facing run log used once the harness can execute real
projects. It is not a runtime health report, a score packet, or a test result. It is the
primary artifact for the behavioural iteration loop: observe a run, understand why agents
acted as they did, identify behaviour that should change, then tune the system.

The project is opinionated about behaviour. Runtime mechanisms matter insofar as they let the
desired behaviour happen and make it observable.

## 1. Purpose

A behavioural run log answers:

- what happened, in order;
- what each agent seemed to believe its job was;
- what context, prompt surface, artifacts, validator feedback, or prior examples shaped that belief;
- where the agent behaved well;
- where the agent drifted, overreached, under-reached, stalled, or worked at the wrong altitude;
- what the likely cause was;
- what kind of intervention would steer future runs better.

It should be possible to read the log and understand the run without opening transcripts first.
Transcripts, ledgers, review artifacts, and reasoning summaries remain the evidence layer, not
the main reading surface.

## 2. Observation Posture

Prefer passive observation. The observer reads artifacts the system already produces: ledgers,
inboxes, signals, reports, review artifacts, transcripts, visible reasoning summaries, tool calls,
validator feedback, and produced work. If an observation mechanism changes agent instructions,
available tools, timing, workspace shape, or incentives, record that as instrumentation and do not
compare it blindly with uninstrumented runs.

When behaviour looks wrong, first ask why it was locally reasonable. Inspect what the agent saw and
what it appeared to optimize for before proposing a fix. The default assumption is that the agent
followed some visible instruction, context cue, validator shape, or workspace affordance.

Hard gates are for structural invariants: ownership, identity, artifact presence, stale tokens,
review-owned forwarding, and other cases where the runtime must not allow an unsafe transition.
Behavioural drift should first be treated as a design issue: role clarity, context packaging,
prompt posture, examples, output contracts, or process affordances.

## 3. Source Layers

Use the deterministic layers as the timeline skeleton and the human-readable layers as the
behavioural evidence:

1. **Ledger and binding state** — ordering, ownership, gates, wakes, terminal states.
2. **Inbox and signal files** — what the agent was told and what it claimed at boundaries.
3. **Produced artifacts** — briefs, plans, designs, reports, review artifacts, code, tests.
4. **Transcripts and visible reasoning summaries** — what the agent attended to, inferred, and
   thought it was doing.
5. **Tool calls and validator feedback** — what happened in the environment and how the agent
   reacted.
6. **Human/operator interventions** — messages, nudges, answers, stops, approvals.

Do not let the ledger become the report. A ledger can prove that a wake arrived; it cannot explain
whether the parent made a good judgment after waking.

## 4. Run Log Shape

Each behavioural run log should use this structure.

### 4.1 Overview

Short, plain-language summary:

- run id, branch, commit, runtime root, and scenario;
- terminal state;
- scoreability / contamination status, including operator interventions if any;
- what was produced;
- the most important behaviour observed;
- the most important open behavioural questions.

### 4.2 System Map

The actual hierarchy that formed:

- L1 root and project nodes;
- L2 project owner;
- L3 areas;
- L4 workstreams;
- L5 execution tasks;
- review seats and gate ids;
- final promotion/delivery path.

This is the run's organizational chart, not just a list of bindings.

### 4.3 Chronological Behavioural Timeline

Write the timeline in plain language, grouped by phase. Include timestamps or ledger seqs when
useful, but the main unit is behaviour:

- what the agent did;
- why it appeared to do it;
- what artifact or decision resulted;
- what happened next.

Example entry shape:

```text
L3 app area received the app brief and spent its first turn reconstructing upstream parse/report
context before writing plan.md. This violated the literal plan-first wording, but the transcript
shows the agent was trying to make the plan meaningful: the brief pointed at upstream contracts and
accepted work that it needed to understand before planning the composition area. Behavioural read:
bounded context-gathering before planning may be legitimate; the issue is whether the kickoff package
should provide a smaller plan-scoped context bundle.
```

### 4.4 Agent Behaviour Portraits

For every important agent, write a short portrait:

- **Assigned role:** what the system meant the agent to do.
- **Perceived role:** what the agent appeared to believe its job was.
- **Context used:** documents, examples, inbox messages, artifacts, prior gate outputs.
- **Main actions:** the work it actually did.
- **Key judgments:** choices, assumptions, escalations, non-escalations, tradeoffs.
- **Behavioural assessment:** aligned, acceptable, suspicious, drift, or blocker.
- **Likely cause:** role doc, spawn prompt, stale example, missing context, conflicting instruction,
  validator feedback, workspace shape, tool friction, or model judgment.
- **Steering change:** prompt/doc/context/process/tooling change that would make the desired
  behaviour the natural completion next time.

This section is intentionally fuzzy and judgment-bearing. It is where the system's behaviour becomes
legible.

### 4.5 Boundary And Handoff Review

Review each joint in the cascade:

- user intake to L1 intent/spec ownership;
- L1 to L2 project brief;
- L2 to L3 area briefs;
- L3 to L4 workstream briefs;
- L4 to L5 task briefs;
- L5 to L5+ review;
- L4+/L3+/L2+ review and parent consumption;
- final L1 fidelity and promotion.

For each boundary, ask:

- Did the receiving agent get the right context at the right altitude?
- Did it understand the job?
- Did it preserve the parent intent?
- Did it escalate real ambiguity or silently fill gaps?
- Did it delegate at the intended layer?
- Did it produce a parent-consumable artifact?

### 4.6 Review-Gate Behaviour

Review gates should be observed at their own altitude.

Upper gates should normally rely on lower gates being competent. They may sanity-check lower gate
evidence for existence, credibility, and fit, but their core job is the work their altitude can see:

- L5+ reviews local implementation quality and spec fidelity for a task.
- L4+ reviews how L5 outputs compose into the assigned workstream.
- L3+ reviews how workstreams compose into an area and expose the right contract.
- L2+ reviews product coherence, cross-area fit, intent fidelity, and readiness for L1.
- L1 reviews client intent and delivery fitness.

If an upper gate spends most of its effort repeating lower-gate tests, log that as a behavioural
issue unless the lower evidence looked suspicious or the replay found new material defects. Repeated
checking with the same shape of evidence is friction, not quality.

### 4.7 Behavioural Findings

Findings are not just defects. A finding can be positive behaviour worth preserving, a drift pattern,
or an open design question.

Use this shape:

- **Observation:** what happened.
- **Why it matters:** what behaviour or project principle it touches.
- **Evidence:** transcript/artifact/ledger pointers.
- **Likely cause:** what made the behaviour locally reasonable.
- **Preferred intervention:** behavioural tuning, context packaging, examples, process affordance,
  runtime support, or hard enforcement.
- **Owner:** design, docs, runtime, prompts, template, observability, or user decision.

Separate runtime failures from behavioural findings. Runtime failures explain whether the run is
scoreable; behavioural findings explain how the system acted.

Separate review routing, bounce audit, and failures. `gate_escalated` is routing evidence.
`gate_bounced` is not automatically a product failure, but every bounce is a mandatory audit
signal: something probably went wrong, so inspect the frozen candidate, the cited contract clause,
and the review finding even if the gate later passed. The run report must include every bounce in a
LOOK-HERE section and analyze whether it exposed a producer defect, review drift, contract
ambiguity, or a healthy correction. Never silently normalize a bounce as routine routing.

### 4.8 Intervention Backlog

Group follow-ups by intervention type:

- **Role/prompt tuning** — desired posture was unclear or stale.
- **Context packaging** — the agent needed context but had to hunt for it.
- **Examples/templates** — the agent followed an old pattern or lacked a good example.
- **Process affordance** — the desired behaviour was possible but awkward.
- **Runtime structural invariant** — the system must prevent an unsafe transition.
- **Observability improvement** — the behaviour could not be reconstructed well enough.

Prefer behavioural tuning before hard gates when the issue is judgment, role interpretation, or
altitude. Use hard gates when the transition itself must be impossible.

## 5. Output Contract

A completed behavioural run log must include:

- overview;
- run scoreability and any operator interventions that materially changed continuation;
- system map;
- chronological behavioural timeline;
- agent behaviour portraits for the load-bearing agents;
- boundary/handoff review;
- review-gate behaviour section;
- complete gate-bounce LOOK-HERE audit (including bounces followed by PASS);
- findings grouped by positive behaviours, behavioural issues, runtime issues, and open questions;
- intervention backlog;
- evidence appendix with stable paths to ledgers, transcripts, gate artifacts, reports, and run views.

It should not be limited to what can be deterministically asserted. The point is to make judgment
explicit and evidence-backed, not to pretend every useful observation is machine-verifiable.

## 6. Applying It To The Next Iteration

For each completed run:

1. Build passive evidence artifacts: evidence index, behavioural views, score packet, dashboard.
2. Write or update the behavioural run log using this template.
3. Review the log with the user at the behavioural level first.
4. Convert findings into an intervention backlog.
5. Implement the smallest set of changes that makes the desired behaviour more natural.
6. Run again and compare behaviour, not just terminal success.

The real loop begins here: observe, understand, tune, rerun.
