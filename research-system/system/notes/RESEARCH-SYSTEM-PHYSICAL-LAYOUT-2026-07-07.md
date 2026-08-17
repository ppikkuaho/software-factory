# Hypothesis Tree Research System — Physical Layout (Design Area A2)

> **Status:** Working note; design converged 2026-07-07.
> **Parent documents:** `RESEARCH-SYSTEM-CONCEPT-2026-07-07.md` (ratified concept)
> and `RESEARCH-SYSTEM-TREE-SCHEMA-2026-07-07.md` (A1 schemas, v2). Same directory.
> **Scope:** item 9 of the concept §13 queue — repos, directories, git mechanics,
> and the first concrete D7 enforcement machinery. Also settles cross-tree
> (multi-component) v1 rules surfaced by DP-A2-2.

Tags: ✅ committed · 🟡 committed concept, mechanics open · 🔲 open.

---

## 1. The sidecar principle ✅

**Research state never lives inside the subject repo.** If tree/ledger state were
committed into the harness repo, every node branch would fork a divergent copy of
the research state — the map would fork with its territory. Therefore:

- **Subject repo** (the L1-L5 harness — the *private ops* repo, where the harness
  is run and developed): touched only by units, only on `ht/<node-id>-<slug>`
  branches, via per-dispatch worktrees. Trunk moves only through the merge gate.
  (The public harness repo is downstream and out of scope here.)
- **Research root** — `research-system/` (DP-A2-1) — the public sibling
  directory in this repository (DP-A2-3): all system state. Single canonical store; never
  forks; its commit history provides write-audit and the growth-replay substrate
  for free.

## 2. Layout ✅

```
research-system/                         # research root — public sibling directory
├─ system/                                # methodology lane
│  ├─ notes/                              # concept + design notes (move here when doc overhaul settles)
│  ├─ schemas/*.json                      # validation schemas — D7 machinery
│  └─ instruments/                        # registry: suites, rubrics, noise floors, versions
├─ trees/<component>/                     # e.g. trees/L4/
│  ├─ tree.json                           # epoch, cursor, decision_log, learnings, watch_queue
│  ├─ index.json                          # canonical full index, depth-ordered
│  ├─ index.live.json                     # live view — mechanically derived, canonical
│  ├─ nodes/<id>/
│  │  ├─ node.json                        # nav + epistemic domains (A1 §2)
│  │  ├─ dispatches/d-<id>-N.json
│  │  ├─ reports/d-<id>-N-report.md
│  │  └─ archive/                         # transcripts, raw logs — GIT-IGNORED, write-once via tool
│  └─ views/                              # generated: index.md, tree.svg — uncitable
├─ ledger/                                # ONE shared ledger (DP-A2-2): user/ research/ observatory/
│                                         # entries carry optional component tag
├─ readout/
│  ├─ statistics.json                     # computed aggregates
│  └─ INTERPRETATION.md                   # co-located interpretation rules (user ruling)
└─ worktrees/                             # transient unit workspaces (subject-repo worktrees)
```

Serialization (closes A1 §11.1): JSON state files as shown — one `node.json` per
node, one `tree.json` per tree; reports as Markdown; archives as plain files with
path+span addressing for claim anchors. Markdown views generated, uncitable.

## 3. Git mechanics in the subject repo ✅

- **Epoch = tagged merge commit on trunk** (`epoch-8`). Epoch history is auditable
  in git; `tree.json.epoch_history` mirrors the tags. Frozen trunk = nothing
  reaches main except the merge gate.
- Node branches fork from trunk head **at first dispatch** (A1 §2). **Revive** =
  new branch from current trunk (`ht/2.2-e9`); the old branch is preserved; fork
  history in `node.json` matches branch names; nothing is rewritten.
- Units receive a worktree checked out on their node's branch — that worktree is
  their entire subject-repo world.

## 4. D7 enforcement — first machinery ✅

Two layers, defense in depth:

