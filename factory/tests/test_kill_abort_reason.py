"""F2r (ipc-2) — _handle_kill must ROUTE collapse()'s TransitionResult and report the SPECIFIC abort
reason (CAS miss / fencing / illegal-edge / no-such-node), never the generic post-read verdict.

F2b (412244c) made chokepoint.collapse() RETURN the TransitionResult; the watchdog routes it. But the
kill IPC handler still DISCARDED that return and post-read the ledger instead: any binding whose state
happened to sit in ("done","failed","dead") reported ok=True — even when the transition itself ABORTED
(killing an already-terminal node is an illegal edge, yet the post-read called it success), and a
fenced/CAS abort surfaced only as the generic "kill did not collapse <addr> to a terminal state",
throwing away executor's structured reason. Fix: capture the result, shape it through
_transition_response, and name the absence explicitly when collapse returns None.
"""

import copy

import pytest

import harnessd.fencing as fencing
import harnessd.ipc as ipc
import harnessd.ledger as ledger


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


ADDR = "proj/widget/task#exec"


def _seed(runtime, addr=ADDR, state="running"):
    token = fencing.mint_owner_token(addr, "sa", "uuid", 2)
    rec = {"node_address": addr, "parent_address": "proj/widget#exec", "level": "L5", "subagent_id": "sa",
           "session_uuid": "uuid", "state": state, "generation": 5, "lease_epoch": 2,
           "owner_token": token, "last_applied_seq": 0, "liveness_state": "working",
           "tmux_target": "harness:" + addr}
    ledger.write_binding({addr: copy.deepcopy(rec)}, _lock_held=True)
    return token


def test_kill_abort_reports_the_fencing_reason(runtime):
    """A fenced kill (stale/wrong owner_token) must surface executor's SPECIFIC structured reason
    ('fencing abort (owner_token)'), not the generic terminal-state message.

    Mutant killed: revert _handle_kill to the post-read -> the response carries only 'kill did not
    collapse ...' (no 'fencing abort' substring) -> caught."""
    _seed(runtime)
    resp = ipc.handle_request({
        "command": "kill",
        "addr": ADDR,
        "terminal_signal": "FAILED",
        "expected_owner_token": "STALE-WRONG-TOKEN",
    })
    assert resp["ok"] is False, "a fenced kill must report the abort (never ok)"
    assert any("fencing abort (owner_token)" in e for e in resp["errors"]), (
        f"the kill response must carry executor's SPECIFIC abort reason, got: {resp['errors']}"
    )
    assert ledger.read_binding(ADDR)["state"] == "running", "a fenced kill must change NOTHING"
    assert resp["binding"]["state"] == "running", (
        "TransitionResult.binding is the live binding, unchanged on abort"
    )


def test_kill_of_an_already_terminal_node_is_not_a_phantom_success(runtime):
    """Killing a node already in a terminal state ABORTS on the illegal edge (done has no outgoing
    edges) — it must report ok=False with the 'illegal transition' reason.

    THE strongest mutant-killer: the pre-fix post-read returned ok=True here (state 'done' is in the
    terminal set) even though the transition aborted — a phantom success."""
    _seed(runtime, state="done")
    resp = ipc.handle_request({"command": "kill", "addr": ADDR, "terminal_signal": "FAILED"})
    assert resp["ok"] is False, (
        "killing an already-terminal node is an ABORTED transition — reporting ok=True is the "
        "phantom success the post-read produced"
    )
    assert any("illegal transition" in e for e in resp["errors"]), (
        f"the abort reason must name the illegal edge, got: {resp['errors']}"
    )


def test_kill_of_an_absent_node_names_the_absence(runtime):
    """collapse() returns None when the address has NO binding — the response must NAME the absence
    ('no such node' + the addr), never the generic terminal-state message.

    Mutant killed: treat collapse-returns-None as success (or fall through to the post-read, which
    reports the generic message) -> caught."""
    ghost = "proj/ghost#exec"
    resp = ipc.handle_request({"command": "kill", "addr": ghost, "terminal_signal": "FAILED"})
    assert resp["ok"] is False
    assert any("no such node" in e and ghost in e for e in resp["errors"]), (
        f"the error must name the absent node, got: {resp['errors']}"
    )
    assert resp["binding"] is None


def test_kill_success_routes_a_clean_result(runtime):
    """Regression guard: routing the result must not break the happy path. A correctly fenced kill
    commits through the REAL executor (running -> failed, generation bumped) and reports a clean
    routed result (ok / empty errors / post-state binding / the terminal_signal echo)."""
    token = _seed(runtime)
    resp = ipc.handle_request({
        "command": "kill",
        "addr": ADDR,
        "terminal_signal": "FAILED",
        "expected_owner_token": token,
    })
    assert resp["ok"] is True
    assert resp["errors"] == []
    assert resp["terminal_signal"] == "FAILED"
    assert resp["binding"]["state"] == "failed"
    after = ledger.read_binding(ADDR)
    assert after["state"] == "failed", "the mutation went through the REAL executor"
    assert after["generation"] == 6, "the executor bumps the per-node generation on commit"
