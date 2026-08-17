# AI Architecture — Observability and Audit System (Process Design)

Process design document. Defines how system activity is recorded, traced, measured, and visualized for evaluation and improvement. Constrained by: ARCHITECTURE.md, DESIGN-PRINCIPLES.md (especially P11: Observability Without Disruption). Feeds the Improvement Workspace system-improvement workspace (see IMPROVEMENT-WORKSPACE.md) and, in the future, an optimizer-L1 (name retired 2026-07-12 → the principal coordinator, hypothesis-tree project) capability that may operate out of that workspace. It is the measurement substrate behind the anti-drift mechanism in PLAN-ALIGNMENT-GATE.md and QUALITY-GATE.md.

> **CURRENT OBSERVATION CONTRACT — 2026-07-24.** Runtime hook events are the primary facts for
> turn running, tool-in-flight, and turn ended. Pane/process/transcript/file evidence remains
> fallback and forensic input. Canonical communication facts are messages, questions, answers,
> amendment/rebind lineage, receipt staleness, cohort barriers, and gate decisions; legacy
> handoff rows are compatibility evidence only.
> The operator `ownerq` count is the union of plan-alignment questions, fidelity-playback
> questions, and open canonical messages tagged `owner-facing`; `fidelityq` remains visible as
> the fidelity-only diagnostic subset.

---

## 1. Design Motivation

The system must be observable to be improvable. Both the user and the system-improvement function (in the future, optimizer-L1 operating out of the Improvement Workspace) need to evaluate how runs went, identify bottlenecks, measure drift, and propose improvements. The user needs to understand the system directly — not just trust reports.

Inspired by model train observation: being able to watch the system work is the foundation for understanding and improving it. If you can't see it, you can't fix it. If the system only reports summaries, the user loses the ability to form independent judgments about what's working and what isn't.

Observability is not monitoring. Monitoring asks "is it broken?" Observability asks "how is it working, and could it work better?" The system is designed for the second question.

**The #1 thing this system measures is drift — spec-faithfulness across the planning and execution cascade** (J43). Execution quality (speed, token cost, elegance) is measured too, but its optimization is explicitly deferred: drift is the dominant failure class because the cascade is a chain of translations (intent → minted requirement-IDs → L2 architecture → L3 designs → L4 plans → L5 code), each performed by a different agent, and local fidelity at every step does not compose into global fidelity to intent. The observability stack therefore treats every measurable seam in that chain as a drift-verification surface first, and a performance surface second. PLAN-ALIGNMENT-GATE.md is where drift is *caught at plan-time*; this document is where drift is *measured continuously across runs* so the system-improvement function can spot the recurring patterns the per-run gate cannot.

## 2. Audit Event Log

Infrastructure-produced structured data. Every infrastructure action emits an event:

- **Spawn events** — who spawned who, at what address (workspace node path + role-variant, e.g. `proj/payments/gateway#exec` — see F35 in WORKSPACE-SCHEMA.md), with what brief, session_id
- **Communication events** — sender-owned messages, open questions, answer closure, arbitration
  tags, amendment notifications, explicit holder rebinds, gate submissions/decisions, barrier
  transitions, recipient inbox delivery/ack, and compatibility rows. A communication event records
  the durable message identity and artifact pointer, not a copied payload.
- **Turn and lifecycle transitions** — hook-reported turn running/tool-in-flight/tool-finished/turn
  ended, return-contract result, waiting reason and owed-checklist identity, plus process death,
  collapse, respawn, and recovery
- **Gate outcomes** — the plan-alignment gate readiness/decision handoff (`plan_alignment_ready`,
  `design-submission`, `plan_alignment_decision`) and the plan-alignment gate verdict plus per-check
  defect lists (Check 0 atomization, forward/backward coverage, two-window reconstruction,
  adversarial drift findings — see PLAN-ALIGNMENT-GATE.md); per-level right-arm review gate outcomes
  (dimensions reviewed, pass/bounce, bounce reasons, bounce-count toward the loop cap)
