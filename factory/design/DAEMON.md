# DAEMON — The Harness-as-a-Process (Cluster ① spec)

Status: design, v1 cut. This is the **substrate** spec — clusters ②/③/④ sit on it.

> **SUPERSEDED IN PART — 2026-07-24.** The current contract below overrides §1.4's transport and
> detector rows, the liveness/terminal/answer portions of §3.2–3.6, special
> `child_escalated` routing in §3.5, §5.2's detector order, §6.3b's handoff taxonomy, and the
> cluster-②/③ seats in §9 wherever they conflict. Those sections remain derivation and compatibility
> history. The run-scoped lifecycle in §2.1–2.3 remains current.

## 0. Current substrate contract — 2026-07-24

The daemon is a run-scoped, single-writer gateway. Genesis starts it on demand; a per-run launchd
job restarts it while any binding proves a live build; reconcile-on-start recovers an in-flight
build; a definitive no-live-build sweep permits clean exit and launchd cleanup.

Current per-seat state includes the runtime hook profile, `.turn-state.<seat>.json`,
`.turn-events.<seat>.jsonl`, `.owed-checklist.<seat>.json`, `.sign-off.<seat>.json`,
`.signal.<seat>.json`, and `.inbox.<seat>.jsonl`. Current run state also includes sender-owned
`messages/`, open-question projections, address-owned product/review-check cohort barriers,
notary stamps and candidate snapshots, contract version/lineage/receipt state under
`contract-revisions/` and explicit holder rebinds under `contract-rebind/`, plus the selected
local-file blinder mode.

At L2+ FULL review, the daemon derives the exact five-seat roster before opening any actor. It
notary-checks the intent receipt, Q3 coverage report, and candidate snapshot; writes one
content-addressed confirmed-journey/MNF probe roster; prepares separate writable verified
candidate copies for user-simulation and performance/robustness; then pre-registers the entire
cohort before spawning. The existing dynamic `review_check` barrier remains the one synthesis
wake. Probe dispatch fails closed while either versioned charter lacks owner+director calibration
or an explicit model/runtime assignment.

Runtime hook turn state is the primary normal liveness read. The shared return-contract projection
checks every observed turn end for exactly one of: owed product present, ledger-derivable wait, or a
prod requiring the seat to produce/explain/escalate. Process death, a hook-observed hung tool, and
missing/hook-less runtime edges retain evidence-floor recovery. Legacy `handoffs/*.json`,
coordination verbs, and `.signal ESCALATED` are read-side compatibility projections, not parallel
truth; new direct-edge communication uses canonical messages/questions. The daemon wake table
delivers direct messages and decisions, keeps ordinary child completion silent until its cohort
barrier crosses, and never lets a message amend a frozen contract.

The run ledger carries replay-changing decisions plus the bounded recovery and audit rows with named
live consumers; it is not a copy of every runtime observation. Ordinary `turn_state_observed` rows
live only in the existing seat-qualified turn-event/current files. Adoption still refreshes the
derived binding checkpoint under the single-writer lock, while hook degraded/recovered edges remain
replayable. The machine-readable 92-event classification and drift guard live in
`harnessd/wal_policy.py`; `tests/test_wal_diet.py` proves every authored event
is classified, and living documents reference the production authority rather
than duplicating it.
Legacy WALs retain their old rows and remain replayable.

Whole-build observability is deliberately **not a poll leg**. `harnessctl view` and
`harnessctl journey` directly join the atomic binding checkpoint with node-local turn, checklist,
question, contract, gate, barrier, and blinder facts when the operator asks. The same read path
works after this run-scoped daemon exits. Its default HTML capture lives under
`<runtime-root>/.harnessd/views/`, outside every node/gate/blinder surface. The historical
`log.aggregate.md` / `status.aggregate.md` append-relay has no daemon write path; legacy suffix
exclusions remain only so old runtime trees replay safely.

This document specifies the run-scoped harness daemon: the process that maintains the
**accountability invariant** across the L1–L5 agent tree. It is the cluster-① deliverable
named in `working-notes/runtime-decisions-and-commissioning-2026-06-04.md` §6 and closes
gap-review blocking **#1** (daemon SPOF / genesis), the cluster-① half of blocking **#2**
(per-node process-liveness/death signal + durable coordinator-completion artifact), and the
**lost `state-ledger-races` lens** (ledger atomicity/locking/write-races,
`arch-gap-review-2026-06-04.md` line 105).

It **adapts** the recovered self-improvement-harness control plane
(`research/orchestration-frame/self-improvement-harness/control_plane.py`, 1706 lines, +
`CONTROL-PLANE.md`, `manifest.yaml`, `workboard.py`, `watchdog.py`). The recovered model is **one
loop / one manifest**; cluster ① generalizes it to a **tree of per-node bindings**.

Two sources sit in **different streams**, and the doc keeps them distinct (the provenance honesty
matters for trusting the reuse claims):

- the **recovered control plane** (`self-improvement-harness/`) — the CAS/transition/atomicity
  skeleton this doc generalizes. It has **no** `lease_epoch`/`owner_token`/CAS-on-spawn (verified:
  those strings appear nowhere in its `control_plane.py`, `manifest.yaml`, or `watchdog.py`).
- the **phase-2 lease design** (`research/orchestration-frame/phase-2-runs/research/watchdog-design-01.md`)
  — a **proposed-but-never-coded** lease design that originates the `lease_epoch` + composite
  `owner_token` format (L17, L23–24, L65); it was *partially* wired into
  `phase-2-runs/harness/loop_supervisor.py`'s `--expected-owner-token` plumbing (L176–178) but never
  into a CAS-guarded spawn. v1 **promotes this proven-on-paper design into code for the first time.**

Do **NOT** import the older superseded **`phase-2-runs/harness/loop_supervisor.py`** (the prior
live proving-ground supervisor that carries the older `owner_token` wiring). Per `CONTROL-PLANE.md`'s
precedence rule, `phase-2-runs` is a proving ground — designs promote only through a canonical round,
which is exactly why its `loop_supervisor.py` is **not** the adaptation base (the
`self-improvement-harness` control plane is). *(Note: there is no `control_plane.py` under
`phase-2-runs/`; `loop_supervisor.py` is the file the "do-not-import" guard actually names.)*

---

## 1. Charter & Scope

### 1.1 The accountability invariant (LOCKED, quoted verbatim from runtime-decisions §1)

> The daemon maintains the **accountability invariant** — single-owner-always, continuous
> reconciliation, fenced ownership transfer.

with the precise guarantee:

> **every session owned, reconciled within bounded time, stale actions fenced**

(a brief reconciliation lag is expected; *fencing makes the lag safe*.)

The invariant decomposes into two orthogonal layers (runtime-decisions §1 line 16):

- **Accounting = single owner always.** Supervision tree (one parent-owner per node) +
  the binding ledger as the authoritative registry + a continuous reconciliation loop that
  keeps *actual* (tmux) == *recorded* (ledger). Solves the **orphan** problem (including the
  dead-coordinator-with-live-children case).
- **Fencing = a stale owner's action fails.** `lease_epoch` + `owner_token` + compare-and-set,
  enforced at the instant the actor acts. Solves the **stale-authority / split-brain** problem
  (incident F-024) that accounting alone cannot.

They compose: accounting guarantees *someone* owns every node; fencing guarantees only the
*current* owner can change it.

### 1.2 What the daemon HOSTS

- the single-writer **control-plane executor** (the only state-changing code path),
- the **binding ledger** (per-node current state) + the append-only **run-ledger** (history),
- the single **spawn chokepoint** (claim-before-spawn),
- the **reconcile loop** (on-restart + continuous),
- the **watchdog loop** (it runs *inside* this process; the **loop body / detector / recovery
  state-machine are cluster ②** — the daemon owns the loop scheduling and the ledger fields the
  loop reads/writes through the executor),
- **genesis / first-boot**.

Generated operator views are outside this hosted set: the daemon neither writes nor serves them.

### 1.3 Cluster ① owns (and ONLY these — sufficiency-cut INCLUDE set)

Per the sufficiency-cut test (runtime-decisions §2: *retrofitting it later means re-plumbing
every call site → now; attaches at one known site later → defer; single-writer is the keystone
that makes deferral safe*), v1 includes:

1. the **daemon process** itself (run-scoped service-manager protection, PID/lock, behavior when it dies),
2. the **per-node binding-ledger schema** (incl. `lease_epoch`/`owner_token`/liveness/deliverable/terminal-signal),
3. the **append-only run-ledger** event format,
4. the **single-writer executor** (CAS-guarded transition chokepoint, lock-serialized, atomic,
   validate-before-commit) + the **ledger write-race/atomicity model**,
5. **reconcile-on-restart + continuous reconciliation**,
6. the **single spawn chokepoint** (Claude-Code boot via `--system-prompt-file
   <node-workspace>/.identity-prompt.md`, composed from the ONE shared minimal prompt plus the
   selected identity trio; broader protocol docs are delivered as documents the agent reads —
   claim-before-spawn / F-024 fix, thin per-runtime adapter),
7. **genesis / first-boot**,
8. **fencing** (`lease_epoch` + `owner_token` + CAS on transition **and** spawn).

### 1.4 Explicitly DEFERRED (named here, owned elsewhere — NOT designed in this doc)

| Deferred item | Owner | Cluster ① provides the SEAT |
|---|---|---|
| Evidence-lease recovery state-machine (`stale_suspect → recovery_in_progress → failed_confirmed → adopt`) | ② | the binding fields the machine reads/writes: `condition`, `suspect_since`, `recovery_attempts`, `last_evidence` |
| Detector internals (multi-signal fusion: window_activity + CPU + JSONL-growth) | ② | the `liveness-state` field + `last-progress` field the detector writes through the executor |
| Leaf-reap vs coordinator-recover policy; auto-resume *execution* logic | ② | `auto_resume_command` + `allow_recovery` plumbing (carried, not fired) |
| Bus transport + wake contract + escalation-answer-down + human channels | ③ | terminal-signal events are journaled by the executor; the bus carries best-effort nudges |
| Admission control / 429-backoff / per-runtime ceilings / resource envelope | ④ | the spawn chokepoint's claim-slot pre-step (admission wedges *between* claim-accepted and actor-open) |
| Full desired-vs-actual reconciliation **controller** | ② (later) | the v1 continuous reconcile loop (boundary drawn in §5) |
| 2-week resurrection GC reaper | infra (separate) | distinct from the daemon's liveness reap (see §5.4) |

The cluster-① rule: **provide the seats, not the internals.** Every deferred policy attaches to a
ledger field or the spawn-chokepoint claim-slot that this doc defines now.

---

## 2. The Daemon Process

### 2.1 What it is

A single **run-scoped, service-manager-protected** process — the `harnessd` executor + watchdog
scheduler + reconcile loop in one address space while a build is live. It is the only writer of durable
control-plane state. It is **infrastructure, not an agent**: it never reasons, never calls a
model; it executes deterministic transitions, sweeps tmux, and fires the watchdog tick.

