# SCALE.md — Cluster ④: Scale-as-a-resource

**Status:** DESIGN (mostly designed-now, built-lazily). This is the **lightest** cluster. It owns
**one gate at one seat**, a usage-model spec, and a measurement plan — and **defers** every live
implementation behind explicit triggers.

**Cluster ④ does NOT build anything in v1.** "Done for ④" = **the seat is bound + the usage model is
specified + the measurement plan is written + each deferral's trigger is recorded** — NOT a running
admission gate, NOT live ceilings, NOT a backoff/queue implementation. The seat ④ attaches to is
already built by cluster ① (DAEMON.md §6.1). ④ does not duplicate it, does not re-open it, and adds
no new writer, lock, counter authority, or spawn path.

> **Reading order before implementing:** DAEMON.md §6.1 (the ADMISSION SEAT) and §2.4 (single-central
> daemon) first; then §3.3 (the release rollback edge), §3.5/§3.6 (run-ledger terminal vocabulary),
> §6.3 (per-runtime adapter + distinct-failure-class precedent), §9 (the seat-to-④ contract). This
> doc cites those sections; it does not restate their mechanics.

---

## 1. Charter + scope

### 1.1 The five-part charter (gap-review blocker #8 resolution, verbatim)

> "a hard **max-in-flight admission gate in the spawn script** (atomically claim a slot before launch;
> over-ceiling spawns queue or bounce-to-parent, never best-effort-launch); per-runtime sub-ceilings;
> replace 'no token limits' with a usage model (defined behavior on 429/overload/usage-exhausted:
> backoff/queue, never silent-fail or mis-classify as a semantic agent failure; backpressure near the
> usage window); decide whether the cap is denominated in sessions or shared-account usage headroom."

Mapped to this doc:

| # | Charter element | Section |
|---|---|---|
| 1 | Hard max-in-flight admission gate at the spawn chokepoint | §2 |
| 2 | Per-runtime sub-ceilings | §3 |
| 3 | Usage model / 429-backoff / backpressure, never mis-classify | §4 |
| 4 | Cap denomination (sessions vs usage headroom) | §5 |
| 5 | Per-session resource envelope (measure, don't guess) | §6 |

### 1.2 What ④ OWNS

- The **hard max-in-flight admission gate logic** that wedges at ①'s claim-slot pre-step (§2).
- The **per-runtime sub-ceiling values + split rule** (Opus-one-Max-pool vs Codex-separate) (§3).
- The **usage model / failure behavior** spec: 429/overload/usage-exhausted → backoff/queue,
  backpressure near the usage window, and a **distinct rate-limit failure class** that never lands as
  a semantic agent `FAILED`/`died_*` (§4).
- The **cap-denomination decision** (sessions vs shared-account usage headroom) (§5).
- The **resource-envelope measurement plan**: what to measure on the first commissioning runs and the
  rule that the cap **derives from measured headroom** (§6).

### 1.3 What ④ CONSUMES from ① (do NOT redesign — bind to these)

Cluster ① built the spawn machinery; ④ attaches to it. ④ touches **none** of these — it reads/uses
them.

| ④ consumes | DAEMON.md anchor | What it gives ④ |
|---|---|---|
| The **ADMISSION SEAT** line between claim-accepted and actor-open | §6.1 (`# --- ADMISSION SEAT (cluster ④ wedges here ...) ---`) | the exact insertion point; STEP 1 CLAIM done, STEP 2 adapter not yet run |
| The **single-central daemon** — one serialization domain, **one writer, one exclusive lock** | §2.4 Option A (RECOMMENDED) | the *substrate* the in-flight count lives on: the only place a shared count can be maintained or derived atomically. **NOT a pre-existing counter** — ① defines none (see below) |
| The **reconcile sweep** that enumerates live tmux sessions + the per-node `liveness_state` field | §5.4, §3.2 (line 759 *"list live tmux targets"*) | the live-session set ④'s slot count is reconciled against (drift check), and the per-node field a prefix-scan would read |
| The **`claim` CAS primitive** + the **`claimed → planned` release rollback edge** | §3.3, §6.1 | the legal, WAL-replayable way to bounce an over-ceiling spawn |
| The **terminal-collapse / `reconcile-finds-dead` edges** + the single-writer terminal transaction | §3.3 (lifecycle), §3.6, §5.4 | where ④'s slot RELEASE-on-terminal rides (the same transaction that stamps `terminal_signal` / necro's a dead node) |
| The **per-runtime adapter** + the **`model_used` binding field** | §3.2, §6.3 | the config field the per-runtime sub-ceiling keys off |
| The **distinct-spawn-failure-class pattern** (`auth_expired` precedent) | §6.3 | the template for ④'s distinct rate-limit class |
| The **run-ledger event/terminal vocabulary** | §3.5, §3.6 | the vocabulary a 429 must stay OUT of |
| The **WAL replay / atomicity / `last_applied_seq`** | §4.4 | a released-on-deny claim and a release-on-terminal decrement are both crash-safe / replayable |