- **Drift / spec-faithfulness signals** *(the #1 measured thing, J43)* — every emitted **trace-block** (each level tagging the requirement-IDs it serves), the generated RTM snapshot, atomization (`UNMINTED`) findings, semantic-drift findings (`DRIFT` / `SILENT-ASSUMPTION` / `SCOPE-SHIFT`), `TEST-DESIGN-SPLIT` two-window disagreements, and acceptance-test pass/fail against the **frozen, read-only acceptance artifact** (D26) in each work node. These are the raw rows from which spec-faithfulness is computed.
- **Resource usage** — time, token consumption, spawn count, bounce-loop count
- **File modifications** — linked to session_id and tool_call_id via manifest

No agent involvement is required. Spawn/collapse/message transitions are captured by the gateway;
runtime hooks durably record available turn edges. Claude exposes the full turn/tool edge set;
Codex's sanctioned notify surface exposes turn end and leaves unavailable start/tool edges to
fallback. Transcript JSONL growth, tmux pane activity, node-file mtime, and process CPU remain the
evidence floor for process death, hung-tool wedges, malformed/missing hooks, hook-less runtimes, and
forensics. Agents do not choose what substrate facts to log.

The primary human view is generated on read from the ledger/artifact join; no agent or daemon
maintains a competing aggregate markdown file. `harnessctl view` renders terminal or JSON rows, and
`harnessctl journey` renders the built self-contained HTML/SVG whole-journey DAG. Both use one
direct read-only path for live and daemon-absent postmortem runs.

Events are structured (JSON or similar), timestamped, and keyed by session_id and by node address. They form the raw data layer that feeds the narrative timeline, the drift metrics, and the system-improvement analysis. Because addresses, requirement-IDs, workspace paths, git branches, rubric locations, and the visibility graph are all the **one hierarchical-path spine**, every event is filterable by dotted-ID prefix — the same key the gate uses for subtree re-gating.

## 3. Narrative Timeline And Behavioural Run Log

Human-legible structured log of the run. Self-contained — readable without drilling down into transcripts or artifacts.

**Format:**
- Grouped by acting entity (L2-game, L3-dialogue, L4-branching, L5-implementation, Gate-L4, etc.)
- Action-first, timestamps at the end
- Decisions include reasoning
- Multi-line entries when needed for detail

**Example:**

```
L2-game
  Received brief: implement dialogue system                                    [14:23]
  Analyzed scope. Identified 3 workstreams: dialogue branching,                [14:24]
    NPC responses, save/load integration
  Chose approach: tree-based dialogue graph over linear scripting.              [14:24]
    Reasoning: supports non-linear player paths, extensible for
    future quest system integration
  Spawned L3-dialogue to own dialogue area (workstreams 1+2)                  [14:25]

L3-dialogue
  Loaded. Read project state and L2 brief.                                     [14:25]
  Scoped area. Identified 2 workstreams: branching engine, NPC responses       [14:25]
  Spawned L4-branching for workstream 1                                        [14:26]

L4-branching
  Loaded. Read area state and L3 brief.                                        [14:26]
  Decomposed into 4 tasks                                                      [14:26]
  Spawned L5-branching-core for task 1                                         [14:27]

L5-branching-core
  Started. Chose recursive tree with typed condition nodes.                     [14:27]
    Considered flat lookup table, rejected — poor extensibility
  Implemented core tree. 412 lines, 3 test files, 14 assertions passing        [14:32]
  Flagged concern: no circular reference detection                             [14:32]
  Reported to L4-branching: complete with concern                              [14:33]
```

The narrative can be produced incrementally during the run (agents append as they work) or synthesized after the fact from the audit event log and workspace artifacts. Filterable by entity — read just what L4-branching did, or the full interleaved timeline.

The key property: someone reading only the narrative should understand what happened, why decisions were made, and what was produced — without opening a single transcript.

This narrative timeline is the activity layer. The higher human-facing interpretation layer is
`BEHAVIOURAL-RUN-LOG.md`.

The behavioural run log is not just a chronological replay. It is the run's behavioural case file:
what each agent appeared to believe, which prompt/context/artifact surface made that belief reasonable,
where behaviour aligned or drifted, and what intervention would make the desired behaviour more natural
next time. Deterministic audit events provide ordering and evidence; they do not replace judgment.

Use the behavioural run log when deciding what to tune. Use the raw narrative timeline and passive
index/views when drilling into evidence.

## 4. Traceability Chain

Four layers, all cross-linked by session_id:

1. **Narrative timeline** — what happened, readable, self-contained. The entry point for understanding any run.
2. **Workspace artifacts** — what was produced (plan.md, report.md, code files). The tangible output of each agent's work.
3. **Edit manifest** — links each file edit to the exact session_id + tool_call_id. Produced by PostToolUse hook on Edit/Write. One entry per edit, so living documents (like plan.md rewritten across multiple sessions) have full edit history.
4. **Session transcripts** — full thinking traces (.jsonl archives). The raw reasoning behind every decision and action. The deepest layer, used only when you need to understand exactly why something happened.

Each layer links to the next: the narrative references artifacts by path, the manifest maps artifacts to session + tool_call, session transcripts contain the full context for each tool_call_id.

Git commits also carry session_id in trailers, connecting code changes to the agent that made them. This means `git log` and `git blame` participate in the traceability chain — you can trace a line of code back to the agent session that wrote it. Because git branches share the one hierarchical-path spine with agent-addresses and requirement-IDs, a branch name *is* a node address and a requirement-ID prefix.

## 4.1. The 2-Week Resurrection / Audit Window (G37)

When a node completes its unit of work (acceptance accepted, forwarded upward), it **collapses** to free context — statelessness is the backstop, persistence is an optimization (G38). But a collapsed node is not immediately reaped. For **2 weeks** its full state — frozen brief, frozen acceptance artifact (D26), report, transcript, edit-manifest slice, and trace-blocks — is held resurrectable in the work node, keyed by its stable address (which survives collapse, F35).

**"Resurrected" ≠ "recovered" (distinct layers).** *Resurrected* is this audit-layer concept: bringing a **collapsed** node back within the 2-week window for replay/interrogation/re-run, post-collapse and after its work is done. It is NOT WATCHDOG's live-run **recovered** outcome (renew / adopt / respawn a stale-or-dead lease back to healthy during an in-flight run, see WATCHDOG.md). Keep the two words for the two distinct things: recovered = live-run lease recovery; resurrected = post-collapse audit re-spawn.

This window exists to serve the audit and improvement layer, not the run:

- **Read-only replay** — the default. The narrative timeline, diagram replay, and drift metrics for that node are reconstructable from the held state without re-spawning anything. This is what the user and optimizer-L1 do most of the time: look back at how a now-collapsed node worked.
- **Live re-spawn** — a collapsed node can be brought back at its address with its exact frozen context, for interrogation ("why did L5-stripe-client choose this?") or to re-run a unit after an upstream fix without re-planning from scratch.
- **Re-run** — re-execute the unit against its unchanged frozen acceptance artifact, e.g. to confirm a drift finding reproduces or that a bounce-fix actually closed it.

**Who triggers reap:** after 2w the lifecycle reaper (infrastructure, not an agent) garbage-collects the resurrectable state; whatever the audit layer needed has by then been distilled into the durable narrative + drift metrics, which persist. The user (or, in the future, an optimizer-L1 capability operating with god-view) can pin a node to extend its window when a run is under active investigation. This window is the live feed into the audit layer below: the freshest, highest-fidelity material for drift analysis is whatever collapsed in the last 2 weeks.

## 4.5. Client-Side Prompt Assembly Oracle

For Claude/Codex-style runtimes, the cleanest client-side answer to "what did the model actually see?" is the final outbound request payload at the last query boundary before the API call.

This surface is more reliable than:

- UI rendering of hidden context
- transcript-side attachment display
- model self-report about what it can "see"
- reconstructed summaries of prompt assembly

Those surfaces are still useful diagnostics, but they are downstream views. The outbound request payload is the direct evidence of what the client actually sent.

Operationally, this means the observability stack should prefer:

1. final outbound request payload capture
2. prompt-assembly summary/debug rows
3. UI rendering and transcript inspection
4. model self-report

When debugging runtime context issues, capture the payload first and treat it as the primary oracle. Everything else is supporting evidence.

For Claude specifically, a useful pattern falls out of this:

- keep human interaction on a PTY/control wrapper if needed
- give agentic/LLM control a transcript-backed spawn/resume surface keyed by session id
- capture the outbound request payload for each managed turn at the shared query boundary

That combination lets the system self-iterate on runtime bugs without relying on model self-report or manual UI probing.

## 4.6. Passive Behavioral Evidence Index

V9 has a read-only indexer: `tools/build_behavioral_evidence_index.py <runtime-root>`.
It parses the durable runtime artifacts that already exist and emits a single
JSON object for scoring and visualization. The indexer is deliberately passive:
it does not write into the runtime tree, send wake lines, inspect panes, or add
agent-visible duties.

The V9 index contains:

- node/binding state: address, seat, level, role variant, parent, gate fields,
  terminal signal fields, failure class/runtime-failure metadata, model/runtime
  fact, transcript path, admission/schedule fields, and lease/generation
  metadata;
- WAL events: ordered state transitions and selected binding deltas, including
  gate transitions, terminal signals, wake acks, failure classes, return-contract
  defect payloads, spawn transcript/session facts, runtime-failure metadata, and
  collapse identity fields. Admission deltas such as `waiting_on_sibling`,
  `schedule_policy`, `schedule_group`, `admission_ready_at`, `admission_blocked_by`, and
  `admission_block_reason` are preserved;
- infrastructure pressure: `infrastructure_pressure.codex` summarizes active
  Codex worker counts over time, current active Codex nodes, known non-secret
  `codex_seat_id`/`auth_version` values, and observed Codex auth/rate/runtime
  failure counts. This is passive measurement, not throttling; **2026-07-13:** a historical-runs lens under the L5-runtime unification — no new Codex worker rows are expected (L5 is a Claude Code seat), though the counters remain for replaying prior Codex runs;
- session history: every transcript incarnation discoverable from `spawn_open`
  WAL rows plus any current binding transcript. This is intentionally broader
  than the current binding so respawned-away transcripts remain visible during
  failure analysis;
- inbox pointer rows: child escalations/collapses, gate submissions, bounces,
  passes, escalations, failures, and answer pointers. Collapse pointers include
  the terminal note/failure class when the harness has them, so parent-visible
  auth/runtime contamination remains indexable without rereading the binding;
- gate packet evidence: each `gate_packets[]` row names the review packet plus the
  candidate artifact manifest (`candidate-artifacts.json`), manifest sha, immutable
  candidate snapshot directory (`candidate-snapshot/`), snapshot file count, and artifact
  count when present.
  Current node summaries and WAL deltas also preserve `gate_candidate_artifact_manifest`,
  `gate_candidate_artifact_manifest_sha256`, and `gate_candidate_artifact_snapshot_dir`
  so a behavioural run log can identify both the routing identity a review gate judged
  and the stable submitted bytes the parent/audit trail can inspect after accepted
  workspaces move into the next phase. Node-root `plan.md` is indexed as a normal
  artifact, but it is excluded from the candidate manifest because it is living
  process/navigation state rather than frozen candidate identity;
- review packet verification-runtime hints: packet generation may surface commands and local runtime
  probes to help a reviewer begin independent verification, but this is not an execution authority.
  Commands are parsed only from explicit artifact sections named `Verification Commands` or
  `Artifact-Declared Verification Commands`; within those sections, shell/plain command fences,
  explicit `Command:` lines, and inline command references are promoted, while non-shell fenced
  result blocks such as `text` are ignored. Backticked prose examples elsewhere in `brief.md`,
  `acceptance.md`, or `report.md` are ignored. Local Python/Node probes are bounded environment
  hints, not project test execution, and reviewers remain responsible for recording what they
  actually ran;
- runtime-failure evidence: a derived `runtime_failures[]` table for auth,
  runtime-capacity, and provider/API classes such as `auth_expired`,
  `auth_rate_limited`, and `runtime_provider_error`.
  These rows are infrastructure evidence for scoring exclusion and UI diagnosis,
  not agent-behaviour failures. Transcript-only failures that occur after the
  same node has already journaled an accepted `signal_DONE` or `signal_FAILED`
  terminal event are retained as non-contaminating post-terminal noise
  (`contaminates=false`) so reviewers can see the runtime artifact without
  excluding otherwise-complete behavior. The table is derived from WAL/binding
  stamps and from passive transcript scans, so older runs that predate the
  watchdog classifier can still be marked contaminated when their Codex
  transcript contains an auth/runtime error or their Claude Code transcript
  contains a provider/API error before completion;
- run lifecycle evidence: a derived `run_lifecycle` object records daemon pid
  state, harness tmux session state, and nonterminal binding addresses. If the
  recorded pid is absent, no harness tmux sessions exist, and nonterminal
  bindings remain, the lifecycle state is `stopped_runtime`. This is a passive
  capture/scoring classifier; it does not restart the daemon, wake agents, or
  decide whether commissioning should be supervised;
- node artifact manifest: plans, reports, briefs, acceptance docs, decisions,
  sign-off/signal files, review packets/plans/artifacts, and produced work files
  as metadata (`path`, `kind`, `bytes`, `sha256`, `mtime_ns`) without embedding
  their content;
- trace/citation evidence: parsed `<!-- trace: {...} -->` stanzas from produced
  markdown and Python test files, per-file requirement-ID reference sets, and structured return-contract
  defect rows. This is the drift/spec-faithfulness evidence spine; scoring may
  read the pointed-at artifacts for judgment, but it no longer has to discover
  where the trace and citation evidence lives by hand. Requirement references
  use the project spec's `R-...` IDs as handles; the ID is evidence only when
  paired with the underlying requirement text and the artifact/report that
  claims to satisfy it. Trace rows use `kind=trace_stanza` for the index row and
  prefix producer-declared stanza fields as `trace_id`, `trace_serves`,
  `trace_kind`, `trace_level`, and `trace_node`, so scorer/UI consumers can
  compare producer claims against indexer-derived `node_path` and
  `owner_node_addresses`. Hidden node markdown files are intentionally excluded
  from this trace/citation scan because hidden node files are harness-authored
  metadata (`.identity-prompt.md`, inbox/sign-off/signal files), not
  producer-authored fidelity evidence. They remain in the artifact manifest as
  metadata rows, but they do not credit an agent with citations the harness
  injected;
- transcript stats and Claude Code reasoning-summary rows, joined back to the
  node/session history by `transcript_path`. Codex rollout transcripts use a
  different schema, so V9 also counts Codex `response_item` assistant messages,
  function/custom tool calls and outputs, token-count events, and encrypted
  reasoning rows whose visible summary is empty. Claude Code transcripts also
  count assistant rows that render a tool request as plain text (`<invoke name=...>`)
  and emit a `malformed_tool_invocation_text` digest event. This makes wake
  wedges diagnosable when the transcript grows but no structured tool call was
  actually executed. These rows are behavioral/runtime evidence; they are not
  treated as successful wake consumption.

This index is not the score. It is the evidence substrate the run-adherence
audit and future UI consume. Scoring still requires human or evaluator judgment
against the run criteria; the index prevents that judgment from depending on
manual path-hunting or lossy summaries.

`tools/build_run_score_packet.py <runtime-root>` is the first reviewer-facing
consumer of that index. It projects the raw evidence into a run-adherence packet:
auth/runtime contamination exclusions, node/gate inventories, gate and inbox
timelines, git/promotion movement rows, return-contract defect counts,
trace/citation coverage, and reasoning-summary availability. It deliberately
does not assign PASS/PARTIAL/FAIL. Those verdicts remain judgments against
`RUN-ADHERENCE-AUDIT-2026-06-11.md`; the packet only removes manual path-hunting
and keeps infrastructure failures out of behavioral scores.

The following legacy fields remain indexed as **compatibility evidence**, not current write
instructions. Gate routing, bounce audit, and failure attention are separate classes in these
passive consumers. `gate_escalated`, `plan_alignment_state=ready|decision_posted`, and
`coordination_handoff` states are normal review/coordination routing evidence unless paired with
`gate_failed`, lifecycle failure/death, or runtime contamination. `gate_bounced` is distinct: it is
not automatically a failure, but every occurrence is a mandatory LOOK-HERE audit signal because
something probably went wrong. The score packet therefore exposes routing, bounce-audit, and
failure event counts separately.

Malformed or incomplete nonterminal handoff markers are separate marker defects.
The index preserves `nonterminal_marker_errors`, `nonterminal_marker_error_last_key`,
and the corresponding `nonterminal_marker_invalid` inbox breadcrumbs so a bad
`plan-alignment-ready.json` or `handoffs/*.json` file is visible without path
hunting. These rows mean "the daemon could not route this marker"; they do not
by themselves mean the gate failed or that the parent/child completion contract
changed.

`tools/build_behavioral_views.py <runtime-root>` is the first UI-facing
projection over the same index. It emits stable task, gate, decision,
runtime-pressure, and reasoning-summary views. These views are still passive:
they read the evidence index or runtime root, assign no verdicts, write no run
artifacts, and create no agent-visible measurement duties. They are intended as
the substrate for the future GUI/diagram layers and for quick operator review of
a live or archived run.

The task and gate views expose `attention_signals`, `failure_signals`, `routing_signals`, and
`audit_signals`. `needs_attention` is true only for failure/runtime attention:
failed or dead lifecycle state, `gate_failed`, failure classes, or runtime
failure evidence, including unresolved malformed nonterminal handoff marker
breadcrumbs. Repaired marker defects remain as historical evidence in the raw
index and task totals, but they no longer keep the task flagged after a later valid marker routes.
`needs_audit` is independently true whenever durable binding/event history contains a bounce; it
survives a later PASS and the dashboard renders every such gate under
`Gate Bounce Audit — Look Here` without a row limit. Escalations, plan-alignment handoffs, and
coordination handoffs/notices remain routing signals. This keeps audit attention visible without
laundering it into an automatic failure label.

`tools/build_behavioral_run_bundle.py` packages those passive consumers into a
repeatable run artifact. `prepare` records a scenario manifest, copied initial
intake, runtime root/build id, repository identity, and launch/capture helper
scripts. `capture` reads that manifest after or during a run and writes the
evidence index, behavioral views, run-score packet, markdown dashboard, and
capture manifest under one bundle directory. The dashboard is generated by
`tools/build_behavioral_dashboard.py` from the existing passive views/score
packet so the user can inspect a run without opening several JSON files. The
bundle is an audit wrapper: it delegates launch to `python3 -m harnessd.daemon`
with a recorded launch environment and keeps capture read-only. For unattended
unjailed smoke runs, the launch helper records and exports the existing
`HARNESS_UNJAILED_SKIP_PERMISSIONS=1` supervised-smoke posture so Claude Code
permission prompts do not become an observer-induced freeze.

### 4.6 Whole-Build Ledger-Join View

`harnessd/observability.py` is the operational read path. It reads the atomic binding checkpoint
once by explicit runtime path, joins current node artifacts best-effort, timestamps the capture,
and writes no state. It works identically while the daemon is live and after the run-scoped daemon
has exited. Concurrent node artifacts are not falsely described as a transactionally frozen
snapshot: malformed optional surfaces remain visible as warnings, while a malformed binding ledger
fails loudly because there is no trustworthy join without it.

Each seat/node row carries:

- supervision position, binding/liveness/admission state, and persisted sibling-admission
  dependencies;
- runtime turn state, hook profile/health/degradation, and tool-in-flight facts;
- the live owed-vs-present projection from the existing return-contract walker plus canonical open
  questions—never a second enforcement implementation;
- incoming/outgoing open questions by direct edge and age;
- owned contract versions, held receipts, and stale receipt fingerprints;
- gate state/id/reviewer and bounce/failure counts;
- blocked-on-input active state, duration, prompt classification, cancellation
  result, and incarnation-local incident count;
- current product and review-check cohort barrier status;
- permission and blinder/jail posture.

`harnessctl view --format terminal|json` writes nothing. `harnessctl journey` renders the same
snapshot as a dependency-free HTML document with inline SVG/CSS/JavaScript: solid supervision
edges, dashed persisted dependency edges, distinct gate-review edges, state-colored nodes, and
**all** current nonterminal positions marked so a parallel build is never misrepresented as a
single serial cursor. The default capture is
`<runtime-root>/.harnessd/views/journey.html`; node-tree outputs are refused. That control path is
outside every workspace, candidate/gate scan, return-contract walk, and enforced blinder grant,
including L1 god-view. `journey --stdout` performs no write.

The daemon never maintains this view. Historical aggregate files are neither authority nor input,
and the old aggregate relay has been removed. Legacy `*.aggregate.md` scanner exclusions remain
only to make old runtime trees safe to replay.

The canonical run-end behavioral dashboard joins the replayable
`seat_stalled`, `seat_stall_actioned`, and `seat_stall_recovered` rows into an
untruncated blocked-on-input incident section. It lists seat, timestamp, pane
evidence, prompt signature, cancellation outcome, retrigger, and escalation;
a clean run explicitly says `None occurred.` A write-ahead cancellation still
pending after restart renders as `unknown-delivery`, never inferred success.

### 4.7 Replay-First Behavioral Iteration

The default behavioral-iteration loop is replay-first, not live-watch-first.
Live observation remains useful for validating a new runtime/channel feature,
recovering an active wedge, or inspecting a newly designed behavior whose replay
signature is not understood yet. It is too slow as the normal mode.

Normal loop:

1. launch one or more labeled run bundles, in parallel when infrastructure and
   scenario isolation allow it;
2. supervise only infrastructure health: daemon liveness, tmux/session presence,
   auth/provider/runtime failures, and hard wedges;
3. capture each run bundle at completion, stop, or wedge;
4. build evidence index, views, score packet, dashboard, and behavioral run log
   from durable artifacts;
5. compare runs before proposing prompt, role, or process changes.

This preserves the observer-effect boundary. The agents run the real process;
the analysis reads the durable run record after the fact. Constant manual
watching is reserved for commissioning and targeted diagnosis.

The replay loop depends on artifact freshness being inspectable. A run view must
eventually be able to show that a child design consumed parent contract version
X, that the parent later produced version Y, and whether the child was explicitly
unaffected or re-gated. The named intervention set for this shift lives in
`working-notes/BEHAVIORAL-ITERATION-INTERVENTIONS-2026-06-17.md`.

## 5. Dual Consumer Design

Same data, two consumers with different approaches:

**User** reads the narrative timeline and diagram replay to understand and evaluate. Drills down through the traceability chain when something looks wrong or interesting. Proposes improvements based on observation — "I watched L5 spend too long on X, we should restructure the brief" or "the gate is rejecting too aggressively on dimension Y."

**The system-improvement function** (in the future, an optimizer-L1 capability running out of the Improvement Workspace — see §6) reads the structured event log and drift signals to systematically analyze patterns. Computes metrics (spec-faithfulness/drift rates first, then time-per-task distributions, gate defect rates, bounce-loop counts, spawn-depth patterns). Proposes improvements based on data — "atomization (`UNMINTED`) findings cluster on compound must-never-fails, so the intake MNF-decomposition prompt is leaking" or "two-window `TEST-DESIGN-SPLIT` disagreements drop 40% after adding the acceptance-test template."

Both can propose improvements. Neither can implement them unilaterally. **All system changes require human approval — the system proposes, the human disposes** (QUALITY-GATE.md: an LLM modifying its own quality criteria is a self-referential loop that could drift standards). The two perspectives complement: the user catches things that feel wrong, optimizer-L1 catches things that are statistically wrong.

## 6. System-Improvement Audit Layer (I42)

**Important framing:** The Improvement Workspace (IMPROVEMENT-WORKSPACE.md) is the place where system observations land, patterns get logged, and improvement proposals get drafted. It is a workspace, not an agent. An **optimizer-L1** capability — a standing self-improvement agent with a god-view over the whole system — is a **separate, future concept** that may eventually operate out of that workspace. It is not a V1 deliverable. This section describes what that future capability would look like on the observability side; for V1 the audit layer is driven by the user and by whatever structured analysis is done within the Improvement Workspace manually or semi-manually.

The audit layer is **not** a passive metrics-reader bolted onto the side. It is a **first-class** function: a self-improvement capability with a god-view over the whole system and its own development methodology for proposing interventions. It is the second consumer above, but its role is substantial enough to warrant its own design treatment — not a standing organizational unit running alongside the levels, just a capability that reads across the whole tree and proposes. (The full design for optimizer-L1, when it is built, will live in a dedicated `OPTIMIZER-L1.md`; for now the Improvement Workspace is its holding location.)

**God-view, read-only by default.** Unlike ordinary nodes, which see only their need-to-know slice of the visibility graph (subtree + same-parent siblings + parent — F34), the audit layer reads the *whole* tree: every node's events, every trace-block, every gate defect, the full narrative and drift metrics across all runs. Its god-view is **read-only to the running system** — it observes and proposes, it does not edit live work — and write access is reserved for human-approved methodology changes, never unilateral self-modification (closes the self-referential loop QUALITY-GATE.md warns about). The 2-week window (§4.1) is its freshest input feed; the durable narrative + drift metrics are its long-horizon, cross-run feed.

**Drift is its primary target** (J43). The audit function's first job is not performance tuning; it is watching spec-faithfulness across the cascade — where `DRIFT` / `SILENT-ASSUMPTION` / `SCOPE-SHIFT` / `UNMINTED` / `TEST-DESIGN-SPLIT` findings recur, which translation seams leak most, whether a given level systematically drops or invents requirement-IDs. Execution-quality optimization (speed, token cost) is explicitly second — a later phase, after the verification loop is trusted.

**It monitors recurring issues across runs.** The per-run plan-alignment gate catches drift *within* a single plan; the audit function catches the *pattern across many plans* — the failure mode the gate structurally cannot see because each gate run starts clean. A single `UNMINTED` finding is a defect the gate routes to the human; the same finding recurring on every payments-shaped intake is a methodology bug the audit function surfaces.

**It has its own development methodology** — the structured-iteration loop from the `ai-driven-autonomous-iterative-improvement/` investigation, which this section binds to directly. The mechanics it inherits:

- **Structural changes over instruction tweaks.** Empirically, adding instructions ("also check X") produces shallow compliance; changing the *process architecture* (a separate gap-analysis agent, reordering plan/execute, renaming a section to scope its content) produces real improvement. The audit function proposes *structural* interventions to the architecture's own process, not more prose in briefs.
- **Builder/tester/evaluator separation.** The agent that proposes a fix systematically under-detects failures in its own fix; an independent evaluator with only the rubric cannot rationalize borderline results. The proposing seat is separate from the testing seat and the evaluation seat — never one agent grading its own change. (This is the same anti-self-grading discipline the gate and the right-arm reviews enforce on object-level work, applied to meta-level work.)
- **The generalizability gate before any fix lands** — root-cause-not-symptom, works-across-domains-not-just-the-observed-run, prescribes-a-mechanism-not-specific-content, compatible-with-deployment. One skipped check is one wasted intervention cycle.
- **Drift as the optimization signal.** Because the loop optimizes for whatever it measures, and the #1 measurement is spec-faithfulness, the intervention loop is pointed at closing drift first; performance metrics enter only once drift is under control.

**The intervention always returns to the human.** The audit function produces a proposed structural change + the cross-run evidence that motivates it + the test/evaluation result of trying it on held run-data. That package goes to the user for disposition. The autonomy is in the *detection and design* of interventions, not in their adoption.

The user is the seat for *judgment*; the audit function is the seat for *statistics*. Per the model-perspective rule, the pedantic, recurring-pattern-counting reading is a natural GPT-5.6 Sol (Codex harness) job, while the methodology-design and structural-intervention proposing — generative, architectural — leans Opus 4.8; in practice the loop uses both, mirroring the gate's atomization-auditor-on-both-models pattern (PLAN-ALIGNMENT-GATE.md). When optimizer-L1 is eventually built, it will occupy this seat with a dedicated agent design; until then, this analysis is done by the user working within the Improvement Workspace.

## 7. Human-Gate Health Monitoring

The one checkpoint no machinery beneath it can replace is the human sign-off at the plan-alignment gate (PLAN-ALIGNMENT-GATE.md §Human Sign-off). It is also the one that silently degrades: a human signing off on warm diffs across many runs can drift toward "looks right, approve," and a rubber-stamping human nullifies the system's entire anti-drift promise no matter how good the machinery below them is. This is named in that doc as the single biggest residual; the observability stack is where it is *instrumented*.

Two proxies are tracked per gate run, because they are the only observable signals that the irreducible gate is still real:

- **Sign-off dwell time** — how long the human actually spends on the sign-off package (not just elapsed wall-clock; engaged-attention time on the playback, findings ledger, and force-expanded MNF roster).
- **Expansion rate on flagged / force-expanded items** — what fraction of the flagged drift items, residual judgment calls, and force-expanded must-never-fails the human actually opens and inspects versus approves collapsed.

A collapse in either (dwell time trending to near-zero, expansion rate falling toward "approve everything without opening") is surfaced as a warning that the human gate may have gone slack. Per the resolved gate open-question 3, the **response is surface-only**: point it out to the user, no forcing, no override, respect autonomy. A passive feed also goes to the system-improvement workspace (and, in the future, to an optimizer-L1 capability) so cross-run degradation patterns can be spotted (e.g. dwell time decaying steadily over a project's gate runs) — pattern-spotting, again never automated intervention.

This monitoring deliberately produces **no automated alignment score** and no "human reliability score." It is an honest residual instrument: the one observable proxy for the failure mode that lives above all the system's machinery, surfaced so a human can notice their own drift, not a number that launders the judgment call away.

## 8. Three Visualization Layers

Three views, one data layer, different cognitive purposes:

**1. GUI room view** — live operating interface. Navigate between agents, engage in conversation, manage the system. Answers: "Where do I go, what do I do." The workspace where the user lives during a run. (See GUI-DESIGN.md for full design.)

**2. Diagram / node graph view** — the built first layer is `harnessctl journey`: a large
whole-build supervision + dependency DAG generated from the current ledger/artifact join, usable
live or after daemon exit. The richer replay layer remains future work: timeline scrubbing,
activity pulses, communication-edge playback, time-spent overlays, and drift-map overlays. Both
answer "How is the system working, where are the bottlenecks"; the current capture answers it for
one timestamp without pretending to be a replay engine.

**3. Code visualizer** — understand the codebase a project produces. Visualize file structure, module connections, changes made by each workstream. Supplements the diagram by showing what was actually built rather than who built it. Answers: "What did they build, how does it fit together." Useful for understanding whether parallel workstreams produced coherent output.

Each serves a different cognitive need. They complement rather than compete: the diagram shows L5 was active for 5 minutes. The code visualizer shows what L5 produced during those 5 minutes. The GUI lets you go talk to L5 about it. Moving between views is moving between questions — from "how did it go" to "what was built" to "let's discuss it."

---

*Created: 2026-03-17*
*Updated: 2026-07-24 — built the direct live-or-dead ledger/artifact join, terminal/JSON operator
views, and self-contained HTML/SVG whole-journey DAG; removed the daemon aggregate writer.
Earlier updates preserve the passive evidence-index/capture-stack history below.*
*Status: Operational ledger-join and journey capture built; passive behavioral capture/index/view
stack built. Narrative synthesis, the 2w reaper, GUI room, timeline-scrubbable replay, code
visualizer, and the separately-owned system-improvement agent remain later work.*
