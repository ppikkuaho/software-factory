# Hypothesis Tree Research System — Trace Reader Build Design (Area C6)

> **Status:** Working note; design converged 2026-07-07. **First build target.**
> **Parents:** `RESEARCH-SYSTEM-CONCEPT-2026-07-07.md` (§10 three-stream design,
> §8.4 graded control, §13.4 work-queue item) + A2 (location), A3 (packet
> provenance). Same directory.
> **Provenance note:** parts of this design were first worked in a post-compaction
> branch of the founding session that was later abandoned by a rewind. Everything
> here is re-ratified or freshly verified — attributions in §9 and the Decision
> Log. Substrate claims below were **verified against real transcripts**, not
> assumed.

Tags: ✅ committed · 🟡 committed concept, mechanics open · 🔲 open.

---

## 1. Two populations, two products ✅ (the load-bearing distinction)

The trace reader is one shared instrument over one substrate, but its outputs split
by *purpose* — and conflating them was a real observed failure (a compacted
successor transferred the live-read justification from research units onto L1-L5,
where nothing requires it):

| | **Behavioral Record** | **Progress Overview** |
|---|---|---|
| Grade | Evidence | Control signal |
| Mode | Post-hoc, complete sessions | Live-read, running sessions |
| Subject | L1-L5 production runs (and, later, unit archives) | Research-unit dispatches; L1-L5 live runs for the user |
| Consumers | Observatory, verifier | Director (§8.4 graded control); the user, pre-director |
| Standing | Anchored, observer-effect labeled; enters the system **only through adjudication** | **Uncitable by rule** (same standing as generated views); labeled "as of step N — still running"; **no completion or coverage statements legal from a partial trace**; expires on use |
| Failure cost | Corrupted research (false claims in the tree) | A wrong steer/recall — operational, recoverable |

Ruling 2026-07-07: **both products are built in one go** (deferral of Progress
Overview overridden by user — shared substrate makes it cheap, and live L1-L5
visibility is useful to the user before any director exists).

## 2. Verified substrate facts ✅ (checked against real transcripts, incl. the founding session)

- **One content block per JSONL line** — each thinking block, text block, and tool
  call is its own line; live-append at per-action granularity (measured mid-turn).
- **Typed non-message lines exist**: `system/compact_boundary` (exactly one per
  compaction), `file-history-snapshot` (checkpoint markers — 61 in the founding
  session), `mode`, `permission-mode`, `queue-operation`, `attachment`, etc.
- **Transcripts are branching trees**, not lines: every message carries
  `parentUuid`; rewinds/retries create fork points (5 in the founding session).
  The "conversation" is a *chosen root-to-leaf path*; abandoned branches persist
  in the file. **Measured ground truth for the founding-session fixture**
  (2026-07-07, re-derivable from the transcript; gates Phase 1 acceptance):
  536 messages on the active path; 111 abandoned messages in **10 branch
  groups** — groups ≥ fork points, because chain-break roots (e.g. the
  post-compact segment) form groups without a >1-child fork; exactly 1
  `compact_boundary`.
- **The parent chain traverses non-message rows** (checkpoint snapshots, meta
  lines): a chain graph built over message rows only *snaps* at rewind points —
  verified the hard way during the v2 dialogue extraction (active path collapsed
  from 536 to 10 messages until the graph included all uuid-bearing rows). The
  extractor's graph must be built over **every** uuid-bearing row; type filtering
  happens at emit time only.
