"""F2b — collapse() must ROUTE its transition result, not silently succeed (review chokepoint-2, watchdog-2).

chokepoint.collapse() called executor.transition() and `return None` UNCONDITIONALLY — so a FAILED
terminal transition (a CAS miss / fencing rejection) was reported as success to every caller. The
watchdog trusts collapse(), so a fenced/illegal terminal transition was still reported as a clean
COLLAPSE. Fix: collapse() returns the TransitionResult; check_leaf checks it and does NOT report a false
COLLAPSE when the transition aborted.
"""

import copy

import pytest

import harnessd.clock as clock
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.spawn.chokepoint as chokepoint
import harnessd.watchdog as watchdog


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


ADDR = "proj/widget/task#exec"


def _seed_running(runtime, addr=ADDR):
    token = fencing.mint_owner_token(addr, "sa", "uuid", 2)
    rec = {"node_address": addr, "parent_address": "proj/widget#exec", "level": "L5", "subagent_id": "sa",
           "session_uuid": "uuid", "state": "running", "generation": 5, "lease_epoch": 2,
           "owner_token": token, "last_applied_seq": 0, "liveness_state": "working",
           "tmux_target": "harness:" + addr}
    ledger.write_binding({addr: copy.deepcopy(rec)}, _lock_held=True)
    return token


def test_collapse_returns_a_result_and_succeeds_with_the_right_token(runtime):
    """collapse() returns the TransitionResult (not None) and the node is done on success."""
    token = _seed_running(runtime)
    result = chokepoint.collapse(ADDR, "DONE", expected_owner_token=token)
    assert result is not None and getattr(result, "ok", False) is True
    assert ledger.read_binding(ADDR)["state"] == "done"


def test_collapse_does_not_silently_succeed_on_a_fenced_abort(runtime):
    """collapse() with a STALE/wrong expected_owner_token must NOT silently succeed — it returns a not-ok
    result and the node state is UNCHANGED (the fencing CAS aborts). The chokepoint-2 swallow would have
    returned None and looked like success."""
    _seed_running(runtime)
    result = chokepoint.collapse(ADDR, "DONE", expected_owner_token="STALE-WRONG-TOKEN")
    assert result is None or getattr(result, "ok", True) is False, (
        "a fenced collapse must report failure (not silently succeed)"
    )
    assert ledger.read_binding(ADDR)["state"] == "running", "a fenced collapse must NOT change the state"


def test_check_leaf_does_not_report_a_false_COLLAPSE_when_the_transition_aborts(runtime, monkeypatch):
    """watchdog-2: if collapse() reports failure (a fenced/illegal terminal transition), check_leaf must
    NOT return a COLLAPSE action (which would tell the daemon the node is gone when it is not). It
    downgrades to a non-COLLAPSE action so the next tick retries against the still-present signal."""
    import harnessd.addressing as addressing
    token = _seed_running(runtime)
    # write a fenced DONE signal so check_leaf's STEP A fires
    p = addressing.signal_path(ADDR, runtime); p.parent.mkdir(parents=True, exist_ok=True)
    import json
    p.write_text(json.dumps({"signal": "DONE", "ts": clock.now_utc(), "owner_token": token, "evidence": {}}))

    # force collapse to REPORT FAILURE (simulate a CAS race that aborts the terminal transition)
    from harnessd.spawn.adapters.base import SpawnResult  # any object with ok=False works
    class _Failed:
        ok = False
        errors = ["simulated CAS abort"]
    monkeypatch.setattr(chokepoint, "collapse", lambda *a, **k: _Failed())

    node = {"node_address": ADDR, "tmux_target": "harness:" + ADDR, "transcript_path": None}
    action = watchdog.check_leaf(node, ledger.read_binding(ADDR), now=clock.now_utc())

    assert action.kind != "COLLAPSE", (
        f"a failed collapse must NOT be reported as a COLLAPSE (got {action.kind}); the node did not "
        "actually collapse"
    )
