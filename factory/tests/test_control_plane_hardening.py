"""Daemon/state correctness — control-plane hardening (pre-live-run cluster B, part 1).

Pins four fixes:

  * SWL-01 — failure-path journal rows (spawn_failed escalations, *_send_failed, kickoff_*,
    delivery_failed_escalation, ipc_request_failed, …) allocate ``next_seq()`` + append the WAL row
    INSIDE the per-mutation EX lock (``executor.journal`` owns it), so the two-thread daemon
    (IPC + poll) can never mint duplicate seq values against the locked single writer. A
    source-audit pins that no module outside executor/ledger appends to the WAL directly.

  * RR-1 — one bad IPC client (non-JSON bytes, a handler exception, an early disconnect) must
    NEVER kill the daemon's control plane: serve_one returns a structured ``{ok: false, errors}``
    response and journals ``ipc_request_failed`` for real request/response failures. The
    fire-and-forget ``turn-hook-adopt`` caller deliberately does not read a response, so its broken
    respond leg is not a failure. serve_forever continues past every per-connection fault
    (BrokenPipe IS OSError — the pre-fix loop misread it as listener shutdown and exited the accept
    loop cleanly and permanently) and exits ONLY on a closed listener.

  * RR-2 — a malformed agent-written ``.signal.<seat>.json`` (torn JSON / non-UTF-8 / a JSON
    non-dict) is agent-controlled input: it must degrade to a journaled rejection
    (``signal_artifact_invalid`` + quarantine to ``*.invalid``), never crash the watchdog tick or
    the detector/reconcile path (the pre-fix daemon either crash-looped — the poison file
    survives relaunch — or silently disabled the node's supervision forever).

  * RR-6 — ``_watchdog_tick``'s per-node fault isolator journals an edge-triggered
    ``watchdog_sweep_error`` row instead of a silent ``continue`` (the deliberate fail-loud
    raises, e.g. MissingTranscriptPath, used to terminate in zero trace).

Style: real ledger/executor on a tmp RUNTIME_ROOT (the test_watchdog.py pattern); real AF_UNIX
sockets for the IPC legs; no model usage, no real tmux.
"""

from __future__ import annotations

import copy
import json
import socket
import threading
from pathlib import Path

import pytest

import harnessd.addressing as addressing
import harnessd.daemon as daemon
import harnessd.detector_signals as detector_signals
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ipc as ipc
import harnessd.ledger as ledger
import harnessd.store as store
import harnessd.watchdog as watchdog


LEAF = "proj/widget#exec"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(detector_signals, "_size_cache", {}, raising=False)
    # RR-6 edge-trigger memory is per-process; isolate it per test.
    monkeypatch.setattr(daemon, "_SWEEP_ERRORS_JOURNALED", {}, raising=False)
    return tmp_path


def _binding(node_address=LEAF, *, state="running", lease_epoch=1, generation=0,
             transcript_path=None, extra=None):
    token = fencing.mint_owner_token(node_address, "subagent-x", "sess-x", lease_epoch)
    rec = {
        "node_address": node_address,
        "parent_address": None,
        "level": "L5",
        "subagent_id": "subagent-x",
        "session_uuid": "sess-x",
        "tmux_target": "harness:t.0",
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "working",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": transcript_path,
    }
    if extra:
        rec.update(extra)
    return rec


def _seed(*bindings):
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _events(node=None):
    rows = ledger.load_wal()
    if node is not None:
        rows = [r for r in rows if r.get("node_address") == node]
    return [r.get("event") for r in rows]


# ===========================================================================
# SWL-01 — executor.journal owns the EX lock around next_seq + append_wal.
# ===========================================================================

