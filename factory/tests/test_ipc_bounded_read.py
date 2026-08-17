"""IPC transport — the accepted connection is BOUNDED, and IPC fds never leak into subprocesses.

Pins the fix for the 2026-07-31 control-plane outage (IPC-DEADLOCK-2026-08-01). ``harnessd.ipc`` is a
SINGLE-THREADED accept loop over an EOF-framed protocol: ``serve_one`` accepts one connection and
``_recv_all`` reads it until the peer signals EOF. Pre-fix that read had NO bound, so ONE client that
sent a complete request and then never closed its write half parked the daemon in ``recv`` for 14
hours. The listen backlog filled (66 queued connections), every later client got ECONNREFUSED, and the
whole CLI->daemon control plane was down while the daemon kept running and writing WAL.

  * BOUNDED READ — the accepted connection carries ``ipc.REQUEST_IDLE_TIMEOUT_S`` as its socket
    timeout. A peer that stops producing bytes past the bound gets a structured
    ``{ok: false, errors}`` abort + an ``ipc_request_failed`` journal row at stage ``read``, its
    connection is closed, and the loop RETURNS TO ``accept`` — one stalled client can no longer
    starve every other client. A slow-but-legitimate client that keeps producing under the bound is
    still served in full. The bound is deliberately per-``recv`` IDLE time, not a total request
    deadline: it is sized for "no legitimate request ever goes this long without a byte", and the
    largest observed real request (~1.2MB over a local AF_UNIX socket) completes sub-second.
  * CLOSE-ON-EXEC — the listener and every accepted connection are non-inheritable, so a
    daemon-spawned subprocess can never hold an IPC connection open behind the client's back. Python
    sets this by default (PEP 446); the explicit calls + these tests are the regression pin.

Style: real AF_UNIX sockets on a tmp RUNTIME_ROOT and the BOUNDED ``serve_one`` primitive driven a
fixed number of steps (the module's own "no unbounded serve loop in a test path" rule); no model
usage, no real tmux. Every test that waits on a socket carries its own hard timeout, so the RED state
FAILS rather than hanging the suite.
"""

from __future__ import annotations

import copy
import json
import socket
import threading

import pytest

import harnessd.daemon as daemon
import harnessd.detector_signals as detector_signals
import harnessd.fencing as fencing
import harnessd.ipc as ipc
import harnessd.ledger as ledger


LEAF = "proj/widget#exec"

# Every socket wait in this module is capped at this many seconds. It must be comfortably larger
# than the per-test bound so a RED run FAILS on the assertion instead of hanging the suite.
HARD_TIMEOUT_S = 10.0


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(detector_signals, "_size_cache", {}, raising=False)
    return tmp_path


def _seed_leaf():
    """One committed binding so the real ``show`` handler has ledger state to return."""
    token = fencing.mint_owner_token(LEAF, "subagent-x", "sess-x", 1)
    rec = {
        "node_address": LEAF,
        "parent_address": None,
        "level": "L5",
        "subagent_id": "subagent-x",
        "session_uuid": "sess-x",
        "tmux_target": "harness:t.0",
        "state": "running",
        "generation": 0,
        "lease_epoch": 1,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "working",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": None,
    }
    ledger.write_binding({LEAF: copy.deepcopy(rec)}, _lock_held=True)


def _events():
    return [row.get("event") for row in ledger.load_wal()]


def _ipc_failure_rows(stage=None):
    rows = [row for row in ledger.load_wal() if row.get("event") == "ipc_request_failed"]
    if stage is not None:
        rows = [row for row in rows if (row.get("binding_delta") or {}).get("stage") == stage]
    return rows


def _listener(path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)
    return listener


