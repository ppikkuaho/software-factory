# AI Architecture — Workspace and Document Schema (Process Design)

Defines workspace structure, document types, edit policies, and naming conventions for each level in the 5-level hierarchy.

> **CURRENT CONTROL-PLANE LAYOUT — 2026-07-24.** The canonical runtime communication and
> observation paths are listed below. Older `comms/` mailbox and daemon-maintained
> `log.aggregate.md` / `status.aggregate.md` sections are historical; they are not the current
> write target or a second source of truth.

For each occupied seat the harness owns `.turn-state.<seat>.json`,
`.turn-events.<seat>.jsonl`, `.owed-checklist.<seat>.json`, `.sign-off.<seat>.json`,
`.signal.<seat>.json`, and `.inbox.<seat>.jsonl`. Each sender node owns its `messages/`; each
contract owner owns immutable `contract-revisions/`; and each holder owns fenced acknowledgements
under `contract-rebind/`. Review submission owns `reviews/<gate-id>/candidate-artifacts.json` plus
immutable `reviews/<gate-id>/candidate-snapshot/`. An L2+ FULL review also owns one
content-addressed `product-probe-roster.<sha256>.json` and separate
`reviewers/<probe>/disposable-instance/` copies for its user-simulation and
performance/robustness seats. Accepted tests remain at their one stable L4-owned contract home.
Intent, briefs, acceptance, accepted tests, and review candidates are identified by notary
stamps/receipts.

Human-readable status and the built large journey DAG are generated on read by joining bindings,
messages/questions, turn state, receipts, barriers, artifacts, reports, and verdicts. The separate
post-hoc timeline/capture stack additionally reads WAL and transcripts. Agents and the daemon do
not maintain aggregate markdown truth.

---

## Overview

The project workspace is a shared file space where ephemeral agents come and go. Quality lives in artifacts and conventions, not individual continuity. Closest real-world analog: open source projects — stateless contributors, artifact-first, quality from conventions not continuity. Also relevant: consulting firm methodologies (standardized docs, rotating staff) and SBAR medical handoffs (structured handoff protocols).

The workspace tree mirrors the management tree. Each parent physically contains its children's workspaces. Five levels, nested: L1 owns the portfolio, L2 owns the project, L3 owns areas within the project, L4 owns workstreams within areas, L5 owns tasks within workstreams. The path tells you who owns what.

## The One Hierarchical-Path Spine

The single most load-bearing structural decision in the system: **one hierarchical path is reused as six things at once.** A node's position in the workspace tree *is* its identity for every other subsystem. There is one namespace, not six parallel ones to keep in sync.

| Facet | Realized as | Example |
|-------|-------------|---------|
| **Workspace path** | The folder in this tree | `proj/payments/gateway/stripe-client/` |
| **Agent address** | Workspace node path + `#role-variant` suffix | `proj/payments/gateway#exec`, `proj/payments/gateway#review` |
| **Requirement ID** | Dotted prefix mirroring the same descent | `R-003.2.1` |
| **Git branch** | The path as a branch name | `payments/gateway/stripe-client` |
| **Rubric location** | The `acceptance.md` at that node | `proj/payments/gateway/stripe-client/acceptance.md` |
| **Visibility scope** | Derived from the path (subtree + siblings + parent) | see below |

Properties of the spine:

- **Semantic, not numeric.** Addresses use area/workstream/task names (`payments/gateway`), not opaque indices (`L3.1`). The name is the address.
- **Bound to the work node, not the instance.** An address survives respawn, collapse, and resurrection because it is a property of the *position in the tree*, not of any one ephemeral agent. A fresh execution-L3 (L3) inherits the same address its planning-L3 (L3&) used.
- **Role-variant suffix disambiguates seats at one node.** A work node hosts more than one seat — an executor (`#exec`) and its independent reviewer (`#review`, the L5+ / right-arm reviewer). The suffix names the seat; the path names the node.
- **Parent is recoverable by truncation.** Drop the last path segment (or the last dotted index) and you have the parent — for both addresses and requirement IDs. A child cannot exist without a declared parent.
- **One decision serves all.** Decide where a unit sits once, and its branch, its rubric file, its address, its requirement-ID prefix, and its visibility scope all follow. This is what makes the [requirements traceability](PLAN-ALIGNMENT-GATE.md) RTM generable by truncation and the visibility graph derivable rather than hand-maintained.

