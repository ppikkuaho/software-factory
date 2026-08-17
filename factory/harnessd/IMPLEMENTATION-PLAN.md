# harnessd — Walking-Skeleton Implementation Plan (Phase 5a)

> **2026-07-13 (L5-runtime unification):** the Codex increments recorded below are a **historical record** of the now-dormant Codex-adapter path. L5 runs GPT-5.6 Sol on the Claude Code harness (option (a) ruling); the Codex adapter stays registered as dormant capability. See `design/working-notes/L5-RUNTIME-UNIFICATION-RULING-2026-07-13.md`.

> **CURRENT ARCHITECTURE OVERLAY — 2026-07-24.** The numbered 0–16 queue and older pseudocode
> remain implementation chronology, not the current architecture where they conflict. Current
> extensions are:
>
> - `lifecycle.py` + `genesis.py`: run-scoped launchd protection, one-shot start request, reconcile
>   on crash/restart, clean exit only after a definitive no-live-build sweep;
> - `turn_state.py` + `turn_state_hook.py`: per-runtime hook profiles, durable running/end/tool
>   events, live owed checklist, and the shared return-contract three-way check; detector evidence
>   is fallback for process death, hung tools, or missing/hook-less edges. Ordinary observations
>   stay in the existing node-local turn files and refresh the derived binding checkpoint without
>   a duplicate WAL row; `harnessd/wal_policy.py` is the exhaustive
>   event-classification authority and `tests/test_wal_diet.py` proves authored
>   spelling completeness;
> - `messages.py`: sender-owned direct-edge messages, open questions/answers, a daemon-owned
>   zero-knob wake table, and product/review-check cohort barriers;
> - `notary.py` + `contracts.py`: one stamp/receipt/check authority, intent revision enforcement,
>   contract version/lineage/holder receipts, amendment wakes, and fenced explicit rebind;
> - `traceability.py` + `plan_alignment.py`: one closed trace-stanza parser shared with the
>   return-contract boundary, plus the gate-hard plan-submission graph walk (forward design/test
>   coverage, per-MNF failure-path presence, backward orphan/DR checks, deferred/DD listings, and
>   content-addressed coverage reports);
> - `plan_alignment_cell.py`: physical semantic-manifest partition, exact seat input rosters,
>   closed report shapes, daemon-owned cohort dependencies, content-addressed evidence/elevations,
>   once-per-intent atomization reuse, and trace-graph incremental re-gating; production seat
>   dispatch is fail-closed pending notary-backed owner+director instruction calibration;
> - `spawn/chokepoint.py` + adapters: one gateway with hook installation, notary receipts, and
>   strong-form local-file blinder profiles (`observe`/`enforce`);
> - `observability.py`: one direct, read-only live-or-dead binding/artifact join with terminal,
>   JSON, and self-contained HTML/SVG journey renderers; captures default to
>   `.harnessd/views/`, outside the node tree, and no daemon poll maintains them;
> - `promote.py`: L1-accept-gated control-plane delivery; and
> - `harnessctl.py` plus `.claude/skills/l1-l5-harness/`: the verb layer and versioned Claude
>   Code operator surface, including offline `view` / `journey` reads.
>
> The IPC response leg for the fire-and-forget `turn-hook-adopt` command does not mint
> `ipc_request_failed` when the non-listening caller has already closed. Decode/handle/serve faults
> and response failures for request/response commands remain typed WAL evidence.
>
> Current disk truth uses seat-qualified `.turn-state.*`, `.turn-events.*`, `.owed-checklist.*`,
> `.sign-off.*`, `.signal.*`, and `.inbox.*` files plus `messages/`, `contract-revisions/`,
> `contract-rebind/`, notary stamps/receipts, and immutable review candidate snapshots. Generated
> status views join those facts on read; the historical daemon aggregate relay has been removed.
> Legacy `*.aggregate.md` scanner exclusions remain replay compatibility, never a current writer.

**Status:** code-architecture + build plan. NOT system design — the system design is settled in
`../design/DAEMON.md`, `../design/WATCHDOG.md`, `../design/TRANSPORTS.md`, `../design/SCALE.md`
(repo-root `design/`; this plan lives in `harnessd/`, so all `design/…` section citations below
resolve to `../design/…` — verified present at repo root). This document maps
that design onto Python modules, interfaces, on-disk files, tests, and an ordered build queue that
BUILD workflows execute increment-by-increment.

**Scope = exactly the 11 INCLUDE-in-v1 items** (runtime-decisions §2) **plus the minimal ③ wake**
(sender/count send-keys notification + notification-driven inbox read) **and the ④ admission SEAT** (claim-slot pre-step,
count present, ceilings deferred). **Everything else is DEFERRED** — and named as such in §"Deferred"
below so no increment silently widens scope.

**Done-when for the whole skeleton (the Phase-5 gate):** the daemon boots; spawns L1 in-role via a
per-spawn `.identity-prompt.md` (`operational/shared/system-prompt.md` first, then the selected L1
identity trio) with the broader protocol/load documents delivered in the brief's load-manifest
(H40 resolved, LR-4 amended — see `../operational/shared/agent-definition-principles.md` §4,
`../operational/shared/system-prompt.md`, and `../design/ROLE-RESOLUTION.md`); the ledger reconciles on restart; one agent is spawned → detected →
signed-off → collapsed through the single writer with fencing active; AND a `kill -9` of the daemon
followed by relaunch recovers state from the ledger (binding-ledger + WAL, including a torn tail).

**HARD INVARIANT (threaded through §7 and a dedicated test):** OAuth / subscription auth only.
Claude Code via `CLAUDE_CODE_OAUTH_TOKEN` (Max subscription); Codex via its ChatGPT-subscription auth.
NEVER a raw `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`; NEVER `--bare` (H40: the only path that forces
API-key auth and breaks OAuth). The spawn adapter enforces this by construction — it refuses to spawn
if the resolved env/argv would fall back to an API key.

**Code is suspect.** `control_plane.py` (1706 lines) and `watchdog.py` (446 lines) under
`research/orchestration-frame/self-improvement-harness/` are RECOVERED prior art. We PORT their
durability discipline, GENERALIZE their one-manifest model to per-node bindings, and write NEW the
parts that have no precedent (tmux liveness, fencing, reconcile, intent-first WAL, torn-tail
recovery). We rewrite clean — we do not import wholesale. See §6 for the line-by-line map.

---

## 1. Module decomposition

Tracked code lives under `harnessd/`. Each module maps to a DAEMON/WATCHDOG section. "PORT /
GENERALIZE / NEW" tags the origin relative to the recovered code.

