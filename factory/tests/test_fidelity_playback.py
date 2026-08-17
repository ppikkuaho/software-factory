"""Q6 — L1 preliminary fidelity playback plus owner-final promotion authority."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from harnessd import (
    addressing,
    commissioning,
    fencing,
    fidelity_playback,
    harnessctl,
    ipc,
    ledger,
    observability,
)


NODE = "proj/demo#exec"
CHILD = "proj/demo/component#exec"

INTENT = """# Intent Spec

## Outcomes
| Outcome ID | Outcome |
|---|---|
| O-001 | The recipient can run the demo. |
| O-002 | The recipient sees a safe refusal for invalid input. |

## Requirements
| ID | Requirement | Tag | MNF | Reflect-back status |
|---|---|---|---|---|
| R-001 | Run the demo | decided | — | confirmed |
| R-002 | Never corrupt invalid input | decided | YES | confirmed |

## Delivery destination
| Destination | in-place / no external delivery |
| Kind | in-place |
"""

JUDGMENT = """# Fidelity Judgment

Asked: Run the demo and reject invalid input safely.

Delivered: The recipient-visible command and refusal were driven.

Deviations: None.

Preliminary Verdict: accept

## Outcome Playback
| Outcome ID | Drove | Observed | Evidence | Preliminary Result |
|---|---|---|---|---|
| O-001 | `demo run` | successful recipient output | evidence/outcome-1.txt | accept |
| O-002 | `demo run --invalid` | safe refusal | evidence/outcome-2.txt | accept |

