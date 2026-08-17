"""daemon — the run-scoped harnessd process: boot/recover + reconcile timer + clean idle exit.

The daemon is the ROOT of the supervision-tree custody chain — it starts L1, which has no parent
agent (DAEMON §7: "L1 has no parent agent — the daemon is what starts L1"). A per-run launchd job
restarts crashes only while work is live; no job is loaded at login. Three responsibilities
(IMPLEMENTATION-PLAN §3 module table, daemon.py row):
  * ``boot`` — acquire the PERSISTENT single-instance lock (`.harnessd.instance.lock`, §2.3 —
    held for the process lifetime; a second instance refuses LOUDLY before writing anything),
    then write the §2.3 runtime.json descriptor, then run genesis end-to-end (lock ->
    runtime.json -> preconditions -> reconcile_on_restart -> spawn-or-resume L1, Integration A).
  * ``poll_loop`` — reconcile_tick on a timer while any durable binding is nonterminal, with the
    body FACTORED into a single drivable iteration (``poll_once``) so a test can drive exactly ONE
    tick. A definitive empty/all-terminal read after a completed tick returns normally.
  * ``write_status`` — the lock-FREE status sidecar (the ONE deliberate atomicity carve-out, §4.4):
    a best-effort liveness mirror written every poll WITHOUT the EX lock (taking the lock would
    serialize a non-event against real mutations every tick). Recovery NEVER trusts it (the ledger is
    truth). The SAME carve-out covers ``stamp_last_tick``: ``poll_once`` stamps
    ``runtime.json.last_tick_at`` lock-free every tick — the §2.6 completed-tick diagnostic/future
    hang-detector surface (best-effort, zero WAL rows, recovery never trusts it).

Authoritative sources:
  - IMPLEMENTATION-PLAN §2.12 (``boot(runtime) -> None`` / ``poll_loop(interval_s) -> NoReturn``),
    §3 module table (daemon.py row, L64), §3 on-disk tree (runtime.json L459 / status.json L460).
  - DAEMON §2.2 (run-scoped service-manager protection), §2.3 (runtime.json / status.json), §4.4 (the lock-free
    status carve-out), §5.2 (continuous reconciliation), §7 (the genesis sequence boot drives).

BUILDER DECISIONS (the §2.12 details the frozen tests leave open — stated in the build report):

  * THE ``runtime`` DESCRIPTOR SHAPE — §2.12 names ``boot(runtime)``; the test threads a permissive
    SimpleNamespace carrying ``runtime_root`` / ``build_id`` / ``config`` (the genesis config) /
    ``adapter`` (the RuntimeAdapter to wire into the chokepoint) / ``tmux`` / ``executor``. boot reads
    them defensively. FORK-DAEMON-RUNTIME: the precise carrier is the caller's; the load-bearing facts
    (boot writes runtime.json AND runs genesis end-to-end) are pinned by the tests.

  * THE ADAPTER WIRING — production boot wires the concrete adapter into the chokepoint via the
    module-level seam (``chokepoint.set_adapter``), exactly the ledger.RUNTIME_ROOT precedent. The
    test pre-installs its FakeAdapter, so boot wires the runtime's adapter ONLY when one is supplied
    (it does NOT clobber a pre-installed test adapter with None).

  * status.json SHAPE + PATH — the §2.3 liveness fields (``pid`` / ``started_at`` / ``incarnation``
    + a best-effort ``runtime_root``), written to ``<runtime_root>/.harnessd/status.json`` (the §3
    on-disk tree, L460). Written via ``store.atomic_replace`` (tmp + fsync + os.replace) so the
    sidecar is never torn — but CRUCIALLY WITHOUT the EX lock (the §4.4 carve-out): the writer never
    enters ``store.file_lock``. It is NOT control state — it appends ZERO WAL rows.

  * THE SINGLE-ITERATION FACTOR — ``poll_once`` is the loop body (ONE ``reconcile.reconcile_tick``);
    ``poll_loop`` calls it on the timer and checks durable live-build truth only after completion.
    The factor lets a test drive exactly one iteration.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Optional, Tuple

from harnessd import clock as _clock
from harnessd import genesis as _genesis_mod
from harnessd import ipc as _ipc_mod
from harnessd import (
    addressing,
    contracts as _contracts_mod,
    ledger,
    lifecycle,
    messages as _messages_mod,
    states,
    store,
    turn_state,
)
from harnessd import reconcile as _reconcile_mod
from harnessd import watchdog as _watchdog_mod
from harnessd.spawn import chokepoint
from harnessd.spawn import outbox as _outbox_mod


# ---------------------------------------------------------------------------
# The §2.3 single-instance guard — a PERSISTENT flock on `.harnessd.instance.lock`, acquired
# non-blocking at boot and held for the process lifetime. DELIBERATELY a separate file from the
# §4.3 per-mutation `.harnessd.lock`: flock conflicts across fds even within one process, so a
# lifetime hold of the mutation-lock file would deadlock every executor mutation (the resolved
# DAEMON §2.3-vs-§4.3 conflict, review SWCAS-02; fork decided 2026-06-10: separate file).
# ---------------------------------------------------------------------------

class DaemonAlreadyRunning(RuntimeError):
    """The §2.3 single-instance refusal — another harnessd instance already holds the lock.

    Raised by ``acquire_instance_lock`` BEFORE boot writes anything (adapter wiring, runtime.json,
    genesis), so a refused second daemon clobbers NOTHING. launchd will not spawn two, but a
    manual launch must not race the service-managed one (DAEMON §2.3). Under launchd KeepAlive a
    duplicate plist would re-raise every ThrottleInterval (≥10s) — loud and throttled, by design.
    """


# The lifetime hold: (path, open handle). The flock dies with the fd/process; tests release it
# explicitly via release_instance_lock(). Module-global on purpose — the daemon process is the
# unit of single-instance-ness, not any one boot() call.
_INSTANCE_LOCK: Optional[Tuple[Path, IO]] = None


def acquire_instance_lock(runtime_root) -> Path:
    """Acquire the §2.3 PERSISTENT single-instance lock; hold it for the process lifetime.

    Non-blocking LOCK_EX|LOCK_NB via ``store.flock_exclusive_nb`` on
    ``genesis.instance_lock_path(runtime_root)``; the open handle is stashed in the module global
    so the flock survives until ``release_instance_lock`` / process exit. Raises
    ``DaemonAlreadyRunning`` (loud, names the path) when another instance holds it.

    IDEMPOTENT for the SAME path: an in-process re-boot of the same root is a no-op return —
    mandatory, because flock self-conflicts across fds, so a naive re-acquire would refuse against
    ITSELF. A hold on a DIFFERENT path (test tmp-root rebinding) is released first.
    """
    global _INSTANCE_LOCK
    path = _genesis_mod.instance_lock_path(runtime_root)
    if _INSTANCE_LOCK is not None:
        held_path, _handle = _INSTANCE_LOCK
        if held_path == path:
            return path  # same-path re-boot: the hold is already ours (idempotent no-op)
        release_instance_lock()  # a different root (test rebinding): drop the stale hold first
    try:
        handle = store.flock_exclusive_nb(path)
    except BlockingIOError as exc:
        raise DaemonAlreadyRunning(
            f"another harnessd instance already holds the lock at {path} — refusing to start "
            "(DAEMON §2.3)"
        ) from exc
    _INSTANCE_LOCK = (path, handle)
    return path


def release_instance_lock() -> None:
    """Release the lifetime instance-lock hold (LOCK_UN + close + clear the global).

    For tests and explicit shutdown paths; production never needs it — the flock dies with the
    process (§2.3). Safe to call when nothing is held (no-op).
    """
    global _INSTANCE_LOCK
    if _INSTANCE_LOCK is None:
        return
    _path, handle = _INSTANCE_LOCK
    _INSTANCE_LOCK = None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# boot — the daemon entry: instance-lock, write runtime.json, then run genesis end-to-end (§2.12).
# ---------------------------------------------------------------------------

def _runtime_root(runtime) -> Path:
    """Resolve the runtime root from the runtime descriptor (falls back to ledger.RUNTIME_ROOT)."""
    root = getattr(runtime, "runtime_root", None)
    if root is None:
        cfg = getattr(runtime, "config", None)
        root = getattr(cfg, "runtime_root", None)
    if root is not None:
        return Path(root)
    if ledger.RUNTIME_ROOT is not None:
        return Path(ledger.RUNTIME_ROOT)
    raise RuntimeError(
        "daemon runtime_root is not configured: pass runtime.runtime_root or bind ledger.RUNTIME_ROOT"
    )


def boot(runtime, *, recover_only: bool = False) -> None:
    """Daemon boot: instance-lock -> runtime.json -> full genesis OR restart reconciliation.

    (0) Acquire the §2.3 PERSISTENT single-instance lock (`.harnessd.instance.lock`) FIRST —
        non-blocking, held for the process lifetime. A second instance raises
        ``DaemonAlreadyRunning`` HERE, before the adapter wiring and the descriptor write, so the
        refused loser clobbers nothing.
    (a) Wire the concrete RuntimeAdapter into the ONE spawn chokepoint (the module-level seam) WHEN
        the runtime supplies one — production wires the real adapter; a test pre-installs its fake and
        passes no adapter (so boot does not clobber it). Then bind the REAL spawn env
        (``runtime.config.env`` -> ``chokepoint.set_spawn_env``, LT-1) so the commissioned 4-var
        OAuth env — not the structural placeholder — is what every production pane boots with.
    (b) Write ``runtime.json`` (the §2.3 daemon runtime descriptor: build-id / started_at / pid) so a
        crash between here and the first genesis write still leaves the descriptor on disk.
    (c) A deliberate one-shot start runs genesis END-TO-END (brief EX lock -> runtime.json ->
        preconditions -> reconcile_on_restart -> spawn-or-resume L1) through the REAL chokepoint.
        A crash restart sets ``recover_only``: it calls the SAME ``reconcile_on_restart`` seam but
        never enters genesis's register/spawn-new-root routing.

    genesis itself re-writes runtime.json inside its brief EX acquire (§7 step 3); writing it here
    too is deliberate (boot owns the descriptor independent of whether genesis reaches its own
    write) and is idempotent (the same atomic-replace target). Lock-acquire order is FIXED:
    instance lock first, always — the per-mutation `.harnessd.lock` is only ever taken while the
    instance lock is already held, so the two-lock ordering is deadlock-free by construction.
    """
    runtime_root = _runtime_root(runtime)

    # (0) THE single-instance gate (§2.3) — BEFORE anything is written or wired.
    acquire_instance_lock(runtime_root)

    build_id = getattr(runtime, "build_id", None)
    cfg = getattr(runtime, "config", None)
    if build_id is None and cfg is not None:
        build_id = getattr(cfg, "build_id", None)

    # (a) Wire the supplied adapter into the chokepoint (do NOT clobber a pre-installed test adapter).
    adapter = getattr(runtime, "adapter", None)
    if adapter is not None:
        chokepoint.set_adapter(adapter)

    # (a2) Bind the REAL spawn env into the chokepoint (LT-1, the set_adapter-mirroring seam):
    # commissioning assembled runtime.config.env (live OAuth token + pinned CLAUDE_CONFIG_DIR) for
    # the genesis credential precondition — without THIS binding it never reached a pane, and every
    # production spawn launched the structural placeholder env ('$HARNESS/...', '<oauth-token-file>').
    # Bound only when the runtime carries one (a sparse test descriptor leaves the fallback intact).
    spawn_env = getattr(cfg, "env", None) if cfg is not None else None
    if spawn_env:
        chokepoint.set_spawn_env(spawn_env)

    # (b) Write the §2.3 runtime descriptor (lock_path names the INSTANCE lock acquired in (0)).
    _genesis_mod.write_runtime_json(
        runtime_root, build_id=build_id,
        lock_path=str(_genesis_mod.instance_lock_path(runtime_root)),
    )

    # (c) Run deliberate genesis OR the recovery-only half. Recovery reuses the existing restart
    # reconciler directly; it must never flow into run_genesis's terminal/absent -> fresh L1 route.
    executor = getattr(runtime, "executor", None)
    if executor is None:
        from harnessd import executor as executor  # noqa: F811 — production default
    tmux = getattr(runtime, "tmux", None)
    if cfg is None:
        raise RuntimeError("daemon.boot requires runtime.config (the genesis config) to run genesis")
    if recover_only:
        _reconcile_mod.reconcile_on_restart(executor, tmux)
    else:
        _genesis_mod.run_genesis(executor, tmux, cfg)
    return None


# ---------------------------------------------------------------------------
# poll_once / poll_loop — the continuous-while-live reconcile sweep (§5.2). poll_once is the
# SINGLE-iteration factor a test drives; poll_loop composes ticks until definitive idle.
# ---------------------------------------------------------------------------

def poll_once(executor, tmux, detector) -> None:
    """ONE poll-loop iteration: run exactly ONE ``reconcile.reconcile_tick`` (§5.2, edge-triggered).

    The factored loop body (§2.12: "factor it so a test can drive a SINGLE iteration"). One tick
    re-derives liveness via the detector and applies the §5.1 resolutions edge-triggered — only a
    state/condition CHANGE appends a run-ledger row. poll_loop calls this on the timer; a test drives
    it exactly once.

    The SAME tick also drains the spawn-request OUTBOXES (FORK-SPAWN-CHANNEL): every live non-leaf
    node's pending child-spawn requests are adjudicated + spawned through the chokepoint. This is how a
    PARENT AGENT's spawn-request becomes a live child — the agent drops a request in its workroot, the
    next poll services it. Best-effort + isolated: an outbox error NEVER aborts the reconcile sweep
    (the supervision tree's liveness must keep advancing even if one node's request is malformed).

    The tick's LAST act stamps ``runtime.json.last_tick_at`` lock-free (§4.4) — the §2.6 hang-detector
    diagnostic/future hang-detector surface. End-of-body placement = completed-tick semantics: a
    wedge inside the tick body stops the stamp advancing IMMEDIATELY. Best-effort (the same isolation
    as the outbox drain), zero WAL rows; recovery never trusts it.

    THE RECONCILE REPORT IS CONSUMED (RR-4): the v1 escalation seat ("ship the detect+escalate
    loop in v1", DAEMON L859-866) was a dead end in the daemon loop — an alive-but-unowned
    ORPHAN pane (the F35 double-spawn symptom) was detected and dropped every tick with zero
    trace, and a CAS-aborted leaf-necro's ``necro_failed`` escalation evaporated. Each escalation
    now lands ONE edge-triggered ``reconcile_escalation`` WAL row (keyed node+kind, the
    watchdog_checkpoint edge-trigger pattern: steady-state re-detection never spams).

    THE SWEEP BRACKETS ITSELF WITH THE USAGE ESCAPE HATCH: it OPENS with the control-socket
    self-probe (a failed probe holds new wakes for this sweep, so the gate is set before the
    watchdog reaches its wake leg) and CLOSES with the freeze detector (which reads the state this
    sweep just left behind). Both are best-effort — neither may take down the sweep it protects.
    """
    _wake_hold_sweep_best_effort()
    _turn_state_sweep_best_effort(executor, tmux)
    report = _reconcile_mod.reconcile_tick(executor, tmux, detector)
    _route_reconcile_escalations(report)
    _watchdog_tick(executor, tmux, detector)
    _service_outboxes_best_effort()
    _redrive_planned_spawns_best_effort(executor, tmux)
    _dispatch_review_check_seats_best_effort()
    _submit_message_markers_best_effort()
    _service_contract_rebinds_best_effort()
    _deliver_contract_amendments_best_effort()
    _recover_message_state_best_effort()
    _submit_coordination_handoff_markers_best_effort()
    _recover_coordination_handoff_notifications_best_effort()
    _submit_plan_alignment_markers_best_effort()
    _reconcile_plan_alignment_cells_best_effort()
    _reconcile_plan_alignment_elevations_best_effort()
    _recover_plan_alignment_notifications_best_effort()
    _recover_gate_notifications_best_effort()
    _recover_terminal_notifications_best_effort()
    _freeze_sweep_best_effort()
    _stamp_last_tick_best_effort()
    return None


def _submit_message_markers_best_effort() -> None:
    """Adopt every unprocessed canonical marker, including markers left by a dead sender."""
    root = Path(ledger.RUNTIME_ROOT) if ledger.RUNTIME_ROOT is not None else None
    if root is None:
        return
    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 - one broken ledger is handled by daemon supervision
        return
    marker_root = root / addressing.NODES_DIRNAME
    if not marker_root.is_dir():
        return
    for marker in sorted(marker_root.glob("**/messages/*.json")):
        senders = _messages_mod.marker_senders(marker, bindings)
        for sender in senders:
            try:
                _messages_mod.submit_marker(sender, marker, runtime_root=root)
            except Exception as exc:  # noqa: BLE001 - malformed marker must not stop other seats
                binding = bindings.get(sender) or {}
                _record_nonterminal_marker_error(
                    sender,
                    binding,
                    marker,
                    marker_kind="message",
                    result=type("_MessageFailure", (), {"errors": [str(exc)]})(),
                )


def _recover_message_state_best_effort() -> None:
    """Withdraw terminal questions and replay lost canonical pointer/checklist delivery."""
    if ledger.RUNTIME_ROOT is None:
        return
    try:
        _messages_mod.withdraw_terminal_questions()
        _messages_mod.recover_deliveries(runtime_root=ledger.RUNTIME_ROOT)
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 - recovery is retried next tick
        return
    for address, binding in bindings.items():
        if not binding.get("turn_hook_profile"):
            continue
        try:
            turn_state.refresh_checklist(
                address,
                binding,
                runtime_root=ledger.RUNTIME_ROOT,
            )
        except Exception:  # noqa: BLE001 - derived checklist repair is isolated per seat
            continue


def _service_contract_rebinds_best_effort() -> None:
    """Adopt holder-fenced rebind markers before computing stale-receipt ripple."""
    if ledger.RUNTIME_ROOT is None:
        return
    try:
        _contracts_mod.service_rebind_markers(runtime_root=ledger.RUNTIME_ROOT)
    except Exception:  # noqa: BLE001 - retried next tick; marker stays visible
        return


def _deliver_contract_amendments_best_effort() -> None:
    """Derive one ordinary 3a amendment message per live stale holder."""
    if ledger.RUNTIME_ROOT is None:
        return
    try:
        _contracts_mod.deliver_amendment_ripple(runtime_root=ledger.RUNTIME_ROOT)
    except Exception:  # noqa: BLE001 - lineage remains canonical and sweep-derived
        return


def _turn_state_sweep_best_effort(executor, tmux) -> None:
    """Adopt node-local hook observations and deliver pending Codex exit prods.

    Each seat is isolated: malformed hook state degrades to the evidence floor and is journaled,
    never allowed to abort the live-run tick.
    """
    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 - a broken ledger is handled by the outer supervision loop
        return
    for address, snapshot in list(bindings.items()):
        if states.is_terminal(snapshot.get("state")):
            continue
        profile = snapshot.get("turn_hook_profile")
        if not profile or profile == turn_state.HOOKLESS_FALLBACK:
            continue
        try:
            _adopt_turn_state_for_seat(executor, tmux, address, snapshot)
        except Exception as exc:  # noqa: BLE001 - one hook surface never aborts other seats
            try:
                executor.turn_state_checkpoint(
                    address,
                    expected_owner_token=snapshot.get("owner_token"),
                    delta={
                        "turn_hook_health": "degraded",
                        "turn_hook_error": f"{type(exc).__name__}: {exc}",
                    },
                    event="turn_hook_degraded",
                    summary=(
                        f"turn hook degraded for {address}: {type(exc).__name__}: {exc}; "
                        "detector evidence fallback remains active"
                    ),
                )
            except Exception:  # noqa: BLE001 - journaling degradation is best-effort
                pass


_TURN_STATE_ADOPTION_LOCK = threading.Lock()


def _adopt_turn_state_for_seat(
    executor,
    tmux,
    address: str,
    snapshot: dict,
    *,
    target_event_id: str | None = None,
) -> dict:
    """Serialize the poll and IPC callers through the one bounded adoption seam."""
    with _TURN_STATE_ADOPTION_LOCK:
        return _adopt_turn_state_for_seat_locked(
            executor,
            tmux,
            address,
            snapshot,
            target_event_id=target_event_id,
        )


def _adopt_turn_state_for_seat_locked(
    executor,
    tmux,
    address: str,
    snapshot: dict,
    *,
    target_event_id: str | None = None,
) -> dict:
    events_path = snapshot.get("turn_events_path") or str(
        addressing.turn_events_path(address, ledger.RUNTIME_ROOT)
    )
    offset = int(snapshot.get("turn_event_acked_offset") or 0)
    rows: list[dict] = []
    parse_errors: list[str] = []
    new_offset = offset
    adopted_ingress: set[str] = set()
    target_found = False
    target_response_written = False
    target_response_required = False

    # Drain to a fixed point. A daemon-side adoption appends derived rows to this same event log;
    # a concurrent raw append after the final read remains beyond ``new_offset`` for the next sweep.
    while True:
        batch, batch_offset, batch_errors = turn_state.read_event_tail(
            events_path,
            new_offset,
        )
        parse_errors.extend(batch_errors)
        if not batch:
            break
        rows.extend(batch)
        new_offset = batch_offset
        adopted_ingress.update(
            str((row.get("detail") or {}).get("ingress_event_id"))
            for row in batch
            if row.get("row_kind") == turn_state.ADOPTED_HOOK_EVENT
            and (row.get("detail") or {}).get("ingress_event_id")
        )
        for row in batch:
            if row.get("row_kind") != turn_state.RAW_HOOK_EVENT:
                continue
            event_id = str(row.get("event_id") or "")
            if event_id == target_event_id:
                target_found = True
                target_response_required = str(row.get("hook_event") or "") in {
                    "PostToolUse",
                    "PostToolUseFailure",
                    "Stop",
                }
            if event_id in adopted_ingress:
                continue
            if row.get("owner_token") != snapshot.get("owner_token"):
                continue
            if str(row.get("hook_event") or "") == "malformed_hook_payload":
                turn_state.record_hook_fault(
                    runtime_root=ledger.RUNTIME_ROOT,
                    node_address=address,
                    owner_token=str(row.get("owner_token") or ""),
                    runtime=str(row.get("runtime") or snapshot.get("runtime") or ""),
                    reason=str(
                        (row.get("payload") or {}).get("fault_reason")
                        or "malformed hook callback"
                    ),
                    binding=snapshot,
                    ingress_event_id=event_id,
                    adopted=True,
                )
                adopted_ingress.add(event_id)
                continue
            response = turn_state.handle_hook_event(
                runtime_root=ledger.RUNTIME_ROOT,
                node_address=address,
                owner_token=str(row.get("owner_token") or ""),
                runtime=str(row.get("runtime") or snapshot.get("runtime") or ""),
                payload=dict(row.get("payload") or {}),
                binding=snapshot,
                ingress_event_id=event_id,
            )
            adopted_ingress.add(event_id)
            if str(row.get("hook_event") or "") in {
                "PostToolUse",
                "PostToolUseFailure",
                "Stop",
            }:
                turn_state.append_hook_response(
                    runtime_root=ledger.RUNTIME_ROOT,
                    node_address=address,
                    owner_token=str(snapshot.get("owner_token") or ""),
                    ingress_event_id=event_id,
                    response=response,
                )
                if event_id == target_event_id:
                    target_response_written = True

    stale_rows: list[dict] = []
    malformed_rows: list[str] = list(parse_errors)
    latest_valid: dict | None = None
    for row in rows:
        if (
            row.get("schema_version") != turn_state.SCHEMA_VERSION
            or row.get("node_address") != address
            or not row.get("event_id")
        ):
            malformed_rows.append(
                f"event {row.get('event_id') or '<missing>'}: invalid schema/address/id"
            )
            continue
        if row.get("row_kind") == turn_state.RAW_HOOK_EVENT:
            if row.get("owner_token") != snapshot.get("owner_token"):
                stale_rows.append(row)
            continue
        if row.get("row_kind") == turn_state.HOOK_RESPONSE:
            if row.get("responds_to_event_id") == target_event_id:
                target_response_written = True
            continue
        if row.get("owner_token") != snapshot.get("owner_token"):
            stale_rows.append(row)
            continue
        if row.get("adopted") is not True:
            malformed_rows.append(
                f"event {row.get('event_id')}: current-token event was not adopted"
            )
            continue
        # Checklist acknowledgements are derived audit rows, not a second state edge. The
        # immediately preceding PostToolUse row (or the atomic current snapshot) carries the full
        # in-flight/open-item state; never let the sparse ack detail erase it in the binding.
        if row.get("hook_event") != "owed_checklist_ack":
            latest_valid = row

    for row in stale_rows:
        executor.journal(
            address,
            event="turn_state_stale_ignored",
            from_state=snapshot.get("state"),
            to_state=snapshot.get("state"),
            lease_epoch=snapshot.get("lease_epoch"),
            owner_token=snapshot.get("owner_token"),
            binding_delta={
                "event_id": row.get("event_id"),
                "presented_owner_token": row.get("owner_token"),
                "hook_event": row.get("hook_event"),
            },
            summary=(
                f"stale runtime-hook event ignored for {address}: "
                f"event_id={row.get('event_id')} hook_event={row.get('hook_event')}"
            ),
        )

    current = turn_state.read_current(address, snapshot, runtime_root=ledger.RUNTIME_ROOT)
    if current.status in {"missing", "malformed", "stale"}:
        malformed_rows.append(f"current state {current.status}: {current.reason or ''}".strip())

    prior_health = snapshot.get("turn_hook_health")
    health = "degraded" if malformed_rows else "healthy"
    delta: dict = {
        "turn_event_acked_offset": new_offset,
        "turn_hook_health": health,
        "turn_hook_error": " | ".join(malformed_rows[:5]) if malformed_rows else None,
    }
    observed = current.payload if current.status == "valid" else None
    if latest_valid is not None:
        detail = latest_valid.get("detail") if isinstance(latest_valid.get("detail"), dict) else {}
        delta.update(
            {
                "turn_state": latest_valid.get("state"),
                "turn_state_at": latest_valid.get("ts"),
                "turn_state_event_id": latest_valid.get("event_id"),
                "turn_state_hook_event": latest_valid.get("hook_event"),
                "turn_in_flight_tools": list(detail.get("in_flight_tools") or []),
                "turn_waiting_on_human_tool_id": detail.get(
                    "waiting_on_human_tool_id"
                ),
                "turn_waiting_on_human_tool_name": detail.get(
                    "waiting_on_human_tool_name"
                ),
                "turn_exit_decision": detail.get("exit_decision"),
                "turn_exit_delivery": detail.get("exit_delivery"),
                "turn_open_item_ids": list(detail.get("open_item_ids") or []),
            }
        )
    elif observed is not None:
        delta.update(
            {
                "turn_state": observed.get("state"),
                "turn_state_at": observed.get("updated_at"),
                "turn_state_event_id": observed.get("last_event_id"),
                "turn_state_hook_event": observed.get("last_hook_event"),
                "turn_in_flight_tools": list(observed.get("in_flight_tools") or []),
                "turn_waiting_on_human_tool_id": observed.get(
                    "waiting_on_human_tool_id"
                ),
                "turn_waiting_on_human_tool_name": observed.get(
                    "waiting_on_human_tool_name"
                ),
                "turn_exit_decision": observed.get("exit_decision"),
                "turn_exit_delivery": observed.get("exit_delivery"),
                "turn_open_item_ids": list(observed.get("open_item_ids") or []),
            }
        )

    if health == "degraded":
        event = "turn_hook_degraded"
        summary = (
            f"turn hook degraded for {address}: {delta['turn_hook_error']}; "
            "detector evidence fallback remains active"
        )
    elif prior_health == "degraded":
        event = "turn_hook_recovered"
        summary = f"turn hook recovered for {address}; valid owner-fenced state is primary again"
    else:
        event = "turn_state_observed"
        summary = f"owner-fenced runtime-hook turn state observed for {address}"
    result = executor.turn_state_checkpoint(
        address,
        expected_owner_token=snapshot.get("owner_token"),
        delta=delta,
        event=event,
        summary=summary,
    )
    live = getattr(result, "binding", None) or ledger.read_binding(address) or snapshot
    adoption_result = {
        "found": target_found or target_event_id is None,
        "response_event_id": (
            target_event_id if target_event_id and target_response_written else None
        ),
        "response_required": target_response_required,
    }

    # Codex legacy notify cannot block inline. Deliver the exact blocked-stop message through the
    # already prompt-gated/fenced tmux path, once per notify event, retrying transport misses.
    current = turn_state.read_current(address, live, runtime_root=ledger.RUNTIME_ROOT)
    payload = current.payload if current.status == "valid" else None
    if not payload:
        return adoption_result
    event_id = payload.get("last_event_id")
    if not (
        payload.get("hook_profile") == turn_state.CODEX_TURN_END_ONLY
        and payload.get("exit_decision") == turn_state.PROD_REQUIRED
        and payload.get("exit_delivery") == "daemon_prod"
        and event_id
        and payload.get("prod_dispatched_event_id") != event_id
    ):
        return adoption_result
    if not _watchdog_mod.prod_precondition(live):
        return adoption_result
    fenced = _send_fence_open(address, live)
    if fenced is None:
        return adoption_result
    message = str(payload.get("blocked_message") or "").strip()
    if not message:
        return adoption_result
    if not _deliver_keystroke(tmux, fenced, message, kind="turn_exit_prod"):
        return adoption_result
    turn_state.mark_prod_dispatched(
        address,
        fenced,
        runtime_root=ledger.RUNTIME_ROOT,
        event_id=event_id,
    )
    executor.turn_state_checkpoint(
        address,
        expected_owner_token=fenced.get("owner_token"),
        delta={"turn_exit_prod_event_id": event_id},
        event="turn_exit_prod_delivered",
        summary=(
            f"Codex turn-end exit prod delivered for {address}: event_id={event_id}; "
            "message replayed the live owed checklist and all admissible exits"
        ),
    )
    return adoption_result


def _dispatch_review_check_seats_best_effort(
    *,
    allow_uncalibrated: bool = False,
) -> None:
    """Open auxiliary review-check seats after a higher gate lead records FULL mode."""
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 — one sweep must not stop the daemon tick
        return
    for address, binding in list(bindings.items()):
        try:
            if not (binding or {}).get("gate_for"):
                continue
            if (binding or {}).get("state") != "running":
                continue
            _chokepoint.dispatch_review_check_seats(
                address,
                allow_uncalibrated=allow_uncalibrated,
            )
        except Exception:  # noqa: BLE001 — a bad review gate cannot abort the whole tick
            continue


# LR-15 — a pieces-refused (or otherwise post-claim-failed) child spawn used to WEDGE forever:
# the binding sat `planned` with the outbox request consumed (.done); _child_already_live counts
# planned as live (a parent re-request is swallowed), reconcile excludes pre-spawn states from
# owned-but-dead (INT-4), and the F21 claim-as-is leg is L1-only. This sweep leg re-drives them:
# any `planned` binding whose node is PREPARED (brief.md present — the E1 derivation source) and
# that has no live pane gets one claim_and_spawn per cooldown window. The CAS makes racing a
# concurrent legitimate claim harmless (one wins; the loser opens no actor); a still-broken node
# fails the same gate again and re-escalates — LOUD and bounded, never a silent wedge.
_REDRIVE_COOLDOWN_S: float = 60.0
_REDRIVE_LAST_ATTEMPT: dict = {}  # node_address -> time.monotonic() (daemon-incarnation memory)


def _redrive_planned_spawns_best_effort(executor, tmux=None) -> None:
    import time as _time

    from harnessd import config as _config
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 — the sweep must advance
        return
    # LR-19: the tmux_target STAMP proves nothing — register_child stamps the canonical
    # session-name placeholder on EVERY child at birth (F18), and a failed spawn's rollback
    # (release_claim) leaves the stamp behind. Skipping on the stamp alone made the sweep skip
    # every registered child forever (observed live: Run-2 markdown L3, tmux_session_collision
    # rollback -> planned + stamped -> 8h wedge). Tmux is the truth: only a session tmux REPORTS
    # alive disqualifies a node (mid-spawn shapes are already excluded by state != planned, and
    # the claim CAS makes racing a concurrent legitimate opener harmless). One list per sweep;
    # an unreachable tmux falls back to stamp-means-skip (conservative — no blind spawn storms).
    live_sessions = None
    if tmux is not None:
        try:
            live_sessions = {str(t).split(":", 1)[0] for t in (tmux.list_targets() or {})}
        except Exception:  # noqa: BLE001 — an unreachable tmux must not kill the sweep
            live_sessions = None
    for address, binding in list(bindings.items()):
        try:
            if (binding or {}).get("state") != "planned":
                continue
            # Semantic-cell reconciliation owns this cohort's dependency order and retry
            # lifecycle. Generic prepared-seat recovery must not bypass the comparator hold.
            if binding.get("semantic_cell_for"):
                continue
            gate_for = binding.get("gate_for")
            if gate_for:
                producer = ledger.read_binding(gate_for)
                if (producer or {}).get("gate_state") != "candidate_submitted":
                    continue
            review_check_for = binding.get("review_check_for")
            if review_check_for:
                review_lead = ledger.read_binding(review_check_for) or {}
                candidate = ledger.read_binding(binding.get("review_check_candidate")) or {}
                if (
                    review_lead.get("state") != "running"
                    or candidate.get("gate_state") != "candidate_submitted"
                    or str(candidate.get("gate_id") or "") != str(binding.get("gate_id") or "")
                ):
                    continue
            if binding.get("admission_state") in {"waiting_on_sibling", "blocked_on_sibling"}:
                if not _chokepoint.release_serial_l3_wait_if_ready(address):
                    continue
                binding = ledger.read_binding(address) or binding
                if binding.get("state") != "planned":
                    continue
            target = (binding.get("tmux_target") or "").strip()
            if target:
                session = target.split(":", 1)[0]
                if live_sessions is None or session in live_sessions:
                    if not gate_for or live_sessions is None:
                        continue  # a LIVE pane (or unverifiable tmux) — not a re-drive candidate
                    # A planned #review binding owns no live actor. If tmux still reports the
                    # deterministic session, it is a prior reviewer incarnation that must be
                    # cleared before this candidate's fresh reviewer can open. Re-read the
                    # review binding immediately before teardown: the sweep iterates a snapshot,
                    # and an operator/IPC spawn can claim the same #review address between the
                    # snapshot and this branch. Only a still-planned review slot is teardown-safe.
                    live_binding = ledger.read_binding(address)
                    if (
                        live_binding is None
                        or live_binding.get("state") != "planned"
                        or live_binding.get("gate_for") != gate_for
                    ):
                        continue
                    binding = live_binding
                    target = (binding.get("tmux_target") or "").strip()
                    if target:
                        session = target.split(":", 1)[0]
                        if session in live_sessions:
                            _chokepoint.kill_stale_pane(target)
                            live_sessions.discard(session)
            now = _time.monotonic()
            last = _REDRIVE_LAST_ATTEMPT.get(address)
            if last is not None and (now - last) < _REDRIVE_COOLDOWN_S:
                continue
            node_dir = addressing.node_dir(address, ledger.RUNTIME_ROOT)
            if not (node_dir / "brief.md").is_file():
                continue  # not a prepared node — nothing to drive (genesis/outbox owns it)
            level = (binding.get("level") or "").strip()
            try:
                level_config = _config.get_level_config(level)
            except Exception:  # noqa: BLE001 — an unknown level is not re-drivable
                continue
            _REDRIVE_LAST_ATTEMPT[address] = now
            result = _chokepoint.claim_and_spawn(
                address,
                expected_state="planned",
                expected_generation=binding.get("generation"),
                expected_owner_token=binding.get("owner_token"),
                level_config=level_config,
            )
            if (
                gate_for
                and result is not None
                and getattr(result, "ok", True) is False
                and getattr(result, "failure_class", None) not in {"claim_lost", "paused_subtree"}
            ):
                _chokepoint.fail_gate_open(
                    address,
                    failure_class=getattr(result, "failure_class", None) or "review_open_failed",
                    detail=f"redrive_planned_spawns failed opening {address}",
                )
            if (
                review_check_for
                and result is not None
                and getattr(result, "ok", True) is False
                and getattr(result, "failure_class", None) not in {"claim_lost", "paused_subtree"}
            ):
                _chokepoint.fail_review_check_open(
                    address,
                    failure_class=getattr(result, "failure_class", None) or "review_check_open_failed",
                    detail=f"redrive_planned_spawns failed opening {address}",
                )
        except Exception:  # noqa: BLE001 — one node's re-drive fault never aborts the sweep
            continue


def _recover_gate_notifications_best_effort() -> None:
    """Replay lost gate inbox pointers from durable binding state.

    The transition rows/binding state are authoritative; the original inbox append is best-effort.
    This sweep is lock-free and idempotent in the notification helpers, so an unchanged tick rewrites
    nothing and a missing pointer is repaired.
    """
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 — a recovery sweep must not stop the daemon tick
        return
    for address, binding in list(bindings.items()):
        try:
            if not (binding or {}).get("gate_required"):
                continue
            if (binding or {}).get("gate_state") not in {
                "candidate_submitted",
                "gate_bounced",
                "gate_failed",
                "gate_escalated",
                "gate_passed",
            }:
                continue
            _chokepoint.recover_gate_notification(address, binding)
        except Exception:  # noqa: BLE001 — one malformed binding never aborts recovery
            continue


def _daemon_inbox_has_line(inbox: Path, **criteria) -> bool:
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


def _marker_sha256(marker: Path) -> str | None:
    try:
        return hashlib.sha256(marker.read_bytes()).hexdigest()
    except OSError:
        return None


def _record_nonterminal_marker_error(
    address: str,
    binding: dict,
    marker: Path,
    *,
    marker_kind: str,
    result,
) -> None:
    """Journal and wake once for a malformed nonterminal handoff marker."""
    from harnessd import executor as _executor_mod

    try:
        live = ledger.read_binding(address) or binding or {}
        errors = list(getattr(result, "errors", None) or ["marker could not be processed"])
        marker_sha = _marker_sha256(marker)
        key_material = "\0".join([
            marker_kind,
            str(marker),
            marker_sha or "",
            "\n".join(str(error) for error in errors),
        ])
        marker_error_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:16]
        records = live.get("nonterminal_marker_errors") or {}
        if not isinstance(records, dict):
            records = {}
        record = records.get(marker_error_key)
        if not record:
            record = {
                "marker_error_key": marker_error_key,
                "marker_kind": marker_kind,
                "marker_artifact": str(marker),
                "marker_sha256": marker_sha,
                "errors": errors,
                "observed_at": _clock.now_utc(),
            }
            updated = dict(records)
            updated[marker_error_key] = record
            transition = _executor_mod.record_admission(
                address,
                expected_owner_token=live.get("owner_token"),
                delta={
                    "nonterminal_marker_errors": updated,
                    "nonterminal_marker_error_last_key": marker_error_key,
                },
                event=f"{marker_kind}_marker_invalid",
                summary=f"{address} has invalid {marker_kind} marker {marker}",
            )
            if not getattr(transition, "ok", False):
                return
        inbox = addressing.inbox_path(address, ledger.RUNTIME_ROOT)
        if _daemon_inbox_has_line(
            inbox,
            type="nonterminal_marker_invalid",
            marker_error_key=marker_error_key,
        ):
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "from": "harnessd",
            "type": "nonterminal_marker_invalid",
            "marker_error_key": marker_error_key,
            "marker_kind": marker_kind,
            "marker_artifact": str(marker),
            "marker_sha256": marker_sha,
            "errors": errors,
            "message": "A nonterminal handoff marker could not be processed; repair the marker or referenced artifact.",
            "ts": _clock.now_utc(),
        }
        with inbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001 - marker-error observability must not abort the sweep
        return


def _submit_coordination_handoff_markers_best_effort() -> None:
    """Detect normal nonterminal coordination handoff markers and wake the direct parent."""
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 - a marker sweep must not stop the daemon tick
        return
    for address, binding in list(bindings.items()):
        try:
            if (binding or {}).get("state") != "running":
                continue
            if not str(address).endswith("#exec"):
                continue
            if not (binding or {}).get("parent_address"):
                continue
            marker_dir = (
                addressing.node_dir(address, ledger.RUNTIME_ROOT)
                / _chokepoint.COORDINATION_HANDOFF_DIRNAME
            )
            if not marker_dir.is_dir():
                continue
            for marker in sorted(marker_dir.glob("*.json")):
                result = _chokepoint.submit_coordination_handoff(
                    address,
                    marker_path=marker,
                    expected_owner_token=(binding or {}).get("owner_token"),
                )
                if result is not None and not getattr(result, "ok", False):
                    _record_nonterminal_marker_error(
                        address,
                        binding or {},
                        marker,
                        marker_kind="coordination_handoff",
                        result=result,
                    )
        except Exception:  # noqa: BLE001 - one malformed marker never aborts recovery
            continue


def _recover_coordination_handoff_notifications_best_effort() -> None:
    """Replay lost coordination-handoff pointers from durable binding state."""
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 — a recovery sweep must not stop the daemon tick
        return
    for address, binding in list(bindings.items()):
        try:
            if not (binding or {}).get("coordination_handoffs"):
                continue
            _chokepoint.recover_coordination_handoff_notifications(address, binding)
        except Exception:  # noqa: BLE001 — one malformed binding never aborts recovery
            continue


def _submit_plan_alignment_markers_best_effort(
    *,
    allow_uncalibrated: bool = False,
) -> None:
    """Detect L2 plan-alignment ready markers and journal the L2 -> L1 handoff.

    The marker is authored by the L2 agent inside its own node workspace. The daemon turns that
    durable local artifact into the control-plane readiness state plus the parent inbox wake.
    """
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 - a marker sweep must not stop the daemon tick
        return
    for address, binding in list(bindings.items()):
        try:
            if (binding or {}).get("state") != "running":
                continue
            if ((binding or {}).get("level") or "").split("#", 1)[0] != "L2":
                continue
            marker = (
                addressing.node_dir(address, ledger.RUNTIME_ROOT)
                / _chokepoint.PLAN_ALIGNMENT_READY_FILENAME
            )
            if not marker.is_file():
                continue
            result = _chokepoint.submit_plan_alignment_ready(
                address,
                marker_path=marker,
                expected_owner_token=(binding or {}).get("owner_token"),
                allow_uncalibrated=allow_uncalibrated,
            )
            if result is not None and not getattr(result, "ok", False):
                _record_nonterminal_marker_error(
                    address,
                    binding or {},
                    marker,
                    marker_kind="plan_alignment",
                    result=result,
                )
        except Exception:  # noqa: BLE001 - one malformed marker never aborts recovery
            continue


def _reconcile_plan_alignment_cells_best_effort(
    *,
    allow_uncalibrated: bool = False,
) -> None:
    """Drive pending semantic cohorts; only a complete evidence index wakes L1."""
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 — a cell sweep must not stop the daemon tick
        return
    for address, binding in list(bindings.items()):
        try:
            if (binding or {}).get("state") != "running":
                continue
            if ((binding or {}).get("level") or "").split("#", 1)[0] != "L2":
                continue
            if (
                (binding or {}).get("plan_alignment_state")
                != _chokepoint.PLAN_ALIGNMENT_STATE_SEMANTIC_PENDING
            ):
                continue
            _chokepoint.reconcile_plan_alignment_cell(
                address,
                allow_uncalibrated=allow_uncalibrated,
            )
        except Exception:  # noqa: BLE001 — one cell cannot stop another build edge
            continue


def _reconcile_plan_alignment_elevations_best_effort() -> None:
    """Adopt current L1 elevation markers without creating a second transport."""
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 — one read failure must not stop the daemon tick
        return
    for address, binding in list(bindings.items()):
        try:
            if ((binding or {}).get("level") or "").split("#", 1)[0] != "L2":
                continue
            if (
                (binding or {}).get("plan_alignment_state")
                != _chokepoint.PLAN_ALIGNMENT_STATE_READY
            ):
                continue
            _chokepoint.reconcile_plan_alignment_elevations(address)
        except Exception:  # noqa: BLE001 — malformed owner input is isolated per project
            continue


def _recover_plan_alignment_notifications_best_effort() -> None:
    """Replay lost L2 -> L1 plan-alignment readiness pointers from binding state."""
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 — a recovery sweep must not stop the daemon tick
        return
    for address, binding in list(bindings.items()):
        try:
            if (binding or {}).get("plan_alignment_state") != _chokepoint.PLAN_ALIGNMENT_STATE_READY:
                continue
            _chokepoint.recover_plan_alignment_notification(address, binding)
        except Exception:  # noqa: BLE001 — one malformed binding never aborts recovery
            continue


def _recover_terminal_notifications_best_effort() -> None:
    """Replay lost non-gate parent relay pointers from durable terminal binding state."""
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 — a recovery sweep must not stop the daemon tick
        return
    for address, binding in list(bindings.items()):
        try:
            if not (binding or {}).get("terminal_signal"):
                continue
            _chokepoint.recover_terminal_notification(address, binding)
        except Exception:  # noqa: BLE001 — one malformed binding never aborts recovery
            continue


def _route_reconcile_escalations(report) -> None:
    """Journal each ReconcileReport escalation ONCE (edge-triggered on node+kind) — RR-4.

    Best-effort and isolated (the outbox-drain discipline): a journaling fault must never abort
    the sweep. The row is the v1 escalation seat's durable surface — an L1 reconcile reader (and
    the operator's WAL tail) sees orphan / necro_failed / coordinator_died with their evidence.
    """
    for escalation in (getattr(report, "escalations", None) or []):
        node = escalation.get("node_address")
        kind = escalation.get("kind") or "unknown"
        try:
            _journal_escalation_once(
                node,
                kind=kind,
                summary=(
                    f"reconcile escalation ({kind}) for {node}: "
                    f"{escalation.get('reason', 'no reason recorded')} (RR-4 — the v1 "
                    "detect+escalate seat, edge-triggered)"
                ),
                detail=dict(escalation),
            )
        except Exception:  # noqa: BLE001 — escalation routing must never abort the sweep
            pass
        try:
            if kind == "coordinator_died":
                _route_dead_gate_review_lead(node, escalation)
        except Exception:  # noqa: BLE001 — gate-failure routing is best-effort and retried next tick
            continue


def _route_dead_gate_review_lead(review_address: str, escalation: dict) -> None:
    """Turn a dead gate review lead into a producer-visible gate failure.

    Generic coordinator death remains a reconcile escalation. A `#review` coordinator is also
    review substrate for a held candidate; if it dies while the producer is still
    `candidate_submitted`, leaving only the reconcile row strands the producer. Reuse the existing
    gate_failed recovery surface so the parent can retry or repair the gate.
    """
    if not review_address:
        return
    review = ledger.read_binding(review_address)
    if not review or not review.get("gate_for"):
        return
    producer_address = review.get("gate_for")
    producer = ledger.read_binding(producer_address)
    if (
        not producer
        or producer.get("state") != "running"
        or producer.get("gate_state") != "candidate_submitted"
        or producer.get("gate_review_address") != review_address
    ):
        return
    chokepoint.fail_gate_open(
        review_address,
        failure_class="review_lead_died",
        detail=(
            "gate review lead died before producing a verdict; "
            f"reconcile detail: {dict(escalation)}"
        ),
    )


def _journal_escalation_once(node_address, *, kind: str, summary: str, detail: dict) -> None:
    """Append ONE ``reconcile_escalation`` row for (node, kind) — re-detection journals nothing.

    The dedup scans the WAL (the same pattern as watchdog._has_coordinator_died_event): a
    steady-state condition (an orphan pane nobody resolves; a persistently aborting necro) is
    re-detected every tick but journaled once. The journal rides ``executor.journal`` (SWL-01).
    NOTE: the row is keyed to the escalation's node identity — for an ORPHAN that identity is its
    tmux_target (no binding exists); journal rows are non-transition rows (expected_generation
    None), which boot replay deliberately never reconstructs a binding from.
    """
    for record in ledger.load_wal():
        if (
            record.get("event") == "reconcile_escalation"
            and record.get("node_address") == node_address
            and (record.get("binding_delta") or {}).get("kind") == kind
        ):
            return  # already journaled — the edge already fired
    try:
        from harnessd import executor as _executor_mod

        _executor_mod.journal(
            node_address,
            event="reconcile_escalation",
            binding_delta={"kind": kind, **{k: v for k, v in detail.items() if k != "node_address"}},
            summary=summary,
        )
    except Exception:  # noqa: BLE001 — best-effort visibility row
        pass


def _service_outboxes_best_effort() -> None:
    """Drain all spawn-request outboxes; swallow any error so it never aborts the reconcile sweep."""
    try:
        _outbox_mod.service_all_outboxes()
    except Exception:  # noqa: BLE001 — the reconcile sweep must advance regardless of one bad outbox
        pass


# ---------------------------------------------------------------------------
# _watchdog_tick — the §2.9 watchdog as a verdict+policy invoked by ①'s in-process sweep (WATCHDOG.md
# L214: "② is a verdict + policy function invoked by ①'s in-process sweep, NOT a separate polling
# daemon"). THE keystone wiring the review found missing: without this, no node ever auto-collapses on
# sign-off, no idle leaf ever fails-loud, no coordinator-death is probed.
# ---------------------------------------------------------------------------

def _has_live_descendant(node_address: str, bindings: dict) -> bool:
    """True iff any descendant binding is NON-terminal (the bottom-up shutdown gate for coordinator
    terminal-signal processing). Mirrors _is_coordinator's two-way descendant match (parent_address
    edge, address-prefix fallback) so the two predicates cannot disagree about what a descendant is."""
    this_path = node_address.split("#", 1)[0]
    for other_address, other in bindings.items():
        if other_address == node_address:
            continue
        is_descendant = other.get("parent_address") == node_address or other_address.split("#", 1)[
            0
        ].startswith(this_path + "/")
        if is_descendant and not states.is_terminal(other.get("state")):
            return True
    return False


def _is_coordinator(node_address: str, bindings: dict) -> bool:
    """A COORDINATOR has at least one (live-or-any) descendant binding (§5.4). Mirrors reconcile's split:
    primary = another binding names this node as ``parent_address``; fallback = address-prefix arithmetic
    on the one-spine path (a descendant's path begins with ``<this-path>/``)."""
    this_path = node_address.split("#", 1)[0]
    for other_address, other in bindings.items():
        if other_address == node_address:
            continue
        if other.get("parent_address") == node_address:
            return True
        other_path = other_address.split("#", 1)[0]
        if other_path.startswith(this_path + "/"):
            return True
    return False


def _stall_incident_id(address: str, binding: dict, probe: dict, count: int) -> str:
    identity = "|".join(
        (
            address,
            str(binding.get("owner_token") or ""),
            str(probe.get("last_life_evidence_at") or ""),
            str(probe.get("classification") or ""),
            str(count),
        )
    )
    return "stall-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _blocked_input_notice(binding: dict) -> str:
    return (
        "[HARNESS SYSTEM NOTICE]\n\n"
        f"At {binding.get('seat_stall_since')}, this turn was blocked at an interactive "
        f"prompt ({binding.get('seat_stall_prompt_signature')}). The harness sent Escape "
        "because it cannot answer or approve on your behalf. No option was selected. "
        "The harness-provisioned scratch path `.tmp` exists in this seat workspace.\n\n"
        f"Pane evidence: {binding.get('seat_stall_pane_excerpt') or '(unavailable)'}\n"
    )


def _blocked_input_parent_notice(binding: dict) -> str:
    return (
        "[HARNESS SYSTEM NOTICE]\n\n"
        f"At {binding.get('seat_stall_since')}, seat {binding.get('node_address')} reached "
        "three blocked interactive prompts in its current incarnation. The third prompt was "
        "not cancelled, and no option was selected. The active incident remains visible for "
        "parent judgment.\n\n"
        f"Pane evidence: {binding.get('seat_stall_pane_excerpt') or '(unavailable)'}\n"
    )


def _stall_message_id(incident_id: str, recipient_kind: str) -> str:
    digest = hashlib.sha256(
        f"{incident_id}|{recipient_kind}".encode("utf-8")
    ).hexdigest()
    return f"harness-stall-{digest[:24]}"


def _deliver_stall_notice(binding: dict, *, to_parent: bool = False) -> None:
    if ledger.RUNTIME_ROOT is None:
        raise RuntimeError("blocked-input notice requires a bound runtime root")
    target = binding.get("parent_address") if to_parent else binding.get("node_address")
    if not target:
        return
    incident_id = str(binding.get("seat_stall_incident_id") or "")
    _messages_mod.author_system_message(
        target=str(target),
        message_id=_stall_message_id(
            incident_id,
            "parent" if to_parent else "seat",
        ),
        content=(
            _blocked_input_parent_notice(binding)
            if to_parent
            else _blocked_input_notice(binding)
        ),
        summary=(
            f"Harness observed three blocked prompts on {binding.get('node_address')}"
            if to_parent
            else "Harness cancelled an interactive prompt without selecting an option"
        ),
        tags=["blocked-on-input", "harness-system"],
        metadata={
            "incident_id": incident_id,
            "classification": binding.get("seat_stall_classification"),
            "cancel_status": binding.get("seat_stall_cancel_status"),
            "retriggered": bool(binding.get("seat_stall_retriggered")),
            "escalated": bool(binding.get("seat_stall_escalated")),
        },
        runtime_root=ledger.RUNTIME_ROOT,
    )


def _act_on_blocked_input(
    executor,
    tmux,
    address: str,
    snapshot: dict,
    probe: dict,
    *,
    recheck=None,
) -> bool:
    """Persist and resolve one blocked-input edge. True means ordinary prod/wake policy stops."""
    classification = str(probe.get("classification") or "unknown")
    active = bool(snapshot.get("seat_stall_active"))

    if active:
        if snapshot.get("seat_stall_escalated") and snapshot.get("parent_address"):
            _deliver_stall_notice(snapshot, to_parent=True)
        elif snapshot.get("seat_stall_cancel_status") == "sent":
            _deliver_stall_notice(snapshot)
        if classification == "healthy":
            result = executor.recover_seat_stall(
                address,
                expected_owner_token=snapshot.get("owner_token"),
                incident_id=str(snapshot.get("seat_stall_incident_id") or ""),
                recovered_at=_clock.now_utc(),
                reason=str(probe.get("reason") or "life_evidence_resumed"),
            )
            return not bool(getattr(result, "ok", False))
        if classification == "blocked_on_input":
            # The write-ahead pending result is terminal-unknown across restart: never infer
            # delivery and never send a second Escape. A sent incident may retry only its
            # deterministic canonical notice.
            return True
        if classification in {"silent_in_flight_unconfirmed", "unknown"}:
            return True
        return True

    if classification not in {
        "blocked_on_input",
        "silent_in_flight_unconfirmed",
    }:
        return False

    prior_positive = int(snapshot.get("seat_stall_positive_incident_count") or 0)
    positive = classification == "blocked_on_input"
    positive_count = prior_positive + int(positive)
    retriggered = positive and prior_positive > 0
    escalated = positive and positive_count > 2
    root_limit = escalated and not snapshot.get("parent_address")
    cancel_status = (
        "pending" if positive and not escalated else "not_attempted"
    )
    incident_id = _stall_incident_id(
        address,
        snapshot,
        probe,
        positive_count,
    )
    intent = executor.record_seat_stall(
        address,
        expected_owner_token=snapshot.get("owner_token"),
        incident_id=incident_id,
        detected_at=str(probe.get("detected_at") or _clock.now_utc()),
        classification=classification,
        silent_seconds=float(probe.get("silent_seconds") or 0.0),
        pane_excerpt=str(probe.get("pane_excerpt") or ""),
        prompt_signature=probe.get("prompt_signature"),
        cancel_status=cancel_status,
        positive_incident_count=positive_count,
        retriggered=retriggered,
        escalated=escalated,
        root_limit=root_limit,
    )
    if not getattr(intent, "ok", False):
        raise RuntimeError("; ".join(getattr(intent, "errors", None) or ["seat stall intent failed"]))
    live = intent.binding or ledger.read_binding(address) or snapshot

    if not positive:
        return True
    if escalated:
        if live.get("parent_address"):
            _deliver_stall_notice(live, to_parent=True)
        return True

    fenced = _send_fence_open(address, snapshot)
    if fenced is None:
        action = executor.record_seat_stall_action(
            address,
            expected_owner_token=snapshot.get("owner_token"),
            incident_id=incident_id,
            actioned_at=_clock.now_utc(),
            cancel_status="failed",
        )
        if not getattr(action, "ok", False):
            raise RuntimeError("; ".join(action.errors))
        return True
    fresh_probe = recheck(fenced) if callable(recheck) else _watchdog_mod.probe_blocked_on_input(
        fenced,
        fenced,
        now=_clock.now_utc(),
    )
    if (
        fresh_probe.get("classification") != "blocked_on_input"
        or fresh_probe.get("prompt_signature") != probe.get("prompt_signature")
    ):
        action = executor.record_seat_stall_action(
            address,
            expected_owner_token=snapshot.get("owner_token"),
            incident_id=incident_id,
            actioned_at=_clock.now_utc(),
            cancel_status="failed",
        )
        if not getattr(action, "ok", False):
            raise RuntimeError("; ".join(action.errors))
        return True
    send_cancel = getattr(tmux, "send_cancel", None)
    sent = bool(send_cancel and send_cancel(fenced.get("tmux_target")))
    action = executor.record_seat_stall_action(
        address,
        expected_owner_token=snapshot.get("owner_token"),
        incident_id=incident_id,
        actioned_at=_clock.now_utc(),
        cancel_status="sent" if sent else "failed",
    )
    if not getattr(action, "ok", False):
        raise RuntimeError("; ".join(action.errors))
    if sent:
        _deliver_stall_notice(action.binding or fenced)
    return True


def _watchdog_tick(executor, tmux, detector) -> None:
    """Run the §2.9 watchdog verdict+policy over every LIVE node, edge-triggered, leaf/coordinator-split.

    LEAF (ephemeral L5/L5+): ``watchdog.check_leaf`` — terminal-signal-FIRST collapse (a fenced DONE/
    FAILED ``.signal.json`` routes through ``chokepoint.collapse`` -> the REAL executor) then the
    idle->prod->FAILED ladder. ``check_leaf`` ENACTS its ledger-side action and returns a
    WatchdogAction; THIS sweep enacts the action's KEYSTROKE half — a PROD's
    ``detail['keystroke']`` is actually delivered via ``tmux.send_keys(binding tmux_target)``
    (the transport increment: an un-enacted PROD nudges nobody).
    COORDINATOR (persistent): ``watchdog.check_coordinator_death`` — the dead-pid+live-children ->
    ESCALATE probe (never blind-collapsed; the full evidence-lease recovery machine is DEFERRED, per
    WATCHDOG.md §1). A coordinator is NOT run through the leaf sign-off/ladder (the §5.4 split: do not
    auto-fail a coordinator for idle — it may be waiting on children).

    THE ③-WAKE (previously unwired — F16 noted the gap): EVERY live node (leaf AND coordinator —
    the answer verb wakes the PARENT's inbox) with a line appended past its acked watermark
    (``watchdog.inbox_has_unacked``) gets ONE pointer nudge (``watchdog.wake_keystroke`` — never
    the payload), gated by ``prod_precondition`` (never type into a mid-turn pane) and skipped
    for a PAUSED subtree (§3.4 STEP 0: no recovery actions). A DELIVERED nudge does NOT advance
    the edge-trigger watermark (R-1: send-keys rc=0 is not consumption) — it records a pending
    verification (``executor.record_wake_pending``); a LATER tick acks (``executor.ack_inbox``)
    only after verify-new-turn confirms an agent-progress transcript row after the send baseline.
    Provider/API/runtime-error rows do not count as consumption. A suppressed/failed/unverified
    send leaves the watermark unmoved so the next tick retries — deferred, never lost.

    Liveness is injected into the watchdog via its ``set_liveness`` seam so ``check_leaf`` reads THIS
    sweep's detector verdict. Each node is isolated: one node's watchdog error never aborts the sweep
    (the supervision tree must keep advancing — the same best-effort discipline as the outbox drain).
    A failed keystroke SEND is never swallowed silently: it is journaled to the run-ledger
    (``prod_send_failed`` / ``wake_send_failed``) per the result-routing convention.
    """
    if detector is not None:
        _watchdog_mod.set_liveness(lambda addr: detector.liveness(addr))
    try:
        now = _clock.now_utc()
        bindings = ledger.all_nodes()
        for address, binding in list(bindings.items()):
            if states.is_terminal(binding.get("state")):
                continue
            if binding.get("state") in {"planned", "claimed", "spawning"}:
                # These are not actor-bearing watchdog subjects yet. Planned seats, including
                # gate #review slots, deliberately have no transcript_path until the spawn/redrive
                # path opens them; applying the detector here turns valid future seats into
                # MissingTranscriptPath faults. Reconcile/spawn-redrive own these states.
                continue
            # The watchdog's ``node`` arg is the node OBJECT (a dict carrying node_address / tmux_target /
            # transcript_path — read by read_terminal_signal + pane_alive), NOT the bare address string.
            # The binding dict carries those fields, so it serves as both ``node`` and ``binding``.
            try:
                coordinator = _is_coordinator(address, bindings)
                # Terminal truth wins for EVERY seat before blocked-input classification. A
                # fenced sign-off is an actual completion fact; no dialog heuristic may cancel
                # after that fact has arrived.
                sig_action = _watchdog_mod.check_terminal_signal(
                    binding,
                    binding,
                    allow_collapse=(
                        not _has_live_descendant(address, bindings)
                        if coordinator
                        else True
                    ),
                )
                if sig_action is not None:
                    _wake_on_unacked_inbox(executor, tmux, address, binding)
                    continue

                stall_probe = _watchdog_mod.probe_blocked_on_input(
                    binding,
                    binding,
                    now=now,
                )
                if _act_on_blocked_input(
                    executor,
                    tmux,
                    address,
                    binding,
                    stall_probe,
                    recheck=lambda live: _watchdog_mod.probe_blocked_on_input(
                        live,
                        live,
                        now=_clock.now_utc(),
                    ),
                ):
                    # A current incident owns the pane. No ordinary prod or inbox wake may type
                    # into a chooser; recovery re-arms those existing paths on a later/healthy read.
                    continue

                if coordinator:
                    # ROUTE the verdict (RR-4): check_coordinator_death is PURE — its ESCALATE
                    # (the only carrier of target=parent_address + reason=recoverable_orphan)
                    # used to evaporate right here every tick. One edge-triggered row per
                    # (node, reason); WATCHDOG §5.5's v1 escalation seat is now actually wired.
                    cd_action = _watchdog_mod.check_coordinator_death(binding, binding, ledger)
                    if getattr(cd_action, "kind", None) == _watchdog_mod.ESCALATE:
                        detail = dict(getattr(cd_action, "detail", None) or {})
                        kind = detail.get("reason") or "coordinator_escalate"
                        detail["target"] = getattr(cd_action, "target", None)
                        _journal_escalation_once(
                            address, kind=kind,
                            summary=(
                                f"watchdog escalation ({kind}) for {address}: dead coordinator "
                                f"over live children -> escalate to "
                                f"{detail.get('target')!r} (WATCHDOG §5.1/§5.5; RR-4)"
                            ),
                            detail=detail,
                        )
                else:
                    action = _watchdog_mod.check_leaf(binding, binding, now=now)
                    # ENACT the keystroke half of a PROD (the ledger half — the stale-counter
                    # advance — check_leaf already routed through the executor). Fenced (LT-3):
                    # the live binding is re-read immediately before the send.
                    if getattr(action, "kind", None) == _watchdog_mod.PROD:
                        keystroke = (getattr(action, "detail", None) or {}).get("keystroke")
                        if keystroke:
                            live = _send_fence_open(address, binding)
                            if live is not None:
                                _deliver_keystroke(tmux, live, keystroke, kind="prod")
                # The ③-wake runs for EVERY live node (the answer verb wakes the PARENT inbox).
                _wake_on_unacked_inbox(executor, tmux, address, binding)
            except Exception as exc:  # noqa: BLE001 — one node's fault must not abort the whole sweep
                # RR-6: fault isolation per node is correct, but ZERO-journaling was the defect —
                # the deliberate fail-loud raises (e.g. MissingTranscriptPath) terminated in a
                # silent `continue`, permanently disabling STEP-A collapse, the idle ladder AND
                # the ③-wake for that node with no trace. Journal it (edge-triggered per node on
                # the error text, so a persistently-broken node is one row, not one per tick).
                _journal_sweep_error(binding, address, exc)
                continue
    finally:
        if detector is not None:
            _watchdog_mod.set_liveness(None)  # restore the default detector.liveness seam


# RR-6 edge-trigger memory: node_address -> the last journaled sweep-error text. A node whose
# watchdog evaluation throws the SAME error every tick is journaled ONCE per daemon incarnation
# (a changed error re-journals); in-memory on purpose — the row is a visibility aid, not control
# state, and a relaunch re-detecting the fault SHOULD re-journal it once.
_SWEEP_ERRORS_JOURNALED: dict = {}


def _journal_sweep_error(binding, address: str, exc: BaseException) -> None:
    """Best-effort, edge-triggered ``watchdog_sweep_error`` run-ledger row (RR-6)."""
    error = f"{type(exc).__name__}: {exc}"
    if _SWEEP_ERRORS_JOURNALED.get(address) == error:
        return  # steady-state re-detection of the same fault — already journaled
    try:
        from harnessd import executor as _executor_mod

        _executor_mod.journal(
            address,
            event="watchdog_sweep_error",
            from_state=binding.get("state"),
            to_state=binding.get("state"),
            lease_epoch=binding.get("lease_epoch"),
            binding_delta={"error": error},
            summary=(
                f"watchdog sweep error for {address}: {error} — node isolated this tick "
                "(STEP-A/ladder/③-wake skipped); fault journaled, sweep continues (RR-6)"
            ),
        )
        _SWEEP_ERRORS_JOURNALED[address] = error
    except Exception:  # noqa: BLE001 — the journal itself is best-effort
        pass


def _deliver_keystroke(tmux, binding, keystroke: str, *, kind: str) -> bool:
    """Deliver a watchdog keystroke into the binding's pane; journal a failed send (never silent).

    Best-effort transport: ``tmux.send_keys(tmux_target, keystroke)`` — the canonical F18 target.
    A transport without ``send_keys`` (the older test fakes) is a no-op miss. A send that RAISES
    or that RETURNS False (LT-2: the real ``tmux.send_keys`` now surfaces a non-zero rc — a
    dead/missing target between gate-capture and send) is journaled as a ``<kind>_send_failed``
    run-ledger row (the result-routing convention: a lost nudge must be visible, even though the
    ③-wake/next-tick retry is the actual healer). Returns True iff the send reported delivery;
    a legacy fake returning None is treated as delivered (it cannot know better).
    """
    target = binding.get("tmux_target")
    send = getattr(tmux, "send_keys", None)
    if not target or send is None:
        return False
    try:
        result = send(target, keystroke)
    except Exception as exc:  # noqa: BLE001 — journal, never crash the sweep
        _journal_send_failed(binding, target, kind, str(exc))
        return False
    if result is False:
        # The transport surfaced a delivery failure (non-zero tmux rc — LT-2). Journal it and
        # report undelivered so the caller leaves the wake watermark unmoved (next tick retries).
        _journal_send_failed(binding, target, kind, "send-keys exited non-zero (target gone?)")
        return False
    return True


def _journal_send_failed(binding, target, kind: str, error: str) -> None:
    """Best-effort ``<kind>_send_failed`` run-ledger row — a lost nudge is visible, never silent.

    Routed through ``executor.journal`` (SWL-01): the seq allocation + append run under the
    per-mutation EX lock, never racing the locked single writer on the other thread.
    """
    try:
        from harnessd import executor as _executor_mod

        _executor_mod.journal(
            binding.get("node_address"),
            event=f"{kind}_send_failed",
            from_state=binding.get("state"),
            to_state=binding.get("state"),
            lease_epoch=binding.get("lease_epoch"),
            binding_delta={"tmux_target": target, "error": error},
            summary=f"{kind} keystroke send to {target} failed: {error} (retried next tick)",
        )
    except Exception:  # noqa: BLE001 — the journal itself is best-effort
        pass


def _send_fence_open(address: str, snapshot: dict) -> Optional[dict]:
    """The SENDER-SIDE FENCE (TRANSPORTS §3.2 precondition 2, LT-3): re-read the LIVE binding
    immediately before a keystroke send; return it iff the send is still safe, else None (abort).

    The watchdog loop computes its actions off a pre-tick ``snapshot``; the SAME tick can collapse
    a signed-off leaf (STEP A) and then reach the ③-wake — without this re-read the daemon types
    'resume' into the pane of a node the ledger just recorded terminal, and acks the inbox on a
    terminal binding. Aborts when:
      * the binding is gone, or its lifecycle state is terminal (the collapsing/terminal set);
      * ``owner_token`` / ``lease_epoch`` drifted from the snapshot (a re-claim fenced it);
      * ``session_uuid`` drifted (a respawned incarnation — the nudge was computed for a pane
        that no longer exists).
    A None abort leaves the wake watermark unmoved — the next tick recomputes from durable truth.
    """
    live = ledger.read_binding(address)
    if live is None:
        return None
    if states.is_terminal(live.get("state")):
        return None
    if live.get("owner_token") != snapshot.get("owner_token"):
        return None
    if live.get("lease_epoch") != snapshot.get("lease_epoch"):
        return None
    if live.get("session_uuid") != snapshot.get("session_uuid"):
        return None
    return live


def _wake_on_unacked_inbox(executor, tmux, address: str, binding: dict) -> None:
    """The ③-wake delivery: unacked inbox line -> ONE gated+fenced pointer nudge -> VERIFIED ack.

    EDGE-TRIGGERED: one coherent ``unconsumed_inbox_notification`` scan reads
    complete rows after ``last_inbox_acked_offset``, derives sender/count
    metadata, and supplies the exact covered offset for eventual ack. PAUSED
    subtrees get no nudge (§3.4 STEP 0 — no recovery actions); the pointer
    names the inbox re-read, NEVER the message payload.

    Three transport-correctness rules (the pre-live-run fixes):
      * R-1 / VERIFY-NEW-TURN (TRANSPORTS §3.2 P3) — a DELIVERED send does NOT ack: send-keys
        rc=0 only proves tmux accepted the bytes, not that the agent consumed them (an operator's
        interactive attach in copy-mode EATS the keystrokes while capture-pane still shows the
        idle prompt and send-keys still exits 0 — for a coordinator, which has no prod-ladder
        healer, a swallowed human-answer wake wedged the run silently and permanently). The send
        records a pending verification instead (``wake_pending_ack_offset`` = the pre-send inbox
        size, ``wake_sent_transcript_size`` = the transcript byte size at send time); a LATER
        tick resolves it FIRST — an agent-progress transcript row landed after the baseline
        (``confirm_prod_worked``; absent/unreadable/error-only reads NOT-confirmed, conservative)
        -> ``ack_inbox`` to the recorded offset
        (which clears the pending fields in the same write — never a double-ack); not grown ->
        watermark unmoved, and the still-unacked line permits a gated RE-SEND that OVERWRITES the
        pending record (one pending verification at a time per node; one send per tick, no spin).
        The third delivered wake emits one typed ``wake_cap`` row; no fourth
        wake is sent while the receipt remains unconsumed. Pending verification
        stays armed and only a verified ack resets the durable attempt count.
      * SWL-03 — the coherent inbox scan's last complete-row offset is the
        eventual ack target: the inbox is a multi-writer append log, so a line
        landing after notification composition stays ABOVE the watermark and
        re-triggers after the covered receipt is consumed.
      * LT-3 — the sender-side fence (``_send_fence_open``, TRANSPORTS §3.2 P2) re-reads the live
        binding immediately before the send (and before the verified ack) and aborts on terminal
        state / token / epoch / session_uuid drift — the same tick that collapses a signed-off
        leaf must NOT then nudge its pane 'resume' nor ack the inbox on the terminal binding.
    """
    from harnessd.spawn import chokepoint as _chokepoint

    try:
        notification = _watchdog_mod.unconsumed_inbox_notification(binding, binding)
    except Exception:  # noqa: BLE001 — an unreadable inbox is "nothing to wake", not a crash
        return
    if not notification or not notification.get("should_wake"):
        return
    if _chokepoint.subtree_paused(address):
        return  # a paused subtree gets NO recovery nudge (WATCHDOG §3.4 STEP 0)

    # R-1: resolve a PRIOR delivered-but-unverified nudge BEFORE considering a new one.
    pending_offset = binding.get("wake_pending_ack_offset")
    if pending_offset is not None:
        baseline = binding.get("wake_sent_transcript_size") or 0
        if _watchdog_mod.confirm_prod_worked(binding, baseline):
            # Verified: a NEW turn landed in the transcript — the agent actually resumed.
            # Fence the ack too (LT-3): no inbox-ack WAL write may land on a terminal/
            # re-claimed/respawned binding.
            if _send_fence_open(address, binding) is None:
                return
            executor.ack_inbox(address, acked_offset=int(pending_offset))
            binding = dict(binding)
            binding["last_inbox_acked_offset"] = int(pending_offset)
            binding["wake_pending_ack_offset"] = None
            binding["wake_sent_transcript_size"] = None
            binding["wake_attempt_count"] = 0
            binding["wake_cap_emitted_at"] = None
            try:
                notification = _watchdog_mod.unconsumed_inbox_notification(binding, binding)
                if not notification or not notification.get("should_wake"):
                    return  # everything covered by the verified nudge — no new nudge owed
            except Exception:  # noqa: BLE001
                return
        # Not grown -> conservative: watermark unmoved; fall through to the gated re-send
        # (which OVERWRITES the pending record with ITS pre-send sizes).

    if wake_hold_engaged():
        # The control-socket self-probe is failing: issue NO new wake into a world where oversight
        # is blind (the r5 escape hatch). Deliberately placed AFTER the pending-verification
        # resolution — resolving a wake already delivered is not issuing one — and it mutates
        # nothing: the watermark stays put and the next healthy sweep sends.
        return
    if int(binding.get("wake_attempt_count") or 0) >= 3:
        return  # notification budget exhausted; watchdog health owns the next move
    if not _watchdog_mod.prod_precondition(binding):
        return  # pane mid-turn / unreadable -> defer; the un-acked watermark retries next tick
    # SWL-03: the ack target is the exact last complete row covered by the same
    # snapshot that produced this notification's count/senders.
    covered_offset = int(notification["covered_offset"])
    # LT-3: the sender-side fence — re-read the live binding immediately before the send.
    live = _send_fence_open(address, binding)
    if live is None:
        return  # terminal/collapsing/re-claimed/respawned -> no nudge, watermark unmoved
    if not _deliver_keystroke(
        tmux,
        live,
        _watchdog_mod.wake_keystroke(live, notification),
        kind="wake",
    ):
        return  # failed/unsupported send -> watermark unmoved -> next tick retries
    # R-1: delivered != consumed — do NOT ack on rc=0. Record the pending verification: the
    # covered inbox offset (the SWL-03 ack target) + the transcript baseline at send time.
    executor.record_wake_pending(
        address,
        pending_ack_offset=covered_offset,
        sent_transcript_size=_transcript_size(live),
    )


def _transcript_size(binding) -> int:
    """The verify-new-turn baseline (R-1): the transcript JSONL byte size at send time.

    An absent/unreadable transcript records 0 — the conservative direction: the eventual
    verification then requires the transcript to EXIST with agent progress before any ack."""
    path = binding.get("transcript_path")
    if not path:
        return 0
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# THE USAGE ESCAPE HATCH (owner ruling 2026-08-01, DEFERRED-REGISTER "Usage escape hatch RULED").
# Motivating incident: a 14h half-dead daemon (the IPC wedge) kept waking seats into a world where
# oversight was blind. Two daemon-side layers, whose strongest automated action is "hold and tell
# someone" — the seat-side third layer is prose law in the shared decision-delivery-signoff block.
#
#   * the SELF-CLEARING WAKE HOLD — one control-socket self-probe per sweep; a failed probe gates
#     ISSUING NEW WAKES and nothing else, and clears itself the moment the probe passes. Active-sick
#     runs burn money, so minutes matter here; a false positive costs exactly one probe interval.
#   * the FREEZE DETECTOR + ESCALATION LADDER — frozen runs burn nothing, so latency (not cost) is
#     what matters: 45 minutes of run-level quiescence is a freeze, and each rung fires only if the
#     one below it failed to move anyone.
# ---------------------------------------------------------------------------

# The self-probe's own bound. Short on purpose: it runs inside the sweep, and an unanswered probe is
# the ANSWER (degrade), never something to wait out.
SELF_PROBE_TIMEOUT_S = 2.0

# Quiescence must PERSIST this long to be a freeze. By construction the self-healing tier has
# already failed by then — nothing that heals itself takes 45 minutes.
FREEZE_QUIESCENCE_S = 45 * 60

# How long the agents get to answer before the ladder wakes the human (phone-notification
# philosophy applied up the chain).
FREEZE_ACK_WINDOW_S = 15 * 60

# The director sessions rung (b) notifies, and the transport it notifies them through.
DIRECTOR_PEERS = ("fable-run5-director", "fable-l1-l5-director")
BUS_LITE_PATH = os.environ.get("BUS_LITE_PATH", "bus-lite")

# Bounds on the two escalation subprocesses. Neither may park the sweep.
_ESCALATION_SUBPROCESS_TIMEOUT_S = 10.0

# In-memory on purpose (the RR-6 precedent): the hold is a gate on THIS incarnation's wake issuing,
# not control state. A relaunched daemon re-probes and re-derives it within one sweep.
_WAKE_HOLD: dict = {"degraded": False, "reason": None}

# Set once this process is actually serving its own control socket. The probe's subject is the
# daemon's OWN listener: a process that serves none (every test that drives poll_once directly) has
# nothing to probe and must not be gated.
_IPC_SERVING = False

# The freeze episode + the per-seat life-evidence watermark the quiescence read compares against.
# Both in-memory: a restart legitimately restarts the quiescence clock from zero.
_FREEZE: dict = {"quiescent_since": None, "episode": None}
_LIFE_EVIDENCE_SEEN: dict = {}

# Pre-actor states: the spawn path owns these seats, so work IS in flight for them.
_SPAWN_IN_FLIGHT_STATES = frozenset({"planned", "claimed", "spawning"})


def wake_hold_engaged() -> bool:
    """True while the last control-socket self-probe failed — issue no NEW wakes."""
    return bool(_WAKE_HOLD.get("degraded"))


def probe_control_socket(runtime_root=None) -> Tuple[bool, Optional[str]]:
    """Connect to this daemon's OWN control socket and take one minimal round-trip.

    Healthy means the listener accepted, dispatched, and answered with a JSON object inside
    ``SELF_PROBE_TIMEOUT_S`` — the exact property the 2026-07-31 outage destroyed while the daemon
    kept running and writing WAL. The probe command is the cheapest read on the surface
    (``show`` against no address: a keyed-map lookup, no WAL load, no lock, no mutation); what is
    load-bearing is the round-trip, not the payload, so ANY parseable object answer is health.
    """
    path = ipc_socket_path(runtime_root)
    payload = json.dumps({"command": "show", "addr": None}).encode("utf-8")
    chunks: list[bytes] = []
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(SELF_PROBE_TIMEOUT_S)
            client.connect(str(path))
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)  # the daemon's read is EOF-framed
            while True:
                data = client.recv(65536)
                if not data:
                    break
                chunks.append(data)
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc or 'control socket did not answer'}"
    raw = b"".join(chunks)
    if not raw.strip():
        return False, "control socket answered with no bytes"
    try:
        response = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return False, f"control socket answer was not JSON: {exc}"
    if not isinstance(response, dict):
        return False, "control socket answer was not a JSON object"
    return True, None


