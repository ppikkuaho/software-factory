# Git Protocol — Operational Reference

How agents use git. Loaded at boot for levels that own code movement and gate evidence: L5 task
implementation, L4 workstream integration, L3 area merge gates, and L5+ review evidence.

Git is one of the strands of the **single hierarchical-path spine**: requirement-IDs, agent-addresses, workspace-paths, logical git branches, rubric-file locations, and the visibility graph are all the same dotted/path scheme. A logical branch name *is* a workspace path *is* an address. See `WORKSPACE-SCHEMA.md` (the tree), `runtime-and-model-map.md` (F35 addressing), and `COMMUNICATION.md` (the visibility graph).

---

## Branch Strategy

The branch structure mirrors the management hierarchy and is **isomorphic to the workspace path / station address (F35)**. A logical branch name is a node path with no level numbers in it — semantic segments only (area names, not `L3.1`):

- **Task branch** — L5 works here. The leaf. Path: `proj/{area}/{workstream}/{task}` (e.g. `proj/payments/gateway/stripe-client`).
- **Workstream branch** — L4 owns this. Path: `proj/{area}/{workstream}` (e.g. `proj/payments/gateway`).
- **Area branch** — L3 owns this; it is the **merge gate** into project trunk. Path: `proj/{area}` (e.g. `proj/payments`).
- **Main / trunk** — `proj` — project source of truth. Only receives merges that have cleared the area-level review gate.

So the mapping is: **task = L5, workstream = L4, area merge-gate = L3, trunk = the project.** A child branch is its parent branch dotted with one more semantic segment, exactly as a child workspace path is its parent path plus a segment and a child requirement-ID is its parent ID plus a local index. Truncate the last segment and you have the parent branch / parent address / parent node. This isomorphism is not cosmetic — it is what lets the visibility graph, the RTM trace, and the merge topology all derive from one scheme decided once.

**Substrate first.** The substrate (Money, IDs, events, audit, the idempotency primitive, the base data model — B14) is built before the feature areas, on its own area branch under `proj/substrate`, via the walking-skeleton spike. Feature-area branches fork *after* the substrate has merged to trunk, so every area builds on the stable core rather than racing it.

**Stations and branches are the same node.** A role-variant suffix (`#exec`, `#review`) addresses *who is acting on* a node, not a separate branch — the executor seat and the review seat both operate against the same task/workstream/area branch. The branch carries the code; the `#`-suffix carries the seat (see `runtime-and-model-map.md`).

**Concrete Git ref codec.** The logical branch path is the node path above, but the concrete Git ref
adds a terminal `__self__` segment: `proj/payments/gateway/__self__`,
`proj/payments/gateway/stripe-client/__self__`, and so on. Git cannot store both a branch named
`proj/payments/gateway` and a branch named `proj/payments/gateway/stripe-client` because one ref
would need to be both a file and a directory. The `__self__` sentinel preserves the one-spine prefix
while making parent and child branches coexist as real Git refs. Agents, reports, review packets,
and merge commands use the logical path; the sentinel is substrate bookkeeping, not part of the
identity or requirement trace.

## How You Work With Git

**L5 (Task Executor) — executor seat (`…#exec`):**
- Work on your task branch (`proj/{area}/{workstream}/{task}`).
- Commit frequently as you go — small, meaningful commits.
- Run the pre-written acceptance tests (the frozen `acceptance.md` rubric in your work node — read-only to you, D26), your own unit tests, and **CI — the automated floor — before reporting** (D28). A red CI floor means the task is not done; do not report green.
- When the task is complete, write `report.md` and signal L4 over the bus. You do **not** open PRs or merge, and you do **not** review your own work — the review path handles that (D23: the producing seat never signs off on itself).

**L5+ (review seat, `…#review`) — code review is L5-class work:**
- A *separate* agent (different seat, ideally a different runtime/model for judgment diversity — Opus for the review read; see `runtime-and-model-map.md`) does its own testing and reviews the L5 code **against the frozen `acceptance.md` and the spec** — never against the code-as-written.
- Review is **at altitude** (D24): judge fidelity to spec and the quality of *this* unit's composition; do not re-derive or re-decide the level below.
- **Accept** → the durable PASS fires the sanctioned automatic merge consequence and both L5 seats collapse. **Bounce** → typed, neutral findings return to L5, which keeps its context and continues. The bounce loop is **bounded** (loop-cap N; persistent failure escalates, it does not spin), and every bounce remains a loud LOOK-HERE audit signal even after a later PASS.

