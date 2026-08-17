"""T65 - cascade-wide nonterminal coordination handoffs."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

import harnessd.addressing as addressing
import harnessd.daemon as daemon
import harnessd.fencing as fencing
import harnessd.harnessctl as harnessctl
import harnessd.ipc as ipc
import harnessd.ledger as ledger
from harnessd.spawn import chokepoint


L2 = "proj/logview#exec"
L3 = "proj/logview/parser#exec"
L3_REVIEW = "proj/logview/parser#review"
SESSION = "sess-coordination-0001"
SUBAGENT = "subagent-coordination"


@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _binding(
    node_address: str,
    *,
    parent_address=None,
    level="L3",
    state="running",
    lease_epoch=1,
    extra=None,
):
    token = fencing.mint_owner_token(node_address, SUBAGENT, SESSION, lease_epoch)
    rec = {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": level,
        "subagent_id": SUBAGENT,
        "session_uuid": SESSION,
        "state": state,
        "generation": 0,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "spec_pointer": "brief.md",
        "frozen_acceptance_ref": "acceptance.md",
        "last_inbox_acked_offset": 0,
        "stale_check_count": 0,
        "stale_grace_checks": 2,
        "recovery_attempts": 0,
        "paused_at": None,
        "terminal_signal": None,
        "transcript_path": None,
        "liveness_state": "idle",
        "last_progress_at": _now_iso(),
    }
    if extra:
        rec.update(extra)
    return rec, token


def _seed(bindings):
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _jsonl(path):
    if not path.is_file():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines()]


def _write_handoff(runtime, *, node_address=L3, handoff_id="interface-gap-1", marker_extra=None):
    node_dir = addressing.node_dir(node_address, runtime)
    handoff_dir = node_dir / "handoffs"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    artifact = handoff_dir / f"{handoff_id}.md"
    artifact.write_text("# Interface Gap\n\nParser output shape needs parent decision.\n", encoding="utf-8")
    marker = {
        "type": "coordination_handoff",
        "handoff_id": handoff_id,
        "handoff_kind": "plan_gap",
        "artifact": f"handoffs/{handoff_id}.md",
        "summary": "Parser output shape needs L2 direction.",
        "response_required": True,
    }
    if marker_extra:
        marker.update(marker_extra)
    marker_path = handoff_dir / f"{handoff_id}.json"
    marker_path.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
    return marker_path, artifact


def _wal_events(node):
    return [row["event"] for row in ledger.load_wal() if row.get("node_address") == node]


def test_coordination_handoff_wakes_parent_without_terminalizing_child(runtime):
    l2, _ = _binding(L2, parent_address=None, level="L2")
    l3, token = _binding(L3, parent_address=L2, level="L3")
    _seed([l2, l3])
    marker, artifact = _write_handoff(runtime)

    result = chokepoint.submit_coordination_handoff(L3, marker_path=marker, expected_owner_token=token)

    assert result.ok
    live = ledger.read_binding(L3)
    assert live["state"] == "running"
    record = live["messages"]["interface-gap-1"]
    assert record["question_state"] == "open"
    assert record["artifact"] == "handoffs/interface-gap-1.md"
    assert record["metadata"]["kind"] == "plan_gap"
    assert "coordination_handoffs" not in live
    assert _wal_events(L3) == ["coordination_handoff_submitted"]
    rows = _jsonl(addressing.inbox_path(L2, runtime))
    assert len(rows) == 1
    assert rows[0]["type"] == "message"
    assert rows[0]["sender"] == L3
    assert rows[0]["message_id"] == "interface-gap-1"


@pytest.mark.parametrize(
    ("parent", "child", "parent_level", "child_level"),
    [
        (L2, L3, "L2", "L3"),
        ("proj/logview/parser#exec", "proj/logview/parser/ingest#exec", "L3", "L4"),
        (
            "proj/logview/parser/ingest#exec",
            "proj/logview/parser/ingest/path-reader#exec",
            "L4",
            "L5",
        ),
    ],
)
def test_coordination_handoff_applies_to_every_direct_execution_edge(
    runtime,
    parent,
    child,
    parent_level,
    child_level,
):
    parent_binding, _ = _binding(parent, parent_address=None, level=parent_level)
    child_binding, token = _binding(child, parent_address=parent, level=child_level)
    _seed([parent_binding, child_binding])
    marker, artifact = _write_handoff(runtime, node_address=child, handoff_id="edge-gap-1")

    result = chokepoint.submit_coordination_handoff(
        child,
        marker_path=marker,
        expected_owner_token=token,
    )

    assert result.ok
    live = ledger.read_binding(child)
    assert live["state"] == "running"
    record = live["messages"]["edge-gap-1"]
    assert record["question_state"] == "open"
    assert record["artifact"].endswith("edge-gap-1.md")
    rows = _jsonl(addressing.inbox_path(parent, runtime))
    assert len(rows) == 1
    assert rows[0]["type"] == "message"
    assert rows[0]["sender"] == child
    assert rows[0]["message_id"] == "edge-gap-1"


def test_daemon_marker_sweep_and_lost_wake_recovery_are_idempotent(runtime):
    l2, _ = _binding(L2, parent_address=None, level="L2")
    l3, _token = _binding(L3, parent_address=L2, level="L3")
    _seed([l2, l3])
    _write_handoff(runtime)

    daemon._submit_coordination_handoff_markers_best_effort()

    live = ledger.read_binding(L3)
    assert live["messages"]["interface-gap-1"]["question_state"] == "open"
    inbox = addressing.inbox_path(L2, runtime)
    assert len(_jsonl(inbox)) == 1

    inbox.unlink()
    daemon._recover_message_state_best_effort()
    daemon._recover_message_state_best_effort()

    rows = _jsonl(inbox)
    assert len(rows) == 1
    assert rows[0]["type"] == "message"
    assert rows[0]["message_id"] == "interface-gap-1"


def test_coordination_decision_wakes_child_and_is_idempotent(runtime):
    l2, _ = _binding(L2, parent_address=None, level="L2")
    l3, token = _binding(L3, parent_address=L2, level="L3")
    _seed([l2, l3])
    marker, _artifact = _write_handoff(runtime)
    assert chokepoint.submit_coordination_handoff(L3, marker_path=marker, expected_owner_token=token).ok
    request = {
        "command": "coordination-decision",
        "addr": L3,
        "handoff_id": "interface-gap-1",
        "decision": "guidance",
        "decision_content": "# Guidance\n\nKeep the existing parser port and add an adapter.",
    }

    first = ipc.handle_request(request)
    second = ipc.handle_request(request)

    assert first["ok"] is True
    assert second["ok"] is True
    live = ledger.read_binding(L3)
    record = live["messages"]["interface-gap-1"]
    assert record["question_state"] == "answered"
    assert record["answered_by"]["answerer_address"] == L2
    parent_messages = ledger.read_binding(L2)["messages"]
    answers = [row for row in parent_messages.values() if row.get("answers_question")]
    assert len(answers) == 1
    assert answers[0]["metadata"]["decision"] == "guidance"
    rows = _jsonl(addressing.inbox_path(L3, runtime))
    assert len([row for row in rows if row.get("type") == "message"]) == 1
    assert rows[0]["answers_question"]["message_id"] == "interface-gap-1"


def test_coordination_note_wakes_child_without_terminalizing_it(runtime):
    l2, _ = _binding(L2, parent_address=None, level="L2")
    l3, _token = _binding(L3, parent_address=L2, level="L3")
    _seed([l2, l3])
    request = {
        "command": "coordination-note",
        "addr": L3,
        "handoff_id": "l2-guidance-1",
        "handoff_kind": "guidance_request",
        "note_content": "# Guidance\n\nUse the stable parser port and defer the optional column.",
        "summary": "L2 guidance for parser boundary.",
    }

    first = ipc.handle_request(request)
    second = ipc.handle_request(request)

    assert first["ok"] is True
    assert second["ok"] is True
    live = ledger.read_binding(L3)
    assert live["state"] == "running"
    assert "coordination_handoffs" not in live
    record = ledger.read_binding(L2)["messages"]["l2-guidance-1"]
    assert record["direction"] == "down"
    assert record["metadata"]["kind"] == "guidance_request"
    rows = _jsonl(addressing.inbox_path(L3, runtime))
    assert len([row for row in rows if row.get("type") == "message"]) == 1
    assert rows[0]["message_id"] == "l2-guidance-1"
    assert rows[0]["artifact"] == record["artifact"]


def test_coordination_child_pointer_recovery_replays_decisions_and_notices(runtime):
    l2, _ = _binding(L2, parent_address=None, level="L2")
    l3, token = _binding(L3, parent_address=L2, level="L3")
    _seed([l2, l3])
    marker, _artifact = _write_handoff(runtime)
    assert chokepoint.submit_coordination_handoff(L3, marker_path=marker, expected_owner_token=token).ok
    assert ipc.handle_request(
        {
            "command": "coordination-decision",
            "addr": L3,
            "handoff_id": "interface-gap-1",
            "decision": "guidance",
            "decision_content": "# Guidance\n\nKeep the existing parser port.",
        }
    )["ok"]
    assert ipc.handle_request(
        {
            "command": "coordination-note",
            "addr": L3,
            "handoff_id": "l2-guidance-1",
            "handoff_kind": "guidance_request",
            "note_content": "# Note\n\nApply the parser boundary clarification.",
        }
    )["ok"]

    inbox = addressing.inbox_path(L3, runtime)
    inbox.unlink()
    daemon._recover_message_state_best_effort()
    daemon._recover_message_state_best_effort()

    rows = _jsonl(inbox)
    assert len([row for row in rows if row.get("type") == "message"]) == 2
    assert {row["message_id"] for row in rows} >= {"l2-guidance-1"}


def test_coordination_handoff_refuses_review_seat_and_reused_id_with_changed_marker(runtime):
    l2, _ = _binding(L2, parent_address=None, level="L2")
    l3, token = _binding(L3, parent_address=L2, level="L3")
    review, review_token = _binding(L3_REVIEW, parent_address=L2, level="L3+", extra={"gate_for": L3})
    _seed([l2, l3, review])
    marker, _artifact = _write_handoff(runtime)

    assert chokepoint.submit_coordination_handoff(
        L3_REVIEW,
        marker_path=marker,
        expected_owner_token=review_token,
    ).ok is False
    assert chokepoint.submit_coordination_handoff(L3, marker_path=marker, expected_owner_token=token).ok

    marker.write_text(
        json.dumps(
            {
                "type": "coordination_handoff",
                "handoff_id": "interface-gap-1",
                "handoff_kind": "plan_gap",
                "artifact": "handoffs/interface-gap-1.md",
                "summary": "Changed after submit.",
                "response_required": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    reused = chokepoint.submit_coordination_handoff(L3, marker_path=marker, expected_owner_token=token)

    assert reused.ok is False
    assert "fresh handoff_id" in reused.errors[0]


def test_malformed_coordination_handoff_marker_journals_once_and_wakes_child(runtime):
    l2, _ = _binding(L2, parent_address=None, level="L2")
    l3, _token = _binding(L3, parent_address=L2, level="L3")
    _seed([l2, l3])
    node_dir = addressing.node_dir(L3, runtime)
    marker_dir = node_dir / "handoffs"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / "broken.json"
    marker.write_text(
        json.dumps(
            {
                "type": "coordination_handoff",
                "handoff_id": "broken",
                "handoff_kind": "plan_gap",
                "artifact": "handoffs/missing.md",
                "summary": "This marker points at a missing artifact.",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    daemon._submit_coordination_handoff_markers_best_effort()
    daemon._submit_coordination_handoff_markers_best_effort()

    assert _wal_events(L3).count("coordination_handoff_marker_invalid") == 1
    live = ledger.read_binding(L3)
    assert len(live["nonterminal_marker_errors"]) == 1
    rows = _jsonl(addressing.inbox_path(L3, runtime))
    marker_rows = [row for row in rows if row.get("type") == "nonterminal_marker_invalid"]
    assert len(marker_rows) == 1
    assert marker_rows[0]["marker_kind"] == "coordination_handoff"
    assert marker_rows[0]["marker_artifact"] == str(marker)


def test_coordination_decision_cli_serializes_file_content(tmp_path):
    decision = tmp_path / "decision.md"
    decision.write_text("# Guidance\n", encoding="utf-8")
    parser = harnessctl.build_parser()
    args = parser.parse_args(
        [
            "--socket",
            str(tmp_path / "daemon.sock"),
            "coordination-decision",
            L3,
            "--handoff-id",
            "interface-gap-1",
            "--decision",
            "guidance",
            "--file",
            str(decision),
        ]
    )

    request = harnessctl._build_request(args)

    assert request["command"] == "coordination-decision"
    assert request["addr"] == L3
    assert request["handoff_id"] == "interface-gap-1"
    assert request["decision"] == "guidance"
    assert request["decision_content"] == "# Guidance\n"


def test_coordination_note_cli_serializes_file_content(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Note\n", encoding="utf-8")
    parser = harnessctl.build_parser()
    args = parser.parse_args(
        [
            "--socket",
            str(tmp_path / "daemon.sock"),
            "coordination-note",
            L3,
            "--handoff-id",
            "l2-guidance-1",
            "--kind",
            "guidance_request",
            "--summary",
            "L2 guidance for parser boundary.",
            "--file",
            str(note),
        ]
    )

    request = harnessctl._build_request(args)

    assert request["command"] == "coordination-note"
    assert request["addr"] == L3
    assert request["handoff_id"] == "l2-guidance-1"
    assert request["handoff_kind"] == "guidance_request"
    assert request["summary"] == "L2 guidance for parser boundary."
    assert request["note_content"] == "# Note\n"