def _journal_run_level(*, event: str, summary: str, delta: Optional[dict] = None) -> None:
    """Best-effort run-level (node_address=None) journal row through the locked primitive."""
    try:
        from harnessd import executor as _executor_mod

        _executor_mod.journal(None, event=event, binding_delta=delta or {}, summary=summary)
    except Exception:  # noqa: BLE001 — the journal never crashes the sweep it observes
        pass


def _wake_hold_sweep_best_effort() -> None:
    """One self-probe per sweep; WAL rows on the TRANSITIONS ONLY (the standing over-logging diet).

    A row per probe would be a row every interval forever — exactly the diet the owner ruled
    against. Entering degraded and leaving it are the two facts worth durable space; the reason is
    kept current in memory either way.
    """
    if not _IPC_SERVING:
        return
    try:
        ok, reason = probe_control_socket()
    except Exception as exc:  # noqa: BLE001 — a probe fault is a fault, not a crash
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    if ok:
        if _WAKE_HOLD.get("degraded"):
            prior = _WAKE_HOLD.get("reason")
            _WAKE_HOLD["degraded"] = False
            _WAKE_HOLD["reason"] = None
            _journal_run_level(
                event="wake_hold_cleared",
                summary=f"control-socket self-probe answered again: new wakes resume "
                f"(was: {prior})",
                delta={"reason": prior},
            )
        return
    if not _WAKE_HOLD.get("degraded"):
        _WAKE_HOLD["degraded"] = True
        _journal_run_level(
            event="wake_hold_engaged",
            summary=f"control-socket self-probe failed ({reason}): issuing NO new wakes "
            "until it answers; mid-turn seats finish, nothing is cancelled",
            delta={"reason": reason},
        )
    _WAKE_HOLD["reason"] = reason