| Module | Responsibility | Design section | Origin |
|---|---|---|---|
| `harnessd/store.py` | Atomic-IO + lock primitive. `atomic_replace` (tmp+flush+fsync+os.replace), framed single-write+fsync append, `file_lock` (fcntl SH/EX context manager), `normalize_scalars`. The fsync-before-replace line is load-bearing durability — kept verbatim. | DAEMON §4.3, §4.4 | `atomic_replace`/`file_lock`/`normalize_scalars` = PORT (`save_manifest` L191-197, `control_plane_lock` L248-256, L171-180). `append_framed` = PORT (fsync discipline of `append_ledger` L241-246, verified plain-json no-frame) + NEW (the `<len>\t` frame is net-new on both write and read) |
| `harnessd/clock.py` | The ONE canonical clock. `now_utc()` → tz-aware UTC ISO-8601; `age_seconds()`; `parse_iso()`. All freshness/lease math goes through it (F-019 was a UTC-vs-local stale misdiagnosis). | DAEMON §4.6, WATCHDOG §3.3 | PORT-and-FIX (`now_iso` L167 used local `astimezone()`) |
| `harnessd/states.py` | Static legality table + enums. The generic per-node lifecycle `planned\|claimed\|spawning\|running\|blocked\|done\|failed\|dead` with rollback edges (claimed→planned, spawning→planned/failed) and re-adopt edges (running→claimed, dead→claimed) as FIRST-CLASS members. `liveness_state` enum (working\|waiting\|idle\|dead). The §3.6 terminal-vocabulary mapping table. | DAEMON §3.3, §3.6; WATCHDOG §2.1 | GENERALIZE (replaces reviewer-loop `KNOWN_STATES`/`ALLOWED_TRANSITIONS` L26-88 contents; keeps the mechanism) |
| `harnessd/ledger.py` | Binding-ledger.yaml I/O + run-ledger.jsonl WAL I/O. `read_binding`/`write_binding` (whole-map atomic-replace), `append_wal` (framed `<len>\t<json>\n` + fsync), `load_wal` (REWRITE — torn-tail-tolerant), `build_wal_record`, `next_seq`, `all_nodes`, `last_applied_seq` watermark. | DAEMON §3.2, §3.4, §3.5, §4.4 | PORT discipline + NEW torn-tail + GENERALIZE one-manifest→per-node |
| `harnessd/wal_policy.py` | The one executable event-disposition authority consumed by recovery and the exhaustive authored-vocabulary test. Only `REPLAY` rows may merge a binding delta; the generic operator-repair transition retains its strict one-generation CAS shape fallback. | DAEMON §3.5, §4.4 | NEW (WAL-diet table promoted into production when equal-generation replay made disposition load-bearing) |
| `harnessd/validate.py` | Pure `validate(candidate_binding, wal_tail) -> (errors, warnings)` for strict pre-commit lifecycle admission, plus pure `validate_committed_snapshot(candidate_binding, node_effects)` for the read-only whole-ledger projection. The latter validates the binding effect selected by the checkpoint watermark from recovery's shared primary-plus-related effect projection and never borrows a foreign effect. | DAEMON §4.2 | GENERALIZE (`validate()` L618-1035 discipline only; vocab dropped) |
| `harnessd/fencing.py` | `mint_owner_token`, `advance_epoch`, `check_fence`. The self-fencing token `address:subagent-id:session-uuid:lease_epoch` — comparing tokens compares epochs. Folds INTO the executor CAS as a 3rd precondition. | DAEMON §8 | NEW (recovered had `activity_lease`, no epoch/token/CAS) |
| `harnessd/executor.py` | THE single-writer transition primitive + commit funnel. `transition()` (CAS on expected_state + per-node expected_generation + expected_owner_token; legality gate; validate-before-commit), `claim()` (transition variant → claimed), `commit()` (intent-first: WAL append+fsync FIRST, then binding atomic-replace), `heartbeat`/`release_lease`/`watchdog_checkpoint`. Holds the one exclusive serialization lock. The ONLY code path that mutates binding state. | DAEMON §4.1-4.5 | GENERALIZE `cmd_transition` L1505-1610 + NEW fencing-into-CAS + REVERSE `commit_mutation` L1264 ordering |
| `harnessd/detector.py` | Thin liveness detector behind the stable `liveness(node) -> {state, last_progress}` interface. v1 floor = JSONL-growth + pane-alive ONLY. PURE-READ — returns a verdict; the watchdog writes it through the executor. Multi-signal fusion DEFERRED behind this signature. | WATCHDOG §2.1-2.4; DAEMON §5.2 | NEW (recovered `derive_lease_health` L531 is heartbeat self-report — explicitly the thing §1 says NOT to do) |
| `harnessd/detector_signals.py` | The three raw signal readers behind their own functions: `jsonl_progress(node)`, `pane_alive(node)`, `pane_pid_cpu(node)`. CPU remains outside the general liveness verdict and is read only for the current blocked-input discriminator after in-flight + silence prerequisites hold. Split out so deferred fusion is added without touching verdict logic. | WATCHDOG §0.2, §2.3 | NEW |
| `harnessd/watchdog.py` | The PER-NODE verdict+policy function invoked by the daemon's in-process reconcile sweep (NOT a standalone polling daemon — WATCHDOG §3(b) drops the recovered `main()/--watch` loop). Owns: leaf sign-off-or-fail path, the idle→prod→mark-FAILED ladder, coordinator process-death branch, renew-on-progress edge-trigger, and the signature-gated blocked-input verdict that can only cancel with Escape. Two-counter discipline (`stale_check_count` for leaf prods vs `recovery_attempts` for coordinator cycles); blocked-input incidents touch neither. | WATCHDOG §0.2, §3.2, §4, §5.1 | PORT ladder SHAPE (`derive_checkpoint` L105-276) + NEW signal source |
| `harnessd/reconcile.py` | The tmux↔ledger sweep. `replay_wal()` (deterministic WAL re-apply with generation pre-image check), `reconcile_on_restart()` (boot-once: replay then sweep), `reconcile_tick()` (continuous, on the watchdog timer; edge-triggered). Computes divergence, applies the two unambiguous resolutions (adopt / leaf-necro), escalates the rest. Calls executor for every mutation — never raw. | DAEMON §5.1-5.3 | NEW (recovered code has no reconcile — trusts the manifest) |
| `harnessd/spawn/chokepoint.py` | The ONE spawn path. `claim_and_spawn` / `resume` / `release_claim` / `collapse`. STEP0 pause-check → STEP1 CAS-claim (= ④ in_flight CLAIM-increment) → STEP2 adapter+neutral-brief → STEP3 pin-confirm → STEP4 open tmux + record session_uuid/model_used → STEP5 claimed→spawning→running. NOT a writer — calls `executor.transition()` for every state change. On any post-claim failure, CAS-releases the claim (claimed→planned, bump epoch). | DAEMON §6.1, §6.4 | NEW (recovered `launch_recovery` L84-102 is a generic Popen recovery launcher, not tmux/in-role) |
| `harnessd/spawn/adapters/base.py` | The `RuntimeAdapter` port (hexagonal). Abstract `pin_and_open(brief, level_config, tmux_target, env) -> SpawnResult`; injects ONLY the 3 runtime-specific things (tool manifest, harness invocation, output format) over the runtime-NEUTRAL contract. Spawn-failure classification: `auth_expired \| model_unavailable \| override_rejected \| runtime_down`. | DAEMON §6.3; runtime-and-model-map E31/E32 | NEW |
| `harnessd/spawn/adapters/claude_code.py` | The Claude-Code adapter (fully specified by H40, amended by LR-4). Boots the pinned binary in detached tmux with `--system-prompt-file <node-workspace>/.identity-prompt.md`, a composed file containing the constant shared minimal prompt first and the selected identity trio after it, the exact isolation env, OAuth via file/FD. Broader protocol docs are delivered as documents the agent reads via the brief's load-manifest. Verifies binary version/hash. ENFORCES OAuth-only (refuses `--bare`, refuses any `ANTHROPIC_API_KEY`). | DAEMON §6.2; H40-FINDINGS; PINNED-CC | NEW |
| `harnessd/spawn/adapters/codex.py`; `harnessd/spawn/codex_auth.py` | The Codex adapter fill. Boots pinned Codex/GPT-5.6 Sol with native Codex base instructions plus harness task context in the neutral brief/boot prompt, preflights canonical ChatGPT/OAuth access-token freshness before pane-open, seeds a per-worker isolated `CODEX_HOME` under `.codex-pinned/seats/` with no usable child refresh token, stamps `codex_seat_id` / `auth_version` evidence, discovers the real rollout transcript by cwd, and keeps the shared OAuth-only negative gate. | DAEMON §6.3; T38 | NEW |
| `harnessd/spawn/tmux.py` | Thin tmux wrapper: `create_detached`, `capture_pane`, `list_targets` (pane_pid + pane_dead for reconcile/detector), `kill` (only via executor-stamping path). Deterministic first-boot trust (pre-seeded `CLAUDE_CONFIG_DIR` trust state / non-interactive permission mode) — NOT a send-keys race against the trust dialog. | DAEMON §6.2 | NEW |
| `harnessd/spawn/oauth_guard.py` | The OAuth-only enforcer (the test target). SPLIT guard: `assert_no_api_key(env, argv)` (runtime-AGNOSTIC negative invariant — always on, Claude+Codex) + `assert_pane_env_isolated(pane_argv, server_env)` (closes the tmux-server env-leak) composed in `assert_oauth_only(env, argv, pane_argv, server_env)`; plus the CLAUDE-SPECIFIC positive `check_credential_health(env)` (token present + unexpired). Raises `ApiKeyForbidden` / `AuthExpired`. `auth_expired` is a DISTINCT class so a token lapse reads as "refresh the token", not a fleet-wide model-outage storm. | DAEMON §7, §8; H40 | NEW |
| `harnessd/spawn/brief.py` | Assembles the runtime-NEUTRAL task contract (identity/address, spec pointer, frozen acceptance ref, interface contracts, constraints, workspace location, reporting expectations) and the DELTA brief for resume (what changed since prior incarnation, pointing at the durable work node). | DAEMON §6.3, §6.4 | NEW |
| `harnessd/traceability.py` | The one parser for the closed `{id, serves, kind, level, node}` trace stanza. Return-contract and plan-alignment callers retain their own domain/refusal projections but cannot fork the syntax. | PLAN-ALIGNMENT-GATE §Requirements Traceability | NEW (2026-07-24 Q3) |
| `harnessd/plan_alignment.py` | Reads the current notary-checked intent receipt and the L2 package sidecar, builds the exact submitted trace graph, gate-hard checks forward design/test coverage, per-MNF failure-path presence, and backward orphan/DR coverage, and writes the content-addressed coverage report. No semantic judgment or agent seat. | PLAN-ALIGNMENT-GATE Checks 1–3 | NEW (2026-07-24 Q3) |
| `harnessd/plan_alignment_cell.py` | Validates the physical construction/verification partition; generates exact blind seat rosters; checks reconstruction, comparator, coherence, and atomization report shapes; joins evidence and required elevations; scopes same-intent repairs from stamped trace indexes; caches atomization by intent fingerprint. | PLAN-ALIGNMENT-GATE trimmed Q4 cell | NEW (2026-07-24 Q4) |
| `harnessd/instruction_calibration.py` | Generic notary-backed calibration mint/check for exact versioned instruction files: immutable adjacent record + receipt, prior/current fingerprint lineage, ratification-note evidence, channel, and byte-drift refusal. Q4 and Q5 supply their own channels and typed defect codes; production callers fail closed. | PLAN-ALIGNMENT-GATE Q4; HIGHER-LEVEL-GATES Q5 | NEW (2026-07-24 Q5 extraction; mint path added Q8) |
| `harnessd/product_probes.py` | Strictly parses exact live frozen requirement text and confirmed intent journeys, joins them with current Q3 MNF failure-path facts and candidate identity, discovers only artifact-declared invocations, freezes the content-addressed probe roster, and creates separate manifest-verified writable candidate copies for the two L2+ probes. | HIGHER-LEVEL-GATES L2+ product probes | NEW (2026-07-24 Q5) |
| `harnessd/necro.py` | Basic necro: delta-brief assembly seam for `--resume`. Does NOT own a second copy of the gate-firewall — the NEVER-RESUME-ACROSS-THE-GATE check lives in EXACTLY ONE place (`chokepoint.resume`, the only path that can issue `--resume`); `necro.resume_brief` calls that single check rather than re-implementing a `raise`. Built + tested as part of Increment 10 (no separate increment). | DAEMON §6.4; runtime-decisions §2 item 8 | NEW |
| `harnessd/genesis.py` | First-boot sequence. `run_genesis`: briefly take the `.harnessd.lock` EX domain for the runtime.json/precondition step (the lifetime instance guard is the daemon's `.harnessd.instance.lock`, already held) → write runtime.json → `preconditions()` (OAuth health + pinned-binary hash, fail-loud) → `reconcile_on_restart` → if no live non-terminal L1 binding: author the L1 brief, optionally pre-seed the configured initial intake into L1's durable inbox, then `spawn.claim_and_spawn(L1-root, role_variant='L1', parent=null)` — boots with the composed L1 `.identity-prompt.md` + broader L1 load-manifest assembled into the brief — else RESUME (no double-spawn, F35). | DAEMON §7 | NEW |
| `harnessd/lifecycle.py` | Run-scoped process lifecycle only (no ledger/executor imports): duplicate probes, serialized one-shot request publication, tracked-plist rendering, on-demand launchctl bootstrap, crash-only protection, and bounded after-parent idle bootout. | DAEMON §2.2, §7 | NEW (2026-07-24 run-scoped ruling) |
| `harnessd/daemon.py` | The harnessd run loop + PID/lock/runtime.json. Acquires the persistent `.harnessd.instance.lock`; a claimed request runs full genesis, a crash restart runs reconciliation-only, and the loop returns after a completed tick definitively finds no nonterminal binding. Writes the lock-free status sidecar and schedules clean-idle bootout; launchd protects crashes only while the run is live. | DAEMON §2.2, §2.3, §5.2 | NEW (poll cadence SHAPE from recovered `main` `--watch` L406-427; lifecycle superseded 2026-07-24) |
| `harnessd/harnessctl.py` | CLI client — NOT a state writer. `start` is the narrow pre-daemon lifecycle exception (launchd/files only); status/attach/read/mutation helpers otherwise use daemon IPC, and every state mutation is performed inside the daemon's one lock. This is the verb layer under the versioned Claude Code operator skill. | DAEMON §4.3, §7 | GENERALIZE (`build_parser` L1613-1696 subcommand structure → node-addressed; operator surface amended 2026-07-25) |
| `harnessd/config.py` | Reads config-time seats the rest of the code must NOT hardcode: `LevelConfig` per level (model/runtime/`role_variant`/tool_manifest) plus the CONSTANT shared prompt base `system_prompt_file = operational/shared/system-prompt.md` (identical L1–L5, not a per-level role path), the per-state suspicion windows `W(state)` (placeholder constants in v1, see FORK-W), the pinned-binary version/hash. Commissioning tunes these without a code change. | WATCHDOG §3.3, §8; runtime-and-model-map E31 | NEW |

**Module dependency direction (lower depends on nothing above-but-itself; arrows = "calls"):**
`store, clock, states, config, lifecycle` (leaf/process primitives) ← `ledger, fencing, validate` ← `executor` ←
`detector(+detector_signals), watchdog, reconcile, spawn/*, necro` ← `genesis` ← `daemon` ←
`harnessctl` (out-of-process client). The executor is the single chokepoint every mutator funnels
through; nothing writes the ledger except `executor.commit`.

---

## 2. Inter-module interfaces (concrete signatures)

Types: `Binding` and `WalRecord` are `dict` in v1 (YAML/JSON-native; typed dataclasses are a later
refinement). `TransitionResult`, `SpawnResult`, `Liveness`, `ReconcileReport`, `WatchdogAction` are
small frozen dataclasses.

### 2.1 store.py
```python
def atomic_replace(path: Path, render_fn: Callable[[IO[str]], None]) -> None
    # tmp = path.with_name(f".{path.name}.tmp"); render_fn(tmp_handle); flush; os.fsync(fileno); os.replace(tmp, path)
    # PORT of save_manifest L191-197. The fsync-before-replace is the load-bearing durability line — kept verbatim.

@contextmanager
def file_lock(path: Path, *, shared: bool) -> Iterator[None]
    # fcntl.flock(LOCK_SH if shared else LOCK_EX); LOCK_UN in finally. PORT of control_plane_lock L248-256.
    # In the daemon this is taken ONCE by the daemon process, not per-CLI-call.

def append_framed(path: Path, payload: str) -> None
    # `append_framed` is the SOLE owner of framing — callers hand it the RAW json string, never pre-framed.
    # ONE framed record = ONE write() syscall of `f"{len(payload.encode())}\t{payload}\n"` + flush + one os.fsync.
    #   The `<byte-len>` prefix is computed HERE over the exact json bytes and is the SOLE source of truth
    #   for torn-tail detection (no `len` field inside the json — that would be byte-level circular).
    #   CONTRACT (load-bearing, asserted in tests): the entire framed line is written in a single write()
    #   then fsync'd — NEVER a buffered/partial flush. This is what makes "only the LAST line can be torn"
    #   true by construction; a split non-final record must be impossible, not merely improbable.
    # PORT (fsync discipline of append_ledger L241-246, verified plain-json no-frame) + NEW (the `<len>\t` frame).

def normalize_scalars(obj: Any) -> Any   # PORT L171-180
```

### 2.2 clock.py
```python
def now_utc() -> str                     # tz-aware UTC ISO-8601. Replaces now_iso() L167 (local astimezone bug).
def parse_iso(s: str) -> datetime        # PORT parse_iso_timestamp L265
def age_seconds(then_iso: str, *, now: str | None = None) -> float
```

### 2.3 states.py
```python
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "planned":  {"claimed"},
    "claimed":  {"spawning", "planned"},            # planned = release-rollback (FIRST-CLASS)
    "spawning": {"running", "planned", "failed"},   # planned = rollback; failed = give-up
    "running":  {"blocked", "done", "failed", "claimed"},  # claimed = re-adopt/resume §6.4
    "blocked":  {"running"},
    "dead":     {"claimed"},                         # claimed = re-adopt/necro
    # {any non-terminal} -> dead is reconcile-driven; done|failed|dead are terminal.
}
LIVENESS_STATES = ("working", "waiting", "idle", "dead")  # LOCKED 4-value; working/waiting MUST stay distinct
TERMINAL_VOCAB: dict  # the §3.6 mapping table: agent-tag -> terminal_signal -> event -> resulting state
# Gate collapse fires on state ∈ {done,failed,dead}, NEVER on terminal_signal != null.
# ESCALATED is asymmetric: terminal_signal=ESCALATED but state STAYS running (holds its slot, no in_flight release).
def is_terminal(state: str) -> bool
def is_legal(from_state: str, to_state: str) -> bool
```

### 2.4 ledger.py
```python
def read_binding(node_address: str) -> dict | None          # one node record (Option A keyed map)
def all_nodes() -> dict[str, dict]                           # whole binding-ledger.yaml
def write_binding(candidate_map: dict, *, _lock_held: bool) -> None
    # PRIVATE whole-map atomic-replace (Option A). With a single keyed file, a whole-map replace by
    # anyone but the serialized daemon writer silently clobbers a concurrent write to a DIFFERENT node
    # (per-node generation CAS does NOT catch a cross-node overwrite — DAEMON §4.3). So this is callable
    # ONLY from executor.commit and reconcile.replay, both of which run inside the held EX lock
    # (reconcile's boot-replay checkpoint ACQUIRES it explicitly around its read-replay-write).
    # Structural guard, not convention: ASSERT _lock_held (raise loudly if called without the lock).
    # CLIs are clients, never writers — they route mutations through the daemon, never call this.

def append_wal(record: dict) -> None
    # hands RAW json to the framer: append_framed(path, json.dumps(record)). NEVER pre-frames.
    # record carries crc32 (content integrity, NOT length — so not circular) but NO `len` field:
    #   the `<byte-len>` PREFIX written by append_framed is the only length, and it is load-bearing. (FORK-CRC)

def load_wal() -> list[dict]
    # REWRITE of load_ledger L209-225 (PORT fsync-read shape + NEW framed parse). Read each line, split on
    # the first TAB into (declared_prefix_len, json_payload). Torn-tail detection rule (prefix = authoritative):
    #   a line is TORN iff  int(declared_prefix_len) != len(json_payload.encode())  OR  json.loads(payload) fails
    #                       OR  crc32(payload) != record['crc32']   (catches a silently-SPLIT record whose
    #                                                                 length frame happens to still parse)
    #   torn on the LAST line  => truncate-and-continue (torn append; its binding-replace never landed)
    #   torn on ANY non-final line => FAIL CLOSED (mid-file corruption-halt — proves the single-write contract held)
    # This is the §4.4 CORRECTION: recovered load_ledger RAISES ValueError on any JSONDecodeError (L220) — bricks boot-recovery.

def next_seq() -> int                                        # last WAL seq + 1 on load (crash-safe allocation, see FORK-SEQ)

def build_wal_record(*, node_address, event, from_state, to_state, expected_generation, generation,
                     lease_epoch, owner_token, binding_delta, summary, artifacts, seq) -> dict
    # {ts, seq, node_address, event, actor:'harnessd', crc32, from_state, to_state,
    #  expected_generation, generation (=expected+1), lease_epoch, owner_token, binding_delta, summary, artifacts}
    # NO `len` field in-record: the framed `<byte-len>` PREFIX (written by append_framed over the exact
    #   json bytes) is the sole length authority. crc32 is computed over the json content (post-dump, sans
    #   the prefix) for content integrity. Putting byte-len inside the json it measures is circular.
    # Non-state-changing rows (heartbeats) omit the transition/binding_delta block and are NEVER replayed.
```

### 2.5 fencing.py
```python
def mint_owner_token(node_address: str, subagent_id: str, session_uuid: str, lease_epoch: int) -> str
    # composite 'address:subagent-id:session-uuid:lease_epoch' — self-fencing (comparing tokens compares epochs)
def advance_epoch(binding: dict) -> int                      # old + 1
def check_fence(binding: dict, expected_owner_token: str | None, expected_lease_epoch: int | None) -> bool
    # False => caller journals stale_return_ignored and leaves the live binding UNCHANGED (non-destructive de-auth)
```

### 2.6 executor.py
```python
def transition(node_address: str, *, expected_state: str, expected_generation: int,
               expected_owner_token: str | None, target_state: str, binding_delta: dict,
               new_lease_epoch: int | None = None, new_owner_token: str | None = None,
               event: str, actor: str = "harnessd", summary: str = "",
               artifacts: list[str] | None = None) -> TransitionResult
    # Body (GENERALIZES cmd_transition L1505-1610):
    #   with store.file_lock(EX):                              # the one serialization domain
    #     binding = ledger.read_binding(node_address)
    #     if binding.state      != expected_state:        ABORT  (recovered L1511)
    #     if binding.generation != expected_generation:   ABORT  (recovered L1516 but PER-NODE generation, not global len(ledger))
    #     if expected_owner_token is not None and binding.owner_token != expected_owner_token:
    #            ABORT  -> journal stale_return_ignored WAL row, binding UNCHANGED   (NEW fencing precond)
    #     if not states.is_legal(binding.state, target_state): ABORT  (recovered legality gate L1526)
    #     candidate = deepcopy(binding); apply binding_delta; candidate.generation += 1
    #     if new_lease_epoch/new_owner_token: rotate IN THE SAME candidate (F-012)
    #     entry = ledger.build_wal_record(...)
    #     errors, warnings = validate.validate(candidate, wal_tail + [entry])
    #     if errors: ABORT (validate-before-commit — nothing written)   (recovered L1603)
    #     commit(candidate, entry)
    #   return TransitionResult(ok, errors, warnings, binding)

def claim(node_address: str, *, expected_state: str, expected_generation: int,
          expected_owner_token: str | None, level_config: dict) -> TransitionResult
    # §6.1 STEP-1 slot-claim. transition() variant: target_state='claimed';
    # expected_state ∈ {planned (fresh), running (resume-live §6.4), dead (necro §5)};
    # mints new_owner_token=fencing.mint_owner_token(...), new_lease_epoch=old+1.
    # Carries ④'s in_flight CLAIM-INCREMENT seat. claimed->planned release is itself a CAS-guarded transition.

def commit(candidate_binding: dict, entry: dict) -> None
    # INTENT-FIRST crash-atomicity (§4.4), REVERSING commit_mutation L1264. PRIVATE — called ONLY from
    # transition(), INSIDE its held EX lock (write_binding asserts _lock_held):
    #   candidate_binding['last_applied_seq'] = entry['seq']
    #   (1) ledger.append_wal(entry)        # ONE write() of the framed line + ONE fsync = the intent (no partial flush)
    #   (2) ledger.write_binding(merge_node_into_map(candidate_binding), _lock_held=True) # whole-map tmp+fsync+os.replace
    #   (3) regenerate derived handoff packet
    # Crash between (1) and (2) => WAL-ahead-of-binding, replayable. Crash before (1) => no-op the actor's CAS retries.
    # The single-write-then-fsync append (store.append_framed contract) is what makes "only the FINAL WAL
    # line can ever be torn" true by construction — a split non-final record must be impossible.

def heartbeat(node_address, *, expected_owner_token, ...) -> TransitionResult     # PORT cmd_heartbeat L1423 + owner_token required (§4.5)
def release_lease(node_address, *, expected_owner_token, ...) -> TransitionResult  # PORT cmd_release_lease L1468
def watchdog_checkpoint(node_address, *, condition, liveness_state, last_progress_at, last_evidence,
                        expected_owner_token, gate_crossed_at=None) -> CheckpointResult
    # own-slice liveness write + one run-ledger row, EDGE-TRIGGERED (no append on steady-healthy poll).
    # PORT cmd_watchdog_checkpoint L1314+ incl. stale-counter reset-on-healthy (L1337-1350).
```

### 2.7 validate.py
```python
def validate(candidate_binding: dict, wal_tail: list[dict]) -> tuple[list[str], list[str]]
    # pure; errors-block / warnings-allow. PORT validate() L618 discipline; per-node; NO cross-file workboard checks.

def validate_committed_snapshot(candidate_binding: dict, node_effects: list[dict]) -> tuple[list[str], list[str]]
    # pure READ projection for harnessctl validate. Select exactly last_applied_seq from recovery's
    # shared primary-plus-related binding-effect projection. Real lifecycle state changes and gate
    # events -> validate() unchanged; other binding effects -> exact state/generation relation/
    # identity/delta checkpoint match; journal-only rows cannot anchor; fresh generation-0 planned
    # registration may have no effect. Never fall back to a global/foreign effect.
```

### 2.8 detector.py / detector_signals.py
```python
def liveness(node_address: str) -> Liveness
    # Liveness(state: 'working'|'waiting'|'idle'|'dead', last_progress_at: str | None)
    # v1 floor fuses ONLY jsonl_progress + pane_alive.
    #   grew within W                          -> working
    #   flat beyond W + pane warm + legit reason (terminal_signal==ESCALATED OR coord w/ live-descendant roll-up) -> waiting
    #   flat beyond W + pane warm + NO reason  -> idle   (the only actionable flat case)
    #   pane_dead==1 OR pane gone              -> dead
# detector_signals.py:
def jsonl_progress(node) -> tuple[bool, str | None]   # (grew, mtime_iso) via os.stat(transcript).st_size/st_mtime vs cached
def pane_alive(node) -> tuple[bool, int | None]       # tmux display-message '#{pane_dead} #{pane_pid}'
def pane_pid_cpu(node, pane_pid) -> float | None      # one /bin/ps tree snapshot; blocked-input path only, UNKNOWN on any read failure
def read_terminal_signal(node, binding) -> dict | None
    # Reads /runtime/.../nodes/<addr>/.signal.json (agent-written, atomic tmp+rename). The PRODUCER side
    # of INCLUDE-item #3 (terminal-signal artifacts). FENCED against the live binding's owner_token:
    #   returns {signal: DONE|FAILED|ESCALATED, ts, owner_token, evidence, _signal_artifact_seen_at}
    #   IFF the file's owner_token == binding.owner_token (current epoch). A STALE owner_token -> return None
    #   (ignored, journal stale_return_ignored — a dead incarnation's leftover signal never collapses a
    #   re-spawned node). Absent file -> None. This is the seam that turns a present signal into a
    #   terminal EVENT; the watchdog/reconcile sweep then routes it to chokepoint.collapse.
```

### 2.9 watchdog.py
```python
def check_leaf(node, binding, *, now) -> WatchdogAction
    # STEP A (terminal-signal FIRST — the producer for INCLUDE-item #3): sig = detector_signals.read_terminal_signal(node, binding).
    #   sig present & fenced (owner_token matches): signal ∈ {DONE,FAILED} => COLLAPSE (route to chokepoint.collapse
    #     through the executor); signal == ESCALATED => NOOP after journaling signal_ESCALATED exactly once
    #     per harness-observed artifact identity and appending the child_escalated parent relay.
    #   sig present but STALE owner_token => ignore (journal stale_return_ignored), fall through to liveness.
    # STEP B (no actionable signal): reads liveness(node); idle + age>W => PROD (gated by prod_precondition) up to
    #   stale_grace_checks, else FAILED.
    # CLOSING ACTION on FAILED (INCLUDE-item #5, v1 floor): the leaf is marked running->failed via the executor
    #   (actor='harnessd', reason='watchdog_nonresponse') AND the death is ESCALATED TO THE PARENT (the parent
    #   coordinator agent re-claims at the stable address — WATCHDOG §4 L444/L489: "the parent respawns").
    #   v1 does NOT auto-respawn from harnessd; harnessd-initiated step-8 respawn + recovery_attempts ceiling
    #   is the full lease-recovery state machine, DEFERRED (named in Deferred + §2-item-5 mapping). The
    #   auto_resume_command field exists in the schema for that deferred path; v1 leaves it unread on the leaf leg.
    # PORT ladder shape from derive_checkpoint (stale_count+1 if STALE_FAMILY else 1; escalate when >= grace; L206-211).
def prod_precondition(node) -> bool       # capture-pane shows idle input prompt (golden string per CC version; FORK-PROMPT)
def confirm_prod_worked(node, jsonl_size_before) -> bool   # re-read JSONL; True iff a new turn appeared (send-keys is fire-and-forget)
def unconsumed_inbox_notification(node, binding) -> dict | None
                                          # one complete-row snapshot after last_inbox_acked_offset:
                                          # total, ordered per-sender counts, wake-table verdict, covered offset.
def inbox_has_unacked(node, binding) -> bool   # compatibility boolean projection of that snapshot.
def wake_keystroke(node, notification) -> str # sender/count metadata + seat-qualified inbox pointer, NEVER body content.
def nonresponse_prod_keystroke(node) -> str   # distinct leaf liveness PROD: turn-end contract, never an inbox claim.
def check_coordinator_death(node, binding, ledger) -> WatchdogAction
    # reads run-ledger for a coordinator_died EVENT (not a standing field) OR state=='dead'.
    # dead-pid + live-children => RECOVERABLE ORPHAN -> v1 returns ESCALATE (recover-vs-reap DEFERRED, §5.5).
    # quiet pane-alive + live children => waiting (not dead).
```

### 2.10 reconcile.py
```python
def replay_wal(bindings: dict[str, dict], wal: list[dict]) -> dict[str, dict]
    # for each event with seq > binding.last_applied_seq[node]:
    #   if binding.generation == event.expected_generation:   # the CAS pre-image
    #       apply binding_delta; set state=to_state + node_address (authoritative, mirrors §4.2); set generation+owner_token to post-commit; stamp last_applied_seq=seq
    #   elif binding.generation == event.generation:           # already landed -> NO-OP skip
    # BATCH all pending events for a node into ONE atomic-replace (recovery checkpoint atomicity, see FORK-REPLAY).
    # Writes the whole map via ledger.write_binding(..., _lock_held=True) — replay runs single-threaded inside
    # the daemon's held EX lock during reconcile_on_restart, so the cross-node-clobber hazard cannot occur here.

def reconcile_on_restart(executor, tmux) -> ReconcileReport
    # (1) load_wal (torn-tail-tolerant) + replay_wal
    # (2) tmux.list_targets() -> live targets + pane_pids
    # (3) per binding classify:
    #     recorded-alive & tmux-present & session_uuid-matches  -> ADOPT
    #     recorded-alive & tmux-absent LEAF                      -> necro: mark dead, stamp died_* terminal_signal, bump epoch, append
    #     recorded-alive & tmux-absent COORD                     -> mark dead, stamp coordinator_died, bump epoch, append, ESCALATE
    #     recorded-terminal                                      -> LEAVE (reconcile-EXACTLY-once)
    # (4) tmux-present & NO binding                              -> ESCALATE orphan
    # (5) resume-not-double-spawn L1

def reconcile_tick(executor, tmux, detector) -> ReconcileReport
    # §5.2 continuous: same sweep on the watchdog timer; re-derive liveness via detector.liveness;
    # EDGE-TRIGGERED — only state/condition CHANGES append to the WAL.
```

### 2.11 spawn/* (chokepoint, adapters, tmux, oauth_guard, brief)
```python
# chokepoint.py
def claim_and_spawn(node_address, *, expected_state, expected_generation, expected_owner_token, level_config) -> SpawnResult
    # STEP0: abort if any(b.paused_at for b in ancestors_inclusive(node_address))
    # STEP1: executor.claim(node_address, expected_state='planned', ...) -> if aborted return ClaimLost (NO actor opened; F-024 closed)
    # STEP2: brief.assemble_neutral(...)
    # STEP3: adapter.pin_and_open confirms model+runtime BEFORE the child runs
    # STEP4: tmux open; record session_uuid + actual model_used via executor (single writer)
    # STEP5: executor.transition claimed->spawning->running
    # On any failure STEP2-5: release_claim() (CAS claimed->planned, bump epoch — rollback edge is first-class)

def resume(node_address, *, expected_state, expected_generation, expected_owner_token, delta_inputs, level_config) -> SpawnResult
    # GATE FIREWALL (the SINGLE, authoritative enforcement point for never-resume-across-the-gate — LOCKED,
    # correctness-not-optimization DAEMON §6.4; necro.resume_brief delegates here, never re-raises):
    #   if binding.gate_crossed_at is not None -> REFUSE --resume, fall back to fresh claim_and_spawn w/ delta brief.
    #   The `--resume` argv is CONSTRUCTED ONLY on the else-branch below, so crossing the gate is structurally
    #   impossible — there is no code path that builds a --resume argv with gate_crossed_at != null.
    # else: executor.claim with expected_state ∈ {running, dead} (re-adopt edges), bump epoch + re-mint owner_token (fences prior)
    #       brief.delta_brief(...) -> boot via §6.2 recording NEW session_uuid. Never double-spawns a live address.

def release_claim(node_address, *, expected_owner_token) -> None   # CAS claimed->planned, bump epoch
def collapse(node_address, terminal_signal, *, expected_owner_token, ...) -> None
    # terminal write (done/failed/dead) carrying ④'s in_flight RELEASE-DECREMENT (symmetric to STEP1 claim-increment).
    # Routes through executor terminal transaction; crash-safe via last_applied_seq. ESCALATED is NOT a collapse.

# adapters/base.py
class RuntimeAdapter(ABC):
    @abstractmethod
    def pin_and_open(self, neutral_brief, level_config, tmux_target, env) -> SpawnResult: ...
    # MUST confirm configured model+runtime is pinned BEFORE the child runs (E32).
    # On {auth_expired, model_unavailable, override_rejected, runtime_down}: raise SpawnFailure(class) — NO substitute, NO best-effort.
    # Always records actual model_used (config=intent, model_used=fact).

# adapters/claude_code.py — concrete:
#   verify_binary(version="2.1.152", hash=...); check_credential_health(env); oauth_guard.assert_oauth_only(env, argv, pane_argv, tmux.server_env())
#   argv = [CC, "--system-prompt-file", system_prompt_file]   # CONSTANT operational/shared/system-prompt.md (the ONE shared
#                                                             #   minimal prompt, identical L1–L5); the per-level role NEVER
#                                                             #   goes in argv. NEVER --append-system-prompt/--agents/--bare
#   env  = {CLAUDE_CONFIG_DIR=$HARNESS/.cc-pinned/config, CLAUDE_CODE_OAUTH_TOKEN=<file/FD>,
#           CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1, DISABLE_AUTOUPDATER=1}
#   # the ROLE is NOT in argv — brief.assemble_neutral has already written the load-manifest ("Identity — Load
#   #   These Documents" + frozen acceptance.md) into the node; the agent READS its role docs in place from the
#   #   harness root (read-allow graph, F34). The adapter assembles which manifest/role docs by role_variant.
#   tmux.create_detached(name="harness:"+collapse(address)); deterministic trust; capture pane;
#   record model_used="opus-4.8 / claude-code", role_variant, system_prompt_file, system_prompt_file_hash

# tmux.py
def create_detached(session_name, argv, env) -> str            # pane_id
    # CRITICAL (OAuth-only, blocking): the pane env must be EXACTLY `env` and inherit NOTHING from the
    # tmux SERVER environment. A tmux pane otherwise takes its environment from the long-lived tmux
    # SERVER (a launchd-spawned daemon may carry a stray ANTHROPIC_API_KEY/OPENAI_API_KEY into the
    # server env). Build the pane from-empty so a clean 4-var dict cannot be silently widened:
    #   the pane command is `env -i <K=V for each of the 4 vars> <argv...>` — `env -i` clears the
    #   inherited environment, then re-adds only the 4 isolation vars. (Equivalently `tmux new-session`
    #   with per-var `-e K=V` on tmux>=3.2 AND `update-environment ""`; the `env -i` wrapper is the
    #   transport-agnostic floor we standardize on.) NEVER `tmux new-session <cmd>` with a bare
    #   inherited environment. This is the load-bearing mechanism oauth_guard.assert_oauth_only asserts.
def capture_pane(session_name) -> str
def list_targets() -> dict[str, dict]                          # {tmux_target: {pane_pid, pane_dead, window_activity}}
def kill(session_name) -> None                                 # only via executor-stamping path, never raw for control state
def server_env() -> dict                                       # `tmux show-environment -g` readback — what the SERVER would leak (used by the guard)

# oauth_guard.py
def assert_no_api_key(env: dict, argv: list[str]) -> None
    # RUNTIME-AGNOSTIC negative invariant (ALWAYS enforced, Claude AND Codex):
    #   raises ApiKeyForbidden if 'ANTHROPIC_API_KEY' in env, 'OPENAI_API_KEY' in env, or '--bare' in argv.
def assert_pane_env_isolated(pane_argv: list[str], server_env: dict) -> None
    # The pane-layer guard that closes the tmux-server-leak hole (blocking fix):
    #   raises ApiKeyForbidden unless pane_argv begins with the from-empty isolator (`env -i …`, i.e.
    #   the pane environment is built from-empty, NOT inherited) AND neither ANTHROPIC_API_KEY nor
    #   OPENAI_API_KEY is present in the tmux SERVER environment (tmux.server_env()). Checks the env
    #   the PANE WILL ACTUALLY SEE, not the 4-var dict the adapter assembled.
def assert_oauth_only(env: dict, argv: list[str], pane_argv: list[str], server_env: dict) -> None
    # Composes assert_no_api_key + assert_pane_env_isolated. The single call the Claude adapter makes
    # before create_detached. (The Claude-specific POSITIVE token check is NOT here — see below.)
def check_credential_health(env: dict) -> None                 # raises AuthExpired if CLAUDE_CODE_OAUTH_TOKEN absent/expired — CLAUDE-SPECIFIC positive check; lives in the Claude adapter's path, NOT the shared negative gate

# brief.py
def assemble_neutral(node_address, level_config, work_node) -> NeutralContract
def delta_brief(node_address, prior_incarnation, work_node, changes) -> DeltaBrief
```

### 2.12 genesis.py / daemon.py / necro.py
```python
def run_genesis(executor, tmux, config) -> None
    # acquire .harnessd.lock -> write runtime.json -> oauth_guard.check_credential_health + pinned-binary hash (fail-loud)
    # -> reconcile_on_restart -> if no live non-terminal L1 binding: spawn.claim_and_spawn(L1-root, role_variant='L1', parent=null)
    #    after authoring brief.md and, when config.initial_intake is set, pre-seeding .inbox.<seat>.jsonl
    #    (boots with the shared system_prompt_file + the L1 load-manifest delivered in the brief; role-as-documents)
    #    else RESUME (no double-spawn, F35)

def boot(runtime) -> None                                       # daemon: lock, runtime.json, genesis
def poll_loop(interval_s) -> NoReturn                            # reconcile_tick on a timer; SHAPE from main --watch L406-427

def resume_brief(node_address) -> tuple[ResumeArgs, DeltaBrief]
    # Assembles the delta brief. The gate-firewall is NOT re-checked here — it delegates to the SINGLE
    # enforcement point (chokepoint.resume). The --resume argv is constructed ONLY after that check
    # passes, so a resume across the gate is structurally impossible (not merely guarded twice).
```

### 2.13 Result dataclasses
```python
@dataclass(frozen=True)
class TransitionResult: ok: bool; errors: list[str]; warnings: list[str]; binding: dict | None
@dataclass(frozen=True)
class SpawnResult: ok: bool; session_uuid: str | None; model_used: str; role_variant: str; system_prompt_file: str; system_prompt_file_hash: str; tmux_target: str; failure_class: str | None
@dataclass(frozen=True)
class Liveness: state: str; last_progress_at: str | None
@dataclass(frozen=True)
class ReconcileReport: adopted: list[str]; necroed: list[str]; escalations: list[dict]
class SpawnFailure(Exception): ...   # class ∈ {auth_expired, model_unavailable, override_rejected, runtime_down}; carries configured vs actual
class ApiKeyForbidden(Exception): ...
class AuthExpired(SpawnFailure): ...  # DISTINCT spawn-failure class
```

---

## 3. On-disk data layout

**Tracked under `harnessd/` (code only).** **Gitignored under `/runtime/` (per-build throwaway
trees).** `/runtime/` is already in the repo `.gitignore` — confirmed.

```
harnessd/                                  # TRACKED — all code below
  store.py clock.py states.py config.py
  ledger.py fencing.py validate.py executor.py
  detector.py detector_signals.py watchdog.py reconcile.py necro.py
  genesis.py daemon.py harnessctl.py
  spawn/
    chokepoint.py tmux.py oauth_guard.py brief.py
    # NOTE: spawn imports `harnessd.fencing` DIRECTLY — there is NO second fencing module under spawn/.
    # (An earlier draft listed a `spawn/fencing.py` re-export; dropped to avoid a build-workflow
    #  creating a second real module. mint_owner_token/advance_epoch/check_fence live ONLY in
    #  harnessd/fencing.py.)
    adapters/ base.py claude_code.py codex.py
  lifecycle.py                             # run-scoped launchd/request owner (never ledger state)
  launchd/com.harness.daemon.plist         # TRACKED per-run renderer prototype (not installed)
  tests/ ...                               # TRACKED (see §4)
  IMPLEMENTATION-PLAN.md                   # this file

/runtime/<build-id>/                       # GITIGNORED — one tree per build run
  binding-ledger.yaml      # single keyed map (FORK-STORAGE Option A): address#seat -> binding {...}
  run-ledger.jsonl         # append-only FRAMED WAL: each line "<byte-len>\t<json-payload>\n"
  .harnessd.lock           # the ONE per-mutation EX serialization domain (fcntl, DAEMON §4.3)
  .harnessd.instance.lock  # the lifetime single-instance guard (fcntl LOCK_EX|LOCK_NB, DAEMON §2.3)
  .harnessd.pid            # daemon PID
  runtime.json             # daemon runtime descriptor (build-id, started_at, pid)
  .harnessd/status.json    # lock-FREE status sidecar — the ONE atomicity carve-out (§4.4)
  .harnessd/lifecycle/     # per-run request/rendered plist/logs; removed/booted out at clean idle
  next-seq                 # (or recovered from last WAL seq+1 on load — see FORK-SEQ)
  nodes/<collapsed-address>/
    brief.md               # the neutral/delta brief handed to the actor
    report.md              # the node's output
    .signal.json           # terminal-signal artifact (agent-written, atomic tmp+rename):
                           #   current authoring: {signal: DONE|FAILED, ts, owner_token, evidence}
                           #   READ by detector_signals.read_terminal_signal (fenced on owner_token);
                           #   the reader also accepts legacy ESCALATED artifacts for replay/drain;
                           #   reader annotates harness-owned artifact identity -> terminal event
    .inbox.jsonl           # ③-wake surface: MULTI-writer append-only (TRANSPORTS §2 — NOT the executor;
                           #   the opposite of the single-writer ledger). harnessd TAILS it (inbox_has_unacked)
                           #   to decide when to send a sender/count notification; the AGENT reads it when notified.
```

**Binding record (`binding-ledger.yaml` value), keyed by `address#seat`** (e.g.
`payments/gateway/stripe-client#exec`). Fields grouped by owner-cluster (do not redeclare — bind to
DAEMON §3.2):

- **Identity/topology:** `node_address, parent_address (derivable by prefix-truncation), level,
  session_uuid, tmux_target, transcript_path` (captured at spawn from session_uuid — the detector
  cannot stat the JSONL without it; see the spawn↔detector contract in §4/open-questions).
- **Ownership/fencing (NEW):** `owner, lease_epoch (monotonic int), owner_token
  (address:subagent-id:session-uuid:lease_epoch), generation (PER-NODE CAS counter — NOT global
  len(ledger)), last_applied_seq (WAL replay watermark, written in the same atomic-replace),
  paused_at`.
- **Lifecycle:** `state, state_entered_at, last_binding_update_at`.
- **Liveness (②-written):** `liveness_state, last_progress_at, last_heartbeat_at`.
- **Detector/lease (②-read):** `condition, suspect_since, stale_check_count, stale_grace_checks
  (default 2), recovery_attempts, recovery_attempt_ceiling, gate_crossed_at, auto_resume_command,
  last_evidence, last_inbox_acked_offset (③-wake tail watermark — byte offset into .inbox.jsonl past
  which an appended line counts as unacked and triggers a nudge)`.
- **Deliverable (merged from workboard, §3.4):** `deliverable_state, stop_condition, write_targets,
  evidence_refs, acceptance_ref`.
- **Terminal-signal (NEW):** `terminal_signal (DONE|FAILED|ESCALATED|DIED_INFRA|DIED_METHODOLOGY|
  FENCED), terminal_signal_at, terminal_note, signal_artifact_seen_at` (harness-observed signal
  artifact identity; never keyed on the agent-authored `ts`).
- **Spawn fact (H40):** `model_used, role_variant (the per-seat selector — e.g. L1, L5#exec, L5+#review — that
  the chokepoint resolves to a load-manifest/role-doc bundle), system_prompt_file (CONSTANT
  operational/shared/system-prompt.md — what is passed as --system-prompt-file; identical for every binding,
  a runtime-global rather than a per-row path, but at minimum NO LONGER the per-level role path),
  system_prompt_file_hash (detect drift of the shared prompt)`. The role docs themselves are READ-documents the
  agent loads in place (the brief's load-manifest + read-allow graph, F34) — they are NOT system-prompt content.
- **Review-check/product-probe fact:** `review_check_for, review_check_candidate,
  review_check_axis, review_check_report, gate_id`; for L2+ product probes only,
  `product_probe_role, product_probe_roster, product_probe_roster_sha256,
  product_probe_instance_root, product_probe_instance_manifest,
  product_probe_instance_manifest_sha256`. The two probe bindings point at separate writable
  instances and share only the read-only content-addressed roster.

**WAL record (`run-ledger.jsonl` payload):** `{ts, seq (monotonic global = ordering AND per-node
watermark), node_address, event, actor:'harnessd', crc32, from_state, to_state,
expected_generation (CAS pre-image), generation (=expected+1), lease_epoch, owner_token,
binding_delta, summary, artifacts}`. On-disk frame: `<byte-len>\t<json-payload>\n` — the `<byte-len>`
PREFIX (computed by `append_framed` over the exact json bytes) is the sole length authority and the
load-bearing torn-tail signal; there is NO `len` field inside the json (circular). `crc32` lives
inside the json for content integrity / split-record detection only.

**DO NOT carry over** (recovered, reviewer-loop-specific): `WORKBOARD.yaml`, `CONTINUATION.md`, the
two-lock model (`.control-plane.lock` + `.workboard.lock` collapse to ONE), `KNOWN_STATES` /
`ALLOWED_TRANSITIONS` reviewer vocab, `manifest.yaml` single-doc model.

---

## 4. Test strategy

**Subscription-safety rule (non-negotiable):** the entire deterministic suite runs with ZERO real
model usage. Mock `tmux.list_targets`/`create_detached`, use a fake adapter (dry-run spawn returns a
fake session_uuid), hand-write `.signal.json` and WAL fixtures, and synthesize transcript JSONL by
writing bytes to a temp file. The ONLY test that spends real subscription is **one** in-role L1 boot
(the integration gate), guarded behind a pytest marker (`@pytest.mark.real_boot`) so CI's default run
stays usage-free. This matches H40's "one real boot, capture-via-proxy" discipline.

### 4.1 Unit tests (per module — pure filesystem, no tmux, no usage)

- **executor CAS (highest-risk port, F-024/F-012 core):** `transition()` ABORTS independently on each
  of the three preconditions — wrong `expected_state` (recovered L1511 path), wrong
  `expected_generation` (PER-NODE, prove it is NOT global `len(ledger)`), stale `expected_owner_token`
  (lower lease_epoch). The stale-token abort journals a `stale_return_ignored` WAL row and leaves the
  live binding UNCHANGED (non-destructive de-authorization). An illegal `target_state` is rejected
  BEFORE any write (legality gate). A legal transition commits + appends exactly one WAL record.
- **fencing reject-stale:** mint token/epoch; `advance_epoch`; assert a transition presenting the OLD
  owner_token/lease_epoch is rejected as stale while the new one succeeds. The split-brain/double-kill
  guard: two reaps present the same expected_token — first CAS lands, second aborts (§5.4 interlock).
- **WAL torn-tail recovery (the headline FIX):** (a) clean WAL + truncated final framed line →
  `load_wal()` truncates it, returns the clean prefix; (b) corrupt a NON-final line → `load_wal()`
  FAILS CLOSED; (c) round-trip the `<byte-len>\t<json>` framing; (d) **producer-side interrupted
  append** — write a full frame then truncate a second frame mid-payload (fsync-not-reached); assert
  the clean prefix loads and `next_seq` is correct (proves the WRITE path is torn-tail-safe, not only
  hand-authored fixtures); (e) **silently-split non-final record** whose declared prefix length still
  parses → caught by the crc32 backstop, FAILS CLOSED. Regression-guard comment cites that recovered
  `load_ledger` RAISES `ValueError` on any `JSONDecodeError` (L220) — the test proves the rewrite does
  not brick boot. The prefix-length is the authoritative torn signal; crc32 is the split-record
  backstop; there is no in-payload `len`.
- **Intent-first atomicity:** monkeypatch `write_binding` to raise AFTER `append_wal` (simulate crash
  between commit step-1 and step-2). Then `replay_wal` and assert the event is deterministically
  re-applied (generation pre-image verified, binding_delta applied, last_applied_seq stamped). Assert
  an already-landed event (`binding.generation == event.generation`) is a NO-OP skip. This makes
  "reflected in the binding ledger" the checkable predicate `seq <= last_applied_seq`. **Single-write
  contract:** assert `append_framed` issues exactly ONE `write()` per record (spy/mock the file handle)
  then one `fsync` — the premise that makes "only the final line can be torn" hold by construction.
- **atomic-replace durability:** assert save writes tmp then `os.replace` (no torn binding-ledger.yaml
  visible mid-write).
- **validate-before-commit:** a candidate failing structural validation is NOT written (mirrors
  recovered L1603-1606 errors→return-without-commit).
- **states legality table:** rollback edges (claimed→planned) and re-adopt edges (running→claimed,
  dead→claimed) are legal; the spawn-chokepoint release and every adopt/resume would be wrongly
  rejected without them.
- **detector verdicts (fully mockable):** `working` (JSONL grew), `idle` (flat + warm pane + no
  escalation + no live descendants), `waiting_escalated` (flat + terminal_signal==ESCALATED → waiting
  NOT idle — guards the §2.4 false-idle edge), `waiting_live_descendants` (coordinator, flat + roll-up
  warm → waiting), `dead` (pane_dead==1). **False-idle hazard:** pane stays WARM through a long quiet
  model turn — verdict keys off JSONL-growth NOT pane-warmth.
- **terminal-signal reader (the PRODUCER for INCLUDE-item #3, fully mockable):**
  `signal_done_with_live_token_collapses` (a `.signal.json` DONE whose owner_token == binding.owner_token
  → `read_terminal_signal` returns it → `check_leaf` routes to `chokepoint.collapse`);
  `signal_with_stale_token_ignored` (DONE with a PRIOR-epoch owner_token → returns None, journals
  `stale_return_ignored`, the live binding is UNCHANGED, no collapse — a dead incarnation's leftover
  signal never collapses a re-spawned node); `absent_signal_returns_none`; `escalated_signal_no_collapse`
  (ESCALATED → NOOP, holds its slot). This is the seam that gives the DONE_WHEN "signed-off" clause a
  producer; without it Integration B (Increment 14) could not be assembled from its predecessors.
- **watchdog leaf path (mock detector verdict + journal):** `idle_non_signing_prods` (idle + no
  terminal event → PROD); `prod_gate_blocks_mid_tool_call` (prod_precondition False → no prod);
  `prod_resets_on_activity` (agent-progress transcript row resets stale_check_count to 0);
  `bounded_prods_then_failed` (stale_check_count reaches grace → running→failed);
  `watchdog_failed_marked_harnessd` (the FAILED row carries actor='harnessd' +
  reason='watchdog_nonresponse', distinct from agent-self-emitted FAILED);
  `watchdog_failed_escalates_to_parent` (a FAILED leaf produces a parent-directed escalation — the
  v1 closing action is mark-FAILED + escalate, NOT harnessd auto-respawn);
  `escalated_leaf_never_prodded` (ESCALATED → waiting → no prod, no collapse).
- **③-wake trigger (harnessd side, mockable):** `inbox_line_past_watermark_fires_one_nudge` (append a
  line past `last_inbox_acked_offset` → one sender/count `wake_keystroke`);
  `no_new_line_no_nudge` (re-poll with no append → no nudge) and
  `three_wakes_then_cap` (attempt three emits `wake_cap`; no fourth send). The notification tells
  the agent when to read; the independent second-to-last-act sign-off read covers mid-turn arrivals.
- **coordinator-death probe:** `pane_dead_with_live_children_is_orphan` (→ v1 ESCALATE, not silent
  park); `quiet_pane_alive_with_children_is_waiting`.
- **reconcile classification:** feed synthetic (binding, fake-tmux-listing) pairs; assert each §5.1
  branch — adopt, leaf-necro (died_* + epoch bump), coordinator-died (coordinator_died + ESCALATE),
  recorded-terminal → leave (reconcile-EXACTLY-once: no second WAL row on a re-sweep), orphan
  (present + no binding → escalate).
- **OAuth-only (the HARD INVARIANT — split guard, env-only, no spawn):** `assert_no_api_key` RAISES
  when env has `ANTHROPIC_API_KEY` OR `OPENAI_API_KEY`, or when argv contains `--bare` (runtime-agnostic,
  always on); the Claude `check_credential_health` RAISES when `CLAUDE_CODE_OAUTH_TOKEN` is absent and
  PASSES on the pinned env; the Codex fill preflights canonical ChatGPT/OAuth access-token freshness,
  treats `id_token` as presence-only, and runs workers from isolated child `CODEX_HOME` directories
  with no usable child refresh token. The pane-env-leak case is
  the real-tmux test in Increment 9 (§7). (Full detail in §7.)
- **adapter argv/env assembly (dry-run, no real boot):** `ClaudeCodeAdapter` builds
  `argv = [..., '--system-prompt-file', system_prompt_file]` where `system_prompt_file` is the CONSTANT
  shared path `operational/shared/system-prompt.md` (assert it is the shared prompt, NOT a per-level role
  path, and is IDENTICAL across role_variants L1…L5) and NEVER includes
  `--append-system-prompt/--agents/--agent/--bare`; env is exactly the 4-var isolation set; session
  name == `harness:`+collapse(address). **Role-as-documents:** assert the per-seat role arrives as the
  delivered brief/load-manifest the adapter assembled from `role_variant` (the manifest names the role
  docs to read), NOT as system-prompt content — the argv carries no role text. Mock tmux + mock subprocess;
  assert no real `claude.exe` exec.
- **claim-before-spawn / F-024 (mock):** force `executor.claim` to ABORT (concurrent claim won) and
  assert NO tmux actor opened (`tmux.create_detached` asserted not-called) and `SpawnResult=ClaimLost`.
  Happy path asserts claim succeeds BEFORE `create_detached` (ordering).
- **claim-rollback / fencing on spawn failure:** make `adapter.pin_and_open` raise
  `SpawnFailure(model_unavailable)`; assert `release_claim` issues a CAS claimed→planned with bumped
  epoch (slot reclaimable) and an L1 escalation carrying configured-vs-actual + class.
- **gate-firewall:** set `binding.gate_crossed_at != None`; `chokepoint.resume` REFUSES `--resume` and
  falls back to fresh `claim_and_spawn` with a delta brief; with `gate_crossed_at == None` it takes
  the re-adopt edge (expected_state ∈ {running,dead}, epoch bumped, new session_uuid).
- **spawn-failure classification:** each of {auth_expired, model_unavailable, override_rejected,
  runtime_down} maps to the correct class + an L1-terminating escalation; `auth_expired` is DISTINCT
  from `model_unavailable`.
- **Codex adapter:** dry-run tests pin the GPT-5.6 Sol model flag, native-instructions sentinel,
  boot-prompt identity loading, canonical-auth preflight, isolated per-worker `CODEX_HOME`, no copied
  refresh token, rollout discovery, and spawn-window auth/rate-limit failure classification.

### 4.2 Integration tests (climb toward the kill-9 done_when)

- **Integration A — boot + reconcile** (DONE_WHEN clauses 1-2): start daemon on empty `/runtime`,
  assert L1 spawned in-role with the composed `.identity-prompt.md` (shared prompt first, then L1
  identity docs) AND the broader L1 protocol docs delivered as the brief's load-manifest,
  binding registered (parent_address=null, role_variant='L1').
  Pre-seed a binding-ledger with an owned-but-dead node, restart, assert reconcile marks it
  FAILED/necro and reconstructs liveness_state/condition from the LEDGER (not memory) on the first
  sweep. Uses a fake-tmux + dry-run adapter — no usage.
- **Integration B — spawn → detect → sign-off → collapse** (DONE_WHEN clause 3): drive one node
  through claim → spawning → running → done via the single writer with fencing active.
  `detector.liveness` runs the thin REAL impl against a **dummy long-lived pane** (a tiny local script
  that writes a JSONL and exits — real tmux server, fake CLI as the pane process, NO model call). Drives
  the REAL `ClaudeCodeAdapter` argv/env assembly but mocks `tmux.create_detached` (asserts the
  fully-assembled real argv/env incl. `--system-prompt-file <workspace>/.identity-prompt.md`, the
  isolation env, from-empty isolation, no API key — and that broader protocol docs arrived via the
  delivered brief/load-manifest, not in the argv). Test writes `.signal.json DONE` (live owner_token); assert executor collapses and the WAL
  records the full arc. **Fencing by behavior:** mid-arc, present a STALE owner_token after an epoch
  advance → assert `stale_return_ignored` journaled + live binding UNCHANGED while the current token
  commits (so "fencing active" is an observed rejection, not a presence claim).
- **Integration C — kill-9 recovery** (DONE_WHEN clause 4 + the kill-9 clause, the GATE): `kill -9`
  the daemon mid-run; relaunch; assert `reconcile_on_restart` replays the WAL (including a deliberately
  torn tail) + necros the dead node + resumes L1 from the binding. CONCRETE predicates: exactly one
  non-terminal binding per `address#seat`; exactly one live tmux target per resumed address; the
  resumed L1 carries a NEW session_uuid + BUMPED lease_epoch; `create_detached` call-count for L1 ==
  expected (no double-spawn, F35). State recovered from binding-ledger.yaml + run-ledger.
- **Integration gate — ONE real in-role boot** (`@pytest.mark.real_boot`, the only usage-burning
  test): genesis spawns L1 via the real `ClaudeCodeAdapter` (`--system-prompt-file
  operational/shared/system-prompt.md` — the shared constant — with the L1 role delivered as the brief's
  load-manifest, real `CLAUDE_CODE_OAUTH_TOKEN`); assert in-role boot (the agent reads its L1 role docs
  in place per the manifest), session_uuid recorded, model_used written, and that it reaches a single
  journaled sign-off then stops. Bounds usage to a single subscription-window touch.

**tmux/send-keys/capture-pane** are exercised against a real local tmux server with a **fake CLI** as
the pane process — real tmux mechanics, zero model usage.

---

## 5. Build-increment breakdown (the BUILD-WORKFLOW QUEUE)

Ordered risk-first / dependency-sorted. Each increment is independently buildable AND testable, ships
with its own done-test, and is a future build-workflow target. Earlier increments are the
highest-risk ports (CAS, WAL torn-tail, fencing) so failures surface before anything depends on them.

**Interface-before-implementation note (resolves the apparent ordering inversion):** Increments 6
(detector) and 7 (reconcile) CONSUME the tmux layer (`list_targets`/`pane_dead`/`pane_pid`) but the
concrete `spawn/tmux.py` is not built until Increment 9. This is intentional and safe: 6 and 7 depend
only on the tmux INTERFACE (the §2.11 signatures), which is FROZEN in Increment 0, and both are tested
against a mocked tmux contract (Increment 6 "tmux mocked", Increment 7 "fake tmux listing"). The
concrete `tmux.py` landing in Increment 9 must satisfy that frozen contract; Increment 9's done-test
includes a conformance check that `list_targets` returns the `{pane_pid, pane_dead, window_activity}`
shape 6/7 were written against.

> **Increment 0 — repo skeleton + config seats.** Create `harnessd/` package, `pyproject`/test
> harness, `config.py` (LevelConfig, W(state) placeholder constants, pinned-binary hash). Pull the
> actual L1-L5 lifecycle states + ALLOWED_TRANSITIONS from `comms-protocol/agent-lifecycle` into
> `states.py` (OPEN: the vocab is NOT in the recovered files — see forks). **Done-test:** package
> imports; `states.ALLOWED_TRANSITIONS` round-trips the legality table incl. rollback + re-adopt edges.

> **Increment 1 — store + clock primitives.** `store.atomic_replace`, `store.file_lock`,
> `store.append_framed`, `normalize_scalars`; `clock.now_utc/parse_iso/age_seconds`. **Done-test:**
> atomic-replace durability (tmp→os.replace, no torn file mid-write); clock returns tz-aware UTC.

> **Increment 2 — ledger + WAL torn-tail (the headline FIX).** `read_binding`/`write_binding`
> (private, `_lock_held`-asserted)/`all_nodes`, `append_wal` (hands RAW json to `append_framed`; no
> `len` field; crc32 in-payload), `load_wal` (torn-tail-tolerant; prefix-length authoritative + crc32
> split-record backstop), `build_wal_record`, `next_seq`. **Done-test:** (a) last-line truncation
> truncates-and-continues; (b) non-final corruption FAILS CLOSED; (c) `<byte-len>\t<json>` round-trips;
> (d) **producer-side interrupted append** — write a full frame, then a PARTIAL frame (truncate
> mid-payload to simulate fsync-not-reached), `load_wal` returns the clean prefix and `next_seq` is
> correct; (e) **silently-split non-final record** whose declared prefix happens to still parse is
> caught by crc32 and FAILS CLOSED; (f) `write_binding` without `_lock_held` raises. (Regression-guards
> the recovered L220 brick; proves the NEW framing is torn-tail-safe on the WRITE path, not just on
> hand-authored truncations.)

> **Increment 3 — fencing.** `mint_owner_token`, `advance_epoch`, `check_fence`. **Done-test:** stale
> token/epoch rejected, fresh accepted; token comparison reduces to epoch comparison.

> **Increment 4 — states + validate.** Finalize the legality table + terminal-vocab map; `validate`
> (errors-block/warnings-allow, per-node). **Done-test:** illegal transition rejected before write;
> candidate failing validation is NOT written.

> **Increment 5 — executor (the keystone: CAS + intent-first commit).** `transition` (3-precondition
> CAS + legality + validate-before-commit), `claim`, `commit` (WAL-first then binding,
> last_applied_seq stamped), `heartbeat`/`release_lease`/`watchdog_checkpoint`. **Done-test:** the
> three CAS aborts independently; stale-token abort journals `stale_return_ignored` + leaves binding
> unchanged; intent-first crash (write_binding raises after append) is replayable; already-landed
> event is a no-op skip. **This is the single highest-value increment — everything downstream funnels
> through it.**

> **Increment 6 — detector + signals (thin floor).** `liveness` fusing jsonl_progress + pane_alive
> into working/waiting/idle/dead; `pane_pid_cpu` was originally stubbed and is now filled only for
> WATCHDOG §0.2's signature-gated blocked-input read; `read_terminal_signal` reader stub-wired (full
> test in Increment 11). Depends ONLY on the tmux INTERFACE (§2.11 signatures), frozen in Increment 0;
> concrete `tmux.py` lands in Increment 9 — so Increment 6 is independently testable with tmux mocked.
> **Done-test:** the five verdict cases + the false-idle hazard (warm pane + flat JSONL within W still
> reads working if it grew); **plus `transcript_path`-absent fails loud** — a binding with no
> `transcript_path` makes `jsonl_progress`/`liveness` raise (or return an explicit `unknown`), NOT
> silently return `dead`/`idle` (moves the spawn↔detector contract violation to where it is cheapest to
> catch). tmux mocked, JSONL synthesized.

> **Increment 7 — reconcile (replay + on-restart sweep).** `replay_wal` (generation pre-image,
> batched one-replace-per-node), `reconcile_on_restart` classification. **Done-test:** each §5.1 branch
> (adopt / leaf-necro / coordinator-died-escalate / recorded-terminal-leave-EXACTLY-once / orphan);
> replay re-applies a pending event and skips a landed one. Fake tmux listing.

> **Increment 8 — oauth_guard + the OAuth-only test (do this BEFORE any adapter that could spawn).**
> `assert_no_api_key` (runtime-agnostic negative), `assert_pane_env_isolated`, `assert_oauth_only`
> (composed), `check_credential_health` (Claude-specific positive). **Done-test:** `assert_no_api_key`
> raises on `ANTHROPIC_API_KEY` OR `OPENAI_API_KEY` present / `--bare` in argv; `check_credential_health`
> raises when `CLAUDE_CODE_OAUTH_TOKEN` absent and passes on the pinned env; `auth_expired` is a distinct
> class; the negative invariant is shared (cannot be deleted by the future Codex fill). The real-tmux
> pane-env-leak assertion lands in Increment 9. (Pure env-mock here; the HARD-INVARIANT gate — see §7.)

> **Increment 9 — tmux wrapper + Claude-Code adapter (dry-run-first).** `tmux.create_detached`
> (from-empty `env -i` pane isolation)/`capture_pane`/`list_targets`/`kill`/`server_env`;
> `ClaudeCodeAdapter.pin_and_open` (verify_binary, `oauth_guard.assert_oauth_only(env, argv, pane_argv,
> server_env)`, argv/env assembly, deterministic trust, record model_used/role_variant/system_prompt_file_hash +
> `transcript_path` derivation from session_uuid); Codex fill preflights canonical ChatGPT/OAuth auth
> and opens workers from isolated no-usable-refresh-token homes. **Done-test:** (a) argv = `[..., '--system-prompt-file', system_prompt_file]`
> where `system_prompt_file` is the CONSTANT shared `operational/shared/system-prompt.md` (assert it is the
> shared prompt, identical across role_variants — NOT a per-level role path), never
> `--bare/--append-system-prompt/--agents`; env is exactly the 4-var set; session name ==
> `harness:`+collapse(address); the per-seat role arrives as the delivered brief/load-manifest assembled
> from `role_variant` (role-as-documents), NOT as argv/prompt content; mock subprocess
> asserts no real exec. (b) **tmux
> pane-env leak (real tmux server, NO model call) — the OAuth-only blocking test:** start a tmux server
> WITH `ANTHROPIC_API_KEY` set in the SERVER env, spawn a pane, assert capture-pane `printenv
> ANTHROPIC_API_KEY` is EMPTY (the `env -i` from-empty isolation works); and `assert_pane_env_isolated`
> RAISES when the pane is launched without the from-empty wrapper. (c) **tmux-interface conformance:**
> `list_targets` returns the `{pane_pid, pane_dead, window_activity}` shape Increments 6/7 were written
> against. (d) **MOCK-CONTRACT VALIDATION (the Increment-6 detector mock pinned to reality — register
> Lesson 6):** run the REAL `detector_signals` against the REAL boundary, NOT the Inc-6 mock — `pane_alive`
> on a real living pane returns `(alive=True, <real pid>)`; on a real `kill`'d / closed pane returns
> `(alive=False / pane_dead)`; `jsonl_progress` returns `grew=True` for a real transcript appended-to between
> calls and `grew=False` for a flat one. This validates that real tmux actually emits what the Increment-6
> detector logic was mocked against, so the two cannot silently drift (real tmux server + fake CLI pane, zero
> model burn). Without (d), the Increment-6 verdict logic rests on an unverified assumption about tmux.

> **Increment 10 — spawn chokepoint (claim-before-spawn + rollback + gate firewall) + necro seam.**
> `claim_and_spawn` (STEP0-5), `release_claim`, `resume` (gate firewall + re-adopt), `collapse`,
> `brief.assemble_neutral/delta_brief`, **`necro.resume_brief`** (delta assembly; delegates the
> gate-firewall to the SINGLE point in `chokepoint.resume`, no second raise). **Done-test:** F-024
> (claim aborts → no actor opened → ClaimLost; happy path claims before create_detached);
> claim-rollback on SpawnFailure (CAS claimed→planned, epoch bumped, L1 escalation); gate-firewall
> (gate_crossed_at set → refuse resume → fresh claim w/ delta brief; assert NO code path builds a
> `--resume` argv with gate_crossed_at != null); **STEP4 writes a non-null `transcript_path` into the
> binding** (the spawn↔detector contract producer — pairs with Increment 6's transcript-absent test).
> All mock/dry-run.
>
> **Role-bundle / reference-resolution note (role-as-documents, H40):** `brief.assemble_neutral` writes
> the per-spawn load-manifest ("Identity — Load These Documents") into the node — selected by
> `role_variant` — listing the role docs the agent READS in place (the per-level
> `operational/L{n}/{soul,role,config}.md` + level extras, the always-loaded `operational/shared/*.md`
> contract docs, referenced `design/*.md`), each a path relative to the HARNESS ROOT resolved under
> read-allow/write-jail (F34 / SECURITY §1.4). The role is NEVER flattened into the system prompt. The
> manifest schema + this reference-resolution rule are specified in `../design/ROLE-RESOLUTION.md` (repo-root
> `design/`); this increment consumes that spec — it does not re-derive it.

> **Increment 11 — watchdog (terminal-signal collapse + leaf path + ③ wake + coordinator-death probe).**
> `detector_signals.read_terminal_signal` (fenced .signal.json reader — the PRODUCER for INCLUDE-item
> #3), `check_leaf` (terminal-signal-FIRST → collapse/NOOP, then idle→prod→FAILED ladder, two-counter
> discipline, FAILED→escalate-to-parent), `prod_precondition`, `confirm_prod_worked`,
> `inbox_has_unacked` + `wake_keystroke` (③-wake trigger + nudge payload),
> `check_coordinator_death` (dead-pid detect; ambiguous → ESCALATE; recover-vs-reap DEFERRED).
> **Done-test:** the §4.1 terminal-signal-reader battery (DONE+live-token collapses; DONE+stale-token
> ignored, binding unchanged; ESCALATED→NOOP; absent→None) + watchdog-leaf battery (incl.
> FAILED-escalates-to-parent, NOT auto-respawn) + ③-wake battery (one nudge per new inbox line,
> edge-triggered) + coordinator-death battery. Mock detector + journal.

> **Increment 12 — genesis + daemon + launchd + reconcile_tick.** `run_genesis` (lock → runtime.json →
> preconditions → reconcile_on_restart → optional initial-intake pre-seed → spawn-or-resume L1), `daemon.boot/poll_loop`,
> `reconcile.reconcile_tick` (continuous edge-triggered sweep on the timer), the launchd plist, the
> lock-free status sidecar. **Done-test:** Integration A (boot+reconcile on a fake adapter) — L1
> spawned in-role (dry-run) with the composed L1 `.identity-prompt.md` plus the broader L1
> load-manifest in the brief, configured initial intake present in
> `.inbox.<seat>.jsonl` before the actor opens, owned-but-dead node necro'd, liveness
> reconstructed from ledger; **PLUS
> reconcile_tick edge-trigger:** one mid-run liveness CHANGE produces exactly one WAL row, and N
> steady-healthy polls produce ZERO rows (the continuous path is not shipped untested).
>
> **Lifecycle supersession (2026-07-24):** the increment's always-on/login posture is retired.
> `harnessctl start` now renders/bootstraps the plist per run; a one-shot request distinguishes
> deliberate full genesis from crash recovery; empty/all-terminal after a completed tick exits 0
> and boots the job out. The reconcile/spawn semantics above remain the shared implementation.
>
> **Panel-arm commissioning extension (2026-07-28):** the same start request
> optionally carries ordered L3 module-pattern review rosters. A non-empty
> declaration is stored on the existing L1 root and selected at the existing
> required-review-spec seam; absence preserves the four-axis default. The
> registered `broad` reviewer is unscoped, while L4 and L2+ rosters remain
> unchanged.

> **Increment 13 — harnessctl CLI + daemon IPC.** Node-addressed subcommands (spawn/transition/show/
> reconcile-inspect/kill) over a local socket/FIFO to the daemon; read-only commands may take the
> shared lock directly. **Done-test:** a mutation via harnessctl is performed by the daemon inside the
> one lock (not by the CLI process); a read command returns ledger state.

> **Increment 14 — Integration B (spawn→detect→sign-off→collapse) against a dummy pane.**
> Wires increments 5/6/7/10/11 end-to-end with the thin REAL detector against a fake-CLI pane. Drives
> the REAL `ClaudeCodeAdapter` argv/env assembly path but STOPS at the tmux boundary (mock
> `tmux.create_detached`, assert it is called with the fully-assembled real argv/env incl.
> `--system-prompt-file <workspace>/.identity-prompt.md` + the isolation env + from-empty pane
> isolation, NO API key, AND the broader protocol docs delivered via the brief's load-manifest) —
> so "spawned in-role" is exercised by the real assembly code here, not only at the usage-burning gate.
> **Done-test:** DONE_WHEN clause 3 — one node through the single writer with .signal DONE → collapse →
> full WAL arc; **fencing is asserted by BEHAVIOR, not presence:** mid-arc, advance the node's epoch
> (simulated re-adopt), replay a transition presenting the OLD owner_token → assert the executor
> journals `stale_return_ignored` and leaves the live binding UNCHANGED while the current-epoch token
> still commits. No usage.

> **Increment 15 — Integration C (kill-9 recovery) — THE GATE for state durability.** `kill -9` the
> daemon mid-run (with a deliberately torn WAL tail), relaunch. **Done-test:** DONE_WHEN clause 4, with
> CONCRETE predicates (no vacuous pass): after relaunch assert (a) WAL replayed (torn tail truncated,
> pending event re-applied, landed event skipped); (b) dead node necro'd (died_* + epoch bump); (c) L1
> resumed from binding; (d) **single-owner intact** = exactly ONE non-terminal binding per `address#seat`
> (ledger scan) AND exactly ONE live tmux target per resumed address (`tmux.list_targets`); (e)
> **resumed-not-double-spawned** = the resumed L1 carries a NEW session_uuid with a BUMPED lease_epoch
> (proves resume, not adopt-stale), AND `create_detached` call-count for L1 == expected (no second spawn
> issued for an already-live address, F35).

> **Increment 16 — the ONE real in-role boot (subscription gate, `@pytest.mark.real_boot`).**
> **Done-test:** genesis spawns L1 via the real ClaudeCodeAdapter with a real OAuth token; in-role
> boot confirmed, session_uuid + model_used recorded, single journaled sign-off then stop. This is the
> only test that touches the subscription window; closes the whole Phase-5 done_when.

> **Increment 17 — control-plane promotion / delivery (promote-out-of-`/runtime/`).** The FIRST and ONLY
> sanctioned cross-write-jail action: a harnessd operation, originally GATED on L1's accept signal
> and now (per the Q6 owner-final amendment) gated on L1's complete preliminary fidelity artifact,
> the current immutable owner/delegate CONFIRM, and a deliberate node-bound promotion trigger for a
> project, that copies the finished deliverable OUT of the gitignored `/runtime/proj/{project}/` node TO
> the delivery destination captured at intake in the frozen intent-spec (a filesystem user-path or a git
> remote). Agents CANNOT do this — every agent is write-jailed to its `/runtime/` node subtree and the
> destination is OUTSIDE every jail; only the control plane (harnessd) may cross it, and only on the
> confirmed playback and explicit trigger. The promote step writes `deliverable_state` + `delivery_destination` on the binding via
> the single writer (`executor.deliver` — an own-slice write, no second mutation path; `write_targets` is
> NEVER touched — it stays the in-jail source surface, DAEMON §3.2 distinct fields), so the delivery is
> journaled in the WAL like any other state change. The intake→L2 stages (KICKOFF through FINAL-ACCEPTANCE) ride EXISTING
> increments — the chokepoint spawn (10/14) and L1 writing `client-brief/` WITHIN its own jail (a node
> below the L1-root) — plus the new intent-spec delivery-destination field; only this promotion step is
> NEW harness code. Project teardown/reclaim of the `/runtime/` tree after delivery is a DEFERRED
> follow-on (register D7), NOT part of this increment. Cross-reference `../design/INTAKE-TO-DELIVERY.md`
> (the end-to-end arc) + the deliverable binding block (§3.2, `deliverable_state, write_targets,
> evidence_refs, acceptance_ref`). **Done-test:** on a FAKE accepted project (synthesized
> `/runtime/proj/{project}/` tree + an intent-spec carrying a temp-dir destination + a complete L1
> preliminary fidelity artifact + current owner-confirmed playback + deliberate promotion trigger),
> `promote` lands the deliverable AT the captured destination and the binding shows
> `deliverable_state=delivered` with `delivery_destination` recording the target (a failed promote sets
> `deliverable_state=delivery-failed` + escalates; `write_targets` stays the in-jail source surface) — and the write is
> attributable to harnessd (the single writer + the control-plane copy), NOT to any agent (no agent ever
> writes outside its `/runtime/` jail; assert no jailed-agent write touched the destination). **Reject
> path:** with NO deliberate trigger, incomplete preliminary evidence, or no current CONFIRM,
> `promote` is a no-op — the destination is untouched and
> `/runtime/` is left intact (promotion is gated, never speculative). Git-remote destination variant:
> push lands the deliverable at the captured remote (or is dry-run-asserted to issue exactly that push),
> same gate. No real model usage.

---

> **BUILD STATUS (2026-06-06) — single source of truth: `design/working-notes/BRIDGE-2026-06-06.md`.**
> Phase 5 substrate: **Increments 0–16 BUILT + committed + mutation-verified** (commits `bb00093`…`8a0cfbc`).
> **Increment 17 BUILT but UNCOMMITTED** — it FAILED the verification gate (uncaught ValueError, accept-gate
> not bound to the node, a flaky test, an executor fence change to scrutinize); fix-then-commit per the bridge.
> Suite: 503 passed + 1 skipped (the skip = Inc-16 real-boot, **blocked on an expired pinned OAuth token** —
> see the bridge's credential-refresh thread). **Phase 6 (below) is PLANNED, not built**; Inc 18 (pieces-present)
> is un-blocked; 19–24 need a working real boot (the token). The behavioural contracts in 19–24 are the user's to set.

## Security-jail increment (PREREQUISITE for the Phase-6 real-agent evals) — building 2026-06-06

> **AMENDED 2026-07-24 — strong-form blinders + production request gap closed.** The original
> module/profile existed and was tested, but the real production `LevelConfig` assemblers never
> requested containment; only tests injected the undeclared legacy flag. Production genesis and
> child spawns now always select `blinders_mode` (`observe` default, `enforce` opt-in; no `off`),
> so the write/secret floor wraps every Claude and Codex pane. The read policy is derived after
> launch-surface materialization from the canonical address, current direct neighborhood, exact
> manifest/identity/launch/reference paths, and measured runtime essentials. `observe` reports
> broad reads; `enforce` is deny-by-default. Both adapters share one external render/preflight/
> apply/posture seam. Enforce apply failure refuses; observe degradation is durable.
>
> The SECURITY.md containment (LOCKED Option A: `sandbox-exec`/seatbelt write-jail + secret-read-deny,
> skip-perms-inside-jail) — deferred at design time, built NOW because the Phase-6 evals spawn REAL
> unattended agents with full, auto-approved tools (the jail is the structural blast-radius bound, not
> the agent's judgment). `harnessd/spawn/sandbox.py` renders the §2.3 `.sb` profile (realpath-canonicalized,
> §2.4 load-bearing) + wraps the env-i pane command in `sandbox-exec`; wired into the adapter spawn. Tests
> are the §8.1 regression checklist (REAL sandbox-exec: $HOME-write denied, nested-sandbox/osascript/
> launchctl/crontab/symlink-out blocked, /tmp-vs-/private/tmp canonicalization, secrets denied + role docs
> allowed, real npm/pip cache-redirect) + **gate 1 (the REAL pinned CC boots + auths under the seatbelt)**.
>
> **KEY INSIGHT — auth↔jail interaction (resolves the FORK-ADAPTER-AUTH decision):** the jail's keychain
> mach-deny conflicts with self-auth (the binary needs `securityd` to read its keychain login). So the
> harness should **INJECT the live access token** (read by the control plane from the pinned login's
> keychain credential; the jailed agent uses the env token and needs NO keychain) — which lets the keychain
> mach-deny SHIP, fully closing the keychain in the jail (security-superior, and matches the Inc-9 env-var
> design). Gate 1 confirms this empirically. (Open follow-on: keeping the injected token fresh = the
> deferred daily-login automation, OR the user's periodic re-login.)

## Phase 6 — Behavioural validation (the trace-through)

> Phase 5 (increments 0–17) verifies the substrate is **mechanically correct** (the stage works). Phase 6
> **validates the system's desired behaviour** — that when a real build flows L1-downward, the *design*
> carries it without leaking (a leak = the flow silently routing AROUND a gap: a step skipped, a system it
> leverages not in place, a field defaulted). The agents are the trusted medium; the design is under test.
> Canonical: `design/BEHAVIOURAL-VALIDATION.md`. Two instruments: **(P) pieces-present** — deterministic,
> no agent, FRONT-LOADED (testable now from `brief.py`); **(B) behaviour-per-joint** — real-agent evals,
> rubric-scored, after Inc 16; **(T) full trace** — the commissioning real job. Type-B/T burn the OAuth
> window (user-approved). The **behavioural contracts are the user's to set** — drafts below for review.

> **Increment 18 — Pieces-present harness (P; deterministic, front-loaded — build BEFORE 16/17 if convenient).**
> For each spawn boundary (intake, intent-spec→L2, L2→L3, L3→L4, L4→L5, #review, #test), assemble the
> `brief.assemble_neutral` contract + load-manifest and assert: every manifest doc PATH resolves (present,
> readable under the read-allow graph); every cross-ref inside a manifest doc resolves (no dangling
> pointer); the brief is decision-complete for the receiving level (spec pointer present, frozen
> `acceptance.md` present for executor seats); the `role_variant` selects the CORRECT per-seat manifest
> (L5↔swe-handbook, L3↔planning-template, #review↔reviewer bundle). **Done-test:** a deliberately
> removed/renamed role doc OR a missing brief field makes the harness FAIL (catches "a piece isn't in
> place" deterministically). No model.

> **Increment 19 — L1 intake behavioural eval (B; @pytest.mark.real_boot, real OAuth).** Spawn the real
> grilling-session with a representative request. **Behavioural contract (DRAFT — user to set):** returns
> a contract-complete intent-spec (8 fields, contract-valid, delivery destination captured); an
> UNDER-SPECIFIED request triggers a clarification/escalation (loud), NOT a silently-invented spec (the
> leak test). Rubric-scored, not exact-match. The earliest real-agent joint — isolates intake gaps before
> anything downstream.

> **Increment 20 — L2 concept-design eval (B). Contract (DRAFT):** produces the concept-design artifacts
> (component map + interface contracts + ADRs + per-module specs); had its pieces present; stayed in lane
> (architected — did NOT code or re-open intent); on an injected ambiguity submitted a canonical
> `needs_answer` question and parked rather than papering over it.

> **Increment 21 — L3 area-design eval (B). Contract (DRAFT):** realization-not-redesign; mints requirement
> IDs + trace-blocks; escalates a cross-area dependency rather than absorbing it.

> **Increment 22 — L4/L5/L5+ execution eval (B). Contract (DRAFT):** L4 spawns the tester lateral BEFORE
> L5 + decomposes to executable tasks; L5 is spec-faithful + escalates-don't-decides on an under-spec task;
> L5+ reviews against the frozen rubric and bounces a real defect.

> **Increment 23 — cascade-dynamics eval (B or scripted). Contract (DRAFT):** a report-up reaches the
> parent and is acted on; a quality-gate bounce loops back to L5; an escalation round-trips (up + answer
> down); collapsing a coordinator cascades to its subtree.

> **Increment 24 — the full trace-through (T; commissioning).** The real job the user designed — a
> non-trivial build it is MEANT for, poured in at L1 and traced slowly L1-down, watching the named joints
> for leaks (esp. silent gap-routing). The goal is not the artifact; it is to watch how the water moves and
> find what we forgot. Expects breakage by design.

---

**Minimal ③ wake placement — the two halves, and which is harnessd vs agent:**
- **harnessd side (the send-keys notification + its trigger):**
  `unconsumed_inbox_notification` reads one coherent complete-row snapshot after the receipt;
  `wake_keystroke` renders total plus ordered per-sender counts and the inbox pointer;
  `prod_precondition`/`confirm_prod_worked` gate and verify it. The immediate kickoff and later
  re-prods share the durable attempt account: three delivered wakes maximum, then one `wake_cap`.
- **AGENT side (notification-driven read):** the booted agent reads `.inbox.jsonl` when the
  metadata-bearing wake tells it unread rows exist. It does not poll the full inbox at every turn
  start. The generated role's second-to-last-act sign-off law independently covers mid-turn arrivals.

**④ seat placement:** the ④ admission SEAT (claim-slot pre-step, count present, ceilings DEFERRED) is
the CLAIM-INCREMENT carried by `executor.claim` in **Increment 5** and the RELEASE-DECREMENT carried by
`chokepoint.collapse` in **Increment 10**. Count-present source = ledger scan (FORK-ADMISSION).

---

## 6. Adapt-from-control_plane.py mapping (PORTED / GENERALIZED / NEW)

Treat the recovered code as SUSPECT — rewrite clean, do not import wholesale. Verified against the
files: `load_ledger` (L209-225) DOES raise `ValueError` on any bad line (the torn-tail brick to fix);
`commit_mutation` (L1264) DOES `save_manifest` (binding) FIRST then `append_ledger` (WAL) — the exact
reverse of intent-first.

### PORTED (keep the discipline verbatim or near-verbatim)
- `save_manifest` L191-197 → `store.atomic_replace`. The `flush → os.fsync(fileno) → os.replace`
  sequence is the load-bearing durability line.
- `append_ledger` L241-246 → `store.append_framed`: PORT only the `write → flush → os.fsync` durability
  sequence (verified: recovered writes plain `json.dumps(entry)+"\n"`, NO frame). The `<len>\t<json>`
  frame AND the single-write-then-fsync atomicity contract are NEW (listed under NEW below, not a port).
- `control_plane_lock` L248-256 → `store.file_lock` (fcntl SH/EX context manager).
- `now_iso` L167 / `parse_iso_timestamp` L265 → `clock.*` — but FIX the local-`astimezone` bug to
  tz-aware UTC (F-019).
- `cmd_transition` precondition + legality structure L1505-1534 → `executor.transition` CAS body
  (expected_state check + ALLOWED_TRANSITIONS membership).
- validate-before-commit ordering L1603-1606 (errors → return without commit) → `executor` +
  `validate`.
- `cmd_heartbeat` L1423 / `cmd_release_lease` L1468 / `cmd_watchdog_checkpoint` L1314 + stale-counter
  reset-on-healthy L1337-1350 → `executor.heartbeat/release_lease/watchdog_checkpoint`.
- `watchdog.py` `derive_checkpoint` ladder SHAPE L105-276 (stale_count vs stale_grace_checks,
  edge-triggered checkpoint, escalate-when-`>=`) → `watchdog.check_leaf`.
- `build_parser` L1613-1696 argparse structure + print-JSON/exit-code convention → `harnessctl`.

### GENERALIZED (one-manifest → per-node bindings)
- single `manifest.yaml` + `MANIFEST_PATH`/`LEDGER_PATH` module-globals L19-24 → per-build runtime-root
  args + per-node `binding-ledger.yaml` keyed map (FORK-STORAGE Option A).
- `expected_ledger_entries == len(ledger)` global CAS (L1516) → PER-NODE `generation` counter.
- `validate()` L618-1035 (400+ lines of reviewer-loop schema) → keep ONLY the (errors,warnings)
  discipline; DROP the `KNOWN_STATES`/`WORKBOARD`/contact-policy vocab.
- `KNOWN_STATES`/`ALLOWED_TRANSITIONS` reviewer vocab L26-88 → the generic per-node lifecycle in
  `states.py`.
- `supervise_once` L325-346 + the `subprocess run_json`/`checkpoint_manifest` call pattern → in-process
  per-node `watchdog` calls (the daemon owns the writer; no subprocess hop).
- `main --watch` loop L406-427 + `write_runtime` L69-71 → `daemon.poll_loop` cadence + PID/runtime.json
  (and DROP the recovered standalone watchdog `main()` + its separate fcntl single-instance lock —
  WATCHDOG §3(b)).
- the recovered two-lock model (`.control-plane.lock` + `.workboard.lock`) → ONE exclusive
  serialization domain owned by the live run daemon.

### NEW (no recovered precedent — write from spec)
- **Intent-first WAL ordering** — REVERSE `commit_mutation` L1264 (binding-first) to WAL-append-first
  then binding-replace, with `last_applied_seq` stamped in the same `os.replace`. Flagged as a
  DELIBERATE divergence from recovered code.
- **WAL framing + single-write atomicity** — the `<byte-len>\t<json>\n` frame (write side) and its
  read-side parse are entirely net-new (recovered `append_ledger` writes plain JSON, `load_ledger`
  reads plain JSON). The "one framed record = one write() + one fsync, no partial flush" contract is
  the load-bearing premise that makes "only the last line can be torn" true by construction.
- **WAL torn-tail tolerance** — REWRITE `load_ledger` L209-225 (which RAISES `ValueError` on any bad
  line — L220) to truncate-and-continue on the LAST line / fail-closed on any non-final line, with
  prefix-length as the authoritative torn signal and crc32 catching a silently-split record.
- **Fencing** (`lease_epoch` + `owner_token` + CAS) — recovered had `activity_lease` but no epoch/token
  CAS. Folded into `executor.transition` as the 3rd precondition.
- **tmux liveness layer** (`detector` + `detector_signals`): JSONL-growth + pane_dead/pane_pid. The
  recovered watchdog reads NO tmux/pane/pid signals — its liveness was `derive_lease_health` L531
  (heartbeat self-report), explicitly the thing §1 says NOT to do.
- **reconcile** (on-restart + tick) — recovered code has none; it trusts the manifest.
- **spawn chokepoint + tmux/--system-prompt-file in-role boot + adapters** — recovered
  `launch_recovery` L84-102 is a generic `Popen` recovery launcher, not a tmux/in-role boot.
- **`oauth_guard`** — the OAuth-only enforcer.
- **necro gate-firewall** — never-resume-across-the-gate; recovered `resume_packet` (L1565-1567) is
  just a path-list.
- **genesis / run-scoped daemon / on-demand launchd prototype / status sidecar / .signal.json** —
  no analog.

---

## 7. The OAuth-only invariant, wired through the spawn adapter

**The guard is SPLIT into two layers so it cannot be loosened when the Codex adapter is filled.** A
single universal positive `CLAUDE_CODE_OAUTH_TOKEN`-present gate would (a) wrongly reject every
legitimate Codex spawn or (b) get quietly bypassed for Codex — and a loosened guard is exactly where
an OpenAI API-key path sneaks in later. So:

- **Runtime-AGNOSTIC negative invariant** (`assert_no_api_key`, ALWAYS enforced for Claude AND Codex):
  no `ANTHROPIC_API_KEY`, no `OPENAI_API_KEY`, no `--bare`. The Codex fill CANNOT satisfy the gate by
  deleting a check — this negative invariant is shared and immovable.
- **Per-ADAPTER positive credential assertion** (lives inside each adapter, NOT the shared gate):
  Claude asserts `CLAUDE_CODE_OAUTH_TOKEN` present + unexpired (`check_credential_health`); Codex will
  assert its ChatGPT-subscription token present. A missing positive token is that adapter's failure,
  not a universal one.

**Where it lives (defense in depth — three checkpoints):**

1. **Genesis precondition** — `genesis.run_genesis` calls the active adapter's `check_credential_health`
   FIRST (Claude: `CLAUDE_CODE_OAUTH_TOKEN` present + unexpired). If the only available credential path
   is a raw API key, or the subscription token is absent/expired, genesis FAILS LOUD and does not
   proceed to spawn. `auth_expired` is raised as a DISTINCT class so a token lapse reads as "refresh
   the token", not a fleet-wide model-outage storm.
2. **Adapter boundary (pane-env-aware — the blocking fix)** — `ClaudeCodeAdapter.pin_and_open` calls
   `oauth_guard.assert_oauth_only(env, argv, pane_argv, tmux.server_env())` BEFORE
   `tmux.create_detached`. This verifies the env the PANE WILL ACTUALLY SEE, not the dict the adapter
   assembled: tmux panes inherit from the tmux SERVER environment, so a clean 4-var dict can pass a
   naive check while the pane still inherits a stray key. The guard therefore asserts both (a) the pane
   is launched from-empty (`env -i …`, no server inheritance) and (b) no API key sits in the tmux
   SERVER env. The assembled argv is `[CC, '--system-prompt-file', system_prompt_file]` — where
   `system_prompt_file` is the CONSTANT `operational/shared/system-prompt.md` (the one shared minimal
   prompt, identical L1–L5; the role is delivered separately as documents, not in the argv) — NEVER
   `--bare`, NEVER `--append-system-prompt`/`--agents`. The assembled env is exactly `{CLAUDE_CONFIG_DIR,
   CLAUDE_CODE_OAUTH_TOKEN (file/FD), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1, DISABLE_AUTOUPDATER=1}`.
3. **Codex by construction** — the v1 Codex adapter is a stub that RAISES "adapter port to be
   supplied" rather than silently falling back to an OpenAI API key or a substitute model. Its own
   contract ASSERTS `OPENAI_API_KEY` absent NOW (the negative invariant runs even for the stub), so the
   future fill inherits the closed gate. Codex auth is ChatGPT-subscription only; the no-silent-fallback
   invariant holds until the owed Codex audit lands.

**The enforcer:**
```python
def assert_no_api_key(env: dict, argv: list[str]) -> None:           # runtime-agnostic, always on
    if "ANTHROPIC_API_KEY" in env or "OPENAI_API_KEY" in env:  raise ApiKeyForbidden(...)
    if "--bare" in argv:                                        raise ApiKeyForbidden(...)

def assert_pane_env_isolated(pane_argv, server_env) -> None:         # closes the tmux-server leak
    if not _is_env_i_isolated(pane_argv):                       raise ApiKeyForbidden("pane not from-empty")
    if "ANTHROPIC_API_KEY" in server_env or "OPENAI_API_KEY" in server_env:
                                                                raise ApiKeyForbidden("server env leaks a key")

def assert_oauth_only(env, argv, pane_argv, server_env) -> None:     # the composed adapter call
    assert_no_api_key(env, argv); assert_pane_env_isolated(pane_argv, server_env)

# CLAUDE-SPECIFIC positive check — in the Claude adapter, NOT the shared gate:
def check_credential_health(env: dict) -> None:
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env or _expired(env):   raise AuthExpired(...)
```
The H40 oracle proxy forwards the OAuth token; it is NOT a separate API path — it does not introduce
an API key and is not exempted from `assert_no_api_key`.

**The tests (Increment 8 = pure-env unit; Increment 9 = real-tmux pane-leak; ZERO usage in both):**
- **Increment 8/adapter auth checks:** `assert_no_api_key` RAISES on
  `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`--bare`; the Claude `check_credential_health` RAISES when
  `CLAUDE_CODE_OAUTH_TOKEN` is absent and PASSES on the pinned env; `auth_expired` is a distinct class;
  the Codex fill asserts no `OPENAI_API_KEY`, preflights canonical ChatGPT/OAuth access-token
  freshness, and fails stale Codex credentials before opening a pane.
- **Increment 9 (real tmux server, NO model call):** start a tmux server WITH `ANTHROPIC_API_KEY` set in
  the SERVER environment, spawn a pane via `create_detached`, and assert the pane's
  `printenv ANTHROPIC_API_KEY` (capture-pane readback) is EMPTY — proving the `env -i` from-empty
  isolation actually closes the leak. A second case asserts `assert_pane_env_isolated` RAISES when the
  pane is launched WITHOUT the from-empty wrapper. This is the test that would FAIL on today's
  dict-only guard.
`claim_and_spawn` REFUSES to launch when any guard raises (asserted via mock — `tmux.create_detached`
not-called). The integration gate (Increment 16) confirms the one real boot uses the OAuth token and
no API key.

---

## Deferred (explicitly OUT of the walking skeleton — named so no increment widens scope)

- Full lease-recovery state machine (`stale_suspect → recovery_in_progress → recovery_required` full
  ladder) — v1 ships idle→prod→mark-FAILED-and-ESCALATE-TO-PARENT. **Explicitly narrowed:** the
  closing action on a FAILED leaf is mark-FAILED + escalate-to-parent (the parent coordinator
  re-claims, WATCHDOG §4); harnessd does NOT itself auto-respawn the leaf. The §3.4 step-8
  harnessd-initiated RESPAWN, the `recovery_attempts` ceiling, and reading `auto_resume_command` on
  the leaf leg are part of this deferred ladder. (runtime-decisions §2 item 5 names "respawn"; in this
  architecture that respawn is the PARENT's action, surfaced by the watchdog's escalation, not a
  harnessd code path in v1.)
- Multi-signal fusion in the detector (window_activity, semantic-event probe, growing-but-spinning
  Option B) — v1 = JSONL-growth + pane-alive ONLY, behind the stable interface.
- The broader non-chooser wedge detector remains deferred. `pane_pid_cpu` is
  implemented only for §0.2's no-kill, signature-gated blocked-input path; W2
  kill is uncommissioned.
- recover-vs-reap (adopt/respawn-from-ledger for coordinator death) — v1 detects + ESCALATEs.
- The continuous-reconciliation controller — v1 = on-restart sweep + a simple tick on the watchdog
  timer.
- Admission ceiling numbers — v1 = claim-slot pre-step + count-present only; ceilings deferred.
- Human channels (the full §"human" surface).
- Codex auth supervision — the adapter now has canonical preflight and per-worker homes, but unattended
  refresh, user-facing reconnect, seat-cap admission, and refresh-under-load proof remain open.

---

## Forks — for user review

> **FORK-STORAGE (DAEMON §3.4) — binding-ledger storage.** Single keyed `binding-ledger.yaml`
> (Option A) vs one `.binding.yaml` per node-address (Option B). **Recommendation: A** for v1 — one
> atomic-replace, simplest cross-node atomic commit. The `read_binding`/`write_binding` interface is
> written to survive a later switch to B (finer locks, ledger key == filesystem path).

> **FORK-SEAT (DAEMON §3.1) — seat storage.** Per-seat ledger row (flat per-key single-owner,
> independent lease_epoch/owner_token per `#exec`/`#review`) vs one node row with nested seats.
> **Recommendation: per-seat row** — keys `read_binding` by `address#seat`. Matches the runtime-decisions
> §7 per-seat decision.

> **FORK-CRC (DAEMON §4.4) — record framing.** The `<byte-len>` PREFIX (written by `append_framed`,
> NOT a field inside the json — a self-referential in-payload `len` is byte-level circular and is
> removed) is the primary load-bearing torn-tail signal. **Recommendation: always emit crc32** inside
> `build_wal_record` AND make it load-bearing for the ONE case the length-prefix alone can miss: a
> silently-SPLIT record whose declared prefix happens to still equal the surviving byte count. So the
> rule is: prefix-length = primary torn signal; crc32 = the split-record backstop (not merely optional
> belt-and-suspenders). No `len` field in the record.
> **RESOLVED 2026-06-05 (user-delegated → recommendation adopted):** crc32 backstop. `build_wal_record`
> always emits `crc32(payload)`; `load_wal` treats `crc32(payload) != record['crc32']` as a torn signal
> alongside the length-prefix mismatch. Built in Increment 2.

> **FORK-TERMINAL-MIRROR (DAEMON §10) — terminal-signal mirroring.** Mirror terminal-signal WAL rows
> into the project `log.md` too, or keep them strictly in the run-ledger? **Recommendation: strictly
> in the run-ledger** — the executor does NOT write a second artifact on terminal transitions
> (simplest, single source of truth).

> **FORK-REPLAY (DAEMON §3.5/§10) — replay atomicity.** Confirm `replay_wal` BATCHES all pending events
> for a node into ONE binding atomic-replace (not one replace per event). **Recommendation: batch**
> — one recovery checkpoint per node (performance + atomicity).

> **FORK-SEQ (DAEMON §3.5/§10) — seq allocation.** The global monotonic `seq` doubles as ordering AND
> per-node watermark; its allocation must be crash-safe. **Recommendation:** `next_seq() = last WAL
> seq + 1 on load` (derive from the WAL itself; no separate persisted counter to desync). The
> `/runtime/.../next-seq` file is optional cache, not the source of truth.
> **RESOLVED 2026-06-05 (user-delegated → recommendation adopted):** derive from WAL. `next_seq()` =
> last WAL `seq` + 1 computed on load; the WAL is the single source of truth; no authoritative
> persisted counter. Built in Increment 2.

> **FORK-W (WATCHDOG §8) — suspicion-window numbers.** `W(state)` numbers are KNOWN-OPEN. **v1 ships
> placeholder constants** (e.g. `W_working=120s`, `W_waiting_on_child=600s`, `W_writing_final=60s`)
> in `config.py` (a config seat, NOT hardcoded inline) with a TODO — commissioning tunes without a
> code change.

> **FORK-PROMPT (WATCHDOG §4.3) — prod-gate prompt string.** The idle-input prompt string is
> Claude-Code-TUI-version-specific. **Recommendation:** a captured golden string per CC version in the
> patch registry (`runtime/patches`), NOT hardcoded; Codex prompt-string is a separate value. ASSUMED-not-
> verified until first run.

> **FORK-ADMISSION (runtime-decisions §2 ④) — count-present source.** Ledger scan vs tmux scan.
> **Recommendation: ledger scan** — the ledger is the authoritative present-count; tmux is the
> liveness signal, not the admission count.

> **OPEN (not a fork, a hard dependency) — node-state vocab.** The actual L1-L5 lifecycle states +
> ALLOWED_TRANSITIONS must be pulled from `comms-protocol/agent-lifecycle` (referenced in
> runtime-decisions §2 item 3 but NOT in the three recovered files). `executor.transition`'s
> allowed-target table cannot be finalized without it. Resolve in Increment 0.

> **OPEN — spawn↔detector transcript-path contract.** The detector needs `NodeRef → transcript file
> path` (`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`). The session_uuid is the spawn
> output, so the spawn chokepoint MUST write `transcript_path` into the node binding at STEP4 or the
> detector cannot stat the JSONL. Pin this in Increment 9/10; the detector (Increment 6) reads it.

> **OPEN — owner_token subagent-id source.** The composite embeds `subagent-id`. Where the spawn path
> obtains it at STEP1 claim time (vs only post-boot) must be pinned so the token can be minted at
> claim. Resolve in Increment 5/10.

> **OPEN — deterministic first-boot trust + binary hash-pinning.** The non-interactive trust mechanism
> (pre-seed `CLAUDE_CONFIG_DIR` trust state vs non-interactive permission mode) must NOT be a send-keys
> race against the trust dialog; and `DISABLE_AUTOUPDATER` was NOT found in the binary strings
> (PINNED-CC) so the chokepoint must verify npm-version + isolated-prefix + a hash at every spawn.
> Concrete recipes owed; land in Increment 9.
