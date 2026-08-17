"""F2r (harnessctl-1, harnessctl-2) — the CLI fails STRUCTURED, never a traceback.

Two unguarded edges in harnessctl.main:
  * harnessctl-1: the client-side input-file reads in _build_request (spawn ``--brief``, and after F16
    also answer ``--file``) ran OUTSIDE any guard — a missing/unreadable file tracebacked with
    FileNotFoundError. Fix: a guard around _build_request prints a structured JSON error NAMING the
    actual input file and returns exit 2 (a command-level abort — the daemon was never contacted; the
    ledger is untouched, DAEMON §4.3 "CLIs are clients, not writers").
  * harnessctl-2: a garbled (non-JSON) daemon response propagated json.JSONDecodeError out of main
    (JSONDecodeError is a ValueError, NOT caught by the transport's OSError-family arm). Fix: a
    dedicated except arm prints a structured "garbled response from daemon" error and returns exit 3
    (transport-class — the daemon did not speak the protocol).

Socket discipline: the garbled-response test stands up a BOUNDED fake daemon on a REAL AF_UNIX socket
(accepts EXACTLY ONE connection, reads to EOF, answers non-JSON bytes, exits; joined + unlinked in
teardown) — NEVER serve-forever in a test path.
"""

from __future__ import annotations

import copy
import json
import socket
import threading
from pathlib import Path

import pytest

import harnessd.fencing as fencing
import harnessd.harnessctl as harnessctl
import harnessd.ledger as ledger


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


NODE = "proj/widget#exec"


def _seed(runtime, addr=NODE, state="running"):
    token = fencing.mint_owner_token(addr, "sa", "uuid", 2)
    rec = {"node_address": addr, "parent_address": "proj#exec", "level": "L3", "subagent_id": "sa",
           "session_uuid": "uuid", "state": state, "generation": 4, "lease_epoch": 2,
           "owner_token": token, "last_applied_seq": 0, "liveness_state": "working"}
    whole = ledger.all_nodes()
    whole[addr] = copy.deepcopy(rec)
    ledger.write_binding(whole, _lock_held=True)
    return token


def _last_json_line(out: str):
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    return None


# ---------------------------------------------------------------------------
# harnessctl-1 — the client-side input-file reads (spawn --brief / answer --file).
# ---------------------------------------------------------------------------


def _missing_brief_argv(tmp_path):
    return (["spawn", "proj/child#exec", "--parent", "proj#exec",
             "--brief", str(tmp_path / "no-such-brief.md")], "no-such-brief.md", "--brief")


def _dir_brief_argv(tmp_path):
    briefdir = tmp_path / "brief-is-a-dir"
    briefdir.mkdir(exist_ok=True)
    return (["spawn", "proj/child#exec", "--parent", "proj#exec",
             "--brief", str(briefdir)], "brief-is-a-dir", "--brief")


def _missing_answer_file_argv(tmp_path):
    # The C2 adaptation: F16 added a SECOND client-side file read (answer --file) — the guard must
    # name THAT flag+file, not hard-code its message to --brief.
    return (["answer", NODE, "--file", str(tmp_path / "no-such-answer.md")],
            "no-such-answer.md", "--file")


@pytest.mark.parametrize("make_argv", [
    _missing_brief_argv,        # FileNotFoundError (spawn --brief, the original finding)
    _dir_brief_argv,            # IsADirectoryError -> the OSError family path
    _missing_answer_file_argv,  # FileNotFoundError (answer --file, the F16-added read; C2)
])
def test_unreadable_input_file_is_a_structured_error_not_a_traceback(
        runtime, tmp_path, capsys, make_argv):
    """An unreadable client-side input file (--brief / --file) must make main RETURN exit 2 with a
    structured JSON error NAMING the file — never an uncaught traceback, never a daemon-unreachable
    mislabel, never a ledger write.

    Mutants killed: (a) drop the guard -> FileNotFoundError/IsADirectoryError propagates -> caught;
    (b) fold the guard into the transport except -> 'daemon unreachable' mislabel + exit 3 -> caught;
    (c) hard-code the message to one flag -> the other command's case asserts the WRONG flag name
    (the filename alone is too weak: the raw exception text carries the path either way) -> caught."""
    argv, filename, flag = make_argv(tmp_path)
    before = ledger.all_nodes()

    # main must RETURN (an uncaught exception fails the test = the missing-guard mutant).
    code = harnessctl.main(argv, socket_path=str(tmp_path / "no-daemon.sock"))

    assert code == 2, (
        "an unreadable input file is a COMMAND-level abort (exit 2): the daemon was never contacted, "
        f"so 3 'transport failure' is wrong — got {code}"
    )
    payload = _last_json_line(capsys.readouterr().out)
    assert payload is not None, "the failure must print a structured JSON error line"
    assert payload.get("ok") is False
    flat = json.dumps(payload)
    assert filename in flat, f"the error must NAME the unreadable input file, got: {flat}"
    assert flag in flat, (
        f"the error must name the ACTUAL input flag ({flag}) — the C2 hard-code-to---brief mutant "
        f"reports the wrong flag for the answer --file read, got: {flat}"
    )
    assert "daemon unreachable" not in flat, (
        "a client-side input error must NOT be mislabeled as a transport failure"
    )
    assert ledger.all_nodes() == before, "the CLI is not a writer (DAEMON §4.3) — ledger untouched"


# ---------------------------------------------------------------------------
# harnessctl-2 — a garbled (non-JSON) daemon response.
# ---------------------------------------------------------------------------


class GarbledDaemon:
    """A real AF_UNIX socket whose handler thread accepts EXACTLY ONE connection, reads the request
    to EOF, answers with NON-JSON bytes, and exits. BOUNDED — never serve-forever; joined + the
    socket file unlinked in close()."""

    def __init__(self, socket_path: Path):
        self.socket_path = Path(socket_path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.socket_path))
        self._listener.listen(1)
        self._thread = threading.Thread(target=self._run, name="garbled-daemon", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        try:
            conn, _addr = self._listener.accept()  # EXACTLY ONE accept — bounded by construction
        except OSError:
            return
        with conn:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
            conn.sendall(b"ITS-NOT-JSON{{{")

    def close(self):
        try:
            self._listener.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


def test_garbled_daemon_response_is_a_structured_error_not_a_traceback(runtime, tmp_path, capsys):
    """A daemon response the client cannot parse must make main RETURN exit 3 (transport-class: the
    daemon did not speak the protocol) with a structured 'garbled' JSON error — never a
    json.JSONDecodeError traceback.

    Mutant killed: drop the JSONDecodeError arm -> json.loads raises out of main (JSONDecodeError is
    a ValueError, NOT caught by the OSError-family transport arm) -> caught."""
    _seed(runtime)
    before = ledger.all_nodes()
    daemon = GarbledDaemon(tmp_path / "garbled.sock").start()
    try:
        # main must RETURN (an uncaught JSONDecodeError fails the test = the missing-arm mutant).
        code = harnessctl.main(["show", NODE], socket_path=str(daemon.socket_path))
    finally:
        daemon.close()

    assert code == 3, f"a garbled response is a TRANSPORT-class failure (exit 3) — got {code}"
    payload = _last_json_line(capsys.readouterr().out)
    assert payload is not None and payload.get("ok") is False
    assert any("garbled" in e for e in payload.get("errors", [])), (
        f"the error must say the response was garbled, got: {payload}"
    )
    assert ledger.all_nodes() == before, "a read round-trip never touches the ledger"