def _seat_life_evidence_grew(address: str, binding: dict) -> bool:
    """Did this seat produce NEW life evidence since the last quiescence read?

    Reuses the watchdog's own turn/transcript/runtime-log evidence (``latest_life_evidence``) rather
    than ``detector_signals.jsonl_progress``, whose size cache belongs to the detector's verdict —
    a second reader would move that baseline out from under it every sweep.
    """
    try:
        latest = _watchdog_mod.latest_life_evidence(address, binding, binding)
    except Exception:  # noqa: BLE001 — an unreadable seat is not evidence of stillness
        return True
    if latest is None:
        return False
    timestamp = latest[1]
    seen = _LIFE_EVIDENCE_SEEN.get(address)
    _LIFE_EVIDENCE_SEEN[address] = timestamp
    if seen is None:
        return True  # the first read is a baseline, never a verdict
    try:
        return _clock.parse_iso(timestamp) > _clock.parse_iso(seen)
    except (TypeError, ValueError):
        return True


def _seat_inbox_unacked(binding: dict) -> bool:
    """One inbox read per seat per sweep — an unreadable inbox owes nothing."""
    try:
        return bool(_watchdog_mod.inbox_has_unacked(binding, binding))
    except Exception:  # noqa: BLE001
        return False


def _run_quiescence(*, now: str) -> Tuple[bool, dict]:
    """Read run-level quiescence and the evidence snapshot rung (a) durably records.

    Quiescent means ALL of: no live seat producing life evidence, no deliverable wake outstanding,
    no spawn in flight, and the run neither terminal nor paused. Every unreadable/unknown answer is
    resolved AGAINST quiescence — the ladder must never fire on a read fault.
    """
    try:
        bindings = ledger.all_nodes()
    except Exception:  # noqa: BLE001 — a failed truth read is never evidence of a freeze
        return False, {}
    live = {
        address: binding
        for address, binding in bindings.items()
        if isinstance(binding, dict) and not states.is_terminal(binding.get("state"))
    }
    if not live:
        return False, {}  # a finished run is not a frozen one
    if any(binding.get("paused_at") is not None for binding in live.values()):
        return False, {}  # pause is the proven-lossless state, not a freeze
    if any(binding.get("state") in _SPAWN_IN_FLIGHT_STATES for binding in live.values()):
        return False, {}

    owed: list[dict] = []
    alive = False
    for address, binding in sorted(live.items()):
        if _seat_life_evidence_grew(address, binding):
            alive = True
        if _seat_inbox_unacked(binding):
            owed.append({"address": address, "item": "unacked_inbox"})
            # An unacked receipt is a lever only while the notification budget lasts. Once it is
            # spent the watchdog will never nudge again, and that stuck-forever shape is precisely
            # the third-strike unattended stall the ladder exists to cover.
            if int(binding.get("wake_attempt_count") or 0) < 3:
                alive = True
        if binding.get("wake_pending_ack_offset") is not None:
            owed.append({"address": address, "item": "unverified_wake"})
        if binding.get("gate_state") == "candidate_submitted":
            owed.append({"address": address, "item": "gate_candidate_awaiting_verdict"})
    if alive:
        return False, {}

    return True, {
        "seats": [
            {"address": address, "level": binding.get("level"), "state": binding.get("state")}
            for address, binding in sorted(live.items())
        ],
        "owed": owed,
    }