**Two renderings of the same path.** The physical workspace tree carries explicit `L{n}/` segments for namespace isolation and ownership clarity (`L3/payments/L4/gateway/L5/stripe-client/`). The *address* and *branch* renderings collapse those level markers to the semantic spine (`payments/gateway/stripe-client`, matching `git-protocol.md`'s `{area}/{workstream}/{task}`). They are the same descent; the level markers are folder bookkeeping, not part of the identity.

Cross-references: requirement IDs and the RTM are specified in `PLAN-ALIGNMENT-GATE.md`; branch naming in `git-protocol.md`; addressing-as-routing and the live transport in `COMMUNICATION.md`.

### Trace-Blocks Live in the Work-Node Artifacts

The requirement-traceability links are carried **per element, inside the work-node artifacts themselves** — not in a separate trace registry and not as whole-file `intent_ids:` front-matter. Every element a level authors (an ADR, an interface clause or contract field, a substrate primitive, a design element, a workstream, a task, an acceptance test, a rubric line) carries a small adjacent machine-readable `trace:` stanza — `{ id, serves, kind, level, node }` — where `node` is this same one-spine path. The artifacts that hold them are the work-node files defined below: `project.md` / `design.md` / `plan.md` (design and workstream elements), the interface contracts, the decision/ADR records, `brief.md` (the responsible ID-set), and `acceptance.md` (per-test/per-rubric-line stanzas). Because the stanza is co-located with the element and keyed by the node path, the RTM-builder greps the artifacts in place and joins by prefix-truncation and `serves` — generated, never maintained. The exact stanza syntax, the dotted-child minting rule, the per-level emission obligation, and the preflight-hook rejection conditions are specified in `PLAN-ALIGNMENT-GATE.md` (Requirements Traceability); this schema only fixes *where* the stanzas live (in the node) and *that* they are per-element, not per-file.

## Visibility & Read-Permission Graph

**Supersedes** the earlier "any agent can read broadly across the project" posture. Read access is **need-to-know**, derived mechanically from the path spine. The default is *not* broad project-wide read; it is a tight neighborhood plus two god-view exceptions.

For any node, the readable set is:

1. **Own subtree** — everything at and below the node's own path. A node owns its descendants' work and must read it.
2. **Same-level siblings' direct node surfaces** — nodes sharing the same parent
   (`payments/gateway` ↔ `payments/webhooks`). Direct files in each known sibling node remain
   visible, including files published after spawn; nested sibling directories do not. **Cousins are
   excluded** — `payments/gateway` does NOT see `checkout/cart`.
3. **Parent direct node surface** — one level up (the node's path minus its last segment). Direct
   parent files are visible; the parent's other nested branches are not.

Everything else is **not readable by default**. A sibling publishes an interface contract on its
direct surface; its nested internals stay physically closed. Cross-parent coordination is escalated
to the common ancestor that owns both contracts, per `DECOMPOSITION-METHODOLOGY.md`.

**Two god-view exceptions** (read-only, whole-portfolio):

- **L1** sees the entire portfolio. It owns intent and triage; it must be able to read any node to guard fidelity and route escalations.
- **Optimizer-L1** (Improvement Workspace) — a **future, not-V1** capability — would see the entire portfolio, read-only, for recurring-issue monitoring and cross-run pattern-spotting. The Improvement Workspace exists as a passive accumulation layer today; the optimizer that reads it is a separate, later concept. See `IMPROVEMENT-WORKSPACE.md`.

Both god-views are **read-only**: they observe everything, mutate nothing in the work tree.

**Direction asymmetry.** Visibility (read) is the tight neighborhood above. *Authority* (downward command/override) flows freely down one's own subtree and is unrestricted there — a parent may direct any descendant. Visibility ≠ authority: a node can read its siblings but has no authority over them; a parent has authority over the whole subtree but, in practice, reads through the living-doc navigation layer rather than micromanaging every leaf.

**Read table:**

| Reader | Own subtree | Same-level siblings | Parent | Cousins / other subtrees |
|--------|-------------|---------------------|--------|--------------------------|
| L2 | ✅ whole project | (peer projects: no) | client-brief, L1 brief | — |
| L3 (area) | ✅ area + below | ✅ sibling areas (contracts) | L2 project.md, L2 brief | ❌ |
| L4 (workstream) | ✅ workstream + tasks | ✅ sibling workstreams (contracts) | L3 area design, L3 brief | ❌ |
| L5 (task) | ✅ task folder | ✅ sibling tasks (contracts) | L4 workstream plan, brief | ❌ |
| L1 | ✅ whole portfolio (god-view) | — | — | ✅ (god-view) |
| optimizer-L1 | ✅ whole portfolio (god-view, read-only) | — | — | ✅ (god-view, read-only) |

**Derivation, not maintenance.** The spawn chokepoint makes this graph physical with the
strong-form seatbelt blinders in `SECURITY.md`: own subtree is a subpath grant; parent and each
known same-parent sibling are anchored one-level direct-file grants; cousins remain closed. Known
sibling roots are resolved from current bindings at spawn, while the one-level rule allows a
sibling to publish a new direct file without regenerating the profile. Exact load-manifest,
identity/launch, spec/acceptance, and visible reference-map targets are separate declared-document
grants. L1 whole-tree read is derived only from canonical address `L1`; the future optimizer-L1
exception is not implemented. Production rolls the new broad-read inversion out as `observe`
before `enforce`; the write/secret floor is active in both modes. A node respawned at the same path
derives the same address-owned visibility from the then-current graph.

### Loaded Context vs Readable Context

Visibility is not the same as boot context.

The acting agent's first context is deterministic and task-shaped:

1. the per-spawn identity prompt / identity trio selected by `role_variant`;
2. the node-local `brief.md` with its load-manifest;
3. the frozen `acceptance.md` / rubric and the parent-provided task pointers;
4. the specific role/protocol documents named by that manifest.

The wider read graph is available for bounded lookup, not default ingestion. A workstream L4 may be
able to read sibling L4 plans and published contracts, and an execution-L3 may be able to read sibling
area material, but those files are not part of the ordinary working context unless the assignment,
interface, dependency, or a concrete ambiguity makes them relevant. Most work should proceed from the
boot package and the local slice. Broad searching across the visible tree is diagnostic behavior, not
normal startup behavior.

## Logical Project Document And Scoped Slices

The workspace is one logical project document, physically stored as owned slices on the hierarchical
path spine. The files are not private, disconnected documents. They are the local materialization of
one larger project artifact: intent, architecture, interfaces, area designs, workstream plans, task
briefs, acceptance packages, reports, and review verdicts.

Behaviorally, lower agents still experience the system as a small local file set. An L5 reads its task
brief, frozen acceptance, relevant interfaces, and local context. An L3 area reads its area slice,
parent project context, and relevant sibling contracts. It does not need to load the whole project to
do a bounded job.

The logical-document substrate is agent-first. Slice lineage, hashes, dependency edges, freshness
states, and composed-view indexes are harness/control-plane and observability machinery unless a role
specifically owns that work. Ordinary L4/L5/L5+ agents should keep receiving task-shaped context, not a
slice ledger. When lineage machinery discovers that an agent must act, the harness or parent should
translate it into the normal agent surface: a delta brief, a coordination decision, a gate bounce, an
updated acceptance package, or a clear parent instruction with the relevant files.

Higher levels see composed views over larger slices:

- L4 sees the workstream and its task slices;
- L3 sees the full area and workstream slices under it;
- L2 sees the whole project architecture, all area slices, and cross-area contracts;
- L1 sees the project and portfolio view needed for intent fidelity and user-facing sign-off.

Mutation remains owner-scoped. A level can edit only the slice it owns, even when it can read a wider
composed view. Parent edits to shared contracts, ADRs, or architecture are not silent rewrites of
child-owned designs; they create a new parent slice version. If that version changes a dependency a
child slice consumed, the child slice is stale until one of two things happens:

- the parent records that the change does not affect that child slice; or
- the affected child scope is re-incarnated or repaired and passes its review gate against the new
  parent slice.

This is the intended shape for plan-alignment and behavioral observability: the plan is assembled
from distributed owned slices, but it is reviewed as one coherent document. The system must therefore
track slice identity, dependency edges, gate verdicts, and supersession so a higher level can see
whether the composed document is fresh rather than merely present.

## Workspace Structure

```
project-{name}/
  client-brief/
    raw-request.md                    # Exact start intake bytes; frozen with intent
    intent-spec.md                    # Canonical tagged intent; frozen at confirmation
    vision.md                       # The user's vision as articulated with L1 (immutable)
    priorities.md                   # User's triage + priority overrides (immutable)
  L2-config.md                      # L2 role identity, domain context, user priorities (spawn artifact)
  README.md                         # Onboarding (L2 maintains)
  conventions.md                    # Project conventions (L2 writes)
  status.md                         # Central project status board (all leads update)
  log.md                            # Append-only structured project log

  L2/
    project.md                      # Concept design → evolves into living project state
    decisions/                      # Numbered, immutable
    briefs/                         # Area briefs for L3s

  L3/
    {area}/
      design.md                     # Detailed area design (north star for execution)
      plan.md                       # Living workstream status (execution phase)
      briefs/                       # Workstream briefs for L4s
      reviews/                      # Reviews of L4 work

      L4/
        {workstream}/
          plan.md                   # Task decomposition + status
          briefs/                   # Task briefs for L5s
          reviews/                  # Reviews of L5 work

          L5/
            {task}/
              brief.md              # Task brief (L4 → L5; immutable once sent)
              acceptance.md         # FROZEN rubric + acceptance tests (read-only to L5; see below)
              report.md             # Structured report (templated)
              scratch/              # Temp work, infrastructure-cleaned
              [final artifacts]

  messages/                         # Canonical sender-owned direct-edge messages
  status/                           # Status board
  reference/                        # Shared reference material
```

The nesting ensures three things:

- **Namespace isolation** — two areas can have workstreams with the same name without collision.
- **Clear ownership** — the path tells you who owns what: `payments/gateway/stripe-client/` is unambiguous.
- **Access boundaries** — derived from the path: the [Visibility & Read-Permission Graph](#visibility--read-permission-graph) (own subtree + same-level siblings + parent) is computed directly from where a node sits in this tree.

The same hierarchical path is reused as five things at once — see [The One Hierarchical-Path Spine](#the-one-hierarchical-path-spine).

## Key Artifacts

### client-brief/raw-request.md

The exact initial user request bytes copied from the start intake row. L1 freezes it beside the
intent-spec at reflect-back confirmation. It survives the discarded grilling transcript as the
independent source for the once-per-intent atomization audit. Changes require an explicit revision
record.

### client-brief/vision.md

The user's vision as articulated through collaborative discussion with L1. Contains: what is being built, who it serves, what problem it solves, what success looks like, and any constraints or preferences the user has declared. Written by L1. Immutable once the project starts — the founding reference that concept validation checks against. Anyone can read, nobody modifies.

### client-brief/priorities.md

The user's triage of what they care about. Contains: which areas the user wants deep collaborative definition on, which are delegated ("tech stack — your call"), and any priority overrides that should flow through the entire project ("I care about performance more than polish"). Written by L1. Immutable. These priorities override domain defaults at every level.

### `/runtime/` root + promotion-out-of-`/runtime/`

> **AMENDED 2026-06-12 (user ruling — workspaces out of the repo):** the per-build trees no
> longer nest inside the repo's gitignored `/runtime/`. They live in their own root,
> **`~/l1-l5-workspaces/<build-id>/`** (`commissioning.DEFAULT_WORKSPACES_ROOT`;
> `$HARNESS_WORKSPACES_ROOT` relocates the family, per-run `$HARNESS_RUNTIME_ROOT` still wins).
> **RE-AMENDED 2026-07-07 (user ruling — workspaces out of `~/Documents` too):** the root moved
> from `~/Documents/l1-l5-workspaces` to `~/l1-l5-workspaces`, because Claude Code auto-loads
> `CLAUDE.md` from every ancestor of a seat's cwd and `~/Documents` now carries an ambient one.
> Existing trees moved with it; a compat symlink remains at the old path so dated run notes
> still resolve.
> The repo is code + spec; the trees the harness builds are deployment state. Everything below
> about the tree's INTERNAL shape and the write-jail is unchanged — read `/runtime/` as "the
> workspace root". **Registered extension (same ruling, design pending):** L1 gains a
> project-workspace prerogative — at kickoff of a GREENFIELD project it provisions a fresh
> project workspace via a harness verb (the daemon owns the filesystem boundary, agents are the
> vehicle), and for CONTINUATION work it attaches to the existing one; today one daemon boot =
> one workspace = one genesis L1, and projects exist only as subtrees of the L1 node.

The logical `proj/{project}/...` tree shown above is rooted, at runtime, at the **gitignored throwaway `/runtime/`** (per-build project trees live under `/runtime/proj/{project}/`; see `INDEX.md`). Everything an agent produces — the whole project node and its deliverable — is written **inside `/runtime/`**, and every agent's write-jail confines it to its own node subtree there (`SECURITY.md` §1.3, §2.3: global `(deny file-write*)` then an allow-list scoped to `<WORKROOT>`).

A finished project's deliverable does not stay in `/runtime/`. After L1 writes a complete **preliminary fidelity playback** and its current durable question receives the owner-final CONFIRM (or a distinctly labelled, launch-predeclared commissioning-delegate CONFIRM), L1 deliberately invokes promotion and the **control plane (`harnessd`) promotes the deliverable OUT of `/runtime/` to the destination captured at intake** (a user-path or git remote, recorded as a field of the frozen intent-spec). Answering alone never promotes. This destination is **outside every node's write-jail**, so the cross-boundary write is a **control-plane operation, gated on the immutable playback answer plus the explicit node-bound delivery trigger — never a jailed-agent write**. The deliverable binding (`deliverable_state` + the dedicated `delivery_destination`/`delivery_kind`, the harnessd per-node binding block in `harnessd/IMPLEMENTATION-PLAN.md` §3.2) tracks it — the promote step sets `deliverable_state=delivered` (or `delivery-failed`) and records the target in `delivery_destination`, kept distinct from `write_targets` (the in-jail source surface). A failed promote records its failed target, and a later control-plane retry may supply an explicit replacement destination only while the binding is `delivery-failed`. The L1-authored `client-brief/` files (`vision.md`, `priorities.md` — views of the frozen intent-spec, written at project creation, immutable, above) are the brief side of the same arc; promotion is the delivery side. See `INTAKE-TO-DELIVERY.md` for the full intake→delivery arc.

### L2-config.md

The spawn artifact for L2. Contains: professional role identity for this project (e.g., "technical architect for a fintech app"), domain context, and user priorities inherited from the client brief. L1 creates this at project initiation. L1 invokes it to spawn L2. For new projects, L1 follows a skill/guide for creating a new L2 configuration.

### status.md

Central project status board. Hierarchical view of the full project: areas and workstreams with their current state. NOT task-level detail — for that, drill into L4's plan.md.

Updated by each level's lead as part of their normal approval workflow:
- **L2** updates area-level status lines (phase, area approval states)
- **L3** updates workstream-level status lines within their area
- **L4** updates their workstream's progress summary (e.g., "3/5 tasks complete")
- **L5** does not touch this file

Each level only touches their scope. The file gives L1 (and anyone) a single-glance view of where the entire project stands without traversing the tree.

Structure:
```
# Project Status — {project-name}
Phase: {current phase}
Last updated: {timestamp}

## {area-name} [L3: {status}]
Lead: {role identity}
Design: {approved/pending/sent-back}

  {workstream-name} [L4: {status}, {n/m tasks}]
  {workstream-name} [L4: {status}, {n/m tasks}]

## {area-name} [L3: {status}]
...
```

### log.md

Append-only structured project log. Two types of entries:

**State change entries** — appended by each level on every state transition:
```
[{ISO-timestamp}] [{level}] [{scope-path}] [{STATE}] [{optional notes}]
```

States: `STARTED`, `SUBMITTED`, `APPROVED`, `SENT-BACK`

Examples:
```
[2026-03-29T10:00] L5 data-pipeline/api/auth-endpoint SUBMITTED
[2026-03-29T10:30] L4 data-pipeline/api/auth-endpoint APPROVED
[2026-03-29T11:00] L4 data-pipeline/api SUBMITTED workstream complete
[2026-03-29T12:00] L3 data-pipeline/api APPROVED
[2026-03-29T14:00] L3 data-pipeline SUBMITTED area complete
[2026-03-29T15:00] L2 data-pipeline SENT-BACK interface contract mismatch with auth-system
```

**Narrative entries** — appended for significant events that aren't state changes:
```
[{ISO-timestamp}] [{level}] NOTE: {description}
```

The log provides the history view: what happened and when. status.md provides the snapshot view: where things are now. Together they give full state awareness.

### L2/project.md

Starts life as the concept design artifact (Phase 2 of the planning process). Contains: what is this system, how does it work, boundaries, key decisions with reasoning, major areas of work, interfaces between areas. Evolves into living project state as execution proceeds — status updates, course corrections, completion tracking added. L2 is sole owner. This is THE source of truth for the project.

### L3/{area}/design.md

The detailed area design artifact (Phase 4 of the planning process). Contains: area architecture, workstreams with scope and acceptance criteria, interface contracts (cross-area and internal), decisions at this level with reasoning, internal dependencies and sequencing. Submitted to L2 for cross-area coherence review. Once approved, becomes the north star for execution. L3 is owner; L2 has review/approval authority. Immutable after approval — amendments go through decision records.

### L3/{area}/plan.md

Living workstream status during execution phase. Created after design.md is approved. Active workstreams in full detail (description, status, assignee, blockers), completed workstreams collapsed to one-liners. This is L3's execution navigation layer.

### L3/{area}/L4/{workstream}/plan.md

Task decomposition within the workstream. Active tasks in detail, completed collapsed. L4's navigation layer.

### {task}/brief.md

The distilled task brief, written by L4 into the task node at spawn. **Pointer-not-payload:** it carries the spec, constraints, the interface contract the task must honor, and the relevant ADRs (the rationale bridge) — but *references* raw upstream intent rather than copying it (pullable on demand). Thin-but-decision-complete: enough that the worker never has to ask before starting. Immutable once sent. For a cross-runtime executor (e.g. a Codex/GPT-5.6 Sol L5), the semantic brief is identical; only a thin runtime adapter injects the tool manifest and harness envelope at spawn — see `runtime-and-model-map.md`.

### {task}/acceptance.md — frozen rubric (D26)

The acceptance/rubric artifact: a **dedicated, frozen, per-unit** file living *in the work node* alongside `brief.md` and `report.md` — never a section inside the brief, never reconstructed from the worker's output.

- **Authored before implementation, from frozen criteria and the task spec, by != the implementation worker.** The design cycle produces falsifiable acceptance criteria and rubrics. During the build cycle, L4 turns those criteria into executable L5 acceptance by spawning a dedicated L5 `test_author` child, whose package passes L5+ review before any implementation L5 opens. A future `#test` seat can replace that mechanism. This is the temporal anti-theater rule made physical: the work is anchored to a standard the worker cannot move, never to tests reverse-engineered from code.
- **Frozen and READ-ONLY to the executor.** L5 may *read* `acceptance.md` and must make it pass; L5 may not *edit* it. Immutability is the enforcement — a worker who cannot move the goalposts cannot launder failing work into a passing rubric. Edit policy is carried in the file's frontmatter and is infrastructure-enforced.
- **ID-tagged with a per-element trace-block.** Each acceptance test and rubric line carries its own machine-readable `trace:` stanza (`{ id, serves, kind: test, level, node }`) naming the requirement it verifies — *not* a whole-file `intent_ids:` header. These per-element stanzas are what the RTM-builder greps and joins (see Trace-Blocks Live in the Work-Node Artifacts, below, and `PLAN-ALIGNMENT-GATE.md`).
- **Read by two seats:** the executor (`#exec`, to satisfy it) and the independent reviewer (`#review`, the L5+ / right-arm reviewer, who checks the work *against this same frozen file* — not against the code).

**The same pattern recurses at every level.** Each delegating level, during its Plan phase, emits the acceptance criteria / gate rubric for the level below as a frozen artifact in the child's node *before that child executes* — the Plan-phase output contract (spec + acceptance criteria + gate rubric). At area/workstream nodes the artifact carries the level's gate rubric and criteria; at the leaf it carries L5's executable tests produced from those criteria by the M51 test-author path. See `QUALITY-GATE.md` for how the gate rubric is consumed.

### L3/{area}/L4/{workstream}/L5/{task}/report.md

L5's structured report. Pre-seeded template at spawn time. L5 fills it in during execution and submits when complete.

## Spawn Templates

Each spawn point has a template that defines the complete onboarding package for the spawned agent. Templates live in `spawn-templates/` and contain fixed parts (level docs to load, process, communication protocol) and variable parts (role identity, assignment, workspace path, context pointers) that the parent fills in.

| Template | Parent fills | Spawns |
|----------|-------------|--------|
| `L2-SPAWN.md` | L1 | L2 for a project |
| `L3-SPAWN.md` | L2 | L3 for an area |
| `L4-SPAWN.md` | L3 | L4 for a workstream |
| `L5-SPAWN.md` | L4 | L5 for a task |

Planning L3s use `L3-PLANNING.md` instead of `L3-SPAWN.md` — they are temporary spawns that produce a design and collapse.

The spawn template is a contract: if the parent fills in the variables correctly, the spawned agent has everything it needs to do its job without asking questions. If the agent needs to ask before starting, the template is incomplete.

## Document Types and Edit Policies

Each document carries editing rules in frontmatter (owner, edit_policy). The agent reads the file, sees the rules, knows what it can do.

| Document | Owner | Policy |
|----------|-------|--------|
| `client-brief/raw-request.md` | L1 | Exact start intake; frozen with intent confirmation; explicit revision only. |
| `client-brief/intent-spec.md` | L1 | Canonical frozen intent; explicit revision only. |
| `client-brief/vision.md` | L1 | Written at project creation. Immutable. |
| `client-brief/priorities.md` | L1 | Written at project creation. Immutable. |
| `L2-config.md` | L1 | Written at project creation. L1 may update for role/priority changes. |
| `status.md` | Shared | L2 updates area lines. L3 updates workstream lines. L4 updates progress summaries. Each level touches only its scope. |
| `project.md` | L2 | Owner-only. Starts as concept design, evolves to living state. |
| `conventions.md` | L2 | Owner-only |
| `design.md` | L3 (specific) | Owner creates. L2 reviews/approves. Immutable after approval (amendments via decision records). |
| `plan.md` (L3 level) | L3 (specific) | Owner: full edit. L2: override authority. |
| `plan.md` (L4 level) | L4 (specific) | Owner: full edit. L3: override authority. |
| `report.md` | L5 (specific) | Full edit until submitted, immutable after |
| `brief.md` / `briefs/` | Delegating level | Immutable once sent. Distilled, pointer-not-payload. |
| `acceptance.md` (per-unit rubric) | Delegating level / acceptance-authoring path | Written before executor admission from frozen criteria, **frozen**, **READ-ONLY to the executor**. Infrastructure-enforced. |
| `log.md` | -- | Append-only, structured entries, any level |
| `decisions/` | L2 | Immutable once written |
| `README.md` | L2 | Owner-only |

## Naming Conventions

- **Date-prefixed** for chronological files: `YYYY-MM-DD_subject.md` (briefs, reviews)
- **Numbered** for sequential files: `001_topic.md` (decision records)
- **Descriptive** for area/workstream/task folders: `{name}/` (matches the name in the parent's plan or project doc)

## Living Docs as Navigation Layer

No archive, no file moves, no index files. `plan.md`, `project.md`, and `design.md` serve as the navigation layer:
- Active items in full detail (description, status, path, assignee, blockers)
- Completed items collapsed to one-liners (name, date, outcome)
- Files stay where they are — paths stay stable, references don't break

This handles file accumulation without additional infrastructure. Scratch cleanup on task completion is infrastructure-handled.

## Historical `comms/` mailbox

**Superseded 2026-07-24.** Existing `comms/` directories may be read as migration evidence, but new
communication is written to sender-owned `messages/`; recipient inbox rows are derived delivery
pointers.

- The former bus carried a live pointer and `comms/` retained an audit copy.
- The former best-effort argument relied on ambient rereads after a missed nudge.
- Current sender-owned messages and verified retryable wakes replace both roles.

This keeps `comms/` outside git (infrastructure-managed, per `git-protocol.md`) and removes the old `unread/`→`read/` inbox mechanics from the critical path: state awareness comes from docs, not from inbox folder hygiene.

## Shutdown Handoff Protocol

Mandatory, infrastructure-enforced. Shutdown doesn't complete until handoff artifacts are written:
1. Update living docs (project.md, design.md, or plan.md) to reflect current state
2. Update status.md with current state of your scope
3. Append to project log what was done
4. Update README with anything the next instance needs to know

An agent that crashes without completing handoff is degraded but recoverable — living docs and log from before the crash still exist.

## Per-Level Workspace Needs

**L5 (Task Executor):** Receives a seeded task folder (`L3/{area}/L4/{workstream}/L5/{task}/`) containing `brief.md`, the frozen `acceptance.md` (read-only), and a pre-created `report.md` template. Reads brief, conventions, SWE handbook, and the acceptance tests it must make pass — but cannot edit `acceptance.md`. Works in scratch/ and task folder. Fills report.md. Appends to project log. Minimal document needs, fully templated. (On a cross-runtime seat — a Codex/GPT-5.6 Sol L5 — only the runtime adapter differs; the seeded artifacts are identical. See `runtime-and-model-map.md`.)

**L4 (Workstream Coordinator):** Owns `L3/{area}/L4/{workstream}/`. Creates task folders for L5s at spawn time, seeding each with `brief.md` and the frozen `acceptance.md`. The design/validated plan supplies frozen criteria; the current runtime path turns those criteria into executable leaf acceptance through a normal L5 test-author child plus L5+ package review before implementation L5 opens. Writes reviews. Maintains plan.md. Appends to project log. Escalates to L3 over the bus when needed (durable copy lands in L3's mailbox).

**L3 (Module Designer):** Owns `L3/{area}/`. Spawned by L2 with a specific professional role identity and two inputs: the full concept (`L2/project.md`, for context) and the area assignment (`L2/briefs/`, for scope). Two phases:
- *Design phase:* Produces design.md, submits to L2 for cross-area coherence review.
- *Execution phase:* Creates L4 workstream folders, writes briefs, reviews L4 reports, maintains plan.md.

Appends to project log. Escalates to L2 over the bus when needed (durable copy lands in L2's mailbox).

**L2 (Project Architect):** Owns `L2/`, project-level README.md, conventions.md. Creates L3 area folders at spawn time. Produces concept design in project.md. Writes decision records. Reviews L3 designs for cross-area coherence. Reads L3 design.md and plan.md for execution status. Appends to project log. Reports to L1 over the bus; the durable copy lands in L1's `comms/` mailbox.

**L1 (System Orchestrator):** Portfolio-level workspace. See dedicated section below.

## Per-Node File Schemas (Doc-System extension, 2026-06-12)

What files EVERY node of a level contains — the workspace philosophy above extended down to the
single node, so that a stateless successor spawned cold at any node of its level knows where
everything is without exploring, and a parent (or auditor) can read any child node without
per-node archaeology. Grounded empirically in the Run-2 trees
(`~/l1-l5-workspaces/build-site1/nodes/` — pre-2026-07-07 path was `~/Documents/l1-l5-workspaces/…`, still resolving via compat symlink), where sibling nodes at the same level
disagreed by accident: `assembly/ws-b` lacked the `status.md` + `composition-report.md` its
siblings ws-a/ws-c carried; the `assembly` L3 node lacked the `log.md` and `reviews/` the
`markdown` L3 node kept; one run named its review seats `review/`, `builder-review/`, and
`coder-review/`. None of those differences were decisions.

> **ENFORCEMENT BOUNDARY (user ruling — do not move it):** schema conformance is checked
> **EVAL-SIDE only**, by the run-adherence audit instrument — **NEVER as a runtime gate.** The E2
> runtime floor stays exactly the three deterministic checks in `harnessd/return_contract.py`
> (non-empty `report.md` at DONE; given requirement IDs cited for L5-class seats; trace stanzas
> valid where present). Code layout, scratch space, and creative artifacts keep their freedom —
> the schema names the **management skeleton** of a node, not its work product.

**Common to every node (any level, any seat)** — the management skeleton:

| File | Origin | Policy |
|---|---|---|
| `brief.md` | parent-authored before spawn | immutable once sent (pointer-not-payload) |
| `acceptance.md` | delegating level / acceptance-authoring path, where the node is gated | frozen, read-only to the executor (D26) |
| `plan.md` | the node after native task-list creation and bounded orientation (the `plan-first` block) | durable mirror of the runtime task list; goal line + checklist; final three items fixed |
| `report.md` | the node, at terminal sign-off | per `operational/shared/templates/report-template.md`; required at DONE (E2) |
| `reviews/<gate-id>/candidate-artifacts.json` | harness at candidate submission | byte-exact manifest of producer-owned candidate artifacts, source paths, snapshot paths, bytes, and hashes; review verdicts route only while current artifacts and snapshot copies match it |
| `reviews/<gate-id>/candidate-snapshot/` | harness at candidate submission | immutable copies of the submitted candidate artifact bytes for parent/audit proof after the producer workspace is accepted and may be rewritten for the next phase |
| `reviews/<gate-id>/review-packet.md` | harness at candidate submission | pointer packet for the review gate; includes manifest identity, evidence pointers, and review-output paths |
| `reviews/<gate-id>/product-probe-roster.<sha256>.json` | harness before L2+ FULL probe dispatch | read-only content-addressed join of exact live frozen requirement text, confirmed intent journeys, Q3 MNF failure-path IDs, artifact-declared invocations, intent/coverage receipts, and candidate identity |
| `reviews/<gate-id>/reviewers/<user-simulation\|performance-robustness>/disposable-instance/` | harness before L2+ FULL probe dispatch | one writable manifest-verified candidate copy per probe; never shared with the other probe and never the live producer tree |
| `reviews/<gate-id>/probe-instance-manifests/<probe>.json` | harness before L2+ FULL probe dispatch | read-only identity and byte-hash record for the corresponding disposable instance |
| `.sign-off.<seat>.json` / `.signal.<seat>.json` / `.inbox.<seat>.jsonl` | harness | machinery, not documentation |

During the L2 design→build transition, the project node also carries:

| File | Origin | Policy |
|---|---|---|
| `plan/validated-plan-package.md` | L2 | human entrypoint to the candidate assembled plan |
| `plan/plan-alignment-coverage.json` | L2 | minimal machine index only: each artifact row is `{path, trace_ids}`; each MNF has a failure-path mapping to an in-package `kind: test`; never restates requirement tags/MNF flags or claims PASS |
| `plan/plan-alignment-semantic.json` | L2 | physical, disjoint verification-artifact/construction-module partition; every trace-bearing Q3 artifact belongs to one window |
| `plan-alignment-ready.json` | L2 | nonterminal marker pointing to the package and both manifests |
| `plan/plan-alignment-coverage.<bundle-sha256>.json` | harness | generated, read-only deterministic report over the current intent receipt and exact submitted bundle; lists deferred exemptions, `DD-` exclusions, and typed defects |
| `plan-alignment/<cell-prefix>/control/{atomization-input,element-module-index,cell}.json` | harness | read-only cell inputs, full durable trace graph, incremental scope, and exact identities |
| `plan-alignment/<cell-prefix>/evidence-index.json` | harness | read-only joined semantic reports plus machine-derived required elevations |
| `plan-alignment/<cell-prefix>/elevations/<finding-fingerprint>.json` | L1 | one current finding, disposition, and confirmable owner question |

The content-addressed report is not a second maintained RTM. The sidecar is not trusted as coverage:
every declared trace ID must exist in the exact named artifact, and the harness computes all graph
reachability. A deterministic refusal returns the report to L2 through marker-invalid feedback and
does not wake L1.

When a terminal node is intentionally re-opened as a same-address fresh incarnation, the address
stays stable but the active work surface is renewed. Prior incarnation `plan.md`, producer report
artifacts, and seat transport files move under `<runtime>/.harnessd/incarnation-archives/` as
control-plane provenance outside the agent workspace, then fresh current forms are instantiated. The
archive directory includes an `incarnation-archive.json` mapping original relative paths to archive
paths and hashes. Gate directories under
`reviews/<gate-id>/` stay in place because parent reports, review packets, and audit tooling may
still point at those exact paths.

**Per-level, beyond the skeleton** (required = the audit flags its absence; optional = legitimate
when the node's shape needs it, absence is not a defect):

| Level | Required | Optional / when-needed |
|---|---|---|
| **L1 (root)** | `portfolio.md`, `README.md`, `log.md`, `decisions/` | `threads/`, `notes/`, `.intake-scratch/`; project child nodes as subdirs |
| **L2 (project)** | `status.md`, `log.md`, `conventions.md`, `README.md`, `client-brief/` (L1-authored: `raw-request.md`, `vision.md`, `priorities.md`, `intent-spec.md`; `fidelity-judgment.md` at L1 close), `L2/` inner workspace (`project.md`, `decisions/`, `briefs/`, planning outputs), `composition-review.md` at its gate | `L2-config.md` (L1-authored spawn artifact); area child nodes as subdirs |
| **L3 (area)** | `design.md` (frozen north star), `log.md` | `status.md`, `reviews/`, `briefs/`; L4 child nodes as subdirs. (A planning-L3 produces the area design in L2's planning workspace and collapses — the skeleton minus `acceptance.md` applies to its seat node.) |
| **L4 (workstream)** | `status.md`, `composition-report.md` at its gate, `log.md` | `reviews/`; child seat dirs (next row) |
| **L4 child seats (naming convention)** | `tester/` (the M51 lateral); `{task}/` (the L5 executor, named for the task); `{task}-review/` (its L5+ reviewer) | — Run-2's `review`/`builder-review`/`coder-review` spread is exactly what this fixes |
| **L5 (task)** | the work artifacts themselves | `scratch/` (infrastructure-cleaned) |
| **L5+ (review seat)** | `report.md` carries the per-criterion **verdict table — it IS the gate artifact**, no separate `verdict.md` (the verdict is restated in the terminal signal's `evidence.notes`) | — |

**Resolved log/status tension.** The old Log Mechanics wording proposed one ancestor-owned project
board, but the write jail correctly makes each child write only its own per-node `log.md` /
`status.md`. A 2026-06-12 implementation reconciled that tension with daemon-maintained
`log.aggregate.md` / `status.aggregate.md`; r6 proved that a daemon-written aggregate can become a
second artifact truth and participate in a gate livelock. The 2026-07-24 ruling supersedes the
relay: per-node files remain the only agent-written form, and `harnessctl view` / `journey`
generate the whole-build ledger/artifact join only when read.

An optional HTML capture lives at `<runtime-root>/.harnessd/views/journey.html`. That harness
control directory is outside `nodes/`: no seat (including L1 god-view), candidate manifest,
return-contract walk, or gate can read or adopt it as product evidence. Historical aggregate files
are left untouched for postmortem provenance but are neither current schema nor renderer input.

See `design/DOC-SYSTEM.md` for the block/template mechanism that delivers these duties into the
role docs, and the critical constraint on what mechanical checks can and cannot certify.

## L1 Portfolio Workspace

L1's workspace is fundamentally different from L2-L5. Lower levels produce work products in structured folders. L1 produces decisions and direction, and mostly consumes information from below. Its primary input is unstructured user conversation across any number of topics; its primary output is structured delegation.

### Workspace Structure

```
L1/
  portfolio.md              # Living portfolio state — L1's source of truth
  README.md                 # L1 onboarding (any instance can boot from this)
  log.md                    # Append-only portfolio log
  decisions/                # Portfolio-level decisions (numbered, immutable)
  comms/                    # Durable mailbox / audit copy of L2 messages (live nudges arrive on the bus)
  threads/                  # Open conversation threads with user (one file per topic)
  notes/                    # Structured session captures (date-prefixed, immutable)
```

This sits alongside project workspaces — L1 is a peer, not a container:

```
workspace/
  L1/                       # Portfolio-level (above)
  projects/
    {project-name}/          # Per-project (existing schema)
```

### L1 Documents

| Document | Purpose | Policy |
|----------|---------|--------|
| `portfolio.md` | Projects, priorities, open items, pointers | L1 owner, updated in real time |
| `README.md` | Onboarding for any new L1 instance | L1 owner |
| `log.md` | Append-only portfolio log | Append-only |
| `decisions/` | Portfolio-level decisions (priority changes, project creation/pausing, resource allocation) | Numbered, immutable once written |
| `threads/{topic}.md` | Living doc per ongoing conversation thread with user | L1 owner, timestamped entries |
| `notes/{date}_{topic}.md` | Structured extracts from user sessions | Immutable after capture |
| `comms/` | Durable mailbox / audit copy of inbound messages | Append-only record; the live channel is the bus (see below) |

### portfolio.md

L1's equivalent of L2's `project.md`. Everything a cold-started L1 needs to orient. Thin and navigational — does not duplicate project state.

Contents:
- **Projects table** — name, status, current focus, L2 config path, workspace path
- **Priorities** — current ordering with reasoning (not just "what" but "why")
- **Open items** — pending actions, items waiting on user, things to follow up
- **Active threads** — pointers to `threads/` with one-line summaries
- **Pointers** — user profile, status board, resource config paths

### threads/

Living docs per ongoing topic with the user. Each thread has enough context that a cold-started L1 can resume the conversation. Threads have explicit states: open, parked (with revisit-by date), closed (collapsed to a one-liner in portfolio.md). Each entry is timestamped.

### notes/

Structured captures from user conversations. Date-prefixed, immutable after capture. These are the output of the documentation-capture process — discrete items extracted, classified, and recorded. Not transcripts — structured extractions.

### What L1 Reads (Produced by Others)

- **L2's `project.md`** — drill-down when L1 needs project detail. L1 reads the top for status, doesn't need a separate compressed report.
- **Status board** (`status/`) — infrastructure-managed, shows active agents and states across all projects
- **User profile** — preferences, communication style, context
- **L2 messages** — L2 reports nudge L1 live over the bus and land a durable copy in L1's `comms/` mailbox; the truth they point at lives in `project.md`

### What L1 Writes

- **portfolio.md** — updated continuously during conversation, not just at shutdown
- **threads/** — created and updated as conversation topics emerge
- **notes/** — session captures, produced by documentation-capture process
- **decisions/** — portfolio-level decisions, numbered and immutable
- **log.md** — append-only, what happened at portfolio level

### L2 Configuration Artifacts

Each project has a predefined L2 configuration — the artifact that defines how to invoke an L2 for that project. Includes: the professional role identity L2 should be spawned with (e.g., "technical architect for a fintech app"), domain context, project conventions, and any user-declared priorities that override domain defaults. Lives in the project workspace (property of the project, not of L1):

```
projects/{name}/L2-config.md
```

L1 invokes these, doesn't build them from scratch in normal operation. For new projects, L1 follows a skill/guide for creating a new L2 configuration.

### Real-Time Note-Taking Discipline

L1's primary challenge: it receives continuous, unstructured user conversation with no session boundaries. Compacts can fire at any time and destroy context. Anything not written to disk is lost.

L1 does not batch note-taking for "later" — there is no later. The discipline is: **after any exchange that produces something worth keeping, write it down before moving on.** This is a core behavioral requirement, specified in L1's soul doc.

Three mechanisms ensure nothing is lost, ordered by intensity:

1. **Real-time writing** — L1's continuous behavioral discipline. After any decision, action item, priority shift, new idea, or significant user statement, L1 updates the relevant file (thread, portfolio.md, or creates a note). This is the primary mechanism.

2. **Periodic refresh** — every ~5-10 user turns, infrastructure triggers a lightweight documentation-capture pass. A background agent scans recent conversation, checks: did L1 miss anything? Any undocumented decisions, unrecorded action items, new topics without threads? Catches what real-time discipline missed.

3. **Pre-compact capture** — before compact fires (automatic every ~30 turns, or user-triggered), a full documentation-capture sweep runs. Scans everything in current context, extracts all undocumented items, writes to files, reconciles living docs with current state. Only after this completes does compact proceed. This is the hard guarantee — nothing survives in context that isn't on disk.

### Boot Reconciliation

Every new L1 instance follows a boot sequence before engaging with the user:

1. Read `README.md` — orient to portfolio structure
2. Read `portfolio.md` — current state, priorities, open items
3. Scan `threads/` — flag anything with last-update beyond staleness threshold
4. Read `comms/` — process the durable mailbox copy of L2 reports/escalations that arrived while no instance was live
5. Reconcile — update portfolio.md if anything changed from steps 3-4
6. Ready for user

Infrastructure-enforced, not optional. Ensures staleness is caught every time a new instance boots.

### Staleness Protection

All entries in threads and portfolio.md carry timestamps (infrastructure-applied, not L1-managed). Staleness is addressed at three points:

- **Per-turn** — real-time updates keep active threads current
- **Periodic** — refresh pass every 5-10 turns flags drift
- **Boot** — reconciliation scan on every new instance

Threads untouched beyond a configurable threshold are flagged. L1 reviews flagged threads and either updates, parks (with revisit-by date), or closes them (collapses to one-liner in portfolio.md).

## Log Mechanics

Single log at project level only (no area-level or workstream-level logs — plan.md already tracks status at each level). Append-only with an append queue mechanism rather than file locks, since appends don't conflict with each other. Multiple agents can submit entries concurrently; entries are appended in order.

---

*Created: 2026-03-17*
*Updated: 2026-03-29 — Restructured for 5-level hierarchy with nested workspace tree*
*Updated: 2026-06-02 — Added the one hierarchical-path spine (F35/D26 unification); the Visibility & Read-Permission Graph (F34, supersedes broad project-wide read); per-unit frozen read-only `acceptance.md` rubric (D26); reframed `comms/` as a durable mailbox/audit copy with the bus as live transport (F33).*
*Updated: 2026-06-12 — Per-Node File Schemas section (Doc-System extension; eval-side only, never a runtime gate; Run-2 empirical grounding; recorded the shared-log/status vs write-jail tension). See `design/DOC-SYSTEM.md`.*
*Updated: 2026-06-18 — Added the agent-first constraint for the logical-project-document substrate: lineage/freshness metadata is control-plane and observability machinery, while ordinary agents keep task-shaped local context. Earlier 2026-06-17 update added the logical-project-document model: physically distributed owned slices, hierarchical composed views, owner-scoped mutation, and explicit freshness/supersession when parent slices change after child gate PASS.*
*Status: Extracted from NOTES.md and promoted to standalone process design document; consolidated against `working-notes/consolidation-plan-2026-06-02.md`*
