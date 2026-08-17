"""LR-25/LR-26 - ESCALATED artifacts are journaled once and relayed to the parent.

The forward escalation path is now harness-relayed: when a child commits a new
``signal_ESCALATED`` artifact, the harness appends a ``child_escalated`` pointer line to the
parent's inbox. That line is what the daemon's inbox-tail wake sees; the agent-authored nudge is
only an optional fast-path.
"""

from __future__ import annotations

import copy
import json

import pytest

import harnessd.addressing as addressing
import harnessd.clock as clock
import harnessd.detector_signals as detector_signals
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.watchdog as watchdog
from harnessd.detector import Liveness


PARENT = "proj#exec"
CHILD = "proj/child#exec"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(detector_signals, "_size_cache", {}, raising=False)
    return tmp_path


def _binding(
    node_address=CHILD,
    *,
    parent_address=PARENT,
    state="running",
    generation=1,
    lease_epoch=1,
    subagent_id="subagent-child",
    session_uuid="sess-child",
    level="L5",
):
    token = fencing.mint_owner_token(node_address, subagent_id, session_uuid, lease_epoch)
    return {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": level,
        "subagent_id": subagent_id,
        "session_uuid": session_uuid,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "working",
        "last_progress_at": None,
        "last_inbox_acked_offset": 0,
        "stale_check_count": 0,
        "stale_grace_checks": 2,
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": "/dev/null",
    }


def _seed(*bindings):
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _read(node_address=CHILD):
    return ledger.read_binding(node_address)


def _node(binding):
    return {
        "node_address": binding["node_address"],
        "transcript_path": binding.get("transcript_path"),
        "tmux_target": binding.get("tmux_target", "harness:t"),
    }


def _write_escalated(runtime, binding, *, question="need a decision", ts=None):
    path = addressing.signal_path(binding["node_address"], runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "signal": "ESCALATED",
        "ts": ts or clock.now_utc(),
        "owner_token": binding["owner_token"],
        "evidence": {"notes": question},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _tick():
    live = _read()
    return watchdog.check_leaf(_node(live), live, now=clock.now_utc())


def _parent_inbox_lines(runtime):
    inbox = addressing.inbox_path(PARENT, runtime)
    if not inbox.exists():
        return []
    return [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines()]


def _escalation_rows():
    return [
        row for row in ledger.load_wal()
        if row.get("node_address") == CHILD and row.get("event") == "signal_ESCALATED"
    ]


def test_child_escalated_relay_appends_parent_inbox_line(runtime):
    parent = _binding(
        PARENT, parent_address=None, subagent_id="subagent-parent", session_uuid="sess-parent"
    )
    child = _binding()
    _seed(parent, child)
    _write_escalated(runtime, child, question="should I split this into two nodes?")

    action = _tick()

    assert getattr(action, "kind", None) == watchdog.NOOP
    lines = _parent_inbox_lines(runtime)
    relay = [line for line in lines if line.get("type") == "message"]
    assert len(relay) == 1
    assert relay[0]["sender"] == CHILD
    assert relay[0]["needs_answer"] is True
    assert _read()["state"] == "running"


def test_child_escalated_relay_line_points_at_artifact_directory(runtime):
    parent = _binding(
        PARENT, parent_address=None, subagent_id="subagent-parent", session_uuid="sess-parent"
    )
    child = _binding()
    _seed(parent, child)
    _write_escalated(runtime, child, question="gate request for L4")

    _tick()

    relay = [
        line for line in _parent_inbox_lines(runtime) if line.get("type") == "message"
    ][0]
    assert relay["artifact"] == ".signal.exec.json"
    record = next(iter(_read()["messages"].values()))
    assert record["metadata"]["kind"] == "legacy_escalation"
    assert record["question_state"] == "open"


def test_same_escalated_artifact_does_not_duplicate_relay_even_with_future_ts(runtime):
    parent = _binding(
        PARENT, parent_address=None, subagent_id="subagent-parent", session_uuid="sess-parent"
    )
    child = _binding()
    _seed(parent, child)
    _write_escalated(runtime, child, ts="2999-01-01T00:00:00+00:00")

    _tick()
    first_identity = _read()["signal_artifact_seen_at"]
    _tick()

    assert len(_escalation_rows()) == 1
    assert len([
        line for line in _parent_inbox_lines(runtime) if line.get("type") == "message"
    ]) == 1
    assert _read()["signal_artifact_seen_at"] == first_identity


def test_fresh_escalation_after_answer_rejournals_and_relays_again_ignoring_agent_ts(runtime):
    parent = _binding(
        PARENT, parent_address=None, subagent_id="subagent-parent", session_uuid="sess-parent"
    )
    child = _binding()
    _seed(parent, child)
    fixed_ts = "2026-06-11T00:00:00+00:00"
    _write_escalated(runtime, child, question="q1", ts=fixed_ts)
    _tick()
    assert executor.post_answer(CHILD, answer="answer q1").ok

    _write_escalated(runtime, _read(), question="q2", ts=fixed_ts)
    _tick()

    assert len(_escalation_rows()) == 2
    relay = [line for line in _parent_inbox_lines(runtime) if line.get("type") == "message"]
    assert len(relay) == 2
    assert relay[0]["message_id"] != relay[1]["message_id"]


def test_child_escalated_relay_triggers_parent_inbox_wake(runtime):
    parent = _binding(
        PARENT, parent_address=None, subagent_id="subagent-parent", session_uuid="sess-parent"
    )
    child = _binding()
    _seed(parent, child)
    _write_escalated(runtime, child)

    _tick()

    parent_live = _read(PARENT)
    assert watchdog.inbox_has_unacked(_node(parent_live), parent_live) is True


def test_child_escalated_relay_recovery_replays_lost_pointer_once(runtime):
    """A durable ESCALATED stamp repairs a lost parent inbox pointer on the daemon sweep."""
    parent = _binding(
        PARENT, parent_address=None, subagent_id="subagent-parent", session_uuid="sess-parent"
    )
    child = _binding()
    _seed(parent, child)
    _write_escalated(runtime, child, question="need parent decision")
    _tick()

    inbox = addressing.inbox_path(PARENT, runtime)
    inbox.unlink()

    from harnessd import daemon

    daemon._recover_terminal_notifications_best_effort()
    daemon._recover_message_state_best_effort()
    relay = [line for line in _parent_inbox_lines(runtime) if line.get("type") == "message"]
    assert len(relay) == 1
    assert relay[0].get("sender") == CHILD

    daemon._recover_terminal_notifications_best_effort()
    daemon._recover_message_state_best_effort()
    assert len([
        line for line in _parent_inbox_lines(runtime) if line.get("type") == "message"
    ]) == 1


def test_answered_same_artifact_stops_holding_without_new_relay(runtime):
    parent = _binding(
        PARENT, parent_address=None, subagent_id="subagent-parent", session_uuid="sess-parent"
    )
    child = _binding()
    _seed(parent, child)
    _write_escalated(runtime, child)
    _tick()
    assert executor.post_answer(CHILD, answer="continue").ok

    watchdog.set_liveness(lambda _addr: Liveness(state="working", last_progress_at=clock.now_utc()))
    try:
        action = _tick()
    finally:
        watchdog.set_liveness(None)

    assert (action.detail or {}).get("reason") == "not_idle"
    assert len([
        line for line in _parent_inbox_lines(runtime) if line.get("type") == "message"
    ]) == 1
