You are producing a **Behavioral Record v2** for one completed agent run. Work
only from the extracted trace bundle named below. The record is a generated,
uncitable view which becomes acceptable only after independent review; as the
generation participant, do not issue an acceptance verdict or claim that the
record passed review.

## Input contract

Read only these files in the working directory:

- `trace.jsonl` — the active-path event stream, one JSON event per line.
- `branches.jsonl` — abandoned events carrying `branch_id` and `fork_raw_ptr`.
  These are REJECTED timelines: never report them as actions the run performed.
- `actors.json` — actors, join information, and orphan flags.
- `meta.json` — run provenance, counts, `partial`, and actor watermarks.
- `orient.json` — if present, computed orientation features per actor.

Do not read the raw transcript, source bundle, repository, or any other file.
The trace is the whole world. If these inputs do not establish a fact, report it
as unknown rather than infer it. Treat `thinking` events only as transcribed
stated rationale, never as mechanism, knowledge, intent, or proof. Actions are
stronger evidence but do not justify imputing intent. If orphan events exist,
quarantine them from attributed claims and disclose them tersely in the record.

The driver mechanically supplies these values for the footer; reproduce them
exactly and do not calculate replacements:

- prompt template sha256: `{{PROMPT_TEMPLATE_SHA256}}`
- trace-file sha256 values: `{{TRACE_FILES_SHA256}}`

## The one hard rule — anchoring

EVERY sentence that states something about the run's behavior MUST cite the
step(s) it rests on, inline, in square brackets: `[main 210]`, `[main 210-235]`,
`[agent-a069c431db7cf11bb 5-9]`. A ranged cite is fine. An unanchored behavioral
claim is a BUG — the reader must be able to descend to the exact events. The
structural diagram and compact header fields must also carry anchors wherever
they state behavior; section headings and epistemic/provenance codes are exempt.

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

The mechanical anchor detector is presence-only and cannot catch a plausible but
wrong step. An independent reviewer therefore performs the acceptance-blocking
semantic spot-check of at least ten anchors; never self-certify that check.

## Mechanically stamped frontmatter contract

Emit only the four body sections below. The driver, not you, prepends YAML
frontmatter with this grammar and the real computed values:

```yaml
---
digest_version: trace-reader-digest/2.0.0
generation: 3
predecessor: digest.v2.md
flavor: record
generation_path: claude-p
model: model-id
generated_at: 2000-01-01T00:00:00Z
prompt_template:
  name: behavioral-record.v2.md
  version: trace-reader-digest/2.0.0
  sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
trace_files:
  trace.jsonl: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  branches.jsonl: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  actors.json: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  meta.json: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  orient.json: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
source_partial: true
orphan_events: 0
permission_denials: []
anchor_check:
  paragraphs_checked: 0
  unanchored: 0
  note: presence-only/off-by-N limitation; reviewer semantic check is blocking
standing: "generated view — cite the anchored trace spans, not this digest"
---
```

The lineage mapping is mandatory and explicit: `generation: 3`,
`predecessor: digest.v2.md`, and `prompt_template` containing `name`, `version`,
and `sha256`. Do not reconstruct or alter it from memory.

## Output contract — exactly four sections, in this order

Your entire response is the digest body. Write no preamble, framing block,
overview, notable-behaviors section, caveat-prose section, acknowledgement, or
status line. Never claim to have “fully read” a partial trace. Target roughly
half the length of the v1 Behavioral Record; prefer short clauses over caveat
paragraphs. Produce exactly these four surfaces and nothing else:

1. **Header line.** One physical line beginning `**Run:**` with compact fields
   for run id, actors, outcome, total tokens, wall-clock, permission_denials,
   and supporting anchors. Use `unknown` where a value is not evidenced. No
   prose paragraph; at most 45 words.
2. **`## Flow map`.** A compact text diagram of actors and named phases, with
   arrows for handoffs, explicit break points, and explicit loop/bounce counts.
   This is the high-level flow view. Use at most eight diagram lines plus two
   terse anchored explanatory sentences.
3. **`## Per-actor play-by-play`.** A linear, chronological account, actor by
   actor. Under each `### <actor-id>`, cover exactly three labeled items:
   `Exploration` — judge whether the actor explored a reasonable task-relevant
   set and found it efficiently, giving the computed basis from `orient.json`
   (`params.relevant`, `first_relevant_read` including latency,
   `reads_before_first_relevant`, and coverage limitations), not a list of
   filenames; `Operations` — tests, code or other changes, commands/runs, and
   their results; `Closedown` — how the actor concluded, including its report,
   signals, and workspace hygiene. Every behavioral sentence needs inline step
   anchors. Budget one or two sentences per labeled item; cut the fluff.
4. **`## Record footer`.** Exactly two physical lines and no prose after them:
   one `**Provenance:**` line containing the supplied prompt-template and
   trace-file sha256 values plus the `meta.json` partial/watermark values; then
   one `**Epistemic:**` line containing all codes
   `standing=generated-view/uncitable`, `observer-effect=post-hoc/naming-shapes-view`,
   `thinking-opacity=stated-rationale-not-mechanism`,
   `partial/watermark=<meta values>`, and
   `detector-limitation=presence-only/off-by-N-blind;semantic-review>=10-blocking`.

Draft to the ratified RQ-2 requirements: **Play-by-play, linear.** Give a
**high-level flow view** of where work flowed, broke, and looped. For exploration
quality, do **not** give a list of named files: judge whether it explored
reasonable files and whether its finding was efficient, using the computed
task-relevant file set and time-to-first-relevant-read as the basis. State the
operations taken and their results, then the closedown. **Cut the fluff (~half
the current file)** while preserving terse inline evidence anchors.