**L4 (Workstream Coordinator):**
- Create task branches for L5s at spawn (fork from the workstream branch).
- L4 ensured the frozen `acceptance.md` for each task was authored before implementation L5 coded, through the normal M51 test-author path from frozen criteria — acceptance author != implementation producer (D26 / anti-theater temporal rule).
- After a task clears L5+ review, its durable PASS automatically runs the sanctioned merge from the task branch into the workstream branch. Read the parent `gate_passed` pointer's `merge_outcome`; never assume branch movement from review PASS alone. `harnessctl merge` is repair-only when that outcome is `failed`.
- When the workstream is complete, signal L3.

**L3 (Module Designer) — owns the area merge gate:**
- Create workstream branches for L4s at spawn (fork from the area branch).
- After a workstream clears review, the area-level PASS automatically runs the sanctioned merge into the area branch.
- The **area branch → trunk merge is the project's hard gate**: an area merges to `proj` only after the area-level independent review signs off; that PASS automatically runs the same sanctioned merge wrapper. L3 resolves cross-workstream merge conflicts through the repair path; cross-area conflicts escalate to L2.

## Independent Review Is the Default Merge Path (V1)

Independent review at each level is **in for V1** (D30) — it is the *normal* merge path at every level boundary, not a post-V1 toggle. There is no "direct merge" mode that bypasses it.

- Review is a **per-level function, not a standing coordinator parallel to the level**: an independent `#review` seat co-located at each node (P4: independent of the producing hierarchy), spun up against the same branch the producer acts on. Code review at each boundary is **L5-class work** — independent reviewer seats, clean context, judging at altitude (D23/D24). See `QUALITY-GATE.md` for the dimension presets and the per-level gate mechanics.
- **CI is the automated floor *beneath* the independent review function, not a substitute for it** (D28). CI must be green before a unit is eligible to enter the gate; the independent reviewer then does the judgment work CI cannot (architectural fit, interface-contract fidelity, drift against spec). Green CI alone never authorizes a merge.
- The two review surfaces are distinct and both run in V1: **CI floor** (automated, deterministic, per-commit) and the **independent review gate** (judgment, at the level boundary, before merge).

This is the merge topology, end to end:

> L5 commits → CI floor green + L5+ accept → L4 routes task through review gate → merge to workstream branch → workstream review gate → merge to area branch → **area review gate → merge to trunk.**

On every durable `gate_passed`, the daemon automatically invokes the sanctioned merge runner. It
derives the repository top level from the producer workspace and authorizes it only when it lies
inside the current runtime's `nodes/` tree. A workspace outside a Git worktree is
`not_applicable`; a discovered enclosing repository outside the runtime is also not applicable and
is journaled as a loud anomaly so the daemon can never merge an unrelated checkout. The parent
`gate_passed` pointer always carries `merge_outcome=merged|not_applicable|failed`; a failed outcome
also carries the exact repair command, and the parent must not compose under an assumed merge.

The explicit sanctioned operation
`harnessctl merge <source-node-address> --repo <repo-path> --requested-by <parent-node-address>`
is repair-only. Repair or commissioning may omit `--requested-by`, which records the requester as
`operator`. The runner derives the logical source branch from the source node address, derives the target from
the source path's structural parent unless `--target-branch` is supplied, verifies the binding's
`parent_address` maps to that same structural parent, verifies the source node is `done` with
`gate_state=gate_passed` and a gate proof pointer, maps logical branches to concrete `__self__` Git
refs, performs `git merge --no-ff`, and journals `git_merged` with the requester and accepted gate
proof. Replay first checks whether the source is already an ancestor of the target, making a
re-fired wrapper a no-op success rather than a duplicate merge or duplicate movement row. If Git
reports a conflict, the daemon returns a typed merge refusal after `git merge --abort`
restores the target checkout for parent-owned conflict handling. A raw agent-authored `git merge` is
outside the sanctioned path because it cannot prove review-gate ownership.

The plan-alignment gate (`PLAN-ALIGNMENT-GATE.md`) is a *different* checkpoint — it sits once, between the design cycle and the build cycle, and authorizes construction at all. These per-boundary merge gates run *during* the build cycle, on code. Do not conflate them.

Harness enforcement status: the merge permission is now a mechanical control-plane guard on the
real merge runner and CLI/IPC command. Before a source branch can move into its parent branch,
`harnessd.merge_gate.merge_branch` must see the source binding finalized as `state=done`,
`gate_state=gate_passed`, and carrying the `gate_id` that names the accepted gate packet. A producer
`DONE`, green CI, or an accepted-looking report is not sufficient. The runner also refuses
non-parent target branches so a task cannot skip the workstream/area spine, refuses a binding whose
recorded parent is not the source path's parent, and maps logical source/target paths to the concrete
`__self__` Git refs only at the Git command boundary. Each `git_merged` journal row records
`merge_requested_by`, normally the parent node that owns movement into its branch; `operator` means a
manual control-plane repair or commissioning action. A non-operator requester must map to the same
parent branch as the merge target; otherwise the merge is refused before Git is touched.