## MNF Playback
| MNF ID | Drove | Observed | Evidence | Preliminary Result |
|---|---|---|---|---|
| R-002 | `demo run --invalid` | refused without mutation | evidence/mnf-r002.txt | accept |
"""


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = root
    try:
        yield root
    finally:
        ledger.RUNTIME_ROOT = previous


def _binding(
    address=NODE,
    *,
    parent="root#exec",
    level="L1",
    state="done",
    authority="owner",
    delegate=None,
    reason=None,
):
    token = fencing.mint_owner_token(address, f"sa-{level}", f"sess-{level}", 1)
    return {
        "node_address": address,
        "parent_address": parent,
        "level": level,
        "subagent_id": f"sa-{level}",
        "session_uuid": f"sess-{level}",
        "state": state,
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "terminal" if state == "done" else "working",
        "deliverable_state": "completed",
        "delivery_destination": "in-place",
        "delivery_kind": "in-place",
        "fidelity_playback_authority": authority,
        "fidelity_playback_delegate": delegate,
        "fidelity_playback_delegation_reason": reason,
        "fidelity_playback_authority_build_id": "commissioning-q6",
    }


def _seed(runtime, *, authority="owner", delegate=None, reason=None, child=False):
    node = _binding(
        authority=authority,
        delegate=delegate,
        reason=reason,
    )
    bindings = {NODE: node}
    if child:
        bindings[CHILD] = _binding(
            CHILD,
            parent=NODE,
            level="L2",
            state="running",
        )
    ledger.write_binding(copy.deepcopy(bindings), _lock_held=True)
    root = addressing.node_dir(NODE, runtime)
    brief = root / "client-brief"
    evidence = root / "evidence"
    brief.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    (brief / "intent-spec.md").write_text(INTENT, encoding="utf-8")
    (brief / "fidelity-judgment.md").write_text(JUDGMENT, encoding="utf-8")
    for name in ("outcome-1.txt", "outcome-2.txt", "mnf-r002.txt"):
        (evidence / name).write_text(f"observed {name}\n", encoding="utf-8")
    (root / "product.txt").write_text("deliverable\n", encoding="utf-8")
    return root


def _post_question():
    response = ipc.handle_request(
        {"command": "fidelity-playback", "addr": NODE}
    )
    assert response["ok"] is True, response
    return response


def _answer(question_id, decision="confirm", text="Owner confirms.", **extra):
    return ipc.handle_request(
        {
            "command": "answer",
            "addr": NODE,
            "question_id": question_id,
            "decision": decision,
            "answer_content": text,
            **extra,
        }
    )


def test_preliminary_question_is_content_addressed_idempotent_and_pointer_only(runtime):
    _seed(runtime)
    first = _post_question()
    second = _post_question()

    assert second["question_id"] == first["question_id"]
    question_path = Path(first["question_artifact"])
    assert question_path.stat().st_mode & 0o777 == 0o444
    payload = json.loads(question_path.read_text(encoding="utf-8"))
    assert payload["preliminary_verdict"] == "accept"
    assert payload["outcome_ids"] == ["O-001", "O-002"]
    assert payload["mnf_ids"] == ["R-002"]
    assert "successful recipient output" not in json.dumps(payload)
    questions = ledger.read_binding(NODE)["fidelity_playback_owner_questions"]
    assert list(questions) == [first["question_id"]]


def test_incomplete_or_out_of_jail_playback_evidence_is_refused(runtime):
    root = _seed(runtime)
    judgment = root / "client-brief" / "fidelity-judgment.md"
    judgment.write_text(
        JUDGMENT.replace(
            "| O-002 | `demo run --invalid` | safe refusal | evidence/outcome-2.txt | accept |\n",
            "",
        ).replace("evidence/mnf-r002.txt", "../../outside.txt"),
        encoding="utf-8",
    )

    response = ipc.handle_request(
        {"command": "fidelity-playback", "addr": NODE}
    )

    assert response["ok"] is False
    assert "FIDELITY-PLAYBACK-MISSING-EVIDENCE-ROW:O-002" in response["errors"]
    assert any(
        error.startswith("FIDELITY-PLAYBACK-EVIDENCE-POINTER-INVALID:R-002")
        for error in response["errors"]
    )


def test_playback_table_requires_the_exact_column_order(runtime):
    root = _seed(runtime)
    judgment = root / "client-brief" / "fidelity-judgment.md"
    judgment.write_text(
        JUDGMENT.replace(
            "Outcome ID | Drove | Observed | Evidence | Preliminary Result",
            "Outcome ID | Observed | Drove | Evidence | Preliminary Result",
        ),
        encoding="utf-8",
    )

    response = ipc.handle_request(
        {"command": "fidelity-playback", "addr": NODE}
    )

    assert response["ok"] is False
    assert response["errors"] == [
        "FIDELITY-PLAYBACK-OUTCOME-PLAYBACK-TABLE-SCHEMA"
    ]


def test_owner_confirm_is_immutable_and_opens_deliberate_promote(runtime):
    _seed(runtime)
    question = _post_question()

    held = ipc.handle_request(
        {"command": "promote", "addr": NODE, "decision": "accept"}
    )
    assert held["ok"] is False
    assert "OWNER-CONFIRMED-FIDELITY-PLAYBACK-REQUIRED" in held["errors"][0]

    answer = _answer(question["question_id"])
    assert answer["ok"] is True, answer
    assert answer["answer_authority"] == "owner"
    assert Path(answer["answer_artifact"]).stat().st_mode & 0o777 == 0o444
    assert ledger.read_binding(NODE)["deliverable_state"] == "completed"

    promoted = ipc.handle_request(
        {"command": "promote", "addr": NODE, "decision": "accept"}
    )
    assert promoted["ok"] is True, promoted
    assert promoted["deliverable_state"] == "delivered"
    assert (
        promoted["playback_authorization"]
        == "OWNER-CONFIRMED-FIDELITY-PLAYBACK"
    )


def test_changed_judgment_invalidates_prior_answer_and_requires_new_question(runtime):
    root = _seed(runtime)
    question = _post_question()
    assert _answer(question["question_id"])["ok"] is True
    judgment = root / "client-brief" / "fidelity-judgment.md"
    judgment.chmod(0o644)
    judgment.write_text(
        JUDGMENT.replace("successful recipient output", "successful exact output"),
        encoding="utf-8",
    )

    _authorization, blockers = fidelity_playback.promotion_authorization(NODE)

    assert any("missing-current-question" in blocker for blocker in blockers)
    next_question = _post_question()
    assert next_question["question_id"] != question["question_id"]


def test_changed_evidence_bytes_invalidate_confirmed_playback(runtime):
    root = _seed(runtime)
    question = _post_question()
    assert _answer(question["question_id"])["ok"] is True
    (root / "evidence" / "outcome-1.txt").write_text(
        "different observation\n",
        encoding="utf-8",
    )

    _authorization, blockers = fidelity_playback.promotion_authorization(NODE)

    assert "FIDELITY-PLAYBACK-EVIDENCE-DRIFTED:O-001" in blockers


def test_reject_requires_reason_wakes_l1_and_routes_one_canonical_repair(runtime):
    _seed(runtime, child=True)
    question = _post_question()
    refused = _answer(question["question_id"], decision="reject", text="")
    assert refused["errors"] == ["FIDELITY-PLAYBACK-REJECT-REQUIRES-REASON"]

    rejected = _answer(
        question["question_id"],
        decision="reject",
        text="The invalid-input refusal loses the original file.",
    )

    assert rejected["ok"] is True, rejected
    parent = ledger.read_binding(NODE)
    repair = next(
        row
        for row in parent["messages"].values()
        if "fidelity-playback-repair" in row["tags"]
    )
    assert repair["target"] == CHILD
    assert repair["needs_answer"] is False
    artifact = addressing.node_dir(NODE, runtime) / repair["artifact"]
    assert "The invalid-input refusal loses the original file." in artifact.read_text(
        encoding="utf-8"
    )
    inbox = addressing.inbox_path(NODE, runtime).read_text(encoding="utf-8")
    assert "fidelity_playback_owner_question_answered" in inbox


def test_reject_answer_persists_but_zero_or_many_repair_target_fails_loud(runtime):
    _seed(runtime)
    question = _post_question()

    response = _answer(
        question["question_id"],
        decision="reject",
        text="Repair the recipient-visible mismatch.",
    )

    assert response["ok"] is False
    assert response["errors"] == [
        f"FIDELITY-PLAYBACK-REPAIR-TARGET-AMBIGUOUS:{NODE}:"
        "expected-one-live-direct-L2:found-0"
    ]
    row = ledger.read_binding(NODE)["fidelity_playback_owner_questions"][
        question["question_id"]
    ]
    assert row["status"] == "rejected"

    bindings = ledger.all_nodes()
    bindings[CHILD] = _binding(
        CHILD,
        parent=NODE,
        level="L2",
        state="running",
    )
    ledger.write_binding(bindings, _lock_held=True)
    redriven = _answer(
        question["question_id"],
        decision="reject",
        text="Repair the recipient-visible mismatch.",
    )
    assert redriven["ok"] is True, redriven
    assert any(
        "fidelity-playback-repair" in record.get("tags", [])
        for record in ledger.read_binding(NODE)["messages"].values()
    )


def test_delegate_requires_launch_authorization_and_never_looks_owner_confirmed(runtime):
    _seed(
        runtime,
        authority="operator-delegate",
        delegate="commissioning-operator",
        reason="supervised Q6 commissioning",
    )
    question = _post_question()
    mismatch = _answer(
        question["question_id"],
        answer_authority="operator-delegate",
        answer_actor="someone-else",
    )
    assert mismatch["ok"] is False
    assert "DELEGATE-ACTOR-MISMATCH" in mismatch["errors"][0]

    accepted = _answer(
        question["question_id"],
        answer_authority="operator-delegate",
        answer_actor="commissioning-operator",
    )
    assert accepted["ok"] is True, accepted
    authorization, blockers = fidelity_playback.promotion_authorization(NODE)
    assert blockers == []
    assert (
        authorization["label"]
        == "COMMISSIONING-DELEGATE-CONFIRMED-FIDELITY-PLAYBACK"
    )
    assert authorization["answer_authority"] == "operator-delegate"
    snapshot = observability.snapshot(runtime_root=runtime)
    row = next(row for row in snapshot["nodes"] if row["node_address"] == NODE)
    assert row["fidelity_playback"]["last_answer_authority"] == "operator-delegate"
    assert "owner" not in authorization["label"].lower()
    answer_payload = json.loads(
        Path(accepted["answer_artifact"]).read_text(encoding="utf-8")
    )
    assert answer_payload["answer_authority"] == "operator-delegate"
    inbox = addressing.inbox_path(NODE, runtime).read_text(encoding="utf-8")
    assert "fidelity_playback_commissioning_delegate_question_answered" in inbox
    assert "fidelity_playback_owner_question_answered" not in inbox
    wal_events = [row["event"] for row in ledger.load_wal()]
    assert "fidelity_playback_commissioning_delegate_answered" in wal_events
    assert "fidelity_playback_owner_answered" not in wal_events


def test_owner_override_remains_live_on_delegate_enabled_run(runtime):
    _seed(
        runtime,
        authority="operator-delegate",
        delegate="commissioning-operator",
        reason="supervised Q6 commissioning",
    )
    question = _post_question()

    accepted = _answer(question["question_id"])

    assert accepted["ok"] is True, accepted
    authorization, blockers = fidelity_playback.promotion_authorization(NODE)
    assert blockers == []
    assert authorization["label"] == "OWNER-CONFIRMED-FIDELITY-PLAYBACK"


def test_cli_serializes_playback_and_explicit_delegate_authority():
    parser = harnessctl.build_parser()
    playback = harnessctl._build_request(
        parser.parse_args(["fidelity-playback", NODE])
    )
    answer = harnessctl._build_request(
        parser.parse_args(
            [
                "answer",
                NODE,
                "--question-id",
                "fidelity-playback-abc",
                "--decision",
                "confirm",
                "--authority",
                "operator-delegate",
                "--actor",
                "commissioning-operator",
                "--text",
                "Confirm.",
            ]
        )
    )

    assert playback == {"command": "fidelity-playback", "addr": NODE}
    assert answer["answer_authority"] == "operator-delegate"
    assert answer["answer_actor"] == "commissioning-operator"


def test_commissioning_delegate_requires_all_three_explicit_inputs(monkeypatch, tmp_path):
    monkeypatch.setenv(
        commissioning.FIDELITY_PLAYBACK_AUTHORITY_ENV,
        "operator-delegate",
    )
    with pytest.raises(RuntimeError, match="requires both"):
        commissioning.build_runtime(
            runtime_root=tmp_path / "runtime",
            build_id="q6",
            oauth_token="test-ant-oat01-X",
        )
    monkeypatch.setenv(
        commissioning.FIDELITY_PLAYBACK_DELEGATE_ENV,
        "commissioning-operator",
    )
    monkeypatch.setenv(
        commissioning.FIDELITY_PLAYBACK_DELEGATION_REASON_ENV,
        "supervised test run",
    )

    runtime_descriptor = commissioning.build_runtime(
        runtime_root=tmp_path / "runtime",
        build_id="q6",
        oauth_token="test-ant-oat01-X",
    )

    assert (
        runtime_descriptor.config.fidelity_playback_authority
        == "operator-delegate"
    )
    assert (
        runtime_descriptor.config.fidelity_playback_delegate
        == "commissioning-operator"
    )