Distinct from the recovered control plane, which was a **per-CLI-invocation** process that took
an advisory lock and exited (`CONTROL-PLANE.md` L119–124: *"The current goal is not full
daemonization."*). v1 keeps one process for the **duration of a live build** so that (a)
restart = recovery has a defined owner, (b) the watchdog loop has a host that survives a child's
death, and (c) all bindings sit under **one serialization domain** (closing the two-lock gap,
§4.4). It is not loaded at login and has no standing no-build residency.

> **Adapted from control_plane.py: reused / changed.**
> *Reused:* the read→validate→commit-inside-the-lock mutation skeleton; atomic-replace + append-fsync
> primitives; validate-before-commit. *Changed:* per-CLI-process → run-scoped single-writer; one
> manifest → tree of bindings; two locks (`.control-plane.lock` + `.workboard.lock`) → one
> serialization domain.

### 2.2 Service-manager-hosted (genesis charter)

`harnessctl start` renders a **per-run launchd user-agent plist under the runtime root** and
bootstraps it on demand. The tracked plist in `harnessd/launchd/` is the renderer prototype, not
an installed login agent. Its policy is:

- `KeepAlive = { SuccessfulExit = false }` — restart only a crash/nonzero exit;
- no `RunAtLoad` — `harnessctl start` is the only deliberate start trigger;
- `ThrottleInterval ≥ 10` — no crash-restart storm while a build is live;
- absolute interpreter/repository/runtime/request arguments and run-local logs;
- a rendered deterministic environment: preparation resolves every daemon-direct external tool
  (`tmux`, `git`, `sandbox-exec`, and the exact Python interpreter), refuses once with the complete
  missing set, and freezes their absolute paths plus a minimal PATH into the per-run plist.

The fixed argv carries an atomic one-shot start request. The first owning process consumes it and
runs full genesis; a launchd crash restart sees it absent and runs reconciliation-only. After a
completed tick proves the binding ledger empty/all-terminal, the daemon closes IPC, releases the
instance lock, exits 0, and a bounded after-parent cleanup child boots the per-run service out and
removes the rendered plist. A crash path cannot schedule that cleanup, so protection remains loaded.

The request may also carry ordered L3 module review-panel arms commissioned by
repeated `harnessctl start --review-panel-arm` options. The field is omitted
when unconfigured. Genesis copies a non-empty declaration list onto the
parentless L1 binding so crash recovery reads the same run-scoped choice from
the ledger; no daemon-memory registry or second config file exists.

There is no standing pinger job in the run-scoped v1 artifact set (§2.6).

### 2.3 PID / lock — single-instance guard

On boot the daemon acquires an **exclusive non-blocking `fcntl.flock` on
`.harnessd.instance.lock`, held for the daemon's lifetime**, and writes a `runtime.json`
self-report `{pid, started_at, lock_path, last_tick_at, last_reconcile_at, incarnation}`
(`lock_path` names the INSTANCE lock). If the lock is already held, the new instance exits
(*"another harnessd instance already holds the lock"*) — **before** writing `runtime.json`, so a
refused second instance clobbers nothing. `harnessctl start` also probes IPC, this lock, and the
exact protective job before publishing a request; the daemon lock remains the final race fence.

> **Resolution note (two lock files; fork decided 2026-06-10).** `.harnessd.instance.lock` is
> DELIBERATELY DISTINCT from the §4.3 per-mutation `.harnessd.lock`: `flock` conflicts across open
> file descriptions even within one process, so a lifetime hold of the mutation lock would
> deadlock every executor write — the previously-unresolved §2.3-vs-§4.3 single-file conflict
> (review SWCAS-02). Each section now names its own file; the two never contend. Acquire order is
> fixed (instance lock first, always), keeping the two-lock ordering deadlock-free by
> construction.

> **Naming guard (one-spine discipline).** `incarnation` here is the **daemon-restart counter**
> (how many times launchd has relaunched `harnessd`) — it is a NEW field of the daemon's
> `runtime.json` self-report and is **deliberately distinct** from the per-node binding
> `generation` (the per-node CAS counter, §3.2/§4.2). Same-name collision avoided on purpose: one
> word, one meaning. `last_tick_at` remains the completed-tick diagnostic / future external
> hang-detector surface (§2.6); it is not a reason to keep a standing job.

> **Adapted from watchdog.py: reused / changed.** *Reused:* the `fcntl.flock LOCK_EX`
> single-instance guard (watchdog.py L74–81; v1 splits the recovered one-lock pattern into the two
> files above — instance guard vs per-mutation domain) and the `runtime.json` self-report surface, whose
> **reused** fields are `{pid, started_at, last_checked_at, last_condition}` (verified against
> watchdog.py L370–440 — it carries no CAS/generation field). *Changed / NEW:* `last_tick_at`,
> `last_reconcile_at`, `incarnation`, and `lock_path` are NEW fields on the daemon's self-report
> (not part of the reused watchdog.py set); the lock guards the *whole run-scoped daemon* (not just a
> watchdog loop); the surface is launchd-protected while live rather than a bare pid-file (gap-review note:
> existing service status *"only detects pid-file daemons"*).

### 2.4 FORK — single-central daemon vs one-daemon-per-node

> **FORK — for user review.** The charter and gap-review both flag this as undecided
> (blocking #1: *"one-central vs one-per-node"*).
>
> - **Option A — single-central daemon (RECOMMENDED).** One `harnessd` holds all per-node
>   bindings, sweeps the whole tree each reconcile tick, and serializes every mutation behind one
>   lock. **Pro:** one serialization domain ⇒ a node's control + deliverable update is one atomic
>   transaction (closes the two-lock gap directly); one CAS authority; one reconcile sweep; matches
>   the single-writer keystone. **Con:** it is itself a SPOF — but that is exactly what launchd
>   `KeepAlive` + lease-state-in-the-ledger + relaunch=recovery is designed to neutralize (its
>   death is recoverable, not catastrophic, because all state is durable and fenced). Scaling
>   ceiling: one process sweeping N tmux panes per tick — fine for an L1–L5 tree (tens of nodes),
>   and the sweep is I/O-cheap (tmux list + mtime stat).
> - **Option B — one daemon per node.** Each node has its own supervisor process. **Pro:** no
>   central SPOF; natural process-tree parenting. **Con:** N lock domains ⇒ no atomic cross-node
>   commit; reconcile across the tree must read N processes' state; fencing needs a distributed
>   CAS authority; far more moving parts for a tens-of-nodes tree. Re-introduces the very
>   two-lock atomicity gap §4.4 closes.
>
> **Recommendation: Option A.** The single-writer keystone (runtime-decisions §2) is the whole
> reason deferral is safe; splitting the writer per-node forfeits it. The SPOF objection is
> answered structurally by §2.5 (relaunch = recovery), not by sharding the writer.

### 2.5 Behavior when the daemon dies (the SPOF answer)

This is the core of gap-review blocking #1 (*"Machine reboot/OOM kills the tmux server + every
session + the watchdog together, with no surviving actor to recover"*). Two death modes:

1. **Daemon dies during a live run, tmux survives** (OOM-kill of `harnessd` only, or a daemon
   crash). The already-loaded per-run job relaunches → the daemon runs **reconcile-on-restart**
   (§5.1): read bindings → check each
   recorded session-uuid against live tmux → resume ownership, necro the dead, escalate the
   unowned. No double-spawn (resume-not-double-spawn, §7). The surviving agents kept working
   while unsupervised; reconciliation re-establishes accountability within bounded time (the
   "brief reconciliation lag" the guarantee explicitly tolerates — fencing makes it safe).
2. **Machine reboot / login ends / tmux server dies too.** Nothing starts at login. The operator
   deliberately invokes `harnessctl start` again; its full path first runs the same restart reconciler,
   finds no live panes, necros stale ownership, and resumes/recreates L1 through the existing
   no-double-spawn genesis routing (§7). Lease-state-in-the-ledger makes this on-demand recovery
   safe without standing residency.

The invariant that makes both safe: **lease-state lives in the ledger, not in the daemon's RAM.**
The daemon is reconstructible from the ledger on every boot. This is the recovered research's
proven rule (`watchdog-design-01.md`: *"The lease belongs to the work unit, not to the watchdog
process. A restarted watchdog can resume from manifest state."*).

### 2.6 Behavior when the daemon HANGS (the third death mode)

launchd crash-only `KeepAlive` restarts only on **exit** — it does nothing for a **wedged-but-alive**
`harnessd` (a deadlock, an infinite loop, a stuck syscall) that still holds `.harnessd.lock` and
the serialization domain. A hang freezes the tree just as hard as a crash. The daemon still stamps
`runtime.json.last_tick_at` after every completed tick, so status/commissioning can diagnose a
wedge and a later external detector has a stable surface. The prior always-loaded
`harnessd-pinger` design is **not installed or loaded** by the run-scoped lifecycle; adding a
bounded live-run hang killer remains a separate explicitly-scoped hardening, not justification for
login residency. `ThrottleInterval ≥ 10` still bounds actual crash relaunches.

---

## 3. The Ledgers

Two durable surfaces, mirroring the recovered `manifest.yaml` (current state) + `run-ledger.jsonl`
(history) split, **generalized to a tree**:

- the **binding ledger** — per-node *current* state, one record per node-address. Atomic-replace.
- the **run-ledger** — append-only event journal, global ordering, one line per event. Append+fsync.

### 3.1 The ledger key — the one-spine semantic address

The binding-ledger key is the **collapsed semantic address** (`WORKSPACE-SCHEMA.md` §"One
Hierarchical-Path Spine"): node-path + `#role-variant` suffix — e.g.
`payments/gateway/stripe-client#exec`. NOT the physical `L{n}/`-laden workspace path. The address
is **stable across respawn/collapse/resurrection** (F35) because it is a property of the *position
in the tree*, not of any ephemeral instance. This is the one-spine identity reused as ledger key:

> node-address = requirement-ID = filesystem path = git branch = agent-address = **ledger key**.

Topology is derivable from the key by **prefix arithmetic** (parent = truncate last segment;
children/siblings = prefix match) — no separate parent-pointer field is *required* for
correctness, though a denormalized `parent` field MAY be stored to speed reconcile sweeps.

**Seats co-locate on a node.** A node hosts more than one seat (`#exec`, `#review`, and future
first-class `#test`). Each
seat is its own ledger key (`...gateway#exec`, `...gateway#review`) with its own session-uuid /
owner / lease. "Single-owner-always" is **per seat**, not per node — two live sessions legitimately
occupy one node via different suffixes. Current V1 exception/refresh acceptance authoring uses an
L5 test-author child plus L5+ review rather than opening a live `#test` seat. (FORK note below.)

> **FORK — for user review.** Per-seat ledger row vs one node row with per-seat sub-fields.
> **Recommendation: per-seat row (one binding per `address#suffix`).** It keeps "single-owner" a
> flat per-key property, lets `#exec` and `#review` carry independent lease_epoch/owner_token, and
> keeps reconcile a flat sweep. Alternative (one row, nested seats) couples two leases' atomicity
> and complicates CAS. Cost of the recommendation: a node's "is anything alive here" query becomes
> a prefix scan over suffixes — cheap.

### 3.2 Binding-ledger record schema (per node-address#seat)

> **Adapted:** the field set generalizes the recovered `manifest.yaml`'s `activity_lease` +
> `watchdog` + `observation_window` blocks (which existed **once**, for one loop) into **one record
> per node-address**, and **adds** the fencing tokens and the process-liveness/death fields the
> recovered model lacked.

```yaml
# binding-ledger.yaml  (or per-node files — see FORK §3.4); keyed by address
"payments/gateway/stripe-client#exec":

  # --- identity / topology ---
  node_address: payments/gateway/stripe-client#exec   # the one-spine key (== this map key)
  parent_address: payments/gateway#exec               # denormalized; derivable by truncation
  level: L5                                            # L1..L5 / L5+ (#review)
  session_uuid: 3f2a9c81-0000-4abc-9def-111122223333   # the CC/Codex session id (per incarnation)
  tmux_target: harness:payments/gateway/stripe-client#exec  # tmux session/pane name (§6.2)

  # --- ownership / fencing (NEW vs recovered) ---
  owner: payments/gateway#exec        # the parent-owner address (supervision tree)
  lease_epoch: 3                      # monotonic int; bumped on every claim/adopt/respawn
  owner_token: "payments/gateway/stripe-client#exec:subagent-0a1b2c3d:3f2a9c81:3"
                                      # composite: address:subagent-id:session-uuid:lease_epoch
  generation: 7                       # per-node CAS counter (NOT global ledger length — see §4.2)
  last_applied_seq: 412               # run-ledger seq of the last WAL event committed to THIS node
                                      #   (the replay watermark — written in the same atomic-replace; §4.4)
  paused_at: null                     # ISO-UTC timestamp; null = not paused (TRANSPORTS §5.3). A subtree
                                      #   is paused if THIS node OR any ancestor (address-prefix) has it set.
                                      #   Set/cleared ONLY by the human control surface (③), routed through
                                      #   the single-writer executor — never raw. Two enforcing read-points:
                                      #   ①'s §6.1 claim-slot pre-step (refuse to launch a child under a
                                      #   paused subtree) and ②'s recovery loop (skip prod/respawn for a
                                      #   paused subtree). Carried here; honored at those read-points.

  # --- lifecycle state (GENERIC per-node, NOT the reviewer-loop vocabulary) ---
  state: running                      # planned|claimed|spawning|running|blocked|done|failed|dead
  state_entered_at: '2026-06-05T10:00:00+03:00'
  last_binding_update_at: '2026-06-05T10:14:00+03:00'   # last-touch, separate from entry-time

  # --- process-admission metadata (control-plane scheduling, not lifecycle) ---
  admission_state: admitted            # admitted|waiting_on_sibling|blocked_on_sibling; waiting/blocked nodes stay planned
  schedule_policy: serial_l3_workstreams
  schedule_group: payments/gateway#exec:L3
  schedule_index: 2                    # sibling order inside the schedule group
  waiting_on_sibling: payments/gateway/parser#exec  # immediate predecessor; null/absent once first
  queue_reason: workstream_serialization
  queued_since: '2026-06-05T10:02:00+03:00'
  admission_ready_at: null             # set when predecessor gate_state=gate_passed releases it
  admission_released_by: null          # predecessor address that released this node, when applicable
  admission_blocked_by: null           # predecessor address that blocked this node, when applicable
  admission_block_reason: null         # e.g. predecessor_terminal_not_passed

  # --- liveness-state (written by the reconcile loop from ACTUAL tmux; NEW) ---
  liveness_state: working             # working|waiting|idle|dead  (CANONICAL — agent-lifecycle.md owns
                                      #   this enum; the ② detector writes it). Optional bookkeeping
                                      #   values: 'claimed' (pre-spawn, no actor yet), 'terminal'
                                      #   (post terminal-signal). working/waiting are NOT folded — the
                                      #   waiting-vs-idle split is load-bearing for the §5.4 coordinator roll-up.
  last_progress_at: '2026-06-05T10:13:30+03:00'         # forward-progress (artifact/JSONL growth)
  last_heartbeat_at: '2026-06-05T10:14:00+03:00'        # liveness ping (separate from progress)

  # --- detector/lease fields READ BY cluster ② (carried, not interpreted here) ---
  condition: healthy                  # never_checked|healthy|inactive|stale_suspect
                                      #   |recovery_required|recovery_in_progress|terminal|invalid
                                      #   (terminal = the confirmed-failed lease value, WATCHDOG §3.1 —
                                      #    no separate failed_confirmed enum; condition IS the surface)
  suspect_since: null
  stale_check_count: 0                # consecutive-stale-poll GRACE counter (name matches recovered
                                      #   watchdog.py); reset on ANY renewal. ② keys the grace gate off it.
  stale_grace_checks: 2               # the grace THRESHOLD (config, default 2): ② escalates to recovery
                                      #   when stale_check_count >= stale_grace_checks (WATCHDOG §3.5; watchdog.py L211).
  recovery_attempts: 0                # recovery-CYCLE counter; reset ONLY on confirmed-healthy-after-recovery.
                                      #   DISTINCT from stale_check_count — never overload one onto the other (WATCHDOG §3.5).
  recovery_attempt_ceiling: 3         # respawn bound for recovery_attempts (per-node config, set at spawn
                                      #   like W); recovery ESCALATES past it instead of looping (WATCHDOG §3.4 step 8).
  consecutive_failed_incarnations: 0  # S5 stable-address actor-death streak; only post-open deaths count.
  failed_incarnation_causes: []       # bounded to the current streak's three {event, reason, at, incarnation} rows.
  respawn_parked_at: null             # third consecutive actor death; every actor-open/re-register path refuses/preserves it.
  gate_crossed_at: null               # resume-firewall flag (§6.4): set when this node crosses a quality-gate
                                      #   boundary. ② maintains it; ① reads it to REFUSE --resume (fail-closed).
  auto_resume_command: null           # cluster-② SEAT; carried, fired only with --run-recovery
  last_evidence: { source: reconcile_sweep, heartbeat_at: ..., progress_at: ...,
                   semantic_event_at: null }   # semantic_event_at: ② writes (semantic detector); null in v1

  # --- deliverable-state (the workboard-stream half, merged onto the node; §3.3) ---
  deliverable_state: active           # planned|active|waiting|completed|blocked|cancelled|delivered|delivery-failed
  stop_condition: "stripe-client passes acceptance.md"
  write_targets: [ "src/payments/gateway/stripe-client/" ]   # IN-JAIL relative write surface inside the node
  evidence_refs: [ "report.md" ]                              # completion artifacts
  acceptance_ref: "acceptance.md"     # frozen rubric at the node (read-only)
  delivery_source: null               # IN-JAIL promotion SOURCE — optional product-surface dir under the node workspace; null means node workspace root.
  delivery_destination: null          # OUT-OF-JAIL promotion TARGET — user-path or git-remote (from intent-spec §8); set at intake, consumed by promote-out (INTAKE-TO-DELIVERY §3). Distinct from write_targets (the in-jail source).
  delivery_kind: null                 # filesystem-path | git-remote — drives copy-out vs push at promotion
  fidelity_playback_authority: owner  # owner | operator-delegate; run-scoped, default owner
  fidelity_playback_delegate: null    # exact launch-declared delegate label; never inferred
  fidelity_playback_delegation_reason: null
  fidelity_playback_authority_build_id: null
  fidelity_playback_current_question_id: null
  fidelity_playback_owner_questions: {}  # content-addressed pointer packages + immutable answers
  fidelity_playback_last_answer_authority: null  # owner | operator-delegate, never conflated
  fidelity_playback_last_answer_actor: null

  # --- terminal-signal (the durable completion/death fact the parent's kill keys off; NEW) ---
  terminal_signal: null               # DONE|FAILED|ESCALATED|DIED_INFRA|DIED_METHODOLOGY|FENCED (§3.6 table)
  terminal_signal_at: null
  terminal_note: null                 # free text (e.g. ESCALATED question, FAILED reason)
  signal_artifact_seen_at: null       # harness-observed identity of the last <node-dir>/.signal.<seat>.json journaled (journal-once guard; §3.5)
  answered_at: null                   # canonical expiry for the held ESCALATED question; fresher than terminal_signal_at means this artifact was answered
  answer_route: null                  # human_to_parent | parent_to_child_alive | parent_to_child_resume
  answer_actor: null                  # node/control actor that supplied the answer
  answer_artifact: null               # durable decision artifact for parent-to-child answer-down
  answered_signal_artifact_seen_at: null  # signal_artifact_seen_at identity the answer resolves

  # --- verifiable spawn fact (H40 / runtime-and-model-map) ---
  model_used: "opus-5.0 / claude-code"  # ACTUAL model+runtime that ran (config=intent, this=fact)
  system_prompt_file: "<node-workspace>/.identity-prompt.md"  # FACT — the concrete prompt file loaded
                                        #   by Claude-Code. Its first section is the constant shared
                                        #   prompt; the selected identity trio follows under provenance
                                        #   headers. At minimum it is never a bare per-level role path.
  role_variant: "L5#exec"               # PER-binding — selects WHICH load-manifest/bundle + per-level role
                                        #   docs the chokepoint assembles into the brief (e.g. "L4",
                                        #   "L5+#review"). This is the field that varies by seat.
  role_bundle_hash: "sha256:…"          # detect role-bundle drift (fencing surface, open Q)
```

**Two-surface split (kept from the recovered model).** `liveness_state` + `condition` (lease/health)
is one surface; `deliverable_state` + `terminal_signal` (semantic/work) is the other. They are
*distinct* (F-003/F-033): a session can be `alive` but its deliverable `blocked`. The reconcile
loop writes the liveness surface from tmux; the executor writes the deliverable surface from
terminal signals and transitions.

> **Provenance — reused / promoted / NEW (split honestly across the two streams).**
> *Reused from the recovered `manifest.yaml` (self-improvement-harness):* `activity_lease`
> `{last_heartbeat_at, last_progress_at, status}` → `last_heartbeat_at`/`last_progress_at`/
> `liveness_state`; the `watchdog` block `{condition, suspect_since, recovery_attempts,
> stale_grace_checks, auto_resume_command, last_evidence}` → carried per node (this block has **no**
> `lease_epoch` — verified); the workboard stream `{stream_id, status, owner, stop_condition,
> write_targets, evidence_refs}` → the deliverable fields.
> *Promoted from phase-2 `watchdog-design-01.md` (proposed-but-never-coded; see header):*
> `lease_epoch` + the composite `owner_token` format (`address:subagent-id:session-uuid:lease_epoch`,
> = the design's `role:…` with `role` → the one-spine `address`). These are **NOT** "reused from the
> recovered manifest" — they were **absent** from the recovered control plane's code AND manifest,
> and present only as an uncoded phase-2 design (partially wired in `loop_supervisor.py`'s
> `--expected-owner-token` plumbing). v1 codes them into CAS for the first time (F-012/F-024 fix).
> *Genuinely NEW here:* `session_uuid` + `tmux_target` + `liveness_state` (no process-liveness field
> existed in either stream); `terminal_signal` + `signal_artifact_seen_at` (no per-node died/done
> signal existed — terminality was only a global `status: stopped`); `last_applied_seq` (the WAL
> replay watermark, §4.4); generic lifecycle `state` vocab replacing the reviewer-loop
> `builder/reviewer_1/reviewer_2` states; per-node `generation` replacing global `len(ledger)`.

### 3.3 Generic per-node lifecycle state machine

The recovered `KNOWN_STATES`/`ALLOWED_TRANSITIONS` encode reviewer-loop semantics
(`builder`/`reviewer_1_pending`/…). v1 keeps the **mechanism** (a static legality table +
the CAS legality gate, `cmd_transition` L1526) but replaces the **contents** with a generic
node lifecycle:

```
planned ──claim──▶ claimed ──spawn-ok──▶ spawning ──actor-open──▶ running
claimed ──release (admission-deny / E32-pin-fail)──▶ planned     (ROLLBACK; §6.1)
spawning ──actor-open-fails──▶ planned                           (ROLLBACK after claim→spawning; §6.1)
spawning ──unrecoverable-spawn-error──▶ failed                   (give up the slot, not retry)
running ──block──▶ blocked ──unblock──▶ running
running ──DONE──▶ done
running ──FAILED/DIED──▶ failed
{any non-terminal} ──reconcile-finds-dead──▶ dead     (reconcile-driven, not actor-driven)
{any non-terminal} ──daemon-stamped died_* (§3.6)──▶ failed   (reconcile-driven leaf-necro: the §3.6 table OVERRIDES the generic →dead edge for the leaf DIED_* classes — DIED_INFRA/DIED_METHODOLOGY resolve to lifecycle state `failed`; coordinator_died keeps `dead`)
running ──re-adopt(claim, expected_state=running)──▶ claimed   (RESUME a live address; §6.4 — fences the prior incarnation via lease_epoch bump)
dead    ──re-adopt(claim, expected_state=dead)──▶ claimed       (RESUME/necro a dead address; §6.4 / §5)
done | failed | dead = terminal
```

`ALLOWED_TRANSITIONS` is the static table; an illegal target is rejected before any write (the
recovered legality gate, reused verbatim in mechanism). **The rollback edges (`claimed → planned`,
`spawning → planned`, `spawning → failed`) are first-class members of the table** — without them
the §4.2 legality gate would reject the very claim-release the spawn chokepoint (§6.1) depends on,
leaking an un-reclaimable `claimed` slot (a worse F-024 than the duplicate it prevents). Every edge
the spawn chokepoint can traverse on failure is enumerated here so the gate permits it. **The re-adopt
edges (`running → claimed`, `dead → claimed`) are likewise first-class:** RESUME/necro (§6.4; WATCHDOG
ADOPT) is a `claim` variant whose CAS precondition is `expected_state ∈ {running, dead}` — NOT the
fresh-claim's `expected_state=planned`. The `claim` primitive therefore takes an `expected_state`
parameter (`planned` fresh | `running` resume-live | `dead` necro), CAS-guarded against whichever it is
given. Without these edges the legality gate would abort every adopt/resume on its precondition —
un-buildable as WATCHDOG §3/§5 require.

### 3.4 Merge of binding + deliverable (closes the two-registry split)

The recovered model kept **two** registries (`manifest` control-state + `WORKBOARD.yaml` stream
deliverables) reconciled by the validator across **two locks** — so no atomic two-file commit
existed. v1 **merges them into one per-node record keyed on the one-spine address.** The
workboard's free-form `stream_id` (`WS-001`) becomes the node-address (per one-spine). Control-state
and deliverable-state update in **one atomic transaction** (§4.4), not two locks reconciled later.

> **FORK — for user review.** Binding-ledger physical storage: **one keyed file vs one file per
> node-address.**
> - **Option A — single keyed file** (`binding-ledger.yaml`, the whole map): one atomic-replace
>   target ⇒ one write-lock ⇒ simplest CAS and simplest cross-node atomic commit. Con: every
>   mutation rewrites the whole map (fine at tens of nodes; the recovered model already rewrote the
>   whole manifest each commit).
> - **Option B — one file per node-address** (`<path>/.binding.yaml`): aligns the ledger key with
>   the one-spine filesystem path (node-address *is* a path); finer locks. Con: N lock domains
>   unless the run-scoped daemon serializes them all (which Option A in §2.4 does anyway), and
>   cross-node reconcile reads N files.
> - **Recommendation: Option A** (single keyed file) for v1. With the single-central daemon
>   (§2.4-A) there is one writer anyway, so the single-file model gives atomic whole-tree commits
>   for free and the simplest crash-atomicity story. Revisit if the tree grows past hundreds of
>   nodes.

### 3.5 Run-ledger event format (append-only journal)

`run-ledger.jsonl` — one JSON object per line, **global ordering**, append+fsync. This is the
**harness-level lifecycle/terminal-signal journal** written by the single-writer executor.
Distinct from per-project `log.md` (agent-written `STARTED/SUBMITTED/APPROVED/SENT-BACK`,
append-queue, `WORKSPACE-SCHEMA.md` §log.md) — they are **siblings, not the same file**: `log.md`
is project-domain history written by agents; the run-ledger is process-lifecycle history written
only by `harnessd`. (Whether terminal-signal entries are *also* mirrored into `log.md` is an open
seam — see §10.)

Every state-changing entry is the **WAL record for exactly one transition** and carries enough to
replay that transition deterministically (§4.4):

```json
{ "ts": "2026-06-05T10:14:00+03:00",
  "seq": 412,                       // monotonic global sequence (= the ordering AND the per-node watermark)
  "node_address": "payments/gateway/stripe-client#exec",
  "event": "spawned",
  "actor": "harnessd",              // executor is the only writer of this journal
  "crc32": "a1b2c3d4",              // content checksum (FORK-CRC); NO in-payload "len" — the byte length is the append_framed <byte-len> PREFIX (§4.4), not a field inside the json (self-referential len is circular)

  // --- the transition this WAL row commits (drives deterministic replay) ---
  "from_state": "spawning",         // pre-image state the CAS expected
  "to_state": "running",            // post-commit state
  "expected_generation": 6,         // pre-image generation the CAS checked against
  "generation": 7,                  // POST-commit generation (= expected_generation + 1)
  "lease_epoch": 3,
  "owner_token": "payments/gateway/stripe-client#exec:subagent-0a1b2c3d:3f2a9c81:3",  // post-commit token

  // --- the mutation payload (the fields this transition set on the binding) ---
  "binding_delta": { "session_uuid": "3f2a9c81-…", "model_used": "opus-5.0 / claude-code",
                     "liveness_state": "working", "state_entered_at": "2026-06-05T10:14:00+03:00" },

  "summary": "claimed slot + opened tmux actor on opus-5.0/claude-code",
  "artifacts": ["report.md"] }
```

**Replay is a deterministic re-apply, not a guess.** Recovery (§4.4 / §5.1) replays *only* events
whose `seq > binding.last_applied_seq` for that node. For each, it verifies the binding's current
`generation == expected_generation` (the pre-image the CAS checked); if so it applies `binding_delta`,
sets `state` to the record's authoritative `to_state` (and `node_address` to the record's
`node_address`) — the same authoritative-state rule the §4.2 transition applies on the commit side —
sets `generation` and `owner_token` to the post-commit values, and stamps `last_applied_seq = seq`
— all in the **same atomic-replace**. If the binding's generation already equals the event's
post-commit `generation` (the event already landed before the crash), the event is a no-op skip.
This makes "reflected in the binding ledger" a **checkable predicate** (`seq ≤ last_applied_seq`),
not prose. Some state-preserving rows carry replayable binding deltas; replay-neutral rows remain
only when a named recovery or behavioral-evidence consumer needs the decision/fault record. The
current exhaustive classification lives in `harnessd/wal_policy.py` and is
enforced for authored spelling completeness by `tests/test_wal_diet.py`.

**Event vocabulary** (closed set the run-ledger accepts), composed from the recovered ledger's
event taxonomy + the comms-protocol terminal signals + the F-017/stale-return classes:

- **lifecycle:** `node_planned`, `slot_claimed`, `spawned`, `state_transition`, `collapsed`,
  `necroed`, `resumed`.
- **lease / ownership:** `lease_renewed`, `ownership_replaced`, `stale_suspect_opened`,
  `recovery_probe_started`, `lease_recovered`, **`stale_return_ignored`** (a fenced actor returned
  after respawn — non-destructive de-authorization, F-012/F-024).
- **terminal-signal (first-class):** `signal_DONE`, `signal_FAILED`, `signal_ESCALATED`
  (the comms-protocol §terminal-signal contract — the executor journals the *fact-of-being-sent*;
  the watchdog's sign-off check reads **this journal**, not the transient bus nudge — see the
  durable write-path below), plus daemon-stamped death classes `coordinator_died`,
  `died_infrastructure`, `died_methodology` (F-017: infra-vs-methodology are *distinct* terminal
  classes; recovery branches on them).
- **completion:** `coordinator_completed` (the durable harness-stamped row the parent's kill keys
  off — gap-review #2 cluster-① half). Under the gate-owned-forwarding lifecycle
  (`GATE-LIFECYCLE.md`), a producing `#exec` seat's completion artifact is only a candidate
  submission; the parent-facing completion row is emitted only after the node's review gate passes.

**Terminal-signal write-path — durable artifact, not a fragile in-process call (closes the
gap-review #2 transport gap).** The signing agent runs in a tmux pane; it **cannot** call the
in-process executor, and routing its terminal signal *only* over the best-effort bus would mean a
**dropped nudge loses the durable row entirely** — the exact failure the design distrusts. So the
signal is durable-by-write, journaled-by-sweep:

1. **Agent writes the signal to a durable per-seat artifact** — `<node-dir>/.signal.<seat>.json`
   (the canonical per-seat path, `harnessd/addressing.signal_path` — seat-qualified so the L5/L5+
   pair sharing one node dir don't clobber each other's sign-off)
   `{signal: DONE|FAILED, ts, owner_token, evidence}` (an atomic tmp+rename the agent
   does as its last act, alongside `report.md`). Gate lifecycle v4 keeps this write-path but changes
   what a producing seat's `DONE` means at a gated boundary: candidate submitted to review, not
   parent-visible completion. The explicit final signal vocabulary (`SUBMITTED` / `PASS` / `BOUNCE`
   versus seat-sensitive `DONE`) is tracked in `GATE-LIFECYCLE.md` and must be resolved before the
   code path is changed. The `owner_token` is copied **verbatim** from the
   `<node-dir>/.sign-off.<seat>.json` handshake the chokepoint seeds strictly post-claim /
   pre-open (the only agent-visible channel for the token — the brief payload omits it, brief.md
   may be pre-authored before the claim mints it, and the pane env is contractually the 4
   isolation vars). This is the durable fact; the bus nudge is only an *optional fast-path wake*.
   (Accepted v1 window: a fenced prior incarnation that re-reads the *refreshed* handshake could
   sign with the new token — the respawn kills the old pane, so the window is negligible.)
   Blocked work uses a canonical `needs_answer` question and parks without a terminal signal.
   Legacy `ESCALATED` signal artifacts remain valid read-side inputs for older seats and replay;
   the sweep behavior for that compatibility row remains specified below.
2. **The reconcile sweep detects the artifact and the executor journals from the durable read** —
   each tick, reconcile checks for a `.signal.<seat>.json` whose harness-observed artifact identity
   differs from the binding's `signal_artifact_seen_at`; on finding one it stamps
   `terminal_signal` + appends the `signal_*` run-ledger row (validating the artifact's
   `owner_token` against the live binding's `owner_token` to fence a stale-actor signal — the
   composite token embeds session_uuid AND lease_epoch (§8), so even a re-claimed/rolled-back
   incarnation that KEPT its session_uuid is fenced). A dropped bus nudge therefore only **delays
   journaling to the next sweep**, never loses it. The bus nudge, if it arrives, just triggers an
   *immediate* sweep of that node instead of waiting for the timer.
   Coordinator seats with live descendants still run this artifact check. The live-descendant guard
   defers only collapse on `DONE`/`FAILED`; it does not suppress a legacy `ESCALATED` artifact,
   because compatibility reads must preserve an older coordinator's question while lower seats
   are still running.
3. **For legacy `signal_ESCALATED`, the successful journal also relays the forward wake to the parent** —
   the harness appends a `child_escalated` pointer line to the parent's inbox, best-effort and
   idempotent because it fires only on the journal-once artifact transition. This is the symmetric
   wake leg to `child_collapsed` and the human `answer_posted` relay.

The watchdog terminal/liveness body applies to actor-bearing bindings only. `running` and `blocked`
nodes have an opened actor and therefore must carry the spawn↔detector `transcript_path` contract.
`planned`, `claimed`, and `spawning` are controlled by spawn/redrive/reconcile and may legitimately
lack a transcript path before the actor opens; they are skipped by the watchdog body rather than
being treated as `MissingTranscriptPath` faults.

The binding carries `signal_artifact_seen_at` so the sweep journals each artifact **exactly once**
(idempotent; matches "terminal states reconcile exactly once"). The value is harness-owned signal
artifact identity (content hash plus filesystem write identity), never the agent-authored `ts`.

**Edge-triggered append (anti-spam, reused verbatim).** Steady-state healthy reconcile sweeps do
**not** append. Replay-neutral ordinary turn observations stay in their existing node-local durable
home, and other observations use the status sidecar (`runtime.json` / `.harnessd/status.json`).
The WAL retains replay-changing rows and the classified decision/fault rows required by named
recovery or evidence consumers. Terminal states reconcile **exactly once**. Invalid candidate
checkpoints **fail closed** (validate-before-commit).

> **Adapted from run-ledger.jsonl + watchdog.py: reused / changed.** *Reused:* append-only
> JSONL one-object-per-line + append+fsync; per-entry `{ts, event, actor, state, summary,
> artifacts?}`; the edge-triggered "append only on condition change, sidecar every poll, terminal
> reconciled once, invalid fails closed" idempotency rule (WATCHDOG.md L120–128); the
> ownership-lifecycle event names from `watchdog-design-01.md`. *Changed / NEW:* every entry is
> keyed by `node_address`; a global `seq` is added (the recovered ledger had no explicit global
> sequence — it relied on file order, which v1 keeps but names); explicit terminal-signal events
> (`signal_DONE/FAILED/ESCALATED`) and death-class events (`coordinator_died`, `died_*`) become
> first-class (the recovered model encoded terminality only as `state: stopped`); `iteration` is
> dropped (reviewer-loop-specific).

### 3.6 Terminal vocabulary — the one normative mapping table

Three layers each have their own word for "ended," and a builder must be able to translate among
them to write the sign-off check. This table is **normative**; the layers are deliberately distinct
(different granularity), and this is the only place the translation is defined.

| Signal-artifact tag (current authored set plus legacy read compatibility) | `terminal_signal` (binding) | run-ledger `event` | lifecycle `state` | Node collapsed? |
|---|---|---|---|---|
| `DONE` | `DONE` | `signal_DONE` | `done` / candidate-submitted at gated `#exec` boundary | yes for ungated terminal completion; **no parent-facing completion for a gated producing seat until review PASS** |
| `FAILED` | `FAILED` | `signal_FAILED` | `failed` | yes (parent respawns/escalates) |
| `ESCALATED` *(legacy read compatibility only)* | `ESCALATED` | `signal_ESCALATED` | **stays `running`** (non-terminal) | **NO — keeps context, waits** |
| *(none — daemon-stamped)* | `DIED_INFRA` | `died_infrastructure` | `failed` | per ② recovery policy |
| *(none — daemon-stamped)* | `DIED_METHODOLOGY` | `died_methodology` | `failed` | per ② recovery policy |
| *(none — daemon-stamped)* | `FENCED` | `stale_return_ignored` | *(unchanged — stale actor only)* | no (live owner unaffected) |
| *(none — daemon-stamped, coordinator)* | *(none)* | `coordinator_died` | `dead` | recovered-as-orphan, not collapsed (§5.4) |
| *(none — daemon-stamped, coordinator)* | `DONE` | `coordinator_completed` | `done` | yes |

Two rules a builder must encode:

- **Legacy ESCALATED is the asymmetric compatibility case:** `terminal_signal` is **set** but `state` stays
  **`running`** and the node is **not collapsed** — the agent keeps context and waits for the
  answer-round-trip (comms-protocol). The sign-off check ("is there a terminal-signal event for
  this node?") is satisfied, yet the node is *not* terminal. Any code that assumes
  `terminal_signal != null ⇒ collapse` is wrong; gate collapse on `state ∈ {done, failed, dead}`.
- **The spelling split is deliberate, not an accident:** the binding field uses SCREAMING
  `DIED_INFRA` (a value); the run-ledger uses snake `died_infrastructure` (an event name); the
  lifecycle uses lowercase `dead`/`failed` (a state). They are three layers, and this table is the
  exact translation — do not "unify" them by renaming; translate through here.

> **in_flight release-DECREMENT rides the terminal transaction (④'s count, ①'s hook).** The
> single-writer terminal write — the executor write that stamps `terminal_signal` on a
> `done`/`failed`/`dead` collapse, AND the §5.4 necro / §5.1 reconcile-finds-dead path — carries
> cluster ④'s symmetric in_flight **release-decrement**. It rides the **existing** single-writer
> terminal write (no new writer, no second mutator): same atomic-replace, crash-safe via
> `last_applied_seq`, exactly symmetric to the §6.1 claim-INCREMENT seat. ④ owns the slot COUNT; ①
> only acknowledges and provides this reserved decrement hook so ④'s admission gate is a balanced
> counter, not an increment-only ratchet. (The `ESCALATED` row is **not** a release — it stays
> `running`, holds its slot, and waits for the answer round-trip.)

---

## 4. The Single-Writer Executor + Atomicity Model

This section closes the **lost `state-ledger-races` lens** explicitly: *who may write, the
ordering, and crash-atomicity* (gap-review line 105: *"multiple events stamping the same ledger;
one-writer discipline"*).

### 4.1 Who may write — exactly one writer

**Only `harnessd` mutates durable control-plane state.** All mutation flows through one funnel
(the descendant of `commit_mutation`, L1264). Even the **watchdog and the detector write through
the executor** — they never edit the binding ledger directly; they call the executor's
checkpoint/transition primitive, which mutates only their slice and appends one run-ledger row
(the recovered "observer writes through the executor" pattern — `watchdog.py` shells
`control_plane.py watchdog-checkpoint`, never touches the manifest). In the single-writer daemon model these
are in-process calls into the single executor, not subprocess shell-outs, but the discipline is
identical: **one writer, no second mutator.**

> **Adapted: reused.** The "observer writes through the executor" rule from `watchdog.py` /
> `watchdog-design-01.md` (three-writer discipline: manifest = truth, ledger = history, transition
> = only state-changer). Reused as-is; collapsed from cross-process shell-out to in-process call.

### 4.2 The transition primitive — CAS-guarded, lock-serialized, validate-before-commit

The single state-changing primitive, lifted from `cmd_transition` (L1505–1610) and generalized
per-node:

```
transition(node_address, expected_state, expected_generation, expected_owner_token, target_state, …):
    with EXCLUSIVE serialization-domain lock:          # §4.3
        binding  = read_binding(node_address)
        # --- CAS preconditions (ALL checked before ANY mutation) ---
        if binding.state      != expected_state:        abort  # recovered L1511
        if binding.generation != expected_generation:   abort  # per-node generation, NOT len(ledger)
        if binding.owner_token != expected_owner_token:  abort  # FENCING (new) — reject stale owner
        if target_state not in ALLOWED_TRANSITIONS[binding.state]: abort   # legality gate (recovered L1526)
        # --- build candidate, validate, commit ---
        candidate = deepcopy(binding); mutate(candidate); candidate.generation += 1
        entry = build_run_ledger_entry(...)
        errors, warnings = validate(candidate, ledger + [entry])      # recovered validate() L618
        if errors: abort   # validate-before-commit: NOTHING written
        commit(candidate, entry)                                       # §4.4 ordering
```

**Three CAS preconditions** (the F-024 fix is here):

1. `expected_state` — the recovered expected-state guard (L1511).
2. `expected_generation` — the recovered ledger-generation guard (L1516), **but per-node**. The
   recovered guard used **global** `len(ledger)`; in a tree with one shared append-only ledger,
   length is a global counter any node's append bumps, so it cannot fence a single node. v1 uses
   a **per-node `generation` field** bumped on every commit to that node. (open Q resolved: the
   shared run-ledger stays single-file for global ordering; per-node CAS uses per-node generation.)
3. `expected_owner_token` — **NEW fencing precondition.** A stale actor presents its old token;
   the live binding holds the new token (higher `lease_epoch`); mismatch ⇒ abort. This is what
   makes a stale owner's action *fail*, not merely get reconciled later. Because `owner_token`
   embeds `lease_epoch` (`address:subagent-id:session-uuid:lease_epoch`), comparing tokens
   compares epochs — the token is **self-fencing**.

> **Adapted from cmd_transition: reused / changed.** *Reused:* the read→check-preconditions→
> deepcopy→mutate→validate→commit-inside-the-exclusive-lock skeleton (L1505–1610); the
> precondition-accumulate-then-abort-before-write pattern; the static `ALLOWED_TRANSITIONS`
> legality gate; the pure `validate()` returning `(errors, warnings)` with errors-block/
> warnings-allow. *Changed:* added the third CAS precondition (`expected_owner_token`); per-node
> `generation` replaces global `len(ledger)`; per-node binding replaces the single manifest;
> `lease_epoch`/`owner_token` rotate **in the same transaction** as actor-changing transitions
> (F-012 fix — no window where state advanced but ownership didn't).

**Committed whole-ledger validation is a separate read projection.**
`harnessctl validate` does not have an about-to-commit lifecycle candidate; it
reads durable checkpoints after any of the single writer's three row shapes.
For each binding, the read path uses the same primary-plus-related binding-effect
projection as recovery and selects the binding-applying effect at
`last_applied_seq`. A real lifecycle state change and every gate event still run
through the strict validate-before-commit contract above. Other committed
binding effects are admitted only when their state, generation relation,
owner/lease identity, watermark, and every `binding_delta` value match the
checkpoint; a generation-preserving own-slice effect also cannot write reserved
binding fields.
Journal-only evidence carries null generations and never advances the
watermark, so a later journal row cannot obscure the applied transition.
A fresh generation-zero `planned` registration may have no binding effect
because re-registration seeds the global WAL watermark to fence older
incarnations; that one structural shape is read directly. The scanner never
falls back to another node's WAL tail. This distinction is read-only:
`validate(candidate, wal_tail)` and every write-side admission rule remain
unchanged.

### 4.3 Lock discipline — one serialization domain

The recovered model had **two** advisory `fcntl.flock` domains (`.control-plane.lock` +
`.workboard.lock`) — control-state and deliverable-state could not be committed atomically
together. v1 has **one** exclusive serialization domain owned by the live run daemon. Because there
is one writer (the daemon) and one lock, every mutation is a read→validate→commit fully inside the
lock. Reads (show/next/reconcile-inspect) take a shared lock.

**Two locks, two roles.** `.harnessd.lock` is the per-mutation serialization domain described
here: acquired and released inside every executor mutator, AND taken explicitly by reconcile's
boot-replay checkpoint (§5.1 step 1) around its read→replay→write critical section — so the
checkpoint's `write_binding(..., _lock_held=True)` is true by fact, not by flag.
`.harnessd.instance.lock` is the lifetime single-instance guard (§2.3): acquired non-blocking at
boot, held until the process exits. Both are advisory `fcntl.flock` files under `RUNTIME_ROOT`.
They never contend with each other — and they must stay separate files: a lifetime hold of the
per-mutation file would deadlock every executor write, because `flock` conflicts across open file
descriptions even within one process (review SWCAS-02; fork decided 2026-06-10).

**No CLI read-modify-replace of the shared map — all mutation routes through the daemon.** With the
single-keyed binding file (§3.4-A), a cooperating CLI that atomic-replaced the *whole map* to change
one node would silently clobber a concurrent daemon write to a *different* node — and per-node
`generation` CAS does **not** guard that cross-node clobber (it guards the node the CLI changed, not
the bystander it overwrote). So the rule is: **CLIs are clients, not writers.** A `harnessctl`
command sends a request to the running daemon (over a local socket / fifo / the same executor
entrypoint), and the daemon performs the mutation inside the one lock. No external process ever
read-modify-replaces `binding-ledger.yaml` directly. (Option 3.4-B per-node files would also remove
the cross-node clobber, by giving each node its own replace target — noted in the §3.4 fork.)

**Caveat (honest):** the lock is still *advisory and process-local* to `harnessd` — it fences the
daemon's own concurrent operations, **not** a rogue process that ignores both the lock and the
route-through-daemon rule. The real protection against a rogue/stale *actor* is **fencing** (§8),
not the lock; the lock serializes the *daemon's* writes.

### 4.4 Crash-atomicity ordering — intent-first

The recovered `commit_mutation` (L1264) did `save_manifest` → `append_ledger` → `save_continuation`
as three separate fsync'd ops; a crash mid-funnel leaves the manifest **ahead of** the ledger
(manifest replaced, ledger not yet appended), forcing recovery to tolerate manifest-newer-than-ledger.
v1 fixes the ordering to make the **append-only run-ledger the single source of truth**:

```
commit(candidate_binding, entry):
    # entry carries: seq, from/to_state, expected_generation, post-commit generation+owner_token, binding_delta (§3.5)
    candidate_binding.last_applied_seq = entry.seq         # stamp the watermark IN the checkpoint
    1. append_ledger(entry)                  # append+fsync the INTENT/EVENT FIRST (framed line, §4.4 box)
    2. atomic_replace(binding_ledger, candidate_binding)   # tmp + fsync + os.replace (incl. last_applied_seq)
    3. regenerate derived handoff (continuation/next-action packet)   # derived, never hand-edited
```

**Why intent-first.** Step 1 is append-only, and a crash can only ever corrupt the **final** line
(the one being appended). If the daemon crashes between step 1 and step 2, recovery sees a
run-ledger event with **no corresponding binding update** and **re-applies it deterministically**
(see the replay watermark + pre-image rule below). If it crashes before step 1, nothing happened —
the actor's CAS will simply be retried. This makes reconcile-on-restart's job: *replay any
run-ledger event whose `seq` is greater than the binding's `last_applied_seq` for that node.* The
run-ledger is the WAL; the binding ledger is the checkpoint.

> **CORRECTION to the recovered code — torn-tail tolerance is NOT inherited, it is ADDED in v1.**
> The recovered `load_ledger` (control_plane.py L209–225) skips only **empty** lines (L216–217);
> on **any** `json.JSONDecodeError` it **raises** `ValueError` (L220–221) and on a non-dict it
> raises (L222–223) — so a torn final WAL line makes the *entire* run-ledger un-loadable, which
> would brick the very boot-recovery path that replays it (§5.1 step 1). v1 **changes** load so a
> crash mid-append survives:
>
> 1. **Frame every record.** Each WAL line is written as `<len>\t<json>\n` where `<len>` is the
>    byte length of the JSON payload (a self-describing length frame; a CRC32 checksum field MAY be
>    added inside the JSON for defence-in-depth). Write-path: format the full line, `write()`,
>    `os.fsync()` — never a partial flush.
> 2. **Recover by truncating only a torn FINAL line.** On load, parse line by line. A length/JSON
>    mismatch or `JSONDecodeError` on the **last** line ⇒ treat it as a torn append: **truncate it
>    and continue** (the binding atomic-replace for that event never landed, so its effect was never
>    committed — dropping the torn intent is correct). A decode/frame error on **any non-final
>    line** ⇒ **fail closed** (corruption-halt: a non-tail corruption is not a crash artifact and
>    must not be silently swallowed).
> 3. **Anchor recovery on the binding atomic-replace, treat the ledger tail as advisory.** The
>    binding ledger (atomic `os.replace`) is the authoritative checkpoint; the WAL tail is replayed
>    only to roll *forward* any committed-intent-not-yet-checkpointed event. A dropped torn tail
>    therefore loses at most the one uncommitted intent, never committed state.

> **Adapted from commit_mutation: reused / changed.** *Reused:* atomic-replace
> (tmp+fsync+os.replace, `save_manifest` L191–197) for the binding ledger; append+fsync JSONL
> (`append_ledger` L241–245) for the run-ledger; the single commit funnel as the one auditable
> write path; derived-handoff-regenerated-on-every-commit (`render_continuation`, never
> hand-edited — F-035). *Changed:* **reversed the ordering** — ledger-append FIRST (intent/WAL),
> then binding atomic-replace, so a crash yields ledger-ahead-of-binding (replayable) instead of
> binding-ahead-of-ledger (ambiguous). Atomic-replace now covers the binding ledger and the
> run-ledger goes through the same funnel — closing the recovered model's "watchdog wrote
> status.json with plain write_text, only control_plane did atomic-replace" gap: **no plain
> `write_text` for any control-plane state that recovery reads — every such write goes through the
> executor's atomic path.**

> **The status sidecar is the ONE deliberate carve-out (not durable control state).**
> `runtime.json` / `.harnessd/status.json` (§2.3 — `{pid, started_at, last_tick_at,
> last_reconcile_at, incarnation, …}`) is a **best-effort, lock-free liveness surface** for an
> external pinger and `service status` reads. It is written **every poll**, so it CANNOT take the
> exclusive serialization lock (that would serialize a non-event against real mutations every tick)
> and is NOT part of the durable journal (edge-triggered append, §3.5). The two claims reconcile by
> scope: the sidecar (a) uses its **own** atomic tmp+rename (so a crash never leaves it torn) but
> (b) takes **no** serialization lock, and (c) **recovery NEVER trusts it for control state** — all
> control state is reconstructed from the binding ledger + WAL. It is a status mirror, not a source
> of truth.

### 4.5 Executor command surface (per-node generalization of the recovered six commands)

Read-only (shared lock): `show <node>`, `next <node>`, `validate`, `reconcile-inspect`.
Mutating (exclusive lock): `transition`, `heartbeat`, `release-lease`, `watchdog-checkpoint`,
and the **NEW** `claim` (the spawn-chokepoint slot-claim, §6) and `reconcile-apply` (§5).
`heartbeat`/`release-lease`/`watchdog-checkpoint` still blind-overwrite their own slice under the
lock **but now also present `owner_token`** so a stale owner cannot heartbeat over a live one — the
recovered model let `cmd_heartbeat` (L1440) blindly set `owner` with no epoch check; v1 requires
the token on every mutator, not just `transition`.

### 4.6 Single canonical clock (F-019)

All lease-freshness math (`now − last_heartbeat_at`, `now − last_progress_at`) uses **one canonical
clock: UTC**, and **all timestamps are stored timezone-aware ISO-8601**. The recovered incident
F-019 manufactured a false 3-hour-stale diagnosis by comparing UTC trace timestamps against
local-wall-clock supervision. The reconcile loop and every binding timestamp use UTC; rendering to
local time is a display concern only.

---

## 5. Reconciliation (on-restart + continuous)

Reconciliation keeps **actual (tmux) == recorded (binding ledger).** It is the *accounting* layer.
The recovery **policy** (what to do with a stale_suspect) is cluster ②; v1 reconcile does the
**mechanical** part: detect divergence, apply the unambiguous resolutions, escalate the rest.

### 5.1 Reconcile-on-restart (genesis-recovery, runs once per boot)

```
on daemon boot, with the instance lock held (§2.3; step 1's replay checkpoint takes the
per-mutation .harnessd.lock itself, and every later mutation re-takes it — §4.3):
  1. load run-ledger with torn-tail tolerance (§4.4 box); replay WAL: for each event with
       seq > binding.last_applied_seq[node], deterministically re-apply it (verify pre-image
       generation, apply binding_delta, set state to the record's authoritative to_state +
       node_address to the record's node_address — the §4.2 commit-side rule, mirrored —
       stamp last_applied_seq — §3.5/§4.4)
  2. list live tmux targets (tmux list-sessions/panes → set of live tmux_targets + pane_pids)
  3. for each binding:
       recorded-alive & tmux-present & session-uuid matches  → ADOPT (resume ownership, renew lease)
       recorded-alive & tmux-absent (or pane_dead), LEAF     → owned-but-dead → necro: mark dead,
                                                                stamp died_* terminal_signal, bump
                                                                lease_epoch, append run-ledger event
       recorded-alive & tmux-absent (or pane_dead), COORD    → owned-but-dead → mark dead, stamp
                                                                coordinator_died, bump lease_epoch,
                                                                append event, and ESCALATE (recover-
                                                                vs-reap is ②, §5.4 — NOT decided here)
       recorded-terminal                                     → leave (reconcile-once; no action)
  4. tmux-present & NO binding (alive-but-unowned)            → ESCALATE (orphan; record, hand to
                                                                cluster-② policy / L1)
  5. resume-not-double-spawn L1 (§7): if L1's binding is non-terminal and its tmux is gone,
     resume L1 from its binding; if its tmux is alive, ADOPT — never spawn a second L1.
```

### 5.2 Continuous reconciliation (the v1 loop)

The same sweep on a timer (the watchdog tick — one loop, one sweep, matches single-central §2.4-A).
Each tick: re-derive `liveness_state` per node from floor signals — *examples the cluster-② detector
MAY fuse, not the v1 floor*: transcript-JSONL growth, tmux `window_activity`/`pane_dead`, node-file
mtimes, `pane_pid` CPU. The detector sits behind the stable
`liveness(node) → {working|waiting|idle|dead, last_progress}` interface (the canonical enum, §3.2),
written through the executor; **which signals it fuses and how is cluster-② internals (§1.4)** —
cluster ① owns only the interface and the field. Apply the same divergence resolutions as §5.1.
**Owned-but-dead → reaped/necro'd (leaf) or escalated (coordinator, §5.4); alive-but-unowned →
escalated.** Edge-triggered: only state/condition *changes* append to the run-ledger.

### 5.3 The v1 boundary vs the deferred reconciliation controller

> **FORK — for user review (boundary, not a true fork; recommendation stated).** runtime-decisions
> §3 defers the *full desired-vs-actual reconciliation controller* (trigger: *"orphan/ghost cases
> the watchdog process-check misses"*). The v1 line:
> - **v1 IN:** the per-node read-tmux-vs-ledger divergence detection + the two unambiguous
>   resolutions (owned-but-dead → necro; recorded-terminal → leave) + **escalate** everything
>   ambiguous (alive-but-unowned orphan, dead-pid-but-live-children).
> - **DEFERRED to ②:** *automatically reconciling* the ambiguous cases (adopting an orphan into a
>   new owner, GC'ing ghost subtrees, the full controller). v1 **escalates** these rather than
>   auto-resolving them — which is the correct conservative posture for the commissioning phase
>   (*"don't auto-recover past a break — freeze and examine"*, runtime-decisions §5).
> - **Recommendation:** ship the detect+escalate loop in v1; let cluster ② add auto-resolution
>   behind the same escalation seat. This is a one-site add later (the escalation handler), which
>   is exactly why deferring it is safe under the sufficiency-cut test.

### 5.4 Two reapers — keep them distinct

- **Liveness reap (daemon-hosted, v1) — the v1 mechanical action is detect + escalate, not
  auto-recover.** The reconcile loop necros an owned-but-dead session (evidence-based: tmux gone /
  `pane_dead`), stamps a `died_*` terminal signal, never blind-kills. Coordinator vs leaf
  asymmetry, drawn at the **mechanism** level cluster ① owns:
    - a dead **leaf** (L5/L5+) is reaped → `FAILED` (the unambiguous resolution);
    - a dead **coordinator** is marked **owned-but-dead**, the daemon stamps `coordinator_died`
      (which fires on `pane_pid` death *regardless of subtree activity* — the cheap orphan-killer
      the subtree-gating misses, gap-review #2), and the daemon **ESCALATES** it. The
      **recover-vs-reap CHOICE** (adopt the orphan from the ledger vs give it up) is the cluster-②
      recovery *policy* reading the `coordinator_died` signal — **NOT** decided here (it is a §1.4
      DEFER). Cluster ① detects + escalates; ② chooses. This keeps §5.4 consistent with the §5.3
      detect-and-escalate boundary and avoids pre-committing the recovery policy.
  A coordinator is idle-actionable **only when its whole subtree is also quiet** (per-node
  live-descendant roll-up — visibility the daemon computes by prefix scan; this roll-up is why
  `liveness_state` must keep `waiting` distinct from `idle`, §3.2).
  **in_flight release-DECREMENT seat (④).** Every necro/collapse here is a single-writer terminal
  write (stamps `died_*`/`FAILED` + appends the run-ledger row), so it is also the point that carries
  cluster ④'s in_flight **release-decrement** — symmetric to the §6.1 claim-increment, riding the
  existing terminal write (no new writer; crash-safe via `last_applied_seq`; §3.6). The leaf-reap and
  the coordinator-died necro both release the slot the §6.1 claim reserved.
- **2-week resurrection GC (separate infra, NOT the daemon).** Collapse-on-finish (G37) holds a
  node's state resurrectable for 2 weeks keyed by its stable address; a separate lifecycle reaper
  GCs it after the window. **The daemon does not host this.** Do not conflate the evidence-based
  liveness reap with the time-based 2w garbage-collector.

> **Adapted: reused / changed.** *Reused:* the recovered/lifecycle model of evidence-based reap
> (never blind-kill), the coordinator-vs-leaf **asymmetry** (here drawn at the mechanism level:
> leaf-reap vs coordinator-detect-and-escalate; the recover-vs-reap policy is ②'s), live-descendant
> roll-up.
> *Changed / NEW:* there was **no** tmux↔ledger reconcile loop in the recovered code at all
> (liveness was inferred only from heartbeat-age, nothing checked actual tmux) — the entire
> reconcile sweep, the orphan escalation, and the `coordinator_died` process-death event are new
> in cluster ①.

---

## 6. The Single Spawn Chokepoint

One spawn path. It boots a pinned Claude Code (or Codex) actor in a detached tmux session, in role,
and — critically — **claims the slot in control-plane state BEFORE opening the actor** (the F-024
structural fix).

### 6.1 Claim-before-spawn (the F-024 fix, headline)

F-024: the recovered CAS guarded `transition` but `work_scoped_agent.py spawn` bypassed
control-plane state entirely, so a stale session double-spawned a duplicate actor. *"Spawning is
side-effecting and cannot be undone by a rejected transition. If the spawn happens before the
transition, the guard fires too late."* The fix makes spawn a **CAS-guarded transition into a
claimed state that must succeed before the actor opens:**

```
spawn(node_address, expected_state, expected_generation, expected_owner_token, level_config):
    # STEP 0 — PAUSE-SUBTREE READ-POINT (③'s human-control primitive; ① seats the enforcement here).
    #   Refuse to launch a child under a paused subtree: address-prefix check — if THIS node OR any
    #   ancestor binding has paused_at != null, ABORT before claiming. A paused subtree admits no new
    #   children. (Set/cleared only by the human control surface via the single writer — §3.2 / ③ §5.3.)
    if any(b.paused_at is not None for b in ancestors_inclusive(node_address)):  return  # paused — no spawn

    # STEP 1 — CLAIM (CAS-guarded transition, §4.2). Atomic. Fails if a concurrent/stale claim won.
    #   This claim seat also reserves the in_flight CLAIM-INCREMENT for cluster ④ (④ owns the slot
    #   COUNT; ① provides the seat — no new writer). Symmetric to the terminal release-DECREMENT
    #   (§3.6 / §5.4) so ④'s admission gate can't be an increment-only ratchet.
    claim = transition(node_address, expected_state=planned, target_state=claimed,
                       expected_generation, expected_owner_token,
                       new_lease_epoch = old+1, new_owner_token = mint(...))   # §8
    if claim aborted:  return  # someone else already claimed this slot — NO actor opened. F-024 closed.

    # --- ADMISSION SEAT (cluster ④ wedges here, between claim-accepted and actor-open) ---

    # STEP 2 — ADAPTER: read level config, assemble the runtime-NEUTRAL brief INCLUDING its
    #   load-manifest ("Identity — Load These Documents": broader protocol docs + referenced design
    #   docs the child READS at boot, per role_variant), pick runtime adapter; Claude-Code composes
    #   --system-prompt-file as <node-workspace>/.identity-prompt.md from the shared prompt base plus
    #   the selected identity trio (§6.2).
    # STEP 3 — confirm model+runtime pinned (E32) BEFORE the child runs; on failure → escalate, RELEASE claim
    # STEP 4 — open the tmux actor (§6.2); record session_uuid + model_used into the binding (one writer)
    # STEP 5 — transition claimed → spawning → running as the actor confirms boot
```

The claim is a **distinct pre-step** so admission control (cluster ④) can wedge between
claim-accepted and actor-open **without re-opening the CAS**. If admission denies, or the adapter
cannot pin the configured model/runtime (E32), or the actor fails to open, the chokepoint
**releases the claim** (transition `claimed → planned`, bump epoch) — a FAILED claim is rolled back
atomically so the slot is reclaimable. (open Q resolved: crash-atomicity of a failed claim = the
release is itself a CAS-guarded transition, replayable via the WAL.)

**Pre-claim process scheduling.** L2-owned L3 workstreams have a semantic scheduler before STEP 1:
`register_and_spawn_child` may register an L3 as `state=planned` with
`admission_state=waiting_on_sibling`, `schedule_policy=serial_l3_workstreams`, and
`waiting_on_sibling=<previous L3 exec>`. No actor opens and no claim is taken for that node. The daemon
planned-spawn redrive releases the wait through the single writer when the predecessor's durable
`gate_state=gate_passed`, then the normal claim-before-spawn path runs. If the predecessor reaches a
non-passed terminal state, or a parent-visible non-passed gate state such as `gate_failed` or
`gate_escalated`, the waiting successor becomes `admission_state=blocked_on_sibling` and the daemon
wakes the L2 parent with a `serial_admission_blocked` pointer. The successor remains planned and can
still release if the predecessor later passes; L2 owns retry, resequencing, cancellation, or
escalation. This is process ordering, not a runtime capacity cap: lower L4/L5 fanout remains
unconstrained unless a separate capacity gate is later justified by measured pressure.

**Two seats this pre-step provides.** (1) **Pause-subtree (③):** STEP 0 is the enforcing read-point
for the human-control pause primitive (③ §5.3) — the chokepoint **refuses** to launch a child under a
paused subtree (address-prefix check over the node + its ancestors). ① only seats the read-point; ③'s
human control surface sets/clears `paused_at` through the single writer (§3.2). (2) **in_flight
CLAIM-increment (④):** STEP 1's accepted claim is the seat where cluster ④'s in_flight slot count is
**incremented**. ④ owns the COUNT; ① provides the seat (no new writer). This increment is symmetric to
the **release-DECREMENT** carried on the terminal transaction (§3.6 / §5.4) — the two seats are a
matched pair so ④'s gate is a balanced counter, not an increment-only ratchet that leaks slots.

> **Adapted: NEW (the headline fix).** The recovered code had **no spawn command at all** and only
> `transition` was CAS-guarded. Claim-before-spawn is net-new: it extends the *same* CAS
> precondition pattern (§4.2) to the spawn path, which F-024 left open. F-024 was OPEN in the prior
> art (documented as discipline, never code-fixed); this makes it **structural**.

### 6.2 In-role boot — the H40 recipe (Claude-Code adapter)

The Claude-Code adapter boots the **pinned** binary with `--system-prompt-file` pointed at the
per-spawn composed identity bundle (`<node-workspace>/.identity-prompt.md`). That file begins with
`operational/shared/system-prompt.md` (the H40 shared minimal prompt), then flattens the selected
level identity trio (`soul.md`, `role.md`, `config.md`) under provenance headers. This is the LR-4
identity-autoload cure: identity acquisition no longer depends on agent diligence after boot. The
broader protocol and reference documents named in the spawn brief's load-manifest remain read-in-place
documents under the read-allowed harness graph; only the identity trio is in the boot prompt. The
role-bundle delivery is specified in `design/ROLE-RESOLUTION.md`. Concrete invocation:

- **Binary:** `.cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe`, version pinned
  `2.1.152`. The chokepoint **verifies the binary version/hash at spawn** (do not trust
  `DISABLE_AUTOUPDATER` alone — PINNED-CC.md notes it was not found in the binary strings; the real
  pin is the npm version + isolated prefix).
- **Flag:** `--system-prompt-file <node-workspace>/.identity-prompt.md` (REPLACES base block 2, keeps
  the shared prompt as the first section, keeps the 24-tool set, works interactively,
  OAuth-compatible). The composed file is generated by the adapter from the shared prompt plus the
  selected `role_variant`; it is not a bare per-level role path. The shared prompt and identity docs
  must be **read-allowed** to the adapter at spawn. **Do NOT** use
  `--append-system-prompt` (keeps full framing), `--agents`/`--agent` (does not inject persona),
  or `--bare` (reads auth strictly from `ANTHROPIC_API_KEY`, errors `Invalid API key` on an OAuth
  subscription token — a latent foot-gun guarded below).
- **Isolation env (exact set):** `CLAUDE_CONFIG_DIR=$HARNESS/.cc-pinned/config` (clean config — no
  inherited hooks/MCP/injections), `CLAUDE_CODE_OAUTH_TOKEN` (read via token-file `$(cat …)` /
  `_FILE_DESCRIPTOR` so the literal credential never lands in the pane or transcript),
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1`.
- **tmux:** create a **detached** session whose name is **derived from the one-spine address**
  (`harness:<collapsed-address>`), so the reconcile loop can match tmux↔ledger across respawn.
  First-boot trust/permission acceptance must be **deterministic for an unattended session**
  (pre-seed trust state in `CLAUDE_CONFIG_DIR` or use a non-interactive permission mode) — **not** a
  send-keys race against the trust dialog (the H40 experiment auto-accepted interactively; the
  production chokepoint must not). Pane capture is the readback channel for the reconcile/observe
  loop.
- **`--bare` guardrail:** if any level config selects a minimal tool surface, the chokepoint MUST
  NOT use `--bare` with an OAuth token; route tool-trimming through an OAuth-compatible mechanism.

> **Adapted from H40 / run_probe.sh: reused / changed.** *Reused:* the exact flag, env-var
> isolation set, token-never-in-pane discipline, detached-tmux + pane-capture pattern.
> *Changed:* generalized the one-off interactive experiment (`run_probe.sh`: auto-accept trust +
> type a probe + tear down) into a **daemon-owned, claim-before-spawn, per-node** path; tmux session
> names bound to the one-spine address (the experiment used ad-hoc names); first-boot trust made
> deterministic instead of a send-keys race; binary version verified at spawn.

### 6.3 Per-runtime adapter (hexagonal: neutral core + thin port)

The parent emits a **runtime-neutral task contract** (identity/address, spec, frozen acceptance
artifact, interface contracts, constraints, workspace location, reporting expectations —
**identical across runtimes**). The adapter injects **only three** runtime-specific things:
(1) tool manifest, (2) harness invocation, (3) output format. Swapping a level's runtime = swapping
its adapter. Model+runtime is **config-time, not run-time** (E31): the chokepoint reads the level
config; no agent picks its own or its child's model. Assignment: L1–L4 = Opus 5.0 / Claude Code;
L5 = GPT-5.6 Sol / Codex; L5+ (#review) = Opus 5.0 / Claude Code (judgment diversity).

**Spawn-failure contract (E32, no silent fallback).** The adapter MUST confirm it pinned the
configured model+runtime **before** the child runs. On any of {**auth-expired**, model-unavailable,
override-rejected, runtime-down}: do **not** spawn on a substitute, do **not** best-effort —
**release the claim** and emit a spawn-failure escalation to L1 (child-address + configured vs actual
model/runtime + **which class fired**). The chokepoint also appends a best-effort
`child_spawn_failed` pointer to the direct parent's inbox, idempotent for the same child/failure
tuple, so a parent does not wait for a gate route from a child that never opened. L1 alerts the user
when the failure reaches the root/operator boundary. The chokepoint always writes the
**actual** `model_used` into the binding via the single-writer path (config = intent; recorded
`model_used` = fact; a checker asserts every spawned child has a `model_used` == configured, or a
corresponding escalation exists).

**Auth-expiry is a DISTINCT failure class, not a model-unavailable.** An expired/lapsed OAuth token
makes *every* spawn hit the E32 "cannot pin the model" path at once — so without a distinct class it
masquerades as a **fleet-wide model-outage storm**, and (worse) a post-expiry reboot cannot respawn
even L1. The chokepoint classifies an auth failure as **`auth_expired`** (separate from
`model_unavailable`) so the escalation says "refresh the token," not "the model is down," and so a
storm of identical `auth_expired` escalations is recognizable as one credential problem. Token health
is also a **named genesis precondition** (§7): genesis verifies the credential before spawning L1, so
the very first spawn fails *loudly on the credential* rather than as a mystery model-pin failure.

> **Dormant 2026-07-13 (L5-runtime unification):** L5 now resolves to the `claude-code` adapter (GPT-5.6 Sol main loop via the local CLIProxyAPI); the Codex adapter below stays registered as dormant, registry-selectable capability.
>
> **Codex adapter.** The Codex runtime is filled for GPT-5.6 Sol L5 execution. It boots the pinned
> Codex CLI with Codex's native base instructions, delivers harness role/task context through the
> neutral brief and boot prompt, discovers the real Codex rollout JSONL by matching the workspace
> cwd, and records the discovered session/transcript facts. For workspace-backed spawns the full
> boot prompt is written to `.codex-boot-prompt.md` in the node workspace and a short tmux pane
> wrapper reads that file before execing Codex; this preserves Codex's first-turn prompt behavior
> without placing a generated launch packet on tmux's `new-session` command line. The canonical
> pinned Codex home owns the user's ChatGPT/OAuth refresh token; each worker gets a fresh isolated
> `CODEX_HOME` under `.codex-pinned/seats/`, seeded with current access/id credentials and a present
> but non-usable refresh-token field. Canonical access-token freshness is preflighted before
> pane-open with a safety margin; `id_token` is presence-only. Each successful spawn records
> `codex_seat_id`, `auth_version`, and access-token remaining seconds into the binding for
> scoring/diagnostics, so expired/missing Codex OAuth fails as `auth_expired` rather than a late
> watchdog nonresponse.
> Result-flow needs no
> runtime-specific return channel (F33): both runtimes write durable truth into the work node +
> post a best-effort bus pointer; the parent re-reads the node.

### 6.3a Plan-Alignment Readiness Handoff

The plan-alignment gate is a normal nonterminal phase boundary, not a child collapse. When a running
L2 writes `plan-alignment-ready.json` in its node workspace, the daemon sweep validates that the
marker is owned by that node and points at a package, coverage sidecar, and semantic manifest
inside that node. Before
the handoff is admitted, the Q3 deterministic floor checks the sidecar's `{path, trace_ids}` rows
against the package stanzas, reads requirement tags/MNF only from the current notary-checked
intent-spec receipt, and runs forward design/test coverage, per-MNF failure-path presence, and
backward orphan/DR coverage. It then records `plan_alignment_state=semantic_cell_pending` plus:

- `plan_alignment_ready_artifact`
- `plan_alignment_ready_sha256`
- `plan_alignment_package`
- `plan_alignment_coverage_manifest`
- `plan_alignment_coverage_report`
- `plan_alignment_coverage_report_sha256`
- `plan_alignment_bundle_sha256`
- `plan_alignment_semantic_manifest` and hash
- `plan_alignment_semantic_bundle_sha256`
- `plan_alignment_semantic_control`
- `plan_alignment_element_index` and hash
- `plan_alignment_intent_fingerprint`
- `plan_alignment_ready_at`

Q3 admission does not wake L1. The daemon pre-registers the atomization, two reconstruction,
coherence, and dependency-held comparator bindings, then opens the first wave through the normal
spawn chokepoint with exact-read enforce-mode blinders. Comparator opens only after both
reconstruction reports are DONE, shape-valid, and stamped. Draft instruction text is not a config
opt-in: production dispatch verifies current calibration receipts and ratified
`semantic_cell_calibration` revision records, otherwise records
`SEMANTIC-CELL-INSTRUCTIONS-UNCALIBRATED` and fails closed.

When all five reports are current, the daemon writes a content-addressed evidence index, records
`plan_alignment_state=ready`, the semantic evidence path/hash, required elevations and delta, and
the once-per-intent atomization cache. Only then does it append one idempotent
`design-submission` line with `phase=plan_alignment` into L1. The recovery sweep replays this final
pointer if the best-effort append is lost. Same-intent repairs use the prior stamped element index
to scope changed dotted-ID subtrees plus trace-graph neighbors; intent revisions run the full cell.

If `plan-alignment-ready.json` is malformed, incomplete, or points outside the node workspace, the
daemon does not route the handoff. It records one bounded marker-defect row on the L2 binding and
wakes the same L2 with a `nonterminal_marker_invalid` inbox pointer naming the marker path, marker
hash, and validation errors. Re-polling the unchanged bad marker repairs a lost pointer but does not
spam WAL rows. The same refusal path carries deterministic trace/coverage defects plus the generated
content-addressed coverage-report pointer; L1 receives no readiness wake until those defects clear
and the semantic cell completes.

L1 returns the gate verdict through the IPC command
`plan-alignment-decision <L2>#exec --decision pass|fail --file <verdict>`. The daemon writes the
decision artifact, stamps `plan_alignment_state=decision_posted`,
`plan_alignment_decision=pass|fail`, and `plan_alignment_decision_artifact`, then wakes the L2 inbox
with a `plan_alignment_decision` pointer. PASS refuses while a machine-required elevation is
missing, unanswered, rejected, or stale. Current owner questions use the human `answer` verb with
`--question-id` and `--decision`; the legacy `answer-down` parent-to-child shim remains
repair/compatibility only.

### 6.3b Coordination Handoffs

T65 generalizes the nonterminal handoff idea below the plan-alignment gate without broadening the
gate PASS edge.

When a running `#exec` node writes `handoffs/<handoff-id>.json`, the daemon validates:

- the marker is inside that node workspace;
- the marker has `type: coordination_handoff`;
- `handoff_id` is a safe single-event identity;
- `handoff_kind` is one of `phase_ready`, `scope_issue`, `plan_gap`, `interface_issue`,
  `acceptance_gap`, `approval_request`, `guidance_request`, or `status_notice`;
- the referenced artifact exists inside the same node workspace;
- the node is running, has a direct parent, and is an `#exec` seat.

The daemon stamps the child binding's `coordination_handoffs[handoff_id]` record with
`state=submitted`, artifact paths, marker hash, summary, response-required flag, and target parent.
It appends one idempotent `coordination-handoff` line to the direct parent's inbox. A recovery sweep
replays lost parent pointers from `coordination_handoffs[*].state=submitted`.

If a `handoffs/*.json` marker is malformed, incomplete, or references invalid local evidence, the
daemon does not route the handoff. It records one bounded marker-defect row on the child binding and
wakes the same child with a `nonterminal_marker_invalid` inbox pointer naming the marker path, marker
hash, and validation errors. Re-polling the unchanged bad marker repairs a lost pointer but does not
spam WAL rows.

Parents answer child-authored handoffs through:

```bash
harnessctl coordination-decision <child>#exec \
  --handoff-id <id> \
  --decision ack|approve|reject|revise|guidance \
  --file <decision-artifact>
```

The daemon writes the decision artifact into the child node, updates
`coordination_handoffs[handoff_id].state=decision_posted`, and appends a `coordination_decision`
pointer to the child's inbox. The recovery sweep can replay this child-visible pointer from binding
state if the inbox append is lost after the durable stamp.

Parents can also send nonterminal guidance to a running child without a prior child handoff:

```bash
harnessctl coordination-note <child>#exec \
  --handoff-id <id> \
  --kind <handoff-kind> \
  --file <note-artifact>
```

This writes a child-owned coordination artifact, records
`coordination_handoffs[handoff_id].state=notice_posted`, and appends a `coordination_notice` pointer
to the child inbox. The recovery sweep can replay the notice pointer from the binding record. It is
the normal downward counterpart for clarified constraints, local guidance, or parent-owned
coordination updates. It is not a review verdict, a gate PASS, or an escalation answer.

### 6.4 Resume / necro — a spawn variant through the same chokepoint (with the gate firewall)

"Resume" appears throughout reconcile-on-restart (§5) and genesis (§7); this is its contract. It is
**not** a separate code path — it is a **variant of the spawn chokepoint** that re-adopts an
existing address instead of claiming a fresh one. Sufficiency-cut item #8 ("basic necro —
`--resume` + delta brief — WITH the gate-firewall carve-out") makes the carve-out a v1 INCLUDE, and
it lives in the chokepoint cluster ① owns.

The resume path:

1. **Re-adopt the address** through `claim` (§6.1) — the **re-adopt variant**: a CAS-guarded claim
   with `expected_state ∈ {running, dead}` (the §3.3 re-adopt edges), NOT the fresh-claim's
   `expected_state=planned`, on the **existing** node-address, **bumping `lease_epoch` and re-minting
   `owner_token`** (the prior incarnation, if it returns, is now fenced — §8). Resume is therefore
   claim-before-spawn too; it never double-spawns a live address (the §5.1 ADOPT-vs-resume split
   decides which).
2. **Assemble a delta brief** — not the full original brief: what changed since the prior
   incarnation (new messages, parent answers to an `ESCALATED`, reconcile findings), pointing at the
   durable work node the fresh instance re-reads (`status.md`, `log.md`, `report.md`, frozen
   acceptance — `agent-lifecycle.md`'s stateless-respawn recovery).
3. **Boot via §6.2** with the shared minimal prompt + the (delta) brief and its load-manifest,
   recording the **new** `session_uuid` into the binding via the single writer.

**The gate firewall (LOCKED correctness invariant, runtime-decisions §2.8): NEVER `--resume` a
session across a quality-gate boundary.** A session that has crossed a gate (e.g. an L5 whose work
went to `#review`, or any node whose plan was approved at a `PLAN-ALIGNMENT-GATE`) must be **re-spawned
fresh**, never resumed — carrying the pre-gate session's conversational context past the gate
re-introduces the exact contamination the gate exists to stop. This is **correctness, not
optimization**: the chokepoint **refuses** a `--resume` when the node's gate-crossing flag is set,
and falls back to a fresh spawn with a delta brief. The chokepoint enforces the refusal in cluster
①; the **gate-crossed signal itself** is a binding field cluster ② maintains
(`gate_crossed_at` / equivalent) — cluster ① reads it and enforces the firewall, ② decides when it
flips.

> **Adapted: NEW (mechanism) + LOCKED (firewall).** The recovered code had `--resume`-style
> continuation but no gate concept (single reviewer loop, no plan-alignment gate). The resume-as-
> spawn-variant routing and the never-resume-across-the-gate firewall are net-new here, promoted
> from runtime-decisions §2.8.

---

## 7. Genesis / First-Boot

LOCKED sequence (runtime-decisions §4 + gap-review #1 resolution):

```
1. The operator invokes the explicit Claude Code `l1-l5-harness` skill's start action (the
   underlying verb is `harnessctl start`).
2. harnessctl proves no reachable daemon / held instance lock / loaded protective job, writes the
   atomic one-shot start request, renders the per-run plist, and bootstraps it on demand
   (KeepAlive={SuccessfulExit=false}, no RunAtLoad, ThrottleInterval≥10).
3. harnessd acquires .harnessd.instance.lock, atomically consumes the request, and writes
   runtime.json. A crash restart finds no pending request and is recovery-only.
4. PRECONDITION CHECK (fail loud, do not spawn on a bad precondition):
     - credential health: the OAuth token / CLAUDE_CODE_OAUTH_TOKEN is present and unexpired
       (refresh if a refresh path exists; else escalate `auth_expired` to the user — §6.3). This is
       a NAMED genesis precondition precisely because a lapsed token makes the FIRST L1 spawn fail
       as a mystery model-pin error otherwise.
     - pinned-binary present + version/hash verified (§6.2).
5. Both deliberate start and recovery run reconcile-on-restart (§5.1):
     - first boot ever: binding ledger empty → nothing to reconcile.
     - start with in-flight work / crash relaunch: read ledger → replay WAL
       (torn-tail-tolerant, §4.4) → reconcile tmux → necro the dead → adopt live work.
     - crash recovery STOPS after reconciliation; it never registers or spawns a new L1.
6. On a deliberate start only, if no live, non-terminal L1 binding exists:
     - REGISTER L1 as the root node in the binding ledger (parent_address = null; it is the only
       node with no parent — every other node has a declared parent by the supervision-tree
       invariant).
     - If the launch descriptor carries an initial L1 intake payload, append it to L1's durable
       `.inbox.<seat>.jsonl` before the actor opens (`from=operator`, `type=intake`, idempotent by
       payload hash). This removes the parentless-root async-intake idle window: L1's first turn can
       read the intake artifact the brief points at. If no initial intake is configured, genesis
       does not invent work; L1 waits for the normal inbox path.
     - SPAWN L1 as root via the single spawn chokepoint (§6), in role
       (`--system-prompt-file <L1-workspace>/.identity-prompt.md` — shared prompt first, then L1
       identity docs; broader load-manifest in the brief; role_variant = L1), claim-before-spawn at
       address (the L1 root address).
   Else (a live or resumable L1 binding exists):
     - RESUME, do NOT double-spawn (the F35 stable-address resume-not-double-spawn rule).
```

L1 has no parent agent — **the daemon is what starts L1** (closing the gap-review genesis hole:
*"every bootstrap is child-spawned-by-parent; L1 has no parent and nothing starts it"*). The daemon
is the root of the supervision tree's *custody* chain even though it is not an agent.

> **FORK — for user review (minor).** Ordering of the daemon's process-level resume of L1 vs L1's
> own doc-level boot-reconciliation (L1 reads README → portfolio → threads → comms). These are two
> different reconciliations firing at L1 boot. **Recommendation:** the daemon resumes/spawns the L1
> *actor* first (process-level), then L1 performs its own doc-reconcile *inside* its session
> (agent-level) — the daemon establishes custody, then the agent orients. They are layered, not
> competing.

The versioned Claude Code project skill at `.claude/skills/l1-l5-harness/SKILL.md` is the committed
S6 operator surface. It codifies start, status/attach, the preliminary `fidelity-playback`
question, owner/delegate answer forms, deliberate promote, and the decision/repair verbs;
`harnessctl` remains the underlying boundary and the daemon remains the only state writer.

---

## 8. Fencing

**Why in v1 (defend against "defer it"):** fencing's failure mode (split-brain, incident F-024) is
**silent**, so its trigger is a **design event** (enabling concurrent-spawn or live-necro), not a
failure event — you cannot wait for it to "bite" in a run, because it fails *quietly*. And it is
**cheap once single-writer exists** (it is three extra fields + one extra CAS precondition).
(runtime-decisions §2 item 11 + §3 special-status note.)

**Mechanism:**

- **`lease_epoch`** — a monotonic int per node, **bumped on every claim / adopt / respawn /
  ownership-transfer**, rotated **in the same atomic transaction** as the actor-changing transition
  (F-012 fix: no window where state advanced but ownership didn't).
- **`owner_token`** — a composite identity minted at claim/adopt time:
  `address:subagent-id:session-uuid:lease_epoch` (the recovered
  `role:subagent-id:session-uuid:lease_epoch` format with `role` → the one-spine `address`). Because
  it embeds the epoch, **the token is self-fencing** — comparing tokens compares epochs.
- **CAS on transition AND spawn** — every mutator (transition, heartbeat, release-lease,
  watchdog-checkpoint, **claim/spawn**) presents `expected_owner_token`; a mismatch aborts the
  mutation. This is what extends the recovered `transition`-only guard to **every** write path,
  including the spawn path F-024 left unguarded.

**Stale-return fencing (non-destructive).** If an old actor returns after a respawn and tries to
act, its token's epoch is lower than the live binding's; the mutation aborts and the executor
records **`stale_return_ignored`** in the run-ledger. *The old actor is de-authorized, not
auto-killed* (`watchdog-design-01.md`). Its eventual terminal signal, if any, is journaled with
`terminal_signal = FENCED` so cluster ②'s policy can tell "fenced/de-authorized" apart from
"completed" / "died-infra" / "died-methodology."

> **Provenance — promoted from phase-2 design / NEW in code.** *Promoted from
> `phase-2-runs/research/watchdog-design-01.md` (proposed-but-never-coded):* the composite
> `owner_token` format, the "lease belongs to the work unit, restart resumes from ledger state"
> principle, and the `stale_return_ignored` non-destructive de-authorization event. *NEW in code
> here:* `lease_epoch`/`owner_token` were **entirely absent from the recovered control plane**
> (`self-improvement-harness/` — its manifest carried only a name-string `owner` with no epoch, and
> its `control_plane.py` `cmd_transition` checked only `expected_state` + `len(ledger)`, no
> `--expected-owner-token`; F-012/F-024); the CAS-on-every-mutator and CAS-on-spawn are new (the
> phase-2 plumbing exposed `--expected-owner-token` on a supervisor but never gated a CAS spawn);
> epoch rotation made transactional with the actor-changing transition is new. This is **promoting a
> proven-on-paper lease into code for the first time**, not inventing it against a clean baseline.

---

## 9. The Seats Provided to Clusters ②/③/④

Cluster ① provides **seats, not internals.** Each deferred policy attaches to a field or a
chokepoint defined above — so adding it later is a one-site change, not a re-plumb.

**To cluster ② (Liveness & lifecycle):**
- the binding fields the evidence-lease state-machine reads/writes: `condition`, `suspect_since`,
  `recovery_attempts`, `stale_grace_checks`, `last_evidence`, `liveness_state`, `last_progress_at`,
  `last_heartbeat_at`.
- the **terminal-signal field + the closed event set** (`signal_DONE/FAILED/ESCALATED`,
  `coordinator_died`, `died_infrastructure`, `died_methodology`, `coordinator_completed`,
  `stale_return_ignored`) — recovery branches on the terminal class (F-017).
- `auto_resume_command` + the `--run-recovery` `allow_recovery` plumbing (the two-keyed interlock):
  carried by cluster ①, **fired** by cluster ② (the daemon never auto-resumes without both the
  field and the flag).
- the reconcile **hooks**: the per-node read-tmux-vs-ledger pass (where ② plugs the multi-signal
  detector behind the stable `liveness(node)` interface), the escalation seat for ambiguous cases
  (where ② plugs auto-resolution), and the **coordinator-died ESCALATE seat** (§5.4 — ② reads
  `coordinator_died` and chooses recover-vs-reap; ① only detects + escalates).
- the **gate-crossed signal** the resume firewall reads (§6.4): cluster ① *enforces* "never resume
  across the gate" by reading a binding field (`gate_crossed_at` / equivalent); **② maintains when
  that field flips** (it owns gate detection). The enforcement is ①'s; the signal is ②'s.
- the watchdog **loop scheduling** (the daemon runs the tick; ② supplies the loop body / probe /
  state-machine, written through the executor — never a second writer).

**To cluster ③ (Transports):**
- the **terminal-signal journaling** contract: the agent writes a durable
  `<node-dir>/.signal.<seat>.json` `{signal, ts, owner_token, evidence}` as its last act (the
  `owner_token` copied verbatim from the chokepoint-seeded `.sign-off.<seat>.json` handshake); the
  reconcile sweep detects it and the executor journals from the durable read
  (§3.5) — so the sign-off check reads the **journal**, and a dropped live bus nudge can at worst
  **delay** journaling to the next sweep, **never** lose the durable row or cause a false sign-off
  failure. The bus carries a best-effort *wake* (triggering an immediate sweep); the durable fact
  lives in `.signal.<seat>.json` → the ledger + the work-node `report.md`. On `signal_ESCALATED`,
  the same journal-once transition appends the harness-owned `child_escalated` pointer line to the
  parent's inbox so a parked coordinator wakes; any agent-authored escalation nudge is only an
  optional fast-path.
- the human control surface attaches to the harness app (start path) and routes human-kill
  **through the executor's stamping path** (never raw tmux), and the answer-escalation slot rides
  the `terminal_signal = ESCALATED` + `terminal_note` carried in the binding.
- the **pause-subtree read-point**: ③'s human control surface sets/clears the `paused_at` binding
  field (§3.2) — set/cleared **only** through the single-writer executor, never raw — and ① **seats
  the enforcement** at the §6.1 claim-slot pre-step (STEP 0), which refuses to launch a child under a
  paused subtree (the node OR any ancestor by address-prefix). The companion read-point in ②'s
  recovery loop (skip prod/respawn for a paused subtree) is the other half; both are required or
  `paused_at` is a flag no one honors.

**To cluster ④ (Scale-as-resource):**
- the **claim-slot pre-step** in the spawn chokepoint (§6.1): admission control wedges between
  claim-accepted and actor-open **without re-opening the CAS**. Per-runtime ceilings / 429-backoff /
  resource envelope all gate that same single spawn path. The claim-then-admit-then-open ordering is
  defined now precisely so ④ has a clean insertion point.
- the **in_flight increment/decrement seat pair** for ④'s slot count (④ owns the COUNT; ① provides
  the SEATS, no new writer). The §6.1 accepted-claim is the **claim-INCREMENT** seat; the
  single-writer **terminal transaction** (the §3.6 `done`/`failed`/`dead` collapse write, AND the
  §5.4 necro / §5.1 reconcile-finds-dead path) carries the symmetric **release-DECREMENT** — it rides
  the existing terminal write, crash-safe via `last_applied_seq`. The pair is matched on purpose so
  ④'s admission gate is a balanced counter, not an increment-only ratchet that leaks slots.

---

## 10. Open Seams (recorded, not resolved here)

- **run-ledger vs project `log.md`:** are terminal-signal entries mirrored into `log.md`, or kept
  strictly in the daemon run-ledger? Recommendation leans "strictly in the run-ledger; `log.md`
  stays agent-written project history" — but the mirror decision is left open.
- **`role_bundle_hash` as a fencing surface:** whether a role-bundle change mid-flight should be
  detectable/fence-triggering is recorded as a binding field (`role_bundle_hash`) but the policy is
  open.
- **launchd token lifecycle:** token *health* is now a named genesis precondition and `auth_expired`
  is a distinct spawn-failure class (§6.3/§7). What remains open is the **refresh mechanism** for a
  live run (token-file vs `_FILE_DESCRIPTOR`, whether an unattended automatic refresh path exists
  or expiry must always escalate to the user) — unaddressed in the source material, flagged for
  the credential design. Run-scoping removes the no-build/login-residency part of this concern.
- **`--system-prompt-file` re-verification on version bump:** H40 lists the re-checks for the shared
  `operational/shared/system-prompt.md` boot (flag exists, still REPLACES base block 2, still
  interactive, still OAuth). Whether genesis runs this as a self-check is unresolved.
- **Codex adapter fill** (§6.3) — owed.

---

## Adaptation summary (control_plane.py → DAEMON.md)

Sourcing note: `control_plane.py`/`manifest.yaml`/`watchdog.py` are the **recovered**
self-improvement-harness; rows marked *(phase-2)* are promoted from
`phase-2-runs/research/watchdog-design-01.md` (proposed-but-never-coded), NOT from the recovered code.

| Component | Reused from the recovered control plane | Changed / NEW for the tree |
|---|---|---|
| Mutation skeleton | read→validate→commit-inside-lock (cmd_transition) | per-node binding; one run-scoped writer not per-CLI |
| CAS | expected-state + expected-ledger-entries (L1511/L1516) | + `expected_owner_token` *(phase-2 token, first time in CAS)*; per-node `generation` not global `len(ledger)` |
| Lock | `fcntl.flock` SH/EX contextmanager (L248) | one serialization domain (was two: control-plane + workboard); CLIs route through daemon, no external read-modify-replace |
| Atomicity | tmp+fsync+os.replace; append+fsync JSONL (L191/L241) | ordering reversed: ledger-append FIRST (WAL); covers binding ledger; **torn-tail tolerance ADDED** (recovered `load_ledger` RAISES on a torn line — §4.4); WAL record carries `binding_delta`+pre/post `generation`+`last_applied_seq` watermark; no plain write_text for recovery-read state (status sidecar is the lock-free carve-out) |
| Validate | pure `validate()` errors-block/warnings-allow (L618) | per-node admission check |
| Manifest | `activity_lease`+`watchdog`+`observation_window` blocks | one block → map keyed by node-address; + `lease_epoch`/`owner_token` *(phase-2)* / `session_uuid`/`liveness_state` (canonical working\|waiting\|idle\|dead) / `terminal_signal` / `last_applied_seq` |
| Workboard stream | `{stream_id,status,owner,stop_condition,write_targets,evidence_refs}` | merged onto the node record, keyed on one-spine address (was separate file/lock) |
| Run-ledger | append-only JSONL event journal; edge-triggered; reconcile-once | keyed by node-address; + global seq (= replay watermark); + record framing (len/crc); + first-class terminal/death events; + §3.6 terminal mapping table |
| Resume / necro | `--resume`-style continuation (no gate concept) | NEW: resume = spawn-variant through the chokepoint (re-adopt + bump epoch + delta brief); LOCKED gate firewall (never resume across the gate) |
| Spawn | (none — no spawn command existed) | NEW single chokepoint; claim-before-spawn (F-024 fix); H40 in-role boot; per-runtime adapter |
| Reconcile | (none — only heartbeat-age inference) | NEW tmux↔ledger sweep; on-restart + continuous; orphan escalation; `coordinator_died` |
| Daemon | (none — explicitly deferred) | NEW run-scoped launchd-protected process; PID/lock; crash relaunch=recovery; one-shot genesis |
| Watchdog ownership | observer-writes-through-executor | loop hosted in the live run daemon; in-process not shell-out |
