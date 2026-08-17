# Hypothesis Tree Research System — Consolidated Build Plan

> **Status:** Working note; drafted 2026-07-07, post-design-completion.
> **Parents:** all seven design notes (concept, A1–A3, B4, C1, C6). This plan
> *sequences* what they specify; it introduces no new design. Where it makes a
> sequencing judgment call, that is flagged and vetoable.
> **Executor:** `hypothesis-tree-implement` (bus), directed by
> `hypothesis-tree-design` (this session holds full design context).
> **Ratifier:** the user, at the checkpoints marked 👤.

Tags: ✅ committed · 🟡 drafter sequencing judgment · 🔲 open.

---

## 0. Build principles ✅

1. **Bootstrap order = value order.** Every phase pays off standalone, before the
   next exists. No phase is justified only by a later one.
2. **Lean-first + P-1.** V1 of each component is the leanest ratified shape; no
   spend fences or machinery for failure modes never observed.
3. **Plan-gate per phase:** implementer replies with a plan of attack before
   writing code; director gates it.
4. **Review-gate per phase:** phase ends with a review pass (director + reviewer
   agent as warranted) against the owning design note before the next begins.
5. **The notes are authoritative.** Decision logs are not re-litigable by the
   implementer; deviations need director sign-off (and user ratification if they
   touch a user ruling).
6. **Manual before autonomous.** The user (advised by the director session) plays
   every authority role manually before any agent automates it — verifier gating
   before the verifier exists, navigation before the director exists. Calibration
   data accumulates from day one (decision_log populated even under manual
   navigation).

## 1. Dependency graph ✅

```
Phase 0  repo + schemas + ht tool ──────────────┐
   │                                            │
Phase 1  trace reader (needs only repo dir) ─┐  │
   │                                         │  │
Phase 2  observatory v1 (traces + audit log) │  │
   │            (feeds ledger candidates)    │  │
Phase 3  research loop core (schemas/ht; ledger; roles; verifier) ── first tree
   │                                         │
Phase 4  director + monitor (needs 3 running + 1's Progress Overview)
   │
Phase 5  hardening / growth-path (ongoing)
```

Phase 1 is independent of 0's schemas (only the repo skeleton) — 0 and 1 can
overlap if useful. 2 hard-requires 1. 3 hard-requires 0; consumes 2's output but
does not block on it. 4 hard-requires 3 + 1.

## 2. Phase 0 — Foundation (size S) ✅

Per A2 (physical layout) + A1 §10 + B4 §9.

- `research-system/` — public sibling research root; layout:
  `system/` (notes/, instruments/, roles/), `trees/`, `ledger/`, `readout/`,
  `worktrees/`.
- **Schemas as code**: A1 v2.1 node / dispatch / claim / tree-state **+
  ledger_entry** (A1 §7) as machine validation, **+ instruments-registry stub**
  (`system/instruments/`: suite id, version, noise floor, invocation command) —
  both consumed by Phases 2–3, well before their Phase 5 note-consolidation
  (review F1/F3).
- **`ht` tool v0**: init-tree + write-gated mutations; enforces B4 §9 mechanical
  validation, A1 §2.1 status-transition legality, D10 authorship domains;
  pre-commit hook rejects out-of-band writes (D7: invariants in machinery).
- Fixtures dir; founding-session transcript registered as fixture.

**Acceptance:** the B4 §9 illegal-write matrix is rejected case by case; a toy
tree walks the full lifecycle (mint → dispatch → claim → standing → park →
close) through `ht` only.

## 3. Phase 1 — Trace reader (size M) ✅

Per C6. Location: `system/instruments/trace-reader/`.

1. **Extractor**: session bundle → typed trace. Constraints (hard): graph over
   all uuid-bearing rows; parent-chain walk, abandoned branches labeled;
   `raw_ptr` anchors on every event; no-EOF assumption.
   **Acceptance (mechanical):** reproduces the founding-session ground truth
   (536-msg active path, 111 msgs / 10 labeled branches, 1 compact boundary,
   subagent joins).
2. **Behavioral digest + orientation view** (first view = the felt L4 pain).
   **Acceptance 👤:** run on a real L1-L5 run; user judges the digest genuinely
   informative.
