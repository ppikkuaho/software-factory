"""genesis — the first-boot sequence that establishes the L1 root custody (IMPLEMENTATION-PLAN §2.12).

THE single load-bearing property: ``run_genesis`` drives the DAEMON §7 LOCKED genesis sequence in
order — (1) briefly take the EX serialization lock (the §4.3 per-mutation domain; the LIFETIME
single-instance guard is the daemon's separate ``.harnessd.instance.lock``, acquired in
daemon.boot BEFORE genesis runs — §2.3); (2) write runtime.json; (3) the PRECONDITION CHECK
(OAuth credential health + pinned-binary, FAIL-LOUD, do NOT spawn on a bad precondition); (4)
reconcile-on-restart (replay the WAL, classify every binding against live tmux, necro the
owned-but-dead — liveness reconstructed from the LEDGER, not memory); (5) route the L1 root by its
POST-reconcile state (review CFW-02 — never a binary spawn-or-resume): ADOPTED -> done (already
live, F35); ``running`` -> RESUME through necro.resume_brief with the result ROUTED (GenesisError on
a failed resume — never a silent clean boot); a ``planned`` survivor -> claim_and_spawn the SURVIVING
slot (never --resume the registration placeholder, never an epoch-resetting re-register);
``claimed``/``spawning``/``blocked`` -> REAP to terminal (DIED_INFRA via reconcile's single
terminal-necro write) then SPAWN fresh through the ONE spawn chokepoint (role_variant='L1',
parent_address=null, the shared prompt base + the L1 load-manifest in the neutral brief; the adapter
composes the runtime `.identity-prompt.md`). No leg
double-spawns (F35) and no leg discards a result (fail-loud).

genesis is NOT a writer and NOT a spawn path of its own: every binding mutation routes through the
REAL executor (the single writer), the spawn rides the REAL chokepoint.claim_and_spawn (the ONE
spawn path, F-024 claim-before-spawn), and the reconcile rides the REAL reconcile_on_restart. The
ONLY thing genesis owns is the SEQUENCE (lock -> runtime.json -> precondition -> reconcile ->
spawn-or-resume) and the L1-root REGISTRATION (the parentless root planned slot the chokepoint claims).

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.12 (``run_genesis(executor, tmux, config) -> None``), §3 module table
    (genesis.py row, L63): "acquire .harnessd.lock -> write runtime.json -> preconditions() (OAuth
    health + pinned-binary hash, fail-loud) -> reconcile_on_restart -> if no live non-terminal L1
    binding: spawn.claim_and_spawn(L1-root, role_variant='L1', parent=null) ... else RESUME (no
    double-spawn, F35)".
  - DAEMON §7 (the LOCKED genesis sequence, L1058-1087), §5.1 (reconcile-on-restart), §6.1
    (claim-before-spawn), §6.3 (the auth_expired precondition class).

BUILDER DECISIONS (the §2.12 details the frozen tests leave open — stated in the build report):

  * THE GENESIS ``config`` SHAPE — §2.12 names only ``config``; the test threads a permissive
    SimpleNamespace carrying ``env`` (the OAuth env for the credential precondition), ``l1_address``
    (the L1 root address — the test owns the identity, genesis owns the sequence), ``l1_level`` /
    ``level_config`` (the L1 LevelConfig), ``runtime_root``, ``build_id``, and ``pinned_binary``.
    genesis reads these defensively (``getattr`` with sane fallbacks) so a sparse config still drives
    a well-formed boot. FORK-GENESIS-CONFIG: the precise carrier is the caller's; the load-bearing
    inputs (env for the precondition, the L1 address + level) are pinned by the tests.

  * runtime.json SHAPE — the §2.3 daemon runtime descriptor: ``{build_id, started_at, pid}`` (+
    ``runtime_root`` for legibility). Written via the durable ``store.atomic_replace`` (tmp + fsync +
    os.replace) so a concurrent reader never sees a torn descriptor. It is written INSIDE the held EX
    lock (§7 step 3 orders it after the lock), but it is a daemon-runtime descriptor, NOT control
    state, so it does NOT route through the executor / append a WAL row. Path: ``<runtime_root>/
    runtime.json`` (the §3 on-disk tree).

  * THE L1-ROOT REGISTRATION — the chokepoint claims an EXISTING planned slot (executor.claim reads
    the live binding). On first boot there is no L1 binding, so genesis REGISTERS the parentless root
    planned slot first (state='planned', generation=0, lease_epoch=1, parent_address=null,
    level/role_variant=L1, a minted owner_token), then hands it to claim_and_spawn. The registration
    is the ONE direct ``ledger.write_binding(_lock_held=True)`` genesis does (it runs inside the held
    EX lock, the §2.10-sanctioned lock-held seeding path the suite uses) — the chokepoint then drives
    planned->claimed->spawning->running through the REAL executor. FORK-L1-REGISTER: an alternative is
    a dedicated executor.register primitive; v1 reuses the lock-held write_binding seed the suite
    already validates, keeping the registration a single faithful write.

  * THE LIVE-L1 CHECK (§7 step 6 spawn-vs-resume) — "no live, non-terminal L1 binding" means: the L1
    binding is absent, OR it is terminal (done/failed/dead). reconcile_on_restart has already
    classified it (adopt a live pane, necro a dead one) BEFORE this check, so genesis reads the
    POST-reconcile binding and routes by its STATE (review CFW-02): only ``running`` is resumable
    (the legality table admits ->claimed only from {planned, running, dead}); a ``planned`` survivor
    is claimed fresh as-is; an intermediate ``claimed``/``spawning``/``blocked`` is reaped to
    terminal and respawned. An absent or terminal binding -> SPAWN. By reading the post-reconcile
    ledger (not memory) the no-double-spawn invariant holds against the durable truth.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Optional

from harnessd import addressing
from harnessd import config as _config_mod
from harnessd import executor as _executor_mod
from harnessd import fencing, ledger, states, store
from harnessd import reconcile as _reconcile_mod
from harnessd.spawn import chokepoint
from harnessd.spawn import oauth_guard


class GenesisError(RuntimeError):
    """First-boot failed fatally. L1 is the ROOT — there is no parent to escalate to (DAEMON §7), so a
    failed L1 spawn cannot be a silent clean boot: it raises here, naming the failure_class, so the
    operator (and the launchd-hosted process) sees a loud, debuggable boot failure (review genesis-1)."""


# ---------------------------------------------------------------------------
# Step 1 — the EX serialization lock (the single serialization domain, §4.3 / §7 step 3).
# The lock file co-locates with the WAL/binding under RUNTIME_ROOT (executor.LOCK_FILENAME).
# ---------------------------------------------------------------------------

_LOCK_FILENAME = ".harnessd.lock"

# The PERSISTENT single-instance lock (DAEMON §2.3) — DISTINCT from `.harnessd.lock` (the §4.3
# per-mutation serialization domain) on purpose: flock conflicts across fds even within ONE
# process, so a lifetime hold of the mutation-lock file would deadlock every executor mutation.
# The separate file dissolves the DAEMON §2.3-vs-§4.3 one-file conflict (review SWCAS-02; fork
# decided 2026-06-10: separate instance-lock file). Acquired + held by daemon.boot, NOT here —
# genesis-standalone (the test-harness mode) runs without the lifetime guard.
INSTANCE_LOCK_FILENAME = ".harnessd.instance.lock"


def _lock_path(runtime_root: Path) -> Path:
    """The EX serialization-domain lock path (``<runtime_root>/.harnessd.lock``, §4.3)."""
    return Path(runtime_root) / _LOCK_FILENAME


def instance_lock_path(runtime_root) -> Path:
    """The §2.3 lifetime single-instance lock path (``<runtime_root>/.harnessd.instance.lock``)."""
    return Path(runtime_root) / INSTANCE_LOCK_FILENAME


def _runtime_root(cfg) -> Path:
    """Resolve the runtime root from the config, falling back to the bound ledger.RUNTIME_ROOT."""
    root = getattr(cfg, "runtime_root", None)
    if root is not None:
        return Path(root)
    if ledger.RUNTIME_ROOT is not None:
        return Path(ledger.RUNTIME_ROOT)
    raise RuntimeError(
        "genesis runtime_root is not configured: pass config.runtime_root or bind ledger.RUNTIME_ROOT"
    )


# ---------------------------------------------------------------------------
# Step 2 — runtime.json (the §2.3 daemon runtime descriptor: build-id / started_at / pid).
# ---------------------------------------------------------------------------

def write_runtime_json(
    runtime_root: Path, *, build_id: Optional[str], pid: Optional[int] = None,
    lock_path: Optional[str] = None,
) -> Path:
    """Write the §2.3 daemon runtime descriptor (``runtime.json``) durably (tmp + fsync + replace).

    Carries ``build_id`` / ``started_at`` (the single canonical UTC clock, §4.6) / ``pid`` /
    ``lock_path`` (the §2.3 self-report field — it names the INSTANCE lock; both production
    callers, ``daemon.boot`` and run_genesis STEP 2, pass ``instance_lock_path(runtime_root)``) /
    the ``runtime_root`` for legibility. A daemon-runtime descriptor, NOT control state — it does
    NOT route through the executor and appends no WAL row (only durable bindings + the WAL are
    control truth). Returns the written path.

    NOTE: this is a WHOLESALE rewrite — on a daemon relaunch it DROPS the previous
    ``last_tick_at`` until the first poll tick re-stamps it (``daemon.stamp_last_tick``, §2.6);
    a future §2.6 hang detector must read "last_tick_at missing but started_at fresh" as
    just-booted, not wedged.
    """
    import os

    from harnessd import clock

    runtime_root = Path(runtime_root)
    path = runtime_root / "runtime.json"
    descriptor = {
        "build_id": build_id,
        "started_at": clock.now_utc(),
        "pid": pid if pid is not None else os.getpid(),
        "lock_path": lock_path,
        "runtime_root": str(runtime_root),
    }

    import json

    def render(handle):
        json.dump(descriptor, handle, ensure_ascii=True, sort_keys=True, indent=2)
        handle.write("\n")

    store.atomic_replace(path, render)
    return path


# ---------------------------------------------------------------------------
# Step 4 — preconditions (FAIL-LOUD, do NOT spawn on a bad precondition; DAEMON §7 step 4).
# ---------------------------------------------------------------------------

def preconditions(cfg) -> None:
    """The §7 step-4 precondition gate (FAIL-LOUD — raises BEFORE any spawn on a bad precondition).

    (1) OAuth credential health via the REAL ``oauth_guard.check_credential_health`` — an absent /
        expired ``CLAUDE_CODE_OAUTH_TOKEN`` raises the DISTINCT ``AuthExpired`` (a SpawnFailure, NOT
        an ApiKeyForbidden), so a token lapse reads as "refresh the token", not a model outage
        (DAEMON §6.3). genesis does NOT home-grow a string check — it routes through the positive
        oauth_guard check, the single credential authority.
    (2) Pinned-binary present + version (§6.2). The pinned hash is a v1 placeholder (config.PINNED_
        BINARY_HASH is None until commissioning captures it), so the hash check is a no-op when the
        hash is unset; the VERSION seat must be present (a missing version is a misconfiguration).

    Raises (AuthExpired / a precondition error) BEFORE returning — the caller (run_genesis) never
    reaches the spawn on a raised precondition, so no L1 actor opens on a bad precondition.
    """
    env = getattr(cfg, "env", None) or {}
    # (1) Credential health — the REAL positive check (raises AuthExpired on an absent token).
    oauth_guard.check_credential_health(env)

    # (2) Pinned-binary present + version verified (§6.2). The hash is verified only when captured.
    pinned = getattr(cfg, "pinned_binary", None) or _config_mod.PINNED_BINARY
    version = getattr(pinned, "version", None)
    if not version:
        raise RuntimeError(
            "pinned-binary precondition failed: no pinned version configured (§6.2) — "
            "do not spawn on a bad precondition (DAEMON §7 step 4)"
        )
    return None


# ---------------------------------------------------------------------------
# Step 6 — the L1-root registration (the parentless planned slot the chokepoint claims).
# ---------------------------------------------------------------------------

def _register_l1_root(
    l1_address: str,
    level: str,
    role_variant: str,
    runtime_root: Path,
    *,
    fidelity_playback_authority: str = "owner",
    fidelity_playback_delegate: Optional[str] = None,
    fidelity_playback_delegation_reason: Optional[str] = None,
    fidelity_playback_authority_build_id: Optional[str] = None,
    review_panel_arms: Optional[list[dict]] = None,
) -> dict:
    """Register the parentless L1 root in the binding ledger as a fresh ``planned`` slot.

    The chokepoint claims an EXISTING planned slot (executor.claim CAS-reads the live binding); on
    first boot there is no L1 binding, so genesis seeds the parentless root here first. parent_address
    is null (the L1 root is the ONLY node with no parent — DAEMON §7), generation=0 / lease_epoch=1 /
    a minted owner_token (the same fresh-planned shape the chokepoint tests seed).

    The whole-map write takes the EX serialization lock HERE (a brief, self-contained acquire) so
    ``_lock_held=True`` is TRUE-by-fact, not just by flag — genesis's brief STEP 1+2 acquire of the
    same ``.harnessd.lock`` was released before STEP 5, so this seeding write must re-acquire it.
    (The LIFETIME single-instance guard is the daemon's separate ``.harnessd.instance.lock`` —
    §2.3 — which never contends with this per-mutation domain.) The lock is released when this
    function returns, BEFORE ``chokepoint.claim_and_spawn`` runs (which re-takes it per-mutation via
    ``executor.claim``) — so there is no re-entrant fcntl deadlock. Returns the registered binding
    (its owner_token is the claim's CAS precondition).
    """
    subagent_id = "subagent-l1-root"
    session_uuid = "genesis-l1-root"
    # SM-1/SM-2/INT-4(b): on a RE-register (a terminal/reaped prior root — e.g. a daemon reboot
    # after a terminal childless L1, or the STEP-5 intermediate-state reap above) the fence and
    # the replay watermark must NEVER regress: lease_epoch seeds at prior+1 (the old reset-to-1
    # made the next claim re-mint the PRIOR incarnation's byte-identical owner_token — the fixed
    # placeholder identity is deterministic — so a leftover .signal passed the F19 fence and
    # collapsed every fresh root, and F21's reap-leg epoch bump was immediately undone) and
    # last_applied_seq seeds at the current max WAL seq (boot replay must not re-apply the dead
    # incarnation's chain, SM-2). A true first boot seeds (1, 0) — unchanged.
    previous = ledger.read_binding(l1_address)
    if chokepoint.respawn_parked(previous):
        return previous
    lease_epoch, last_applied_seq = chokepoint.reregister_identity_seed(l1_address)
    generation = 0
    owner_token = fencing.mint_owner_token(l1_address, subagent_id, session_uuid, lease_epoch)
    # SM-1 belt-and-braces: drop the dead incarnation's seat artifacts before the slot reopens.
    chokepoint.purge_stale_seat_artifacts(l1_address)
    binding = {
        "node_address": l1_address,
        "parent_address": None,  # the L1 root is the ONLY parentless node (DAEMON §7)
        "level": level,
        "subagent_id": subagent_id,
        "session_uuid": session_uuid,
        # The PRE-SPAWN placeholder: the canonical session name (F18 — tmux-rename-safe).
        # The chokepoint's STEP4 overwrites it with the live '<session>:<window>.<pane>' triple.
        "tmux_target": addressing.session_name_for(l1_address),
        "state": "planned",
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": owner_token,
        "last_applied_seq": last_applied_seq,
        "liveness_state": "claimed",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        # The L1 root's workspace = its canonical nested node dir (mirror _register_child). Without it
        # service_outbox short-circuits to [] and L1 can never spawn L2 — the cascade dies at edge 1
        # (REMEDIATION F7 / review CFW-01).
        "workspace": str(addressing.node_dir(l1_address, runtime_root)),
        "fidelity_playback_authority": fidelity_playback_authority,
        "fidelity_playback_delegate": fidelity_playback_delegate,
        "fidelity_playback_delegation_reason": fidelity_playback_delegation_reason,
        "fidelity_playback_authority_build_id": fidelity_playback_authority_build_id,
    }
    if review_panel_arms:
        binding["review_panel_arms"] = copy.deepcopy(review_panel_arms)
    if previous is not None and isinstance(previous.get("messages"), dict):
        binding["messages"] = copy.deepcopy(previous["messages"])
        if previous.get("message_last_id"):
            binding["message_last_id"] = previous["message_last_id"]
    chokepoint.inherit_respawn_accounting(binding, previous)
    # Merge into the live map (preserve any pre-existing siblings) and write under a BRIEFLY-held EX
    # lock — released on exit, before claim_and_spawn re-takes it per-mutation (no re-entrant deadlock).
    with store.file_lock(_lock_path(runtime_root), shared=False):
        live_map = dict(ledger.all_nodes())
        live_map[l1_address] = binding
        ledger.write_binding(live_map, _lock_held=True)
    return binding


def _ensure_l1_brief(l1_address: str, level_config, registered: dict, runtime_root: Path) -> None:
    """Author the L1 root's ``brief.md`` at genesis spawn time (INT-5).

    Every spawn's STEP6 kickoff tells the agent to 'Read brief.md in your workspace' — but only the
    parent-spawns-child path (``chokepoint._write_child_brief``) ever authored one. The parentless
    L1 root booted INSTRUCTION-LESS: its only delivered pointer named a missing file, and the F19
    Sign-off manifest block never existed at the cascade root. genesis now reuses the SAME brief
    writer (manifest header — who you are, the load-manifest read-in-place identity — plus the
    Sign-off block naming the .sign-off.<seat>.json handshake) with a v1-HONEST task: the L1 root
    has no parent to brief it, so the task is an INTAKE POINTER — await/read the inbox, never
    invent work. A pre-authored ``brief.md`` (a prior boot's, or an operator's) is left intact —
    the same derivation default the child path honors.
    """
    brief_path = addressing.node_dir(l1_address, runtime_root) / "brief.md"
    if brief_path.exists():
        return  # pre-authored (prior boot / operator) — never clobbered
    seat = addressing.split_address(l1_address)[1]
    task = (
        "You are the L1 root (the parentless System Orchestrator). The daemon's genesis spawned "
        "you directly — no parent authored a task into this brief; in v1 the HUMAN INTAKE is "
        "your task source.\n\n"
        f"AWAIT YOUR INTAKE: read .inbox.{seat}.jsonl in this workspace now. If it carries no "
        "intake/task message yet, WAIT for the next inbox nudge rather than inventing work. "
        "Read your identity documents (listed above) in place before acting, and sign off per "
        "the Sign-off section only when assigned work is actually complete."
    )
    chokepoint._write_child_brief(l1_address, level_config, registered, task)


def _initial_intake_line_id(initial_intake: str) -> str:
    digest = hashlib.sha256(initial_intake.encode("utf-8")).hexdigest()
    return f"genesis-initial-intake:{digest}"


def _seed_l1_initial_intake(l1_address: str, runtime_root: Path, initial_intake) -> None:
    """Append the optional LR-24 initial intake before the L1 actor opens.

    This is deliberately an inbox artifact, not ledger control state: it is the same durable
    wake surface the L1 brief already tells the root to read. Idempotency is keyed to the
    normalized payload hash so a launch retry does not duplicate the same human intake line.
    """
    if initial_intake is None:
        return
    message = str(initial_intake).strip()
    if not message:
        return

    from harnessd import clock

    line_id = _initial_intake_line_id(message)
    inbox = addressing.inbox_path(l1_address, runtime_root)
    try:
        if inbox.exists():
            for raw in inbox.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                try:
                    if json.loads(raw).get("id") == line_id:
                        return
                except ValueError:
                    continue
        inbox.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "from": "operator",
            "type": "intake",
            "id": line_id,
            "message": message,
            "ts": clock.now_utc(),
        }
        with inbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        raise GenesisError(
            f"failed to seed L1 initial intake before spawn (address={l1_address!r}, inbox={inbox})"
        )


# ---------------------------------------------------------------------------
# run_genesis — the §2.12 frozen entry. The DAEMON §7 LOCKED sequence, in order.
# ---------------------------------------------------------------------------

def run_genesis(executor, tmux, config) -> None:
    """The first-boot sequence (§2.12 / DAEMON §7 LOCKED) — establishes the L1 root custody.

    Sequence (in order):
      1. briefly take ``.harnessd.lock`` EX (the §4.3 per-mutation serialization domain) around
         steps 2–3 — NOT the single-instance guard: the lifetime instance lock is the daemon's
         separate ``.harnessd.instance.lock``, already held by daemon.boot before genesis runs
         (§2.3);
      2. write ``runtime.json`` (the §2.3 daemon runtime descriptor);
      3. PRECONDITIONS — OAuth credential health + pinned-binary (FAIL-LOUD; do NOT spawn on a bad
         precondition — an absent token raises AuthExpired BEFORE any spawn, §7 step 4);
      4. ``reconcile_on_restart`` — replay the WAL + classify every binding against live tmux (necro
         the owned-but-dead, adopt the live; liveness reconstructed from the LEDGER, §5.1);
      5. route the L1 root by its POST-reconcile state (§7 step 6 / review CFW-02): ADOPTED -> done
         (already live, F35); ``running`` -> ``necro.resume_brief`` (RESUME) with the result ROUTED
         (GenesisError on a failed resume); a ``planned`` survivor -> ``chokepoint.claim_and_spawn``
         the SURVIVING slot (no placeholder --resume, no epoch-resetting re-register);
         ``claimed``/``spawning``/``blocked`` -> REAP to terminal (DIED_INFRA via reconcile's single
         terminal-necro write, result routed) then register + ``chokepoint.claim_and_spawn`` fresh
         in-role (role_variant='L1', the shared prompt base + the L1 load-manifest in the neutral
         brief; the adapter composes the runtime `.identity-prompt.md`); absent/terminal ->
         register + spawn (first boot).

    Returns None. Raises a precondition failure (AuthExpired / a pinned-binary error) BEFORE the
    spawn — no L1 actor opens on a bad precondition.
    """
    runtime_root = _runtime_root(config)
    l1_address = getattr(config, "l1_address", "L1#exec")
    l1_level = getattr(config, "l1_level", "L1")
    level_config = getattr(config, "level_config", None) or _config_mod.LevelConfig.for_level(l1_level)
    role_variant = getattr(level_config, "role_variant", l1_level)
    build_id = getattr(config, "build_id", None)
    fidelity_playback_authority = getattr(
        config, "fidelity_playback_authority", "owner"
    )
    fidelity_playback_delegate = getattr(
        config, "fidelity_playback_delegate", None
    )
    fidelity_playback_delegation_reason = getattr(
        config, "fidelity_playback_delegation_reason", None
    )
    review_panel_arms = getattr(config, "review_panel_arms", None)

    # STEP 1+2 — the runtime-descriptor write + precondition gate, under a BRIEFLY-held EX lock.
    # The lock here SERIALIZES the descriptor write + precondition step against concurrent
    # mutations (the §4.3 per-mutation domain) — it is NOT the single-instance guard. The lifetime
    # single-instance guard is the daemon's separate `.harnessd.instance.lock`, acquired in
    # daemon.boot BEFORE genesis runs and held for the process lifetime (§2.3 — the resolved
    # §2.3-vs-§4.3 conflict, review SWCAS-02). This brief acquire is RELEASED before the
    # executor-backed work below and must NOT wrap STEP 4/5: the frozen single-writer executor
    # (and reconcile's replay checkpoint) take this SAME .harnessd.lock, so holding it across them
    # would re-enter fcntl LOCK_EX on the same path and DEADLOCK.
    with store.file_lock(_lock_path(runtime_root), shared=False):
        # STEP 2 — write the daemon runtime descriptor (§2.3). Not control state; no WAL row.
        # lock_path names the INSTANCE lock (the §2.3 self-report; deferred from F6 into F14).
        write_runtime_json(
            runtime_root, build_id=build_id,
            lock_path=str(instance_lock_path(runtime_root)),
        )

        # STEP 3 — PRECONDITION CHECK (FAIL-LOUD). Raises BEFORE any spawn on a bad precondition
        # (an absent OAuth token -> AuthExpired) — the fail-loud gate is ahead of the spawn (§7 step 4).
        # Run inside the held EX lock (no executor call) so a bad precondition fails before any
        # concurrent mutator advances the root slot.
        preconditions(config)

    # STEP 4 — reconcile-on-restart: replay the WAL, classify every binding against live tmux, necro
    # the owned-but-dead, adopt the live. Liveness is reconstructed from the LEDGER (+ tmux), not from
    # memory (§5.1). Routes through the REAL executor, which takes the .harnessd.lock per mutation
    # (and the replay checkpoint takes it explicitly) — so it runs OUTSIDE the brief EX acquire
    # above (no re-entrant fcntl deadlock). NEVER spawns
    # here — the spawn-or-resume decision is STEP 5. The report's ``adopted`` list tells genesis
    # whether the L1 pane was found ALIVE (adopted) — the no-double-spawn discriminator below.
    report = _reconcile_mod.reconcile_on_restart(executor, tmux)

    # STEP 5 — route the L1 root by its POST-reconcile state (§7 step 6 / review CFW-02), reading the
    # POST-reconcile durable ledger. NOT a binary spawn-or-resume: "non-terminal and not adopted" is
    # NOT "resumable" — resume's re-adopt claim (<state> -> claimed) is ILLEGAL from claimed/spawning/
    # blocked (states.ALLOWED_TRANSITIONS admits ->claimed only from {planned, running, dead}), and a
    # planned slot carries only the 'genesis-l1-root' registration PLACEHOLDER session — neither may
    # route to chokepoint.resume. The old binary check dead-ended the cascade root: an illegal-
    # transition claim_lost was DISCARDED and the daemon reported a clean boot with NO L1 actor.
    post = ledger.read_binding(l1_address)
    if post is not None and not states.is_terminal(post.get("state")):
        # A non-terminal L1 binding survived reconcile -> do NOT double-spawn (F35). Four-way routing:
        if l1_address in (report.adopted or []):
            # ALREADY LIVE: reconcile ADOPTED the live L1 pane (pane alive + uuid-matched). The actor
            # is already running and its custody is re-established — there is NOTHING to (re)open. A
            # fresh --resume here would open a SECOND L1 actor (F35 violation). Adopt-and-done.
            return None
        post_state = post.get("state")
        if post_state == "running":
            # RESUME: a running-but-unadopted root (e.g. a uuid-MISMATCHED leftover pane) resumes via
            # necro.resume_brief -> the SINGLE gate-firewall point (chokepoint.resume), which re-adopts
            # the address (running->claimed is legal) and continues it (never a second fresh root).
            # ROUTE the result (review CFW-02, mirroring the F2a spawn-branch surface): a discarded
            # ok=False here was the silent-dead-boot dead-end — L1 is the root, so a failed resume
            # is FATAL and must be loud.
            from harnessd import necro

            result = necro.resume_brief(l1_address, level_config=level_config)
            if not getattr(result, "ok", False):
                failure_class = getattr(result, "failure_class", None) or "unknown"
                raise GenesisError(
                    f"L1 RESUME failed (failure_class={failure_class}, address={l1_address!r}): no L1 "
                    "actor opened — the cascade root would dead-end silently (review CFW-02). Resolve "
                    "the cause and relaunch."
                )
            return None
        if post_state != "planned":
            # INTERMEDIATE (claimed/spawning/blocked — plus any unknown non-terminal state,
            # defensively total): neither adoptable nor resumably-running. REAP the slot to terminal
            # through reconcile's SINGLE terminal-necro write (the legal {non-terminal}->failed
            # DIED_INFRA edge, §3.6 vocab; executor stays the single writer, the epoch bump fences
            # the prior incarnation), ROUTE the result, then FALL THROUGH to the fresh SPAWN below —
            # the reap terminals the old slot BEFORE any fresh spawn (F35 no-double-spawn).
            _died_infra = states.TERMINAL_VOCAB["died_infrastructure"]
            reap = _reconcile_mod._terminal_necro(
                executor,
                l1_address,
                post,
                terminal_signal=_died_infra.terminal_signal,
                target_state=_died_infra.state,
                event=_died_infra.event,
                summary=(
                    f"genesis intermediate-state reap: post-reconcile L1 state={post_state!r} is "
                    "neither adoptable nor resumable (DAEMON §7 step 6 / review CFW-02) — reaped to "
                    "spawn fresh"
                ),
            )
            if reap is None or not getattr(reap, "ok", False):
                errors = list(getattr(reap, "errors", None) or [])
                raise GenesisError(
                    f"genesis intermediate-state reap FAILED (state={post_state!r}, "
                    f"address={l1_address!r}, errors={errors}): the L1 root can be neither resumed "
                    f"from {post_state!r} nor reaped to terminal — boot cannot proceed (review CFW-02)."
                )
        # post_state == 'planned' (or the reap above landed terminal): FALL THROUGH to the SPAWN
        # branch — a planned survivor is spawned fresh through the normal planned->claimed edge.

    # No live/resumable L1 (first boot, a necro'd/reaped prior root, or a planned survivor): SPAWN
    # in-role through the ONE spawn chokepoint (claim-before-spawn, F-024). Re-read the binding — the
    # reap above may have terminal'd it.
    survivor = ledger.read_binding(l1_address)
    if survivor is not None and survivor.get("state") == "planned":
        # An interrupted prior boot left the planned slot: claim it AS-IS. Re-registering would RESET
        # lease_epoch to 1 / generation to 0 — un-fencing a prior incarnation (fencing never regresses).
        registered = survivor
    else:
        if survivor is not None:
            # LT-4/INT-1: a reaped/terminal prior root may still hold its WARM pane (the reap is a
            # ledger write — nothing tears tmux down). The deterministic session name would collide
            # the fresh spawn below ('duplicate session') on EVERY launchd relaunch — an invisible
            # boot crash-loop. Tear the fenced dead incarnation's recorded pane down first.
            chokepoint.kill_stale_pane(survivor.get("tmux_target"))
        registered = _register_l1_root(
            l1_address,
            l1_level,
            role_variant,
            runtime_root,
            fidelity_playback_authority=fidelity_playback_authority,
            fidelity_playback_delegate=fidelity_playback_delegate,
            fidelity_playback_delegation_reason=fidelity_playback_delegation_reason,
            fidelity_playback_authority_build_id=build_id,
            review_panel_arms=review_panel_arms,
        )
    authority_delta = {
        "fidelity_playback_authority": fidelity_playback_authority,
        "fidelity_playback_delegate": fidelity_playback_delegate,
        "fidelity_playback_delegation_reason": (
            fidelity_playback_delegation_reason
        ),
        "fidelity_playback_authority_build_id": build_id,
    }
    if (
        any(registered.get(key) != value for key, value in authority_delta.items())
        or fidelity_playback_authority == "operator-delegate"
    ):
        authority_result = _executor_mod.record_admission(
            l1_address,
            expected_owner_token=registered.get("owner_token"),
            delta=authority_delta,
            event=(
                "fidelity_playback_authority_delegated"
                if fidelity_playback_authority == "operator-delegate"
                else "fidelity_playback_authority_bound"
            ),
            summary=(
                (
                    "commissioning run explicitly delegated final fidelity playback "
                    f"authority to {fidelity_playback_delegate!r}: "
                    f"{fidelity_playback_delegation_reason}"
                )
                if fidelity_playback_authority == "operator-delegate"
                else "run bound owner-final fidelity playback authority"
            ),
        )
        if not authority_result.ok:
            raise GenesisError(
                "could not bind run-scoped fidelity playback authority: "
                + "; ".join(authority_result.errors)
            )
        registered = authority_result.binding
    # INT-5 — the kickoff pointer names brief.md; author the L1 root's brief BEFORE the spawn so
    # the first live agent's first read is a real file (manifest + Sign-off + the intake pointer).
    _ensure_l1_brief(l1_address, level_config, registered, runtime_root)
    _seed_l1_initial_intake(l1_address, runtime_root, getattr(config, "initial_intake", None))
    result = chokepoint.claim_and_spawn(
        l1_address,
        expected_state="planned",
        expected_generation=registered["generation"],
        expected_owner_token=registered["owner_token"],
        level_config=level_config,
    )
    # SURFACE a failed L1 boot (review genesis-1): claim_and_spawn returns ok=False (it does NOT raise)
    # on claim_lost / paused_subtree / a post-claim SpawnFailure (auth_expired / model_unavailable /
    # runtime_down / api_key_forbidden). Discarding it would report a clean boot with NO L1 actor — a
    # silent dead boot. L1 is the root (no parent to escalate to), so a failed L1 spawn is FATAL: raise.
    if not getattr(result, "ok", False):
        failure_class = getattr(result, "failure_class", None) or "unknown"
        raise GenesisError(
            f"first-boot L1 spawn FAILED (failure_class={failure_class}, address={l1_address!r}): no L1 "
            "actor opened. L1 is the root — there is no parent to escalate to; boot cannot proceed. "
            "Resolve the cause (e.g. auth_expired -> refresh the pinned OAuth token) and relaunch."
        )
    return None
