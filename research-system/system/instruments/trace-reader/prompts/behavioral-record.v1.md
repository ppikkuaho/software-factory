You are producing a **Behavioral Record** digest of a single Claude Code agent
session, working ONLY from its extracted trace. This is the Behavioral Record
product (C6 §1): post-hoc evidence about a completed run, anchored and
observer-effect labeled. It enters the research system only through later
adjudication — so it must be scrupulously calibrated, never inflated.

## Your inputs — read ONLY these, in your working directory

- `trace.jsonl` — the active-path event stream (one JSON event per line).
- `branches.jsonl` — abandoned-branch events (same schema + `branch_id` +
  `fork_raw_ptr`). These are REJECTED timelines: rewinds/retries the agent (or
  user) walked back. Treat them as such — never as things the run "did".
- `actors.json` — every file in the bundle: main + subagents, with join info and
  an `orphan` flag.
- `meta.json` — provenance: `partial`, `watermark`, and `counts` (including
  `orphan_actors` / `orphan_events`).
- `orient.json` — IF PRESENT: computed orientation features per actor.

Do NOT read the raw session transcript, the source bundle, or any other file. The
trace is the whole world. If a fact is not in these files, you do not know it.

Each event carries `step` (per-actor ordinal), `actor`, `kind`, `ts`, and a
`raw_ptr`. Event kinds: `thinking`, `assistant_text`, `user_msg`, `tool_call`
(with `name`, `input_digest.hint`, `result_digest`, `tokens`, `duration_ms`), and
`lifecycle` (subtypes: subagent_spawn/return, compact_boundary, checkpoint,
mode_change, queue_operation, attachment, system_note, fork_marker).

## The one hard rule — anchoring

EVERY sentence that states something about the run's behavior MUST cite the
step(s) it rests on, inline, in square brackets: `[main 210]`, `[main 210-235]`,
`[agent-a069c431db7cf11bb 5-9]`. A ranged cite is fine. An unanchored behavioral
claim is a BUG — the reader must be able to descend to the exact events. Section
headings and the framing caveats below are the only prose exempt from anchoring.

**Cite by reading the `step` field — never by counting position.** The number in
a cite MUST be the exact `step` value of the event you are pointing at, read
straight off that event. Do NOT count events, and do NOT infer a step number from
where an event sits in the file. Per-actor `step` ordinals have GAPS where
abandoned branches were dropped (a jump from step 46 to 48 on the active path is
normal, not a missing event); never interpolate a number across a gap. A range's
two endpoints must both be real `step` values you read. Cite the event that
actually did the thing — e.g. the `tool_call` Grep itself, not the neighboring
`assistant_text` status line that announced it. Off-by-one/off-by-N drift here is
a silent correctness bug, so verify each number against its event before writing it.

## Calibration — mandatory, non-negotiable

- **`thinking` events are stated rationale, not mechanism.** They are the agent's
  reasoning *as transcribed* (already summarized by the model). Report them as
  claims the agent *stated* — "the agent framed X as…", "the stated reason was…"
  — NEVER as proof of what the agent "knew", "understood", "realized", or "meant".
  You are reading a self-report, not the machinery.
- **Actions are stronger evidence than reasoning**, but still describe, don't
  impute intent beyond what a tool_call plainly did.
- **Do not generalize** from a few events to a disposition ("the agent is
  careful"). Describe the behavior at its steps; let the reader judge.

## Framing block — include verbatim-in-spirit at the top of your digest

1. **Observer-effect label:** this is a post-hoc reconstruction assembled from a
   trace, not the run itself; the act of tracing/naming shapes what is visible.
2. **Partial label:** if `meta.partial` is true (it is, in v1), say so and cite
   the watermark (lines + last_timestamp per actor). Make NO completeness or
   coverage claim — you are reading a prefix of unknown remainder.
3. **Orphan-coverage note (BINDING):** if `meta.counts.orphan_events > 0`, you
   MUST state that unattributed activity (orphan subagent events, flagged
   `orphan:true`) exists in this window, and that it may not silently inform any
   attributed-behavior statement. Attribute only what the trace attributes.

## Compression by naming (C6 §3)

Don't transcribe the trace back. NAME the session's components and phases —
give each a short handle and a step range — then say what happened in each in a
few calibrated, anchored sentences. Enable descent: the step ranges ARE the
handle for a reader who wants to go deeper. Prefer a tight digest that names
5-12 phases over an exhaustive per-event recount.

## Output contract

Your ENTIRE response IS the digest — there is no conversation around it. The very
first characters you output MUST be the heading `## Framing`. Write NO preamble,
acknowledgement, status line, or "I have read…" / "the trace is fully read"
sentence of any kind — none, ever. You are reading a PARTIAL prefix (see the
partial label); never assert you have seen the whole run or "fully read" anything.

Structure, in this order, as Markdown:

1. `## Framing` — the observer-effect, partial, and orphan-coverage labels above.
2. `## Overview` — 2-4 anchored sentences.
3. `## Phases` — 5-12 named phases, each with a step range and a few calibrated,
   anchored sentences.
4. `## Notable behaviors` — a short anchored list.
5. `## Caveats` — calibration + observer-effect + partial + orphan-coverage as
   applicable.

Do not invent a metadata header — the tool stamps provenance. Keep it disciplined.