3. **Progress Overview**: live-read, partial labeling, session-end heuristic,
   uncitability framing in the output template.
   **Acceptance:** useful "as of step N" overview of a genuinely running
   session (demo: watch the implement session itself).

Out of scope here: Codex adapter, checkpointed tailing (both Phase 5).

## 4. Phase 2 — Observatory v1 (size M) ✅

Per C1. Consumes Phase 1 traces + L1-L5 audit event log.

- Run-completion hook on L1-L5 → per-run observatory pass.
- **Screens + statistics store + defect-flow matrix** (caught-at mechanical;
  introduced-at classified with confidence; production cells for escaped
  defects). Statistics refreshed every run.
- **Triage + deep dives**: impact-driven depth (P-1 — no caps), pass
  done-definition, goal-function enforced.
- **Report cards** per run; **proposed ledger entries** per C1 §6 admission
  classes.
- **Minimal ledger tooling built here** (review F1/F2): backing store +
  `ht ledger create` / `ht ledger echo` against the Phase 0 ledger_entry
  schema. Echo = user-approved match increment (automatic D8 dedup-inform is
  Phase 3).
- **Bootstrap seam 🟡:** until the verifier exists (Phase 3), ledger admission
  is **user-gated manually** — proposed entries queue for user approval, writes
  role-stamped accordingly. The verifier takes over the queue when live.
- 👤 **Spine seed authoring** — 5–8 starting expectations, authored with the
  user. Blocking input for this phase.
- Synthesis every 3 runs.

**Acceptance 👤:** full pass on a fresh real run produces a report card the user
finds accurate; matrix populates; a recurring known issue accumulates echoes
rather than duplicate entries.

## 5. Phase 3 — Research loop core (size L) ✅

Per A1 + A3 + B4 + concept §5–§9. The big phase: first end-to-end dispatch.

- **Ledger completes**: proposal-time dedup-inform (D8) added; verifier-authored
  epistemic writes replace Phase 2's manual gating (store + create/echo already
  exist from Phases 0/2).
- **Instruments registry populated** with the first tree's actual suites
  (versions, noise floors, invocation commands) — dispatch task-package
  compilation (A3 §3.1) and verifier epoch-legality checks (B4 §3, §9) read it
  (review F3).
- **Role packets authored** (A3 two-layer substrate, versioned + hashed):
  senior, junior, checker, verifier. Stored `system/roles/`.
- **Dispatch runtime**: task-package generation, unit session spawn, metering,
  hidden director-side tripwires, blocking-question backchannel, report filing
  (A3 report format), archive layout.
- **Verifier live** (B4 §1–§6; the merge gate §7 is Phase 4 — review F6):
  adjudication pipeline, tier checks, bounce loop (cap 3 → escalate), standing
  rubric, epistemic writes through `ht`. Observatory admission switches from
  manual to verifier-gated.
- 👤 **First tree minted** on a real component — candidate: the L1-L5 harness
  itself (user picks) — seeded from ledger candidates + user ideas.
- **User-as-director** (per principle 6): navigation moves chosen by the user
  advised by the director session, executed via `ht`; decision_log populated
  from the first move. Acknowledged (review F7): the statistics readout —
  part of the director's designed surface (concept §4) — doesn't exist until
  Phase 4; defensible lean-first for a young tree, and retroactive calibration
  from day-one raw data is ratified (concept §11).

**Acceptance:** one complete dispatch: mint → dispatch → unit works → report →
adjudication → claims/standing on the tree — every write through `ht`, every
claim anchored, bounce loop exercised at least once (can be staged).

## 6. Phase 4 — Director + monitor (size M) ✅

Per concept §4, §8.4, §11; A1 §5.

- **Monitor v1** (instrument only, zero authority): statistics computed from
  decision_log + spend data into `readout/`, **interpretation rules co-located
  with the numbers** (user ruling — signals are symptoms, not verdicts).
- **Director role packet**: surface = tree index + ledger triage + statistics +
  trace-reader digests (never raw material); move vocabulary via `ht`; graded
  control loop consuming Progress Overview (§8.4).
- **Shadow mode first 🟡:** director *proposes* moves, user approves — a
  calibration period against the accumulated manual decision_log. Autonomy
  switched on by 👤 user call, not by schedule.