def _notify_directors(message: str) -> None:
    """Rung (b): agents get the first shot — one bus-lite line per configured director peer.

    A send failure is non-fatal and RECORDED: the ladder's whole point is that a rung failing to
    move anyone is what promotes the next one, so a dead transport must never stop the climb.
    """
    for peer in DIRECTOR_PEERS:
        try:
            completed = subprocess.run(
                [BUS_LITE_PATH, "send", peer, message],
                capture_output=True,
                timeout=_ESCALATION_SUBPROCESS_TIMEOUT_S,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 — transport faults are recorded, never fatal
            _journal_run_level(
                event="run_freeze_notify_failed",
                summary=f"bus-lite send to {peer} failed: {type(exc).__name__}: {exc}",
                delta={"peer": peer, "error": f"{type(exc).__name__}: {exc}"},
            )
            continue
        if getattr(completed, "returncode", 0):
            _journal_run_level(
                event="run_freeze_notify_failed",
                summary=f"bus-lite send to {peer} exited {completed.returncode}",
                delta={"peer": peer, "error": f"exit {completed.returncode}"},
            )


def _notify_user_macos(message: str) -> None:
    """Rung (d), first half: the human notification, bounded and best-effort."""
    script = (
        f"display notification {json.dumps(message)} "
        f'with title "L1-L5 harness" subtitle "run frozen — unacked"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=_ESCALATION_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except Exception:  # noqa: BLE001 — a missing notification surface never stops the pause
        pass


def _freeze_notice(evidence: dict, *, quiescent_since: str) -> str:
    """The one-line notice: what happened, how big, and the pointer to the durable event."""
    wal = f"{ledger.RUNTIME_ROOT}/{ledger.WAL_FILENAME}"
    return (
        f"harness run looks FROZEN: quiescent since {quiescent_since} "
        f"({len(evidence.get('seats') or [])} live seats, "
        f"{len(evidence.get('owed') or [])} owed items) — see the run_frozen event in {wal}"
    )


def _freeze_sweep_best_effort() -> None:
    """The freeze detector + the one-shot escalation ladder, one rung per sweep at most.

    Episode lifecycle: quiescence must persist ``FREEZE_QUIESCENCE_S`` before an episode opens; an
    ack or the pause CLOSES it; and a new episode requires quiescence to have CLEARED and
    re-persisted the full window — a closed episode can never re-fire against the same stillness.
    """
    try:
        now = _clock.now_utc()
        quiescent, evidence = _run_quiescence(now=now)
        if not quiescent:
            _FREEZE["quiescent_since"] = None
            _FREEZE["episode"] = None
            return
        if _FREEZE.get("quiescent_since") is None:
            _FREEZE["quiescent_since"] = now
            return
        quiescent_since = _FREEZE["quiescent_since"]
        if _clock.age_seconds(quiescent_since, now=now) < FREEZE_QUIESCENCE_S:
            return

        episode = _FREEZE.get("episode")
        if episode is None:
            # Rung (a): the durable record, written BEFORE anyone is told, so every notice can
            # point at an event that already exists.
            _journal_run_level(
                event="run_frozen",
                summary=f"run-level quiescence has persisted since {quiescent_since} "
                f"({FREEZE_QUIESCENCE_S // 60}m): no seat producing life evidence, no deliverable "
                f"wake, nothing in flight, run neither terminal nor paused",
                delta={"quiescent_since": quiescent_since, **evidence},
            )
            _FREEZE["episode"] = {"declared_at": now, "closed": False}
            # Rung (b): the agents first — this preserves the run's autonomy and is the only rung
            # that can resolve a freeze without a human.
            _notify_directors(_freeze_notice(evidence, quiescent_since=quiescent_since))
            return
        if episode.get("closed"):
            return
        # Rung (c) is the WAIT: the escalation-ack verb closes the episode from the outside.
        if _clock.age_seconds(episode["declared_at"], now=now) < FREEZE_ACK_WINDOW_S:
            return
        # Rung (d): nobody answered. Tell the human, then pause — safe precisely because nothing was
        # moving, and pause is the state proven lossless.
        episode["closed"] = True
        _notify_user_macos(
            f"Run frozen since {quiescent_since} and unacked for "
            f"{FREEZE_ACK_WINDOW_S // 60}m — pausing."
        )
        _pause_frozen_run(quiescent_since=quiescent_since)
    except Exception:  # noqa: BLE001 — the detector must never take down the sweep it watches
        pass


def _pause_frozen_run(*, quiescent_since: str) -> None:
    """Set the pause flag through the EXISTING pause machinery — the pause verb's own single writer.

    ``executor.pause`` on the run root is exactly what ``harnessctl pause`` performs; the flag is
    subtree-scoped through the node-or-ancestor walk, so the root covers the whole run.
    """
    from harnessd import commissioning as _commissioning
    from harnessd import executor as _executor_mod

    address = _commissioning.L1_ADDRESS
    try:
        result = _executor_mod.pause(address)
        ok = bool(getattr(result, "ok", False))
        detail = "" if ok else f" ({'; '.join(getattr(result, 'errors', None) or ['refused'])})"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f" ({type(exc).__name__}: {exc})"
    _journal_run_level(
        event="run_freeze_paused",
        summary=f"freeze escalation went unacked for {FREEZE_ACK_WINDOW_S // 60}m: user "
        f"notified and pause flag {'set' if ok else 'REFUSED'} on {address}{detail}",
        delta={"address": address, "quiescent_since": quiescent_since, "paused": ok},
    )


def ack_freeze_escalation() -> Tuple[bool, str]:
    """Rung (c): close the pending freeze escalation durably. Returns ``(acked, detail)``.

    One-shot and idempotent-by-refusal: with no episode open there is nothing to ack, and the
    caller gets a structured refusal rather than a silent success.
    """
    episode = _FREEZE.get("episode")
    if episode is None or episode.get("closed"):
        return False, "no pending freeze escalation to acknowledge"
    episode["closed"] = True
    declared_at = episode.get("declared_at")
    _journal_run_level(
        event="run_freeze_acked",
        summary=f"freeze escalation declared at {declared_at} acknowledged: the episode is "
        "closed and the ladder stops here (no notification, no pause)",
        delta={"declared_at": declared_at, "quiescent_since": _FREEZE.get("quiescent_since")},
    )
    return True, f"freeze escalation declared at {declared_at} acknowledged"


def has_live_build(*, read_nodes=None) -> bool:
    """Return whether durable binding truth proves this run still has work.

    This is the run-scoped daemon's *only* idle predicate. Call it only after a completed
    ``poll_once``: empty/all-terminal is the sole definitive idle result. Unknown/missing states and
    read failures are conservative-live, so corruption or a transient read fault can never shut
    protection down.
    """
    if read_nodes is None:
        read_nodes = ledger.all_nodes
    try:
        nodes = read_nodes()
    except Exception:  # noqa: BLE001 - a failed truth read must preserve protection
        return True
    if not isinstance(nodes, dict):
        return True
    for binding in nodes.values():
        if not isinstance(binding, dict):
            return True
        if not states.is_terminal(binding.get("state")):
            return True
    return False


def poll_loop(interval_s, executor=None, tmux=None, detector=None) -> None:
    """Run completed reconcile ticks while durable binding truth says the build is live.

    The body remains FACTORED into ``poll_once``. Only after that whole body returns and the
    best-effort status write completes does the loop read the binding ledger. Empty/all-terminal
    returns normally; a missing/unknown state or read failure is conservative-live. A crash anywhere
    in the tick still escapes nonzero to launchd, whose crash-only KeepAlive restarts in recovery mode.

    The production defaults wire the real executor + tmux + detector; this signature keeps them
    injectable so tests can prove both completed-tick ordering and the idle return.
    """
    if executor is None:
        from harnessd import executor as executor  # noqa: F811 — production default
    while True:
        poll_once(executor, tmux, detector)
        try:
            write_status(ledger.RUNTIME_ROOT)
        except Exception:
            # The status sidecar is best-effort (§4.4) — a write hiccup must not kill the run loop.
            pass
        if not has_live_build():
            return None
        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# write_status — the lock-FREE status sidecar (the ONE deliberate atomicity carve-out, §4.4).
# ---------------------------------------------------------------------------

def write_status(runtime_root, status: Optional[dict] = None) -> Optional[Path]:
    """Write the best-effort liveness sidecar ``status.json`` — LOCK-FREE (the §4.4 carve-out).

    The ONE deliberate carve-out: status.json is written every poll WITHOUT the EX serialization lock
    (taking the lock would serialize a non-event against real mutations every tick). This writer NEVER
    enters ``store.file_lock`` — it writes the sidecar via the lock-free ``store.atomic_replace`` (tmp
    + fsync + os.replace, so the sidecar is never TORN, but no lock is taken). It is NOT durable
    control state: it appends ZERO WAL rows (recovery NEVER trusts the sidecar — the ledger is truth).

    Path: ``<runtime_root>/.harnessd/status.json`` (the §3 on-disk tree). Carries the §2.3 liveness
    fields (pid / started_at / incarnation, whatever the caller supplies) + a best-effort runtime_root.
    Returns the written path, or None when no runtime_root is resolvable (best-effort).
    """
    import os

    from harnessd import clock

    if runtime_root is None:
        runtime_root = ledger.RUNTIME_ROOT
    if runtime_root is None:
        return None
    runtime_root = Path(runtime_root)

    payload = dict(status or {})
    # Fill the §2.3 liveness fields the caller did not supply (best-effort defaults).
    payload.setdefault("pid", os.getpid())
    if "started_at" not in payload:
        try:
            import json

            descriptor = json.loads(
                (runtime_root / "runtime.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, UnicodeDecodeError):
            descriptor = {}
        payload["started_at"] = (
            descriptor.get("started_at")
            if isinstance(descriptor, dict) and descriptor.get("started_at")
            else clock.now_utc()
        )
    payload.setdefault("runtime_root", str(runtime_root))

    path = runtime_root / ".harnessd" / "status.json"

    import json

    def render(handle):
        json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=2)
        handle.write("\n")

    # LOCK-FREE: atomic_replace is tmp+fsync+os.replace — it NEVER takes store.file_lock (§4.4).
    store.atomic_replace(path, render)
    return path


def stamp_last_tick(runtime_root=None) -> Optional[Path]:
    """Stamp ``runtime.json.last_tick_at`` — the §2.6 hang-detector surface, LOCK-FREE (§4.4).

    Called at the END of every ``poll_once`` (completed-tick semantics). The field preserves the
    stable diagnostic/future external hang-detector surface for the third death mode (hang), which
    launchd's crash-only KeepAlive cannot see (findings daemon-1 / COMP-5). The run-scoped lifecycle
    does not install a standing pinger.

    READ-MERGE, not a wholesale rewrite: the §2.3 boot descriptor genesis wrote (build_id /
    started_at / pid / lock_path) is preserved; only ``last_tick_at`` is OVERWRITTEN (never
    setdefault — a write-once stamp would look permanently fresh-then-stale and defeat the §2.6
    staleness math). A missing/corrupt runtime.json is self-healed to a minimal descriptor — it
    is a liveness mirror, not control state. The lock-free read-modify-write is safe because the
    daemon's poll thread is the SOLE runtime.json writer post-boot (the instance lock excludes a
    second daemon; the IPC thread never writes it) — a future second writer would make this
    last-writer-wins, acceptable for a best-effort mirror.

    The same §4.4 carve-out as ``write_status``: written via the lock-free ``store.atomic_replace``
    (never torn), NEVER enters ``store.file_lock``, appends ZERO WAL rows (recovery never trusts
    it). Failures are swallowed by the best-effort wrapper; status may then diagnose the run as
    degraded, but recovery never treats this mirror as control truth.
    Returns the written path, or None when no runtime_root is resolvable (best-effort).
    """
    import json
    import os

    if runtime_root is None:
        runtime_root = ledger.RUNTIME_ROOT
    if runtime_root is None:
        return None
    runtime_root = Path(runtime_root)
    path = runtime_root / "runtime.json"

    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(descriptor, dict):
            descriptor = {}  # coerce a non-dict liveness mirror back to a descriptor (self-heal)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        descriptor = {}  # self-heal a missing/corrupt mirror — it is NOT control state
    descriptor.setdefault("pid", os.getpid())
    descriptor.setdefault("runtime_root", str(runtime_root))
    descriptor["last_tick_at"] = _clock.now_utc()  # OVERWRITE — the stamp must ADVANCE every tick

    def render(handle):
        json.dump(descriptor, handle, ensure_ascii=True, sort_keys=True, indent=2)
        handle.write("\n")

    # LOCK-FREE: atomic_replace is tmp+fsync+os.replace — it NEVER takes store.file_lock (§4.4).
    store.atomic_replace(path, render)
    return path


def _stamp_last_tick_best_effort() -> None:
    """Stamp last_tick_at; swallow any error so a sidecar hiccup never aborts the reconcile sweep."""
    try:
        stamp_last_tick(ledger.RUNTIME_ROOT)
    except Exception:  # noqa: BLE001 — §4.4 best-effort: the sweep must advance past a stamp failure
        pass


# ---------------------------------------------------------------------------
# IPC listener — bind+serve the AF_UNIX control socket the CLI dials (FORK-IPC / FORK-SOCKET-PATH). The
# review found serve_forever had NO production caller: the whole CLI->daemon path was dead. The daemon
# binds the listener at boot and serves it in a thread alongside poll_loop.
# ---------------------------------------------------------------------------

# Mirror harnessctl's resolution so client + daemon agree on the path WITHOUT importing the CLI here.
_IPC_DIRNAME = ".harnessd"
_IPC_SOCKET_FILENAME = "harnessd.sock"


def ipc_socket_path(runtime_root) -> Path:
    """The canonical IPC socket path: ``<runtime_root>/.harnessd/harnessd.sock`` (harnessctl's default)."""
    if runtime_root is None:
        runtime_root = ledger.RUNTIME_ROOT
    return Path(runtime_root) / _IPC_DIRNAME / _IPC_SOCKET_FILENAME


def make_ipc_listener(runtime_root) -> "socket.socket":
    """Bind + listen the AF_UNIX control socket at the canonical path; return the listening socket.

    A STALE socket file (a prior daemon's leftover after a crash) is unlinked before bind (AF_UNIX bind
    fails EADDRINUSE on an existing path even if no listener holds it). The caller owns the socket
    lifecycle (close on shutdown); ``serve_forever`` loops ``serve_one`` over it.

    The listener is CLOSE-ON-EXEC (IPC-DEADLOCK-2026-08-01): no daemon-spawned subprocess may hold the
    control socket open behind the daemon's back. Python already makes sockets non-inheritable (PEP
    446) — the explicit call pins the property against a listener ever being rebuilt from a raw fd.
    """
    import os
    import socket

    path = ipc_socket_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists():
            os.unlink(path)  # clear a stale leftover socket file so bind() succeeds
    except OSError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.set_inheritable(False)
    sp = str(path)
    # AF_UNIX sun_path is ~104 bytes (FORK-IPC-SOCKET-LEN). A deeply-nested RUNTIME_ROOT can exceed it.
    # Fallback: chdir to the socket's parent + bind the BASENAME (a short relative path), then restore.
    # Safe here because make_ipc_listener runs ONCE at boot, single-threaded, before the serve thread.
    if len(sp.encode("utf-8")) < 100:
        listener.bind(sp)
    else:
        old_cwd = os.getcwd()
        try:
            os.chdir(str(path.parent))
            listener.bind(path.name)
        finally:
            os.chdir(old_cwd)
    listener.listen(64)
    return listener


# ---------------------------------------------------------------------------
# run — process entrypoint body: claim start authority -> boot/recover -> serve IPC -> poll while
# live -> clean idle teardown. Invoked by the request-aware __main__ guard.
# ---------------------------------------------------------------------------

def _apply_global_seams(runtime) -> None:
    """Bind the process-global seams the substrate READS but ``boot`` does not set: ``ledger.RUNTIME_ROOT``
    (the genesis/executor/ledger anchor) + the dedicated tmux-server socket (so harness panes land on the
    daemon's OWN tmux server, isolated from the user's default — and the operator attaches there to watch:
    ``tmux -L <socket> attach -t harness-<collapsed-addr>``) + the detector's §2.11 tmux seam
    (``detector_signals._tmux`` — pane_alive RAISES un-bound; before this binding the seam was only
    ever bound inside tests, so a real tick could never read pane liveness). Must run BEFORE boot
    (genesis raises without RUNTIME_ROOT). Idempotent.
    """
    runtime_root = _runtime_root(runtime)
    ledger.RUNTIME_ROOT = runtime_root
    from harnessd import detector_signals as _detector_signals
    from harnessd.spawn import tmux as _tmux

    # E4 — the per-runtime adapter REGISTRY (production wiring): the chokepoint resolves the adapter
    # from level_config.runtime. Since the 2026-07-13 L5-runtime unification every seat (L1-L5)
    # resolves the ClaudeCodeAdapter; the CodexAdapter stays registered as a dormant,
    # registry-selectable capability for any future Codex-harness seat. The injected single-adapter
    # seam (runtime.adapter via boot) remains for back-compat/tests; the registry wins per-runtime.
    from harnessd.spawn import chokepoint as _chokepoint
    from harnessd.spawn.adapters.claude_code import ClaudeCodeAdapter as _CCAdapter
    from harnessd.spawn.adapters.codex import CodexAdapter as _CodexAdapter
    _chokepoint.register_runtime_adapter("claude-code", _CCAdapter())
    _chokepoint.register_runtime_adapter("codex", _CodexAdapter())

    tmux_socket = getattr(runtime, "tmux_socket", None)
    if tmux_socket:
        _tmux.set_socket(tmux_socket)
    # The Increment-9 binding detector_signals' docstring promised: pane_alive reads the REAL
    # wrapper (on whatever socket is bound above). Without this, production liveness raises.
    _detector_signals._tmux = _tmux


def _remove_ipc_socket(runtime_root) -> None:
    try:
        ipc_socket_path(runtime_root).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def run(runtime, *, interval_s: float = 5.0, lifecycle_spec=None) -> None:
    """Assemble one run-scoped daemon process.

    With a lifecycle spec, the already-acquired lifetime lock fences an atomic one-shot request
    claim. Claimed means deliberate full genesis; absent means launchd crash recovery and therefore
    reconciliation-only. A definitive idle return closes IPC/releases the lock and schedules the
    bounded after-parent bootout child. Exceptions never schedule cleanup, so launchd protection
    remains loaded and restarts the fixed recovery argv.

    The IPC serve runs in a separate thread so a blocking ``accept()`` never stalls the reconcile/watchdog
    sweep, and the sweep never stalls request handling. Both route every MUTATION through the ONE
    executor under the ONE EX lock (the single-writer invariant holds across the two threads — fcntl
    serializes them).
    """
    global _IPC_SERVING

    _apply_global_seams(runtime)
    runtime_root = _runtime_root(runtime)
    acquire_instance_lock(runtime_root)

    recover_only = False
    if lifecycle_spec is not None:
        claimed = lifecycle.claim_start_request(
            lifecycle_spec.paths.request,
            lifecycle_spec.paths.claimed_request,
            expected_request_id=lifecycle_spec.request_id,
        )
        recover_only = claimed is None

    listener = None
    normal_idle = False
    try:
        boot(runtime, recover_only=recover_only)

        # Recovery can legitimately find that the final binding became terminal before the crash.
        # Do not open a socket/loop for an already-idle recovered run.
        if recover_only and not has_live_build():
            normal_idle = True
            return None

        listener = make_ipc_listener(runtime_root)
        serve_thread = threading.Thread(
            target=_ipc_mod.serve_forever,
            args=(listener,),
            name="harnessd-ipc",
            daemon=True,
        )
        serve_thread.start()
        # From here the sweep's self-probe has a subject: THIS process's own control socket.
        _IPC_SERVING = True

        # Resolve the sweep collaborators (production defaults; tests inject at the factored seams).
        from harnessd import executor as _executor
        from harnessd.spawn import tmux as _tmux
        from harnessd import detector as _detector

        poll_loop(interval_s, executor=_executor, tmux=_tmux, detector=_detector)
        normal_idle = True
        return None
    finally:
        _IPC_SERVING = False
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        _remove_ipc_socket(runtime_root)
        release_instance_lock()
        if normal_idle and lifecycle_spec is not None:
            lifecycle.schedule_cleanup_after_exit(
                lifecycle_spec,
                parent_pid=os.getpid(),
            )


def _entry_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harnessd.daemon")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--start-request", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--launchd-label", required=True)
    parser.add_argument("--launchd-plist", required=True)
    return parser


def main(argv=None) -> int:
    """Assemble the fixed launchd argv into a runtime and run-scoped lifecycle spec."""
    from harnessd import commissioning as _commissioning

    args = _entry_parser().parse_args(argv)
    runtime_root, build_id = _commissioning.resolve_runtime_identity(
        runtime_root=args.runtime_root,
        build_id=args.build_id,
    )
    paths = lifecycle.paths_for(runtime_root)
    supplied_request = Path(args.start_request).expanduser().resolve()
    supplied_plist = Path(args.launchd_plist).expanduser().resolve()
    if supplied_request != paths.request or supplied_plist != paths.plist:
        raise lifecycle.LifecycleError(
            "launchd lifecycle paths do not match the canonical runtime-root paths"
        )
    expected_label = lifecycle.launchd_label(runtime_root, build_id)
    if args.launchd_label != expected_label:
        raise lifecycle.LifecycleError(
            f"launchd label mismatch: expected {expected_label!r}, got {args.launchd_label!r}"
        )

    pending = lifecycle.peek_start_request(paths.request)
    initial_intake = None
    fidelity_playback_authority = "owner"
    fidelity_playback_delegate = None
    fidelity_playback_delegation_reason = None
    review_panel_arms = []
    if pending is not None:
        if pending.get("request_id") != args.request_id:
            raise lifecycle.LifecycleError(
                f"lifecycle request id mismatch: expected {args.request_id!r}, "
                f"found {pending.get('request_id')!r}"
            )
        if pending.get("build_id") != build_id:
            raise lifecycle.LifecycleError("lifecycle request build id does not match launchd argv")
        if Path(pending.get("runtime_root")).expanduser().resolve() != runtime_root:
            raise lifecycle.LifecycleError(
                "lifecycle request runtime root does not match launchd argv"
            )
        initial_intake = pending.get("initial_intake")
        try:
            (
                fidelity_playback_authority,
                fidelity_playback_delegate,
                fidelity_playback_delegation_reason,
            ) = _commissioning.validate_fidelity_playback_authority(
                pending.get("fidelity_playback_authority"),
                pending.get("fidelity_playback_delegate"),
                pending.get("fidelity_playback_delegation_reason"),
            )
        except RuntimeError as exc:
            raise lifecycle.LifecycleError(
                str(exc),
                error_code="fidelity_playback_declaration_invalid",
            ) from exc
        try:
            review_panel_arms = _commissioning.normalize_review_panel_arms(
                pending.get("review_panel_arms")
            )
        except RuntimeError as exc:
            raise lifecycle.LifecycleError(
                str(exc),
                error_code="review_panel_arm_invalid",
            ) from exc

    runtime_kwargs = {
        "runtime_root": runtime_root,
        "build_id": build_id,
        "initial_intake": initial_intake,
        "fidelity_playback_authority": fidelity_playback_authority,
        "fidelity_playback_delegate": fidelity_playback_delegate,
        "fidelity_playback_delegation_reason": (
            fidelity_playback_delegation_reason
        ),
    }
    if review_panel_arms:
        runtime_kwargs["review_panel_arms"] = review_panel_arms
    runtime = _commissioning.build_runtime(
        **runtime_kwargs,
    )
    spec = lifecycle.RunSpec(
        runtime_root=runtime_root,
        build_id=build_id,
        label=expected_label,
        request_id=args.request_id,
        paths=paths,
        repo_root=lifecycle.REPO_ROOT,
        python_executable=sys.executable,
    )
    run(runtime, lifecycle_spec=spec)
    return 0


if __name__ == "__main__":  # pragma: no cover — launchd's run-scoped process entry
    raise SystemExit(main())
