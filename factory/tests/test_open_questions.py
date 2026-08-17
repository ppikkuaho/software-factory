from __future__ import annotations

import copy
import json

import pytest

from harnessd import (
    addressing,
    detector_signals,
    executor,
    fencing,
    ledger,
    messages,
    reconcile,
    turn_state,
)


ASKER = "proj/child#exec"
RECIPIENT = "proj#exec"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    bindings = {
        RECIPIENT: _binding(RECIPIENT),
        ASKER: _binding(ASKER, parent=RECIPIENT),
    }
    ledger.write_binding(bindings, _lock_held=True)
    return tmp_path


def _binding(address, *, parent=None, state="running"):
    return {
        "node_address": address,
        "parent_address": parent,
        "state": state,
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": fencing.mint_owner_token(address, "sub", "session", 1),
        "level": "L3",
        "last_applied_seq": 0,
        "last_inbox_acked_offset": 0,
    }


def _write_marker(root, sender, target, message_id, *, answer=None):
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
                "summary": message_id,
                "needs_answer": answer is None,
                "answers_question": answer,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return marker


def _ask(root):
    marker = _write_marker(root, ASKER, RECIPIENT, "q-1")
    return messages.submit_marker(ASKER, marker, runtime_root=root)


def test_open_question_extends_shared_checklist_and_asker_wait(runtime):
    _ask(runtime)
    bindings = ledger.all_nodes()

    checklist = turn_state.build_checklist(
        RECIPIENT,
        bindings[RECIPIENT],
        runtime_root=runtime,
    )
    assert f"question:{ASKER}:q-1" in checklist["open_item_ids"]
    assert "pending_open_question:q-1" in turn_state.ledger_wait_reasons(
        ASKER,
        bindings[ASKER],
        runtime_root=runtime,
    )


def test_answer_is_an_ordinary_message_and_atomically_closes_question(runtime):
    _ask(runtime)
    answer_ref = {"asker_address": ASKER, "message_id": "q-1"}
    marker = _write_marker(runtime, RECIPIENT, ASKER, "a-1", answer=answer_ref)

    result = messages.submit_marker(RECIPIENT, marker, runtime_root=runtime)

    assert result.ok
    live = ledger.all_nodes()
    assert live[RECIPIENT]["messages"]["a-1"]["answers_question"] == answer_ref
    question = live[ASKER]["messages"]["q-1"]
    assert question["question_state"] == "answered"
    assert question["answered_by"] == {
        "answerer_address": RECIPIENT,
        "message_id": "a-1",
    }
    assert not messages.open_questions_for(RECIPIENT, bindings=live)
    answer_row = ledger.load_wal()[-1]
    assert ASKER in answer_row["related_binding_deltas"]


def test_multi_binding_answer_intent_replays_both_slices_after_checkpoint_crash(
    runtime,
    monkeypatch,
):
    _ask(runtime)
    pre_crash = copy.deepcopy(ledger.all_nodes())
    marker = _write_marker(
        runtime,
        RECIPIENT,
        ASKER,
        "a-crash",
        answer={"asker_address": ASKER, "message_id": "q-1"},
    )
    real_write = ledger.write_binding

    def crash_checkpoint(*args, **kwargs):
        raise RuntimeError("crash after WAL")

    monkeypatch.setattr(ledger, "write_binding", crash_checkpoint)
    with pytest.raises(RuntimeError, match="crash after WAL"):
        messages.submit_marker(RECIPIENT, marker, runtime_root=runtime)
    monkeypatch.setattr(ledger, "write_binding", real_write)

    assert ledger.all_nodes() == pre_crash
    replayed = reconcile.replay_wal(ledger.all_nodes(), ledger.load_wal())
    assert replayed[RECIPIENT]["messages"]["a-crash"]["message_id"] == "a-crash"
    assert replayed[ASKER]["messages"]["q-1"]["question_state"] == "answered"


def test_only_question_target_may_answer(runtime):
    _ask(runtime)
    sibling = "proj/sibling#exec"
    bindings = ledger.all_nodes()
    bindings[sibling] = _binding(sibling, parent=RECIPIENT)
    ledger.write_binding(bindings, _lock_held=True)
    artifact_dir = addressing.messages_dir(sibling, runtime)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / "wrong.md"
    artifact.write_text("wrong", encoding="utf-8")
    record = {
        "message_id": "wrong",
        "content_sha256": "wrong-content",
        "source": sibling,
        "target": ASKER,
        "artifact": "messages/wrong.md",
        "marker": "messages/wrong.md",
    }

    result = executor.record_message(
        sibling,
        message_id="wrong",
        record=record,
        answers_question={"asker_address": ASKER, "message_id": "q-1"},
    )
    assert not result.ok
    assert "only question target" in result.errors[0]


def test_terminal_asker_withdrawal_is_loud_and_removes_recipient_burden(runtime):
    _ask(runtime)
    bindings = ledger.all_nodes()
    bindings[ASKER]["state"] = "failed"
    ledger.write_binding(bindings, _lock_held=True)

    assert messages.withdraw_terminal_questions() == 1
    assert ledger.read_binding(ASKER)["messages"]["q-1"]["question_state"] == "withdrawn"
    assert ledger.load_wal()[-1]["event"] == "open_questions_withdrawn"
    assert not messages.open_questions_for(RECIPIENT)


def test_question_survives_incarnation_change_at_same_address(runtime):
    _ask(runtime)
    bindings = ledger.all_nodes()
    bindings[ASKER]["owner_token"] = "replacement-incarnation-token"
    bindings[ASKER]["lease_epoch"] = 2
    ledger.write_binding(bindings, _lock_held=True)

    assert messages.open_questions_for(RECIPIENT)[0]["source"] == ASKER
    assert "pending_open_question:q-1" in turn_state.ledger_wait_reasons(
        ASKER,
        ledger.read_binding(ASKER),
        runtime_root=runtime,
    )


def test_canonical_answer_releases_legacy_escalation_hold():
    identity = "sha256:legacy-question"
    question_id = messages.escalation_message_id(identity)
    binding = {
        "signal_artifact_seen_at": identity,
        "terminal_signal": "ESCALATED",
        "messages": {
            question_id: {
                "message_id": question_id,
                "needs_answer": True,
                "question_state": "answered",
            }
        },
    }
    assert detector_signals.escalation_answered(binding, identity) is True