> **The in-flight COUNT itself is ④'s deliverable, NOT an ① field.** ① defines **no**
> `in_flight` / `max_in_flight` / concurrency counter anywhere — its only counters are per-node
> (`generation`, `recovery_attempts`, `stale_check_count`) or per-restart (`incarnation`). What ①
> gives ④ is the **substrate** (one writer, one lock, one serialization domain) plus the **derivation
> source** (the reconcile sweep's live-tmux list + per-node `liveness_state`). ④ OWNS specifying the
> count: a denormalized `in_flight` integer (per pool) maintained **inside** the existing claim
> transaction — incremented at the §6.1 CLAIM, decremented on terminal collapse (§2.2) — and
> **reconciled** against the sweep's live-session list (drift check, not a second source of truth).
> "No new lock / no new writer" stays true (the count lives inside ①'s one transaction); "already-seated
> counter" was wrong and is dropped throughout.

The seat-to-④ contract is stated by ① itself (DAEMON.md §9): *"the **claim-slot pre-step** in the
spawn chokepoint (§6.1): admission control wedges between claim-accepted and actor-open **without
re-opening the CAS**. Per-runtime ceilings / 429-backoff / resource envelope all gate that same single
spawn path. The claim-then-admit-then-open ordering is defined now precisely so ④ has a clean
insertion point."*

### 1.4 What ④ DEFERS-with-triggers (loud)

**Almost all of ④'s substance is deferred.** The seat exists in v1; the *logic* attaches under load.
The single canonical trigger for the whole admission-control bundle (runtime-decisions §3 DEFER
table, verbatim): **"run wide enough to hit a rate/resource limit."** Deferrals are pulled during
deliberate **commissioning pressure-up** (runtime-decisions §5: *"pressure-up (concurrency, wide tree)
finds load leaks ... where most DEFERRED items surface ... controlled provocation, not waiting for
prod"*), not in production. Full table in §8.

### 1.5 What ④ must NOT do (discipline)

- MUST NOT redesign the spawn chokepoint, the CAS, the `claim` primitive, the serialization lock, or
  any (re)spawn path — all are ①-built and DONE. Bind to §6.1, §2.4-A, §3.3, §9; cite by section.
- MUST NOT introduce a second counter, a second writer, or a parallel spawn path. Per CLAUDE.md's
  single-writer keystone, **only `harnessd` mutates durable state**; ④'s slot count lives **inside**
  the existing claim transaction.
- MUST NOT carry the **20–64** number forward as a ceiling — it was a coordination-quality guess made
  before the tmux model was locked (§6). The real cap derives from **measured** headroom.
- MUST NOT import the phase-2 "compute-saturation-enforcement" as the cost cap — it is a
  minimum-parallelism **FLOOR**, the opposite mechanism (§7).
- MUST NOT mark ④ items "built." Done = designed + trigger recorded.

### 1.6 Built now: semantic L2→L3 scheduling, not a capacity cap

The first post-auth load-shaping slice is deliberately **not** the Cluster ④ hard cap. It is a
process-ordering rule at the parent-spawn register path:

- An L2's L3 children are admitted serially by sibling order, including both planning-L3 (L3&) and
  execution-L3 (L3) seats.
- `L3.1` is eligible to spawn immediately.
- `L3.2` is registered as `state=planned`, `admission_state=waiting_on_sibling`, and
  `waiting_on_sibling=L3.1` until `L3.1` has durable `gate_state=gate_passed`.
- `L3.3` waits on `L3.2`, not on `L3.1`.
- The daemon's planned-spawn redrive releases a waiting L3 through the single writer, then uses the
  existing claim-before-spawn path.
- If the predecessor cannot provide a pass proof because it failed, died, or reached a parent-visible
  non-passed gate state, the successor becomes `admission_state=blocked_on_sibling` and L2 receives a
  `serial_admission_blocked` pointer. The harness does not silently skip or resequence the chain.
- L4/L5 fanout below the active L3 remains unconstrained by default.

This gives behavioral runs a controlled L3 shape without assuming a Codex concurrency ceiling. The
planning cascade is still a coordinated design round: L2 may register the known planning-L3 siblings
as a set, and later performs compatibility review across their returned designs. The runtime admits
only the next L3 in the chain. Passive evidence, not this scheduler, decides whether a later hard
Codex cap is needed.

---

## 2. The hard admission gate at ①'s claim-slot pre-step

### 2.1 The exact seat

The gate wedges at the line in DAEMON.md §6.1's `spawn(...)`:

```
# --- ADMISSION SEAT (cluster ④ wedges here, between claim-accepted and actor-open) ---
```

The §6.1 ordering, with ④'s seat shown:

```
STEP 1 — CLAIM  (CAS-guarded transition planned→claimed; mints new lease_epoch + owner_token)  [①]
   └─ [ADMISSION SEAT — ④ runs HERE: atomic slot-claim against the shared in-flight counter]
STEP 2 — ADAPTER assembles the runtime-neutral brief, picks the runtime adapter                 [①]
STEP 3 — confirm model/runtime pinned (E32)                                                     [①]
STEP 4 — open the tmux actor                                                                    [①]
STEP 5 — claimed → spawning → running                                                           [①]
```

④ lives **strictly between STEP 1 and STEP 2**. The control-plane CLAIM has already happened (the slot
is reserved at the lease level), but **no actor has opened**. This is deliberate: ① states the claim
is *"a **distinct pre-step** so admission control (cluster ④) can wedge between claim-accepted and
actor-open **without re-opening the CAS**"* (§6.1).

### 2.2 The atomic slot-claim — and its symmetric release

At the seat, ④ atomically claims a slot from the **single in-flight count** maintained in the
single-central daemon's serialization domain (§2.4-A). The count is ④'s addition (§1.3 note), but it
lives **inside ①'s machinery**: one serialization domain behind one exclusive lock (§4.2/§4.3), one
writer. ④ adds **no new lock, no new writer, no new spawn path** — only the count field and the two
edges that move it.

**The seat runs as a second CAS-guarded transition, not the same WAL record as CLAIM.** The §6.1
admission seat sits *strictly after* STEP 1 CLAIM has **committed** (`planned → claimed`) and before
STEP 2 — §2.1 itself places ④ "between claim-accepted and actor-open." The tell that CLAIM has
already committed: ① defines a `claimed → planned` **release** edge (§3.3) for admission-deny — if the
slot mutation were inside the *same* transaction as CLAIM, a denial would simply abort that
transaction (state never leaves `planned`) and there would be nothing to release. The existence of
the release edge proves CLAIM commits first. So ④'s slot-increment is a **second CAS-guarded
transition in the same serialization domain** (same lock, same single writer), sequenced immediately
after the claim commits — **not** "the same WAL record." The one-writer / one-lock guarantee holds;
the single-atomic-record claim was inaccurate and is dropped.

**Claim and release are the two halves of one counter — name BOTH seats now.** The slot is:

- **claimed** (increment) at the §6.1 admission seat, here, and
- **released** (decrement) when the node enters a **terminal lifecycle state** (`done | failed | dead`
  per §3.6) or is **necro'd** (§5.4) — stamped by `harnessd` in the **same terminal transaction** that
  writes `terminal_signal` / runs the `reconcile-finds-dead` collapse (§3.3 lifecycle, §3.6, §5.4).
  Both halves are single-writer, CAS-guarded, and replayable via `last_applied_seq` (§4.4), so the
  count is crash-safe.

Without the release half the count is an **increment-only ratchet**: it would rise monotonically and
permanently saturate the sub-ceiling after the first N sessions the system ever spawns, after which
the gate bounces everything forever. The decrement-on-terminal edge is therefore **load-bearing for
the gate to function at all**, not an optimization — it is named here as a v1 seat, same as the claim.
(It rides ①'s existing terminal-collapse path; it adds no new mechanism.) This decrement-on-terminal
is **deferred-with-trigger** like the rest of the admission logic (§8), but the *seat* is specified now.

The atomic-claim ordering is lifted wholesale from the recovered prior art's branch-open
("classify → claim slot → persist → append ledger → THEN let the caller launch" /
*"never spawn first and validate later"*). Only the **bound being checked changes**: the prior art
claims against a per-round FLOOR-target; ④ claims against the **shared in-flight CEILING**. Same atomic
mechanic, opposite bound.

> **Why a counter and not a prompt rule.** policy-01's load-bearing meta-rule: *"if a rule cannot be
> represented in manifest state, ledger events, branch packets, or watchdog state, it does not count
> as load-bearing ... If the only defense is a prompt reminder, the defense is too weak."* This is
> exactly why ④ replaces the OLD advisory cap (a status-board read consumed by L1/L2 judgment): the
> hard gate must be an **atomic counter in the daemon's serialization domain**, where the
> (cap+1)th spawn is **structurally** blocked, not advised-against.

### 2.3 Over-ceiling behavior — release the claim, then queue or bounce (NEVER best-effort-launch)

If admission **denies** (the counter is at the runtime's sub-ceiling), the chokepoint MUST NOT open
the actor. It **releases the claim** via the first-class legality edge (DAEMON.md §3.3):

```
claimed ──release (admission-deny / E32-pin-fail)──▶ planned   (ROLLBACK; §6.1)
```

This is the **same edge** used for an E32 model-pin failure — admission-deny is named in it explicitly.
The release is itself a CAS-guarded, WAL-replayable transition (§4.4), so a denied/over-ceiling spawn
is rolled back **atomically** and the slot is reclaimable. The node returns to `planned`; **no actor
ever opened.** An over-ceiling spawn therefore **never best-effort-launches** — it releases the slot
and takes one of two dispositions:

- **Queue** — hold the `planned` node and retry the claim when a slot frees (the daemon's reconcile
  sweep is the natural retry tick). Appropriate for non-urgent fan-out. **BUT a held-`planned` node
  has no lease and no pane, so it is OUTSIDE the watchdog's evidence-gated recovery loop** (WATCHDOG
  §3.4 fires on lease/pane staleness; a queued node has neither). If the retry tick silently never
  fires — sweep regression, a pool whose window never resets, or a queue entry orphaned by a daemon
  restart (`planned` is the WAL-replay floor) — the node sits **forever** with no failure signal and
  no escalation: a genuine silent-fail relocated into queue starvation. **Therefore queue is only a
  legal disposition WITH a bound** (see invariant below); an unbounded queue is forbidden.

> **REQUIRED queue invariant (wherever queue is permitted — admission-ceiling AND 429/usage-window,
> §4.1).** A queued/held node MUST carry a `queued_since` timestamp and MUST **bounce-to-parent past a
> `max_queue_age` (and/or `max_queue_depth`) bound**, so a queue can never become an unbounded silent
> wait. This is a design-level seat addition (one binding field + the bound); the live values are
> deferred-with-trigger (§8) like the rest, but the *field + the bound* are mandated now. It closes the
> only residual silent-fail surface in the usage model (§4.2).
- **Bounce-to-parent as a capacity escalation** — surface "could not admit child X: at the Opus pool
  ceiling" up to the parent (ultimately L1), exactly as a model-pin failure routes up. A capacity
  bounce is a **system-level** concern (like E32's model/runtime config-time concern, which
  *"terminates at L1 specifically ... no intermediate level is authorized to pick a substitute"*), so
  it routes up rather than being resolved locally with a best-effort launch.

> **FORK — for user review: queue vs bounce-to-parent as the default over-ceiling disposition.**
> - **Option A — bounce-to-parent (capacity escalation) as the default (RECOMMENDED).** Mirrors the
>   E32 spawn-failure discipline (release-claim + escalate, never substitute). The parent decides
>   whether to wait, re-prioritize, or drop the child. Pro: no hidden queue depth, no unbounded
>   backlog, the decision sits with the level that owns the work. Con: more escalation traffic during
>   a wide fan-out burst.
> - **Option B — queue-with-retry as the default, bounce only after the `max_queue_age`/`max_queue_depth`
>   bound (the §2.3 REQUIRED queue invariant).** Pro: absorbs transient bursts without bothering the
>   parent. Con: introduces a second piece of state (the queue) that needs its bound + its own
>   backpressure — more to build, more to get wrong, and a *bound-less* version can hide a genuine
>   capacity problem behind a growing backlog (which is exactly why the bound is mandatory, not
>   optional, even under Option A's reserved-for-429 use).
> - **Recommendation: Option A** for the admission ceiling (bounce-to-parent), with **Option B
>   reserved for the usage-window/429 case** (§4), where a short transient backoff-and-retry is the
>   right response to a *temporary* throttle rather than a *structural* over-subscription. Decide at
>   pull-in time, informed by the first pressure-up run's escalation volume.

### 2.4 All (re)spawn paths funnel through this one seat — the gate is free for ②/③

Every launch in the system routes through §6.1, confirmed in all three cluster docs. ④'s gate
therefore covers every path **automatically**, with no per-cluster work:

| Path | Routes through §6.1? | Source |
|---|---|---|
| Fresh spawn | yes — `spawn()` | DAEMON §6.1 |
| Resume / necro | yes — §6.4 is *"a **variant of the spawn chokepoint** ... **not** a separate code path"*; re-adopts via `claim` | DAEMON §6.4 |
| ②'s recovery respawn | yes — *"Resume = spawn-variant; ② does NOT build a separate resume path"* | WATCHDOG §10; §11.2 |
| ③'s canonical answer message to a collapsed child | yes — the durable answer pointer is carried through ①'s spawn-chokepoint resume variant, not a separate spawn path | TRANSPORTS §0 |
| ③'s gate-firewall fresh-spawn | yes — *"route through that same chokepoint, so ④'s [gate applies]"* | TRANSPORTS §4.3, §8 |

Both ② and ③ explicitly contribute **nothing new** to ④'s mutation path: WATCHDOG §11.2 — *"② provides
**nothing new** to ④"*; TRANSPORTS §8 — *"③ provides ④ **nothing new in the mutation path** — same as
②."* So ④ designs **one gate at one seat**; it does not design anything per-cluster.

> **The symmetric release (§2.2) covers RESPAWN for free — no new mechanism.** WATCHDOG §3.4 step 8
> RESPAWN releases the dead/stale incarnation (a `failed`/`dead`/necro terminal collapse) and opens a
> fresh actor through the resume chokepoint (§6.4 → §6.1 → seat). The terminal collapse that **precedes**
> the respawn **decrements** the old slot (§2.2 release half); the resume chokepoint's fresh claim
> **increments** one. Net: **one slot per live node-address, not two** — every recovery cycle nets to
> zero leak. Without the release-on-terminal rule this would be the worst case: each respawn would claim
> a *second* permanent slot for the same node, leaking one per recovery cycle. **ADOPT (step 7) is the
> correct exception:** it re-fences an already-live pane and opens **no** new actor, so it consumes
> **no** slot. Only RESPAWN opens an actor and correctly hits the gate; ADOPT correctly does not.

---

## 3. Per-runtime sub-ceilings

### 3.1 Why one pooled cap is insufficient

The locked runtime model makes a single pooled cap wrong: the spawnable runtimes draw on **two
independent resource pools** with **different** limit semantics.

- **Opus / Claude Code pool** — one **Max account**, sharing **5h + weekly usage windows** plus a
  per-account throughput limit. Multiple in-flight Opus sessions contend for the **same** account
  windows.
> **2026-07-13:** the Codex pool is dormant (L5 unified onto Claude Code). OPEN: which subscription pool a Sol-main-loop Claude seat consumes (Opus Max pool vs a separate Sol/proxy pool) — recorded in the ruling note, unresolved (F4).

- **Codex / GPT-5.6 Sol pool** — **billed separately** and **rate-limited separately**. Its limits are
  independent of the Opus account windows.

A single pooled in-flight cap conflates these — it cannot express "we are out of Opus headroom but
have Codex headroom" (or vice versa). The cap MUST split per-runtime: one sub-ceiling per pool.

### 3.2 The pool-membership rule (do NOT model it as "L1–L4 vs L5")

The naïve split "L1–L4 = Opus, L5 = Codex" is **wrong** — it undercounts the Opus pool. From the
Assignment Table (runtime-and-model-map.md E32; DAEMON.md §6.3):

- **Opus / Claude-Code pool** = **L1 + L2 + L3 + L4 + L5+** (the L5+ reviewer is Opus/Claude-Code, a
  *different* runtime from the L5 it reviews — deliberate judgment diversity, but it draws on the
  **same shared Max account**). **Caveat:** that "same shared Max account" premise rests on the §6.3
  **auth-multiplexing-under-load** open seam (*"whether per-session auth multiplexes cleanly is
  unverified"*) — the sub-ceiling math (one pooled Opus window across all these sessions) is
  *correct IF* one Max account multiplexes across many concurrent sessions, which is **assumed, not
  yet confirmed**. If multiplexing does not hold cleanly, the pool-membership math is unaffected but
  the *single-window* assumption underneath it needs revisiting (flagged in §6.3).
- **Codex / GPT-5.6 Sol pool** = **L5 only.**

So the Claude-pool sub-ceiling counts `L1+L2+L3+L4+L5+`; the Codex-pool sub-ceiling counts `L5`.
Stating "L1–L4 vs L5" omits L5+ from the Claude pool and undercounts it.

### 3.3 The split is config-keyed and live-read (not hard-coded)

Model/runtime is a **config-time, per-level, swappable** dimension (E31): *"No agent selects its own or
its child's model/runtime mid-task,"* and the assignment table is *"a config snapshot, not a law."* The
Future first-class tester-seat runtime *may later be moved to GPT-5.6 Sol based on eval* — i.e. a level/seat can migrate from the
Opus pool to the Codex pool. Therefore the sub-ceiling allocator MUST derive pool membership from each
level/seat's **current config field** — the **same** runtime field the adapter selection reads at
STEP 2, immediately after ④'s seat. ④ keys its per-runtime ceiling off `level_config`'s runtime at the
seat and writes the actual runtime as `model_used` (§3.2/§6.3); it never hard-codes a level→pool map.

### 3.4 Codex Sub-Ceiling Remains Empirical

The Codex adapter is now filled, but the Codex-side cost/billing and capacity model is still empirical.
Fresh-token OAuth probes on 2026-06-16 reached N=32 concurrent short Codex/GPT-5.6 Sol turns without auth
mutation or rate failure, which is enough to remove the "adapter missing" blocker. It does not prove
longer-run capacity, refresh-under-load, or future 429/backoff behavior. ④ still must not assume Codex
throttling parity with Opus; the Codex sub-ceiling's concrete numbers and capacity semantics should be
set from runtime evidence and admission-control runs.

The current adapter-level slice gives those runs better evidence: each Codex spawn stamps
`codex_seat_id`, `auth_version`, and access-token remaining seconds into the binding/evidence index.
The product seat-pool cap and queueing policy are still open admission-control work.

---

## 4. The usage model + failure behavior

This **replaces** the stale premise. ARCHITECTURE.md L360–362 currently asserts *"Compute is prepaid
(Claude Code subscription) — the limits are session-based, not token-based in practice"* and *"No hard
concurrency limits have been found on Claude Code Max ... a configurable **soft cap** ... the system is
not expected to hit hard technical limits."* That is contradicted by the locked model (L1–L4 + L5+ all
Opus on **one** Max account sharing 5h/weekly windows + per-account throughput) and by a real dry-run
(a Codex account silently degraded a model override). There is **zero** 429/rate-limit/backoff/
backpressure handling anywhere today. ④ supplies the usage model that replaces it.

### 4.1 Defined behavior on 429 / overload / usage-exhausted

On a rate-limit / overload / usage-window-exhausted signal from a runtime, the defined behavior is:

1. **Release the claim** (the §3.3 `claimed → planned` rollback edge — same as over-ceiling) so no
   actor opens on a throttled account and the slot is reclaimable.
2. **Backoff-and-retry** (transient throttle) **or queue** (sustained) — never silent-fail. For a
   *temporary* 429, a bounded backoff-and-retry against the same claim is the right response (cf. §2.3
   FORK Option B). For a *sustained* usage-exhaustion, hold the node `planned` and resume when the
   window resets.
3. **Backpressure** — as the account's usage window **nears** exhaustion, **pause admitting NEW
   spawns** for that pool (stop minting fresh claims) *before* the hard 429, so the fleet glides into
   the limit instead of slamming it. This is proactive (window-aware) and distinct from reactive 429
   handling.

> **Staged dependency (mirrors §5 Option C "session axis first, headroom later").** The two legs of
> this behavior land at **different times**:
> - The **REACTIVE 429 leg** (step 1 release + step 2 backoff/queue) works from the runtime's **own
>   throttle signal** — no usage telemetry needed — so it is available **first**, with the session axis.
> - The **PROACTIVE window-aware backpressure leg** (step 3, "window *nears* exhaustion") requires
>   knowing how much headroom remains, which depends on the **§5 usage-data read mechanism**
>   (ARCHITECTURE.md L360 usage-exposure) landing — the **same** dependency as the headroom cap axis.
>   Until that read path exists, backpressure degrades to the reactive leg only.

> **Queue here obeys the §2.3 REQUIRED queue invariant.** A 429/usage-window node held `planned` MUST
> carry `queued_since` and bounce-to-parent past `max_queue_age` — a throttle-hold is exactly the case
> the silent-queue-starvation finding targets (a pool whose window never resets would otherwise hold
> the node forever).

### 4.2 The distinct rate-limit failure class — NEVER mis-classified as a semantic FAILED

This is a **hard correctness constraint**, modeled on ①'s `auth_expired` precedent (DAEMON.md §6.3):
①  made `auth_expired` a **distinct** spawn-failure class (separate from `model_unavailable`) precisely
so a credential problem isn't read as a fleet-wide model-outage storm. ④'s rate-limit/usage-exhausted
is the **exact parallel** and gets the **same** treatment.

A 429/usage-exhausted event is a **pre-actor admission outcome** (release claim + backoff/queue/
backpressure). It is **NOT** a run-ledger terminal signal. The run-ledger terminal vocabulary
(DAEMON.md §3.5/§3.6) is the closed set `signal_DONE` / `signal_FAILED` / `signal_ESCALATED` +
daemon-stamped `died_infrastructure` / `died_methodology` / `stale_return_ignored`. **A rate-limit is
none of these** — it happens *before* an actor exists, so it never enters that vocabulary at all.

Why this is load-bearing: the watchdog's **sign-off accounting** reads `terminal_signal`
(§3.5/§3.6). If a 429 were logged as `signal_FAILED` / `died_*`, it would corrupt that accounting —
the watchdog would count a throttled-but-perfectly-healthy node as a **semantic agent failure** and
trigger ②'s recovery/respawn machinery against a node that did nothing wrong. So the rule is absolute:

> **A rate-limit/usage-exhausted event MUST be its own admission-outcome class. It MUST NOT be written
> as `signal_FAILED`, `died_infrastructure`, `died_methodology`, or any `terminal_signal`. It is a
> pre-actor release-and-retry, recorded as an admission event, invisible to the sign-off check.**

The no-mis-classification guarantee covers the **queue disposition** too, not only the "never enters
the vocabulary" framing: a node **released to `planned`** and queued has **no lease, no pane, and no
`slot_claimed`/`spawned` row**, so it is **OUT of the watchdog's watched set** entirely (WATCHDOG §3.4
steps 1–3 are evidence-gated on a stale lease / a probe-able `pane_pid` — a queued node offers neither).
The recovery machinery therefore never reads a throttled-and-queued node as `FAILED`. (The flip side —
that the *absence* of a watcher is itself a silent-stall risk — is exactly why the §2.3 queue invariant
mandates `queued_since` + `max_queue_age` bounce-to-parent: the queue's liveness owner is the bound, not
the watchdog.)

This extends E32's no-silent-degradation discipline (the forbidden mode is *"silent degradation —
adapter quietly running a child on something other than configured and nobody noticing,"* grounded in
the real dry-run where a Codex account silently fell back). A 429 must likewise **never** be silently
absorbed — it is released, classified, and retried/queued, never best-effort-launched and never
swallowed.

> **NOTE — two ceilings, do not conflate (mirrors the floor-vs-cap caveat of §7).** ④'s **system
> max-in-flight** ceiling is distinct from ②'s **per-node `recovery_attempt_ceiling`** (WATCHDOG
> §3.5, a still-OPEN per-node respawn bound). One bounds *how many agents run at once*; the other
> bounds *how many times a single node is respawned*. They are different mechanisms on different axes
> — keep them separate.

---

## 5. Cap denomination — sessions vs usage headroom

Gap-review blocker #8 hands ④ an **open decision**: *"decide whether the cap is denominated in
**sessions** or **shared-account usage headroom**"* — they are conflated today (*"single cap pools both
runtimes"*).

- **Sessions axis** — bound concurrent live tmux actors. The authority is **④'s atomic
  reserved-slot count** (the `in_flight` integer per pool, §2.2), incremented at the §6.1 claim and
  decremented on terminal collapse — **reconciled against** ①'s live-tmux sweep (§5.4; the sweep is a
  drift check that corrects a leaked slot whose actor died without a clean terminal transition, the
  same way reconcile corrects ledger drift — NOT a second source of truth). This is the **only**
  representation that supports **atomic claim-before-launch**, which the hard gate requires; a count
  merely *recomputed* from the sweep is a snapshot taken *after* the fact and cannot **reserve** a slot
  *before* launch. **Cheap in ①** — the *substrate* (single writer + per-node `liveness_state`) is
  pre-seated, so ④'s session count is a small field maintained inside the existing claim transaction
  (or a prefix-scan over the ledger), **not** new infrastructure — **but the count itself is ④'s
  addition, not a pre-existing ① field** (§1.3 note).
- **Usage-headroom axis** — gate on remaining Max-account quota in the current 5h/weekly window. This
  is **NOT in ①** — it requires a mechanism to **read Claude Code usage data** (ARCHITECTURE.md L360
  notes *"Claude Code exposes usage data to the user; the system needs a mechanism to expose it to
  L1/L2"*). Until that read mechanism exists, the headroom axis cannot gate.

> **FORK — for user review: cap denomination.**
> - **Option A — sessions only.** Use ④'s atomic in-flight slot count (§2.2) as the sole cap. Pro:
>   minimal machinery — the *substrate* is pre-seated in ① (one writer, `liveness_state`), so the count
>   is a small field inside the existing claim transaction, not new infrastructure (the count itself is
>   still ④'s addition). Con: a small number of sessions can still exhaust the usage window (token-heavy
>   work), so a pure session cap can over-admit relative to actual headroom.
> - **Option B — usage headroom only.** Gate on remaining account quota. Pro: directly tracks the real
>   constraint (the account window). Con: requires the unbuilt usage-data read mechanism; no session
>   count means nothing bounds tmux/RAM/FD footprint (§6).
> - **Option C — both (RECOMMENDED).** The session count is the **resource-footprint** ceiling (caps
>   RAM/FD/tmux per §6); the usage-headroom signal drives **backpressure** (§4.3 — pause NEW spawns as
>   the window nears exhaustion). They gate **different** constraints and compose cleanly: sessions
>   bound the machine, headroom bounds the account.
> - **Recommendation: Option C, staged.** Ship the **session axis first** (cheap — substrate
>   pre-seated in ①, count is ④'s small addition; gates RAM/FD/tmux immediately). Add the **headroom
>   axis** when the usage-data read mechanism lands —
>   tracked as a dependency on the ARCHITECTURE.md L360 usage-exposure mechanism. This way ④ has a real
>   hard cap from day one (sessions) and grows the headroom-aware backpressure when the read path
>   exists, rather than blocking the whole gate on unbuilt usage telemetry.

---

## 6. The per-session resource envelope — measure, don't guess

The cap MUST derive from **measured** resource headroom, not a guess. The legacy **20–64** figure was a
**coordination-quality** guess (*"how many parallel agents can be meaningfully coordinated without
quality degrading"*) made **before the tmux model was locked** — it is NOT a measured resource
ceiling, and ④ MUST NOT carry it forward as one.

### 6.1 What to measure (on the first commissioning runs)

Per arch-gap-review §IMPORTANT item 12, measure per-interactive-session:

- **idle-coordinator RAM** (a TUI holding RAM while waiting/coordinating),
- **active-worker RAM** (a session doing work),
- **FD count** (open file descriptors per session),
- **max stable window count** (how many tmux windows/panes stay stable before the tmux server or
  per-pane polling degrades).

Concrete known footprint data points to fold in (PINNED-CC.md): each L1–L4 / L5+ session runs a
**pinned, vanilla, isolated** Claude Code — the native binary
(`.cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe`, Mach-O arm64) is **~214 MB**, and
each session needs its **own** `CLAUDE_CONFIG_DIR` + `CLAUDE_CODE_TMPDIR` (per-session config/tmp dirs
that add to the FD/disk footprint). The ~214 MB binary image is the **only** hard measured figure in
the source; RAM working-set, FD count, and stable window count are **NOT** in any source and must be
measured.

### 6.2 The cap derives from headroom; fold into the false-idle first run

The live ceiling **derives from** measured headroom (machine RAM / FD limits / tmux stability ÷
per-session footprint), per pool. This measurement **folds into the SAME first commissioning run** that
measures the detector's false-idle rate (arch-gap-review §IMPORTANT 12 — *"Fold into the same first-run
test as false-idle rate"*) — one instrumented pressure-up run produces both.

### 6.3 Open seams that bear on the envelope

- The **per-session unit** (tmux window vs bare process vs `CLAUDE_CODE_REMOTE`) should be confirmed
  against the DAEMON tmux model before the envelope is finalized; ① uses **detached tmux sessions
  named from the one-spine address** (§6.2), which is the unit to measure.
- **Auth multiplexing under load** is unverified: if many sessions share one `CLAUDE_CODE_OAUTH_TOKEN`
  / one Max account, whether per-session auth multiplexes cleanly bears on the sessions-vs-headroom
  denomination (§5). PINNED-CC.md flags the pinned install is not yet authed and auto-update-off is
  unconfirmed.

---

## 7. CAVEAT — compute-saturation-enforcement is a FLOOR, not the cap

A future implementer will encounter the recovered phase-2 **"compute-saturation-enforcement"** prior
art and may be tempted to wire its numbers as ④'s ceiling. **Do not.** It is the **opposite
mechanism.**

- It is a **per-loop, per-round minimum-parallelism FLOOR**: *minimum* 2 active streams (when a primary
  actor is live and ≥1 independent side question exists), *preferred* 3 during any wait/convergence/
  review gate, *upper bound for a single round* 4 (*"Beyond that ... split into a new round or collapse
  redundant branches"*). The manifest shape is `target_live_count: 4 / minimum_live_count: 3 /
  maximum_live_count: 4` — **per-ROUND, per-loop**, for ONE orchestrator's branches.
- It **PUSHES parallelism UP** to a floor. Its catastrophic signal is *"Fewer than 2 active streams
  exist while ≥2 independent unresolved surfaces remain"* — it fires when there is **too LITTLE**
  parallelism, to force more.
- ④'s admission gate is the **inverse**: a **system-wide shared in-flight CEILING** across ALL agents
  in the one serialization domain, firing when there is **too MUCH** parallelism, to stop more.

> **Do NOT import the "4" (maximum_live_count / "upper bound for a single round") as the system
> admission ceiling.** It is a per-round anti-sprawl threshold that triggers split-or-collapse for ONE
> loop — not a per-account/per-runtime in-flight cap. Conflating them wires a parallelism FLOOR-region
> guard as the global CEILING — the exact mis-import this caveat exists to prevent.

The one thing ④ **does** reuse from this prior art is the **atomic claim-slot-before-launch mechanic**
(*"claim the next spawn slot in the control plane before launching a branch; never spawn first and
validate later"*) — see §2.2. Same atomic ordering, opposite bound (round-floor vs shared-ceiling).

---

## 8. Deferred-with-triggers table

Each deferral records its **exact** trigger. "Done for ④" = these are deferred **with the trigger
written**, not built.

| Deferred item | Owner | Pull-in trigger (verbatim) | Design exists? |
|---|---|---|---|
| Admission-control **logic** + 429/backoff/queue **implementation** + **live ceilings** + per-runtime ceiling **numbers** | ④ | **"run wide enough to hit a rate/resource limit"** (runtime-decisions §3) | partial — **DESIGNED NOW:** the spawn chokepoint + seat (§2.1); both counter edges (claim-increment + terminal-decrement, §2.2); the `in_flight` field as ④'s deliverable (§1.3); the REQUIRED queue invariant `queued_since`+`max_queue_age` (§2.3). **DEFERRED:** the live backoff/queue code + concrete ceiling/queue-bound *values* |
| Per-session **resource envelope** measurement → derived cap | ④ | same trigger; measured on the **first commissioning runs** (pressure-up); folds into the false-idle first run (§6) | plan only (§6) |
| Usage-**headroom** axis of the cap (vs sessions) | ④ | depends on the **usage-data read mechanism** (ARCHITECTURE L360) landing; session axis ships first (§5) | decision framed (§5); read-path unbuilt |
| Codex sub-ceiling concrete numbers + 429 semantics | ④ | a clean current-HEAD Codex-backed run plus admission-control pressure (§3.4) | adapter fill landed; product seat-pool cap still open |
| **Cost-keyed resume-vs-fresh replay policy** (token threshold above which wake = fresh-respawn; count replay tokens vs budget) | ④ | **"replay token cost becomes material"** (runtime-decisions §3) — a **distinct** trigger from the admission bundle | no |
| **GUI cost view** (OTEL cost surfaced in the fleet monitor) | GUI pass | a **later GUI pass** (read-model update; not ④'s seat work) | no |

The two admission-related triggers are **distinct** and fire on **different** signals: the
admission/backoff/ceilings/envelope bundle fires on **concurrency/rate** (*"run wide enough to hit a
rate/resource limit"*); the cost-keyed replay policy fires on **replay-token cost** (*"replay token
cost becomes material"*). Keep them separate.

---

## 9. What ④ provides — and how it gates the other clusters for free

### 9.1 What ④ provides

④ provides **the admission SEAT binding + the usage-model spec + the measurement plan + the recorded
triggers**. It provides **no** running gate, **no** live ceilings, **no** backoff/queue
implementation, **no** cost view — all deferred (§8). The seat itself is already reserved in ① (§9 /
§1.4 deferral table); ④'s contribution is the **specification** that attaches there on the trigger.

### 9.2 How ④ gates every other cluster's spawns automatically

Because **every** launch in the system funnels through the single §6.1 chokepoint (§2.4), ④'s gate at
that one seat covers **all** of them with **zero** per-cluster work:

- ②'s **recovery respawns** (WATCHDOG §10) route through §6.4 → §6.1 → the seat → ④'s ceilings/backoff
  gate them. WATCHDOG §11.2: ②'s respawn path *"goes through the same chokepoint, so ④'s ceilings/
  backoff gate ②'s respawns automatically."*
- ③'s **canonical question/answer path** (TRANSPORTS §0) and **gate-firewall fresh-spawn** (TRANSPORTS historical §4.3)
  route through the same chokepoint → ④ gates them automatically. TRANSPORTS §8: *"both route through
  that same chokepoint, so ④'s ceilings/backoff gate them automatically."*

### 9.3 What ④ exposes back to ②/③

- **To ③'s severity→channel mapping (TRANSPORTS §5.2):** ④'s eventual "deferred due to resource
  ceiling / at usage-window limit" notice surfaces to the user through ③'s channel (a routine
  pull-inbox item for a soft ceiling; a blocking push when a ceiling blocks required work). ③ defines
  the channel; ④ defines when to use it.
- **To ③'s pull-visibility read op (TRANSPORTS §6):** ④'s resource accounting reads node state through
  ③'s existing glob read-path — ④ adds **no** parallel read channel.

### 9.4 Net

④ = **one gate, one seat.** It binds to ①'s claim-slot pre-step (DAEMON §6.1), maintains its own
`in_flight` slot count **inside** the single-central daemon's one serialization domain (§2.2/§2.4-A —
the count is ④'s addition, the substrate is ①'s), with **two symmetric edges** (claim-increment at the
seat, decrement on terminal collapse §3.6/§5.4) reconciled against ①'s live-tmux sweep, releases
over-ceiling/throttled claims via the `claimed → planned` rollback edge (§3.3), bounds every queue with
`queued_since`+`max_queue_age` (§2.3), keys per-runtime sub-ceilings off the config runtime (§3.2/§6.3),
keeps rate-limits out of the terminal vocabulary (§3.5/§3.6), derives the cap from measured headroom
(§6), and defers every live implementation behind the recorded triggers (§8). It redesigns nothing ①
built, adds no new lock/writer/spawn path, and gates ②'s and ③'s respawns for free.