- **Merge gate built + first real merge** (B4 §7: referent-gap conditional
  re-verify, settlement completeness, fresh standing re-derivation, watch-row
  creation) — exercised end to end (review F6: this build lands here, not
  Phase 3).
- **Visual tree view** in the readout (A1 §8: fill=standing, outline=status,
  size=spend, epochs-as-strata; shape language still 🔲, readability first)
  (review F4).

**Acceptance 👤:** a full shadow-mode navigation session whose proposed moves
the user judges sound; first merge lands through the full gate.

## 7. Phase 5 — Hardening / growth-path (ongoing) ✅

In no committed order; each item gated by observed need (P-1):

- Codex-format trace adapter (required before observatory fully covers L5
  seats — C6 §9.4).
- Checkpointed incremental tailing; eval minting from defect-flow pressure;
  impact-estimate calibration via post-merge watches (C1 §4).
- B5 instruments-registry **note** consolidation (the registry itself is built
  in Phases 0/3 — review F3).
- **User↔director surface design** (concept §13.7, still 🔲): Phases 3–4 run
  manual navigation over an undesigned surface (chat + `ht`); design it before
  or at the director-autonomy switch.
- Multi-tree mechanics + cross-tree cursor (when a second component earns a
  tree). Parallel dispatches (designed-for, not enabled).
- Dialogue/experience-stream tooling for non-harness sessions.

## 8. User-input checkpoints (consolidated) 👤

1. Phase 1: judge the first behavioral digest (acceptance).
2. Phase 2: author the spine seed; judge first report card; approve early ledger
   entries (manual gating until Phase 3).
3. Phase 3: pick the first tree's component; ratify starter hypotheses; play
   director (advised).
4. Phase 4: judge shadow-mode director; switch on autonomy.
5. Standing: tier-3 ratifications, spine promotions, doc-placement ruling.

## 9. Risks & mitigations ✅

- **Schema drift between code and notes** → schemas generated/validated against
  A1 v2.1 text at review gates; the note wins.
- **Bootstrap chicken-and-egg (ledger before verifier)** → ledger schema in
  Phase 0, minimal tooling in Phase 2, explicit manual gating seam (🟡) swapped
  for the verifier at Phase 3.
- **Implementer re-litigating rulings from partial context** (observed failure
  mode post-compaction) → notes-are-authoritative rule + decision logs; director
  reviews diffs at phase gates.
- **L5/Codex coverage gap** → named, parked, Phase 5 item.
- **Scope creep in the L phase (3)** → plan-gate splits Phase 3 into
  sub-milestones at implementation planning time; each sub-milestone
  independently reviewable.

## 10. Review triage (2026-07-07, one Opus reviewer, scenario-walking lens)

Seven findings, zero contradictions. **Accepted:** F1 (ledger schema/tooling
predates Phase 2 need — fixed in Phases 0/2), F2 (echo acceptance now runnable
via Phase 2 minimal echo), F3 (instruments registry: stub Phase 0, populated
Phase 3), F4 (visual tree view added to Phase 4), F6 (merge-gate build moved
explicitly to Phase 4), F7 (statistics-gap during manual navigation
acknowledged). **Partial:** F5 — reviewer misidentified which figure was buggy
(10 branch *groups* is the verified count; the bug was the 10-message active
path), but correctly caught that the acceptance figures weren't recorded in C6
§2; fixed at the source (C6 §2 now carries the measured ground truth and the
branch-groups ≠ fork-points distinction). Suggestion adopted: §13.7 flagged in
Phase 5.

## 11. Decision log

| Item | Decision | Date |
|---|---|---|
| Plan scope | Sequences existing design only; no new design surface | 2026-07-07 |
| Phase order | Value-order bootstrap: foundation → trace reader → observatory → loop → director. 🟡 drafter judgment, user-vetoable | 2026-07-07 |
| Manual-before-autonomous | User plays verifier-gate (Phase 2) and director (Phase 3) before agents take the roles; shadow mode before director autonomy | 2026-07-07 |
| Build seam | implement session builds; design session directs/advises; user ratifies at 👤 checkpoints | 2026-07-07 (user) |