def test_journal_allocates_seq_and_appends_under_the_ex_lock(runtime, monkeypatch):
    """The seq allocation runs while .harnessd.lock is HELD: a concurrent non-blocking EX acquire
    must fail with BlockingIOError exactly when next_seq is read (the SWL-01 race window)."""
    _seed(_binding())
    observed = {}
    real_next_seq = ledger.next_seq

    def probing_next_seq(**kwargs):
        try:
            handle = store.flock_exclusive_nb(executor.lock_path())
        except BlockingIOError:
            observed["locked"] = True
        else:  # pragma: no cover — the mutant (unlocked journal) lands here
            observed["locked"] = False
            handle.close()
        return real_next_seq(**kwargs)

    monkeypatch.setattr(ledger, "next_seq", probing_next_seq)
    executor.journal(LEAF, event="probe_journal", summary="SWL-01 lock probe")

    assert observed.get("locked") is True, (
        "executor.journal must allocate next_seq INSIDE the held EX lock — an unlocked "
        "allocation races the single writer on the other daemon thread (SWL-01)"
    )
    assert "probe_journal" in _events(LEAF), "the journal row must actually land in the WAL"


def test_journal_rows_are_never_replayable(runtime):
    """Journal rows carry expected_generation/generation None — replay can never apply them."""
    _seed(_binding())
    executor.journal(LEAF, event="probe_journal", summary="non-transition row")
    row = [r for r in ledger.load_wal() if r.get("event") == "probe_journal"][-1]
    assert row["expected_generation"] is None and row["generation"] is None


def test_no_direct_wal_appends_outside_executor_and_ledger():
    """Grep-audit (the SWL-01 fix shape): every WAL append outside executor/ledger must route
    through executor.journal (the locked primitive) — no unlocked next_seq+append_wal remains."""
    import harnessd

    root = Path(harnessd.__file__).resolve().parent
    offenders = []
    for py in sorted(root.rglob("*.py")):
        if py.name in ("executor.py", "ledger.py"):
            continue
        if "append_wal(" in py.read_text(encoding="utf-8"):
            offenders.append(str(py.relative_to(root)))
    assert not offenders, (
        f"direct ledger.append_wal callers outside executor/ledger: {offenders} — route them "
        "through executor.journal (the EX-locked allocation+append, SWL-01)"
    )


# ===========================================================================
# RR-1 — the IPC control plane survives bad clients.
# ===========================================================================