- **Compact boundaries can lie on abandoned branches** (build finding,
  2026-07-07: the fixture's only `compact_boundary` is on a rewound path) —
  stage-2 experience reconstruction must handle compactions in branches.jsonl,
  not just the active trace. Also verified: subagent transcripts can themselves
  contain rewinds (actor-scoped branching), and a branch *group* can be empty of
  emittable events (branch_ids-in-file may be < branch groups).
- **Session bundle, not file**: subagent transcripts live in a parallel
  `<session-uuid>/subagents/agent-<id>.jsonl` (+ `.meta.json` sidecars), appearing
  mid-session; `tool-results/` and `workflows/` subdirs exist. (Corrected from an
  earlier stale claim that sidechain traffic shares the main file.)
- **Thinking blocks are already-summarized reasoning**, preserved as returned
  (user correction 2026-07-07). The trace stores them **as-transcribed**;
  digests are summaries-of-summaries. Redacted/encrypted segments stored as typed
  markers. **Thinking bodies can be entirely EMPTY in real runs** (token counts
  and timestamps only — verified on L1-L5 run a00d9542): reasoning-stream
  availability varies by run; digests must degrade gracefully to metadata-only
  use with zero mechanism claims.
- **The system prompt is absent from transcripts** — the experience stream must
  source injected material elsewhere; for harness agents that is exactly the A3
  substrate's generated artifacts (`.launch-packet.md` + hash on the binding).
- Per-message token usage (incl. cache split) and timestamps are present
  throughout.

## 3. Architecture: three stages ✅

**Stage 1 — Extractor** (deterministic, no LLM, cheap enough for 100% coverage).
Input: a **session bundle**. Output: the **trace file** — a normalized, typed event
sequence:

- Event kinds: `tool_call` (tool, input digest, result digest, tokens, duration),
  `thinking` (as-transcribed), `user_msg` / `assistant_text`, `lifecycle`
  (subagent spawn/return, **compact_boundary**, checkpoint/fork markers, mode
  changes).
- Every event: `step` ordinal, timestamp, `actor` (which file in the bundle),
  `raw_ptr` back to the exact JSONL line — **anchor discipline from birth**, which
  is what lets verifier claims cite trace spans later.
- **Parent-chain-aware** ✅ (requirement earned from a live failure — see §8): the
  extractor walks the active path from the final leaf; **abandoned branches are
  extracted separately and labeled as branches** (with fork-point refs), never
  interleaved. Naive file-order walking demonstrably mixes rejected timelines into
  the record.
- **No-EOF assumption** ✅: nothing in the extractor requires a session to be
  complete; a transcript is always a valid prefix. (This single constraint is what
  keeps live-read cheap.)

**Stage 2 — Digest** (LLM over the trace, never the raw transcript). Compression by
naming: components named, descent on demand. Two digest flavors keyed to the two
products (§1).

**Stage 3 — Views** (question-specific): largely computed features + judgment on
top — e.g. the orientation study: first-k actions vs. task-relevant file set,
time-to-first-relevant-read, off-task read count.

## 4. The streams, mapped to substrate ✅

- **Action stream** ← tool_call/lifecycle events. Detection: mechanical,
  exhaustive.
- **Reasoning stream** ← thinking events (as-transcribed summaries), **top of the
  diagnostic stack**; calibration rule applies doubly since these are summaries:
  strong evidence of *stated rationale*, never proof of mechanism.
- **Experience stream** ← reconstruction: context assembly at a decision point =
  prior events + injected material (launch packet by hash, A3) + attachments.
  **`compact_boundary` is a context event**: after it, what the agent could see is
  summary + tail — empathetic replay of post-compaction decisions must reconstruct
  *that* view, not the full history.

## 5. Live-read (Progress Overview mechanics) ✅ concept / 🔲 details

- Parse the bundle as it stands now (valid prefix); digest with the mandatory
  partial labeling; **new files appearing mid-session** (subagents) are part of
  tailing.
- **Session-end detection is required and non-trivial** (transcripts don't
  announce completion): terminal-signal heuristic where harness signals exist
  (L1-L5 journaled sign-offs), staleness fallback otherwise. 🔲 heuristic details
  at build.
- **Checkpointed incremental tailing** (offsets, O(delta) updates) is a
  **growth-path optimization** — re-parsing a few MB per request is wasteful but
  cheap; add when overview frequency or file size warrants.

## 6. Location & governance ✅

`research-system/system/instruments/trace-reader/` (A2 ruling;
instrument code lives in the methodology lane beside the registry). Versioned;
changes epoch-gated once the research system runs. First consumer: **L1-L5
behavioral analysis** (the observatory's precursor use) — also the founding
instrument of the parked L1-L5 evidence plane (concept §13.11).

## 7. Build plan sketch 🟡

1. Extractor over Claude Code session bundles — **test fixture: the founding
   session's own transcript** (it contains branches, one compaction, subagent
   spawns, and 850+ lines of every event type in one file).
2. Behavioral-digest prompts + the orientation-study view (the already-felt L4
   pain as acceptance case).
3. Progress Overview: live parse + partial labeling + end-detection heuristic.
4. Wire as observatory layer-1/2 substrate (C1 note §3).

## 8. The branch-awareness requirement — provenance ✅

Earned, not theorized: a hand-rolled dialogue extraction walked the founding
session's JSONL in file order and interleaved an abandoned branch's content
(rejected design framings) into what presented itself as "the complete verbatim
dialogue." Rejected-timeline material masquerading as the record is the temporal
form of claim inflation. Hence: parent-chain walking is **mandatory**, branches are
evidence-but-labeled, and any extraction tool that ignores `parentUuid` is
non-conforming.

## 9. Open within this area 🔲

1. Trace-file serialization format (JSONL of typed events, presumably) — at build.
2. Digest formats per product; overview format for the user as consumer.
3. Session-end detection heuristic (§5).
4. **Codex-format adapter**: L5 executor seats run on the Codex harness (GPT-5.5) —
   different transcript format entirely. V1 = Claude Code format; the Codex
   adapter is required before the observatory can cover L5 seats fully.
5. Checkpointed tailing (growth-path).
6. Experience-stream assembly for non-harness sessions (no launch-packet
   provenance to lean on).

## 10. Decision log

| Ruling | Decision | Date |
|---|---|---|
| DP-2 | Thinking **as-transcribed** in the trace file (transcripts hold already-summarized reasoning — user correction); further compression only at digest time | 2026-07-07 (re-ratified post-rewind) |
| DP-3 | Two products with different epistemic standing; Progress Overview uncitable-by-rule; **both built in one go** (user overrode the deferral); checkpointed tailing growth-path; no-EOF constraint in v1 | 2026-07-07 (re-cut + ratified) |
| DP-1 (successor's) | Withdrawn — location was already settled by A2 | 2026-07-07 |
| Branch-awareness | Parent-chain-aware extraction mandatory; abandoned branches labeled | 2026-07-07 (earned, §8) |
| Substrate facts | §2 verified against real transcripts (founding session as fixture) | 2026-07-07 |
