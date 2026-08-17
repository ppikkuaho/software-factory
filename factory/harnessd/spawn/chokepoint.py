"""chokepoint — THE ONE spawn path (claim-before-spawn + rollback + the gate firewall).

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.11 (the FROZEN chokepoint interface — exact signatures below):
        claim_and_spawn(node_address, *, expected_state, expected_generation,
                        expected_owner_token, level_config) -> SpawnResult
        resume(node_address, *, expected_state, expected_generation,
               expected_owner_token, delta_inputs, level_config) -> SpawnResult
        release_claim(node_address, *, expected_owner_token) -> None
        collapse(node_address, terminal_signal, *, expected_owner_token, ...) -> Optional[TransitionResult]
  - IMPLEMENTATION-PLAN §3 module table (chokepoint.py row): "NOT a writer — calls
    executor.transition() for every state change. On any post-claim failure, CAS-releases the claim
    (claimed->planned, bump epoch)."
  - DAEMON §6.1 (claim-before-spawn STEP0-5, the F-024 fix), §6.3 (the E32 spawn-failure contract),
    §6.4 (the gate firewall — LOCKED, correctness-not-optimization).
  - design/ROLE-RESOLUTION.md (the load-manifest assembled into the brief at STEP2).

THE F-024 STRUCTURAL FIX: the control-plane slot is CLAIMED (a REAL CAS transition into ``claimed``
via ``executor.claim`` under the REAL EX lock) STRICTLY BEFORE the actor opens. A lost claim means
``adapter.pin_and_open`` was NEVER reached — no double-spawn is possible. The chokepoint is NOT a
writer: every state change funnels through the single-writer executor.

THE GATE FIREWALL (LOCKED §6.4): ``resume`` refuses ``--resume`` across a crossed quality gate.
The firewall is the SINGLE enforcement point (necro.resume_brief delegates here, never re-raises).
The ``--resume`` argv is CONSTRUCTED ONLY on the gate-NOT-crossed else-branch — there is structurally
no code path that builds a ``--resume`` under ``gate_crossed_at != null``.

BUILDER DECISIONS (the §2.11 details the frozen tests leave open — stated in the build report):

  * ADAPTER INJECTION SEAM — the §2.11 signature carries NO adapter param, so the adapter is a
    module-level injectable (``set_adapter`` / ``ADAPTER``), exactly the ``ledger.RUNTIME_ROOT``
    precedent. The chokepoint orchestrates the adapter PORT; the concrete Claude/Codex fill is wired
    by the daemon (production) or the test (mock). No real adapter is constructed here.

  * ClaimLost RESULT — a lost CAS-claim returns a ``SpawnResult(ok=False, failure_class="claim_lost",
    …)`` — a NON-null ``failure_class`` distinguishes it from a real spawn AND it reads as a
    ClaimLost-flavored outcome (the repr carries "claim"/"lost"). No actor opened. FORK-CLAIMLOST:
    a distinct ``ClaimLost`` type would also satisfy §2.11; the SpawnResult-with-failure_class shape
    reuses the ONE result dataclass (base.SpawnResult) the adapter already returns.

  * THE ESCALATION RECORD (§6.3) — on a post-claim adapter SpawnFailure the chokepoint (1) RELEASES
    the claim (claimed->planned, bump epoch) and (2) emits an L1 escalation. The escalation surface is
    BOTH spec-faithful halves: the returned SpawnResult carries the ``failure_class`` (configured-vs-
    actual classification) AND a ``spawn_failed`` / escalation WAL row naming the child-address +
    which class fired is appended to the run-ledger (so an L1 reader sees it). FORK-ESCALATION: the
    precise downstream transport (an inbox row, a parent-notify) is a later cluster's fork; v1 surfaces
    it on the result + the WAL, the two channels the tests + an L1 reconcile reader can both observe.

  * ancestors_inclusive — STEP0's pause-subtree read-point walks THIS node + its ancestors via the
    binding's ``parent_address`` chain (DAEMON §3.2): start at ``node_address``, follow
    ``parent_address`` upward, collecting every binding, stopping at a null/empty parent or a missing
    binding (a missing ancestor is not paused — it cannot admit/deny). If ANY collected binding has
    ``paused_at != null`` the spawn ABORTS BEFORE the claim. FORK-ANCESTORS: the address-prefix walk
    (DAEMON §6.1 "address-prefix check over the node + its ancestors") and the parent_address-chain
    walk agree on a well-formed tree; the chain walk is authoritative because parent_address is the
    binding's own recorded edge (an address-string prefix can be spoofed by a sibling naming).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import date as calendar_date
from pathlib import Path
from typing import Optional

from harnessd import (
    addressing,
    clock,
    config,
    contracts,
    executor,
    fencing,
    ledger,
    notary,
    plan_alignment,
    plan_alignment_cell,
    product_probes,
    states,
    store,
    turn_state,
)
from harnessd.spawn import blinders, brief, launch_surface, sandbox
from harnessd.spawn.adapters.base import SpawnResult
from harnessd.spawn.oauth_guard import (
    PLACEHOLDER_CONFIG_DIR,
    PLACEHOLDER_OAUTH_TOKEN,
    ApiKeyForbidden,
    SpawnFailure,
)

# ---------------------------------------------------------------------------
# The adapter injection seam (§2.11 carries no adapter param — see module docstring).
# The daemon wires the concrete Claude/Codex fill; tests inject a mock recorder.
# ---------------------------------------------------------------------------

ADAPTER = None

# E4 — the per-runtime adapter REGISTRY (runtime-and-model-map E32: the runtime is a
# config-time dimension, so the chokepoint resolves the adapter from level_config.runtime).
# PRECEDENCE: the single injected ADAPTER (set_adapter) ALWAYS WINS when set — it is the
# explicit override every test uses (and pytest shares one process: a registry-wins rule let
# one daemon-boot test poison every later FakeAdapter test with the real adapters). The
# registry is the PRODUCTION resolution: boot registers both real adapters and injects NO
# single adapter (commissioning ships adapter=None), so a codex-configured L5 resolves the
# CodexAdapter and can never again be silently driven through the ClaudeCodeAdapter (the
# LT-8/O1 divergence, structurally closed).
ADAPTER_REGISTRY: dict = {}

PLAN_ALIGNMENT_READY_FILENAME = "plan-alignment-ready.json"
PLAN_ALIGNMENT_STATE_SEMANTIC_PENDING = "semantic_cell_pending"
PLAN_ALIGNMENT_STATE_READY = "ready"
PLAN_ALIGNMENT_STATE_DECISION_POSTED = "decision_posted"

RESPAWN_TRY_LIMIT = 3
_RESPAWN_ACCOUNTING_FIELDS = (
    "consecutive_failed_incarnations",
    "failed_incarnation_causes",
    "respawn_parked_at",
)


def respawn_parked(binding: Optional[dict]) -> bool:
    """Return whether the stable address has spent its three actor-bearing tries."""
    if not isinstance(binding, dict):
        return False
    return bool(binding.get("respawn_parked_at")) or int(
        binding.get("consecutive_failed_incarnations") or 0
    ) >= RESPAWN_TRY_LIMIT


def actor_death_accounting_delta(
    binding: dict,
    *,
    event: str,
    reason: str,
) -> dict:
    """Build the bounded address-local accounting carried by an actor-death transition."""
    now = clock.now_utc()
    prior_count = int(binding.get("consecutive_failed_incarnations") or 0)
    count = min(RESPAWN_TRY_LIMIT, prior_count + 1)
    prior_causes = [
        copy.deepcopy(cause)
        for cause in list(binding.get("failed_incarnation_causes") or [])
        if isinstance(cause, dict)
    ]
    cause = {
        "event": str(event),
        "reason": str(reason),
        "at": now,
        "session_uuid": binding.get("session_uuid"),
        "lease_epoch": binding.get("lease_epoch"),
    }
    delta = {
        "consecutive_failed_incarnations": count,
        "failed_incarnation_causes": (prior_causes + [cause])[-RESPAWN_TRY_LIMIT:],
    }
    if count >= RESPAWN_TRY_LIMIT:
        delta["respawn_parked_at"] = binding.get("respawn_parked_at") or now
    return delta


def journal_respawn_parked(node_address: str, binding: dict) -> None:
    """Emit the one typed audit row when an address first reaches its third death."""
    causes = [
        copy.deepcopy(cause)
        for cause in list(binding.get("failed_incarnation_causes") or [])
        if isinstance(cause, dict)
    ][-RESPAWN_TRY_LIMIT:]
    executor.journal(
        node_address,
        event="seat_respawn_parked",
        from_state=binding.get("state"),
        to_state=binding.get("state"),
        lease_epoch=binding.get("lease_epoch"),
        owner_token=binding.get("owner_token"),
        binding_delta={
            "respawn_parked_at": binding.get("respawn_parked_at"),
            "consecutive_failed_incarnations": int(
                binding.get("consecutive_failed_incarnations") or 0
            ),
            "causes": causes,
        },
        summary=(
            f"seat {node_address} parked after three consecutive actor deaths: "
            + " | ".join(
                f"{cause.get('event')}:{cause.get('reason')}" for cause in causes
            )
        ),
    )


def reset_actor_death_streak(
    node_address: str,
    *,
    expected_owner_token: Optional[str],
):
    """Reset the address streak after one successfully consumed fenced terminal signal."""
    live = ledger.read_binding(node_address)
    if live is None:
        return None
    if expected_owner_token is not None and live.get("owner_token") != expected_owner_token:
        return None
    if (
        int(live.get("consecutive_failed_incarnations") or 0) == 0
        and not live.get("failed_incarnation_causes")
        and not live.get("respawn_parked_at")
    ):
        return None
    return executor.record_admission(
        node_address,
        expected_owner_token=expected_owner_token,
        delta={
            "consecutive_failed_incarnations": 0,
            "failed_incarnation_causes": [],
            "respawn_parked_at": None,
        },
        event="seat_respawn_streak_reset",
        summary=(
            "consumed fenced terminal signal proved a functioning incarnation; "
            "reset consecutive actor-death streak"
        ),
    )


def inherit_respawn_accounting(binding: dict, previous: Optional[dict]) -> dict:
    """Carry address-local retry truth across a fresh binding incarnation."""
    if not isinstance(previous, dict):
        return binding
    for key in _RESPAWN_ACCOUNTING_FIELDS:
        if key in previous:
            binding[key] = copy.deepcopy(previous[key])
    return binding
COORDINATION_HANDOFF_DIRNAME = "handoffs"
COORDINATION_HANDOFF_TYPE = "coordination_handoff"
COORDINATION_HANDOFF_STATE_SUBMITTED = "submitted"
COORDINATION_HANDOFF_STATE_DECISION_POSTED = "decision_posted"
COORDINATION_HANDOFF_STATE_NOTICE_POSTED = "notice_posted"
COORDINATION_HANDOFF_ALLOWED_KINDS = {
    "phase_ready",
    "scope_issue",
    "plan_gap",
    "interface_issue",
    "acceptance_gap",
    "approval_request",
    "guidance_request",
    "status_notice",
}


def set_adapter(adapter) -> None:
    """Inject the RuntimeAdapter the chokepoint opens actors through (module-level seam)."""
    global ADAPTER
    ADAPTER = adapter


def register_runtime_adapter(runtime: str, adapter) -> None:
    """Register the adapter for one runtime key (production boot wiring; E4)."""
    ADAPTER_REGISTRY[runtime] = adapter


def clear_runtime_adapters() -> None:
    """Reset the registry (test hygiene)."""
    ADAPTER_REGISTRY.clear()


def _require_adapter(level_config=None):
    """Resolve the adapter: the injected ADAPTER when set (the explicit test/legacy override);
    else the registered per-runtime adapter for level_config.runtime; else fail loud."""
    if ADAPTER is not None:
        return ADAPTER
    runtime = getattr(level_config, "runtime", None) if level_config is not None else None
    if runtime and runtime in ADAPTER_REGISTRY:
        return ADAPTER_REGISTRY[runtime]
    raise RuntimeError(
        "no RuntimeAdapter available for this spawn: register one via "
        "register_runtime_adapter(runtime, adapter) or inject via set_adapter(adapter) "
        "(the §2.11 signature carries no adapter param — the adapter is injected like "
        "ledger.RUNTIME_ROOT)"
    )


# ---------------------------------------------------------------------------
# The spawn-env injection seam (LT-1) — mirrors set_adapter. The daemon binds the REAL
# commissioned 4-var OAuth env at boot (daemon.boot reads runtime.config.env); when nothing is
# bound, _spawn_env() falls back to the STRUCTURAL placeholders (keeps the dry-run tests intact).
# ---------------------------------------------------------------------------

SPAWN_ENV: Optional[dict] = None


def set_spawn_env(env: Optional[dict]) -> None:
    """Bind (or clear) the REAL pane env every structural spawn opens with (module-level seam).

    Production: ``daemon.boot`` binds commissioning's assembled 4-var OAuth-only env
    (``runtime.config.env`` — live token + the pinned CLAUDE_CONFIG_DIR) so the env that passed
    the genesis credential precondition is the SAME env the pane actually boots with (LT-1: the
    placeholder env never reaches a real pane). ``set_spawn_env(None)`` restores the structural
    placeholder fallback (the dry-run shape).
    """
    global SPAWN_ENV
    SPAWN_ENV = dict(env) if env is not None else None


# ---------------------------------------------------------------------------
# ClaimLost / failure result helpers — a lost claim or an aborted spawn is a
# NON-null-failure_class SpawnResult, distinguishable from a real spawn.
# ---------------------------------------------------------------------------

def _result_failed(failure_class: str, *, tmux_target: str = "", model_used: str = "") -> SpawnResult:
    """Build a not-ok SpawnResult carrying a ``failure_class`` (ClaimLost / SpawnFailure outcome).

    No actor opened (or the actor failed and was rolled back). ``failure_class`` names the outcome
    so the caller can distinguish a lost claim / a spawn failure from a successful spawn.
    """
    return SpawnResult(
        ok=False,
        session_uuid=None,
        model_used=model_used,
        role_variant="",
        system_prompt_file=config.SYSTEM_PROMPT_FILE,
        system_prompt_file_hash="",
        tmux_target=tmux_target,
        transcript_path=None,
        failure_class=failure_class,
    )


# ---------------------------------------------------------------------------
# STEP0 — the pause-subtree read-point (ancestors_inclusive).
# ---------------------------------------------------------------------------

def ancestors_inclusive(node_address: str) -> list[dict]:
    """Return THIS node's binding + every ancestor binding, walking ``parent_address`` upward.

    Starts at ``node_address`` and follows the binding's recorded ``parent_address`` edge (DAEMON
    §3.2) to the root, collecting each binding. Stops at a null/empty parent, a missing binding, or
    a cycle (defensive — a self/loop parent terminates the walk). A missing binding contributes
    nothing (it cannot pause a subtree). The returned list is the node + ancestors, inclusive.
    """
    collected: list[dict] = []
    seen: set[str] = set()
    addr: Optional[str] = node_address
    while addr and addr not in seen:
        seen.add(addr)
        binding = ledger.read_binding(addr)
        if binding is None:
            break
        collected.append(binding)
        parent = binding.get("parent_address")
        addr = parent or None
    return collected


def subtree_paused(node_address: str) -> bool:
    """True iff THIS node or any ancestor has ``paused_at`` set — the pause-subtree predicate.

    PUBLIC (F16): both enforcing read-points share this ONE node-or-ancestor walk so the
    prefix semantics cannot drift — the spawn chokepoint's STEP0 (DAEMON §6.1: a paused
    subtree admits no child) AND the watchdog's §3.4 STEP 0 gate (WATCHDOG: a paused subtree
    gets no recovery action).
    """
    return any(b.get("paused_at") is not None for b in ancestors_inclusive(node_address))


_subtree_paused = subtree_paused  # back-compat alias (the pre-F16 private name)


# ---------------------------------------------------------------------------
# Stale-pane teardown (LT-4/INT-1) — the idempotent prior-incarnation kill the
# resume / re-register paths run before reopening a deterministic session name.
# ---------------------------------------------------------------------------

def _stale_session_name(identifier: str) -> str:
    """Normalize ANY recorded pane identifier to the tmux SESSION name the kill targets (LR-21).

    Three shapes reach the teardown: a node ADDRESS ('proj/widget#exec' — the spawn leg passes
    it), the RECORDED '<session>:<window>.<pane>' triple STEP4 stamped (re-register/resume pass
    it), and a bare session name. ``tmux kill-session`` wants the session — an address names no
    session (the kill tears down NOTHING, error swallowed: the Run-2 assembly collision), and a
    triple must keep only its session part.
    """
    if "/" in identifier or "#" in identifier:
        return addressing.session_name_for(identifier)
    return identifier.split(":", 1)[0]


def kill_stale_pane(tmux_target: Optional[str], adapter=None) -> None:
    """Best-effort, IDEMPOTENT teardown of a PRIOR incarnation's recorded pane (LT-4/INT-1).

    ``addressing.session_name_for`` is deterministic per address, so a respawn at an address whose
    previous incarnation's pane still exists collides in ``create_detached`` ('duplicate session' ->
    SpawnFailure tmux_session_collision). The resume / re-register paths call this with the FENCED
    prior incarnation's recorded ``tmux_target``, strictly AFTER the re-adopting claim committed
    (the epoch bump already fenced the prior incarnation — no live owner holds that pane) and
    strictly BEFORE the fresh ``create_detached``. The identifier is normalized to its SESSION
    name (LR-21 — an address or a triple kills nothing as-is). Seam resolution (LR-21): the
    adapter arg, else the injected ADAPTER; a seam that exists but lacks ``tmux.kill`` skips (the
    dry-run tears nothing down); NO seam at all — production, where E4 moved adapters to the
    registry and ADAPTER ships None — routes through the REAL tmux module (the old seam-or-skip
    made the production teardown a silent no-op at the re-register/resume sites). ``tmux.kill``
    itself is idempotent (a session already gone is not an error), and any teardown hiccup is
    swallowed — the collision SpawnFailure at create_detached remains the loud net.
    """
    if not tmux_target:
        return
    session = _stale_session_name(str(tmux_target))
    seam = adapter if adapter is not None else ADAPTER
    if seam is not None:
        kill = getattr(getattr(seam, "tmux", None), "kill", None)
        if kill is None:
            return
    else:
        from harnessd.spawn import tmux as _tmux_mod

        kill = _tmux_mod.kill
    try:
        kill(session)
    except Exception:  # noqa: BLE001 — best-effort teardown; the collision SpawnFailure is the net
        pass


# ---------------------------------------------------------------------------
# Re-registration identity seeding (SM-1 / SM-2 / INT-4(b)) — fencing NEVER regresses.
# ---------------------------------------------------------------------------

def reregister_identity_seed(node_address: str) -> tuple:
    """The fencing-monotonic ``(lease_epoch, last_applied_seq)`` seed for (re-)registering an address.

    A FRESH address (no prior binding AND no WAL rows naming it) seeds ``(1, 0)`` — the first-boot
    shape. An address WITH PRIOR HISTORY seeds:

      * ``lease_epoch = max(prior binding epoch, max WAL epoch for the address) + 1`` — DAEMON §8's
        per-node epoch monotonicity must hold ACROSS incarnations (SM-1): the old reset-to-1 meant
        the next ``executor.claim`` (epoch 2) re-minted the PRIOR incarnation's byte-identical
        owner_token (mint_owner_token is a pure composite over deterministic placeholder identity),
        so a leftover ``.signal.<seat>.json`` passed the F19 fence and collapsed every respawn at
        the same address — and a uuid-mismatched zombie actor retained a VALID token over the new
        incarnation. The WAL max is consulted too: a crash between a necro's WAL append and its
        binding checkpoint must not lose the bump.
      * ``last_applied_seq = the current max WAL seq`` — the per-node replay watermark NEVER
        regresses (SM-2): a reset-to-0 against the append-only WAL let boot replay re-apply the
        dead incarnation's ENTIRE chain (each old row pre-image-matches the re-registered gen-0
        binding), resurrecting the prior terminal state and orphaning the new claim row. Seeding
        the watermark at the current max puts every prior row below it; the new incarnation's own
        rows allocate above and replay normally.
    """
    prior = ledger.read_binding(node_address)
    has_history = prior is not None
    max_epoch = 0
    if prior is not None:
        epoch = prior.get("lease_epoch")
        if isinstance(epoch, int):
            max_epoch = epoch
    max_seq = 0
    for record in ledger.load_wal():
        seq = record.get("seq")
        if isinstance(seq, int) and seq > max_seq:
            max_seq = seq
        if record.get("node_address") == node_address:
            has_history = True
            epoch = record.get("lease_epoch")
            if isinstance(epoch, int) and epoch > max_epoch:
                max_epoch = epoch
    if not has_history:
        return 1, 0
    return max_epoch + 1, max_seq


def purge_stale_seat_artifacts(node_address: str) -> None:
    """Delete the PRIOR incarnation's seat artifacts at re-register time (SM-1 belt-and-braces).

    ``.signal.<seat>.json`` / ``.sign-off.<seat>.json`` in the node dir belong to the incarnation
    being REPLACED — no production path ever unlinked them (promote only excludes them). Epoch
    monotonicity (reregister_identity_seed) already fences them out, but the stale artifact is
    dead weight every future tick re-reads; deleting it at the re-register write-point keeps the
    node dir truthful. SEAT-SCOPED on purpose: the address carries its seat, so a co-located
    ``#review`` seat's live artifacts are never touched. Best-effort (the fence is the guarantee).
    """
    if ledger.RUNTIME_ROOT is None:
        return
    for derive in (
        addressing.signal_path,
        addressing.signoff_path,
        addressing.turn_state_path,
        addressing.turn_events_path,
        addressing.owed_checklist_path,
        addressing.turn_state_lock_path,
    ):
        try:
            derive(node_address, ledger.RUNTIME_ROOT).unlink(missing_ok=True)
        except OSError:
            pass


def _prior_incarnation_archive_dir(node_address: str, previous_binding: Optional[dict]) -> Path:
    _path, seat = addressing.split_address(node_address)
    lease = (previous_binding or {}).get("lease_epoch")
    lease_label = str(lease) if lease is not None else "unknown"
    return (
        Path(ledger.RUNTIME_ROOT)
        / ".harnessd"
        / "incarnation-archives"
        / "nodes"
        / addressing.node_path(node_address)
        / f"lease-{lease_label}"
        / seat
    )


def _archive_path(path: Path, archive_dir: Path, node_dir: Path, runtime_root: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    raw = path.read_bytes()
    original = path.relative_to(node_dir).as_posix()
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / path.name
    if dest.exists():
        digest = hashlib.sha256(raw).hexdigest()[:12]
        dest = archive_dir / f"{path.stem}.{digest}{path.suffix}"
    shutil.move(str(path), str(dest))
    return {
        "original_path": original,
        "archive_path": dest.relative_to(runtime_root).as_posix(),
        "archive_abspath": str(dest),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def archive_prior_incarnation_surface(
    node_address: str,
    previous_binding: Optional[dict],
    *,
    include_work_forms: bool = False,
) -> None:
    """Move prior-incarnation current surfaces aside before a fresh same-address actor opens.

    The address is stable across incarnations, but the acting agent's local surface must be current.
    A fresh execution-L3 after a planning-L3, or a post-PASS repair actor, should not boot with the
    previous actor's ``plan.md``/``report.md`` looking like its own current work. We keep those files
    as control-plane provenance under ``<runtime>/.harnessd/incarnation-archives/`` and let the spawn
    instantiate fresh forms.

    Seat transport is also incarnation-local: old ``.inbox.<seat>.jsonl`` rows and sign-off artifacts
    are history, not instructions for the next actor. Review gate directories stay in place because
    they are gate-id keyed provenance and may still be referenced by parent/audit artifacts.
    """
    if ledger.RUNTIME_ROOT is None or previous_binding is None:
        return
    archive_dir = _prior_incarnation_archive_dir(node_address, previous_binding)
    try:
        runtime_root = Path(ledger.RUNTIME_ROOT)
        node_dir = _child_node_dir(node_address)
        archived: list[dict] = []
        if include_work_forms:
            for name in (
                "plan.md",
                "report.md",
                "composition-report.md",
                "area-composition-review.md",
                "composition-review.md",
            ):
                entry = _archive_path(node_dir / name, archive_dir, node_dir, runtime_root)
                if entry:
                    entry["kind"] = "work_surface"
                    archived.append(entry)
        for derive in (
            addressing.inbox_path,
            addressing.signal_path,
            addressing.signoff_path,
            addressing.turn_state_path,
            addressing.turn_events_path,
            addressing.owed_checklist_path,
            addressing.turn_state_lock_path,
        ):
            entry = _archive_path(derive(node_address, ledger.RUNTIME_ROOT), archive_dir, node_dir, runtime_root)
            if entry:
                entry["kind"] = "seat_transport"
                archived.append(entry)
        if archived:
            _path, seat = addressing.split_address(node_address)
            payload = {
                "schema_version": 1,
                "node_address": node_address,
                "seat": seat,
                "previous_lease_epoch": previous_binding.get("lease_epoch"),
                "previous_gate_id": previous_binding.get("gate_id"),
                "previous_gate_state": previous_binding.get("gate_state"),
                "archived_at": clock.now_utc(),
                "archive_location": "runtime_control_plane",
                "archive_root": str(archive_dir),
                "files": archived,
            }
            store.atomic_replace(
                archive_dir / "incarnation-archive.json",
                lambda handle: handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n"),
            )
    except Exception:  # noqa: BLE001 — the fence/fresh forms are the guarantee; archive is best-effort
        pass


def _current_inbox_size(node_address: str) -> int:
    if ledger.RUNTIME_ROOT is None:
        return 0
    try:
        return addressing.inbox_path(node_address, ledger.RUNTIME_ROOT).stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# The escalation seam (§6.3) — emit an L1 spawn-failure escalation WAL row.
# ---------------------------------------------------------------------------

def _emit_spawn_failure_escalation(
    node_address: str,
    failure_class: str,
    model_used: str,
    *,
    released: bool = True,
    failure_reason: Optional[str] = None,
) -> None:
    """Append a spawn-failure escalation row to the run-ledger naming the child + which class fired.

    §6.3: on a refused spawn, RELEASE the claim and emit a spawn-failure escalation to L1
    (child-address + configured vs actual + which class fired). This is a DIRECT WAL append (not a
    lifecycle transition — the lifecycle rollback is the separate release_claim): an L1 reconcile
    reader sees the ``spawn_failed`` event naming the node + the class. Best-effort journaling — a
    journaling hiccup must not mask the underlying spawn failure, which is also carried on the result.
    Routed through ``executor.journal`` (SWL-01): the seq allocation + append run under the
    per-mutation EX lock, never racing the locked single writer on the other daemon thread.

    ``released`` threads the rollback's TRUE outcome (RR-7): the row used to hard-code 'claim
    released (§6.3)' even when the release_claim/_rollback_spawning CAS aborted — a journaled
    phantom rollback. A released=False row says so explicitly (the slot is still claimed/spawning;
    reconcile will eventually reap it, and the audit trail must not lie about the mutation outcome).
    """
    try:
        rollback_note = (
            "claim released (§6.3)" if released
            else "CLAIM ROLLBACK ABORTED — slot NOT released (the rollback CAS missed; §6.3 routing)"
        )
        reason = str(failure_reason or "").strip()
        reason_note = f", reason={reason}" if reason else ""
        delta = {
            "failure_class": failure_class,
            "model_used": model_used,
            "claim_released": released,
        }
        if reason:
            delta["failure_reason"] = reason
        executor.journal(
            node_address,
            event="spawn_failed",
            from_state="claimed",
            to_state="planned" if released else "claimed",
            binding_delta=delta,
            summary=(
                f"spawn-failure escalation -> L1: node {node_address} failed to spawn "
                f"(class={failure_class}, model_used={model_used}{reason_note}); {rollback_note}"
            ),
        )
        _notify_parent_of_spawn_failure(
            node_address,
            failure_class=failure_class,
            model_used=model_used,
            claim_released=released,
            failure_reason=reason or None,
        )
    except Exception:
        # The result already carries failure_class; a WAL hiccup must not swallow the spawn failure.
        return None


def _notify_parent_of_spawn_failure(
    node_address: str,
    *,
    failure_class: str,
    model_used: str,
    claim_released: bool,
    failure_reason: Optional[str] = None,
) -> None:
    """Append one child_spawn_failed pointer line to the direct parent's inbox."""
    try:
        if ledger.RUNTIME_ROOT is None:
            return
        child = ledger.read_binding(node_address)
        parent = (child or {}).get("parent_address")
        if not parent:
            return
        parent_binding = ledger.read_binding(parent)
        if parent_binding is None or states.is_terminal(parent_binding.get("state")):
            return
        inbox = addressing.inbox_path(parent, ledger.RUNTIME_ROOT)
        if _inbox_has_line(
            inbox,
            type="child_spawn_failed",
            child=node_address,
            failure_class=failure_class,
            failure_reason=failure_reason,
        ):
            return
        node_dir = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
        inbox.parent.mkdir(parents=True, exist_ok=True)
        reason = str(failure_reason or "").strip()
        line = json.dumps({
            "from": "harnessd",
            "type": "child_spawn_failed",
            "child": node_address,
            "failure_class": failure_class,
            "failure_reason": reason or None,
            "model_used": model_used,
            "claim_released": claim_released,
            "message": (
                f"Your child {node_address} could not open its runtime "
                f"(class={failure_class}, model_used={model_used or 'unknown'}"
                + (f", reason={reason}" if reason else "")
                + f"). Its workspace is {node_dir}/. Decide whether to retry, repair runtime "
                "preconditions, adjust the task, or escalate."
            ),
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort, never masks spawn failure
        return None


# ---------------------------------------------------------------------------
# release_claim — the standalone rollback edge (CAS claimed -> planned, bump epoch).
# ---------------------------------------------------------------------------

def release_claim(node_address: str, *, expected_owner_token: Optional[str]):
    """Release a claim: CAS ``claimed`` -> ``planned``, BUMP the epoch (§6.1 rollback edge).

    The first-class rollback edge. The release is itself a CAS-guarded transition (replayable via the
    WAL), so a failed claim is reclaimable: the slot returns to ``planned`` and the epoch advances
    AGAIN (claim 1->2, release 2->3) so the rolled-back slot fences the failed incarnation. NOT a
    writer itself — routes through ``executor.transition`` (the single writer). Re-mints the
    owner_token at the bumped epoch in the SAME candidate (F-012, no split window).

    RETURNS the ``TransitionResult`` (RR-7 — the last unrouted TransitionResult on the spawn
    path): a CAS-aborted rollback leaves the slot ``claimed``, and the §6.3 escalation row must
    record the TRUE rollback outcome, never assert 'claim released' for a release that aborted.
    Returns None only when the node is absent (nothing to release). Callers MAY ignore an ok=True
    result — only the failure leg is load-bearing.
    """
    live = ledger.read_binding(node_address)
    if live is None:
        return None

    new_lease_epoch = fencing.advance_epoch(live)
    new_owner_token = fencing.mint_owner_token(
        node_address,
        live.get("subagent_id"),
        live.get("session_uuid"),
        new_lease_epoch,
    )
    return executor.transition(
        node_address,
        expected_state="claimed",
        expected_generation=live["generation"],
        expected_owner_token=expected_owner_token,
        target_state="planned",
        binding_delta={
            "state": "planned",
            "lease_epoch": new_lease_epoch,
            "owner_token": new_owner_token,
        },
        new_lease_epoch=new_lease_epoch,
        new_owner_token=new_owner_token,
        event="release_claim",
        summary="claim released: CAS claimed->planned, bump epoch (rollback edge, §6.1)",
    )


# ---------------------------------------------------------------------------
# The shared post-claim spawn body (STEP2-5) — used by claim_and_spawn and resume's branches.
# ---------------------------------------------------------------------------

def _write_signoff_handshake(node_address: str, owner_token: str) -> None:
    """Seed the per-incarnation sign-off HANDSHAKE into the node dir (F19 — the token delivery).

    ``<node-dir>/.sign-off.<seat>.json`` carries the POST-claim re-minted ``owner_token``, the
    absolute ``.signal.<seat>.json`` path, and the signal schema. Written strictly AFTER the claim
    commits (a lost claim never reaches this — no loser-token handshake can land) and strictly
    BEFORE ``adapter.pin_and_open`` (the agent can read it from its first turn). This is the ONLY
    channel that delivers the fence token to the live agent: the brief payload omits it, brief.md
    may be pre-authored at plan time BEFORE the claim mints the token, and the unjailed pane env is
    contractually EXACTLY the 4 isolation vars (claude_code adapter) — so a node-dir file is the one
    channel that survives all three. The agent copies ``owner_token`` VERBATIM into its
    ``.signal.<seat>.json``; the fenced reader (detector_signals.read_terminal_signal) silently
    ignores any other token.

    Refreshed by THIS same write-point on every re-claim (the §6.4 resume flows through
    ``_spawn_after_claim``), so the file always names the CURRENT incarnation's token. A stale
    leftover (a post-claim spawn failure releases the claim at a bumped epoch) is harmless: the
    fence rejects its token, and the next successful claim overwrites the file BEFORE the new pane
    opens.

    NODE-WORKSPACE SEEDING (like brief.md), NOT a ledger write — the executor stays the single
    ledger writer; no TransitionResult is produced or swallowed here.
    """
    if ledger.RUNTIME_ROOT is None:
        raise RuntimeError(
            "_write_signoff_handshake: ledger.RUNTIME_ROOT is not bound — the handshake lands under "
            "the runtime tree (nodes/<nested-path>/.sign-off.<seat>.json); bind it (daemon startup / "
            "tests). Never a silent skip: an agent without the handshake cannot sign off (its "
            "terminal signal would be fenced as stale)."
        )
    payload = {
        "owner_token": owner_token,
        "signal_path": str(addressing.signal_path(node_address, ledger.RUNTIME_ROOT)),
        "schema": {
            "signal": "DONE|FAILED",
            "ts": "ISO-8601 UTC",
            "owner_token": "<this token, verbatim>",
            "evidence": (
                "optional dict (e.g. {report: 'report.md', notes: '<completion note / failure "
                "reason>'}); blocked work uses a canonical needs_answer question and park, not "
                "a terminal signal"
            ),
        },
    }
    store.atomic_replace(
        addressing.signoff_path(node_address, ledger.RUNTIME_ROOT),
        lambda h: (h.write(json.dumps(payload, indent=2)), h.write("\n")),
    )


def _materialize_launch_surface(node_address: str, level_config, spawn_brief: dict) -> dict:
    """Generate pilot-role launch/reference artifacts and enrich the adapter brief.

    The generator is intentionally post-handshake and pre-adapter-open: it can include the live
    sign-off token/path without asking the agent to discover runtime substrate. Non-pilot roles
    return the original brief shape unchanged.
    """
    artifacts = launch_surface.materialize(
        node_address,
        level_config,
        dict(spawn_brief),
        runtime_root=ledger.RUNTIME_ROOT,
    )
    if artifacts is None:
        return spawn_brief
    enriched = dict(spawn_brief)
    enriched.update(
        {
            "launch_packet_file": artifacts.launch_packet_file,
            "launch_packet_hash": artifacts.launch_packet_hash,
            "launch_surface_source_hash": artifacts.launch_surface_source_hash,
            "reference_map_file": artifacts.reference_map_file,
            "reference_map_hash": artifacts.reference_map_hash,
            "reference_map_json_file": artifacts.reference_map_json_file,
            "launch_surface_version": artifacts.version,
            "launch_surface_role": artifacts.role,
            "launch_surface_source_blocks": list(artifacts.source_blocks),
        }
    )
    return enriched


def _spawn_after_claim(
    node_address: str,
    claimed_binding: dict,
    level_config,
    spawn_brief,
    spawn_env: Optional[dict] = None,
) -> SpawnResult:
    """STEP2-5 after a committed claim: assemble brief, open the actor, record facts, reach running.

    The claim is ALREADY committed (``claimed_binding`` is the post-claim binding: state='claimed',
    bumped epoch, re-minted owner_token). On ANY failure STEP2-5 the claim is RELEASED
    (claimed->planned, bump epoch) and a spawn-failure escalation is emitted (§6.3). On success the
    node ends in ``running`` with the actual session_uuid / transcript_path / model_used recorded.

    ``spawn_env`` is the pane env handed to the adapter. The JAIL-WIRING path passes the
    cache-redirect-MERGED env (a containment spawn); the structural path passes None, falling back to
    the bare 4-var ``_spawn_env()`` (UNJAILED — exactly the 4 isolation vars, the Increment-14
    integration-B contract).
    """
    adapter = _require_adapter(level_config)
    # LR-18 — a PRIOR incarnation's pane at this address's deterministic session name collides
    # with the fresh create_detached (collapse never reaps panes; observed twice live in Run-2:
    # the watchdog-FAILED L1's pane, then the collapsed planning-L3's pane vs its execution-L3
    # respawn). The claim is COMMITTED (epoch bumped — any prior owner is fenced out), so the
    # stale session is teardown-safe; best-effort, the collision SpawnFailure stays the loud net.
    kill_stale_pane(node_address, adapter=adapter)
    post_claim_token = claimed_binding["owner_token"]
    post_claim_generation = claimed_binding["generation"]
    pane_env = _spawn_env() if spawn_env is None else spawn_env

    # STEP2b — seed the sign-off HANDSHAKE (F19): strictly AFTER the committed claim
    # (post_claim_token IS the re-minted token) and strictly BEFORE the actor opens, so the agent
    # reads its owner_token + signal path in place from its very first turn. A lost claim never
    # reaches here (the F-024 ordering), so a loser token never lands in the node dir.
    _write_signoff_handshake(node_address, post_claim_token)

    # STEP3 — materialize the required hook/checklist surface, then pin + open the actor. The claim
    # is STRICTLY before this (the F-024 ordering). Hook materialization lives inside the same TOTAL
    # post-claim rollback net as adapter open: a seat without its truthful turn-state contract never
    # opens and never strands the claim.
    turn_surface: dict = {}
    try:
        hook_profile = turn_state.profile_for_runtime(getattr(level_config, "runtime", None))
        turn_surface = turn_state.seed(
            node_address,
            claimed_binding,
            runtime_root=ledger.RUNTIME_ROOT,
            profile=hook_profile,
        )
        spawn_brief = dict(spawn_brief)
        spawn_brief.update(turn_surface)
        spawn_brief = _materialize_launch_surface(node_address, level_config, spawn_brief)
        # The physical readable world includes the launch/reference surfaces generated immediately
        # above, so containment MUST be finalized here rather than at neutral-brief assembly.
        if _containment_requested(level_config) and ledger.RUNTIME_ROOT is not None:
            spawn_brief, pane_env = _produce_containment(
                node_address,
                level_config,
                spawn_brief,
                pane_env,
            )
        spawn_result = adapter.pin_and_open(spawn_brief, level_config, node_address, pane_env)
    except (SpawnFailure, ApiKeyForbidden) as exc:
        # POST-claim failure (§6.3): release the claim and escalate to L1 with the SPECIFIC class that
        # fired. ApiKeyForbidden is caught here too (it is NOT a SpawnFailure but is a post-claim spawn
        # refusal) — else it leaks UNCAUGHT past the chokepoint, crashing the spawn path AND leaving the
        # claim committed (review claude_code-3). Each exception now carries its own failure_class
        # (auth_expired / api_key_forbidden / …) so an auth lapse no longer masquerades as a model outage.
        failure_class = getattr(exc, "failure_class", None) or "model_unavailable"
        model_used = getattr(exc, "model_used", "")
        released = release_claim(node_address, expected_owner_token=post_claim_token)
        _emit_spawn_failure_escalation(
            node_address, failure_class, model_used,
            released=bool(getattr(released, "ok", False)),
            failure_reason=str(exc),
        )
        return _result_failed(failure_class, tmux_target=node_address, model_used=model_used)
    except Exception as exc:  # noqa: BLE001 — RR-5: the §6.3 rollback net must be TOTAL
        # ANY non-blessed exception (a tmux CalledProcessError the LT-4 conversion missed, a
        # FileNotFoundError on the tmux binary, an AttributeError from a garbled .claude.json in
        # seed_trust, an OSError on a profile write, …) used to escape with the claim COMMITTED:
        # no release, no spawn_failed escalation, the node stranded `claimed` until reconcile
        # mis-necro'd it DIED_INFRA (a death class — the §6.3 'which class fired' row L1 should
        # see never existed), and on the IPC route it killed the control-plane thread (RR-1).
        # The module's own STEP2-5 contract is 'on ANY failure the claim is RELEASED + a
        # spawn-failure escalation is emitted' — make the catch total, preserving the specific
        # classes above. Mirrors outbox._service_one's catch-all rationale.
        failure_class = f"spawn_exception:{type(exc).__name__}"
        released = release_claim(node_address, expected_owner_token=post_claim_token)
        _emit_spawn_failure_escalation(
            node_address, failure_class, "",
            released=bool(getattr(released, "ok", False)),
            failure_reason=str(exc),
        )
        return _result_failed(failure_class, tmux_target=node_address)

    if not getattr(spawn_result, "ok", False):
        # The adapter reported a non-ok spawn without raising — treat as a post-claim failure too.
        failure_class = getattr(spawn_result, "failure_class", None) or "runtime_down"
        released = release_claim(node_address, expected_owner_token=post_claim_token)
        _emit_spawn_failure_escalation(
            node_address, failure_class, getattr(spawn_result, "model_used", ""),
            released=bool(getattr(released, "ok", False)),
            failure_reason=getattr(spawn_result, "failure_reason", None)
            or getattr(spawn_result, "failure_message", None),
        )
        return _result_failed(failure_class, tmux_target=node_address)

    # STEP4 — record the actor's products (session_uuid + the NON-NULL transcript_path + the ACTUAL
    # model_used) via the single writer. claimed -> spawning. config = intent; model_used = fact.
    step4 = executor.transition(
        node_address,
        expected_state="claimed",
        expected_generation=post_claim_generation,
        expected_owner_token=post_claim_token,
        target_state="spawning",
        binding_delta={
            "session_uuid": spawn_result.session_uuid,
            "transcript_path": spawn_result.transcript_path,
            "model_used": spawn_result.model_used,
            "role_variant": spawn_result.role_variant,
            "system_prompt_file": spawn_result.system_prompt_file,
            # F18/OSA-01: the CANONICAL live target ('<session>:<window>.<pane>', tmux's own
            # post-rename report returned by create_detached) overwrites the registration
            # placeholder — pane_alive / the reconcile sweep / send-keys key off THIS value.
            **({"tmux_target": spawn_result.tmux_target} if spawn_result.tmux_target else {}),
            # The journaled permission posture (SECURITY.md §4.3 — auditable like OAuth-only):
            # 'jailed-skip-permissions' | 'unjailed-prompting' |
            # 'unjailed-skip-permissions-override' (the USER-APPROVED supervised-smoke knob).
            # Stamped only when the adapter reports it (a fake/legacy fill omitting the field
            # leaves the binding unchanged).
            **(
                {"permission_posture": spawn_result.permission_posture}
                if getattr(spawn_result, "permission_posture", None)
                else {}
            ),
            **(
                {"containment_posture": spawn_result.containment_posture}
                if getattr(spawn_result, "containment_posture", None)
                else {}
            ),
            **(
                {"codex_seat_id": spawn_result.codex_seat_id}
                if getattr(spawn_result, "codex_seat_id", None)
                else {}
            ),
            **(
                {"auth_version": spawn_result.codex_auth_version}
                if getattr(spawn_result, "codex_auth_version", None)
                else {}
            ),
            **(
                {"codex_access_seconds_remaining": spawn_result.codex_access_seconds_remaining}
                if getattr(spawn_result, "codex_access_seconds_remaining", None) is not None
                else {}
            ),
            **(
                {"launch_packet_file": spawn_result.launch_packet_file}
                if getattr(spawn_result, "launch_packet_file", None)
                else {}
            ),
            **(
                {"launch_packet_hash": spawn_result.launch_packet_hash}
                if getattr(spawn_result, "launch_packet_hash", None)
                else {}
            ),
            **(
                {"launch_surface_source_hash": spawn_result.launch_surface_source_hash}
                if getattr(spawn_result, "launch_surface_source_hash", None)
                else {}
            ),
            **(
                {"reference_map_file": spawn_result.reference_map_file}
                if getattr(spawn_result, "reference_map_file", None)
                else {}
            ),
            **(
                {"reference_map_hash": spawn_result.reference_map_hash}
                if getattr(spawn_result, "reference_map_hash", None)
                else {}
            ),
            **(
                {"reference_map_json_file": spawn_result.reference_map_json_file}
                if getattr(spawn_result, "reference_map_json_file", None)
                else {}
            ),
            **(
                {"launch_surface_version": spawn_result.launch_surface_version}
                if getattr(spawn_result, "launch_surface_version", None)
                else {}
            ),
            **{
                key: turn_surface[key]
                for key in (
                    "turn_hook_profile",
                    "turn_state_path",
                    "turn_events_path",
                    "owed_checklist_path",
                    "turn_state_lock_path",
                )
                if turn_surface.get(key)
            },
            # RESPAWN-CURSOR (live Run-5, 2026-07-31) — the turn-event consumer cursor is
            # per-INCARNATION, because STEP3's ``turn_state.seed`` RECREATES the seat's
            # ``.turn-events.<seat>.jsonl`` (a zero-byte durable truncate) a few lines above. On the
            # claim-from-dead necro route (claim_and_spawn against a reconcile-stamped ``dead``
            # binding) the row SURVIVES the respawn, so a stale ``turn_event_acked_offset`` from the
            # dead incarnation (observed: 398659 against an 18K file) made
            # daemon._adopt_turn_state_for_seat seek past EOF, read zero rows, and re-stamp the same
            # offset forever: turn_state frozen at ``not_started``, Stop rows unconsumed, the
            # wake/owed machinery dead for that seat. The register route
            # (register_and_spawn_child -> _register_child) never showed it only because it writes a
            # WHOLE FRESH binding row, dropping the cursor and seeding liveness_state='claimed'.
            # Reset both HERE — the one transition that stamps this incarnation's turn surface —
            # so every spawn route agrees. Same rationale as executor.claim's seat_stall_* reset
            # ("a claim opens a fresh actor incarnation"); placed at STEP4 rather than at the claim
            # so the zero cursor is committed AFTER the log is truncated, never against the dead
            # incarnation's still-fat file.
            #
            # ``last_inbox_acked_offset`` is deliberately NOT reset alongside it: the inbox file is
            # DURABLE across incarnations (nothing recreates it at spawn — the register path seeds
            # the cursor to the CURRENT inbox size for exactly that reason), so zeroing it would
            # re-deliver the prior incarnation's mail. Recreated-file staleness is the test, and
            # only the turn-event log meets it.
            "turn_event_acked_offset": 0,
            "liveness_state": "claimed",
        },
        event="spawn_open",
        summary="STEP4: actor opened; record session_uuid + transcript_path + model_used + canonical "
                "tmux_target (claimed->spawning)",
    )
    if not step4.ok:
        released = release_claim(node_address, expected_owner_token=post_claim_token)
        _emit_spawn_failure_escalation(
            node_address, "runtime_down", spawn_result.model_used,
            released=bool(getattr(released, "ok", False)),
        )
        return _result_failed("runtime_down", tmux_target=node_address)

    # STEP5 — confirm boot: spawning -> running. The owner_token/epoch are unchanged across STEP4/5
    # (the claim minted them); the generation advanced by STEP4.
    step5 = executor.transition(
        node_address,
        expected_state="spawning",
        expected_generation=step4.binding["generation"],
        expected_owner_token=post_claim_token,
        target_state="running",
        binding_delta={},
        event="spawn_running",
        summary="STEP5: actor confirmed boot (spawning->running)",
    )
    if not step5.ok:
        # The actor opened but the running transition failed: still a post-claim failure -> rollback.
        # (release_claim CAS targets 'claimed'; the node is now 'spawning', so route the rollback
        # spawning->planned through the executor directly with the live token.)
        released = _rollback_spawning(node_address, post_claim_token)
        _emit_spawn_failure_escalation(
            node_address, "runtime_down", spawn_result.model_used,
            released=bool(getattr(released, "ok", False)),
        )
        return _result_failed("runtime_down", tmux_target=node_address)

    # STEP6 — the KICKOFF (the transport increment): deliver the agent's starting instruction.
    # Durable-artifact-FIRST then best-effort nudge (the architecture's own pattern): the kickoff
    # line lands in the node's .inbox.<seat>.jsonl (the multi-writer append log the ③-wake tails),
    # THEN the pointer is typed into the live pane. A lost keystroke is HEALED by the watchdog's
    # ③-wake: the inbox line sits unacked past the watermark, so the next poll re-nudges. Pointer,
    # never payload (the wake_keystroke discipline — the brief content stays in brief.md).
    # Best-effort by construction: a kickoff hiccup never rolls back a successfully RUNNING node.
    _deliver_kickoff(node_address, spawn_result, adapter)

    return spawn_result


# The kickoff prompt-gate bound (LT-6): how long STEP6 polls the freshly-opened pane for the
# idle input prompt before giving up on the IMMEDIATE nudge. CC cold-boots to its prompt in a
# few seconds; a pane that never shows it within the bound (e.g. an unexpected boot dialog —
# send-keys into a modal ANSWERS the modal, TRANSPORTS §3.3) gets NO blind keystroke: the
# durable inbox line + the ③-wake deliver the pointer once the gate opens. Module seats so a
# test (and commissioning) can tune them without a code change.
KICKOFF_GATE_DEADLINE_S: float = 30.0
KICKOFF_GATE_POLL_S: float = 0.5


def _kickoff_gate_open(pane_text: str) -> bool:
    """True iff the captured pane shows the IDLE input prompt and no working/dialog marker.

    The SAME two-part match as ``watchdog.prod_precondition`` (one source for the measured CC
    marker strings — FORK_PROMPT / _WORKING_MARKER / _DIALOG_MARKERS — so the two gates cannot
    drift), but fed by the ADAPTER's OWN tmux seam: the chokepoint may be driving a transport the
    global wrapper knows nothing about. Lazy import — watchdog imports chokepoint at module level.
    """
    from harnessd import watchdog as _watchdog

    if not pane_text:
        return False
    if _watchdog._WORKING_MARKER in pane_text:
        return False
    if any(marker in pane_text for marker in _watchdog._DIALOG_MARKERS):
        return False
    return any(marker in pane_text for marker in _watchdog.PROMPT_MARKERS)  # E4: '❯' or '›'


def _await_kickoff_gate(capture, target) -> bool:
    """Bounded poll (LT-6): capture the pane until the idle prompt shows or the deadline lapses."""
    import time as _time

    deadline = _time.monotonic() + KICKOFF_GATE_DEADLINE_S
    while True:
        try:
            pane = capture(target) or ""
        except Exception:  # noqa: BLE001 — an unreadable pane is gate-closed evidence
            pane = ""
        if _kickoff_gate_open(pane):
            return True
        if _time.monotonic() >= deadline:
            return False
        _time.sleep(KICKOFF_GATE_POLL_S)


def _journal_kickoff_event(node_address: str, event: str, inbox, target, note: str) -> None:
    """Best-effort kickoff-journal row — a skipped/failed nudge is visible, never silent.

    Routed through ``executor.journal`` (SWL-01): seq allocation + append under the EX lock.
    """
    try:
        executor.journal(
            node_address, event=event,
            from_state="running", to_state="running",
            binding_delta={"inbox": str(inbox), "tmux_target": target},
            summary=f"{event} for {node_address}: {note}",
        )
    except Exception:  # noqa: BLE001 — the journal itself is best-effort
        pass


def _deliver_kickoff(node_address: str, spawn_result, adapter) -> None:
    """STEP6: durable kickoff line -> prompt-gated pointer nudge -> verify-then-ack the line.

    (1) DURABLE FIRST — one JSON line in ``addressing.inbox_path`` (the same line discipline the
        F16 answer verb uses: from/type/message/ts). This is the artifact the ③-wake edge-trigger
        reads: as long as it sits past ``last_inbox_acked_offset``, the watchdog keeps re-nudging,
        so the kickoff cannot be lost to a dropped keystroke.
    (2) THE PROMPT GATE (LT-6) — the kickoff fires milliseconds after create_detached into a
        mid-boot pane, the ONE delivery that previously bypassed the prompt-string gate. When the
        adapter's transport can ``capture_pane``, STEP6 now polls (bounded —
        ``KICKOFF_GATE_DEADLINE_S``) until the pane shows the idle input prompt with no
        working/dialog marker; a gate that never opens SKIPS the immediate nudge (journaled
        ``boot_confirm_pending``) — send-keys into a boot dialog would CONFIRM the highlighted
        option (TRANSPORTS §3.3), while the durable line + ③-wake heal the skip for free. A
        transport without ``capture_pane`` (legacy dry-run mocks) keeps the ungated best-effort
        send.
    (3) THE NUDGE — ``tmux.send_keys(<canonical tmux_target>, notification)`` through the
        adapter's OWN tmux seam (the same transport that opened the pane; mocks without
        ``send_keys`` simply skip — the dry-run never types). The notification derives sender
        and count from the durable kickoff row, points at the seat-qualified inbox, and never
        carries the brief/task content itself.
    (4) VERIFY-THEN-ACK (LT-9 + R-1) — a kickoff whose send REPORTED delivery does NOT ack on
        rc=0 (rc=0 is not consumption — verify-new-turn, TRANSPORTS §3.2 P3: an operator's
        copy-mode attach eats the keystrokes while send-keys still exits 0). It records the
        pending verification instead (``executor.record_wake_pending``: the notification
        snapshot's covered offset + the transcript size at send time), spending wake attempt
        one for the kickoff row; the daemon's ③-wake resolver acks once
        the transcript grows (the agent's FIRST turn), terminating the heal loop — LT-9's
        no-spurious-re-nudge goal now holds only for kickoffs that were actually consumed, while
        a SWALLOWED kickoff keeps the watermark unmoved so the ③-wake re-nudges from the durable
        line (at worst one duplicate nudge in the consume-to-verify window — tolerated). A
        failed/skipped send records nothing — the wake delivers instead.
    """
    from harnessd import clock

    if ledger.RUNTIME_ROOT is None:
        return  # no runtime tree (a bare adapter-level dry-run) -> nothing durable to land

    seat = addressing.split_address(node_address)[1]
    binding = ledger.read_binding(node_address) or {}
    node_dir = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
    inbox = addressing.inbox_path(node_address, ledger.RUNTIME_ROOT)
    try:
        from harnessd import review_dispatch as _review_dispatch
    except Exception:  # noqa: BLE001 — kickoff must still land if import hiccups
        _review_dispatch = None

    if _review_dispatch is not None and _review_dispatch.is_review_check_binding(binding):
        report_path = binding.get("review_check_report") or "the assigned review-check report"
        packet = binding.get("gate_review_packet") or "the review packet named in your brief"
        gate_id = binding.get("gate_id") or "<gate-id>"
        gate_lead = binding.get("review_check_for") or "<gate-lead>"
        candidate = binding.get("review_check_candidate") or "<candidate>"
        pointer = (
            f"You are {node_address}, a review-check seat for gate {gate_id}. "
            f"Gate lead: {gate_lead}. Candidate: {candidate}. "
            "Startup order: first create or refresh your native task list with a few high-level "
            "steps for this single review check. If the task-list tool is deferred, first discover "
            "only that tool family, then create the initial list before file reads or workspace "
            f"inspection. Then read this workspace's .launch-packet.md, brief.md, current "
            f".inbox.{seat}.jsonl rows, and review packet {packet}. Write exactly the assigned "
            f"check report at {report_path}. "
            "Do not create or fill plan.md for this review-check seat unless a later inbox row "
            "explicitly asks for it. Do not write the final gate artifact, do not author sibling "
            "check reports, and do not render ACCEPT/BOUNCE/ESCALATE for the candidate. "
            "Do not use native Agent/Task/subagent sidechains for this gate; this harness seat is "
            "already the independent reviewer context. Sign DONE only after the assigned report exists."
        )
    elif seat == "review" and binding.get("gate_for"):
        producer_address = binding.get("gate_for")
        producer = ledger.read_binding(producer_address) or {}
        gate_dir = Path(producer.get("gate_review_dir") or (node_dir / "reviews"))
        packet = producer.get("gate_review_packet") or str(gate_dir / "review-packet.md")
        gate_id = producer.get("gate_id") or "<gate-id>"
        try:
            from harnessd import return_contract as _return_contract
            gate_artifact_path = _return_contract.gate_artifact_path(node_address, binding)
            gate_artifact_name = _return_contract.gate_artifact_name(binding)
        except Exception:  # noqa: BLE001 — kickoff must still land if artifact lookup hiccups
            gate_artifact_path = None
            gate_artifact_name = None
        if gate_artifact_path is None:
            gate_artifact_path = gate_dir / (gate_artifact_name or "gate-report.md")
        try:
            check_report_names = (
                _review_dispatch.required_check_report_names(binding)
                if _review_dispatch is not None
                else ()
            )
        except Exception:  # noqa: BLE001 — kickoff must still land if report lookup hiccups
            check_report_names = ()
        if check_report_names:
            configured_axes = _review_dispatch.configured_module_panel_axes(binding)
            if configured_axes is not None:
                roster_label = (
                    f"the configured {len(configured_axes)}-seat module panel "
                    f"({', '.join(configured_axes)})"
                )
            else:
                roster_label = (
                    "the five L2+ product-altitude axes"
                    if len(check_report_names) == 5
                    else "the four V1 axes"
                )
            configured_clause = (
                "This commissioned module panel supersedes the static four-axis default. "
                if configured_axes is not None
                else ""
            )
            check_report_clause = (
                "This is a higher-level review gate. Normal mode is FULL: write "
                "`review-plan.md` first with a plain `Review Mode: FULL` line and a "
                f"`## Role Selection` section naming {roster_label}. "
                f"{configured_clause}"
                "The harness daemon will dispatch independent "
                "review-check seats for these exact report files in "
                f"{gate_dir}: " + ", ".join(check_report_names) + ". "
                "Wait until every selected check has both its report file and matching "
                "current-gate child-completion inbox row before synthesis; report files alone are not "
                "completion evidence. "
                "If any selected check is missing its report or completion row, end the current turn with waiting status "
                "and let the harness wake this seat; do not hold the pane in a long foreground "
                "polling loop. "
                "Do not author these check reports yourself and do not use native "
                "Agent/Task/subagent sidechains for these gate reviewers. Use SHORT only when "
                "the exact short-review exception applies; missing reviewer substrate is not "
                "a SHORT reason. "
            )
        else:
            check_report_clause = (
                "This is a local L5+ review gate. Complete the review inside this reviewer "
                "seat: perform your independent local review, write `gate-report.md`, then "
                "sign the verdict. The daemon will not open auxiliary reviewer seats for this "
                "gate. "
            )
        pointer = (
            f"You are {node_address}, the review seat for {producer_address}. "
            "Startup order: first create or refresh your native task list with high-level "
            "review-management steps for this gate. If the task-list tool is deferred, first "
            "discover only that tool family, then create the initial list before file reads or "
            f"workspace inspection. Then start from .inbox.{seat}.jsonl in this workspace and "
            f"the review packet at {packet}. "
            f"Gate id: {gate_id}. First authored review file: {gate_dir / 'review-plan.md'}. "
            f"{check_report_clause}"
            f"Use the producer's node-root artifacts as evidence inputs; do not overwrite them. "
            f"Read {addressing.signoff_path(node_address, ledger.RUNTIME_ROOT)} and copy owner_token "
            "verbatim into your terminal signal. "
            f"Write the final gate artifact at {gate_artifact_path}, then sign off through "
            f"{addressing.signal_path(node_address, ledger.RUNTIME_ROOT)} with evidence.gate_id, "
            "evidence.producer_artifact, and evidence.gate_artifact pointing at the candidate and "
            "review-owned artifacts."
        )
    else:
        pointer = (
            f"You are {node_address} in workspace {node_dir}. "
            f"Startup order: first create or refresh your native task list with high-level "
            f"role-appropriate steps for orienting on the task, doing the assigned work, reporting, "
            f"and signing off. If the task-list tool is deferred, first discover only that tool "
            f"family, then create the initial list before file reads or workspace inspection. Then "
            f"read brief.md and current .inbox.{seat}.jsonl rows, do only bounded orientation "
            f"needed to make the plan concrete, refresh the native task list if needed, and fill or update the "
            f"preinstantiated plan.md form as the durable mirror before substantive work or child "
            f"spawning. When updating preinstantiated forms such as plan.md or report.md, first open "
            f"the form through the runtime's file-read tool so the editor has current file state. "
            f"plan.md needs a one-line goal plus a task checklist whose final three items "
            f"are: fill report.md, verify your requirement-ID citations, sign off. "
            f"Messages arrive in .inbox.{seat}.jsonl."
        )

    # (1) the durable kickoff line (multi-writer append log — NOT the single-writer ledger).
    # ``fh.tell()`` right after the write is THIS line's end-offset — the LT-9 ack target.
    line = {"from": "harnessd", "type": "kickoff", "message": pointer, "ts": clock.now_utc()}
    try:
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        _journal_kickoff_event(
            node_address, "kickoff_append_failed", inbox, getattr(spawn_result, "tmux_target", None),
            f"kickoff inbox append failed (inbox {inbox})",
        )
        return  # no durable line -> do not type a nudge the wake could never heal/repeat

    from harnessd import watchdog as _watchdog

    notification = _watchdog.unconsumed_inbox_notification(binding, binding)
    if not notification or not notification.get("should_wake"):
        return  # no complete unconsumed row -> no inbox notification
    if int(binding.get("wake_attempt_count") or 0) >= 3:
        return  # stable receipt is capped; watchdog health owns the next move

    send = getattr(getattr(adapter, "tmux", None), "send_keys", None)
    target = getattr(spawn_result, "tmux_target", None)
    if send is None or not target:
        return  # no keystroke transport (dry-run mock) -> the ③-wake delivers the durable line

    # (2) the prompt gate (LT-6) — only when the transport can capture the pane.
    capture = getattr(getattr(adapter, "tmux", None), "capture_pane", None)
    if capture is not None and not _await_kickoff_gate(capture, target):
        _journal_kickoff_event(
            node_address, "boot_confirm_pending", inbox, target,
            "pane never reached the idle prompt within the bound — immediate nudge SKIPPED "
            "(a keystroke into a boot dialog answers the dialog); the durable inbox line + the "
            "③-wake deliver the pointer once the gate opens",
        )
        return

    # (3) the nudge. A lost keystroke is healed by the ③-wake on the unacked line.
    try:
        delivered = send(target, _watchdog.wake_keystroke(binding, notification))
    except Exception:  # noqa: BLE001 — fire-and-forget; the unacked inbox line re-nudges
        return
    if delivered is False:
        # The transport surfaced a delivery failure (LT-2's send_keys contract) — watermark
        # unmoved, the wake retries; journaled so the lost nudge is visible.
        _journal_kickoff_event(
            node_address, "kickoff_send_failed", inbox, target,
            "send-keys exited non-zero — the ③-wake re-nudges from the durable line",
        )
        return

    # (4) LT-9 + R-1: a DELIVERED kickoff does NOT ack on rc=0 — record the pending verification
    # through the single writer (the kickoff line's own end-offset + the transcript baseline at
    # send time); the daemon's ③-wake resolver acks once the transcript grows (the agent's first
    # turn). Best-effort: a failed record only costs redundant wakes, the pre-LT-9 steady state.
    transcript_path = getattr(spawn_result, "transcript_path", None)
    try:
        baseline = os.stat(transcript_path).st_size if transcript_path else 0
    except OSError:
        baseline = 0  # absent at send time — any later content reads as the first turn
    try:
        executor.record_wake_pending(
            node_address,
            pending_ack_offset=int(notification["covered_offset"]),
            sent_transcript_size=baseline,
        )
    except Exception:  # noqa: BLE001
        pass


def _rollback_spawning(node_address: str, expected_owner_token: str):
    """Roll a ``spawning`` node back to ``planned`` (the §6.1 spawning->planned rollback edge).

    RETURNS the ``TransitionResult`` (RR-7, mirroring release_claim) so the §6.3 escalation row
    records the true rollback outcome; None when the node is absent / not ``spawning``.
    """
    live = ledger.read_binding(node_address)
    if live is None or live.get("state") != "spawning":
        return None
    new_lease_epoch = fencing.advance_epoch(live)
    new_owner_token = fencing.mint_owner_token(
        node_address,
        live.get("subagent_id"),
        live.get("session_uuid"),
        new_lease_epoch,
    )
    return executor.transition(
        node_address,
        expected_state="spawning",
        expected_generation=live["generation"],
        expected_owner_token=expected_owner_token,
        target_state="planned",
        binding_delta={
            "state": "planned",
            "lease_epoch": new_lease_epoch,
            "owner_token": new_owner_token,
        },
        new_lease_epoch=new_lease_epoch,
        new_owner_token=new_owner_token,
        event="release_claim",
        summary="actor-open-confirm failed: spawning->planned rollback, bump epoch (§6.1)",
    )


def _spawn_env() -> dict:
    """The 4-var isolation env the adapter opens the pane with (DAEMON §6.2).

    The chokepoint orchestrates; the env is the OAuth-only isolation set the adapter expects.
    The concrete credential values are resolved by the daemon at boot and BOUND through
    ``set_spawn_env`` (LT-1: ``daemon.boot`` threads ``runtime.config.env`` — commissioning's
    live token + pinned CLAUDE_CONFIG_DIR — into this seam, so every production spawn boots the
    pane with the SAME env that passed the genesis credential precondition). When nothing is
    bound (the dry-run/structural tests), this falls back to the 4-var placeholder shape (no raw
    API key, never a --resume token) so the gate firewall's no-resume scan stays clean — and the
    REAL transport refuses to launch the token sentinel (tmux.create_detached, fail-loud).
    """
    if SPAWN_ENV is not None:
        return dict(SPAWN_ENV)
    return {
        "CLAUDE_CONFIG_DIR": PLACEHOLDER_CONFIG_DIR,
        "CLAUDE_CODE_OAUTH_TOKEN": PLACEHOLDER_OAUTH_TOKEN,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        # LR-2: PATH joined the floor (commissioning mirrors this; PATH is not a credential).
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
    }


# ---------------------------------------------------------------------------
# PRODUCTION BLINDERS + legacy structural seam. Production assemblers always stamp
# ``level_config.blinders_mode`` (observe/enforce, never off). The undeclared
# ``level_config.containment`` flag remains only for historical structural tests and maps to
# enforce. A config with neither flag is therefore a neutral test/dry-run seam, not production.
# ---------------------------------------------------------------------------

def _containment_requested(level_config) -> bool:
    """True for a production blinders mode or the historical containment test flag."""
    return bool(
        getattr(level_config, "blinders_mode", None)
        or getattr(level_config, "containment", False)
    )


def _blinders_mode(level_config) -> str:
    """Production mode or the legacy containment test seam's historical enforcing posture."""
    mode = getattr(level_config, "blinders_mode", None)
    if mode:
        return str(mode)
    if getattr(level_config, "containment", False):
        return blinders.ENFORCE
    raise ValueError("containment requested without a blinders mode")


def _resolve_containment_config_dir(spawn_env: dict, *, runtime: str, workroot: str) -> str:
    """Resolve the CONFIG dir handed to ``sandbox.resolve_containment`` (CC's own state-write root).

    FORK-CONTAINMENT-CONFIG (spec-faithful, STATED): §2.5c lists CONFIG (CLAUDE_CONFIG_DIR) as a
    write-allow root; the concrete on-disk dir is a deployment value the daemon binds at boot. v1
    reads it from the ``HARNESS_CC_CONFIG_DIR`` env override when present (the seam the production
    daemon/eval-spawn sets), else falls back to ``<RUNTIME_ROOT>/cc-config``. Either lands a REAL
    on-disk writable dir the adapter's ``_write_profile`` can write the rendered ``.sb`` into.
    """
    if runtime == "claude-code":
        config_dir = spawn_env.get("CLAUDE_CONFIG_DIR")
        if config_dir and config_dir != PLACEHOLDER_CONFIG_DIR:
            return str(config_dir)
        override = os.environ.get("HARNESS_CC_CONFIG_DIR")
        if override:
            return override
        return os.path.join(str(ledger.RUNTIME_ROOT), "cc-config")
    # Codex mints its exact worker home inside the adapter.  This placeholder is replaced before
    # profile rendering and never becomes a child-readable canonical auth home.
    return os.path.join(workroot, ".codex-runtime")


def _git_metadata(workspace: str, *, env: dict) -> tuple[str | None, str | None]:
    """Derive exact git internals without consulting the user's git configuration."""
    xcode_git = "/Applications/Xcode.app/Contents/Developer/usr/bin/git"
    git = (
        os.environ.get("HARNESSD_GIT")
        or (xcode_git if os.path.isfile(xcode_git) else None)
        or shutil.which("git")
    )
    if not git:
        return None, None
    probe_env = dict(os.environ)
    probe_env.update(env)
    probe_env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_CONFIG_HOME": os.path.join(workspace, ".config"),
        }
    )

    def _read(flag: str) -> str | None:
        try:
            result = subprocess.run(
                [git, "-C", workspace, "rev-parse", flag],
                capture_output=True,
                text=True,
                timeout=10,
                env=probe_env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        value = result.stdout.strip().splitlines()[-1]
        return os.path.abspath(os.path.join(workspace, value)) if not os.path.isabs(value) else value

    return _read("--absolute-git-dir"), _read("--git-common-dir")


def _produce_containment(node_address: str, level_config, spawn_brief: dict, env: dict):
    """Derive and attach the physical policy; merge the cache/environment write-jail floor.

    Returns the fully materialized dict brief with its resolved containment block and the cache,
    temp, isolated-git environment. Production always calls this; only neutral structural tests
    can leave the brief/env untouched.

    HOME resolution (FORK-CONTAINMENT-HOME, STATED): §2.5a anchors the secret-deny set on the user
    HOME; v1 lets ``sandbox.resolve_containment`` default HOME to ``os.path.expanduser("~")`` (its
    own documented default — the secret-deny anchor), so the chokepoint passes ``home=None``. A
    per-deployment HOME override is a deferred refinement.
    """
    runtime = str(getattr(level_config, "runtime", "claude-code"))
    workroot = str(addressing.node_dir(node_address, ledger.RUNTIME_ROOT))
    tmpdir = sandbox.runtime_scratch_dir(workroot)
    xdg_config = os.path.join(workroot, ".config")
    Path(tmpdir).mkdir(parents=True, exist_ok=True)
    Path(xdg_config).mkdir(parents=True, exist_ok=True)
    git_dir, git_common_dir = _git_metadata(workroot, env=env)
    live = ledger.read_binding(node_address) or {}
    exact_inputs = live.get("semantic_exact_read_paths")
    product_probe_inputs = live.get("product_probe_exact_read_paths")
    if live.get("semantic_cell_role") and isinstance(exact_inputs, list):
        harness_root = Path(__file__).resolve().parents[2]
        harness_documents = [
            str(harness_root / value)
            for value in [
                *(spawn_brief.get("load_manifest") or []),
                spawn_brief.get("system_prompt_file"),
            ]
            if isinstance(value, str) and value.strip()
        ]
        policy = blinders.derive_exact_policy(
            node_address=node_address,
            runtime_root=str(ledger.RUNTIME_ROOT),
            workspace=workroot,
            exact_documents=[
                *[str(value) for value in exact_inputs],
                *harness_documents,
            ],
            runtime=runtime,
            harness_root=str(harness_root),
        )
    elif live.get("product_probe_role") and isinstance(product_probe_inputs, list):
        harness_root = Path(__file__).resolve().parents[2]
        harness_documents = [
            str(harness_root / value)
            for value in [
                *(spawn_brief.get("load_manifest") or []),
                spawn_brief.get("system_prompt_file"),
            ]
            if isinstance(value, str) and value.strip()
        ]
        policy = blinders.derive_exact_policy(
            node_address=node_address,
            runtime_root=str(ledger.RUNTIME_ROOT),
            workspace=workroot,
            exact_documents=[
                *[str(value) for value in product_probe_inputs],
                *harness_documents,
            ],
            runtime=runtime,
            harness_root=str(harness_root),
        )
    else:
        policy = blinders.derive_policy(
            node_address=node_address,
            runtime_root=str(ledger.RUNTIME_ROOT),
            bindings=ledger.all_nodes(),
            mode=_blinders_mode(level_config),
            workspace=workroot,
            load_manifest=spawn_brief.get("load_manifest") or [],
            system_prompt_file=spawn_brief.get("system_prompt_file"),
            spec_pointer=spawn_brief.get("spec_pointer"),
            frozen_acceptance_ref=spawn_brief.get("frozen_acceptance_ref"),
            launch_packet_file=spawn_brief.get("launch_packet_file"),
            reference_map_file=spawn_brief.get("reference_map_file"),
            reference_map_json_file=spawn_brief.get("reference_map_json_file"),
            runtime=runtime,
            harness_root=str(Path(__file__).resolve().parents[2]),
            git_dir=git_dir,
            git_common_dir=git_common_dir,
            role_variant=spawn_brief.get("role_variant"),
        )
    block = sandbox.resolve_containment(
        node_address,
        runtime_root=ledger.RUNTIME_ROOT,
        config_dir=_resolve_containment_config_dir(
            env,
            runtime=runtime,
            workroot=workroot,
        ),
        home=None,
        read_policy=policy,
        runtime=runtime,
    )
    enriched = dict(spawn_brief)
    enriched["containment_profile"] = block
    merged_env = dict(env)
    merged_env.update(sandbox.cache_redirect_env(block["WORKROOT"]))
    merged_env.update(
        {
            "TMPDIR": tmpdir,
            "CLAUDE_CODE_TMPDIR": tmpdir,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_CONFIG_HOME": xdg_config,
        }
    )
    xcode_bin = "/Applications/Xcode.app/Contents/Developer/usr/bin"
    if os.path.isdir(xcode_bin):
        current_path = merged_env.get("PATH", "")
        merged_env["PATH"] = (
            f"{xcode_bin}:{current_path}" if current_path else xcode_bin
        )
    return enriched, merged_env


# ---------------------------------------------------------------------------
# claim_and_spawn — STEP0-5 (the F-024 claim-before-spawn path).
# ---------------------------------------------------------------------------

def claim_and_spawn(
    node_address: str,
    *,
    expected_state: str,
    expected_generation: int,
    expected_owner_token: Optional[str],
    level_config,
) -> SpawnResult:
    """THE spawn path (§6.1 STEP0-5). Claim STRICTLY before the actor (F-024).

    STEP0 pause-subtree read-point (abort BEFORE claiming if the subtree is paused);
    STEP1 CAS-claim into ``claimed`` (a lost claim -> ClaimLost, NO actor opened);
    STEP1.5 receipt every handed contract and co-record owner version rows;
    STEP2 assemble the runtime-neutral brief + load-manifest;
    STEP3 adapter.pin_and_open (the actor opens — STRICTLY after the committed claim);
    STEP4 record session_uuid + NON-NULL transcript_path + actual model_used (claimed->spawning);
    STEP5 spawning->running.
    On ANY post-claim failure STEP2-5: release_claim (claimed->planned, bump epoch) + L1 escalation.
    """
    # STEP0 — a paused subtree admits no child: ABORT BEFORE claiming (no claim, no actor).
    if subtree_paused(node_address):
        return _result_failed("paused_subtree", tmux_target=node_address)
    live = ledger.read_binding(node_address)
    if respawn_parked(live):
        return _result_failed("respawn_parked", tmux_target=node_address)

    # STEP1 — the CAS-claim (a REAL transition into ``claimed`` under the REAL EX lock). A lost claim
    # (wrong CAS precondition) returns ClaimLost with the binding UNCHANGED and NO actor opened.
    claim_result = executor.claim(
        node_address,
        expected_state=expected_state,
        expected_generation=expected_generation,
        expected_owner_token=expected_owner_token,
        level_config=level_config,
    )
    if not claim_result.ok:
        # F-024: the slot was claimed by someone else (or the CAS precondition missed) -> NO actor
        # opened (we have not reached the adapter), the binding is unchanged. ClaimLost.
        return _result_failed("claim_lost", tmux_target=node_address)

    # STEP1.5 — every notary-stamped contract handed to this incarnation is receipted after the
    # claim is known to belong to this caller and before the claim can open an actor. This central
    # placement covers ordinary children, fresh reviewers, review-check seats, and recovery spawns
    # without changing the ClaimLost contract for stale CAS attempts.
    receipt_result = _install_spawn_contract_receipts(
        node_address,
        expected_state="claimed",
        expected_generation=claim_result.binding["generation"],
        expected_owner_token=claim_result.binding.get("owner_token"),
    )
    if receipt_result is None or not receipt_result.ok:
        released = release_claim(
            node_address,
            expected_owner_token=claim_result.binding.get("owner_token"),
        )
        _emit_spawn_failure_escalation(
            node_address,
            "contract_receipt_failed: handed contracts could not be receipted before actor open",
            "",
            released=bool(getattr(released, "ok", False)),
        )
        return _result_failed("contract_receipt_failed", tmux_target=node_address)
    claimed_binding = receipt_result.binding

    # STEP2 — assemble the runtime-neutral brief + the role_variant-selected load-manifest.
    work_node = _work_node_for(node_address, claimed_binding)
    neutral = brief.assemble_neutral(node_address, level_config, work_node)

    # STEP2.5 — THE PIECES-PRESENT GATE (E1, enforcement spine): an under-equipped actor NEVER
    # opens. Deterministic, no model (Inc-18 semantics run at the runtime spawn path, not just in
    # the offline eval): manifest non-empty + every doc resolves + decision-complete brief
    # (spec_pointer; frozen acceptance for executor seats) + the correct per-seat bundle. On fail:
    # the SAME §6.3 rollback as a STEP3 failure — release the claim + a spawn-failure escalation
    # NAMING the missing piece. agent-lifecycle L13 ("you never bootstrap yourself") made mechanical.
    gate_failure = _pieces_gate(node_address, level_config, work_node, claimed_binding)
    if gate_failure is not None:
        return gate_failure

    spawn_brief = _brief_payload(neutral)

    # STEP3-5 — open the actor, record facts, reach running (rollback on any failure).
    return _spawn_after_claim(
        node_address, claimed_binding, level_config, spawn_brief, None
    )


def _install_spawn_contract_receipts(
    node_address: str,
    *,
    expected_state: str,
    expected_generation: int,
    expected_owner_token: Optional[str],
) -> executor.TransitionResult:
    """Co-commit holder receipts and owner version-1 rows before actor open."""
    binding = ledger.read_binding(node_address)
    if binding is None:
        return executor.TransitionResult(
            ok=False,
            errors=[f"no binding for spawn receipt holder {node_address!r}"],
            warnings=[],
            binding=None,
        )
    if (
        binding.get("state") != expected_state
        or binding.get("generation") != expected_generation
        or (
            expected_owner_token is not None
            and binding.get("owner_token") != expected_owner_token
        )
    ):
        return executor.TransitionResult(
            ok=False,
            errors=[f"spawn receipt precondition changed for {node_address!r}"],
            warnings=[],
            binding=binding,
        )
    owner_address = binding.get("parent_address")
    if not owner_address:
        return executor.TransitionResult(
            ok=True,
            errors=[],
            warnings=[],
            binding=binding,
        )
    owner = ledger.read_binding(owner_address)
    if owner is None:
        # Compatibility for the pre-registered/direct-claim seam used by recovery and old
        # fixtures: it may hold only the child slice.  Production parent-spawn registration
        # always has the owner and therefore always installs receipts before actor open.
        return executor.TransitionResult(
            ok=True,
            errors=[],
            warnings=[],
            binding=binding,
        )

    receipts = contracts.merge_receipts(binding.get("contract_receipts"))
    preexisting_receipt_paths = set(receipts)
    candidates: list[tuple[Path, dict]] = []
    spec_pointer = binding.get("spec_pointer")
    if spec_pointer:
        spec_path = Path(str(spec_pointer))
        stamped = notary.stamp(spec_path)
        if stamped.get("present") is True:
            candidates.append((spec_path, stamped))
    acceptance_ref = binding.get("frozen_acceptance_ref")
    if acceptance_ref and binding.get("child_purpose") != CHILD_PURPOSE_TEST_AUTHOR:
        acceptance_path = Path(str(acceptance_ref))
        stamped = notary.stamp(acceptance_path)
        if stamped.get("present") is True:
            candidates.append((acceptance_path, stamped))

    for artifact, stamped in candidates:
        receipt = contracts.contract_receipt(
            node_address,
            owner_address,
            artifact,
            stamped,
        )
        receipts = contracts.merge_receipts(receipts, receipt)

    versions = copy.deepcopy(owner.get("contract_versions") or {})
    for receipt in receipts.values():
        if receipt.get("owner_address") != owner_address:
            continue
        path = contracts.canonical_contract_path(receipt["artifact"])
        existing = versions.get(path)
        fingerprint = contracts.receipt_fingerprint(receipt)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                if path in preexisting_receipt_paths:
                    return executor.TransitionResult(
                        ok=False,
                        errors=[
                            f"contract {path} changed from committed fingerprint "
                            f"{existing.get('fingerprint')} to {fingerprint}; owner revision required"
                        ],
                        warnings=[],
                        binding=binding,
                    )
                # A terminal address reused for a new task gets a new brief/acceptance contract
                # at that address-local home.  The fresh binding intentionally carries no prior
                # receipt for those spawn-authored surfaces, so this is version 1 of a new
                # incarnation contract, not an amendment of the completed task.
                versions[path] = contracts.version_entry(
                    owner_address=owner_address,
                    artifact=path,
                    stamped=receipt["stamp"],
                )
            continue
        versions[path] = contracts.version_entry(
            owner_address=owner_address,
            artifact=path,
            stamped=receipt["stamp"],
        )

    if (
        receipts == (binding.get("contract_receipts") or {})
        and versions == (owner.get("contract_versions") or {})
    ):
        return executor.TransitionResult(
            ok=True,
            errors=[],
            warnings=[],
            binding=binding,
        )
    return executor.record_related_updates(
        node_address,
        primary_delta={"contract_receipts": receipts},
        related_deltas={owner_address: {"contract_versions": versions}},
        expected_owner_tokens={
            node_address: expected_owner_token,
            owner_address: owner.get("owner_token"),
        },
        event="spawn_contracts_receipted",
        summary=f"contract versions handed to {node_address} were receipted before actor open",
        artifacts=list(receipts),
    )


def _pieces_gate(node_address: str, level_config, work_node: dict, claimed_binding: dict):
    """STEP2.5 — run the pieces-present check post-claim/pre-actor; roll back + escalate on fail.

    Returns None on pass, or the not-ok SpawnResult the caller returns verbatim. The rollback is
    EXACTLY the §6.3 post-claim contract (release_claim + spawn-failure escalation), with
    failure_class ``pieces_missing`` and the check's fail_message (which NAMES the missing piece)
    riding the escalation detail. The check itself is best-effort-contained: an unexpected checker
    error refuses the spawn too (fail-loud, not fail-open — an unverifiable brief must not boot).
    """
    from harnessd import pieces_present

    try:
        verdict = pieces_present.check_boundary(node_address, level_config, work_node)
        ok = bool(verdict)
        message = getattr(verdict, "fail_message", "") or ""
    except Exception as exc:  # noqa: BLE001 — fail-loud: an unverifiable brief never boots
        ok = False
        message = f"pieces-present check errored: {type(exc).__name__}: {exc}"
    if ok:
        return None
    post_claim_token = claimed_binding.get("owner_token")
    released = release_claim(node_address, expected_owner_token=post_claim_token)
    _emit_spawn_failure_escalation(
        node_address, f"pieces_missing: {message}"[:500], "",
        released=bool(getattr(released, "ok", False)),
    )
    return _result_failed("pieces_missing", tmux_target=node_address)


def _work_node_for(node_address: str, binding: dict) -> dict:
    """Assemble the durable work-node pointer the brief is built against (read off the binding).

    v1 derives the pointers from the binding's recorded fields where present. ABSENT pointers are
    DERIVED from the PREPARED NODE on disk (E1, agent-lifecycle §How-You-Spawn-a-Child: "the daemon
    derives its spec/acceptance pointers from the node you prepared"): ``brief.md`` in the node dir
    yields ``spec_pointer``; ``acceptance.md`` yields ``frozen_acceptance_ref``. This is what makes
    the E1 pieces-present gate satisfiable by the REAL preparation flow (parent authors the node,
    drops the outbox request) without any new binding fields — and what makes an UNPREPARED node
    (no brief anywhere) fail the gate loudly instead of booting an under-equipped agent.
    """
    binding = binding or {}
    spec_pointer = binding.get("spec_pointer")
    acceptance_ref = binding.get("frozen_acceptance_ref")
    if (not spec_pointer or not acceptance_ref) and ledger.RUNTIME_ROOT is not None:
        try:
            node_dir = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
            if not spec_pointer:
                brief_md = node_dir / "brief.md"
                if brief_md.is_file():
                    spec_pointer = str(brief_md)
            if not acceptance_ref:
                acceptance_md = node_dir / "acceptance.md"
                if acceptance_md.is_file():
                    acceptance_ref = str(acceptance_md)
        except (OSError, ValueError):
            pass  # derivation is best-effort; the gate decides on what is actually present
    return {
        "node_address": node_address,
        "workspace": binding.get("workspace"),
        "spec_pointer": spec_pointer,
        "frozen_acceptance_ref": acceptance_ref,
        "status_md": binding.get("status_md"),
        "log_md": binding.get("log_md"),
        "report_md": binding.get("report_md"),
    }


def _brief_payload(neutral) -> dict:
    """Flatten the NeutralContract into the dict the adapter's pin_and_open consumes.

    Carries the load-manifest (role-as-documents) + identity/acceptance/spec — and CRUCIALLY no
    ``--resume`` / session-continuation token (the gate-firewall scan asserts no fresh spawn ever
    carries one). role_variant rides the brief so the adapter selects the right manifest.
    """
    return {
        "node_address": neutral.node_address,
        "role_variant": neutral.role_variant,
        "level": neutral.level,
        "system_prompt_file": neutral.system_prompt_file,
        "load_manifest": list(neutral.load_manifest),
        "spec_pointer": neutral.spec_pointer,
        "frozen_acceptance_ref": neutral.frozen_acceptance_ref,
        "workspace": neutral.workspace,
        "reporting": neutral.reporting,
        # JAIL WIRING: carry the resolved §2.5a containment block onto the adapter's dict brief so the
        # adapter's tolerant ``_resolve_containment`` jails the pane. None on the structural path.
        "containment_profile": neutral.containment_profile,
    }


# ---------------------------------------------------------------------------
# register_and_spawn_child — THE ONE parent-spawns-child path (the supervision-tree spawn).
#
# A PARENT (e.g. an L2) creates + briefs + spawns its CHILD (e.g. an L3). genesis registers the
# parentless L1 ROOT (_register_l1_root, parent_address=null); this is the GENERAL case the cascade
# below L1 needs: a live parent registers its child with parent_address SET, writes the brief into
# the child node, then hands it to the EXISTING claim_and_spawn (F-024 preserved). STEPS:
#   (1) PRECONDITION — the parent binding exists + is LIVE (non-terminal). Only a live parent spawns;
#       a dead/absent parent is REFUSED BEFORE any child register (no half-registered orphan slot).
#   (2) REGISTER — the child as a fresh planned slot under the parent (mirror _register_l1_root but
#       parent_address SET): generation=0, lease_epoch=1, a minted owner_token, written via the
#       single-writer lock-held ledger path. SAFE if the child already exists (does NOT clobber a
#       live/non-planned child — that lets the claim lose against it, single-owner preserved).
#   (3) BRIEF — DERIVATION by default (FORK-BRIEF-DERIVATION): the child binding carries
#       spec_pointer -> <node>/brief.md and frozen_acceptance_ref -> <node>/acceptance.md, both
#       PRE-AUTHORED by the parent into the child node at plan time (pointer-not-payload,
#       WORKSPACE-SCHEMA:221). A pre-authored brief.md is left intact; a brief_content OVERRIDE writes
#       it (the exception); neither present -> a manifest stub. The load-manifest also rides the
#       neutral contract to the adapter.
#   (4) SPAWN — the EXISTING chokepoint.claim_and_spawn(child, expected_state=planned, …) — the
#       F-024 claim-before-spawn (a lost claim opens NO actor; on a post-claim failure the claim is
#       released exactly as today).
#
# NODE LANDING (FORK-NODE-NESTING, revises FORK-BRIEF-LANDING): the node dir is NESTED by path
#   (``addressing.node_dir`` = ``<RUNTIME_ROOT>/nodes/<address-path>/``, the #seat stripped, the '/'
#   nesting KEPT), so a child's dir sits UNDER its parent's and the parent's WORKROOT-subtree write-jail
#   can seed brief.md/acceptance.md into it (ARCHITECTURE.md:122). The canonical files are lowercase
#   ``brief.md`` + ``acceptance.md``. The earlier flat ``'/'/'#' -> '__'`` collapse broke the nesting.
#
#   * FORK-CHILD-SUBTREE — child_address must be UNDER the parent subtree (the address-prefix edge).
#     v1 checks ``child_address.startswith(parent_address)`` defensively but does NOT hard-refuse a
#     non-prefixed child (the load-bearing supervision edge is parent_address recorded on the child
#     binding, which is set authoritatively from parent_address here regardless of the string shape;
#     an address-prefix can be spoofed by sibling naming — the recorded edge is authoritative, the
#     same reasoning as ancestors_inclusive's FORK-ANCESTORS).
#
#   * FORK-PARENT-TOKEN — ``expected_parent_owner_token`` authorizes spawning a child under a parent.
#     Two gates: (1) the parent's LIVENESS (non-terminal) is always required; (2) the token, WHEN
#     PRESENTED (non-None), is now a HARD fence — a mismatch is REFUSED before any child register
#     (STEP 1a returns ``_result_failed("parent_fence")``), so a caller can only spawn under a parent
#     it owns. A None token is the daemon-internal/genesis-style unfenced path (the EX lock + local IPC
#     are the bound). It is NEVER the child's claim precondition — the child claim uses the CHILD's
#     freshly-minted registered owner_token (the §6.1 claim CAS).
# ---------------------------------------------------------------------------

def _parent_is_live(parent_binding: Optional[dict]) -> bool:
    """True iff the parent binding exists AND is non-terminal (only a LIVE parent spawns a child)."""
    if parent_binding is None:
        return False
    return not states.is_terminal(parent_binding.get("state"))


def _sanitize_address(node_address: str) -> str:
    """Map a node address to a single filesystem-safe node-dir name ('/' and '#' -> '__')."""
    return node_address.replace("/", "__").replace("#", "__")


def _child_node_dir(node_address: str) -> Path:
    """The child node workspace dir — NESTED by path (``addressing.node_dir``), so a child's dir sits
    UNDER its parent's and the parent's WORKROOT-subtree write-jail can seed it (ARCHITECTURE.md:122)."""
    return addressing.node_dir(node_address, ledger.RUNTIME_ROOT)


FROZEN_INPUT_FILES: tuple[str, ...] = ("brief.md", "acceptance.md")
_PENDING_INTENT_ROW = re.compile(
    r"^.*\bR-\d+(?:\.\d+)*\b.*\bpending\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_REFLECT_BACK_SECTION = re.compile(
    r"(?ims)^[ \t]{0,3}#{1,6}[ \t]+(?:\d+\.[ \t]+)?"
    r"reflect-back[ \t]+script(?:[ \t]*\+[ \t]*confirmation[ \t]+status)?[ \t]*$"
    r"(?P<body>.*?)(?=^[ \t]{0,3}#{1,6}[ \t]+|\Z)"
)
_REFLECT_BACK_CONFIRMED = re.compile(
    r"(?im)^[ \t]*(?:[-*][ \t]+)?status[ \t]*:[ \t]*confirmed[ \t]*$"
)
_REFLECT_BACK_AUTHORITY = re.compile(
    r"(?im)^[ \t]*(?:[-*][ \t]+)?confirmed[ \t]+by[ \t]*:[ \t]*\S.*$"
)
_REFLECT_BACK_DATE = re.compile(
    r"(?im)^[ \t]*(?:[-*][ \t]+)?date[ \t]*:[ \t]*"
    r"(?P<date>\d{4}-\d{2}-\d{2})[ \t]*$"
)


def _has_answered_reflect_back_record(text: str) -> bool:
    """Whether intent records the answered reflect-back, not merely confirmed row labels."""
    section = _REFLECT_BACK_SECTION.search(text)
    if section is None:
        return False
    body = section.group("body")
    date_match = _REFLECT_BACK_DATE.search(body)
    if (
        _REFLECT_BACK_CONFIRMED.search(body) is None
        or _REFLECT_BACK_AUTHORITY.search(body) is None
        or date_match is None
    ):
        return False
    try:
        calendar_date.fromisoformat(date_match.group("date"))
    except ValueError:
        return False
    return True


def _file_stamp(path: Path) -> dict:
    return notary.stamp(path)


def prepare_intent_spec_for_spawn(
    parent_address: str,
    child_address: str,
    *,
    child_level: Optional[str],
) -> tuple[Optional[dict], Optional[tuple[str, str]]]:
    """Freeze the confirmed project intent before the L1 -> L2 actor boundary.

    L1 writes ``client-brief/intent-spec.md`` only after the user reflect-back. That
    materialized file is therefore the existing confirmation boundary: no new verb/event is
    invented. Missing/unconfirmed input refuses loudly through the same pre-spawn blocker
    shape used by accepted-test-package checks.
    """

    parent = ledger.read_binding(parent_address) or {}
    if parent.get("level") != "L1" or child_level != "L2":
        return None, None

    spec = _child_node_dir(child_address) / "client-brief" / "intent-spec.md"
    try:
        text = spec.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, (
            "intent_spec_missing",
            (
                f"required project-genesis piece missing or unreadable: {spec}; "
                "L1 must write the confirmed client-brief/intent-spec.md before spawning L2"
            ),
        )

    pending = _PENDING_INTENT_ROW.findall(text)
    if pending:
        return None, (
            "intent_spec_unconfirmed",
            (
                f"required project-genesis piece is not confirmed: {spec} carries "
                f"{len(pending)} load-bearing R-* row(s) still pending reflect-back; "
                "L1 must confirm the client brief before spawning L2"
            ),
        )
    if not _has_answered_reflect_back_record(text):
        return None, (
            "intent_spec_confirmation_missing",
            (
                "intent spec has no answered reflect-back confirmation record: "
                f"{spec}; the canonical Reflect-back script must contain "
                "status: confirmed, confirmed by: <answering authority>, and "
                "date: YYYY-MM-DD. L1 must ask the owner, receive the answer, "
                "and record that answer before spawning L2"
            ),
        )

    existing = ledger.read_binding(child_address) or {}
    prior_receipt = existing.get("intent_spec_receipt")
    if isinstance(prior_receipt, dict):
        if not notary.check(prior_receipt, target=spec):
            return None, (
                "intent_spec_revision_required",
                (
                    f"frozen intent-spec drifted at {spec}; ordinary spawn cannot re-stamp "
                    "changed intent — an explicit intent-revision record is required, and its "
                    "schema lands with the receipts/amendment pass"
                ),
            )
        notary.stamp(spec, read_only=True)
        return prior_receipt, None

    stamped = notary.stamp(spec, read_only=True)
    if stamped.get("present") is not True:
        return None, (
            "intent_spec_missing",
            (
                f"required project-genesis piece became unreadable while freezing: {spec}; "
                "L2 was not spawned"
            ),
        )
    return contracts.contract_receipt(
        child_address,
        parent_address,
        spec,
        stamped,
    ), None


def prepare_client_brief_for_spawn(
    parent_address: str,
    child_address: str,
    *,
    child_level: Optional[str],
) -> tuple[dict[str, dict], Optional[tuple[str, str]]]:
    """Freeze the verbatim raw request beside intent at the same L1→L2 boundary."""
    parent = ledger.read_binding(parent_address) or {}
    if parent.get("level") != "L1" or child_level != "L2":
        return {}, None
    raw_request = (
        _child_node_dir(child_address) / "client-brief" / "raw-request.md"
    )
    try:
        raw_request.read_bytes()
    except OSError:
        return {}, (
            "raw_request_missing",
            (
                f"required project-genesis piece missing or unreadable: {raw_request}; "
                "L1 must write the verbatim client-brief/raw-request.md beside "
                "client-brief/intent-spec.md before spawning L2"
            ),
        )

    intent_receipt, intent_block = prepare_intent_spec_for_spawn(
        parent_address,
        child_address,
        child_level=child_level,
    )
    if intent_block is not None:
        return {}, intent_block
    existing = ledger.read_binding(child_address) or {}
    prior_raw = existing.get("raw_request_receipt")
    if isinstance(prior_raw, dict):
        if not notary.check(prior_raw, target=raw_request):
            return {}, (
                "raw_request_revision_required",
                (
                    f"frozen raw request drifted at {raw_request}; ordinary spawn cannot "
                    "re-stamp changed intake evidence — an explicit owner-ratified revision "
                    "record is required"
                ),
            )
        notary.stamp(raw_request, read_only=True)
        raw_receipt = prior_raw
    else:
        stamped = notary.stamp(raw_request, read_only=True)
        if stamped.get("present") is not True:
            return {}, (
                "raw_request_missing",
                (
                    f"required project-genesis piece became unreadable while freezing: "
                    f"{raw_request}; L2 was not spawned"
                ),
            )
        raw_receipt = contracts.contract_receipt(
            child_address,
            parent_address,
            raw_request,
            stamped,
        )
    return {
        "intent_spec_receipt": intent_receipt,
        "raw_request_receipt": raw_receipt,
    }, None


def revise_intent_spec(
    l1_address: str,
    *,
    target_address: str,
    candidate_ref: str,
    reason: str,
    expected_owner_token: str,
) -> executor.TransitionResult:
    """Fenced L1-only amendment channel for a confirmed intent-spec."""
    l1 = ledger.read_binding(l1_address)
    if l1 is None:
        return executor.TransitionResult(
            ok=False,
            errors=[f"intent revision owner {l1_address!r} is absent"],
            warnings=[],
            binding=None,
        )
    if l1.get("level") != "L1":
        return executor.TransitionResult(
            ok=False,
            errors=[f"intent revision owner must be L1, got {l1.get('level')!r}"],
            warnings=[],
            binding=l1,
        )
    if l1.get("owner_token") != expected_owner_token:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"intent revision fencing abort: presented {expected_owner_token!r} "
                f"!= live {l1.get('owner_token')!r}"
            ],
            warnings=[],
            binding=l1,
        )
    target = ledger.read_binding(target_address)
    if (
        target is None
        or target.get("parent_address") != l1_address
        or target.get("level") != "L2"
    ):
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"intent revision target {target_address!r} must be the owner's direct L2 child"
            ],
            warnings=[],
            binding=l1,
        )
    owner_workspace = Path(
        l1.get("workspace") or _child_node_dir(l1_address)
    ).resolve()
    candidate = Path(candidate_ref)
    if not candidate.is_absolute():
        candidate = owner_workspace / candidate
    try:
        candidate = candidate.resolve(strict=True)
        candidate.relative_to(owner_workspace)
    except (OSError, RuntimeError, ValueError) as exc:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"intent revision candidate must be a readable file inside the L1 node: {exc}"
            ],
            warnings=[],
            binding=l1,
        )
    if not candidate.is_file():
        return executor.TransitionResult(
            ok=False,
            errors=[f"intent revision candidate {candidate} is not a regular file"],
            warnings=[],
            binding=l1,
        )
    try:
        candidate_bytes = candidate.read_bytes()
        candidate_text = candidate_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return executor.TransitionResult(
            ok=False,
            errors=[f"intent revision candidate must be readable UTF-8: {exc}"],
            warnings=[],
            binding=l1,
        )
    pending = _PENDING_INTENT_ROW.findall(candidate_text)
    if pending:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"intent revision candidate carries {len(pending)} load-bearing R-* row(s) "
                "still pending reflect-back confirmation"
            ],
            warnings=[],
            binding=l1,
        )
    spec = _child_node_dir(target_address) / "client-brief" / "intent-spec.md"
    path_key = contracts.canonical_contract_path(spec)
    receipts = contracts.merge_receipts(
        target.get("contract_receipts"),
        target.get("intent_spec_receipt"),
    )
    prior_receipt = receipts.get(path_key)
    if prior_receipt is None:
        return executor.TransitionResult(
            ok=False,
            errors=[f"L2 target has no intent-spec receipt for {path_key}"],
            warnings=[],
            binding=l1,
        )
    prior_fingerprint = contracts.receipt_fingerprint(prior_receipt)
    candidate_stamp = notary.stamp(candidate)
    if candidate_stamp.get("present") is not True:
        return executor.TransitionResult(
            ok=False,
            errors=[f"intent revision candidate {candidate} cannot be stamped"],
            warnings=[],
            binding=l1,
        )
    if candidate_stamp.get("sha256") == prior_fingerprint:
        return executor.TransitionResult(
            ok=False,
            errors=["intent revision candidate is byte-identical to the held version"],
            warnings=[],
            binding=l1,
        )
    versions = copy.deepcopy(l1.get("contract_versions") or {})
    prior_version = versions.get(path_key)
    if prior_version is None:
        prior_version = contracts.version_entry(
            owner_address=l1_address,
            artifact=spec,
            stamped=prior_receipt["stamp"],
        )
    if prior_version.get("fingerprint") != prior_fingerprint:
        # The holder may legitimately be stale across an earlier revision.  An owner revision
        # must extend the owner's current head, not branch from that holder receipt.
        prior_fingerprint = prior_version.get("fingerprint")
        prior_receipt = contracts.contract_receipt(
            target_address,
            l1_address,
            spec,
            prior_version["stamp"],
            revision_record_ref=prior_version.get("revision_record_ref"),
        )
    try:
        revision_ref, _record = contracts.mint_revision_record(
            owner_address=l1_address,
            owner_workspace=owner_workspace,
            contract_path=spec,
            prior_fingerprint=prior_fingerprint,
            new_fingerprint=candidate_stamp["sha256"],
            reason=reason,
            channel="intent_revision",
            channel_evidence={
                "owner_token": expected_owner_token,
                "target_address": target_address,
                "prior_receipt_holder": target_address,
            },
            replacement_ref=str(candidate),
        )
        store.atomic_replace(spec, lambda handle: handle.write(candidate_text))
        restamped = notary.restamp(
            spec,
            prior_receipt=prior_receipt,
            revision_record_ref=revision_ref,
            read_only=True,
        )
        current_version = contracts.append_lineage(prior_version, restamped)
    except (OSError, contracts.ContractError, notary.NotaryError) as exc:
        return executor.TransitionResult(
            ok=False,
            errors=[f"intent revision could not be ratified: {exc}"],
            warnings=[],
            binding=l1,
        )
    versions[path_key] = current_version
    if (
        (l1.get("contract_versions") or {}).get(path_key) == current_version
        and l1.get("intent_revision_last_ref") == str(revision_ref)
    ):
        return executor.TransitionResult(
            ok=True,
            errors=[],
            warnings=[],
            binding=l1,
        )
    return executor.record_related_updates(
        l1_address,
        primary_delta={
            "contract_versions": versions,
            "intent_revision_last_ref": str(revision_ref),
            "intent_revision_last_target": target_address,
        },
        expected_owner_tokens={l1_address: expected_owner_token},
        event="intent_spec_revised",
        summary=(
            f"L1 ratified intent revision {revision_ref.name} for {target_address}"
        ),
        artifacts=[str(spec), str(revision_ref), str(candidate)],
    )


def _frozen_input_stamps(node_address: str) -> dict:
    node_dir = _child_node_dir(node_address)
    binding = ledger.read_binding(node_address) or {}
    frozen_files = FROZEN_INPUT_FILES
    if binding.get("child_purpose") == CHILD_PURPOSE_TEST_AUTHOR:
        frozen_files = tuple(name for name in FROZEN_INPUT_FILES if name != "acceptance.md")
    stamps: dict = {}
    for name in frozen_files:
        stamps[name] = _file_stamp(node_dir / name)
    return stamps


def _acceptance_stamp(node_address: str) -> dict:
    return _file_stamp(_child_node_dir(node_address) / "acceptance.md")


def _test_refresh_package_stamp(node_address: str) -> dict:
    """Stamp the approved refreshed test package, excluding runtime cache files."""
    tests_dir = _child_node_dir(node_address) / "tests"
    if not tests_dir.is_dir():
        return {"present": False, "reason": "missing tests directory"}
    files = _test_refresh_package_files(tests_dir)
    if not files:
        return {"present": False, "reason": "empty tests directory"}
    return notary.stamp(
        tests_dir,
        members=files,
        root_label="tests",
    )


def _test_refresh_package_files(tests_dir: Path) -> list[Path]:
    """Return the legacy accepted-package membership, with policy kept outside the notary."""
    files: list[Path] = []
    for path in sorted(p for p in tests_dir.rglob("*") if p.is_file()):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.name == ".DS_Store":
            continue
        files.append(path)
    return files


def _test_package_key(package_address: str) -> str:
    path, _seat = addressing.split_address(package_address)
    return path.rsplit("/", 1)[-1]


def _accepted_test_contract_info(
    l4_address: str,
    package_address: str,
) -> Optional[dict]:
    l4 = ledger.read_binding(l4_address) or {}
    info = (l4.get("accepted_test_contracts") or {}).get(package_address)
    return copy.deepcopy(info) if isinstance(info, dict) else None


def _materialize_initial_accepted_test_contract(
    l4_address: str,
    package_address: str,
) -> tuple[dict, dict]:
    """Land a gate-passed test-author candidate at its one L4-owned physical home.

    Returns ``(contract_info, l4_delta)``.  The caller co-commits ``l4_delta`` with the
    producer gate pass.  When recovering a pre-3b already-passed package, the same result can
    be committed as a narrow admission repair before implementation bind.
    """
    l4 = ledger.read_binding(l4_address)
    package = ledger.read_binding(package_address)
    if l4 is None or package is None:
        raise contracts.ContractError("accepted-test owner/package binding is absent")
    existing_info = _accepted_test_contract_info(l4_address, package_address)
    if existing_info is not None:
        home = Path(existing_info["artifact"])
        stamped = contracts.stamp_package(home)
        if stamped.get("sha256") != existing_info.get("fingerprint"):
            raise contracts.ContractError(
                f"accepted-test home {home} drifted from its committed fingerprint"
            )
        return existing_info, {
            "accepted_test_contracts": copy.deepcopy(
                l4.get("accepted_test_contracts") or {}
            ),
            "contract_versions": copy.deepcopy(l4.get("contract_versions") or {}),
        }

    source = _child_node_dir(package_address) / "tests"
    home = contracts.accepted_test_home(
        l4_address,
        _test_package_key(package_address),
    )
    stage = contracts.stage_package_home(source, home)
    stamped = contracts.install_staged_home(stage, home)
    version = contracts.version_entry(
        owner_address=l4_address,
        artifact=home,
        stamped=stamped,
    )
    versions = copy.deepcopy(l4.get("contract_versions") or {})
    path = contracts.canonical_contract_path(home)
    prior = versions.get(path)
    if prior is not None and prior.get("fingerprint") != stamped.get("sha256"):
        raise contracts.ContractError(
            f"accepted-test contract {path} already exists at another fingerprint; "
            "a revision record is required"
        )
    versions[path] = version
    info = {
        "schema_version": 1,
        "package_key": _test_package_key(package_address),
        "package_address": package_address,
        "artifact": path,
        "fingerprint": stamped.get("sha256"),
        "stamp": stamped,
    }
    accepted = copy.deepcopy(l4.get("accepted_test_contracts") or {})
    accepted[package_address] = info
    return info, {
        "accepted_test_contracts": accepted,
        "contract_versions": versions,
    }


def _ensure_accepted_test_contract(
    l4_address: str,
    package_address: str,
) -> Optional[dict]:
    """Resolve the L4 home, repairing only pre-3b already-ratified ledger state."""
    info = _accepted_test_contract_info(l4_address, package_address)
    if info is not None:
        return info
    package = ledger.read_binding(package_address) or {}
    if (
        package.get("gate_state") != "gate_passed"
        or package.get("child_purpose") != CHILD_PURPOSE_TEST_AUTHOR
    ):
        return None
    try:
        info, delta = _materialize_initial_accepted_test_contract(
            l4_address,
            package_address,
        )
    except (OSError, contracts.ContractError):
        return None
    result = executor.record_admission(
        l4_address,
        delta=delta,
        event="accepted_test_contract_home_recovered",
        summary=(
            f"pre-3b accepted package {package_address} materialized at its L4 contract home"
        ),
    )
    return info if result is not None and result.ok else None


def _copytree_ignore_runtime_cache(_dir: str, names: list[str]) -> set[str]:
    return {
        name for name in names
        if name == "__pycache__"
        or name == ".DS_Store"
        or name.endswith((".pyc", ".pyo"))
    }


def _bind_accepted_test_package(
    parent_address: str,
    child_address: str,
    *,
    child_level: Optional[str],
    child_metadata: Optional[dict] = None,
) -> Optional[tuple[str, str]]:
    """Copy a gate-passed test-author package into an implementation child when absent.

    The accepted package is the source of truth. A missing or empty target ``tests/`` tree is a
    pre-spawn materialization gap and can be filled deterministically by the harness. A non-empty
    mismatching target remains a fail-closed condition handled by the existing spawn blockers.
    """
    if child_level != "L5":
        return None
    metadata = child_metadata or {}
    if metadata.get("child_purpose") == CHILD_PURPOSE_TEST_AUTHOR:
        return None
    if metadata.get("no_executable_tests_exception_ref"):
        return None
    parent = ledger.read_binding(parent_address) or {}
    if parent.get("level") != "L4":
        return None
    package_ref = metadata.get("accepted_test_package")
    if not package_ref:
        return None
    package_address = _resolve_test_package_address(parent_address, str(package_ref))
    if not package_address:
        return None
    package = ledger.read_binding(package_address) or {}
    if (
        package.get("parent_address") != parent_address
        or package.get("child_purpose") != CHILD_PURPOSE_TEST_AUTHOR
        or package.get("gate_state") != "gate_passed"
    ):
        return None
    contract_info = _ensure_accepted_test_contract(parent_address, package_address)
    if contract_info is None:
        return None
    expected = contract_info["stamp"]

    actual = _test_refresh_package_stamp(child_address)
    if actual.get("present") is True:
        target_dir = _child_node_dir(child_address) / "tests"
        target_files = _test_refresh_package_files(target_dir)
        if notary.check(
            expected,
            target=target_dir,
            members=target_files,
            root_label="tests",
        ):
            notary.stamp(
                target_dir,
                members=target_files,
                root_label="tests",
                read_only=True,
            )
        return None

    source_dir = Path(contract_info["artifact"])
    target_dir = _child_node_dir(child_address) / "tests"
    if target_dir.exists() and not target_dir.is_dir():
        return (
            "accepted_test_package_bind_failed",
            f"implementation target {child_address!r} has a non-directory tests path",
        )
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = target_dir.with_name(f".{target_dir.name}.accepted-package.tmp")
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    try:
        shutil.copytree(source_dir, temp_dir, ignore=_copytree_ignore_runtime_cache)
        os.replace(temp_dir, target_dir)
    except Exception as exc:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        return (
            "accepted_test_package_bind_failed",
            f"failed to bind accepted test package {package_address!r} into {child_address!r}: {exc}",
        )

    bound_files = _test_refresh_package_files(target_dir)
    bound = notary.stamp(
        target_dir,
        members=bound_files,
        root_label="tests",
        read_only=True,
    )
    if not notary.check(
        expected,
        target=target_dir,
        members=bound_files,
        root_label="tests",
    ):
        return (
            "accepted_test_package_bind_failed",
            (
                f"bound accepted test package for {child_address!r} does not match "
                f"{package_address!r}; expected sha256 {expected.get('sha256')}, "
                f"got {bound.get('sha256')}"
            ),
        )
    return None


def bind_accepted_test_package_for_spawn(
    parent_address: str,
    child_address: str,
    *,
    child_level: Optional[str],
    child_metadata: Optional[dict] = None,
) -> Optional[tuple[str, str]]:
    """Public pre-spawn materialization hook for outbox and direct chokepoint callers."""
    return _bind_accepted_test_package(
        parent_address,
        child_address,
        child_level=child_level,
        child_metadata=child_metadata,
    )


def _frozen_input_drift(binding: dict) -> list[str]:
    expected = binding.get("frozen_input_stamps") or {}
    if not isinstance(expected, dict) or not expected:
        return []
    current = _frozen_input_stamps(binding["node_address"])
    drift: list[str] = []
    for name, expected_stamp in expected.items():
        if not isinstance(expected_stamp, dict):
            continue
        current_stamp = current.get(name) or {"present": False}
        if not notary.check(
            expected_stamp,
            target=_child_node_dir(binding["node_address"]) / name,
        ):
            drift.append(
                f"{name}: expected {expected_stamp}, current {current_stamp}"
            )
    return drift


_GATED_PRODUCER_REVIEW_LEVELS: dict[str, str] = {
    "L2": "L2+",
    "L3": "L3+",
    "L4": "L4+",
    "L5": "L5+",
}


def _review_address_for(producer_address: str) -> str:
    path, _seat = addressing.split_address(producer_address)
    return f"{path}#review"


def _review_level_for_producer(child_address: str, child_level_config) -> Optional[str]:
    """Return the plus-level review seat for a producer, or None when this address is not gated."""
    _path, seat = addressing.split_address(child_address)
    if seat != "exec":
        return None
    level = (getattr(child_level_config, "level", None) or "").split("#", 1)[0]
    return _GATED_PRODUCER_REVIEW_LEVELS.get(level)


SERIAL_L3_WORKSTREAMS_POLICY = "serial_l3_workstreams"
ADMISSION_WAITING_ON_SIBLING = "waiting_on_sibling"
ADMISSION_BLOCKED_ON_SIBLING = "blocked_on_sibling"
ADMISSION_ADMITTED = "admitted"
QUEUE_REASON_WORKSTREAM_SERIALIZATION = "workstream_serialization"
QUEUE_REASON_PREDECESSOR_NOT_PASSED = "predecessor_not_passed"

# (The 2026-07-16 single-warm-runner codex admission gate was REMOVED 2026-07-17 by owner
# ruling: the system is DESIGNED for wide parallelization, and the June credential-race
# concern it over-solved is already structurally closed by the ephemeral-seat-home design
# (seats carry no usable refresh token). Codex concurrency is uncapped again — the 2026-06-16
# posture: record pressure, respond empirically. git history carries the gate.)
CHILD_PURPOSE_TEST_AUTHOR = "test_author"
CHILD_PURPOSE_PLANNING = "planning"
TEST_REFRESH_PENDING_L5_REVIEW = "pending_l5_review"
TEST_REFRESH_PENDING_L3_APPROVAL = "pending_l3_approval"
TEST_REFRESH_APPROVED = "approved"


def _test_refresh_targets_child(parent_binding: dict, child_address: str) -> bool:
    target = parent_binding.get("test_refresh_for")
    if not target:
        return False
    path, _seat = addressing.split_address(child_address)
    candidates = {child_address, path, path.rsplit("/", 1)[-1]}
    return str(target) in candidates


def approved_test_refresh_spawn_block(
    parent_address: str,
    child_address: str,
    *,
    child_level: Optional[str],
    child_metadata: Optional[dict] = None,
) -> Optional[tuple[str, str]]:
    """Return a failure class + reason when a refreshed-acceptance target is not bound.

    L3 approval makes a post-design test-author package active only for the target it names. Before
    opening that implementation L5, the target node's frozen ``acceptance.md`` must match the package
    L3 approved; otherwise L4 could accidentally run implementation against stale local tests.
    """
    if child_level != "L5":
        return None
    metadata = child_metadata or {}
    if metadata.get("child_purpose") == CHILD_PURPOSE_TEST_AUTHOR:
        return None
    parent = ledger.read_binding(parent_address) or {}
    if parent.get("test_refresh_state") != TEST_REFRESH_APPROVED:
        return None
    if not _test_refresh_targets_child(parent, child_address):
        return None

    expected = parent.get("test_refresh_approved_package_stamp")
    if not isinstance(expected, dict) or expected.get("present") is not True or not expected.get("sha256"):
        return (
            "approved_test_refresh_unbound",
            (
                f"parent {parent_address!r} approved refreshed tests for "
                f"{parent.get('test_refresh_for')!r}, but no approved test package hash is recorded"
            ),
        )

    actual = _test_refresh_package_stamp(child_address)
    if actual.get("present") is not True:
        return (
            "approved_test_refresh_package_missing",
            (
                f"implementation target {child_address!r} is missing the approved refreshed tests; "
                f"its tests/ package must match the L3-approved package from "
                f"{parent.get('test_refresh_child')!r}"
            ),
        )
    if not notary.check(
        expected,
        target=_child_node_dir(child_address) / "tests",
        members=_test_refresh_package_files(_child_node_dir(child_address) / "tests"),
        root_label="tests",
    ):
        return (
            "approved_test_refresh_package_mismatch",
            (
                f"implementation target {child_address!r} tests/ package does not match the "
                f"L3-approved refreshed tests for {parent.get('test_refresh_for')!r}; "
                f"expected sha256 {expected.get('sha256')}, got {actual.get('sha256')}"
            ),
        )
    return None


def _parent_path(address: str) -> str:
    path, _seat = addressing.split_address(address)
    return path


def _resolve_test_package_address(parent_address: str, ref: str) -> Optional[str]:
    """Resolve a package reference to a same-parent L5 test-author exec address."""
    value = str(ref or "").strip()
    if not value:
        return None
    parent_path = _parent_path(parent_address)
    if "#" in value or "/" in value:
        path, seat = addressing.split_address(value)
        if seat != "exec":
            return None
        if path.rsplit("/", 1)[0] != parent_path:
            return None
        return f"{path}#exec"
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return None
    return f"{parent_path}/{value}#exec"


def _target_matches_child(target: object, child_address: str) -> bool:
    if not target:
        return False
    target_s = str(target)
    path, _seat = addressing.split_address(child_address)
    candidates = {child_address, path, path.rsplit("/", 1)[-1]}
    return target_s in candidates


def _no_executable_tests_exception_ok(
    parent_address: str,
    child_address: str,
    ref: str,
) -> tuple[bool, str]:
    parent = ledger.read_binding(parent_address) or {}
    workspace = parent.get("workspace")
    if not workspace:
        return False, f"parent {parent_address!r} has no workspace for no-executable-tests exception lookup"
    ref_path = Path(str(ref))
    if ref_path.is_absolute() or ".." in ref_path.parts:
        return False, "no_executable_tests_exception_ref must be a relative path inside the parent node"
    path = Path(workspace) / ref_path
    if not path.is_file():
        return False, f"no-executable-tests exception artifact {ref!r} is missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"no-executable-tests exception artifact {ref!r} is not valid JSON: {exc}"
    if payload.get("type") != "no_executable_tests_exception":
        return False, f"no-executable-tests exception artifact {ref!r} has wrong type"
    if not _target_matches_child(payload.get("target"), child_address):
        return False, f"no-executable-tests exception artifact {ref!r} does not target {child_address!r}"
    if not str(payload.get("approved_by") or "").strip():
        return False, f"no-executable-tests exception artifact {ref!r} has no approved_by"
    if not str(payload.get("reason") or "").strip():
        return False, f"no-executable-tests exception artifact {ref!r} has no reason"
    return True, ""


def accepted_test_package_spawn_block(
    parent_address: str,
    child_address: str,
    *,
    child_level: Optional[str],
    child_metadata: Optional[dict] = None,
) -> Optional[tuple[str, str]]:
    """Return a failure class + reason when an implementation L5 lacks accepted test evidence."""
    if child_level != "L5":
        return None
    metadata = child_metadata or {}
    if metadata.get("child_purpose") == CHILD_PURPOSE_TEST_AUTHOR:
        return None
    parent = ledger.read_binding(parent_address) or {}
    if parent.get("level") != "L4":
        return None

    no_tests_ref = metadata.get("no_executable_tests_exception_ref")
    if no_tests_ref:
        ok, reason = _no_executable_tests_exception_ok(parent_address, child_address, str(no_tests_ref))
        if ok:
            return None
        return ("no_executable_tests_exception_invalid", reason)

    package_ref = metadata.get("accepted_test_package")
    if not package_ref:
        return (
            "accepted_test_package_required",
            (
                f"implementation L5 {child_address!r} requires accepted_test_package from a "
                "gate-passed L5 test_author child, or an approved no-executable-tests exception"
            ),
        )

    package_address = _resolve_test_package_address(parent_address, str(package_ref))
    if not package_address:
        return (
            "accepted_test_package_invalid_ref",
            f"accepted_test_package {package_ref!r} must name a same-parent L5 test_author child",
        )
    package = ledger.read_binding(package_address) or {}
    if package.get("parent_address") != parent_address:
        return (
            "accepted_test_package_wrong_parent",
            f"accepted_test_package {package_address!r} is not a child of {parent_address!r}",
        )
    if package.get("child_purpose") != CHILD_PURPOSE_TEST_AUTHOR:
        return (
            "accepted_test_package_not_test_author",
            f"accepted_test_package {package_address!r} is not a test_author child",
        )
    if package.get("gate_state") != "gate_passed":
        return (
            "accepted_test_package_not_passed",
            f"accepted_test_package {package_address!r} has not passed L5+ review",
        )
    contract_info = _ensure_accepted_test_contract(parent_address, package_address)
    if contract_info is None:
        return (
            "accepted_test_package_missing_tests",
            f"accepted_test_package {package_address!r} has no ratified L4 contract home to bind",
        )
    expected = contract_info["stamp"]
    actual = _test_refresh_package_stamp(child_address)
    if actual.get("present") is not True:
        return (
            "accepted_test_package_unbound",
            (
                f"implementation target {child_address!r} is missing the accepted tests; "
                f"its tests/ package must match {package_address!r}"
            ),
        )
    if not notary.check(
        expected,
        target=_child_node_dir(child_address) / "tests",
        members=_test_refresh_package_files(_child_node_dir(child_address) / "tests"),
        root_label="tests",
    ):
        return (
            "accepted_test_package_mismatch",
            (
                f"implementation target {child_address!r} tests/ package does not match accepted "
                f"test package {package_address!r}; expected sha256 {expected.get('sha256')}, "
                f"got {actual.get('sha256')}"
            ),
        )
    return None


def accepted_test_package_binding_metadata(
    parent_address: str,
    child_address: str,
    *,
    child_level: Optional[str],
    child_metadata: Optional[dict] = None,
) -> dict:
    """Return provenance fields to stamp onto an admitted implementation L5 binding."""
    if child_level != "L5":
        return {}
    metadata = child_metadata or {}
    if metadata.get("child_purpose") == CHILD_PURPOSE_TEST_AUTHOR:
        return {}
    parent = ledger.read_binding(parent_address) or {}
    if parent.get("level") != "L4":
        return {}
    no_tests_ref = metadata.get("no_executable_tests_exception_ref")
    if no_tests_ref:
        return {"no_executable_tests_exception_ref": no_tests_ref}
    package_ref = metadata.get("accepted_test_package")
    package_address = _resolve_test_package_address(parent_address, str(package_ref or ""))
    if not package_address:
        return {}
    package = ledger.read_binding(package_address) or {}
    contract_info = _ensure_accepted_test_contract(parent_address, package_address)
    if package.get("gate_state") != "gate_passed" or contract_info is None:
        return {}
    stamp = contract_info["stamp"]
    receipt = contracts.contract_receipt(
        child_address,
        parent_address,
        contract_info["artifact"],
        stamp,
    )
    return {
        "accepted_test_package": package_ref,
        "accepted_test_package_address": package_address,
        "accepted_test_package_stamp": stamp,
        "accepted_test_contract_home": contract_info["artifact"],
        "accepted_test_package_gate_id": package.get("gate_id"),
        "accepted_test_package_gate_passed_at": package.get("gate_passed_at"),
        "contract_receipts": contracts.merge_receipts(
            metadata.get("contract_receipts"),
            receipt,
        ),
    }


def _is_l2_owned_l3_exec(parent_binding: Optional[dict], child_address: str, child_level_config) -> bool:
    """True for the semantic scheduler's scope: L2 spawning any L3 exec seat."""
    if (parent_binding or {}).get("level") != "L2":
        return False
    _path, seat = addressing.split_address(child_address)
    if seat != "exec":
        return False
    level = (getattr(child_level_config, "level", None) or "").split("#", 1)[0]
    return level == "L3"


def _serial_l3_schedule_siblings(
    *,
    live_map: dict,
    parent_address: str,
    child_address: str,
) -> list[dict]:
    """Existing L3 exec siblings under the same L2, ordered by their schedule index."""
    siblings = []
    for address, binding in live_map.items():
        if address == child_address or not isinstance(binding, dict):
            continue
        if binding.get("parent_address") != parent_address:
            continue
        if binding.get("level") != "L3":
            continue
        _path, seat = addressing.split_address(address)
        if seat != "exec":
            continue
        siblings.append(binding)

    def _key(binding: dict) -> tuple[int, str]:
        index = binding.get("schedule_index")
        if isinstance(index, int):
            return index, str(binding.get("node_address") or "")
        return 0, str(binding.get("node_address") or "")

    return sorted(siblings, key=_key)


def _gate_passed(binding: Optional[dict]) -> bool:
    return (binding or {}).get("gate_state") == "gate_passed"


def _serial_predecessor_block_reason(predecessor: Optional[dict]) -> Optional[str]:
    """Return a parent-visible block reason for a non-passed predecessor, if one exists."""
    if predecessor is None:
        return "predecessor_missing"
    if _gate_passed(predecessor):
        return None
    gate_state = predecessor.get("gate_state")
    if gate_state in {"gate_failed", "gate_escalated"}:
        return str(gate_state)
    if states.is_terminal(predecessor.get("state")):
        return "predecessor_terminal_not_passed"
    return None


# PARALLEL-BY-DEFAULT (owner ruling 2026-07-17): the system is designed for wide
# parallelization — L3 siblings under one L2 are admitted CONCURRENTLY unless the operator
# explicitly re-enables the serial scheduler with HARNESS_SERIAL_L3_WORKSTREAMS=1. The serial
# machinery (the June BI-027 over-fan response) is retained behind that knob so a future
# observed over-fan incident can re-arm it without a code change. "If we notice an actual
# issue, we'll know to add some of this back."
SERIAL_L3_ENV: str = "HARNESS_SERIAL_L3_WORKSTREAMS"


def _serial_l3_requested() -> bool:
    """True iff the operator explicitly re-enabled serial L3 admission (strictly \"1\")."""
    return os.environ.get(SERIAL_L3_ENV) == "1"


def _apply_serial_l3_schedule(
    binding: dict,
    *,
    live_map: dict,
    parent_binding: Optional[dict],
    parent_address: str,
    child_address: str,
    child_level_config,
    child_metadata: Optional[dict] = None,
) -> None:
    """Stamp L2-owned serial L3 admission metadata onto a fresh child binding.

    PARALLEL BY DEFAULT since 2026-07-17 (owner ruling): without the explicit
    HARNESS_SERIAL_L3_WORKSTREAMS=1 knob this stamps nothing and L3 siblings admit
    concurrently. With the knob, the scheduler serializes L3 siblings below one L2,
    including planning-L3 and execution-L3 seats, while leaving lower L4/L5 fanout
    untouched. Waiting children are ledger-visible planned nodes; no actor is opened
    until their immediate predecessor has passed its gate.
    """
    if not _serial_l3_requested():
        return
    if not _is_l2_owned_l3_exec(parent_binding, child_address, child_level_config):
        return

    siblings = _serial_l3_schedule_siblings(
        live_map=live_map,
        parent_address=parent_address,
        child_address=child_address,
    )
    next_index = 1
    if siblings:
        numeric_indexes = [
            value for value in (sib.get("schedule_index") for sib in siblings)
            if isinstance(value, int)
        ]
        next_index = (max(numeric_indexes) if numeric_indexes else len(siblings)) + 1
    predecessor = siblings[-1] if siblings else None

    binding.update({
        "schedule_policy": SERIAL_L3_WORKSTREAMS_POLICY,
        "schedule_group": f"{parent_address}:L3",
        "schedule_index": next_index,
    })
    if predecessor is not None and not _gate_passed(predecessor):
        binding.update({
            "admission_state": ADMISSION_WAITING_ON_SIBLING,
            "waiting_on_sibling": predecessor.get("node_address"),
            "queue_reason": QUEUE_REASON_WORKSTREAM_SERIALIZATION,
            "queued_since": clock.now_utc(),
        })
        return

    binding.update({
        "admission_state": ADMISSION_ADMITTED,
        "admission_ready_at": clock.now_utc(),
    })


def release_serial_l3_wait_if_ready(node_address: str) -> bool:
    """Release a queued L3 when its immediate predecessor has gate_state=gate_passed.

    Returns True when the node is spawn-admissible, False when it is still waiting or cannot be
    verified. The caller remains responsible for the actual claim-and-spawn CAS.
    """
    binding = ledger.read_binding(node_address)
    if not binding:
        return False
    admission_state = binding.get("admission_state")
    if admission_state not in {ADMISSION_WAITING_ON_SIBLING, ADMISSION_BLOCKED_ON_SIBLING}:
        return True
    predecessor_address = binding.get("waiting_on_sibling")
    if not predecessor_address:
        return False
    predecessor = ledger.read_binding(str(predecessor_address))
    if not _gate_passed(predecessor):
        block_reason = _serial_predecessor_block_reason(predecessor)
        if block_reason:
            _record_serial_l3_admission_block(
                node_address,
                binding,
                str(predecessor_address),
                predecessor,
                block_reason,
            )
        return False
    result = executor.record_admission(
        node_address,
        expected_owner_token=binding.get("owner_token"),
        delta={
            "admission_state": ADMISSION_ADMITTED,
            "queue_reason": None,
            "admission_ready_at": clock.now_utc(),
            "admission_released_by": predecessor_address,
        },
        event="admission_released",
        summary=(
            f"serial L3 workstream admitted after predecessor {predecessor_address} "
            "passed its gate"
        ),
    )
    return bool(getattr(result, "ok", False))


def _record_serial_l3_admission_block(
    node_address: str,
    binding: dict,
    predecessor_address: str,
    predecessor: Optional[dict],
    block_reason: str,
) -> None:
    predecessor_state = (predecessor or {}).get("state")
    predecessor_gate_state = (predecessor or {}).get("gate_state")
    already_blocked = (
        binding.get("admission_state") == ADMISSION_BLOCKED_ON_SIBLING
        and binding.get("admission_block_reason") == block_reason
        and binding.get("admission_blocked_by") == predecessor_address
        and binding.get("admission_blocked_predecessor_state") == predecessor_state
        and binding.get("admission_blocked_predecessor_gate_state") == predecessor_gate_state
    )
    if already_blocked:
        _notify_parent_of_serial_admission_block(node_address, binding)
        return
    result = executor.record_admission(
        node_address,
        expected_owner_token=binding.get("owner_token"),
        delta={
            "admission_state": ADMISSION_BLOCKED_ON_SIBLING,
            "queue_reason": QUEUE_REASON_PREDECESSOR_NOT_PASSED,
            "admission_blocked_at": clock.now_utc(),
            "admission_blocked_by": predecessor_address,
            "admission_block_reason": block_reason,
            "admission_blocked_predecessor_state": predecessor_state,
            "admission_blocked_predecessor_gate_state": predecessor_gate_state,
        },
        event="admission_blocked",
        summary=(
            f"serial L3 workstream {node_address} blocked because predecessor "
            f"{predecessor_address} did not pass (reason={block_reason})"
        ),
    )
    if result is not None and getattr(result, "ok", False):
        _notify_parent_of_serial_admission_block(
            node_address,
            getattr(result, "binding", None) or binding,
        )


def _notify_parent_of_serial_admission_block(node_address: str, binding: dict) -> None:
    """Append one parent pointer for a serial L3 queue blocked by its predecessor."""
    try:
        parent = (binding or {}).get("parent_address")
        if not parent or ledger.RUNTIME_ROOT is None:
            return
        parent_binding = ledger.read_binding(parent)
        if parent_binding is None or states.is_terminal(parent_binding.get("state")):
            return
        predecessor = (binding or {}).get("admission_blocked_by") or (binding or {}).get(
            "waiting_on_sibling"
        )
        block_reason = (binding or {}).get("admission_block_reason")
        inbox = addressing.inbox_path(parent, ledger.RUNTIME_ROOT)
        if _inbox_has_line(
            inbox,
            type="serial_admission_blocked",
            child=node_address,
            predecessor=predecessor,
            block_reason=block_reason,
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "from": "harnessd",
            "type": "serial_admission_blocked",
            "child": node_address,
            "predecessor": predecessor,
            "block_reason": block_reason,
            "predecessor_state": (binding or {}).get("admission_blocked_predecessor_state"),
            "predecessor_gate_state": (binding or {}).get("admission_blocked_predecessor_gate_state"),
            "message": (
                f"Serial L3 workstream {node_address} is blocked because predecessor "
                f"{predecessor} did not pass. Decide retry, resequencing, cancellation, or escalation."
            ),
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort; block state remains durable
        pass


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside_path(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _safe_coordination_handoff_id(value: object) -> str:
    handoff_id = str(value or "").strip()
    if not handoff_id:
        raise ValueError("coordination handoff requires handoff_id")
    if len(handoff_id) > 96:
        raise ValueError("coordination handoff handoff_id is too long")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if any(ch not in allowed for ch in handoff_id):
        raise ValueError("coordination handoff handoff_id may contain only letters, digits, '.', '_', and '-'")
    return handoff_id


def _resolve_node_owned_artifact(payload: dict, node_dir: Path, key: str) -> Path:
    artifact_value = payload.get(key)
    if not artifact_value:
        raise ValueError(f"coordination handoff requires {key}")
    artifact = Path(str(artifact_value))
    resolved = artifact if artifact.is_absolute() else node_dir / artifact
    if not _inside_path(resolved, node_dir):
        raise ValueError(f"coordination handoff {key} must be inside its node workspace")
    if not resolved.is_file():
        raise FileNotFoundError(f"coordination handoff {key} does not exist: {resolved}")
    return resolved


def _coordination_handoff_records(binding: dict) -> dict:
    records = (binding or {}).get("coordination_handoffs") or {}
    return dict(records) if isinstance(records, dict) else {}


def _resolve_coordination_handoff_marker(
    marker_payload: dict,
    marker_path: Path,
    node_dir: Path,
) -> tuple[str, str, Path, str, bool, str]:
    if not isinstance(marker_payload, dict) or marker_payload.get("type") != COORDINATION_HANDOFF_TYPE:
        raise ValueError(f"{marker_path} is not a {COORDINATION_HANDOFF_TYPE} marker")
    handoff_id = _safe_coordination_handoff_id(marker_payload.get("handoff_id") or marker_path.stem)
    handoff_kind = str(marker_payload.get("handoff_kind") or "").strip()
    if handoff_kind not in COORDINATION_HANDOFF_ALLOWED_KINDS:
        raise ValueError(
            "coordination handoff handoff_kind must be one of "
            f"{sorted(COORDINATION_HANDOFF_ALLOWED_KINDS)}"
        )
    artifact = _resolve_node_owned_artifact(marker_payload, node_dir, "artifact")
    summary = str(marker_payload.get("summary") or "").strip()
    if not summary:
        raise ValueError("coordination handoff requires a short summary")
    response_required = bool(marker_payload.get("response_required", False))
    phase = str(marker_payload.get("phase") or "").strip()
    return handoff_id, handoff_kind, artifact, summary, response_required, phase


def submit_coordination_handoff(
    node_address: str,
    *,
    marker_path: Optional[Path | str] = None,
    expected_owner_token: Optional[str] = None,
):
    """Record a normal nonterminal child -> parent coordination handoff.

    This is the T65 cascade channel. It is deliberately narrower than a generic
    message bus: only a running #exec seat may submit a durable handoff marker
    from inside its node workspace, and the target is its direct parent.
    Completion still routes through review gates; blocking questions use the
    canonical message primitive and its open-question state.
    """
    live = ledger.read_binding(node_address)
    if live is None:
        return None
    if live.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"coordination handoff applies only to a running node; "
                f"{node_address!r} is {live.get('state')!r}"
            ],
            warnings=[],
            binding=live,
        )
    if not str(node_address).endswith("#exec"):
        return executor.TransitionResult(
            ok=False,
            errors=[f"coordination handoff is owned by #exec seats; {node_address!r} is not an exec seat"],
            warnings=[],
            binding=live,
        )
    parent = live.get("parent_address")
    if not parent:
        return executor.TransitionResult(
            ok=False,
            errors=[f"coordination handoff requires a parent address on {node_address!r}"],
            warnings=[],
            binding=live,
        )
    if ledger.RUNTIME_ROOT is None:
        return executor.TransitionResult(
            ok=False,
            errors=["coordination handoff requires ledger.RUNTIME_ROOT"],
            warnings=[],
            binding=live,
        )
    node_dir = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
    marker = Path(marker_path) if marker_path is not None else node_dir / COORDINATION_HANDOFF_DIRNAME
    if marker.is_dir():
        return executor.TransitionResult(
            ok=False,
            errors=["coordination handoff marker_path must name one marker file"],
            warnings=[],
            binding=live,
        )
    if not _inside_path(marker, node_dir):
        return executor.TransitionResult(
            ok=False,
            errors=["coordination handoff marker must be inside its node workspace"],
            warnings=[],
            binding=live,
        )
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        handoff_id, handoff_kind, artifact, summary, response_required, phase = (
            _resolve_coordination_handoff_marker(payload, marker, node_dir)
        )
        marker_sha = _sha256_file(marker)
    except (OSError, ValueError) as exc:
        return executor.TransitionResult(ok=False, errors=[str(exc)], warnings=[], binding=live)

    # Pre-upgrade rows remain readable. New calls below never extend this parallel map.
    records = _coordination_handoff_records(live)
    previous = records.get(handoff_id)
    if previous:
        if previous.get("marker_sha256") == marker_sha:
            # Adopt a pre-3a durable row into canonical truth before replaying its legacy pointer.
            from harnessd import messages as _messages

            try:
                _messages.submit_compat_message(
                    node_address,
                    target=parent,
                    message_id=handoff_id,
                    artifact=artifact,
                    marker=marker,
                    summary=summary,
                    needs_answer=response_required,
                    metadata={
                        "kind": handoff_kind,
                        "phase": phase,
                        "legacy": "coordination_handoff",
                    },
                    runtime_root=ledger.RUNTIME_ROOT,
                )
            except Exception as exc:
                return executor.TransitionResult(
                    ok=False,
                    errors=[f"legacy handoff canonicalization failed: {exc}"],
                    warnings=[],
                    binding=ledger.read_binding(node_address) or live,
                )
            if previous.get("state") == COORDINATION_HANDOFF_STATE_SUBMITTED:
                _notify_parent_of_coordination_handoff(node_address, live, previous)
            return None
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"coordination handoff {handoff_id!r} already exists with different content; "
                "use a fresh handoff_id"
            ],
            warnings=[],
            binding=live,
        )

    # Legacy marker shim: kind/phase collapse to metadata; response_required becomes needs_answer.
    # No new coordination_handoffs row or legacy inbox type is written.
    from harnessd import messages as _messages

    try:
        result = _messages.submit_compat_message(
            node_address,
            target=parent,
            message_id=handoff_id,
            artifact=artifact,
            marker=marker,
            summary=summary,
            needs_answer=response_required,
            metadata={"kind": handoff_kind, "phase": phase, "legacy": "coordination_handoff"},
            deliver_pointer=True,
            event="coordination_handoff_submitted",
            event_summary=f"{node_address} submitted coordination handoff {handoff_id} to {parent}",
            runtime_root=ledger.RUNTIME_ROOT,
        )
    except Exception as exc:
        return executor.TransitionResult(
            ok=False,
            errors=[f"canonical message refusal: {exc}; use a fresh handoff_id"],
            warnings=[],
            binding=ledger.read_binding(node_address) or live,
        )
    return result


def _notify_parent_of_coordination_handoff(node_address: str, binding: dict, record: dict) -> None:
    """Append one `coordination-handoff` pointer line to the direct parent's inbox."""
    try:
        parent = (record or {}).get("target") or (binding or {}).get("parent_address")
        if not parent or ledger.RUNTIME_ROOT is None:
            return
        parent_binding = ledger.read_binding(parent)
        if parent_binding is None or states.is_terminal(parent_binding.get("state")):
            return
        inbox = addressing.inbox_path(parent, ledger.RUNTIME_ROOT)
        if _inbox_has_line(
            inbox,
            type="coordination-handoff",
            child=node_address,
            handoff_id=record.get("handoff_id"),
            marker_sha256=record.get("marker_sha256"),
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "from": "harnessd",
            "type": "coordination-handoff",
            "child": node_address,
            "handoff_id": record.get("handoff_id"),
            "handoff_kind": record.get("handoff_kind"),
            "phase": record.get("phase"),
            "artifact": record.get("artifact"),
            "marker_artifact": record.get("marker_artifact"),
            "marker_sha256": record.get("marker_sha256"),
            "response_required": record.get("response_required"),
            "message": record.get("summary") or "Coordination handoff submitted.",
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort; handoff remains durable
        pass


def recover_coordination_handoff_notifications(node_address: str, binding: Optional[dict] = None) -> None:
    """Replay lost coordination pointers from durable binding state."""
    live = binding or ledger.read_binding(node_address)
    if not live:
        return
    for record in _coordination_handoff_records(live).values():
        state = (record or {}).get("state")
        if state == COORDINATION_HANDOFF_STATE_SUBMITTED:
            _notify_parent_of_coordination_handoff(node_address, live, record)
        elif state in {
            COORDINATION_HANDOFF_STATE_DECISION_POSTED,
            COORDINATION_HANDOFF_STATE_NOTICE_POSTED,
        }:
            _notify_child_of_coordination_record(node_address, live, record)


def _notify_child_of_coordination_record(node_address: str, binding: dict, record: dict) -> None:
    """Replay a child-visible coordination decision/notice pointer."""
    try:
        if ledger.RUNTIME_ROOT is None:
            return
        live = binding or ledger.read_binding(node_address)
        if live is None or states.is_terminal(live.get("state")):
            return
        inbox = addressing.inbox_path(node_address, ledger.RUNTIME_ROOT)
        state = (record or {}).get("state")
        if state == COORDINATION_HANDOFF_STATE_DECISION_POSTED:
            artifact = record.get("decision_artifact")
            if _inbox_has_line(
                inbox,
                type="coordination_decision",
                handoff_id=record.get("handoff_id"),
                decision_artifact=artifact,
            ):
                return
            line = {
                "from": "harnessd",
                "type": "coordination_decision",
                "handoff_id": record.get("handoff_id"),
                "handoff_kind": record.get("handoff_kind"),
                "phase": record.get("phase"),
                "child": node_address,
                "decision": record.get("decision"),
                "decision_actor": record.get("decision_actor"),
                "decision_artifact": artifact,
                "message": "Parent coordination decision posted; read the artifact and continue.",
                "ts": clock.now_utc(),
            }
        elif state == COORDINATION_HANDOFF_STATE_NOTICE_POSTED:
            artifact = record.get("artifact")
            if _inbox_has_line(
                inbox,
                type="coordination_notice",
                handoff_id=record.get("handoff_id"),
                artifact=artifact,
            ):
                return
            line = {
                "from": "harnessd",
                "type": "coordination_notice",
                "handoff_id": record.get("handoff_id"),
                "handoff_kind": record.get("handoff_kind"),
                "child": node_address,
                "artifact": artifact,
                "message": record.get("summary") or "Parent coordination note posted.",
                "ts": clock.now_utc(),
            }
        else:
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort; handoff remains durable
        pass


def _resolve_plan_alignment_package(marker_payload: dict, marker_path: Path, node_dir: Path) -> Path:
    package_value = marker_payload.get("package") or "validated-plan-package.md"
    package = Path(str(package_value))
    if package.is_absolute():
        resolved = package
    else:
        resolved = node_dir / package
        if not resolved.exists() and package.parts and package.parts[0] == "L2":
            # The design docs sometimes render the L2-owned artifact as
            # L2/validated-plan-package.md. Runtime node workspaces already ARE the L2's
            # workspace, so accept that prefix as a human-facing alias.
            resolved = node_dir / Path(*package.parts[1:])
    if not _inside_path(resolved, node_dir):
        raise ValueError("plan-alignment package must be inside its node workspace")
    if not resolved.is_file():
        raise FileNotFoundError(f"plan-alignment package does not exist: {resolved}")
    return resolved


def submit_plan_alignment_ready(
    node_address: str,
    *,
    marker_path: Optional[Path | str] = None,
    expected_owner_token: Optional[str] = None,
    allow_uncalibrated: bool = False,
):
    """Admit Q3, materialize Q4 inputs, and park the L2 on its semantic cell.

    L1 is not woken at the Q3 boundary.  The daemon-owned semantic cohort must finish
    first; only the final content-addressed evidence index produces the existing
    ``design-submission`` pointer.  ``allow_uncalibrated`` is accepted for the explicit
    test seam, but calibration is enforced when the daemon actually dispatches seats.
    """
    del allow_uncalibrated
    live = ledger.read_binding(node_address)
    if live is None:
        return None
    if live.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"plan-alignment readiness applies only to a running L2 node; "
                f"{node_address!r} is {live.get('state')!r}"
            ],
            warnings=[],
            binding=live,
        )
    if (live.get("level") or "").split("#", 1)[0] != "L2":
        return executor.TransitionResult(
            ok=False,
            errors=[f"plan-alignment readiness is L2-owned; {node_address!r} is level={live.get('level')!r}"],
            warnings=[],
            binding=live,
        )
    if not live.get("parent_address"):
        return executor.TransitionResult(
            ok=False,
            errors=[f"plan-alignment readiness requires a parent L1 address on {node_address!r}"],
            warnings=[],
            binding=live,
        )
    if ledger.RUNTIME_ROOT is None:
        return executor.TransitionResult(
            ok=False,
            errors=["plan-alignment readiness requires ledger.RUNTIME_ROOT"],
            warnings=[],
            binding=live,
        )
    node_dir = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
    marker = Path(marker_path) if marker_path is not None else node_dir / PLAN_ALIGNMENT_READY_FILENAME
    if not _inside_path(marker, node_dir):
        return executor.TransitionResult(
            ok=False,
            errors=["plan-alignment ready marker must be inside its node workspace"],
            warnings=[],
            binding=live,
        )
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return executor.TransitionResult(
            ok=False,
            errors=[f"could not read plan-alignment ready marker {marker}: {exc}"],
            warnings=[],
            binding=live,
        )
    if not isinstance(payload, dict) or payload.get("type") != "plan_alignment_ready":
        return executor.TransitionResult(
            ok=False,
            errors=[f"{marker} is not a plan_alignment_ready marker"],
            warnings=[],
            binding=live,
        )
    try:
        package = _resolve_plan_alignment_package(payload, marker, node_dir)
    except (OSError, ValueError) as exc:
        return executor.TransitionResult(
            ok=False,
            errors=[str(exc)],
            warnings=[],
            binding=live,
        )

    coverage = plan_alignment.evaluate_submission(
        node_address=node_address,
        node_dir=node_dir,
        marker=marker,
        marker_payload=payload,
        package=package,
        binding=live,
    )
    if not coverage.ok:
        errors = list(coverage.defects)
        if coverage.report_path is not None:
            errors.insert(0, f"PLAN-ALIGNMENT-COVERAGE-REPORT={coverage.report_path}")
        return executor.TransitionResult(
            ok=False,
            errors=errors,
            warnings=[],
            binding=live,
        )

    semantic = plan_alignment_cell.prepare_submission(
        node_address=node_address,
        node_dir=node_dir,
        marker_payload=payload,
        coverage=coverage,
        binding=live,
        # Preparation proves the machinery and freezes inputs. Production dispatch below is the
        # fail-closed calibration boundary; treating preparation as dispatch would prevent the
        # durable semantic_cell_pending state the daemon must recover after calibration.
        allow_uncalibrated=True,
    )
    if not semantic.ok:
        return executor.TransitionResult(
            ok=False,
            errors=list(semantic.defects),
            warnings=[],
            binding=live,
        )

    marker_sha = coverage.marker_sha256
    bundle_sha = coverage.bundle_sha256
    semantic_bundle_sha = semantic.cell_sha256
    if live.get("plan_alignment_semantic_bundle_sha256") == semantic_bundle_sha:
        if live.get("plan_alignment_state") == PLAN_ALIGNMENT_STATE_READY:
            _notify_parent_of_plan_alignment_ready(node_address, live)
        return None

    ready_at = clock.now_utc()
    result = executor.record_admission(
        node_address,
        expected_owner_token=expected_owner_token or live.get("owner_token"),
        delta={
            "plan_alignment_state": PLAN_ALIGNMENT_STATE_SEMANTIC_PENDING,
            "plan_alignment_ready_artifact": str(marker),
            "plan_alignment_ready_sha256": marker_sha,
            "plan_alignment_package": str(package),
            "plan_alignment_coverage_manifest": (
                str(coverage.manifest) if coverage.manifest is not None else None
            ),
            "plan_alignment_coverage_report": (
                str(coverage.report_path) if coverage.report_path is not None else None
            ),
            "plan_alignment_coverage_report_sha256": coverage.report_sha256,
            "plan_alignment_bundle_sha256": bundle_sha,
            "plan_alignment_semantic_manifest": str(semantic.semantic_manifest),
            "plan_alignment_semantic_manifest_sha256": (
                semantic.semantic_manifest_sha256
            ),
            "plan_alignment_semantic_bundle_sha256": semantic_bundle_sha,
            "plan_alignment_semantic_control": str(semantic.control_record),
            "plan_alignment_semantic_cell_dir": str(semantic.cell_dir),
            "plan_alignment_element_index": str(semantic.element_index),
            "plan_alignment_element_index_sha256": notary.stamp(
                semantic.element_index
            ).get("sha256"),
            "plan_alignment_atomization_projection": str(
                semantic.atomization_projection
            ),
            "plan_alignment_intent_fingerprint": semantic.intent_fingerprint,
            "plan_alignment_semantic_evidence": None,
            "plan_alignment_semantic_evidence_sha256": None,
            "plan_alignment_semantic_failure": None,
            "plan_alignment_ready_at": ready_at,
            "plan_alignment_decision": None,
            "plan_alignment_decision_artifact": None,
            "plan_alignment_decision_at": None,
        },
        event="plan_alignment_semantic_cell_pending",
        summary=(
            f"{node_address} passed the deterministic plan floor; semantic cell "
            f"{semantic_bundle_sha} is pending before L1 review"
        ),
    )
    return result


def _notify_parent_of_plan_alignment_ready(node_address: str, binding: dict) -> None:
    """Append one `design-submission` pointer line to the parent L1 inbox."""
    try:
        parent = (binding or {}).get("parent_address")
        if not parent or ledger.RUNTIME_ROOT is None:
            return
        parent_binding = ledger.read_binding(parent)
        if parent_binding is None or states.is_terminal(parent_binding.get("state")):
            return
        inbox = addressing.inbox_path(parent, ledger.RUNTIME_ROOT)
        ready_sha = (binding or {}).get("plan_alignment_ready_sha256")
        bundle_sha = (binding or {}).get("plan_alignment_bundle_sha256")
        if _inbox_has_line(
            inbox,
            type="design-submission",
            phase="plan_alignment",
            child=node_address,
            ready_artifact_sha256=ready_sha,
            bundle_sha256=bundle_sha,
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "from": "harnessd",
            "type": "design-submission",
            "phase": "plan_alignment",
            "child": node_address,
            "package": (binding or {}).get("plan_alignment_package"),
            "ready_artifact": (binding or {}).get("plan_alignment_ready_artifact"),
            "ready_artifact_sha256": ready_sha,
            "coverage_manifest": (binding or {}).get("plan_alignment_coverage_manifest"),
            "coverage_report": (binding or {}).get("plan_alignment_coverage_report"),
            "coverage_report_sha256": (
                binding or {}
            ).get("plan_alignment_coverage_report_sha256"),
            "bundle_sha256": bundle_sha,
            "semantic_manifest": (binding or {}).get(
                "plan_alignment_semantic_manifest"
            ),
            "semantic_manifest_sha256": (binding or {}).get(
                "plan_alignment_semantic_manifest_sha256"
            ),
            "semantic_bundle_sha256": (binding or {}).get(
                "plan_alignment_semantic_bundle_sha256"
            ),
            "semantic_evidence": (binding or {}).get(
                "plan_alignment_semantic_evidence"
            ),
            "semantic_evidence_sha256": (binding or {}).get(
                "plan_alignment_semantic_evidence_sha256"
            ),
            "required_elevation_delta": (binding or {}).get(
                "plan_alignment_required_elevation_delta"
            )
            or {"new": [], "changed": [], "cleared": []},
            "message": (
                "Deterministic coverage and the blind semantic cell are ready for "
                "L1 plan-alignment triage."
            ),
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort; readiness remains durable
        pass


def recover_plan_alignment_notification(node_address: str, binding: Optional[dict] = None) -> None:
    """Replay a lost plan-alignment readiness pointer from durable binding state."""
    live = binding or ledger.read_binding(node_address)
    if not live or live.get("plan_alignment_state") != PLAN_ALIGNMENT_STATE_READY:
        return
    _notify_parent_of_plan_alignment_ready(node_address, live)


def _semantic_report_name(role: str) -> str:
    return {
        "reconstruction-verification": "reconstruction-verification.json",
        "reconstruction-construction": "reconstruction-construction.json",
        "comparator": "adversarial-comparator.json",
        "coherence": "coherence.json",
        "atomization": "atomization.json",
    }[role]


def _semantic_brief_text(
    *,
    role: str,
    control: dict,
    input_manifest: Optional[Path],
    report_path: Path,
) -> str:
    dependency_note = (
        "The daemon is holding this seat until both blind reconstructions are committed."
        if role == plan_alignment_cell.COMPARATOR_ROLE
        else "This seat is in the first semantic wave."
    )
    # THE SEAT'S ROLE, NAMED (CELL-ROLE-DANGLING-MANIFEST). A cell seat's identity is deliberately
    # two halves (`brief._assemble_load_manifest`): the ordinary L2+ identity auto-loaded into
    # `.identity-prompt.md` by the adapter, PLUS the bounded semantic instruction read in place from
    # the load-manifest. This hand-rolled brief rendered the manifest section that every other
    # child's brief gets from `_brief_surface_lines` — so the second half was never named to the
    # seat, while this very brief told it to "read the role instruction in your load manifest" and
    # the auto-loaded prompt's trailer pointed at "your brief's load-manifest". Both pointers
    # dangled and the seat kept only the L2+ product-composition-reviewer half (run-5 cell:
    # atomization signed FAILED under that identity). `allow_uncalibrated=True` because this runs at
    # REGISTRATION and must not move the calibration gate, which belongs at the spawn path; the
    # manifest is selected by `role_variant`, which is set either way. Paths are absolutized (LR-3):
    # the pane boots in the node workspace, where a repo-relative path dangles.
    manifest = brief._assemble_load_manifest(
        _semantic_level_config(role, allow_uncalibrated=True)
    )
    return "\n".join(
        [
            f"# Plan-alignment semantic seat — {role}",
            "",
            f"- Cell bundle: `{control['cell_sha256']}`",
            f"- Role: `{role}`",
            f"- Scope prefixes: `{', '.join(control.get('scope_prefixes') or [])}`",
            f"- Input manifest: `{input_manifest or '(dependency-held; daemon writes before open)'}`",
            f"- Assigned JSON report: `{report_path}`",
            f"- Dependency posture: {dependency_note}",
            "",
            "## Identity — Load These Documents (read in place)",
            "",
            *[f"- {_absolutize_manifest_path(path)}" for path in manifest],
            "",
            "Read the role instruction named above and only the exact files listed in "
            "your input manifest. The filesystem sandbox enforces that window.",
            "",
            f"Write exactly one JSON report at `{report_path}`. Then sign DONE through the normal "
            "sign-off handshake. A deterministic shape defect refuses DONE and tells you what to repair.",
            "",
        ]
    )


def _semantic_binding(
    *,
    l2_address: str,
    parent_address: str,
    role: str,
    control: dict,
    input_paths: tuple[Path, ...],
    dependencies: tuple[str, ...],
) -> dict:
    address = plan_alignment_cell.seat_address(
        l2_address,
        str(control["cell_sha256"]),
        role,
    )
    subagent_id = "subagent-" + _sanitize_address(address)
    session_uuid = "registered-" + _sanitize_address(address)
    lease_epoch, last_applied_seq = reregister_identity_seed(address)
    owner_token = fencing.mint_owner_token(
        address, subagent_id, session_uuid, lease_epoch
    )
    node_dir = _child_node_dir(address)
    node_dir.mkdir(parents=True, exist_ok=True)
    input_manifest: Optional[Path] = None
    if role != plan_alignment_cell.COMPARATOR_ROLE:
        input_manifest = plan_alignment_cell.write_input_manifest(
            node_dir,
            role=role,
            control=control,
            paths=input_paths,
        )
    report_path = node_dir / _semantic_report_name(role)
    brief_path = node_dir / "brief.md"
    store.atomic_replace(
        brief_path,
        lambda handle: handle.write(
            _semantic_brief_text(
                role=role,
                control=control,
                input_manifest=input_manifest,
                report_path=report_path,
            )
        ),
    )
    exact_paths = [str(path) for path in input_paths]
    allowlist_sha = hashlib.sha256(
        json.dumps(sorted(exact_paths), separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "node_address": address,
        "parent_address": parent_address,
        "level": "L2+",
        "role_variant": f"plan-alignment#{role}",
        "subagent_id": subagent_id,
        "session_uuid": session_uuid,
        "tmux_target": addressing.session_name_for(address),
        "state": "planned",
        "generation": 0,
        "lease_epoch": lease_epoch,
        "owner_token": owner_token,
        "last_applied_seq": last_applied_seq,
        "last_inbox_acked_offset": _current_inbox_size(address),
        "liveness_state": "claimed",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "workspace": str(node_dir),
        "spec_pointer": str(brief_path),
        "frozen_acceptance_ref": None,
        "child_purpose": "plan_alignment_semantic_cell",
        "semantic_cell_for": l2_address,
        "semantic_cell_role": role,
        "semantic_cell_bundle_sha256": control["cell_sha256"],
        "semantic_cell_control": str(
            Path(control["node_dir"])
            / "plan-alignment"
            / str(control["cell_sha256"])[:16]
            / "control"
            / "cell.json"
        ),
        "semantic_dependencies": sorted(dependencies),
        "semantic_input_manifest": (
            str(input_manifest) if input_manifest is not None else None
        ),
        "semantic_exact_read_paths": exact_paths,
        "semantic_exact_allowlist_sha256": allowlist_sha,
        "semantic_report": str(report_path),
        "verdict_authority": False,
    }


def _register_semantic_cell(node_address: str, live: dict, control: dict) -> dict[str, dict]:
    """Pre-register the whole cohort before any semantic actor opens."""
    parent = str(live.get("parent_address") or "")
    if not parent:
        raise ValueError("semantic cell requires the owning L1 parent")
    recon_addresses = tuple(
        plan_alignment_cell.seat_address(
            node_address,
            str(control["cell_sha256"]),
            role,
        )
        for role in (
            "reconstruction-verification",
            "reconstruction-construction",
        )
    )
    bindings: dict[str, dict] = {}
    failed_incarnations: dict[str, dict] = {}
    for role in plan_alignment_cell.ALL_ROLES:
        address = plan_alignment_cell.seat_address(
            node_address,
            str(control["cell_sha256"]),
            role,
        )
        existing = ledger.read_binding(address)
        if (
            existing is not None
            and existing.get("semantic_cell_bundle_sha256") == control["cell_sha256"]
        ):
            if existing.get("state") != "failed" or respawn_parked(existing):
                bindings[role] = existing
                continue
            failed_incarnations[role] = existing
        input_paths = (
            ()
            if role == plan_alignment_cell.COMPARATOR_ROLE
            else plan_alignment_cell.input_paths_for_role(role, control=control)
        )
        dependencies = (
            recon_addresses
            if role == plan_alignment_cell.COMPARATOR_ROLE
            else ()
        )
        bindings[role] = _semantic_binding(
            l2_address=node_address,
            parent_address=parent,
            role=role,
            control=control,
            input_paths=input_paths,
            dependencies=dependencies,
        )
        inherit_respawn_accounting(bindings[role], existing)
        if role == "atomization" and control.get("atomization_cache"):
            cache = control["atomization_cache"]
            bindings[role].update(
                {
                    "state": "done",
                    "liveness_state": "idle",
                    "terminal_signal": "DONE",
                    "terminal_signal_at": clock.now_utc(),
                    "semantic_report": cache["report"],
                    "semantic_cached": True,
                    "semantic_cached_report_sha256": cache["report_sha256"],
                }
            )

    if ledger.RUNTIME_ROOT is None:
        raise RuntimeError("semantic cell registration requires ledger.RUNTIME_ROOT")
    lock_path = Path(ledger.RUNTIME_ROOT) / executor.LOCK_FILENAME
    with store.file_lock(lock_path, shared=False):
        live_map = dict(ledger.all_nodes())
        for role, binding in bindings.items():
            address = binding["node_address"]
            current = live_map.get(address)
            failed = failed_incarnations.get(role)
            if failed is not None:
                if (
                    current is None
                    or current.get("state") != "failed"
                    or current.get("semantic_cell_bundle_sha256")
                    != control["cell_sha256"]
                    or current.get("lease_epoch") != failed.get("lease_epoch")
                    or current.get("owner_token") != failed.get("owner_token")
                ):
                    bindings[role] = current or binding
                    continue
                kill_stale_pane(current.get("tmux_target"))
                purge_stale_seat_artifacts(address)
                live_map[address] = binding
                continue
            if (
                current is not None
                and current.get("semantic_cell_bundle_sha256") == control["cell_sha256"]
            ):
                bindings[role] = current
                continue
            live_map[address] = binding
        ledger.write_binding(live_map, _lock_held=True)
    return bindings


def _semantic_level_config(role: str, *, allow_uncalibrated: bool = False):
    configured = config.SEMANTIC_CELL_LEVEL_CONFIGS.get(role)
    if configured is None and not allow_uncalibrated:
        raise ValueError(
            f"SEMANTIC-CELL-MODEL-UNCALIBRATED:{role}: joint owner+director "
            "calibration has not installed a model/runtime registry row"
        )
    base = configured or config.LevelConfig.for_level("L2+")
    # BLINDERS MODE — the production resolution, matching the other two launch-path assemblers
    # (`commissioning.build_runtime`, `config.get_level_config`): default observe, enforce only on
    # the explicit HARNESS_BLINDERS_MODE opt-in. This field was hardcoded to ENFORCE by
    # 852302d ("[owner-docket/q4] Enforce plan-alignment semantic cell"), whose ORIGINAL RATIONALE
    # was the cell's blind window — blindness is the check, so a cell seat must never open able to
    # read the spec it is reconstructing. That rationale is fully served WITHOUT this override and
    # always was: `_produce_containment` routes any seat carrying `semantic_exact_read_paths` to
    # `blinders.derive_exact_policy`, which stamps mode=ENFORCE unconditionally and never consults
    # this field. The hardcode therefore bought no blindness — it only mislabeled the seat's
    # deployment posture in the LevelConfig, overriding a parked owner decision on the one branch
    # it does reach (the general-policy fallback). It is not what jailed the run-5 cell seats.
    return dataclasses.replace(
        base,
        role_variant=f"plan-alignment#{role}",
        blinders_mode=config.production_blinders_mode(),
    )


def _open_semantic_seat(binding: dict, *, allow_uncalibrated: bool = False):
    manifest = binding.get("semantic_input_manifest")
    if not manifest:
        return _result_failed(
            "semantic_input_manifest_missing",
            tmux_target=binding["node_address"],
        )
    defects = plan_alignment_cell.validate_input_manifest(Path(manifest))
    if defects:
        return _result_failed(
            "semantic_input_manifest_invalid",
            tmux_target=binding["node_address"],
        )
    role = str(binding["semantic_cell_role"])
    return claim_and_spawn(
        binding["node_address"],
        expected_state="planned",
        expected_generation=binding["generation"],
        expected_owner_token=binding.get("owner_token"),
        level_config=_semantic_level_config(
            role,
            allow_uncalibrated=allow_uncalibrated,
        ),
    )


def _semantic_result(ok: bool, binding: Optional[dict], *errors: str):
    return executor.TransitionResult(
        ok=ok,
        errors=[str(error) for error in errors if error],
        warnings=[],
        binding=binding,
    )


def _record_semantic_failure(node_address: str, live: dict, errors: Iterable[str]):
    unique = list(dict.fromkeys(str(error) for error in errors if str(error)))
    fingerprint = hashlib.sha256(
        json.dumps(unique, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if live.get("plan_alignment_semantic_failure_sha256") == fingerprint:
        return _semantic_result(False, live, *unique)
    result = executor.record_admission(
        node_address,
        expected_owner_token=live.get("owner_token"),
        delta={
            "plan_alignment_semantic_failure": unique,
            "plan_alignment_semantic_failure_sha256": fingerprint,
            "plan_alignment_semantic_failure_at": clock.now_utc(),
        },
        event="plan_alignment_semantic_cell_failed",
        summary=(
            f"semantic cell for {node_address} is not dispatchable/current: "
            + " | ".join(unique)
        ),
    )
    if result is None:
        return _semantic_result(False, ledger.read_binding(node_address), *unique)
    return _semantic_result(False, result.binding, *unique)


def reconcile_plan_alignment_cell(
    node_address: str,
    *,
    allow_uncalibrated: bool = False,
):
    """Idempotently dispatch dependencies and finalize one pending semantic cell."""
    live = ledger.read_binding(node_address)
    if live is None:
        return _semantic_result(False, None, f"no binding for {node_address!r}")
    if live.get("plan_alignment_state") == PLAN_ALIGNMENT_STATE_READY:
        _notify_parent_of_plan_alignment_ready(node_address, live)
        return _semantic_result(True, live)
    if live.get("plan_alignment_state") != PLAN_ALIGNMENT_STATE_SEMANTIC_PENDING:
        return _semantic_result(
            False,
            live,
            f"{node_address!r} has no pending plan-alignment semantic cell",
        )
    control_path = Path(str(live.get("plan_alignment_semantic_control") or ""))
    try:
        control = plan_alignment_cell.load_control(control_path)
    except ValueError as exc:
        return _record_semantic_failure(
            node_address,
            live,
            [f"SEMANTIC-CELL-CONTROL:{exc}"],
        )
    calibration_defects = plan_alignment_cell.instruction_calibration_defects(
        allow_uncalibrated=allow_uncalibrated,
    )
    if calibration_defects:
        return _record_semantic_failure(
            node_address,
            live,
            calibration_defects,
        )

    try:
        seats = _register_semantic_cell(node_address, live, control)
    except (OSError, RuntimeError, ValueError) as exc:
        return _record_semantic_failure(
            node_address,
            live,
            [f"SEMANTIC-CELL-REGISTER:{exc}"],
        )

    for role in plan_alignment_cell.FIRST_WAVE_ROLES:
        binding = ledger.read_binding(seats[role]["node_address"]) or seats[role]
        if binding.get("state") != "planned":
            continue
        try:
            opened = _open_semantic_seat(
                binding,
                allow_uncalibrated=allow_uncalibrated,
            )
        except ValueError as exc:
            return _record_semantic_failure(node_address, live, [str(exc)])
        if opened is not None and not getattr(opened, "ok", False):
            return _record_semantic_failure(
                node_address,
                live,
                [
                    f"SEMANTIC-CELL-SEAT-OPEN:{role}:"
                    f"{getattr(opened, 'failure_class', None) or 'unknown'}"
                ],
            )

    seats = {
        role: ledger.read_binding(binding["node_address"]) or binding
        for role, binding in seats.items()
    }
    reconstruction_roles = (
        "reconstruction-verification",
        "reconstruction-construction",
    )
    if all(seats[role].get("state") == "done" for role in reconstruction_roles):
        report_paths = {
            role: Path(str(seats[role]["semantic_report"]))
            for role in reconstruction_roles
        }
        report_defects: list[str] = []
        for role, path in report_paths.items():
            report_defects.extend(
                plan_alignment_cell.validate_report_path(
                    role,
                    path,
                    control_path=control_path,
                )
            )
        if report_defects:
            return _record_semantic_failure(
                node_address,
                live,
                report_defects,
            )
        for path in report_paths.values():
            notary.stamp(path, read_only=True)
        comparator = seats[plan_alignment_cell.COMPARATOR_ROLE]
        if comparator.get("state") == "planned":
            try:
                exact_paths = plan_alignment_cell.input_paths_for_role(
                    plan_alignment_cell.COMPARATOR_ROLE,
                    control=control,
                    report_paths=report_paths,
                )
                manifest = plan_alignment_cell.write_input_manifest(
                    Path(comparator["workspace"]),
                    role=plan_alignment_cell.COMPARATOR_ROLE,
                    control=control,
                    paths=exact_paths,
                )
                allowlist_sha = hashlib.sha256(
                    json.dumps(
                        sorted(str(path) for path in exact_paths),
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                updated = executor.record_admission(
                    comparator["node_address"],
                    expected_owner_token=comparator.get("owner_token"),
                    delta={
                        "semantic_input_manifest": str(manifest),
                        "semantic_exact_read_paths": [
                            str(path) for path in exact_paths
                        ],
                        "semantic_exact_allowlist_sha256": allowlist_sha,
                        "semantic_dependencies_satisfied_at": clock.now_utc(),
                    },
                    event="semantic_comparator_released",
                    summary=(
                        "both blind reconstructions are current; comparator exact inputs "
                        "were frozen and its dependency hold was released"
                    ),
                )
                comparator = updated.binding if updated is not None else comparator
                opened = _open_semantic_seat(
                    comparator,
                    allow_uncalibrated=allow_uncalibrated,
                )
                if opened is not None and not getattr(opened, "ok", False):
                    return _record_semantic_failure(
                        node_address,
                        live,
                        [
                            "SEMANTIC-CELL-SEAT-OPEN:comparator:"
                            f"{getattr(opened, 'failure_class', None) or 'unknown'}"
                        ],
                    )
            except (OSError, RuntimeError, ValueError) as exc:
                return _record_semantic_failure(
                    node_address,
                    live,
                    [f"SEMANTIC-COMPARATOR-RELEASE:{exc}"],
                )

    seats = {
        role: ledger.read_binding(binding["node_address"]) or binding
        for role, binding in seats.items()
    }
    if not all(
        seats[role].get("state") == "done"
        for role in plan_alignment_cell.ALL_ROLES
    ):
        return _semantic_result(True, ledger.read_binding(node_address) or live)

    report_paths = {
        role: Path(str(seats[role]["semantic_report"]))
        for role in plan_alignment_cell.ALL_ROLES
    }
    evidence_path, evidence_sha, defects = plan_alignment_cell.build_evidence_index(
        control_path=control_path,
        report_paths=report_paths,
    )
    if defects or evidence_path is None or evidence_sha is None:
        return _record_semantic_failure(node_address, live, defects)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    elevations = evidence.get("required_elevations") or []
    elevation_delta = plan_alignment_cell.elevation_delta(
        live.get("plan_alignment_required_elevations") or [],
        elevations,
    )
    atomization_report = report_paths["atomization"]
    atomization_stamp = notary.stamp(atomization_report)
    atomization_cache = {
        "intent_fingerprint": control["intent_fingerprint"],
        "report": str(atomization_report),
        "report_sha256": atomization_stamp.get("sha256"),
    }
    final = executor.record_admission(
        node_address,
        expected_owner_token=(ledger.read_binding(node_address) or live).get(
            "owner_token"
        ),
        delta={
            "plan_alignment_state": PLAN_ALIGNMENT_STATE_READY,
            "plan_alignment_semantic_evidence": str(evidence_path),
            "plan_alignment_semantic_evidence_sha256": evidence_sha,
            "plan_alignment_required_elevations": elevations,
            "plan_alignment_required_elevation_delta": elevation_delta,
            "plan_alignment_required_elevations_sha256": hashlib.sha256(
                json.dumps(
                    elevations,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "plan_alignment_semantic_ready_at": clock.now_utc(),
            "plan_alignment_atomization_cache": atomization_cache,
            "plan_alignment_semantic_failure": None,
            "plan_alignment_semantic_failure_sha256": None,
        },
        event="plan_alignment_semantic_cell_ready",
        summary=(
            f"semantic cell {control['cell_sha256']} completed; L1 receives one "
            "content-addressed evidence wake"
        ),
    )
    if final is not None and final.ok:
        _notify_parent_of_plan_alignment_ready(node_address, final.binding)
        return _semantic_result(True, final.binding)
    return final


def reconcile_plan_alignment_elevations(node_address: str):
    """Adopt L1-authored one-finding/one-question markers onto the L1 binding."""
    live = ledger.read_binding(node_address)
    if (
        live is None
        or live.get("plan_alignment_state") != PLAN_ALIGNMENT_STATE_READY
        or not live.get("plan_alignment_semantic_evidence")
        or not live.get("plan_alignment_semantic_evidence_sha256")
    ):
        return None
    parent_address = live.get("parent_address")
    parent = ledger.read_binding(parent_address) if parent_address else None
    if parent is None or parent.get("level") != "L1":
        return _semantic_result(
            False,
            live,
            f"plan-alignment owner questions require a live L1 parent for {node_address}",
        )
    markers, defects = plan_alignment_cell.read_elevation_markers(
        evidence_path=Path(live["plan_alignment_semantic_evidence"]),
        evidence_sha256=str(live["plan_alignment_semantic_evidence_sha256"]),
    )
    if defects:
        return _record_semantic_failure(node_address, live, defects)
    existing = copy.deepcopy(parent.get("plan_alignment_owner_questions") or {})
    changed = False
    for fingerprint, marker in markers.items():
        question_id = marker["question_id"]
        current = existing.get(question_id)
        next_row = {
            **marker,
            "cell_for": node_address,
            "status": "open",
            "adopted_at": clock.now_utc(),
            "answered_at": None,
            "answer_artifact": None,
            "answer_sha256": None,
            "decision": None,
        }
        if (
            isinstance(current, dict)
            and current.get("finding_fingerprint") == fingerprint
            and current.get("semantic_evidence_sha256")
            == marker["semantic_evidence_sha256"]
            and current.get("marker_sha256") == marker["marker_sha256"]
        ):
            continue
        if (
            isinstance(current, dict)
            and current.get("finding_fingerprint") == fingerprint
            and current.get("intent_fingerprint") == marker["intent_fingerprint"]
            and current.get("status") in {"confirmed", "rejected"}
        ):
            next_row.update(
                {
                    "status": current["status"],
                    "answered_at": current.get("answered_at"),
                    "answer_artifact": current.get("answer_artifact"),
                    "answer_sha256": current.get("answer_sha256"),
                    "decision": current.get("decision"),
                    "answer_reused_by_fingerprint": True,
                }
            )
        existing[question_id] = next_row
        changed = True
    required_fingerprints = {
        str(row.get("fingerprint"))
        for row in live.get("plan_alignment_required_elevations") or []
        if isinstance(row, dict) and row.get("fingerprint")
    }
    for question_id, current in existing.items():
        if (
            not isinstance(current, dict)
            or current.get("cell_for") != node_address
            or current.get("finding_fingerprint") in required_fingerprints
            or current.get("status") == "cleared"
        ):
            continue
        current.update(
            {
                "status": "cleared",
                "cleared_at": clock.now_utc(),
                "cleared_by_semantic_evidence_sha256": live.get(
                    "plan_alignment_semantic_evidence_sha256"
                ),
            }
        )
        existing[question_id] = current
        changed = True
    if not changed:
        return _semantic_result(True, parent)
    result = executor.record_admission(
        parent_address,
        expected_owner_token=parent.get("owner_token"),
        delta={"plan_alignment_owner_questions": existing},
        event="plan_alignment_owner_questions_adopted",
        summary=(
            f"L1 authored {len(markers)} one-finding/one-question plan-alignment "
            f"elevation marker(s) for {node_address}"
        ),
    )
    return result


def _planned_review_binding(
    *,
    producer_address: str,
    parent_address: str,
    review_address: str,
    review_level: str,
) -> dict:
    """Create the planned co-located review gate binding for a producer slot."""
    try:
        review_level_config = config.LevelConfig.for_level(review_level)
    except KeyError:
        review_level_config = None
    role_variant = (
        getattr(review_level_config, "role_variant", None)
        or f"{review_level}#review"
    )
    previous = ledger.read_binding(review_address)
    if respawn_parked(previous):
        return previous
    if previous is not None and previous.get("state") != "running":
        archive_prior_incarnation_surface(review_address, previous, include_work_forms=False)
    subagent_id = "subagent-" + _sanitize_address(review_address)
    session_uuid = "registered-" + _sanitize_address(review_address)
    lease_epoch, last_applied_seq = reregister_identity_seed(review_address)
    owner_token = fencing.mint_owner_token(
        review_address, subagent_id, session_uuid, lease_epoch
    )
    purge_stale_seat_artifacts(review_address)
    node_dir = _child_node_dir(review_address)
    binding = {
        "node_address": review_address,
        "parent_address": parent_address,
        "level": review_level,
        "role_variant": role_variant,
        "subagent_id": subagent_id,
        "session_uuid": session_uuid,
        "tmux_target": addressing.session_name_for(review_address),
        "state": "planned",
        "generation": 0,
        "lease_epoch": lease_epoch,
        "owner_token": owner_token,
        "last_applied_seq": last_applied_seq,
        "last_inbox_acked_offset": _current_inbox_size(review_address),
        "liveness_state": "claimed",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "workspace": str(node_dir),
        "spec_pointer": str(node_dir / "brief.md"),
        "frozen_acceptance_ref": str(node_dir / "acceptance.md"),
        "gate_for": producer_address,
    }
    if previous is not None and isinstance(previous.get("messages"), dict):
        binding["messages"] = copy.deepcopy(previous["messages"])
        if previous.get("message_last_id"):
            binding["message_last_id"] = previous["message_last_id"]
    return inherit_respawn_accounting(binding, previous)


def _review_check_address_for(producer_address: str, gate_id: str, slug: str) -> str:
    producer_path, _seat = addressing.split_address(producer_address)
    return f"{producer_path}/reviews/{gate_id}/reviewers/{slug}#exec"


def _planned_review_check_binding(
    *,
    check_address: str,
    review_address: str,
    review_binding: dict,
    producer_address: str,
    gate_id: str,
    spec: dict,
    report_path: Path,
    brief_path: Path,
) -> dict:
    """Create a planned auxiliary review-check binding.

    Review-check seats are real harness actors with fresh context, but they are not gate verdict
    seats. They intentionally do not set ``gate_for`` or ``gate_required``; only the co-located
    gate lead routes PASS/BOUNCE/ESCALATE for the candidate.
    """
    review_level = (review_binding.get("level") or "L4+").strip()
    previous = ledger.read_binding(check_address)
    if previous is not None:
        if respawn_parked(previous):
            return previous
        if not states.is_terminal(previous.get("state")):
            return previous
        archive_prior_incarnation_surface(check_address, previous, include_work_forms=False)
    subagent_id = "subagent-" + _sanitize_address(check_address)
    session_uuid = "registered-" + _sanitize_address(check_address)
    lease_epoch, last_applied_seq = reregister_identity_seed(check_address)
    owner_token = fencing.mint_owner_token(
        check_address, subagent_id, session_uuid, lease_epoch
    )
    purge_stale_seat_artifacts(check_address)
    node_dir = _child_node_dir(check_address)
    role_variant = f"{review_level}#review-check"
    binding = {
        "node_address": check_address,
        "parent_address": review_address,
        "level": review_level,
        "role_variant": role_variant,
        "subagent_id": subagent_id,
        "session_uuid": session_uuid,
        "tmux_target": addressing.session_name_for(check_address),
        "state": "planned",
        "generation": 0,
        "lease_epoch": lease_epoch,
        "owner_token": owner_token,
        "last_applied_seq": last_applied_seq,
        "last_inbox_acked_offset": _current_inbox_size(check_address),
        "liveness_state": "claimed",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "workspace": str(node_dir),
        "spec_pointer": str(brief_path),
        "frozen_acceptance_ref": None,
        "review_check_for": review_address,
        "review_check_candidate": producer_address,
        "gate_id": gate_id,
        "gate_review_packet": (ledger.read_binding(producer_address) or {}).get("gate_review_packet"),
        "gate_review_dir": str(report_path.parents[2]),
        "review_check_axis": spec.get("slug"),
        "review_check_label": spec.get("label"),
        "review_check_report": str(report_path),
        "verdict_authority": False,
    }
    if spec.get("probe_instruction"):
        binding.update(
            {
                "product_probe_role": str(spec["probe_instruction"]),
                "product_probe_instruction": str(
                    product_probes.INSTRUCTION_PATHS[str(spec["probe_instruction"])]
                ),
                "product_probe_roster": str(spec["probe_roster"]),
                "product_probe_roster_sha256": str(spec["probe_roster_sha256"]),
                "product_probe_instance_root": str(spec["probe_instance_root"]),
                "product_probe_instance_manifest": str(
                    spec["probe_instance_manifest"]
                ),
                "product_probe_instance_manifest_sha256": str(
                    spec["probe_instance_manifest_sha256"]
                ),
                "product_probe_exact_read_paths": [
                    str(spec["probe_roster"]),
                    str(spec["probe_instance_manifest"]),
                    str((ledger.read_binding(producer_address) or {}).get(
                        "gate_candidate_artifact_manifest"
                    )),
                    str((ledger.read_binding(producer_address) or {}).get(
                        "gate_review_packet"
                    )),
                ],
            }
        )
    if previous is not None and isinstance(previous.get("messages"), dict):
        binding["messages"] = copy.deepcopy(previous["messages"])
        if previous.get("message_last_id"):
            binding["message_last_id"] = previous["message_last_id"]
    return inherit_respawn_accounting(binding, previous)


def _register_review_check_binding(
    *,
    check_address: str,
    binding: dict,
    brief_text: str,
) -> dict:
    """Register a planned auxiliary check reviewer and pre-author its task brief."""
    runtime_root = ledger.RUNTIME_ROOT
    if runtime_root is None:
        raise RuntimeError("review-check registration requires ledger.RUNTIME_ROOT")
    live = ledger.read_binding(check_address)
    if live is not None:
        if respawn_parked(live):
            return live
        if not states.is_terminal(live.get("state")):
            return live
        if (
            live.get("state") == "done"
            and live.get("gate_id") == binding.get("gate_id")
            and live.get("review_check_report") == binding.get("review_check_report")
        ):
            return live
        kill_stale_pane(live.get("tmux_target"))

    node_dir = _child_node_dir(check_address)
    node_dir.mkdir(parents=True, exist_ok=True)
    brief_path = Path(binding["spec_pointer"])
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    store.atomic_replace(brief_path, lambda h: (h.write(brief_text), h.write("\n")))

    lock_path = Path(runtime_root) / executor.LOCK_FILENAME
    with store.file_lock(lock_path, shared=False):
        live_map = dict(ledger.all_nodes())
        current = live_map.get(check_address)
        if current is not None and not states.is_terminal(current.get("state")):
            return current
        live_map[check_address] = binding
        ledger.write_binding(live_map, _lock_held=True)
    return binding


def _product_probe_level_config(
    role: str,
    *,
    allow_uncalibrated: bool = False,
):
    configured = config.PRODUCT_PROBE_LEVEL_CONFIGS.get(role)
    if configured is None and not allow_uncalibrated:
        raise ValueError(
            f"{product_probes.MODEL_UNCALIBRATED_DEFECT}:{role}: joint owner+director "
            "calibration has not installed a model/runtime registry row"
        )
    base = configured or config.LevelConfig.for_level("L2+")
    # Same production resolution, same reasoning as `_semantic_level_config` above: the probe's real
    # isolation is the exact policy `_produce_containment` derives from
    # `product_probe_exact_read_paths`, so this field only has to stop misreporting the deployment
    # posture. Cloned from the cell helper by 3b42659 ("[owner-docket/q5] Add L2+ product probe
    # seats"); product probes have never dispatched live, so this half never reached a real seat.
    return dataclasses.replace(
        base,
        role_variant=f"product-probe#{role}",
        blinders_mode=config.production_blinders_mode(),
    )


def _review_dispatch_blocker_payload(gate_dir: Path, defects: list[str]) -> dict:
    plan = gate_dir / "review-plan.md"
    try:
        text = plan.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return {
        "defect": " | ".join(dict.fromkeys(str(value) for value in defects)),
        "plan_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "plan_path": str(plan),
        "gate_dir": str(gate_dir),
    }


def dispatch_review_check_seats(
    review_address: str,
    *,
    allow_uncalibrated: bool = False,
):
    """Open missing first-class review-check seats for a FULL higher-gate review plan.

    The review lead owns the plan and final verdict. The daemon calls this after the lead writes
    `Review Mode: FULL`; this helper materializes the level-specific bounded
    reviewer roster as child actors of the lead. It is idempotent on gate id +
    report path. Product probes expose one
    explicit uncalibrated test seam; production callers use the fail-closed default.
    """
    from harnessd import review_dispatch

    review = ledger.read_binding(review_address)
    if not review or review.get("state") != "running":
        return []
    if not review_dispatch.is_higher_review_gate(review):
        return []
    producer_address = review.get("gate_for")
    producer = ledger.read_binding(producer_address) if producer_address else None
    if not producer or producer.get("gate_state") != "candidate_submitted":
        return []
    gate_id = str(producer.get("gate_id") or "").strip()
    gate_dir_raw = producer.get("gate_review_dir")
    if not gate_id or not gate_dir_raw:
        return []
    if not review_dispatch.review_checks_ready_for_dispatch(review_address, review):
        blocker = review_dispatch.review_check_dispatch_blocker(review_address, review)
        if blocker:
            _notify_review_dispatch_defect(review_address, gate_id, blocker)
        return []
    gate_dir = Path(gate_dir_raw)
    canonical_level = (review.get("level") or "").strip()
    is_product_gate = canonical_level in {"L2", "L2+"}

    try:
        base_config = config.get_level_config((review.get("level") or "").strip())
    except Exception:
        return []
    check_config = dataclasses.replace(
        base_config,
        role_variant=f"{base_config.level}#review-check",
    )

    specs = [dict(spec) for spec in review_dispatch.required_review_check_specs(review)]
    if is_product_gate:
        calibration_defects = list(
            product_probes.instruction_calibration_defects(
                allow_uncalibrated=allow_uncalibrated,
            )
        )
        if not allow_uncalibrated:
            for role in product_probes.PROBE_SLUGS:
                try:
                    _product_probe_level_config(role)
                except ValueError as exc:
                    calibration_defects.append(str(exc))
        if calibration_defects:
            _notify_review_dispatch_defect(
                review_address,
                gate_id,
                _review_dispatch_blocker_payload(gate_dir, calibration_defects),
            )
            return []
        roster = product_probes.prepare_probe_roster(
            producer_address=producer_address,
            producer_binding=producer,
            gate_dir=gate_dir,
        )
        if not roster.ok:
            _notify_review_dispatch_defect(
                review_address,
                gate_id,
                _review_dispatch_blocker_payload(gate_dir, list(roster.defects)),
            )
            return []
        for spec in specs:
            role = spec.get("probe_instruction")
            if not role:
                continue
            instance = product_probes.prepare_disposable_instance(
                producer_address=producer_address,
                producer_binding=producer,
                gate_dir=gate_dir,
                probe_slug=str(role),
            )
            if not instance.ok:
                _notify_review_dispatch_defect(
                    review_address,
                    gate_id,
                    _review_dispatch_blocker_payload(
                        gate_dir,
                        list(instance.defects),
                    ),
                )
                return []
            spec.update(
                {
                    "probe_roster": str(roster.path),
                    "probe_roster_sha256": roster.sha256,
                    "probe_instance_root": str(instance.root),
                    "probe_instance_manifest": str(instance.manifest),
                    "probe_instance_manifest_sha256": instance.manifest_sha256,
                }
            )

    # Register the complete cohort before the first actor can run.  The existing
    # ledger-derived barrier therefore sees all four/five members even if one seat
    # finishes immediately after spawn.
    registrations: list[tuple[dict, dict]] = []
    for spec in specs:
        check_address = _review_check_address_for(producer_address, gate_id, str(spec["slug"]))
        report_path = review_dispatch.review_check_report_path(gate_dir, spec)
        brief_path = report_path.parent / "brief.md"
        brief_text = review_dispatch.render_review_check_brief(
            review_address=review_address,
            review_binding=review,
            producer_address=producer_address,
            gate_id=gate_id,
            gate_dir=gate_dir,
            spec=spec,
        )
        binding = _planned_review_check_binding(
            check_address=check_address,
            review_address=review_address,
            review_binding=review,
            producer_address=producer_address,
            gate_id=gate_id,
            spec=spec,
            report_path=report_path,
            brief_path=brief_path,
        )
        registered = _register_review_check_binding(
            check_address=check_address,
            binding=binding,
            brief_text=brief_text,
        )
        registrations.append((spec, registered))

    results = []
    for spec, registered in registrations:
        if registered.get("state") != "planned":
            continue
        check_address = str(registered["node_address"])
        role = spec.get("probe_instruction")
        level_config = (
            _product_probe_level_config(
                str(role),
                allow_uncalibrated=allow_uncalibrated,
            )
            if role
            else check_config
        )
        result = claim_and_spawn(
            check_address,
            expected_state="planned",
            expected_generation=registered["generation"],
            expected_owner_token=registered.get("owner_token"),
            level_config=level_config,
        )
        results.append(result)
        if (
            result is not None
            and getattr(result, "ok", True) is False
            and getattr(result, "failure_class", None) not in {"claim_lost", "paused_subtree"}
        ):
            _fail_gate_for_producer(
                producer_address,
                producer,
                review_address,
                failure_reason="review_check_open_failed",
                failure_class=getattr(result, "failure_class", None) or "review_check_open_failed",
                detail=f"failed opening review-check seat {check_address}",
            )
            break
    return results


def _notify_review_dispatch_defect(review_address: str, gate_id: str, blocker: dict) -> None:
    """Tell a review lead why its FULL plan did not dispatch reviewer seats."""
    if ledger.RUNTIME_ROOT is None:
        return
    defect = str((blocker or {}).get("defect") or "").strip()
    plan_sha256 = str((blocker or {}).get("plan_sha256") or "").strip()
    if not defect or not plan_sha256:
        return
    inbox = addressing.inbox_path(review_address, ledger.RUNTIME_ROOT)
    if _inbox_has_line(
        inbox,
        type="review_dispatch_defect",
        gate_id=gate_id,
        plan_sha256=plan_sha256,
        defect=defect,
    ):
        return
    inbox.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "from": "harnessd",
        "type": "review_dispatch_defect",
        "gate_id": gate_id,
        "plan_sha256": plan_sha256,
        "plan_path": blocker.get("plan_path"),
        "defect": defect,
        "message": (
            "Review-check dispatch is waiting on review-plan.md. "
            f"Defect: {defect}. Fix review-plan.md and keep `Review Mode: FULL`; "
            "the daemon will open the review-check seats after the exact dispatch "
            "contract is satisfied."
        ),
        "ts": clock.now_utc(),
    })
    with inbox.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _register_child(
    child_address: str,
    parent_address: str,
    child_level_config,
    runtime_root: Path,
    child_metadata: Optional[dict] = None,
) -> Optional[dict]:
    """Register the child as a fresh ``planned`` slot UNDER the parent (mirror _register_l1_root).

    parent_address is SET (the supervision-tree edge — only the L1 root is parentless). generation=0,
    lease_epoch=1, a minted owner_token, written via the single-writer lock-held ledger path under the
    EX lock (the §2.10-sanctioned lock-held seeding the suite uses). SAFE if the child already exists:
    if a NON-planned child binding is already present (e.g. a RUNNING child from a prior spawn) this
    does NOT clobber it — it returns the LIVE binding so the caller's claim (expected_state=planned)
    loses against it (single-owner; no double-register of a live child). A child that is absent (or
    already terminal/planned) is (re-)registered as a fresh planned slot the claim can win.

    Returns the registered (or pre-existing-live) child binding.
    """
    live = ledger.read_binding(child_address)
    if respawn_parked(live):
        return live
    # SINGLE-OWNER: do NOT overwrite a live non-planned child (a running/claimed/spawning incarnation).
    # Returning it lets the caller's planned-expected claim lose against it (no double-open, F35/F-024).
    if live is not None and not states.is_terminal(live.get("state")) and live.get("state") != "planned":
        return live
    fresh_from_terminal = live is not None and states.is_terminal(live.get("state"))
    if fresh_from_terminal:
        archive_prior_incarnation_surface(child_address, live, include_work_forms=True)
    if live is not None and states.is_terminal(live.get("state")):
        # LT-4/INT-1: a TERMINAL child being re-registered (e.g. a watchdog-FAILED leaf the parent
        # re-spawns) may still hold its WARM pane — no production path killed it at FAILED/collapse.
        # The deterministic session name means the fresh claim_and_spawn below would collide
        # ('duplicate session'); tear the dead incarnation's recorded pane down first (idempotent).
        kill_stale_pane(live.get("tmux_target"))

    level = getattr(child_level_config, "level", None) or "L3"
    role_variant = getattr(child_level_config, "role_variant", None) or level
    subagent_id = "subagent-" + _sanitize_address(child_address)
    session_uuid = "registered-" + _sanitize_address(child_address)
    # SM-1/SM-2: a RE-register (a terminal child being respawned -- the ONLY recovery path for a
    # failed child) must never reset the fence or the replay watermark: the epoch seeds at
    # prior+1 (so the next claim's re-minted token can never equal a prior incarnation's -- the
    # leftover .signal fence holds) and last_applied_seq seeds at the current max WAL seq (so
    # boot replay never re-applies the dead incarnation's chain). A fresh child seeds (1, 0).
    lease_epoch, last_applied_seq = reregister_identity_seed(child_address)
    generation = 0
    owner_token = fencing.mint_owner_token(child_address, subagent_id, session_uuid, lease_epoch)
    # SM-1 belt-and-braces: drop the dead incarnation's seat artifacts before the slot reopens.
    purge_stale_seat_artifacts(child_address)
    node_dir = _child_node_dir(child_address)
    binding = {
        "node_address": child_address,
        "parent_address": parent_address,  # the supervision-tree edge — SET (not null), DAEMON §7
        "level": level,
        "subagent_id": subagent_id,
        "session_uuid": session_uuid,
        # The PRE-SPAWN placeholder: the canonical session name (F18 — a name tmux will not
        # rename). STEP4 overwrites it with the full '<session>:<window>.<pane>' triple tmux
        # reports once the pane actually opens.
        "tmux_target": addressing.session_name_for(child_address),
        "state": "planned",
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": owner_token,
        "last_applied_seq": last_applied_seq,
        "last_inbox_acked_offset": _current_inbox_size(child_address),
        "liveness_state": "claimed",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "workspace": str(node_dir),
        # DERIVATION (FORK-BRIEF-DERIVATION): the brief + frozen acceptance are the node's OWN files,
        # pre-authored by the parent at plan time (pointer-not-payload, WORKSPACE-SCHEMA:221). The spawn
        # derives these pointers from the node — it does not carry the spec/acceptance as payload.
        "spec_pointer": str(node_dir / "brief.md"),
        "frozen_acceptance_ref": str(node_dir / "acceptance.md"),
    }
    if live is not None and isinstance(live.get("messages"), dict):
        binding["messages"] = copy.deepcopy(live["messages"])
        if live.get("message_last_id"):
            binding["message_last_id"] = live["message_last_id"]
    inherit_respawn_accounting(binding, live)
    if child_metadata:
        for key in (
            "child_purpose",
            "test_refresh",
            "test_refresh_for",
            "accepted_test_package",
            "accepted_test_package_address",
            "accepted_test_package_stamp",
            "accepted_test_contract_home",
            "accepted_test_package_gate_id",
            "accepted_test_package_gate_passed_at",
            "no_executable_tests_exception_ref",
            "intent_spec_receipt",
            "raw_request_receipt",
            "contract_receipts",
        ):
            if key in child_metadata:
                binding[key] = child_metadata[key]
    review_binding = None
    review_level = _review_level_for_producer(child_address, child_level_config)
    if review_level is not None:
        review_address = _review_address_for(child_address)
        binding.update({
            "gate_required": True,
            "gate_review_address": review_address,
        })
        review_binding = _planned_review_binding(
            producer_address=child_address,
            parent_address=parent_address,
            review_address=review_address,
            review_level=review_level,
        )
    # Merge into the live whole map (preserve siblings) and write under a BRIEFLY-held EX lock —
    # released on exit, before claim_and_spawn re-takes it per-mutation (no re-entrant fcntl deadlock).
    # This mirrors genesis._register_l1_root's lock-held seed exactly, with parent_address SET.
    lock_path = Path(runtime_root) / executor.LOCK_FILENAME
    with store.file_lock(lock_path, shared=False):
        live_map = dict(ledger.all_nodes())
        _apply_serial_l3_schedule(
            binding,
            live_map=live_map,
            parent_binding=live_map.get(parent_address),
            parent_address=parent_address,
            child_address=child_address,
            child_level_config=child_level_config,
            child_metadata=child_metadata,
        )
        live_map[child_address] = binding
        if review_binding is not None:
            live_review = live_map.get(review_binding["node_address"])
            if (
                live_review is None
                or states.is_terminal(live_review.get("state"))
                or live_review.get("state") == "planned"
            ):
                live_map[review_binding["node_address"]] = review_binding
        ledger.write_binding(live_map, _lock_held=True)
    return binding


def _absolutize_manifest_path(path: str) -> str:
    """LR-3: render a load-manifest doc path ABSOLUTE in the authored brief (agents read briefs
    from their node cwd; a repo-relative path dangles there — agent-lifecycle L13's "everything
    is already loaded" demands paths that resolve where the agent stands). The neutral contract
    keeps repo-relative paths (pieces_present resolves them under harness_root); only the
    RENDERED brief absolutizes. A '#fragment' suffix survives; an already-absolute path passes."""
    base, sep, frag = str(path).partition("#")
    if os.path.isabs(base):
        return str(path)
    repo_root = Path(__file__).resolve().parents[2]
    return str(repo_root / base) + (sep + frag if sep else "")


FORM_UNFILLED_SENTINEL = "form:unfilled"


def _instantiate_node_forms(child_address: str, level: str, role_variant: str,
                            parent_address, node_dir: Path) -> None:
    """#30 — pre-instantiated node FORMS (user mental model, 2026-06-12): drop report.md +
    plan.md skeletons into the node at brief-write time — header pre-filled from the address,
    the GIVEN requirement IDs pre-listed (derived from brief.md/acceptance.md, the SAME sources
    the E2 walker reads), the L5+ seat getting the verified-not-discharged template variant.
    "They get everything in front of them": filling the form is the only path; zero
    transcription ambiguity.

    NEVER overwrites an existing file (stateless respawn: the successor inherits the prior
    incarnation's filled forms). Every instantiated report carries FORM_UNFILLED_SENTINEL —
    without it the skeleton would auto-satisfy E2's MISSING-REPORT (file exists) AND the
    citation check (IDs pre-filled); the walker refuses a DONE that still carries it
    (UNFILLED-REPORT-FORM, return_contract). Best-effort: forms are aids, the E2 gate is the
    floor — a template hiccup never fails the spawn.
    """
    try:
        repo_root = Path(__file__).resolve().parents[2]
        templates = repo_root / "operational" / "shared" / "templates"

        # --- the GIVEN ids (the walker's own token + sources — one derivation, two readers) ---
        from harnessd.return_contract import _ID_TOKEN
        given: list = []
        seen = set()
        for name in ("brief.md", "acceptance.md"):
            f = node_dir / name
            if f.is_file():
                for tok in _ID_TOKEN.findall(f.read_text(encoding="utf-8", errors="replace")):
                    if tok not in seen:
                        seen.add(tok)
                        given.append(tok)

        # --- report.md (level variant for the reviewer seat) ---
        report_path = node_dir / "report.md"
        if not report_path.exists():
            if "review-check" in str(role_variant):
                tpl = templates / "check-review-report-template.md"
            else:
                variant = templates / f"report-template.{level}.md"
                tpl = variant if variant.is_file() else (templates / "report-template.md")
            body = tpl.read_text(encoding="utf-8")
            body = body.replace("<node-address>#<seat>", child_address)
            body = body.replace("<parent-address>", str(parent_address or "(root)"))
            body = body.replace("(<level> <role>)", f"({level} {role_variant})")
            if given:
                prefilled = "\n".join(
                    ["Your given IDs (pre-filled by the harness from brief.md/acceptance.md — "
                     "account for each):", ""]
                    + [f"- `{i}` — <discharged | deferred (reason) | escalated>" for i in given]
                    + [""]
                )
                marker = "## Requirement IDs"
                idx = body.find(marker)
                if idx != -1:
                    eol = body.find("\n", idx)
                    if eol != -1:
                        body = body[: eol + 1] + "\n" + prefilled + body[eol + 1:]
            sentinel = (
                f"<!-- {FORM_UNFILLED_SENTINEL} — harness-instantiated skeleton: replace every "
                "<angle-bracket> prompt, account for each pre-listed ID, then DELETE THIS LINE "
                "before signing off (E2 refuses a DONE that still carries it) -->\n"
            )
            store.atomic_replace(report_path, lambda h: (h.write(sentinel), h.write(body)))

        # --- plan.md ---
        plan_path = node_dir / "plan.md"
        if not plan_path.exists():
            tpl = templates / "plan-template.md"
            body = tpl.read_text(encoding="utf-8").replace("<node-address>", child_address)
            store.atomic_replace(plan_path, lambda h: h.write(body))
    except Exception:  # noqa: BLE001 — best-effort: the E2 gate is the floor, never the form
        pass


def _write_child_brief(
    child_address: str,
    child_level_config,
    registered_binding: dict,
    brief_content: Optional[str],
) -> None:
    """Write the child BRIEF.md (the assembled load-manifest + the parent brief_content) into the node.

    The brief lands SOMEWHERE the child reads it in place (DAEMON §6.1 STEP2 + the increment). v1
    assembles the runtime-neutral load-manifest (brief.assemble_neutral — the role-as-documents PATHS
    the child reads) and writes it ALONGSIDE the parent brief_content (the child's actual task) into
    ``<node-dir>/BRIEF.md`` (FORK-BRIEF-LANDING). The brief_content is ALSO recorded onto the child
    binding (a ``brief_content`` field) so the task is recoverable from the binding slice too. A
    skipped brief would leave the child node with no task / no manifest (the mutant the (b) test kills).
    """
    work_node = _work_node_for(child_address, registered_binding)
    neutral = brief.assemble_neutral(child_address, child_level_config, work_node)

    node_dir = _child_node_dir(child_address)
    node_dir.mkdir(parents=True, exist_ok=True)
    brief_path = node_dir / "brief.md"  # canonical lowercase (WORKSPACE-SCHEMA:221)

    manifest_header = [
        f"# brief — {child_address}",
        "",
        f"- node_address: {child_address}",
        f"- parent_address: {registered_binding.get('parent_address')}",
        f"- level: {neutral.level}",
        f"- role_variant: {neutral.role_variant}",
        f"- system_prompt_file: {neutral.system_prompt_file}",
        "",
        *_brief_surface_lines(child_address, child_level_config, neutral),
        # F19 — the sign-off pointer, visible in the first thing the child reads (belt-and-braces
        # alongside the .sign-off.<seat>.json handshake itself + comms-protocol's Terminal Signal).
        "## Sign-off",
        "",
        f"- handshake (your owner_token + signal path): {addressing.signoff_path(child_address, ledger.RUNTIME_ROOT)}",
        f"- terminal signal artifact: {addressing.signal_path(child_address, ledger.RUNTIME_ROOT)}",
        "- your final act: write the signal file (atomic tmp+rename) with the owner_token copied "
        "verbatim from the sign-off file; a stale/wrong token is silently ignored.",
        "",
    ]

    # THREE cases (FORK-BRIEF-DERIVATION):
    #   (1) OVERRIDE — a brief_content was supplied: write brief.md = manifest + the inlined task (the
    #       exception path, e.g. a throwaway task the parent didn't pre-author a node file for).
    #   (2) PRE-AUTHORED DEFAULT — no override AND brief.md already exists: the parent authored the
    #       pointer-not-payload brief into the node at plan time. Do NOT overwrite it; the spawn only
    #       brings the prepared node online (spec_pointer already points at it).
    #   (3) STUB — no override AND no brief.md: write a manifest-only stub so the node is never empty
    #       (an L4 that forgot to pre-author still gets its load-manifest and can escalate the gap).
    wrote = True
    if brief_content is not None:
        if launch_surface.is_pilot(child_level_config):
            task_text = (
                "Parent task note:\n\n"
                f"{brief_content}\n\n"
                "For launch-surface roles, this note does not replace `.launch-packet.md`. Start "
                "from `.launch-packet.md` and its `Task Package`; use this note only as parent "
                "emphasis or a recovery hint."
            )
        else:
            task_text = brief_content
        lines = manifest_header + ["## Task", "", task_text, ""]
        store.atomic_replace(brief_path, lambda h: (h.write("\n".join(lines)), h.write("\n")))
    elif not brief_path.exists():
        if launch_surface.is_pilot(child_level_config):
            task_text = (
                "No standalone task text was supplied in this file. For launch-surface roles, your "
                "assignment is carried by `.launch-packet.md`, especially its `Task Package` section. "
                "Use that launch packet as the normal task surface. Escalate to your parent only if "
                "the launch packet's task package is absent, incoherent, or insufficient for your role."
            )
        else:
            task_text = (
                "(no brief pre-authored and no override supplied — escalate the gap "
                "to your parent rather than guessing the task)"
            )
        lines = manifest_header + ["## Task", "", task_text, ""]
        store.atomic_replace(brief_path, lambda h: (h.write("\n".join(lines)), h.write("\n")))
    else:
        wrote = False  # pre-authored brief.md left intact — the derivation default

    # Record the deliverable_state=briefed onto the child binding (own-slice write through the single
    # writer; the child is still ``planned`` + unclaimed, so we fence on the registered owner_token).
    # The brief is present by whichever path; the spec_pointer already names brief.md on the binding.
    # #30: the pre-instantiated forms ride the same brief-write moment (after brief.md
    # settles so the ID derivation reads the FINAL brief).
    _instantiate_node_forms(
        child_address, neutral.level, neutral.role_variant,
        registered_binding.get("parent_address"), node_dir,
    )

    summary = (f"child brief written into the node ({brief_path})" if wrote
               else f"child brief pre-authored; left intact ({brief_path})")
    executor.deliver(
        child_address,
        deliverable_state="briefed",
        extra_delta={
            "frozen_input_stamps": _frozen_input_stamps(child_address),
            "frozen_input_stamped_at": clock.now_utc(),
        },
        expected_owner_token=registered_binding.get("owner_token"),
        event="brief_written",
        summary=summary,
    )


def _brief_surface_lines(child_address: str, child_level_config, neutral) -> list[str]:
    if launch_surface.is_pilot(child_level_config):
        node_dir = _child_node_dir(child_address)
        return [
            "## Launch Packet",
            "",
            f"- generated at spawn: {node_dir / '.launch-packet.md'}",
            f"- optional reference map: {node_dir / '.reference-map.md'}",
            "- normal work starts from the generated launch packet and task package.",
            "- use the reference map only for concrete lookups the task requires.",
            "",
        ]
    return [
        "## Identity — Load These Documents (read in place)",
        "",
        # LR-3: ABSOLUTE paths — the pane boots in the NODE workspace, so a repo-relative
        # manifest path dangles from the agent's cwd and every agent burned its first turns
        # rediscovering the harness root (observed at EVERY level, 2026-06-11 live run).
        *[f"- {_absolutize_manifest_path(path)}" for path in neutral.load_manifest],
        "",
    ]


def _mark_parent_test_refresh_pending(
    parent_address: str,
    child_address: str,
    child_metadata: Optional[dict],
) -> None:
    """Latch L4 while a post-design acceptance refresh is authored and reviewed."""
    if not child_metadata or child_metadata.get("child_purpose") != CHILD_PURPOSE_TEST_AUTHOR:
        return
    if child_metadata.get("test_refresh") is not True:
        return
    parent = ledger.read_binding(parent_address)
    if not parent:
        return
    executor.record_admission(
        parent_address,
        expected_owner_token=None,
        delta={
            "test_refresh_state": TEST_REFRESH_PENDING_L5_REVIEW,
            "test_refresh_child": child_address,
            "test_refresh_for": child_metadata.get("test_refresh_for"),
            "test_refresh_requested_at": clock.now_utc(),
            "test_refresh_approved_at": None,
            "test_refresh_approved_by": None,
        },
        event="test_refresh_requested",
        summary=(
            f"L4 requested post-design acceptance refresh via {child_address}; "
            "implementation L5 admission waits for L5+ review and L3 approval"
        ),
    )


def register_and_spawn_child(
    parent_address: str,
    child_address: str,
    *,
    child_level_config,
    brief_content: Optional[str] = None,
    expected_parent_owner_token: Optional[str] = None,
    child_metadata: Optional[dict] = None,
) -> SpawnResult:
    """THE parent-spawns-child path: register the child under the parent, brief it, then claim+spawn.

    (1) PRECONDITION — the parent binding exists AND is LIVE (non-terminal). A dead/absent parent is
        REFUSED HERE, BEFORE any child register (no half-registered orphan slot a reconcile sweep
        could adopt). Returns a not-ok SpawnResult; NO child binding, NO actor.
    (2) REGISTER — the child as a fresh planned slot UNDER the parent (parent_address SET; mirror
        genesis._register_l1_root). Safe if the child already exists (does not clobber a live child).
    (3) BRIEF — assemble the load-manifest + write the parent brief_content into the child node.
    (4) SPAWN — the EXISTING chokepoint.claim_and_spawn(child, expected_state=planned, …): the F-024
        claim-before-spawn (a lost claim opens NO actor; a post-claim failure releases the claim).

    The adapter is the module-level injected port (set_adapter / ADAPTER) — register_and_spawn_child
    spawns THROUGH the same real chokepoint, so the child actor open rides the SAME adapter seam.
    """
    runtime_root = ledger.RUNTIME_ROOT
    if runtime_root is None:
        raise RuntimeError(
            "register_and_spawn_child requires ledger.RUNTIME_ROOT bound (the child register + brief "
            "land under the runtime tree; bind it like the executor/ledger path-injection contract)"
        )

    # STEP 1 — PRECONDITION: only a LIVE (existing + non-terminal) parent spawns a child. Decided
    # BEFORE the register so a refused spawn leaves NO half-registered child slot (test (c)/(e)).
    parent_binding = ledger.read_binding(parent_address)
    if not _parent_is_live(parent_binding):
        return _result_failed("dead_parent", tmux_target=child_address)

    # STEP 1a — PARENT FENCE (supervision-tree integrity, FORK-PARENT-TOKEN): when the caller presents
    # an ``expected_parent_owner_token``, it must equal the parent's live owner_token — i.e. the caller
    # must OWN the parent it is spawning under, so an agent can only spawn children under ITS OWN node,
    # not a sibling/cousin subtree. Optional (None) mirrors the deliver()/own-slice pattern: a
    # daemon-internal/genesis-style spawn presents no token (the EX lock + local IPC are the bound). A
    # mismatched token is refused BEFORE the register (no half-registered child).
    if (expected_parent_owner_token is not None
            and parent_binding.get("owner_token") != expected_parent_owner_token):
        return _result_failed("parent_fence", tmux_target=child_address)

    child_metadata = dict(child_metadata or {})
    client_brief_receipts, intent_block = prepare_client_brief_for_spawn(
        parent_address,
        child_address,
        child_level=getattr(child_level_config, "level", None),
    )
    if intent_block is not None:
        failure_class, _reason = intent_block
        return _result_failed(failure_class, tmux_target=child_address)
    if client_brief_receipts:
        child_metadata.update(client_brief_receipts)
        child_metadata["contract_receipts"] = contracts.merge_receipts(
            child_metadata.get("contract_receipts"),
            *client_brief_receipts.values(),
        )
    bind_block = bind_accepted_test_package_for_spawn(
        parent_address,
        child_address,
        child_level=getattr(child_level_config, "level", None),
        child_metadata=child_metadata,
    )
    if bind_block is not None:
        failure_class, _reason = bind_block
        return _result_failed(failure_class, tmux_target=child_address)
    refresh_block = approved_test_refresh_spawn_block(
        parent_address,
        child_address,
        child_level=getattr(child_level_config, "level", None),
        child_metadata=child_metadata,
    )
    if refresh_block is not None:
        failure_class, _reason = refresh_block
        return _result_failed(failure_class, tmux_target=child_address)
    package_block = accepted_test_package_spawn_block(
        parent_address,
        child_address,
        child_level=getattr(child_level_config, "level", None),
        child_metadata=child_metadata,
    )
    if package_block is not None:
        failure_class, _reason = package_block
        return _result_failed(failure_class, tmux_target=child_address)
    child_metadata.update(
        accepted_test_package_binding_metadata(
            parent_address,
            child_address,
            child_level=getattr(child_level_config, "level", None),
            child_metadata=child_metadata,
        )
    )

    # STEP 2 — REGISTER the child as a fresh planned slot UNDER the parent (parent_address SET). Safe
    # if the child already exists live (returns it so the planned-expected claim below loses — single
    # owner, no double-register of a running child).
    registered = _register_child(
        child_address,
        parent_address,
        child_level_config,
        runtime_root,
        child_metadata=child_metadata,
    )
    if registered is None:  # defensive — _register_child always returns a binding
        return _result_failed("register_failed", tmux_target=child_address)

    # STEP 3 — WRITE THE BRIEF (the assembled load-manifest + the parent brief_content) into the child
    # node. Skipped only when the child was NOT freshly registered as planned (an already-live child we
    # did not re-register — the claim below will lose, so there is no fresh brief to write).
    if registered.get("state") == "planned":
        _write_child_brief(child_address, child_level_config, registered, brief_content)
        _mark_parent_test_refresh_pending(parent_address, child_address, child_metadata)

    # STEP 4 — SPAWN via the EXISTING claim-before-spawn (F-024). The child claim's CAS precondition is
    # the CHILD's registered (planned, gen0, minted-token) slot — NOT the parent's token. A lost claim
    # (a racer, or an already-running child this register did not clobber) opens NO actor; a post-claim
    # failure releases the claim, exactly as today.
    fresh = ledger.read_binding(child_address) or registered
    if fresh.get("admission_state") == ADMISSION_WAITING_ON_SIBLING:
        return _result_failed("admission_queued", tmux_target=child_address)
    return claim_and_spawn(
        child_address,
        expected_state="planned",
        expected_generation=registered["generation"],
        expected_owner_token=fresh.get("owner_token"),
        level_config=child_level_config,
    )


# ---------------------------------------------------------------------------
# resume — the spawn variant with the GATE FIREWALL (the SINGLE enforcement point, LOCKED §6.4).
# ---------------------------------------------------------------------------

def resume(
    node_address: str,
    *,
    expected_state: str,
    expected_generation: int,
    expected_owner_token: Optional[str],
    delta_inputs: dict,
    level_config,
) -> SpawnResult:
    """Resume a live/dead address through the chokepoint, WITH the gate firewall (§6.4 — LOCKED).

    THE FIREWALL (the single, authoritative never-resume-across-the-gate point):

      * gate_crossed_at != null  -> REFUSE ``--resume``. Fall back to a FRESH spawn with a DELTA
        brief: re-adopt the live address via ``executor.claim`` (expected_state in {running, dead}),
        then open a FRESH actor recording a NEW session_uuid. The ``--resume`` argv is NEVER built on
        this branch — a crossed gate re-spawns clean, carrying NO pre-gate session context.

      * gate_crossed_at == null  -> the ELSE-branch: re-adopt the live address AND build the
        ``--resume`` continuation (the ONLY place a ``--resume`` argv is ever constructed). Bumps the
        epoch + re-mints the owner_token (fences the prior incarnation) and records a NEW session_uuid.

    STRUCTURAL guarantee: because the ``--resume`` argv is constructed ONLY on the else-branch, there
    is no code path that builds a ``--resume`` under ``gate_crossed_at != null`` — the firewall cannot
    be bypassed (necro.resume_brief delegates here; it owns no second copy of the check).
    """
    # STEP 0 (INT-2) — the SAME pause-subtree read-point claim_and_spawn runs: §6.4 resume is
    # "re-adopt the address through claim (§6.1)", a spawn VARIANT, so the §6.1 STEP-0 gate applies
    # here too. Without it a paused node could be re-claimed (epoch bump, token re-mint —
    # invalidating the incarnation the human paused to inspect), re-spawned, and kicked off via the
    # genesis RESUME leg — exactly the 'flag no one honors' failure mode (DAEMON L1225-1230).
    if subtree_paused(node_address):
        return _result_failed("paused_subtree", tmux_target=node_address)

    live = ledger.read_binding(node_address)
    if live is None:
        return _result_failed("absent_node", tmux_target=node_address)
    if respawn_parked(live):
        return _result_failed("respawn_parked", tmux_target=node_address)

    gate_crossed = live.get("gate_crossed_at") is not None

    # Re-adopt the live address through the claim (the §6.4 re-adopt variant). expected_state is the
    # caller's {running, dead}; the claim bumps the epoch + re-mints the owner_token (fences the prior
    # incarnation). This is claim-before-spawn for resume too — it never double-spawns a live address.
    claim_result = executor.claim(
        node_address,
        expected_state=expected_state,
        expected_generation=expected_generation,
        expected_owner_token=expected_owner_token,
        level_config=level_config,
    )
    if not claim_result.ok:
        return _result_failed("claim_lost", tmux_target=node_address)

    # LT-4/INT-1 — tear down the PRIOR incarnation's still-live pane BEFORE reopening: the genesis
    # RESUME branch's own reachability is a uuid-MISMATCHED leftover pane holding the deterministic
    # session name, which would collide create_detached ('duplicate session'). The claim above has
    # already fenced the prior incarnation (epoch bump), so its recorded target is safe to kill.
    kill_stale_pane(live.get("tmux_target"))

    # STEP2 — assemble the DELTA brief (what changed since the prior incarnation, pointing at the
    # durable work node). The prior incarnation is the pre-claim live binding.
    work_node = _work_node_for(node_address, claim_result.binding)
    prior_incarnation = {
        "session_uuid": live.get("session_uuid"),
        "lease_epoch": live.get("lease_epoch"),
        "generation": live.get("generation"),
    }
    delta = brief.delta_brief(node_address, prior_incarnation, work_node, delta_inputs or {})
    spawn_brief = _delta_brief_payload(delta)

    if gate_crossed:
        # ---- GATE CROSSED: REFUSE --resume. Fall back to a FRESH spawn (no --resume argv built). ----
        # spawn_brief carries NO resume continuation; the pre-gate session is NOT threaded anywhere.
        # The actor opens FRESH, recording a NEW session_uuid (the firewall's whole purpose).
        return _spawn_after_claim(node_address, claim_result.binding, level_config, spawn_brief)

    # ---- ELSE (clean gate): re-adopt + build the --resume continuation (the ONLY place it is built). ----
    spawn_brief = _attach_resume_continuation(spawn_brief, live.get("session_uuid"))
    return _spawn_after_claim(node_address, claim_result.binding, level_config, spawn_brief)


def _delta_brief_payload(delta) -> dict:
    """Flatten the DeltaBrief into the dict the adapter consumes (no --resume token here).

    The base delta payload carries NO session-continuation marker — the ``--resume`` is attached ONLY
    on resume's clean-gate else-branch (``_attach_resume_continuation``), never here, so a crossed-gate
    fallback that uses this payload directly is structurally resume-free.
    """
    return {
        "node_address": delta.node_address,
        "changes": dict(delta.changes),
        "delta": delta.delta,
        "workspace": delta.workspace,
        "frozen_acceptance_ref": delta.frozen_acceptance_ref,
        "prior_lease_epoch": delta.prior_incarnation.get("lease_epoch"),
    }


def _attach_resume_continuation(spawn_brief: dict, prior_session_uuid) -> dict:
    """Attach the ``--resume`` session-continuation to the brief — the ONLY place this is built.

    Called ONLY on resume's clean-gate else-branch (gate_crossed_at == null). Adds the resume argv
    marker + the prior session as the continuation target. Because this is the SINGLE construction
    site and it is unreachable under a crossed gate, the firewall is structural, not merely guarded.
    """
    enriched = dict(spawn_brief)
    enriched["resume_session"] = prior_session_uuid
    enriched["resume_argv"] = ["--resume", str(prior_session_uuid)]
    return enriched


# ---------------------------------------------------------------------------
# collapse — the terminal write carrying the in_flight RELEASE-DECREMENT (§3.6 / §6.1).
# done/failed/dead collapse; ESCALATED is NOT a collapse (asymmetric — state stays running).
# ---------------------------------------------------------------------------

# The terminal signals that DO collapse a node (symmetric to STEP1's claim-increment). ESCALATED is
# DELIBERATELY absent: it is asymmetric (§3.6) — the terminal_signal is set but the state stays running.
_COLLAPSE_TARGETS: dict[str, str] = {
    "DONE": "done",
    "FAILED": "failed",
    "DIED": "failed",
    "DIED_INFRA": "failed",
    "DIED_METHODOLOGY": "failed",
    "DEAD": "dead",
}

# The §3.6 NORMATIVE run-ledger event per terminal signal (SML-01) — sourced from states.TERMINAL_VOCAB
# so the spelling cannot drift (collapse_done/collapse_failed were non-normative names the sign-off
# check cannot key on). Two non-vocab aliases remain: DIED (the ipc kill verb's generic death) maps to
# the died_infrastructure event; DEAD (operator force-kill to `dead`) has NO §3.6 row — it keeps the
# legacy collapse_dead name (open question surfaced to the orchestrator: collapse_dead vs `necroed`).
_COLLAPSE_EVENTS: dict[str, str] = {
    states.TERMINAL_VOCAB["signal_DONE"].terminal_signal: states.TERMINAL_VOCAB["signal_DONE"].event,
    states.TERMINAL_VOCAB["signal_FAILED"].terminal_signal: states.TERMINAL_VOCAB["signal_FAILED"].event,
    states.TERMINAL_VOCAB["died_infrastructure"].terminal_signal: states.TERMINAL_VOCAB["died_infrastructure"].event,
    states.TERMINAL_VOCAB["died_methodology"].terminal_signal: states.TERMINAL_VOCAB["died_methodology"].event,
    "DIED": states.TERMINAL_VOCAB["died_infrastructure"].event,  # alias: generic death -> infra class
    "DEAD": "collapse_dead",  # operator force-kill: no §3.6 row (see comment above)
}


def escalate(
    node_address: str,
    *,
    expected_owner_token: Optional[str],
    signal_artifact_seen_at: Optional[str] = None,
):
    """The §3.6 ESCALATED slot-hold journal (SML-02): stamp ``terminal_signal=ESCALATED`` +
    ``terminal_signal_at`` and journal the ``signal_ESCALATED`` run-ledger row as a FENCED,
    exactly-once-PER-ARTIFACT, generation-bumping (replayable, §4.4) running→running transition
    through the single-writer executor. The lifecycle state STAYS ``running`` and the slot is
    HELD — the delta deliberately carries NO ``in_flight_release`` (the asymmetric row: the node
    keeps its context and waits for the answer round-trip).

    ``signal_artifact_seen_at`` is the harness-observed identity of the on-disk artifact from the
    fenced reader. SM-4/LR-25: idempotency keys on ARTIFACT IDENTITY, not the bare binding stamp
    and not the agent-authored ``ts`` — the old stamp-only early-return made the journal
    at-most-once per INCARNATION, while a future/skewed ``ts`` could make a steady re-poll
    re-journal forever. A stamped node re-journals IFF the current artifact identity differs from
    the recorded ``signal_artifact_seen_at`` (the re-stamp also re-arms the detector's waiting
    reason past any prior ``answered_at``).

    Returns:
      * ``None``                      — nothing to do: the node is absent, or THIS artifact is
                                        already journaled (a re-poll of the same artifact is a no-op);
      * ``TransitionResult(ok=False)``— a refused/aborted write: a non-running node (the slot-hold
                                        applies only to ``running``, §3.6), a CAS miss, or the
                                        fencing abort (the executor journals ``stale_return_ignored``
                                        and leaves the live binding unchanged);
      * ``TransitionResult(ok=True)`` — the slot-hold committed (the durable journal row landed).

    Callers ROUTE the result (the no-result-swallowing convention): the watchdog reports a failed
    journal write as ``escalate_journal_failed`` and retries next tick (the .signal artifact persists).
    """
    live = ledger.read_binding(node_address)
    if live is None:
        return None
    if live.get("terminal_signal") == "ESCALATED":
        # Already journaled for SOME artifact. F16's answer verb deliberately does NOT clear the
        # stamp (the answer RIDES terminal_signal=ESCALATED + terminal_note, TRANSPORTS §5.3 —
        # the parent reads both; clearing belongs to the round-trip COMPLETION), so the stamp
        # alone cannot key idempotency. LR-25: agent-authored signal ts cannot key idempotency
        # either; only a distinct harness-observed artifact identity (a second question) re-journals.
        if (
            not signal_artifact_seen_at
            or live.get("signal_artifact_seen_at") == signal_artifact_seen_at
        ):
            return None
    if live.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"ESCALATED slot-hold applies only to a running node (§3.6); "
                f"{node_address!r} is {live.get('state')!r}"
            ],
            warnings=[],
            binding=live,
        )
    from harnessd import clock  # local import, matching reconcile._now's style

    binding_delta = {
        "terminal_signal": "ESCALATED",
        "terminal_signal_at": clock.now_utc(),
    }
    if signal_artifact_seen_at:
        binding_delta["signal_artifact_seen_at"] = signal_artifact_seen_at

    result = executor.transition(
        node_address,
        expected_state="running",
        expected_generation=live["generation"],
        expected_owner_token=expected_owner_token,
        target_state="running",  # the §3.6 ASYMMETRIC row: signal set, state UNCHANGED
        binding_delta=binding_delta,
        event=states.TERMINAL_VOCAB["signal_ESCALATED"].event,
        summary=(
            "ESCALATED slot-hold: terminal_signal stamped, state stays running, slot HELD "
            "(no in_flight release; §3.6 asymmetric)"
        ),
    )
    # LR-26 — ESCALATION WAKES THE PARENT. The semantic upward path is agent-authored
    # (ESCALATED + escalation doc), but the harness wake trigger is edge-triggered off the
    # PARENT's inbox. Collapse (LR-11) and human answers already relay into that inbox; a
    # successful child ESCALATED needs the symmetric harness relay so the parent wakes and
    # executes the answer-down path.
    if result is not None and getattr(result, "ok", False):
        _notify_parent_of_escalation(node_address, getattr(result, "binding", None) or live)
    return result


def _notify_parent_of_escalation(node_address: str, escalated_binding: dict) -> None:
    """Append the LR-26 ``child_escalated`` pointer line to the parent's inbox (best-effort)."""
    from harnessd import clock  # local import, matching the kickoff line's style

    try:
        parent = (escalated_binding or {}).get("parent_address")
        if not parent or ledger.RUNTIME_ROOT is None:
            return
        parent_binding = ledger.read_binding(parent)
        if parent_binding is None or states.is_terminal(parent_binding.get("state")):
            return
        # Read-side 3a compatibility: the fenced signal remains the agent contract, but its durable
        # question truth is the same canonical sender-owned message used by every other ask.
        from harnessd import messages as _messages

        sig_path = addressing.signal_path(node_address, ledger.RUNTIME_ROOT)
        signal_identity = (escalated_binding or {}).get("signal_artifact_seen_at")
        try:
            _messages.submit_compat_message(
                node_address,
                target=parent,
                message_id=_messages.escalation_message_id(signal_identity),
                artifact=sig_path,
                marker=sig_path,
                summary=f"{node_address} escalated and is waiting for an answer.",
                needs_answer=True,
                metadata={"kind": "legacy_escalation", "signal_identity": signal_identity},
                tags=["escalation"],
                deliver_pointer=True,
                runtime_root=ledger.RUNTIME_ROOT,
            )
        except Exception:
            pass  # legacy relay remains behavior-compatible; daemon recovery retries canonicalization
        evidence = ""
        try:
            sig_path = addressing.signal_path(node_address, ledger.RUNTIME_ROOT)
            if sig_path.is_file():
                sig = json.loads(sig_path.read_text(encoding="utf-8"))
                evidence = str((sig.get("evidence") or {}).get("notes") or "")[:200]
        except (OSError, ValueError):
            pass
        # The canonical message pointer already woke the parent. Pre-upgrade child_escalated rows
        # remain readable, but new signals do not write that second inbox type.
    except Exception:  # noqa: BLE001 — notification is best-effort, never fails a clean escalate
        pass


def submit_gate_candidate(
    node_address: str,
    *,
    expected_owner_token: Optional[str],
    signal_artifact_seen_at: Optional[str] = None,
):
    """Record a gated producer's DONE as a candidate submission, not a collapse.

    Gate-owned forwarding (GATE-LIFECYCLE.md): a producing ``#exec`` seat can submit a
    candidate, but the parent-visible completion is owned by the co-located ``#review`` gate.
    This mirrors ``escalate`` in shape: journal exactly once per signal artifact, keep the
    producer running, and append a best-effort pointer line to the reviewer inbox.
    """
    live = ledger.read_binding(node_address)
    if live is None:
        return None
    if not live.get("gate_required"):
        return None
    if live.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate candidate submission applies only to a running producer; "
                f"{node_address!r} is {live.get('state')!r}"
            ],
            warnings=[],
            binding=live,
        )
    if (
        signal_artifact_seen_at
        and live.get("gate_candidate_signal_artifact_seen_at") == signal_artifact_seen_at
    ):
        return None
    if live.get("gate_state") == "candidate_submitted":
        return None
    if live.get("gate_state") == "gate_escalated":
        return None

    stale_receipts = [
        row
        for row in contracts.stale_receipt_holders()
        if row.get("holder_address") == node_address
    ]
    if stale_receipts:
        defects = [
            (
                "STALE-CONTRACT-RECEIPT: "
                f"contract={row.get('contract_path')}; "
                f"current_revision={row.get('revision_record_ref')}; "
                f"held_fingerprint={row.get('held_fingerprint')}; "
                f"current_fingerprint={row.get('current_fingerprint')}. "
                "Recovery: re-read the revision record, write your contract-rebind marker, "
                "then resubmit the candidate with a fresh signal."
            )
            for row in stale_receipts
        ]
        from harnessd import return_contract as _return_contract

        _return_contract.journal_defects_once(
            node_address,
            live,
            signal_artifact_seen_at,
            defects,
        )
        return executor.TransitionResult(
            ok=False,
            errors=defects,
            warnings=[],
            binding=live,
        )

    from harnessd import clock  # local import, matching the escalation helper

    review_address = live.get("gate_review_address")
    if not review_address:
        path, _seat = addressing.split_address(node_address)
        review_address = f"{path}#review"

    drift = _frozen_input_drift(live)
    if drift:
        return _fail_gate_for_producer(
            node_address,
            live,
            review_address,
            failure_reason="frozen_input_drift",
            failure_class="frozen_input_drift",
            detail=(
                "frozen child input changed after spawn; candidate must be repaired through a "
                "fresh incarnation or explicit correction path: " + " | ".join(drift)
            ),
            signal_artifact_seen_at=signal_artifact_seen_at,
            expected_owner_token=expected_owner_token,
        )

    review_binding = ledger.read_binding(review_address)
    if review_binding is None:
        return _fail_gate_for_producer(
            node_address,
            live,
            review_address,
            failure_reason="review_slot_missing",
            failure_class="review_slot_missing",
            detail=f"gated producer {node_address!r} has no review binding at {review_address!r}",
            signal_artifact_seen_at=signal_artifact_seen_at,
            expected_owner_token=expected_owner_token,
        )
    if (
        states.is_terminal(review_binding.get("state"))
        and review_binding.get("gate_for") == node_address
    ):
        review_binding = _reopen_review_slot_for_resubmitted_candidate(
            node_address,
            live,
            review_address,
            review_binding,
        )
    if states.is_terminal(review_binding.get("state")):
        return _fail_gate_for_producer(
            node_address,
            live,
            review_address,
            failure_reason="review_slot_terminal",
            failure_class="review_slot_terminal",
            detail=(
                f"gated producer {node_address!r} review binding {review_address!r} "
                f"is terminal ({review_binding.get('state')!r})"
            ),
            signal_artifact_seen_at=signal_artifact_seen_at,
            expected_owner_token=expected_owner_token,
        )
    if review_binding.get("gate_for") != node_address:
        return _fail_gate_for_producer(
            node_address,
            live,
            review_address,
            failure_reason="review_slot_mismatch",
            failure_class="review_slot_mismatch",
            detail=(
                f"gated producer {node_address!r} review binding {review_address!r} "
                f"points at {review_binding.get('gate_for')!r}"
            ),
            signal_artifact_seen_at=signal_artifact_seen_at,
            expected_owner_token=expected_owner_token,
        )
    if review_binding.get("state") == "planned":
        kill_stale_pane(review_binding.get("tmux_target") or review_address)

    from harnessd import review_dispatch

    try:
        packet_fields = review_dispatch.create_review_packet(
            node_address,
            live,
            review_address,
            signal_artifact_seen_at,
        )
    except Exception as exc:  # noqa: BLE001 — packet creation is part of opening the gate
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate candidate submission could not create review packet for "
                f"{node_address!r} -> {review_address!r}: {exc}"
            ],
            warnings=[],
            binding=live,
        )

    binding_delta = {
        "gate_state_before": live.get("gate_state"),
        "gate_state": "candidate_submitted",
        "gate_submitted_at": clock.now_utc(),
        "gate_review_address": review_address,
        **packet_fields,
    }
    if live.get("gate_resolution_source") == "parent_return":
        binding_delta.update({
            "gate_resolution_source": None,
            "gate_resolved_by": None,
            "gate_resolution_notes": None,
            "gate_return_message_id": None,
        })
    if signal_artifact_seen_at:
        binding_delta["gate_candidate_signal_artifact_seen_at"] = signal_artifact_seen_at

    result = executor.transition(
        node_address,
        expected_state="running",
        expected_generation=live["generation"],
        expected_owner_token=expected_owner_token,
        target_state="running",
        binding_delta=binding_delta,
        event="gate_candidate_submitted",
        summary=(
            f"gate candidate submitted by {node_address}: producer stays running; "
            f"review owned by {review_address} (GATE-LIFECYCLE)"
        ),
        artifacts=["report.md"],
    )
    if result is not None and getattr(result, "ok", False):
        _notify_review_of_candidate(
            node_address,
            getattr(result, "binding", None) or live,
            review_address,
            packet_fields,
        )
    return result


def _reopen_review_slot_for_resubmitted_candidate(
    producer_address: str,
    producer: dict,
    review_address: str,
    review_binding: dict,
) -> dict:
    """Replace a completed review turn with a fresh planned reviewer for a new candidate.

    A BOUNCE returns work to the producer and closes the reviewer. When the producer later
    submits a new DONE candidate, the same logical ``#review`` address is reused with a new
    lease/token/incarnation, never by resuming the old review context.
    """
    if ledger.RUNTIME_ROOT is None:
        return review_binding
    if respawn_parked(review_binding):
        return review_binding
    review_level = review_binding.get("level")
    if not review_level:
        try:
            producer_level_config = config.LevelConfig.for_level(
                (producer.get("level") or "L5").split("#", 1)[0]
            )
        except KeyError:
            return review_binding
        review_level = _review_level_for_producer(producer_address, producer_level_config)
    if not review_level:
        return review_binding
    kill_stale_pane(review_binding.get("tmux_target") or review_address)
    replacement = _planned_review_binding(
        producer_address=producer_address,
        parent_address=producer.get("parent_address"),
        review_address=review_address,
        review_level=review_level,
    )
    lock_path = Path(ledger.RUNTIME_ROOT) / executor.LOCK_FILENAME
    with store.file_lock(lock_path, shared=False):
        live_map = dict(ledger.all_nodes())
        current = live_map.get(review_address)
        if (
            current is None
            or current.get("gate_for") != producer_address
            or not states.is_terminal(current.get("state"))
        ):
            return current or review_binding
        live_map[review_address] = replacement
        ledger.write_binding(live_map, _lock_held=True)
    return replacement


def _inbox_has_line(inbox: Path, **criteria) -> bool:
    try:
        for raw in inbox.read_text(encoding="utf-8").splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if all(line.get(key) == value for key, value in criteria.items() if value is not None):
                return True
    except OSError:
        return False
    return False


def _notify_review_of_candidate(
    node_address: str,
    binding: dict,
    review_address: str,
    packet_fields: Optional[dict] = None,
) -> None:
    """Append one ``candidate_submitted`` pointer line to the review seat's inbox."""
    from harnessd import clock  # local import, matching the kickoff line's style

    try:
        if not review_address or ledger.RUNTIME_ROOT is None:
            return
        try:
            from harnessd import review_dispatch as _review_dispatch
        except Exception:  # noqa: BLE001 — notification is best-effort, never fails submission
            _review_dispatch = None
        review_binding = ledger.read_binding(review_address)
        if review_binding is None or states.is_terminal(review_binding.get("state")):
            return
        node_dir = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
        packet = (packet_fields or {}).get("gate_review_packet")
        gate_id = (packet_fields or {}).get("gate_id") or (binding or {}).get("gate_id")
        gate_dir = Path((packet_fields or {}).get("gate_review_dir") or (binding or {}).get("gate_review_dir") or (node_dir / "reviews"))
        try:
            from harnessd import return_contract as _return_contract
            gate_artifact_path = _return_contract.gate_artifact_path(review_address, review_binding)
            gate_artifact_name = _return_contract.gate_artifact_name(review_binding)
        except Exception:  # noqa: BLE001
            gate_artifact_path = None
            gate_artifact_name = None
        if gate_artifact_path is None:
            gate_artifact_path = gate_dir / (gate_artifact_name or "gate-report.md")
        try:
            check_report_names = (
                _review_dispatch.required_check_report_names(review_binding)
                if _review_dispatch is not None
                else ()
            )
        except Exception:  # noqa: BLE001 — notification is best-effort, never fails submission
            check_report_names = ()
        if check_report_names:
            configured_axes = _review_dispatch.configured_module_panel_axes(
                review_binding
            )
            if configured_axes is not None:
                roster_label = (
                    f"the configured {len(configured_axes)}-seat module panel "
                    f"({', '.join(configured_axes)})"
                )
            else:
                roster_label = (
                    "the five L2+ product-altitude axes"
                    if len(check_report_names) == 5
                    else "the four V1 axes"
                )
            configured_clause = (
                "This commissioned module panel supersedes the static four-axis default. "
                if configured_axes is not None
                else ""
            )
            review_mode_message = (
                "This is a higher-level review gate. Normal mode is FULL: write "
                "`review-plan.md` first with a plain `Review Mode: FULL` line and a "
                f"`## Role Selection` section naming {roster_label}. "
                f"{configured_clause}"
                "The harness daemon opens independent review-check seats "
                "for these exact report files in "
                f"{gate_dir}: " + ", ".join(check_report_names) + ". "
                "Wait until every selected check has both its report file and matching "
                "current-gate child-completion inbox row before synthesis; report files alone are not "
                "completion evidence. "
                "If any selected check is missing its report or completion row, end the current turn with waiting status "
                "and let the harness wake this seat instead of holding a long foreground polling loop. "
                "Do not use native Agent/Task/subagent sidechains and do not author check reports yourself. Use "
                "SHORT only when the exact short-review exception applies; missing reviewer "
                "substrate is not a SHORT reason. "
            )
        else:
            review_mode_message = (
                "This is a local L5+ review gate. Complete the review inside this reviewer "
                "seat: perform your independent local review, write `gate-report.md`, then "
                "sign the verdict. The daemon will not open auxiliary reviewer seats for this "
                "gate. "
            )
        inbox = addressing.inbox_path(review_address, ledger.RUNTIME_ROOT)
        if _inbox_has_line(inbox, type="candidate_submitted", candidate=node_address, gate_id=gate_id):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "from": "harnessd",
            "type": "candidate_submitted",
            "candidate": node_address,
            "gate_id": gate_id,
            "review_packet": packet,
            "signal_artifact_seen_at": (binding or {}).get("gate_candidate_signal_artifact_seen_at"),
            "message": (
                f"Candidate submitted by {node_address}. "
                f"Candidate artifacts are in {node_dir}/. "
                "First create or refresh the native task list with high-level review-management "
                "steps for this gate; if the task-list tool is deferred, discover only that tool "
                "family first. "
                + (
                    f"Read producer report.md and review packet {packet}. "
                    if packet
                    else "Read producer report.md and the frozen gate packet. "
                )
                + f"Author the review plan at {gate_dir / 'review-plan.md'} and the final gate "
                + f"artifact at {gate_artifact_path}. "
                + review_mode_message
                + f"Keep the producer's node-root artifacts as "
                + "candidate evidence; PASS is the only parent-visible completion; BOUNCE returns "
                + "typed defects to the producer."
            ),
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort, never fails submission
        pass


def _fail_gate_for_producer(
    producer_address: str,
    producer: dict,
    review_address: str,
    *,
    failure_reason: str,
    failure_class: str,
    detail: str = "",
    signal_artifact_seen_at: Optional[str] = None,
    expected_owner_token: Optional[str] = None,
):
    failure_reason = failure_reason or "review_open_failed"
    failure_class = failure_class or failure_reason
    if (
        producer.get("gate_state") == "gate_failed"
        and producer.get("gate_failure_review") == review_address
        and producer.get("gate_failure_class") == failure_class
        and producer.get("gate_failure_reason") == failure_reason
    ):
        return None
    if producer.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate_failed applies only to a running producer; "
                f"{producer_address!r} is {producer.get('state')!r}"
            ],
            warnings=[],
            binding=producer,
        )

    from harnessd import clock

    try:
        failure_count = int(producer.get("gate_failure_count") or 0) + 1
    except (TypeError, ValueError):
        failure_count = 1
    binding_delta = {
        "gate_state_before": producer.get("gate_state"),
        "gate_state": "gate_failed",
        "gate_failed_at": clock.now_utc(),
        "gate_failure_count": failure_count,
        "gate_failure_reason": failure_reason,
        "gate_failure_class": failure_class,
        "gate_failure_detail": detail[:500],
        "gate_failure_review": review_address,
        "gate_review_address": review_address,
    }
    if signal_artifact_seen_at:
        binding_delta["gate_candidate_signal_artifact_seen_at"] = signal_artifact_seen_at

    result = executor.transition(
        producer_address,
        expected_state="running",
        expected_generation=producer["generation"],
        expected_owner_token=expected_owner_token or producer.get("owner_token"),
        target_state="running",
        binding_delta=binding_delta,
        event="gate_failed",
        summary=(
            f"gate failed for {producer_address}: review seat {review_address} "
            f"is unavailable (failure_class={failure_class})"
        ),
    )
    if result is not None and getattr(result, "ok", False):
        _notify_parent_of_gate_failure(
            producer_address,
            review_address,
            producer,
            failure_class=failure_class,
            failure_count=failure_count,
            detail=detail,
        )
    return result


def _review_slot_failure_for_producer(
    producer_address: str,
    review_address: str,
) -> Optional[tuple[str, str]]:
    review = ledger.read_binding(review_address)
    if review is None:
        return (
            "review_slot_missing",
            f"gated producer {producer_address!r} has no review binding at {review_address!r}",
        )
    if states.is_terminal(review.get("state")):
        return (
            "review_slot_terminal",
            (
                f"gated producer {producer_address!r} review binding {review_address!r} "
                f"is terminal ({review.get('state')!r})"
            ),
        )
    if review.get("gate_for") != producer_address:
        return (
            "review_slot_mismatch",
            (
                f"gated producer {producer_address!r} review binding {review_address!r} "
                f"points at {review.get('gate_for')!r}"
            ),
        )
    return None


def fail_gate_open(
    review_address: str,
    *,
    failure_class: str,
    detail: str = "",
):
    """Mark the producer's gate as failed when the review substrate cannot open.

    A candidate that cannot get a review seat must not sit forever in
    ``candidate_submitted``. The producer remains running, but the gate state moves to
    ``gate_failed`` and the parent gets a pointer line for intervention.
    """
    review = ledger.read_binding(review_address)
    if review is None:
        return None
    producer_address = review.get("gate_for")
    if not producer_address:
        return None
    producer = ledger.read_binding(producer_address)
    if producer is None:
        return None
    failure_class = failure_class or "review_open_failed"
    return _fail_gate_for_producer(
        producer_address,
        producer,
        review_address,
        failure_reason="review_open_failed",
        failure_class=failure_class,
        detail=detail,
    )


def fail_review_check_open(
    check_address: str,
    *,
    failure_class: Optional[str] = None,
    detail: str = "",
):
    """Mark the producer gate failed when an auxiliary review-check seat cannot open."""
    check = ledger.read_binding(check_address)
    if check is None:
        return None
    review_address = check.get("review_check_for")
    review = ledger.read_binding(review_address) if review_address else None
    if review is None:
        return None
    producer_address = check.get("review_check_candidate") or review.get("gate_for")
    producer = ledger.read_binding(producer_address) if producer_address else None
    if producer is None:
        return None
    return _fail_gate_for_producer(
        producer_address,
        producer,
        review_address,
        failure_reason="review_check_open_failed",
        failure_class=failure_class or "review_check_open_failed",
        detail=detail or f"review-check seat {check_address} could not open",
    )


def fail_gate_candidate_artifact_drift(
    review_address: str,
    *,
    defects: Optional[list] = None,
):
    """Mark the producer gate failed when its submitted candidate artifact manifest drifted."""
    review = ledger.read_binding(review_address)
    if review is None:
        return None
    producer_address = review.get("gate_for")
    if not producer_address:
        return None
    producer = ledger.read_binding(producer_address)
    if producer is None:
        return None
    detail = " | ".join(str(defect) for defect in list(defects or [])[:5])
    return _fail_gate_for_producer(
        producer_address,
        producer,
        review_address,
        failure_reason="candidate_artifact_drift",
        failure_class="candidate_artifact_drift",
        detail=detail,
    )


def _notify_parent_of_gate_failure(
    producer_address: str,
    review_address: str,
    producer_binding: dict,
    *,
    failure_class: str,
    failure_count: Optional[int] = None,
    detail: str = "",
) -> None:
    """Append one gate_failed pointer line to the parent inbox."""
    from harnessd import clock

    try:
        parent = (producer_binding or {}).get("parent_address")
        if not parent or ledger.RUNTIME_ROOT is None:
            return
        parent_binding = ledger.read_binding(parent)
        if parent_binding is None or states.is_terminal(parent_binding.get("state")):
            return
        producer_dir = addressing.node_dir(producer_address, ledger.RUNTIME_ROOT)
        inbox = addressing.inbox_path(parent, ledger.RUNTIME_ROOT)
        if _inbox_has_line(
            inbox,
            type="gate_failed",
            candidate=producer_address,
            review=review_address,
            failure_class=failure_class,
            gate_failure_count=failure_count,
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "from": "harnessd",
            "type": "gate_failed",
            "candidate": producer_address,
            "review": review_address,
            "failure_class": failure_class,
            "gate_failure_count": failure_count,
            "message": (
                f"Gate failed for {producer_address}: review seat {review_address} is unavailable "
                f"(failure_class={failure_class}). "
                + (f"Detail: {detail[:240]}. " if detail else "")
                + f"Candidate artifacts are in {producer_dir}/. "
                "The candidate has not passed; inspect the review substrate and decide the recovery path."
            ),
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort, never fails gate_failed
        pass


def retry_failed_gate(
    producer_address: str,
    *,
    expected_owner_token: Optional[str] = None,
    reason: str = "",
):
    """Resubmit a failed gate candidate after the review substrate has been repaired.

    This is a parent/operator-owned control-plane recovery action. It does not repair the
    review slot itself; it verifies that the slot now exists, is non-terminal, and still owns
    this producer, then moves the producer back to ``candidate_submitted`` and replays the
    reviewer pointer.
    """
    producer = ledger.read_binding(producer_address)
    if producer is None:
        return executor.TransitionResult(
            ok=False,
            errors=[f"gate retry: no binding for producer {producer_address!r}"],
            warnings=[],
            binding=None,
        )
    if producer.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate retry applies only to a running producer; "
                f"{producer_address!r} is {producer.get('state')!r}"
            ],
            warnings=[],
            binding=producer,
        )
    if producer.get("gate_required") is not True:
        return executor.TransitionResult(
            ok=False,
            errors=[f"gate retry applies only to a gated producer; {producer_address!r} is not gated"],
            warnings=[],
            binding=producer,
        )
    if producer.get("gate_state") != "gate_failed":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate retry applies only from gate_state='gate_failed'; "
                f"{producer_address!r} is {producer.get('gate_state')!r}"
            ],
            warnings=[],
            binding=producer,
        )

    review_address = producer.get("gate_review_address") or _review_address_for(producer_address)
    review = ledger.read_binding(review_address)
    if review is None:
        return executor.TransitionResult(
            ok=False,
            errors=[f"gate retry: review slot {review_address!r} is still missing"],
            warnings=[],
            binding=producer,
        )
    if states.is_terminal(review.get("state")):
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate retry: review slot {review_address!r} is terminal "
                f"({review.get('state')!r})"
            ],
            warnings=[],
            binding=producer,
        )
    if review.get("gate_for") != producer_address:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate retry: review slot {review_address!r} points at "
                f"{review.get('gate_for')!r}, not {producer_address!r}"
            ],
            warnings=[],
            binding=producer,
        )

    from harnessd import clock, review_dispatch

    signal_artifact_seen_at = producer.get("gate_candidate_signal_artifact_seen_at")
    try:
        packet_fields = review_dispatch.create_review_packet(
            producer_address,
            producer,
            review_address,
            signal_artifact_seen_at,
        )
    except Exception as exc:  # noqa: BLE001 — packet creation is part of reopening the gate
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate retry could not create review packet for "
                f"{producer_address!r} -> {review_address!r}: {exc}"
            ],
            warnings=[],
            binding=producer,
        )

    binding_delta = {
        "gate_state_before": producer.get("gate_state"),
        "gate_state": "candidate_submitted",
        "gate_submitted_at": clock.now_utc(),
        "gate_retried_at": clock.now_utc(),
        "gate_retry_reason": reason[:500],
        "gate_review_address": review_address,
        "gate_failure_reason": None,
        "gate_failure_class": None,
        "gate_failure_detail": None,
        "gate_failure_review": None,
        **packet_fields,
    }
    if signal_artifact_seen_at:
        binding_delta["gate_candidate_signal_artifact_seen_at"] = signal_artifact_seen_at

    result = executor.transition(
        producer_address,
        expected_state="running",
        expected_generation=producer["generation"],
        expected_owner_token=expected_owner_token or producer.get("owner_token"),
        target_state="running",
        binding_delta=binding_delta,
        event="gate_candidate_submitted",
        summary=(
            f"gate retry resubmitted {producer_address}: repaired review seat "
            f"{review_address} owns the candidate again"
        ),
        artifacts=["report.md"],
    )
    if result is not None and getattr(result, "ok", False):
        _notify_review_of_candidate(
            producer_address,
            getattr(result, "binding", None) or producer,
            review_address,
            packet_fields,
        )
    return result


def pass_gate(
    review_address: str,
    *,
    expected_owner_token: Optional[str],
    signal_artifact_seen_at: Optional[str] = None,
    verdict_notes: str = "",
):
    """Accept a gated candidate and make that completion parent-visible.

    The review seat owns the boundary. On ACCEPT, the harness finalizes the held producer, finalizes
    the reviewer, then appends a gate-cleared pointer line to the parent inbox. This deliberately
    bypasses the generic collapse relay: the parent sees ``gate_passed``, not an executor-authored
    ``child_collapsed``.
    """
    review = ledger.read_binding(review_address)
    if review is None:
        return None
    if (
        signal_artifact_seen_at
        and review.get("gate_pass_signal_artifact_seen_at") == signal_artifact_seen_at
    ):
        return None
    if review.get("state") == "done" and review.get("gate_verdict") == "ACCEPT":
        return None
    if review.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate PASS applies only to a running review seat; "
                f"{review_address!r} is {review.get('state')!r}"
            ],
            warnings=[],
            binding=review,
        )

    producer_address = review.get("gate_for")
    if not producer_address:
        return executor.TransitionResult(
            ok=False,
            errors=[f"gate PASS review seat {review_address!r} has no gate_for candidate"],
            warnings=[],
            binding=review,
        )
    producer = ledger.read_binding(producer_address)
    if producer is None:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate PASS review seat {review_address!r} points at absent candidate "
                f"{producer_address!r}"
            ],
            warnings=[],
            binding=review,
        )

    from harnessd import clock, return_contract as _return_contract

    now = clock.now_utc()
    gate_artifact_path = _return_contract.gate_artifact_path(review_address, review)
    producer_artifacts = _producer_artifact_paths(producer_address, producer)
    producer_already_passed = (
        producer.get("state") == "done" and producer.get("gate_state") == "gate_passed"
    )
    if not producer_already_passed:
        if producer.get("state") != "running":
            return executor.TransitionResult(
                ok=False,
                errors=[
                    f"gate PASS candidate must be running or already gate_passed; "
                    f"{producer_address!r} is {producer.get('state')!r}"
                ],
                warnings=[],
                binding=producer,
            )
        contract_parent_address = None
        contract_parent_delta: dict = {}
        contract_producer_delta: dict = {}
        if (
            producer.get("child_purpose") == CHILD_PURPOSE_TEST_AUTHOR
            and producer.get("test_refresh") is not True
        ):
            contract_parent_address = producer.get("parent_address")
            try:
                contract_info, contract_parent_delta = (
                    _materialize_initial_accepted_test_contract(
                        contract_parent_address,
                        producer_address,
                    )
                )
            except (OSError, contracts.ContractError) as exc:
                return executor.TransitionResult(
                    ok=False,
                    errors=[
                        f"accepted test package cannot be ratified at its L4 home: {exc}"
                    ],
                    warnings=[],
                    binding=producer,
                )
            contract_producer_delta = {
                "accepted_test_contract_home": contract_info["artifact"],
                "accepted_test_contract_key": contract_info["package_key"],
            }

        producer_delta = {
            "terminal_signal": "DONE",
            "terminal_signal_at": now,
            "in_flight_release": True,
            "gate_state_before": producer.get("gate_state"),
            "gate_state": "gate_passed",
            "gate_passed_at": now,
            "gate_review_address": review_address,
            "gate_reviewed_by": review_address,
            "gate_verdict": "ACCEPT",
            "gate_artifact": str(gate_artifact_path) if gate_artifact_path else None,
            "producer_artifacts": producer_artifacts,
            **contract_producer_delta,
        }
        if signal_artifact_seen_at:
            producer_delta["gate_pass_signal_artifact_seen_at"] = signal_artifact_seen_at
        stamp = _return_contract.report_stamp(producer_address)
        if stamp is not None:
            producer_delta.update(stamp)
        producer_result = executor.transition(
            producer_address,
            expected_state="running",
            expected_generation=producer["generation"],
            expected_owner_token=producer.get("owner_token"),
            target_state="done",
            binding_delta=producer_delta,
            event="gate_passed",
            summary=(
                f"gate PASS accepted {producer_address} via {review_address}; producer finalized "
                "only after review-owned ACCEPT (GATE-LIFECYCLE)"
            ),
            artifacts=_artifact_list(producer_artifacts, gate_artifact_path),
            related_binding_deltas=(
                {contract_parent_address: contract_parent_delta}
                if contract_parent_address and contract_parent_delta
                else None
            ),
            related_expected_owner_tokens=(
                {
                    contract_parent_address: (
                        ledger.read_binding(contract_parent_address) or {}
                    ).get("owner_token")
                }
                if contract_parent_address and contract_parent_delta
                else None
            ),
        )
        if producer_result is None or not getattr(producer_result, "ok", False):
            return producer_result

    from harnessd import merge_gate as _merge_gate

    auto_merge_result = _merge_gate.auto_merge_after_gate_pass(producer_address)

    review_delta = {
        "terminal_signal": "DONE",
        "terminal_signal_at": now,
        "in_flight_release": True,
        "gate_verdict": "ACCEPT",
        "gate_for": producer_address,
        "gate_passed_candidate": producer_address,
        "gate_passed_at": now,
        "gate_artifact": str(gate_artifact_path) if gate_artifact_path else None,
        "producer_artifacts": producer_artifacts,
    }
    if verdict_notes:
        review_delta["gate_verdict_notes"] = verdict_notes[:500]
    if signal_artifact_seen_at:
        review_delta["gate_pass_signal_artifact_seen_at"] = signal_artifact_seen_at
    stamp = _return_contract.artifact_stamp(gate_artifact_path, prefix="gate_artifact")
    if stamp is not None:
        review_delta.update(stamp)
    review_result = executor.transition(
        review_address,
        expected_state="running",
        expected_generation=review["generation"],
        expected_owner_token=expected_owner_token,
        target_state="done",
        binding_delta=review_delta,
        event="gate_review_passed",
        summary=(
            f"review gate {review_address} ACCEPTED {producer_address}; parent wake is gate_passed "
            "(GATE-LIFECYCLE)"
        ),
        artifacts=_artifact_list(producer_artifacts, gate_artifact_path),
    )
    if review_result is not None and getattr(review_result, "ok", False):
        kill_stale_pane((getattr(review_result, "binding", None) or review).get("tmux_target"))
        passed_producer = ledger.read_binding(producer_address) or producer
        _request_l3_approval_for_test_refresh(
            producer_address,
            review_address,
            passed_producer,
            gate_artifact_path,
            producer_artifacts,
        )
        _notify_parent_of_gate_pass(
            producer_address,
            review_address,
            passed_producer,
            verdict_notes,
            auto_merge_result=auto_merge_result,
        )
    return review_result


def _notify_parent_of_gate_pass(
    producer_address: str,
    review_address: str,
    producer_binding: dict,
    verdict_notes: str,
    *,
    auto_merge_result=None,
) -> None:
    """Append one ``gate_passed`` pointer line carrying the automatic merge outcome."""
    from harnessd import clock

    try:
        parent = (producer_binding or {}).get("parent_address")
        if not parent or ledger.RUNTIME_ROOT is None:
            return
        parent_binding = ledger.read_binding(parent)
        if parent_binding is None or states.is_terminal(parent_binding.get("state")):
            return
        producer_dir = addressing.node_dir(producer_address, ledger.RUNTIME_ROOT)
        review_dir = addressing.node_dir(review_address, ledger.RUNTIME_ROOT)
        inbox = addressing.inbox_path(parent, ledger.RUNTIME_ROOT)
        gate_id = (producer_binding or {}).get("gate_id")
        producer_artifacts = _producer_artifact_paths(producer_address, producer_binding)
        gate_artifact = _gate_artifact_path_for_review(review_address)
        review_binding = ledger.read_binding(review_address) or {}
        review_packet = (producer_binding or {}).get("gate_review_packet")
        candidate_manifest = (producer_binding or {}).get("gate_candidate_artifact_manifest")
        candidate_manifest_sha = (producer_binding or {}).get(
            "gate_candidate_artifact_manifest_sha256"
        )
        candidate_snapshot_dir = (producer_binding or {}).get("gate_candidate_artifact_snapshot_dir")
        candidate_signal = (producer_binding or {}).get("gate_candidate_signal_artifact_seen_at")
        if auto_merge_result is None:
            from harnessd import merge_gate as _merge_gate

            auto_merge_result = _merge_gate.auto_merge_after_gate_pass(producer_address)
        if _inbox_has_line(
            inbox,
            type="gate_passed",
            candidate=producer_address,
            review=review_address,
            gate_id=gate_id,
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        note = verdict_notes[:200]
        merge_outcome = getattr(auto_merge_result, "outcome", "failed")
        merge_repair_pointer = getattr(auto_merge_result, "repair_pointer", None)
        if merge_outcome == "merged":
            merge_message = "Automatic merge outcome: merged; parent branch includes the candidate."
        elif merge_outcome == "not_applicable":
            merge_message = (
                "Automatic merge outcome: not-applicable; no Git merge occurred. "
                "Do not assume branch movement."
            )
        else:
            merge_message = (
                "Automatic merge outcome: failed. Parent MUST NOT COMPOSE assuming this "
                "candidate was merged"
                + (
                    f"; repair through `{merge_repair_pointer}`."
                    if merge_repair_pointer
                    else "; inspect the auto-merge journal and repair before composition."
                )
            )
        line = json.dumps({
            "from": "harnessd",
            "type": "gate_passed",
            "candidate": producer_address,
            "review": review_address,
            "gate_id": gate_id,
            "producer_artifacts": producer_artifacts,
            "gate_artifact": str(gate_artifact) if gate_artifact else None,
            "gate_artifact_sha256": review_binding.get("gate_artifact_sha256"),
            "review_packet": review_packet,
            "candidate_artifact_manifest": candidate_manifest,
            "candidate_artifact_manifest_sha256": candidate_manifest_sha,
            "candidate_artifact_snapshot_dir": candidate_snapshot_dir,
            "signal_artifact_seen_at": candidate_signal,
            "merge_outcome": merge_outcome,
            "merge_repo_path": getattr(auto_merge_result, "repo_path", None),
            "merge_source_branch": getattr(auto_merge_result, "source_branch", None),
            "merge_target_branch": getattr(auto_merge_result, "target_branch", None),
            "merge_already_present": bool(
                getattr(auto_merge_result, "already_merged", False)
            ),
            "merge_anomaly": bool(getattr(auto_merge_result, "anomaly", False)),
            "merge_repair_pointer": merge_repair_pointer,
            "merge_errors": list(getattr(auto_merge_result, "errors", []) or []),
            "message": (
                f"Gate passed for {producer_address} by {review_address}. "
                + (f"Verdict notes: {note}. " if note else "")
                + _producer_artifact_pointer(producer_dir, producer_artifacts)
                + "; "
                + _review_artifact_pointer(review_address, review_dir)
                + ". "
                + merge_message
                + " Treat this as the parent-visible review completion."
            ),
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort, never fails a gate PASS
        pass


def _request_l3_approval_for_test_refresh(
    producer_address: str,
    review_address: str,
    producer_binding: dict,
    gate_artifact_path,
    producer_artifacts: list[str],
) -> None:
    """After L5+ passes refreshed tests, ask L3 to approve spec-faithfulness before use."""
    from harnessd import clock

    if (producer_binding or {}).get("child_purpose") != CHILD_PURPOSE_TEST_AUTHOR:
        return
    if (producer_binding or {}).get("test_refresh") is not True:
        return
    l4_address = (producer_binding or {}).get("parent_address")
    if not l4_address:
        return
    l4_binding = ledger.read_binding(l4_address)
    if l4_binding is None or states.is_terminal(l4_binding.get("state")):
        return
    result = executor.record_admission(
        l4_address,
        expected_owner_token=None,
        delta={
            "test_refresh_state": TEST_REFRESH_PENDING_L3_APPROVAL,
            "test_refresh_child": producer_address,
            "test_refresh_review": review_address,
            "test_refresh_gate_id": producer_binding.get("gate_id"),
            "test_refresh_gate_artifact": str(gate_artifact_path) if gate_artifact_path else None,
            "test_refresh_producer_artifacts": producer_artifacts,
            "test_refresh_for": producer_binding.get("test_refresh_for"),
            "test_refresh_review_passed_at": clock.now_utc(),
        },
        event="test_refresh_review_passed",
        summary=(
            f"test-author child {producer_address} passed L5+ review; "
            "waiting on L3 approval before refreshed acceptance becomes active"
        ),
    )
    if result is None or not getattr(result, "ok", False):
        return
    updated_l4 = getattr(result, "binding", None) or ledger.read_binding(l4_address) or l4_binding
    l3_address = updated_l4.get("parent_address")
    if not l3_address or ledger.RUNTIME_ROOT is None:
        return
    try:
        l3 = ledger.read_binding(l3_address)
        if l3 is None or states.is_terminal(l3.get("state")):
            return
        socket_path = Path(ledger.RUNTIME_ROOT) / ".harnessd" / "harnessd.sock"
        approval_command = (
            f"python3 -m harnessd.harnessctl --socket {shlex.quote(str(socket_path))} "
            f"test-refresh-approve {shlex.quote(l4_address)} --approver {shlex.quote(l3_address)} "
            f"--expected-parent-owner-token {shlex.quote(str(l3.get('owner_token')))} "
            "--notes '<your spec-faithfulness notes>'"
        )
        inbox = addressing.inbox_path(l3_address, ledger.RUNTIME_ROOT)
        if _inbox_has_line(
            inbox,
            type="test_refresh_approval_requested",
            l4=l4_address,
            tester=producer_address,
            gate_id=producer_binding.get("gate_id"),
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "from": "harnessd",
            "type": "test_refresh_approval_requested",
            "l4": l4_address,
            "tester": producer_address,
            "review": review_address,
            "gate_id": producer_binding.get("gate_id"),
            "test_refresh_for": producer_binding.get("test_refresh_for"),
            "producer_artifacts": producer_artifacts,
            "gate_artifact": str(gate_artifact_path) if gate_artifact_path else None,
            "approval_command": approval_command,
            "message": (
                f"Post-design acceptance refresh from {producer_address} passed L5+ review. "
                f"Approve {l4_address}'s refreshed tests for spec-faithfulness before L4 spawns "
                "implementation children against them. If you approve, run the approval_command "
                "after recording your spec-faithfulness notes."
            ),
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — approval request is best-effort; gate PASS still stands
        pass


def _notify_l4_of_test_refresh_approval(
    l4_address: str,
    approver_address: str,
    binding: dict,
    notes: str,
) -> None:
    try:
        if ledger.RUNTIME_ROOT is None:
            return
        inbox = addressing.inbox_path(l4_address, ledger.RUNTIME_ROOT)
        child = (binding or {}).get("test_refresh_child")
        if _inbox_has_line(
            inbox,
            type="test_refresh_approved",
            l4=l4_address,
            approver=approver_address,
            tester=child,
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "from": "harnessd",
            "type": "test_refresh_approved",
            "l4": l4_address,
            "approver": approver_address,
            "tester": child,
            "test_refresh_for": (binding or {}).get("test_refresh_for"),
            "message": (
                f"L3 approved refreshed tests for {l4_address}. "
                "Implementation L5 spawns may now proceed after the implementation task binds "
                "the approved tests/ package."
            ),
            "notes": notes[:500],
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — best-effort wake; approval state is durable
        pass


def approve_test_refresh(
    l4_address: str,
    *,
    approver_address: Optional[str] = None,
    expected_parent_owner_token: Optional[str] = None,
    notes: str = "",
):
    """L3 approves a passed post-design acceptance refresh for spec-faithfulness."""
    l4 = ledger.read_binding(l4_address)
    if l4 is None:
        return None
    if l4.get("test_refresh_state") == TEST_REFRESH_APPROVED:
        return executor.TransitionResult(ok=True, errors=[], warnings=[], binding=l4)
    if l4.get("test_refresh_state") != TEST_REFRESH_PENDING_L3_APPROVAL:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"test refresh approval applies only to state "
                f"{TEST_REFRESH_PENDING_L3_APPROVAL!r}; {l4_address!r} is "
                f"{l4.get('test_refresh_state')!r}"
            ],
            warnings=[],
            binding=l4,
        )
    l3_address = l4.get("parent_address")
    approver_address = approver_address or l3_address
    if not l3_address or approver_address != l3_address:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"test refresh for {l4_address!r} must be approved by its L3 parent "
                f"{l3_address!r}, got {approver_address!r}"
            ],
            warnings=[],
            binding=l4,
        )
    l3 = ledger.read_binding(l3_address)
    if expected_parent_owner_token is not None and (
        l3 is None or l3.get("owner_token") != expected_parent_owner_token
    ):
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"test refresh approval fencing abort: presented {expected_parent_owner_token!r} "
                f"!= live parent token {(l3 or {}).get('owner_token')!r}"
            ],
            warnings=[],
            binding=l4,
        )
    child_address = l4.get("test_refresh_child")
    child = ledger.read_binding(child_address) if child_address else None
    if not child or child.get("gate_state") != "gate_passed":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"test refresh child {child_address!r} has not passed its review gate"
            ],
            warnings=[],
            binding=l4,
        )
    approved_package_stamp = _test_refresh_package_stamp(child_address)
    if approved_package_stamp.get("present") is not True:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"test refresh child {child_address!r} has no tests/ package to approve"
            ],
            warnings=[],
            binding=l4,
        )
    target_ref = str(l4.get("test_refresh_for") or "").strip()
    target_address = _resolve_test_package_address(l4_address, target_ref)
    target = ledger.read_binding(target_address) if target_address else None
    prior_package_address = (target or {}).get("accepted_test_package_address")
    prior_home = (target or {}).get("accepted_test_contract_home")
    if not prior_home and prior_package_address:
        prior_info = _accepted_test_contract_info(l4_address, prior_package_address)
        prior_home = (prior_info or {}).get("artifact")
    package_key = (
        Path(str(prior_home)).name
        if prior_home
        else _test_package_key(target_address or child_address)
    )
    home = (
        Path(str(prior_home))
        if prior_home
        else contracts.accepted_test_home(l4_address, package_key)
    )
    path_key = contracts.canonical_contract_path(home)
    versions = copy.deepcopy(l4.get("contract_versions") or {})
    prior_version = versions.get(path_key)
    source = _child_node_dir(child_address) / "tests"
    try:
        stage = contracts.stage_package_home(source, home)
        staged_stamp = contracts.stamp_package(stage)
        revision_ref = None
        if (
            prior_version is not None
            and prior_version.get("fingerprint") != staged_stamp.get("sha256")
        ):
            revision_ref, _record = contracts.mint_revision_record(
                owner_address=l4_address,
                owner_workspace=l4.get("workspace") or _child_node_dir(l4_address),
                contract_path=home,
                prior_fingerprint=prior_version["fingerprint"],
                new_fingerprint=staged_stamp["sha256"],
                reason=notes or (
                    f"L3 approved refreshed tests for {target_ref or package_key}"
                ),
                channel="test_refresh",
                channel_evidence={
                    "test_refresh_child": child_address,
                    "test_refresh_for": target_ref,
                    "package_gate_id": child.get("gate_id"),
                    "package_review_address": child.get("gate_review_address"),
                    "owner_token": l4.get("owner_token"),
                    "approved_by": approver_address,
                    "approver_owner_token": (l3 or {}).get("owner_token"),
                },
            )
        installed_stamp = contracts.install_staged_home(stage, home)
        if revision_ref is not None:
            prior_receipt = contracts.contract_receipt(
                target_address or l4_address,
                l4_address,
                home,
                prior_version["stamp"],
                revision_record_ref=prior_version.get("revision_record_ref"),
            )
            restamped = notary.restamp(
                home,
                prior_receipt=prior_receipt,
                revision_record_ref=revision_ref,
                members=contracts.package_members(home),
                root_label="tests",
                read_only=True,
            )
            current_version = contracts.append_lineage(prior_version, restamped)
        elif prior_version is not None:
            current_version = prior_version
        else:
            current_version = contracts.version_entry(
                owner_address=l4_address,
                artifact=home,
                stamped=installed_stamp,
            )
        versions[path_key] = current_version
    except (OSError, contracts.ContractError, notary.NotaryError) as exc:
        return executor.TransitionResult(
            ok=False,
            errors=[f"test refresh could not ratify the L4 contract home: {exc}"],
            warnings=[],
            binding=l4,
        )

    contract_info = {
        "schema_version": 1,
        "package_key": package_key,
        "package_address": child_address,
        "artifact": path_key,
        "fingerprint": current_version["fingerprint"],
        "stamp": current_version["stamp"],
    }
    accepted_contracts = copy.deepcopy(l4.get("accepted_test_contracts") or {})
    accepted_contracts[child_address] = copy.deepcopy(contract_info)
    if prior_package_address:
        prior_projection = copy.deepcopy(contract_info)
        prior_projection["package_address"] = prior_package_address
        accepted_contracts[prior_package_address] = prior_projection

    approved_at = clock.now_utc()
    l4_delta = {
        "test_refresh_state": TEST_REFRESH_APPROVED,
        "test_refresh_approved_at": approved_at,
        "test_refresh_approved_by": approver_address,
        "test_refresh_approval_notes": notes[:500],
        "test_refresh_approved_package_path": path_key,
        "test_refresh_approved_package_stamp": current_version["stamp"],
        "test_refresh_revision_record_ref": current_version.get("revision_record_ref"),
        "accepted_test_contracts": accepted_contracts,
        "contract_versions": versions,
    }
    child_delta = {
        "test_refresh_approval_state": TEST_REFRESH_APPROVED,
        "test_refresh_approved_at": approved_at,
        "test_refresh_approved_by": approver_address,
        "test_refresh_approved_package_stamp": current_version["stamp"],
        "accepted_test_contract_home": path_key,
        "test_refresh_revision_record_ref": current_version.get("revision_record_ref"),
    }
    result = executor.record_related_updates(
        l4_address,
        primary_delta=l4_delta,
        related_deltas={child_address: child_delta},
        expected_owner_tokens={
            l4_address: l4.get("owner_token"),
            child_address: child.get("owner_token"),
        },
        event="test_refresh_approved",
        summary=(
            f"L3 {approver_address} ratified refreshed tests from {child_address} "
            f"at L4 contract home {path_key}"
        ),
        artifacts=[path_key, current_version.get("revision_record_ref")],
    )
    if result is not None and getattr(result, "ok", False):
        _notify_l4_of_test_refresh_approval(
            l4_address,
            approver_address,
            getattr(result, "binding", None) or l4,
            notes,
        )
    return result


def accept_escalated_gate(
    producer_address: str,
    *,
    resolver_address: Optional[str] = None,
    expected_parent_owner_token: Optional[str] = None,
    verdict_notes: str = "",
):
    """Accept a producer whose review gate escalated to the parent.

    A gate escalation keeps the producer and review seat live while the parent decides. If the
    parent accepts the escalated candidate, the harness must make the same durable transition a
    review ACCEPT would make: producer ``running -> done`` with ``gate_state=gate_passed`` and the
    review seat closed. Without this explicit resolution, a same-address next-phase spawn can be
    consumed as an idempotent already-live request while the old planning producer remains running.
    """
    producer = ledger.read_binding(producer_address)
    if producer is None:
        return None
    if producer.get("state") == "done" and producer.get("gate_state") == "gate_passed":
        return None
    if producer.get("state") != "running" or producer.get("gate_state") != "gate_escalated":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate parent ACCEPT applies only to a running gate_escalated producer; "
                f"{producer_address!r} is state={producer.get('state')!r}, "
                f"gate_state={producer.get('gate_state')!r}"
            ],
            warnings=[],
            binding=producer,
        )

    parent_address = producer.get("parent_address")
    resolver_address = resolver_address or parent_address
    if parent_address and resolver_address != parent_address:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate parent ACCEPT for {producer_address!r} must be resolved by its parent "
                f"{parent_address!r}, got {resolver_address!r}"
            ],
            warnings=[],
            binding=producer,
        )
    parent = ledger.read_binding(parent_address) if parent_address else None
    if expected_parent_owner_token is not None and (
        parent is None or parent.get("owner_token") != expected_parent_owner_token
    ):
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate parent ACCEPT fencing abort: presented {expected_parent_owner_token!r} != "
                f"live parent token {(parent or {}).get('owner_token')!r}"
            ],
            warnings=[],
            binding=producer,
        )

    review_address = producer.get("gate_review_address") or _review_address_for(producer_address)
    review = ledger.read_binding(review_address)
    if review is None:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate parent ACCEPT cannot finalize {producer_address!r}: "
                f"review seat {review_address!r} is absent"
            ],
            warnings=[],
            binding=producer,
        )
    if review.get("state") not in {"running", "done"}:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate parent ACCEPT review seat must be running or done; "
                f"{review_address!r} is {review.get('state')!r}"
            ],
            warnings=[],
            binding=review,
        )
    if review.get("gate_for") != producer_address:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate parent ACCEPT review seat {review_address!r} points at "
                f"{review.get('gate_for')!r}, not {producer_address!r}"
            ],
            warnings=[],
            binding=review,
        )

    from harnessd import clock, return_contract as _return_contract

    now = clock.now_utc()
    gate_artifact_path = _return_contract.gate_artifact_path(review_address, review)
    producer_artifacts = _producer_artifact_paths(producer_address, producer)
    contract_parent_address = None
    contract_parent_delta: dict = {}
    contract_producer_delta: dict = {}
    if (
        producer.get("child_purpose") == CHILD_PURPOSE_TEST_AUTHOR
        and producer.get("test_refresh") is not True
    ):
        contract_parent_address = producer.get("parent_address")
        try:
            contract_info, contract_parent_delta = (
                _materialize_initial_accepted_test_contract(
                    contract_parent_address,
                    producer_address,
                )
            )
        except (OSError, contracts.ContractError) as exc:
            return executor.TransitionResult(
                ok=False,
                errors=[
                    f"accepted test package cannot be ratified at its L4 home: {exc}"
                ],
                warnings=[],
                binding=producer,
            )
        contract_producer_delta = {
            "accepted_test_contract_home": contract_info["artifact"],
            "accepted_test_contract_key": contract_info["package_key"],
        }
    producer_delta = {
        "terminal_signal": "DONE",
        "terminal_signal_at": now,
        "in_flight_release": True,
        "gate_state_before": producer.get("gate_state"),
        "gate_state": "gate_passed",
        "gate_passed_at": now,
        "gate_review_address": review_address,
        "gate_reviewed_by": review_address,
        "gate_verdict": "ACCEPT",
        "gate_resolution_source": "parent_accept",
        "gate_resolved_by": resolver_address,
        "gate_resolution_notes": verdict_notes[:500],
        "gate_artifact": str(gate_artifact_path) if gate_artifact_path else None,
        "producer_artifacts": producer_artifacts,
        **contract_producer_delta,
    }
    stamp = _return_contract.report_stamp(producer_address)
    if stamp is not None:
        producer_delta.update(stamp)
    producer_result = executor.transition(
        producer_address,
        expected_state="running",
        expected_generation=producer["generation"],
        expected_owner_token=producer.get("owner_token"),
        target_state="done",
        binding_delta=producer_delta,
        event="gate_passed",
        summary=(
            f"parent {resolver_address} accepted escalated gate for {producer_address} via "
            f"{review_address}; producer finalized as gate_passed (GATE-LIFECYCLE)"
        ),
        artifacts=_artifact_list(producer_artifacts, gate_artifact_path),
        related_binding_deltas=(
            {contract_parent_address: contract_parent_delta}
            if contract_parent_address and contract_parent_delta
            else None
        ),
        related_expected_owner_tokens=(
            {contract_parent_address: (parent or {}).get("owner_token")}
            if contract_parent_address
            else None
        ),
    )
    if producer_result is None or not getattr(producer_result, "ok", False):
        return producer_result

    from harnessd import merge_gate as _merge_gate

    auto_merge_result = _merge_gate.auto_merge_after_gate_pass(producer_address)

    if review.get("state") == "running":
        review_delta = {
            "terminal_signal": "DONE",
            "terminal_signal_at": now,
            "in_flight_release": True,
            "gate_verdict": review.get("gate_verdict") or "ESCALATE",
            "gate_for": producer_address,
            "gate_parent_resolution": "ACCEPT",
            "gate_parent_resolved_by": resolver_address,
            "gate_parent_resolved_at": now,
            "gate_verdict_notes": verdict_notes[:500],
            "gate_artifact": str(gate_artifact_path) if gate_artifact_path else None,
            "producer_artifacts": producer_artifacts,
        }
        stamp = _return_contract.artifact_stamp(gate_artifact_path, prefix="gate_artifact")
        if stamp is not None:
            review_delta.update(stamp)
        review_result = executor.transition(
            review_address,
            expected_state="running",
            expected_generation=review["generation"],
            expected_owner_token=review.get("owner_token"),
            target_state="done",
            binding_delta=review_delta,
            event="gate_review_parent_resolved",
            summary=(
                f"parent {resolver_address} resolved review escalation from {review_address} "
                f"for {producer_address} as ACCEPT"
            ),
            artifacts=_artifact_list(producer_artifacts, gate_artifact_path),
        )
        if review_result is not None and not getattr(review_result, "ok", False):
            return review_result
        if review_result is not None:
            kill_stale_pane((getattr(review_result, "binding", None) or review).get("tmux_target"))

    _notify_parent_of_gate_pass(
        producer_address,
        review_address,
        ledger.read_binding(producer_address) or producer,
        verdict_notes,
        auto_merge_result=auto_merge_result,
    )
    return producer_result


def return_escalated_gate(
    producer_address: str,
    *,
    resolver_address: Optional[str] = None,
    expected_parent_owner_token: Optional[str] = None,
    verdict_notes: str = "",
):
    """Return a parent-escalated producer to ``gate_bounced`` for a fresh candidate."""
    producer = ledger.read_binding(producer_address)
    if producer is None:
        return None
    if producer.get("state") != "running" or producer.get("gate_state") != "gate_escalated":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate parent RETURN applies only to a running gate_escalated producer; "
                f"{producer_address!r} is state={producer.get('state')!r}, "
                f"gate_state={producer.get('gate_state')!r}"
            ],
            warnings=[],
            binding=producer,
        )
    verdict_notes = str(verdict_notes or "").strip()
    if not verdict_notes:
        return executor.TransitionResult(
            ok=False,
            errors=["gate parent RETURN requires non-empty ruling notes"],
            warnings=[],
            binding=producer,
        )

    parent_address = producer.get("parent_address")
    resolver_address = resolver_address or parent_address
    if not parent_address or resolver_address != parent_address:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate parent RETURN for {producer_address!r} must be resolved by its parent "
                f"{parent_address!r}, got {resolver_address!r}"
            ],
            warnings=[],
            binding=producer,
        )
    parent = ledger.read_binding(parent_address)
    if expected_parent_owner_token is not None and (
        parent is None or parent.get("owner_token") != expected_parent_owner_token
    ):
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate parent RETURN fencing abort: presented "
                f"{expected_parent_owner_token!r} != live parent token "
                f"{(parent or {}).get('owner_token')!r}"
            ],
            warnings=[],
            binding=producer,
        )

    review_address = producer.get("gate_review_address") or _review_address_for(producer_address)
    review = ledger.read_binding(review_address)
    if (
        review is None
        or review.get("state") not in {"running", "done"}
        or review.get("gate_for") != producer_address
    ):
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate parent RETURN requires the matching running/done review seat "
                f"{review_address!r} for {producer_address!r}"
            ],
            warnings=[],
            binding=review or producer,
        )

    from harnessd import messages as _messages

    identity = "|".join(
        str(value or "")
        for value in (
            producer_address,
            producer.get("gate_id"),
            producer.get("gate_escalation_count"),
            producer.get("gate_escalated_at"),
        )
    )
    message_id = "gate-return-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    content = (
        f"# Parent gate-return ruling\n\n"
        f"Candidate: `{producer_address}`\n\n"
        f"Gate: `{producer.get('gate_id') or 'unknown'}`\n\n"
        f"Review: `{review_address}`\n\n"
        f"Ruling: {verdict_notes}\n\n"
        "Repair the ruling, then submit a fresh candidate signal. The prior candidate identity "
        "cannot reopen review.\n"
    )
    try:
        _messages.author_and_submit(
            parent_address,
            target=producer_address,
            message_id=message_id,
            content=content,
            summary=f"Parent returned escalated gate for {producer_address} for repair.",
            tags=["gate-return"],
            metadata={
                "kind": "gate-return",
                "gate_id": producer.get("gate_id"),
                "review_address": review_address,
                "gate_escalation_count": producer.get("gate_escalation_count"),
            },
            runtime_root=ledger.RUNTIME_ROOT,
        )
    except (OSError, ValueError) as exc:
        return executor.TransitionResult(
            ok=False,
            errors=[f"gate parent RETURN could not record its canonical ruling message: {exc}"],
            warnings=[],
            binding=ledger.read_binding(producer_address) or producer,
        )

    now = clock.now_utc()
    result = executor.transition(
        producer_address,
        expected_state="running",
        expected_generation=producer["generation"],
        expected_owner_token=producer.get("owner_token"),
        target_state="running",
        binding_delta={
            "gate_state_before": "gate_escalated",
            "gate_state": "gate_bounced",
            "gate_bounced_at": now,
            "gate_review_address": review_address,
            "gate_last_bounce_review": review_address,
            "gate_last_bounce_notes": verdict_notes[:500],
            "gate_bounce_audit_signal": "gate_bounce",
            "gate_bounce_audit_label": (
                "LOOK HERE — a gate bounce means something probably went wrong; inspect the "
                "candidate, frozen contract, and parent ruling"
            ),
            "gate_verdict": "BOUNCE",
            "gate_resolution_source": "parent_return",
            "gate_resolved_by": resolver_address,
            "gate_resolution_notes": verdict_notes[:500],
            "gate_return_message_id": message_id,
        },
        event="gate_bounced",
        summary=(
            f"GATE RETURN — LOOK CLOSER: parent {resolver_address} returned escalated gate "
            f"{producer.get('gate_id') or 'unknown'} for {producer_address}; producer must repair "
            "and submit a fresh candidate"
        ),
        artifacts=[
            str(addressing.messages_dir(parent_address, ledger.RUNTIME_ROOT) / f"{message_id}.md")
        ],
    )
    return result


def bounce_gate(
    review_address: str,
    *,
    expected_owner_token: Optional[str],
    signal_artifact_seen_at: Optional[str] = None,
    verdict_notes: str = "",
):
    """Return typed gate defects to the producer without waking the parent.

    BOUNCE is a control verdict, not parent-visible completion. The producer keeps its live context
    and receives a pointer to the review report. The review seat closes after the verdict; a later
    producer resubmission opens a fresh reviewer at the same logical ``#review`` address.
    """
    review = ledger.read_binding(review_address)
    if review is None:
        return None
    if review.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate BOUNCE applies only to a running review seat; "
                f"{review_address!r} is {review.get('state')!r}"
            ],
            warnings=[],
            binding=review,
        )
    if expected_owner_token is not None and review.get("owner_token") != expected_owner_token:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate BOUNCE fencing abort: presented {expected_owner_token!r} != "
                f"live review token {review.get('owner_token')!r}"
            ],
            warnings=[],
            binding=review,
        )

    producer_address = review.get("gate_for")
    if not producer_address:
        return executor.TransitionResult(
            ok=False,
            errors=[f"gate BOUNCE review seat {review_address!r} has no gate_for candidate"],
            warnings=[],
            binding=review,
        )
    producer = ledger.read_binding(producer_address)
    if producer is None:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate BOUNCE review seat {review_address!r} points at absent candidate "
                f"{producer_address!r}"
            ],
            warnings=[],
            binding=review,
        )
    if (
        signal_artifact_seen_at
        and producer.get("gate_bounce_signal_artifact_seen_at") == signal_artifact_seen_at
    ):
        return _close_review_after_gate_verdict(
            review_address,
            review,
            producer_address,
            verdict="BOUNCE",
            signal_artifact_seen_at=signal_artifact_seen_at,
            verdict_notes=verdict_notes,
            event="gate_review_bounced",
        )
    if (
        signal_artifact_seen_at
        and producer.get("gate_escalation_signal_artifact_seen_at") == signal_artifact_seen_at
    ):
        return _close_review_after_gate_verdict(
            review_address,
            review,
            producer_address,
            verdict="BOUNCE",
            signal_artifact_seen_at=signal_artifact_seen_at,
            verdict_notes=verdict_notes,
            event="gate_review_escalated",
        )
    if producer.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate BOUNCE candidate must be running; "
                f"{producer_address!r} is {producer.get('state')!r}"
            ],
            warnings=[],
            binding=producer,
        )
    if producer.get("gate_state") == "gate_escalated":
        return _close_review_after_gate_verdict(
            review_address,
            review,
            producer_address,
            verdict="BOUNCE",
            signal_artifact_seen_at=signal_artifact_seen_at,
            verdict_notes=verdict_notes,
            event="gate_review_escalated",
        )

    from harnessd import clock

    producer_artifacts = _producer_artifact_paths(producer_address, producer)
    gate_artifact_path = _gate_artifact_path_for_review(review_address)
    try:
        prior_count = int(producer.get("gate_bounce_count") or 0)
    except (TypeError, ValueError):
        prior_count = 0
    cap = _gate_bounce_cap(producer)
    if prior_count >= cap:
        return _escalate_gate_bounce_cap(
            producer_address,
            review_address,
            producer,
            cap=cap,
            prior_count=prior_count,
            signal_artifact_seen_at=signal_artifact_seen_at,
            verdict_notes=verdict_notes,
        )
    binding_delta = {
        "gate_state_before": producer.get("gate_state"),
        "gate_state": "gate_bounced",
        "gate_bounced_at": clock.now_utc(),
        "gate_bounce_count": prior_count + 1,
        "gate_review_address": review_address,
        "gate_last_bounce_review": review_address,
        "gate_last_bounce_notes": verdict_notes[:500],
        "gate_bounce_audit_signal": "gate_bounce",
        "gate_bounce_audit_label": (
            "LOOK HERE — a gate bounce means something probably went wrong; inspect the "
            "candidate, frozen contract, and review finding"
        ),
        "gate_verdict": "BOUNCE",
        "gate_artifact": str(gate_artifact_path) if gate_artifact_path else None,
        "producer_artifacts": producer_artifacts,
    }
    if signal_artifact_seen_at:
        binding_delta["gate_bounce_signal_artifact_seen_at"] = signal_artifact_seen_at

    result = executor.transition(
        producer_address,
        expected_state="running",
        expected_generation=producer["generation"],
        expected_owner_token=producer.get("owner_token"),
        target_state="running",
        binding_delta=binding_delta,
        event="gate_bounced",
        summary=(
            f"GATE BOUNCE — LOOK CLOSER: {review_address} returned {producer_address}; "
            "producer stays running and receives typed review defects. Any bounce is an audit "
            "signal that something probably went wrong (GATE-LIFECYCLE)"
        ),
        artifacts=_artifact_list(producer_artifacts, gate_artifact_path),
    )
    if result is not None and getattr(result, "ok", False):
        _notify_producer_of_gate_bounce(producer_address, review_address, verdict_notes)
        close_result = _close_review_after_gate_verdict(
            review_address,
            review,
            producer_address,
            verdict="BOUNCE",
            signal_artifact_seen_at=signal_artifact_seen_at,
            verdict_notes=verdict_notes,
            event="gate_review_bounced",
        )
        if close_result is not None and not getattr(close_result, "ok", False):
            return close_result
    return result


def escalate_gate(
    review_address: str,
    *,
    expected_owner_token: Optional[str],
    signal_artifact_seen_at: Optional[str] = None,
    verdict_notes: str = "",
):
    """Escalate a gate verdict to the parent without passing or bouncing the candidate."""
    review = ledger.read_binding(review_address)
    if review is None:
        return None
    if review.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate ESCALATE applies only to a running review seat; "
                f"{review_address!r} is {review.get('state')!r}"
            ],
            warnings=[],
            binding=review,
        )
    if expected_owner_token is not None and review.get("owner_token") != expected_owner_token:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate ESCALATE fencing abort: presented {expected_owner_token!r} != "
                f"live review token {review.get('owner_token')!r}"
            ],
            warnings=[],
            binding=review,
        )

    producer_address = review.get("gate_for")
    if not producer_address:
        return executor.TransitionResult(
            ok=False,
            errors=[f"gate ESCALATE review seat {review_address!r} has no gate_for candidate"],
            warnings=[],
            binding=review,
        )
    producer = ledger.read_binding(producer_address)
    if producer is None:
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate ESCALATE review seat {review_address!r} points at absent candidate "
                f"{producer_address!r}"
            ],
            warnings=[],
            binding=review,
        )
    if (
        signal_artifact_seen_at
        and producer.get("gate_escalation_signal_artifact_seen_at") == signal_artifact_seen_at
    ):
        return _close_review_after_gate_verdict(
            review_address,
            review,
            producer_address,
            verdict="ESCALATE",
            signal_artifact_seen_at=signal_artifact_seen_at,
            verdict_notes=verdict_notes,
            event="gate_review_escalated",
        )
    if producer.get("state") != "running":
        return executor.TransitionResult(
            ok=False,
            errors=[
                f"gate ESCALATE candidate must be running; "
                f"{producer_address!r} is {producer.get('state')!r}"
            ],
            warnings=[],
            binding=producer,
        )
    if producer.get("gate_state") == "gate_escalated":
        return _close_review_after_gate_verdict(
            review_address,
            review,
            producer_address,
            verdict="ESCALATE",
            signal_artifact_seen_at=signal_artifact_seen_at,
            verdict_notes=verdict_notes,
            event="gate_review_escalated",
        )

    from harnessd import clock

    now = clock.now_utc()
    producer_artifacts = _producer_artifact_paths(producer_address, producer)
    gate_artifact_path = _gate_artifact_path_for_review(review_address)
    escalation_count = _next_gate_escalation_count(producer)
    binding_delta = {
        "gate_state_before": producer.get("gate_state"),
        "gate_state": "gate_escalated",
        "gate_escalated_at": now,
        "gate_escalation_reason": "review_escalated",
        "gate_review_address": review_address,
        "gate_last_escalation_review": review_address,
        "gate_last_escalation_notes": verdict_notes[:500],
        "gate_escalation_count": escalation_count,
        "gate_verdict": "ESCALATE",
        "gate_artifact": str(gate_artifact_path) if gate_artifact_path else None,
        "producer_artifacts": producer_artifacts,
    }
    if signal_artifact_seen_at:
        binding_delta["gate_escalation_signal_artifact_seen_at"] = signal_artifact_seen_at

    result = executor.transition(
        producer_address,
        expected_state="running",
        expected_generation=producer["generation"],
        expected_owner_token=producer.get("owner_token"),
        target_state="running",
        binding_delta=binding_delta,
        event="gate_escalated",
        summary=(
            f"gate ESCALATE from {review_address} for {producer_address}: "
            "parent-altitude decision required (GATE-LIFECYCLE)"
        ),
        artifacts=_artifact_list(producer_artifacts, gate_artifact_path),
    )
    if result is not None and getattr(result, "ok", False):
        _apply_gate_escalation_threshold(
            producer_address,
            review_address,
            ledger.read_binding(producer_address) or producer,
            escalation_count,
        )
        _notify_parent_of_gate_review_escalation(
            producer_address,
            review_address,
            ledger.read_binding(producer_address) or producer,
            verdict_notes=verdict_notes,
        )
        close_result = _close_review_after_gate_verdict(
            review_address,
            review,
            producer_address,
            verdict="ESCALATE",
            signal_artifact_seen_at=signal_artifact_seen_at,
            verdict_notes=verdict_notes,
            event="gate_review_escalated",
        )
        if close_result is not None and not getattr(close_result, "ok", False):
            return close_result
    return result


def _close_review_after_gate_verdict(
    review_address: str,
    review: dict,
    producer_address: str,
    *,
    verdict: str,
    signal_artifact_seen_at: Optional[str],
    verdict_notes: str,
    event: str,
):
    """Close a reviewer after one verdict turn.

    Reviewers do not carry context across candidate iterations. PASS already closes through
    ``pass_gate``; BOUNCE and ESCALATE use this shared close so a later candidate must claim a fresh
    incarnation with a fresh owner token.
    """
    if not review or review.get("state") != "running":
        return None
    from harnessd import clock, return_contract as _return_contract

    now = clock.now_utc()
    gate_artifact_path = _return_contract.gate_artifact_path(review_address, review)
    producer_artifacts = _producer_artifact_paths(producer_address)
    binding_delta = {
        "terminal_signal": "DONE",
        "terminal_signal_at": now,
        "in_flight_release": True,
        "gate_verdict": verdict,
        "gate_for": producer_address,
        "gate_verdict_candidate": producer_address,
        "gate_verdict_at": now,
        "gate_artifact": str(gate_artifact_path) if gate_artifact_path else None,
        "producer_artifacts": producer_artifacts,
    }
    if verdict_notes:
        binding_delta["gate_verdict_notes"] = verdict_notes[:500]
    if signal_artifact_seen_at:
        binding_delta["gate_review_signal_artifact_seen_at"] = signal_artifact_seen_at
    stamp = _return_contract.artifact_stamp(gate_artifact_path, prefix="gate_artifact")
    if stamp is not None:
        binding_delta.update(stamp)
    result = executor.transition(
        review_address,
        expected_state="running",
        expected_generation=review["generation"],
        expected_owner_token=review.get("owner_token"),
        target_state="done",
        binding_delta=binding_delta,
        event=event,
        summary=(
            f"review gate {review_address} completed {verdict} for {producer_address}; "
            "reviewer closes after one verdict turn"
        ),
        artifacts=_artifact_list(producer_artifacts, gate_artifact_path),
    )
    if result is not None and getattr(result, "ok", False):
        kill_stale_pane((getattr(result, "binding", None) or review).get("tmux_target"))
    return result


def _gate_bounce_cap(producer: dict) -> int:
    try:
        cap = int(producer.get("gate_bounce_cap") or 3)
    except (TypeError, ValueError):
        cap = 3
    return max(cap, 0)


def _next_gate_escalation_count(producer: dict) -> int:
    try:
        prior = int((producer or {}).get("gate_escalation_count") or 0)
    except (TypeError, ValueError):
        prior = 0
    return prior + 1


def _l1_question_route(node_address: str) -> tuple[Optional[str], Optional[str]]:
    """Return the direct L1 child that can canonically ask L1 about this subtree."""
    lineage = ancestors_inclusive(node_address)
    for index, binding in enumerate(lineage):
        if binding.get("level") != "L1" or not str(binding.get("node_address") or "").endswith("#exec"):
            continue
        if index == 0:
            return None, binding.get("node_address")
        return lineage[index - 1].get("node_address"), binding.get("node_address")
    return None, None


def _apply_gate_escalation_threshold(
    producer_address: str,
    review_address: str,
    producer_binding: dict,
    count: int,
) -> None:
    """Apply the owner-ruled fifth and tenth parent-judgment thresholds."""
    from harnessd import messages as _messages
    from harnessd import return_contract as _return_contract

    gate_id = str((producer_binding or {}).get("gate_id") or "")
    if count == 5:
        _return_contract.elevate_gate_nonconvergence(
            producer_address,
            producer_binding,
            gate_id=gate_id,
            review_address=review_address,
            count=count,
        )
        return
    if count != 10:
        return

    executor.pause(
        producer_address,
        paused_at=clock.now_utc(),
        expected_owner_token=(producer_binding or {}).get("owner_token"),
    )
    live = ledger.read_binding(producer_address) or producer_binding
    executor.journal(
        producer_address,
        event="gate_escalation_nonconverging",
        from_state=live.get("state"),
        to_state=live.get("state"),
        lease_epoch=live.get("lease_epoch"),
        binding_delta={
            "gate_id": gate_id,
            "gate_review_address": review_address,
            "gate_escalation_count": count,
            "threshold": 10,
            "paused_at": live.get("paused_at"),
        },
        summary=(
            f"gate {producer_address} reached escalation count {count}; parent judgment is not "
            "converging, so the affected subtree is paused and an owner-facing question is "
            "raised through L1"
        ),
    )
    sender, l1 = _l1_question_route(producer_address)
    if not sender or not l1 or ledger.RUNTIME_ROOT is None:
        return
    message_id = "gate-convergence-" + hashlib.sha256(
        f"{producer_address}|{gate_id}|{count}".encode("utf-8")
    ).hexdigest()[:24]
    content = (
        "# OWNER-FACING gate-convergence question\n\n"
        f"Gate `{gate_id or 'unknown'}` for `{producer_address}` has escalated {count} times. "
        "Parent judgment is not converging, so the affected subtree is paused.\n\n"
        "Present this question to the owner through L1's owner surface before resuming the "
        "subtree: should L1 re-plan the work, kill it, or issue a final ruling?\n"
    )
    _messages.author_and_submit(
        sender,
        target=l1,
        message_id=message_id,
        content=content,
        summary=f"Owner judgment required for non-converging gate {producer_address}.",
        needs_answer=True,
        tags=["gate-convergence", "owner-facing"],
        metadata={
            "kind": "gate-convergence",
            "gate": producer_address,
            "gate_id": gate_id,
            "gate_escalation_count": count,
        },
        runtime_root=ledger.RUNTIME_ROOT,
    )


def _escalate_gate_bounce_cap(
    producer_address: str,
    review_address: str,
    producer: dict,
    *,
    cap: int,
    prior_count: int,
    signal_artifact_seen_at: Optional[str],
    verdict_notes: str,
):
    from harnessd import clock

    review = ledger.read_binding(review_address)
    now = clock.now_utc()
    producer_artifacts = _producer_artifact_paths(producer_address, producer)
    gate_artifact_path = _gate_artifact_path_for_review(review_address)
    escalation_count = _next_gate_escalation_count(producer)
    binding_delta = {
        "gate_state_before": producer.get("gate_state"),
        "gate_state": "gate_escalated",
        "gate_escalated_at": now,
        "gate_escalation_reason": "bounce_cap_exhausted",
        "gate_bounce_count": prior_count,
        "gate_bounce_cap": cap,
        "gate_escalation_count": escalation_count,
        "gate_review_address": review_address,
        "gate_last_bounce_review": review_address,
        "gate_last_bounce_notes": verdict_notes[:500],
        "gate_verdict": "BOUNCE",
        "gate_artifact": str(gate_artifact_path) if gate_artifact_path else None,
        "producer_artifacts": producer_artifacts,
    }
    if signal_artifact_seen_at:
        binding_delta["gate_escalation_signal_artifact_seen_at"] = signal_artifact_seen_at
    result = executor.transition(
        producer_address,
        expected_state="running",
        expected_generation=producer["generation"],
        expected_owner_token=producer.get("owner_token"),
        target_state="running",
        binding_delta=binding_delta,
        event="gate_escalated",
        summary=(
            f"gate bounce cap exhausted for {producer_address} via {review_address}: "
            f"count={prior_count}, cap={cap}; escalating to parent (GATE-LIFECYCLE)"
        ),
        artifacts=_artifact_list(producer_artifacts, gate_artifact_path),
    )
    if result is not None and getattr(result, "ok", False):
        _apply_gate_escalation_threshold(
            producer_address,
            review_address,
            ledger.read_binding(producer_address) or producer,
            escalation_count,
        )
        _notify_parent_of_gate_escalation(
            producer_address,
            review_address,
            ledger.read_binding(producer_address) or producer,
            cap=cap,
            prior_count=prior_count,
            verdict_notes=verdict_notes,
        )
        close_result = _close_review_after_gate_verdict(
            review_address,
            review or {},
            producer_address,
            verdict="BOUNCE",
            signal_artifact_seen_at=signal_artifact_seen_at,
            verdict_notes=verdict_notes,
            event="gate_review_escalated",
        )
        if close_result is not None and not getattr(close_result, "ok", False):
            return close_result
    return result


def _notify_parent_of_gate_escalation(
    producer_address: str,
    review_address: str,
    producer_binding: dict,
    *,
    cap: int,
    prior_count: int,
    verdict_notes: str,
) -> None:
    """Append one ``gate_escalated`` pointer line to the parent inbox."""
    from harnessd import clock

    try:
        parent = (producer_binding or {}).get("parent_address")
        if not parent or ledger.RUNTIME_ROOT is None:
            return
        parent_binding = ledger.read_binding(parent)
        if parent_binding is None or states.is_terminal(parent_binding.get("state")):
            return
        producer_dir = addressing.node_dir(producer_address, ledger.RUNTIME_ROOT)
        review_dir = addressing.node_dir(review_address, ledger.RUNTIME_ROOT)
        inbox = addressing.inbox_path(parent, ledger.RUNTIME_ROOT)
        producer_artifacts = _producer_artifact_paths(producer_address, producer_binding)
        gate_artifact = _gate_artifact_path_for_review(review_address)
        if _inbox_has_line(
            inbox,
            type="gate_escalated",
            candidate=producer_address,
            review=review_address,
            signal_artifact_seen_at=(producer_binding or {}).get("gate_escalation_signal_artifact_seen_at"),
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        note = verdict_notes[:200]
        line = json.dumps({
            "from": "harnessd",
            "type": "gate_escalated",
            "candidate": producer_address,
            "review": review_address,
            "reason": (producer_binding or {}).get("gate_escalation_reason") or "bounce_cap_exhausted",
            "signal_artifact_seen_at": (producer_binding or {}).get("gate_escalation_signal_artifact_seen_at"),
            "producer_artifacts": producer_artifacts,
            "gate_artifact": str(gate_artifact) if gate_artifact else None,
            "message": (
                f"Gate bounce cap exhausted for {producer_address} "
                f"(count={prior_count}, cap={cap}) after review {review_address}. "
                + (f"Latest verdict notes: {note}. " if note else "")
                + _producer_artifact_pointer(producer_dir, producer_artifacts)
                + "; "
                + _review_artifact_pointer(review_address, review_dir)
                + ". Decide at parent altitude."
            ),
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort, never fails gate escalation
        pass


def _notify_parent_of_gate_review_escalation(
    producer_address: str,
    review_address: str,
    producer_binding: dict,
    *,
    verdict_notes: str,
) -> None:
    """Append one explicit gate-escalation pointer line to the parent inbox."""
    from harnessd import clock

    try:
        parent = (producer_binding or {}).get("parent_address")
        if not parent or ledger.RUNTIME_ROOT is None:
            return
        parent_binding = ledger.read_binding(parent)
        if parent_binding is None or states.is_terminal(parent_binding.get("state")):
            return
        producer_dir = addressing.node_dir(producer_address, ledger.RUNTIME_ROOT)
        review_dir = addressing.node_dir(review_address, ledger.RUNTIME_ROOT)
        inbox = addressing.inbox_path(parent, ledger.RUNTIME_ROOT)
        producer_artifacts = _producer_artifact_paths(producer_address, producer_binding)
        gate_artifact = _gate_artifact_path_for_review(review_address)
        if _inbox_has_line(
            inbox,
            type="gate_escalated",
            candidate=producer_address,
            review=review_address,
            signal_artifact_seen_at=(producer_binding or {}).get("gate_escalation_signal_artifact_seen_at"),
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        note = verdict_notes[:300]
        line = json.dumps({
            "from": "harnessd",
            "type": "gate_escalated",
            "candidate": producer_address,
            "review": review_address,
            "reason": (producer_binding or {}).get("gate_escalation_reason") or "review_escalated",
            "signal_artifact_seen_at": (producer_binding or {}).get("gate_escalation_signal_artifact_seen_at"),
            "producer_artifacts": producer_artifacts,
            "gate_artifact": str(gate_artifact) if gate_artifact else None,
            "message": (
                f"Gate escalated for {producer_address} by {review_address}. "
                + (f"Escalation notes: {note}. " if note else "")
                + _producer_artifact_pointer(producer_dir, producer_artifacts)
                + "; "
                + _review_artifact_pointer(review_address, review_dir)
                + ". Decide at parent altitude."
            ),
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort, never fails gate escalation
        pass


def _notify_producer_of_gate_bounce(
    producer_address: str,
    review_address: str,
    verdict_notes: str,
) -> None:
    """Append one ``gate_bounced`` pointer line to the producer inbox."""
    from harnessd import clock

    try:
        if ledger.RUNTIME_ROOT is None:
            return
        producer_binding = ledger.read_binding(producer_address)
        if producer_binding is None or states.is_terminal(producer_binding.get("state")):
            return
        review_dir = addressing.node_dir(review_address, ledger.RUNTIME_ROOT)
        inbox = addressing.inbox_path(producer_address, ledger.RUNTIME_ROOT)
        bounce_count = producer_binding.get("gate_bounce_count")
        gate_artifact = _gate_artifact_path_for_review(review_address)
        if _inbox_has_line(
            inbox,
            type="gate_bounced",
            review=review_address,
            gate_bounce_count=bounce_count,
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        note = verdict_notes[:300]
        line = json.dumps({
            "from": "harnessd",
            "type": "gate_bounced",
            "review": review_address,
            "gate_bounce_count": bounce_count,
            "signal_artifact_seen_at": producer_binding.get("gate_bounce_signal_artifact_seen_at"),
            "gate_artifact": str(gate_artifact) if gate_artifact else None,
            "message": (
                f"Gate bounced by {review_address}. "
                + (f"Verdict notes: {note}. " if note else "")
                + f"Read {_review_artifact_pointer(review_address, review_dir)}, fix the named defects, "
                "then submit a fresh candidate signal."
            ),
            "ts": clock.now_utc(),
        })
        with inbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — notification is best-effort, never fails a gate BOUNCE
        pass


def recover_gate_notification(producer_address: str, producer_binding: Optional[dict] = None) -> None:
    """Replay the parent/reviewer/producer pointer implied by a committed gate state.

    Gate transitions are the durable truth. The inbox append is deliberately best-effort, so the
    daemon can call this sweep to repair a lost wake without mutating the ledger. Notification
    helpers are idempotent on the committed gate identity.
    """
    producer = producer_binding or ledger.read_binding(producer_address)
    if not producer or producer.get("gate_required") is not True:
        return
    review_address = producer.get("gate_review_address")
    if not review_address:
        return
    state = producer.get("gate_state")
    if state == "candidate_submitted":
        failure = _review_slot_failure_for_producer(producer_address, review_address)
        if failure is not None:
            failure_class, detail = failure
            _fail_gate_for_producer(
                producer_address,
                producer,
                review_address,
                failure_reason=failure_class,
                failure_class=failure_class,
                detail=detail,
                signal_artifact_seen_at=producer.get("gate_candidate_signal_artifact_seen_at"),
            )
            return
        _notify_review_of_candidate(
            producer_address,
            producer,
            review_address,
            {
                "gate_id": producer.get("gate_id"),
                "gate_review_packet": producer.get("gate_review_packet"),
            },
        )
    elif state == "gate_bounced":
        if not (
            producer.get("gate_resolution_source") == "parent_return"
            and producer.get("gate_state_before") == "gate_escalated"
        ):
            _notify_producer_of_gate_bounce(
                producer_address,
                review_address,
                producer.get("gate_last_bounce_notes") or "",
            )
    elif state == "gate_failed":
        _notify_parent_of_gate_failure(
            producer_address,
            review_address,
            producer,
            failure_class=producer.get("gate_failure_class") or "gate_failed",
            failure_count=producer.get("gate_failure_count"),
            detail=producer.get("gate_failure_detail") or "",
        )
    elif state == "gate_escalated":
        reason = producer.get("gate_escalation_reason")
        if reason == "bounce_cap_exhausted":
            try:
                prior_count = int(producer.get("gate_bounce_count") or 0)
            except (TypeError, ValueError):
                prior_count = 0
            _notify_parent_of_gate_escalation(
                producer_address,
                review_address,
                producer,
                cap=_gate_bounce_cap(producer),
                prior_count=prior_count,
                verdict_notes=producer.get("gate_last_bounce_notes") or "",
            )
        else:
            _notify_parent_of_gate_review_escalation(
                producer_address,
                review_address,
                producer,
                verdict_notes=producer.get("gate_last_escalation_notes") or "",
            )
    elif state == "gate_passed":
        _notify_parent_of_gate_pass(
            producer_address,
            review_address,
            producer,
            producer.get("gate_verdict_notes") or "",
        )


def recover_terminal_notification(node_address: str, binding: Optional[dict] = None) -> None:
    """Replay non-gate parent relay pointers implied by durable terminal binding state."""
    live = binding or ledger.read_binding(node_address)
    if not live:
        return
    if live.get("semantic_cell_role"):
        # Semantic completions are consumed by the daemon's bundle-keyed cell reconcile.  Only
        # the final evidence index wakes L1; per-seat completion is intentionally silent.
        return
    terminal_signal = live.get("terminal_signal")
    if terminal_signal == "ESCALATED" and live.get("state") == "running":
        _notify_parent_of_escalation(node_address, live)
        return
    if terminal_signal not in _COLLAPSE_TARGETS:
        return
    if not states.is_terminal(live.get("state")):
        return
    if live.get("gate_state") or live.get("gate_for"):
        return
    if live.get("in_flight_release") is not True:
        return
    _notify_parent_of_collapse(node_address, live, terminal_signal)


def _is_review_check_binding(binding: Optional[dict]) -> bool:
    if not binding:
        return False
    if binding.get("review_check_for"):
        return True
    role_variant = str(binding.get("role_variant") or "")
    return role_variant.endswith("#review-check")


def _kill_collapsed_review_check_pane(binding: Optional[dict]) -> None:
    if not _is_review_check_binding(binding):
        return
    try:
        kill_stale_pane((binding or {}).get("tmux_target"))
    except Exception:  # noqa: BLE001 — post-collapse review-check pane reap is best-effort
        pass


def _producer_artifact_paths(
    producer_address: str,
    producer_binding: Optional[dict] = None,
) -> list[str]:
    """Return existing node-root producer evidence artifacts, never review-owned artifacts."""
    if ledger.RUNTIME_ROOT is None:
        return []
    producer_binding = producer_binding or ledger.read_binding(producer_address) or {}
    producer_dir = addressing.node_dir(producer_address, ledger.RUNTIME_ROOT)
    level = (producer_binding.get("level") or "").strip()
    preferred_by_level = {
        "L5": "report.md",
        "L4": "composition-report.md",
        "L3": "area-composition-review.md",
        "L2": "composition-review.md",
    }
    names = [
        preferred_by_level.get(level, "report.md"),
        "report.md",
        "composition-report.md",
        "area-composition-review.md",
        "composition-review.md",
    ]
    seen: set[str] = set()
    paths: list[str] = []
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        path = producer_dir / name
        if path.is_file():
            paths.append(str(path))
    return paths


def _producer_artifact_pointer(producer_dir: Path, producer_artifacts: list[str]) -> str:
    if producer_artifacts:
        return "Producer artifact(s): " + ", ".join(producer_artifacts)
    return f"Candidate artifacts are in {producer_dir}/"


def _gate_artifact_path_for_review(review_address: str):
    try:
        from harnessd import return_contract as _return_contract

        return _return_contract.gate_artifact_path(
            review_address,
            ledger.read_binding(review_address),
        )
    except Exception:  # noqa: BLE001 — wake text must not break routing
        return None


def _artifact_list(producer_artifacts: list[str], gate_artifact_path) -> list[str]:
    artifacts = list(producer_artifacts or [])
    if gate_artifact_path is not None:
        artifacts.append(str(gate_artifact_path))
    return artifacts


def _review_artifact_pointer(review_address: str, review_dir: Path) -> str:
    """Human-readable pointer to the review summary and authoritative gate artifact."""
    report_path = review_dir / "report.md"
    gate_path = _gate_artifact_path_for_review(review_address) or report_path
    if gate_path == report_path:
        return f"review gate artifact at {gate_path}"
    return f"review gate artifact at {gate_path}"


def collapse(
    node_address: str,
    terminal_signal: str,
    *,
    expected_owner_token: Optional[str],
    **_unused,
) -> None:
    """The terminal collapse (§6.1 / §3.6): route the terminal transition + carry the release-decrement.

    ``DONE`` -> done, ``FAILED``/``DIED*`` -> failed, ``DEAD`` -> dead. The terminal transaction
    carries cluster ④'s in_flight RELEASE-DECREMENT (symmetric to STEP1's claim-increment). NOT a
    writer itself — routes through ``executor.transition`` (the single writer), crash-safe via
    last_applied_seq.

    ESCALATED is NOT a collapse (§3.6, ASYMMETRIC): the terminal_signal is set but the lifecycle state
    STAYS ``running`` and the node is NOT torn down. ``collapse`` REFUSES the ESCALATED signal (raises)
    so a caller cannot collapse a node that is merely waiting for the answer round-trip.
    """
    if terminal_signal == "ESCALATED":
        raise ValueError(
            "ESCALATED is NOT a collapse (§3.6 ASYMMETRIC): the terminal_signal is set but the "
            "lifecycle state STAYS running — collapsing on ESCALATED would tear the node off its "
            "slot while it waits for the answer round-trip. Refusing."
        )

    target_state = _COLLAPSE_TARGETS.get(terminal_signal)
    if target_state is None:
        raise ValueError(
            f"unknown terminal_signal {terminal_signal!r}: collapse routes only the terminal "
            f"vocabulary {sorted(_COLLAPSE_TARGETS)} (ESCALATED is asymmetric, not a collapse)"
        )

    live = ledger.read_binding(node_address)
    if live is None:
        return None

    # The terminal transition carries the in_flight RELEASE-DECREMENT (the slot the §6.1 claim
    # reserved is released here). We record the terminal_signal into the binding alongside the
    # lifecycle collapse, symmetric to STEP1's claim-increment seat.
    #
    # LR-23 (remedy a, user-ruled 2026-06-12) — THE GATE EVIDENCE-STAMP: the gate verdict is a
    # SNAPSHOT, not a seal (Run-2 ws-3: the fenced-out pane stays alive and edited report.md
    # 11s AFTER its accepted DONE — the fence protects only the LEDGER). At the accepted-DONE
    # collapse, freeze the FACT of what the gate approved: sha256 + byte-size of report.md land
    # in THIS row's binding_delta (the durable WAL audit fact) AND on the binding (the live
    # comparison seat promote reads). DONE ONLY — FAILED/DIED*/DEAD collapses passed no gate and
    # ESCALATED never reaches here (refused above). A missing report.md (the substrate paths
    # drive collapse without a tree) stamps nothing and is NEVER an error
    # (return_contract.report_stamp is best-effort by contract — it hashes the SAME report.md
    # the E2 walker just approved). Detection only: no seal, no reap (options b/c NOT ruled).
    binding_delta = {
        "terminal_signal": terminal_signal,
        "in_flight_release": True,
    }
    if terminal_signal == "DONE":
        from harnessd import return_contract as _return_contract  # local import: no module cycle

        stamp = _return_contract.report_stamp(node_address)
        if stamp is not None:
            binding_delta.update(stamp)

    # RETURN the TransitionResult (review chokepoint-2): a FAILED terminal transition (a CAS miss /
    # fencing rejection) must NOT be reported as success. Callers (the watchdog, the kill IPC) route the
    # result; a `return None` here silently swallowed a fenced abort and told every caller it collapsed.
    result = executor.transition(
        node_address,
        expected_state=live["state"],
        expected_generation=live["generation"],
        expected_owner_token=expected_owner_token,
        target_state=target_state,
        binding_delta=binding_delta,
        event=_COLLAPSE_EVENTS[terminal_signal],  # the §3.6 normative event name (SML-01)
        summary=(
            f"terminal collapse: {terminal_signal} -> {target_state} "
            "(carries ④ in_flight RELEASE-DECREMENT, symmetric to STEP1 claim-increment; §6.1/§3.6)"
        ),
    )
    # LR-11 — COLLAPSE WAKES THE PARENT (agent-lifecycle: completion flows UP; COMMUNICATION:
    # the report-up nudge). On a SUCCESSFUL collapse, append ONE child_collapsed pointer line to
    # the parent seat's inbox; the ③-wake delivers the nudge on the next tick. Without this the
    # parent only rediscovers tree state via the generic idle ladder (observed live 2026-06-11:
    # L1 sat unaware of L2's completion until an operator hand-delivered the notification).
    # Best-effort: a notification hiccup never converts a clean collapse into a failure; the
    # parentless L1 root and an absent/terminal parent are silent no-ops.
    if result is not None and getattr(result, "ok", False):
        collapsed_binding = getattr(result, "binding", None) or live
        _notify_parent_of_collapse(node_address, collapsed_binding, terminal_signal)
        _kill_collapsed_review_check_pane(collapsed_binding)
    return result


def _review_check_parent_accepts_collapse(
    collapsed_binding: dict,
    *,
    parent_address: str,
    parent_binding: dict,
) -> bool:
    """Return whether a review-check collapse still belongs to the current review lead.

    Review-check seats are child actors of a gate lead, but their parent address is the stable
    logical ``#review`` seat. After a gate passes, the same review address can later be reused for
    a fresh candidate. Lost-wake recovery must not replay old check completions into that new
    review inbox just because the address matches.
    """
    review_address = (collapsed_binding or {}).get("review_check_for")
    if not review_address:
        return True
    if parent_address != review_address:
        return False
    if (parent_binding or {}).get("state") != "running":
        return False
    if (parent_binding or {}).get("node_address") not in {None, review_address}:
        return False
    candidate = (collapsed_binding or {}).get("review_check_candidate")
    if candidate and (parent_binding or {}).get("gate_for") != candidate:
        return False
    producer = ledger.read_binding(candidate) if candidate else None
    if not producer:
        return False
    if producer.get("gate_state") != "candidate_submitted":
        return False
    if producer.get("gate_review_address") != review_address:
        return False
    expected_gate_id = str((collapsed_binding or {}).get("gate_id") or "").strip()
    current_gate_id = str(producer.get("gate_id") or "").strip()
    if expected_gate_id and current_gate_id != expected_gate_id:
        return False
    return True


def _notify_parent_of_collapse(node_address: str, collapsed_binding: dict, terminal_signal: str) -> None:
    """Append the LR-11 ``child_collapsed`` pointer line to the parent's inbox (best-effort)."""
    from harnessd import clock  # local import, matching the kickoff line's style

    try:
        if (collapsed_binding or {}).get("semantic_cell_role"):
            return
        parent = (collapsed_binding or {}).get("parent_address")
        if not parent or ledger.RUNTIME_ROOT is None:
            return
        parent_binding = ledger.read_binding(parent)
        if parent_binding is None or states.is_terminal(parent_binding.get("state")):
            return
        if not _review_check_parent_accepts_collapse(
            collapsed_binding,
            parent_address=parent,
            parent_binding=parent_binding,
        ):
            return
        evidence = ""
        try:
            sig_path = addressing.signal_path(node_address, ledger.RUNTIME_ROOT)
            if sig_path.is_file():
                sig = json.loads(sig_path.read_text(encoding="utf-8"))
                evidence = str((sig.get("evidence") or {}).get("notes") or "")[:200]
        except (OSError, ValueError):
            pass
        node_dir = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
        inbox = addressing.inbox_path(parent, ledger.RUNTIME_ROOT)
        collapse_generation = (collapsed_binding or {}).get("generation")
        collapse_lease_epoch = (collapsed_binding or {}).get("lease_epoch")
        collapse_already_present = _inbox_has_line(
            inbox,
            type="child_collapsed",
            child=node_address,
            terminal_signal=terminal_signal,
            collapse_lease_epoch=collapse_lease_epoch,
        )
        if not collapse_already_present:
            inbox.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({
                "from": "harnessd",
                "type": "child_collapsed",
                "barrier_mode": True,
                "child": node_address,
                "terminal_signal": terminal_signal,
                "terminal_note": (collapsed_binding or {}).get("terminal_note"),
                "failure_class": (collapsed_binding or {}).get("failure_class"),
                "collapse_generation": collapse_generation,
                "collapse_lease_epoch": collapse_lease_epoch,
                "message": (
                    f"Your child {node_address} collapsed {terminal_signal}. "
                    + (f"Sign-off notes: {evidence}. " if evidence else "")
                    + f"Its report and artifacts are in {node_dir}/ (read report.md). "
                    "Proceed with your own duties for this completion per your role documents."
                ),
                "ts": clock.now_utc(),
            })
            with inbox.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        _notify_parent_barrier_complete(
            parent,
            node_address=node_address,
            collapsed_binding=collapsed_binding,
            terminal_signal=terminal_signal,
        )
    except Exception:  # noqa: BLE001 — notification is best-effort, never fails a clean collapse
        pass


def _completion_cohort(address: str, binding: dict) -> str | None:
    if binding.get("semantic_cell_role"):
        return None
    if binding.get("review_check_for"):
        return "review_check"
    _path, seat = addressing.split_address(address)
    role_variant = str(binding.get("role_variant") or "")
    if seat in {"review", "review-check"} or role_variant.endswith(("#review", "#review-check")):
        return None
    return "product"


def _notify_parent_barrier_complete(
    parent: str,
    *,
    node_address: str,
    collapsed_binding: dict,
    terminal_signal: str,
) -> None:
    """Wake once when the collapsing child's address-bound cohort has no unfinished member."""
    cohort = _completion_cohort(node_address, collapsed_binding)
    if cohort is None:
        return
    bindings = ledger.all_nodes()
    members: list[tuple[str, dict]] = []
    for address, binding in bindings.items():
        member_cohort = _completion_cohort(address, binding)
        if cohort == "review_check":
            belongs = (
                member_cohort == cohort
                and binding.get("review_check_for") == parent
            )
        else:
            belongs = (
                member_cohort == cohort
                and binding.get("parent_address") == parent
            )
        if belongs:
            members.append((address, binding))
    if not members or any(not states.is_terminal(binding.get("state")) for _, binding in members):
        return
    member_surface = [
        {
            "address": address,
            "lease_epoch": binding.get("lease_epoch"),
            "generation": binding.get("generation"),
        }
        for address, binding in sorted(members)
    ]
    barrier_id = hashlib.sha256(
        json.dumps(
            {"parent": parent, "cohort": cohort, "members": member_surface},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    inbox = addressing.inbox_path(parent, ledger.RUNTIME_ROOT)
    if _inbox_has_line(
        inbox,
        type="barrier_complete",
        cohort=cohort,
        barrier_id=barrier_id,
    ):
        return
    line = {
        "from": "harnessd",
        "type": "barrier_complete",
        "cohort": cohort,
        "barrier_id": barrier_id,
        "completed_child": node_address,
        "terminal_signal": terminal_signal,
        "members": [item["address"] for item in member_surface],
        "message": (
            f"All unfinished children in the {cohort} cohort are terminal. "
            "Read their silent child_collapsed pointers and continue."
        ),
        "ts": clock.now_utc(),
    }
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with inbox.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