def _ipc_round_trip(listener_path, payload: bytes, *, handler=None):
    """One real-socket round-trip: client sends ``payload``, serve_one handles, returns response."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(listener_path))
    listener.listen(1)
    response_box = {}

    kwargs = {"handler": handler} if handler is not None else {}

    def _serve():
        ipc.serve_one(listener, **kwargs)

    server = threading.Thread(target=_serve, daemon=True)
    server.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(listener_path))
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            data = client.recv(65536)
            if not data:
                break
            chunks.append(data)
        response_box["raw"] = b"".join(chunks)
    finally:
        client.close()
        server.join(timeout=5)
        listener.close()
    assert not server.is_alive(), "serve_one must return (a wedged serve thread is a dead control plane)"
    return json.loads(response_box["raw"].decode("utf-8")) if response_box.get("raw") else None


def test_non_json_request_returns_structured_error_and_journals(runtime, tmp_path):
    """Garbled client bytes (`echo x | nc -U …`) -> {ok: false, errors} + ipc_request_failed row —
    the pre-fix serve_one raised json.JSONDecodeError out of the daemon's IPC thread (dead control
    plane, zero journal)."""
    response = _ipc_round_trip(tmp_path / "ipc.sock", b"this is not json")
    assert response is not None and response["ok"] is False
    assert response["errors"], "the malformed request must surface a structured reason"
    assert "ipc_request_failed" in _events(), "the control-plane fault must be journaled (RR-1)"


def test_handler_exception_returns_structured_error_not_a_dead_thread(runtime, tmp_path):
    """A handler bug (or RR-2's pre-fix garbled-artifact raise via _handle_answer) is a
    per-request fault: structured error response + journal, never an escaped exception."""
    def _broken_handler(request):
        raise RuntimeError("boom inside a handler")

    payload = json.dumps({"command": "show", "addr": LEAF}).encode("utf-8")
    response = _ipc_round_trip(tmp_path / "ipc.sock", payload, handler=_broken_handler)
    assert response is not None and response["ok"] is False
    assert any("boom inside a handler" in e for e in response["errors"])
    assert "ipc_request_failed" in _events(), "the handler fault must be journaled (RR-1)"


class _BrokenReplyConnection:
    def __init__(self, request):
        self._payload = json.dumps(request).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, _value):
        """serve_one bounds every accepted connection (IPC-DEADLOCK-2026-08-01); this double
        never stalls, so the bound is a no-op here."""

    def set_inheritable(self, _flag):
        """Same for the close-on-exec seal — no real fd behind this double."""

    def recv(self, _size):
        payload, self._payload = self._payload, b""
        return payload

    def sendall(self, _payload):
        raise BrokenPipeError("caller closed without reading the response")


class _OneConnectionListener:
    def __init__(self, request):
        self.connection = _BrokenReplyConnection(request)

    def accept(self):
        return self.connection, None


def test_fire_and_forget_turn_hook_adopt_broken_reply_is_not_an_ipc_failure(runtime):
    request = {
        "command": "turn-hook-adopt",
        "addr": LEAF,
        "event_id": "raw-event-1",
    }

    ipc.serve_one(
        _OneConnectionListener(request),
        handler=lambda payload: {"ok": True, "command": payload["command"]},
    )

    assert "ipc_request_failed" not in _events()


def test_response_bearing_request_broken_reply_still_journals_ipc_failure(runtime):
    request = {"command": "show", "addr": LEAF}

    ipc.serve_one(
        _OneConnectionListener(request),
        handler=lambda payload: {"ok": True, "command": payload["command"]},
    )

    rows = [
        row
        for row in ledger.load_wal()
        if row.get("event") == "ipc_request_failed"
    ]
    assert len(rows) == 1
    assert rows[0]["binding_delta"]["stage"] == "respond"
    assert rows[0]["binding_delta"]["command"] == "show"


def test_serve_forever_survives_per_connection_oserror_and_exits_on_closed_listener(runtime, tmp_path, monkeypatch):
    """BrokenPipeError IS OSError: the pre-fix ``except OSError: return`` misread an early client
    disconnect as listener shutdown and ended the accept loop permanently. The loop must continue
    past per-connection faults and exit ONLY when the listener is actually closed."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(tmp_path / "ipc-forever.sock"))
    listener.listen(1)
    calls = []

    def _fake_serve_one(lst, *, handler=ipc.handle_request):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise BrokenPipeError("client hung up before reading the response")
        if len(calls) == 2:
            raise RuntimeError("a non-OSError per-connection fault")
        lst.close()  # the REAL shutdown signal: subsequent accept raises with fileno() == -1
        raise OSError("accept on closed listener")

    monkeypatch.setattr(ipc, "serve_one", _fake_serve_one)
    ipc.serve_forever(listener)  # must RETURN (not raise, not loop forever)

    assert len(calls) == 3, (
        f"serve_forever made {len(calls)} serve_one call(s) — it must CONTINUE past the "
        "BrokenPipeError AND the non-OSError fault, exiting only on the closed listener (RR-1)"
    )
    assert _events().count("ipc_request_failed") >= 2, (
        "each per-connection serve fault must be journaled ipc_request_failed (best-effort)"
    )


# ===========================================================================
# RR-2 — a malformed .signal artifact is rejected + journaled, never a crash.
# ===========================================================================

def _signal_path(runtime):
    p = addressing.signal_path(LEAF, runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_garbled_signal_artifact_is_rejected_journaled_and_quarantined(runtime):
    binding = _binding()
    _seed(binding)
    p = _signal_path(runtime)
    p.write_text("{torn json — agent died mid-write", encoding="utf-8")

    sig = detector_signals.read_terminal_signal(binding, binding)

    assert sig is None, "a torn artifact is REJECTED, not adopted (WATCHDOG §7) — no verdict input"
    assert "signal_artifact_invalid" in _events(LEAF), (
        "the rejection must be VISIBLE: one signal_artifact_invalid run-ledger row (RR-2)"
    )
    assert not p.exists() and p.with_name(p.name + ".invalid").exists(), (
        "the malformed artifact must be quarantined to *.invalid (the edge-trigger: the next "
        "tick must not re-trip, and the operator/agent can inspect the bytes)"
    )


def test_json_non_dict_signal_artifact_is_rejected_not_attributeerror(runtime):
    binding = _binding()
    _seed(binding)
    _signal_path(runtime).write_text('["DONE", "not-an-object"]', encoding="utf-8")
    assert detector_signals.read_terminal_signal(binding, binding) is None
    assert "signal_artifact_invalid" in _events(LEAF)


def test_check_leaf_survives_garbled_signal_and_falls_through_to_liveness(runtime, monkeypatch):
    """Path A (watchdog tick): the garbled artifact must not crash check_leaf NOR silently
    disable the node — STEP A rejects it and STEP B's liveness ladder still runs."""
    binding = _binding()
    _seed(binding)
    _signal_path(runtime).write_text("not json at all", encoding="utf-8")

    from harnessd.detector import Liveness
    watchdog.set_liveness(lambda addr: Liveness(state="working", last_progress_at=None))
    try:
        action = watchdog.check_leaf(binding, binding, now=None)
    finally:
        watchdog.set_liveness(None)

    assert getattr(action, "kind", None) == watchdog.NOOP, (
        "a garbled signal must fall through to the liveness ladder (here: working -> NOOP), "
        "never raise out of the watchdog tick"
    )
    assert "signal_artifact_invalid" in _events(LEAF)


def test_detector_waiting_reason_survives_garbled_signal(runtime):
    """Path B (detector/reconcile): _has_legit_waiting_reason used to PROPAGATE the
    JSONDecodeError — reconcile_tick aborted, poll_loop died, launchd relaunched into the same
    poison file: a deterministic crash loop. Now it degrades to 'no waiting reason'."""
    from harnessd import detector

    binding = _binding()
    _seed(binding)
    _signal_path(runtime).write_text("\x00\x01 garbage", encoding="utf-8")

    assert detector._has_legit_waiting_reason(LEAF, binding) is False, (
        "a garbled artifact carries no fenced ESCALATED reason — and must NOT raise (RR-2 path B)"
    )


# ===========================================================================
# RR-6 — the per-node sweep fault isolator journals (edge-triggered), never silent.
# ===========================================================================

def test_watchdog_tick_journals_sweep_error_edge_triggered(runtime):
    """A node whose evaluation raises every tick (here: MissingTranscriptPath — the deliberate
    fail-loud contract violation) is isolated AND journaled ONCE, not silently skipped forever."""
    _seed(_binding(transcript_path=None))  # no transcript_path -> detector raises (fail-loud)

    daemon._watchdog_tick(executor, tmux=None, detector=None)
    first = _events(LEAF).count("watchdog_sweep_error")
    assert first == 1, (
        f"the per-node fault must journal ONE watchdog_sweep_error row (got {first}) — the "
        "pre-fix `except Exception: continue` left ZERO trace (RR-6)"
    )

    daemon._watchdog_tick(executor, tmux=None, detector=None)
    assert _events(LEAF).count("watchdog_sweep_error") == 1, (
        "steady-state re-detection of the SAME fault must not re-journal every tick (edge-trigger)"
    )


def test_watchdog_tick_skips_planned_review_without_transcript(runtime):
    """A planned #review slot is registered future work, not an actor-bearing watchdog subject.

    It legitimately has no transcript_path until a candidate submission opens the reviewer. The
    fail-loud MissingTranscriptPath guard must still catch broken running actors, but planned
    seats must stay quiet and be handled by spawn/redrive.
    """
    review = _binding(
        node_address="proj/widget#review",
        state="planned",
        transcript_path=None,
        extra={
            "level": "L4+",
            "role_variant": "L4+#review",
            "parent_address": "proj#exec",
            "gate_for": "proj/widget#exec",
        },
    )
    _seed(review)

    daemon._watchdog_tick(executor, tmux=None, detector=None)

    assert "watchdog_sweep_error" not in _events("proj/widget#review"), (
        "planned review seats are not yet actors; watchdog must not run detector_signals on their "
        "intentionally absent transcript_path"
    )