## Commit Messages

```
[{node-path}] {what was done}

{why, if not obvious from the what}

Seat: {address}#{exec|review}   e.g. proj/payments/gateway/stripe-client#exec
Serves: {requirement-ID(s)}     e.g. R-003.2.1
```

Keep the first line under 72 characters. The body is optional but useful for decisions or non-obvious changes. `{node-path}` is the branch/address; the `Serves:` line feeds the RTM trace (`PLAN-ALIGNMENT-GATE.md`) — a commit names the requirement ID(s) it advances so code is traceable to intent.

## Merge Conflict Protocol

Conflicts are resolved by the level that owns the **target** branch — i.e. the parent node, since the target branch is always the conflict point's parent path:

- **L5** resolves conflicts on its task branch (against the workstream branch it will merge into).
- **L4** resolves conflicts on the workstream branch (when merging multiple L5 task branches that touch overlapping files).
- **L3** resolves cross-workstream conflicts — two workstream branches conflicting on merge to the area branch. L3 has the area context to decide which approach wins. If the conflict touches another area's work, escalate to L2.

The owner of the target path owns the conflict, at every level — same rule as the visibility graph (a node sees its subtree, siblings, and parent; conflicts live at the parent).
When `harnessctl merge` encounters a conflict, it leaves the target checkout clean after aborting the
failed merge attempt. The owning parent resolves the conflict through a new child repair/rework
cycle or its own target-branch commit, then retries the sanctioned merge.

## What Stays Outside Git

- `status.md` — high-frequency updates, not versioned.
- **control-plane communication state** — canonical message/question records are durable but remain
  outside product git, as do derived inbox wake pointers, hook turn events, receipts, barriers, and
  signals. The product/evidence artifact a message points at follows its own versioning policy.
- `status/` board — infrastructure-managed.
- `scratch/` folders — temporary work, cleaned on completion.

Everything else — code, designs, plans, briefs, the frozen `acceptance.md` rubrics, reports, decisions (ADRs), conventions — lives in git.

## Link, Don't Copy in Merge Requests

A merge request references workspace artifacts by their node path — `proj/payments/gateway/stripe-client/report.md`, `proj/payments/gateway/stripe-client/acceptance.md`, `proj/decisions/003_api-design.md` — it does not duplicate their content. Same principle as bus messages: the request carries the verdict and synthesis; the supporting evidence lives in the versioned files it points to. Because the branch path *is* the node path, the link and the branch are the same address.

## Integrity Properties

Git provides integrity guarantees the system relies on — and which underpin the audit/optimizer-L1 substrate:

- **Decision-record (ADR) immutability** — git history shows if a decision record or a frozen `acceptance.md` was modified after creation. The D26 read-only-to-the-executor property is enforced *physically* here: a diff to a frozen rubric is visible and is itself a defect.
- **Append-only verification** — `log.md` diffs should show only additions. Easy to verify no old entries were changed.
- **Traceability** — commits carry the seat address and the `Serves:` requirement-ID(s), connecting every code change to the agent that made it and the intent it advances.

## End-to-End Flow

1. L5 (`…#exec`) works on the task branch, commits as it goes.
2. L5 runs frozen acceptance tests + unit tests + **CI floor**; only on green does it write `report.md` and signal L4 over the bus.
3. L5+ (`…#review`) independently tests and reviews against `acceptance.md` + spec → **accept** (forward; both L5 seats collapse) or **bounce** (bounded loop; L5 keeps context).
4. L4 routes the accepted task through the **independent review gate** → PASS automatically merges the task branch into the workstream branch; explicit `harnessctl merge` is repair-only.
5. Repeat for all tasks; L4 signals L3.
6. L3 routes the workstream through the review gate → PASS automatically merges into the area branch.
7. The **area branch clears the area-level review gate → PASS automatically merges into trunk (`proj`).** Repeat per area; cross-area conflicts escalate to L2.

The independent review function is on the path at steps 3–7 by default — it is how merges happen in V1, not an optional add-on.

---

*Operational reference — loaded at boot for levels that own code movement and gate evidence.*
*Created: 2026-03-29 · Updated: 2026-07-24 (automatic sanctioned merge on PASS; explicit merge repair only; contained workspace-derived repository identity; idempotent replay; parent-visible merge outcome).*