1. **The `ht` write tool** — the only API for state mutation (`ht claim grant`,
   `ht cursor swap`, `ht ledger mint`, …). Validates against `system/schemas/`,
   checks the A1 §10 authority table per field, executes derived writes (live-view
   regeneration, watch-row creation at merge, trigger/write-split executions), and
   commits with role-stamped metadata. Roles never edit state JSON by hand.
2. **Pre-commit hook in the research root** as backstop: schema-validates changed
   state files; rejects commits whose role stamp doesn't own the touched fields.

Unit isolation is a filesystem fact: a unit's writable surface is exactly
`nodes/<id>/reports/ + archive/` plus its worktree. It physically cannot reach
`tree.json`, the index, or the ledger.

**Role identity (DP-A2-4) ✅:** launch-time env/flag (`HT_ROLE=verifier`) —
honor-system at launch, hard per-field enforcement once set. Deliberate v1
softness, recorded as such (agents are unlikely to violate launch identity; the
per-field gate catches drift). Credential separation is a possible hardening,
not planned.

## 5. Multi-component / cross-tree rules ✅ (v1, from DP-A2-2)

Concurrent work on multiple component trees (e.g. L2 + L4) is governed by:

1. **Global trunk freeze.** One trunk, one epoch counter, one merge gate — ever.
   Exploration in parallel across trees is measurement-safe under the frozen
   trunk (the trunk-freezing vs. attention-integrity separability, one level up).
2. **Merges serialize globally.** Rare events; no concurrency needed.
3. **Modularity is a hypothesis, not an assumption.** Whether an L2 merge
   invalidates L4's in-flight measurements depends on real coupling. V1
   conservative default: **every merge flags all other trees' open measured
   branches for a staleness assessment** (cheap — epoch stamps exist; it's a
   comparison, not remeasurement). Each assessment is evidence about
   cross-component coupling; accumulating "unaffected" results builds toward a
   pattern claim that licenses relaxing the default. The system measures its
   subject's modularity as a byproduct.
4. **One shared ledger** with optional component tags — cross-tree convergence is
   exactly the signal per-tree ledgers would destroy.

Cursor capacity across trees remains the concept §3.4 question (one system-wide
cursor); raising capacity is a scheduler change (A1 §5 cursor is already a list).

## 6. L1-L5 evidence-plane alignment 🔲 (parked by user, 2026-07-07)

An external review of the L1-L5 architecture identified an unnamed "evidence
plane" (evidence index, score packets, behavioral views, run bundles,
reasoning-summary capture) grown bottom-up, and proposed naming it as the third
architectural plane: control plane (daemon) / work plane (nodes, artifacts) /
evidence plane (passive observation → scoring → doctrine flowback).

Mapping to this system: **observatory + trace reader + claims/readout machinery =
the evidence plane's implementation**; director/tree = research control sitting on
that plane; units = work plane. If the plane is named with a one-page contract in
ARCHITECTURE.md, this system becomes its first implementation rather than a
parallel structure — and the trace reader (recommended first build) doubles as the
plane's founding instrument. **To be handled separately** — recorded here so it
isn't lost; do not design against it yet.

## 7. Open within this area 🔲

1. `ht` tool command surface + hook implementation (Phase B, with the verifier
   protocol — the tool is where most gate logic lives).
2. Live-view regeneration mechanics (tool-internal; deterministic from status).
3. Archive write-once enforcement mechanism (tool-level; archives are outside git).
4. Worktree lifecycle (creation at dispatch, cleanup policy).
5. Backup story for git-ignored archives.

## 8. Decision log

| Ruling | Decision | Date |
|---|---|---|
| DP-A2-1 | Research root = `research-system/`, public sibling directory to the factory | 2026-07-07 |
| DP-A2-2 | One shared ledger + component tags; global trunk freeze; merges serialize; modularity treated as testable hypothesis with conservative staleness-flag default | 2026-07-07 |
| DP-A2-3 | Third private repo for research; archives git-ignored, write-once via tooling | 2026-07-07 |
| DP-A2-4 | Role identity soft at launch (env/flag), hard per-field in tool + hook; recorded as deliberate v1 softness | 2026-07-07 |