def _serve_steps(listener, count, *, handler=None):
    """Drive EXACTLY ``count`` bounded ``serve_one`` steps on a thread; capture any escaped fault."""
    kwargs = {"handler": handler} if handler is not None else {}
    box: dict = {}

    def _run():
        try:
            for _ in range(count):
                ipc.serve_one(listener, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — the test asserts on what escaped, if anything
            box["escaped"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, box


def _connect(path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(HARD_TIMEOUT_S)
    client.connect(str(path))
    return client


def _read_to_eof(client):
    chunks = []
    while True:
        data = client.recv(65536)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def _echo_handler(request):
    return {"ok": True, "command": request.get("command"), "marker": request.get("marker")}


# ===========================================================================
# The bounded read — one stalled client must not starve the listener.
# ===========================================================================

def test_a_stalled_client_does_not_starve_the_next_client(runtime, tmp_path, monkeypatch):
    """THE INCIDENT, in miniature: client A sends a COMPLETE request and never closes its write half
    (the orphaned ``harnessctl message`` that held its connection for 14 hours). Client B must still
    be served — pre-fix the daemon sat in ``recv`` on A forever and B waited behind it until the test's
    own hard timeout."""
    monkeypatch.setattr(ipc, "REQUEST_IDLE_TIMEOUT_S", 1.0)
    path = tmp_path / "ipc-stall.sock"
    listener = _listener(path)
    thread, box = _serve_steps(listener, 2, handler=_echo_handler)

    stalled = _connect(path)
    stalled.sendall(json.dumps({"command": "show", "addr": LEAF, "marker": "A"}).encode("utf-8"))
    # NO shutdown(SHUT_WR), NO close: the peer stays connected with its write half open.

    served = _connect(path)
    served.sendall(json.dumps({"command": "show", "addr": LEAF, "marker": "B"}).encode("utf-8"))
    served.shutdown(socket.SHUT_WR)
    try:
        raw = _read_to_eof(served)
    except (TimeoutError, socket.timeout):
        raw = b""
    finally:
        served.close()
        stalled.close()
        thread.join(timeout=HARD_TIMEOUT_S)
        listener.close()

    assert raw, (
        f"the second client got NO response within {HARD_TIMEOUT_S}s: the stalled first client "
        "starved the single-threaded accept loop (the 2026-07-31 control-plane outage)"
    )
    response = json.loads(raw.decode("utf-8"))
    assert response["marker"] == "B", f"the second client was served the wrong response: {response}"
    assert box.get("escaped") is None, f"a fault escaped serve_one: {box.get('escaped')!r}"
    assert not thread.is_alive(), "both bounded serve_one steps must return"


def test_timed_out_connection_is_a_structured_abort_not_a_crash(runtime, tmp_path, monkeypatch):
    """The stalled connection aborts the harnessd way: a structured ``{ok: false, errors}`` response
    on the wire + an ``ipc_request_failed`` row at stage ``read`` (the RR-1 convention), never a
    traceback out of the daemon's IPC thread."""
    monkeypatch.setattr(ipc, "REQUEST_IDLE_TIMEOUT_S", 0.5)
    path = tmp_path / "ipc-abort.sock"
    listener = _listener(path)
    thread, box = _serve_steps(listener, 1, handler=_echo_handler)

    stalled = _connect(path)
    stalled.sendall(json.dumps({"command": "show", "addr": LEAF}).encode("utf-8"))
    try:
        raw = _read_to_eof(stalled)
    except (TimeoutError, socket.timeout):
        raw = b""
    finally:
        stalled.close()
        thread.join(timeout=HARD_TIMEOUT_S)
        listener.close()

    assert box.get("escaped") is None, f"a fault escaped serve_one: {box.get('escaped')!r}"
    assert not thread.is_alive(), "serve_one must RETURN on a timed-out read (a wedged loop is the bug)"
    assert raw, "the timed-out connection must receive the structured abort before it is closed"
    response = json.loads(raw.decode("utf-8"))
    assert response["ok"] is False
    assert response["errors"], "the abort must name a reason"
    rows = _ipc_failure_rows("read")
    assert len(rows) == 1, f"expected one ipc_request_failed row at stage 'read', got {_events()}"
    assert "timed out" in rows[0]["binding_delta"]["error"].lower()


def test_a_slow_but_legitimate_client_under_the_bound_is_served(runtime, tmp_path, monkeypatch):
    """The bound is IDLE time, not a rate limit: a client that dribbles its request in chunks, each
    inside the bound, is served in full. The bound must never abort honest slow traffic."""
    monkeypatch.setattr(ipc, "REQUEST_IDLE_TIMEOUT_S", 2.0)
    path = tmp_path / "ipc-slow.sock"
    listener = _listener(path)
    thread, box = _serve_steps(listener, 1, handler=_echo_handler)

    payload = json.dumps({"command": "show", "addr": LEAF, "marker": "slow"}).encode("utf-8")
    split = len(payload) // 2
    client = _connect(path)
    client.sendall(payload[:split])
    threading.Event().wait(1.0)  # under the 2.0s bound, twice
    client.sendall(payload[split:])
    threading.Event().wait(1.0)
    client.shutdown(socket.SHUT_WR)
    try:
        raw = _read_to_eof(client)
    finally:
        client.close()
        thread.join(timeout=HARD_TIMEOUT_S)
        listener.close()

    response = json.loads(raw.decode("utf-8"))
    assert response == {"ok": True, "command": "show", "marker": "slow"}
    assert box.get("escaped") is None, f"a fault escaped serve_one: {box.get('escaped')!r}"
    assert not _ipc_failure_rows(), "a legitimate slow client must not journal a control-plane fault"


def test_ordinary_request_response_is_unchanged(runtime, tmp_path):
    """The real dispatcher, the real EOF framing, the default bound: an ordinary round-trip still
    returns the node's ledger slice and journals nothing."""
    _seed_leaf()
    path = tmp_path / "ipc-plain.sock"
    listener = _listener(path)
    thread, box = _serve_steps(listener, 1)

    client = _connect(path)
    client.sendall(json.dumps({"command": "show", "addr": LEAF}).encode("utf-8"))
    client.shutdown(socket.SHUT_WR)
    try:
        raw = _read_to_eof(client)
    finally:
        client.close()
        thread.join(timeout=HARD_TIMEOUT_S)
        listener.close()

    response = json.loads(raw.decode("utf-8"))
    assert response["ok"] is True
    assert response["command"] == "show"
    assert response["binding"]["node_address"] == LEAF
    assert box.get("escaped") is None, f"a fault escaped serve_one: {box.get('escaped')!r}"
    assert not _ipc_failure_rows(), "a clean round-trip must not journal a control-plane fault"


# ===========================================================================
# Close-on-exec — a daemon-spawned subprocess can never inherit an IPC fd.
# ===========================================================================

class _RecordingConnection:
    """A real socket wrapped so the test can read the fd's state at the moment serve_one closes it."""

    def __init__(self, sock):
        self._sock = sock
        self.observed: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.observed["inheritable"] = self._sock.get_inheritable()
        self.observed["timeout"] = self._sock.gettimeout()
        self._sock.close()
        return None

    def settimeout(self, value):
        self._sock.settimeout(value)

    def set_inheritable(self, flag):
        self._sock.set_inheritable(flag)

    def recv(self, size):
        return self._sock.recv(size)

    def sendall(self, payload):
        return self._sock.sendall(payload)


class _OneRecordedConnectionListener:
    def __init__(self, connection):
        self._connection = connection

    def accept(self):
        return self._connection, None


def test_the_listener_is_close_on_exec(runtime, tmp_path):
    """A daemon-spawned subprocess must not inherit the control socket: a leaked listener fd keeps
    the socket alive across a daemon exit and lets a child answer (or block) the control plane."""
    listener = daemon.make_ipc_listener(tmp_path)
    try:
        assert listener.get_inheritable() is False
    finally:
        listener.close()


def test_the_accepted_connection_is_close_on_exec_and_bounded(runtime):
    """Same for each accepted connection — and it carries the read bound. A leaked daemon-side
    connection fd holds the connection half-open from the CLIENT's side: the daemon closes, an
    unrelated child still references the socket, and the client hangs waiting for a response nobody
    will write. It cannot suppress EOF for the daemon's own read (that is gated by client-side
    references) — this is hygiene, not the 2026-07-31 mechanism."""
    server_end, client_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    server_end.set_inheritable(True)  # the state serve_one must clear
    connection = _RecordingConnection(server_end)

    client_end.sendall(json.dumps({"command": "show", "addr": LEAF}).encode("utf-8"))
    client_end.shutdown(socket.SHUT_WR)
    ipc.serve_one(_OneRecordedConnectionListener(connection), handler=_echo_handler)
    client_end.close()

    assert connection.observed["inheritable"] is False, (
        "the accepted IPC connection must be close-on-exec"
    )
    assert connection.observed["timeout"] == ipc.REQUEST_IDLE_TIMEOUT_S, (
        "the accepted IPC connection must carry the bounded read timeout"
    )
