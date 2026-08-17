from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from harnessd import addressing, daemon, fencing, ipc, ledger, messages


PARENT = "proj#exec"
CHILD = "proj/child#exec"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    parent = _binding(PARENT)
    child = _binding(CHILD, parent=PARENT)
    ledger.write_binding({PARENT: parent, CHILD: child}, _lock_held=True)
    return tmp_path


def _binding(address: str, *, parent=None, state="running") -> dict:
    token = fencing.mint_owner_token(address, "sub", "session", 1)
    return {
        "node_address": address,
        "parent_address": parent,
        "state": state,
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": token,
        "level": "L3",
        "last_applied_seq": 0,
        "last_inbox_acked_offset": 0,
    }


def _marker(
    root,
    sender,
    target,
    message_id,
    *,
    needs_answer=False,
    tags=None,
    summary="A durable message.",
):
    directory = addressing.messages_dir(sender, root)
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / f"{message_id}.md"
    artifact.write_text(f"# {message_id}\n", encoding="utf-8")
    marker = directory / f"{message_id}.json"
    marker.write_text(
        json.dumps(
            {
                "type": "message",
                "sender": sender,
                "message_id": message_id,
                "to": target,
                "artifact": f"messages/{message_id}.md",
                "summary": summary,
                "needs_answer": needs_answer,
                "tags": tags or [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return marker


def _rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("sender,target", [(CHILD, PARENT), (PARENT, CHILD)])
def test_one_primitive_delivers_both_directions_on_a_direct_edge(runtime, sender, target):
    marker = _marker(runtime, sender, target, "edge-message")

    result = messages.submit_marker(sender, marker, runtime_root=runtime)

    assert result.ok
    record = ledger.read_binding(sender)["messages"]["edge-message"]
    assert (record["source"], record["target"]) == (sender, target)
    assert record["direction"] == ("up" if sender == CHILD else "down")
    rows = _rows(addressing.inbox_path(target, runtime))
    assert [row["type"] for row in rows] == ["message"]
    assert rows[0]["message_id"] == "edge-message"
    assert "body" not in rows[0]


def test_same_id_exact_replay_repairs_delivery_but_changed_content_refuses(runtime):
    marker = _marker(runtime, CHILD, PARENT, "immutable")
    messages.submit_marker(CHILD, marker, runtime_root=runtime)
    addressing.inbox_path(PARENT, runtime).unlink()

    messages.submit_marker(CHILD, marker, runtime_root=runtime)
    assert len(_rows(addressing.inbox_path(PARENT, runtime))) == 1
    wal_count = len(ledger.load_wal())

    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["summary"] = "Changed under the same id."
    marker.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(messages.MessageError, match="different immutable content"):
        messages.submit_marker(CHILD, marker, runtime_root=runtime)
    assert len(ledger.load_wal()) == wal_count


def test_direct_ipc_answer_returns_receipt_and_exact_retry_is_idempotent(runtime):
    messages.author_and_submit(
        PARENT,
        target=CHILD,
        message_id="ipc-question",
        content="# Question\n\nWhich ruling applies?\n",
        summary="A direct question for the child.",
        needs_answer=True,
        runtime_root=runtime,
    )
    request = {
        "command": "message",
        "addr": CHILD,
        "to": PARENT,
        "message_id": "ipc-answer",
        "message_content": "# Answer\n\nUse the accepted ruling.\n",
        "summary": "The direct answer.",
        "answers_asker": PARENT,
        "answers_message_id": "ipc-question",
    }

    first = ipc.handle_request(request)

    assert first["ok"] is True
    assert Path(first["artifact"]) == addressing.messages_dir(CHILD, runtime) / "ipc-answer.md"
    assert Path(first["marker"]) == addressing.messages_dir(CHILD, runtime) / "ipc-answer.json"
    assert ledger.read_binding(PARENT)["messages"]["ipc-question"]["question_state"] == "answered"
    assert [row["message_id"] for row in _rows(addressing.inbox_path(PARENT, runtime))] == [
        "ipc-answer"
    ]
    answered_events = [
        row for row in ledger.load_wal() if row.get("event") == "message_answered"
    ]
    assert len(answered_events) == 1

    retried = ipc.handle_request(request)

    assert retried["ok"] is True
    assert retried["artifact"] == first["artifact"]
    assert retried["marker"] == first["marker"]
    assert len(
        [row for row in ledger.load_wal() if row.get("event") == "message_answered"]
    ) == 1
    assert [row["message_id"] for row in _rows(addressing.inbox_path(PARENT, runtime))] == [
        "ipc-answer"
    ]


def test_arbitration_is_only_a_queryable_question_tag(runtime):
    marker = _marker(
        runtime,
        CHILD,
        PARENT,
        "arb-1",
        needs_answer=True,
        tags=["arbitration"],
    )
    messages.submit_marker(CHILD, marker, runtime_root=runtime)

    record = ledger.read_binding(CHILD)["messages"]["arb-1"]
    assert record["question_state"] == "open"
    assert record["tags"] == ["arbitration"]
    assert not any(key.startswith("arbitration_") for key in record)


def test_terminal_marker_sweep_adopts_ordinary_message_but_withdraws_question(runtime):
    bindings = ledger.all_nodes()
    bindings[CHILD]["state"] = "done"
    ledger.write_binding(copy.deepcopy(bindings), _lock_held=True)
    _marker(runtime, CHILD, PARENT, "last-word")
    _marker(runtime, CHILD, PARENT, "too-late-question", needs_answer=True)

    daemon._submit_message_markers_best_effort()

    records = ledger.read_binding(CHILD)["messages"]
    assert records["last-word"]["needs_answer"] is False
    assert records["too-late-question"]["question_state"] == "withdrawn"
    ids = [row["message_id"] for row in _rows(addressing.inbox_path(PARENT, runtime))]
    assert ids == ["last-word"]


def test_one_poll_parses_binding_ledger_once_across_pending_message_markers(
    runtime, monkeypatch
):
    """T-19: marker/checklist fanout reuses one durable binding-ledger parse."""
    marker_count = 5
    for index in range(marker_count):
        _marker(runtime, CHILD, PARENT, f"poll-cache-{index}")

    # Keep the real factored poll and real marker stage; neutralize unrelated
    # supervision stages so this count has exactly one performance owner.
    no_op = lambda *args, **kwargs: None
    monkeypatch.setattr(daemon._reconcile_mod, "reconcile_tick", no_op)
    for name in (
        "_turn_state_sweep_best_effort",
        "_route_reconcile_escalations",
        "_watchdog_tick",
        "_service_outboxes_best_effort",
        "_redrive_planned_spawns_best_effort",
        "_dispatch_review_check_seats_best_effort",
        "_service_contract_rebinds_best_effort",
        "_deliver_contract_amendments_best_effort",
        "_recover_message_state_best_effort",
        "_submit_coordination_handoff_markers_best_effort",
        "_recover_coordination_handoff_notifications_best_effort",
        "_submit_plan_alignment_markers_best_effort",
        "_reconcile_plan_alignment_cells_best_effort",
        "_reconcile_plan_alignment_elevations_best_effort",
        "_recover_plan_alignment_notifications_best_effort",
        "_recover_gate_notifications_best_effort",
        "_recover_terminal_notifications_best_effort",
        "_stamp_last_tick_best_effort",
    ):
        monkeypatch.setattr(daemon, name, no_op)

    binding_path = runtime / ledger.BINDING_FILENAME
    # Force a cold first read even when fixture seeding populated a future
    # write-through cache; this is the daemon-restart shape T-19 exposed.
    stat = binding_path.stat()
    os.utime(
        binding_path,
        ns=(stat.st_atime_ns, stat.st_mtime_ns + 1),
    )
    real_read_text = Path.read_text
    binding_disk_reads = 0

    def counted_read_text(path, *args, **kwargs):
        nonlocal binding_disk_reads
        if path == binding_path:
            binding_disk_reads += 1
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read_text)

    daemon.poll_once(None, None, None)

    records = ledger.read_binding(CHILD)["messages"]
    assert all(f"poll-cache-{index}" in records for index in range(marker_count))
    assert binding_disk_reads == 1


def test_non_direct_edge_is_refused(runtime):
    other = "elsewhere#exec"
    bindings = ledger.all_nodes()
    bindings[other] = _binding(other)
    ledger.write_binding(bindings, _lock_held=True)
    marker = _marker(runtime, CHILD, other, "skip-edge")
    with pytest.raises(messages.MessageError, match="not a direct parent-child edge"):
        messages.submit_marker(CHILD, marker, runtime_root=runtime)
