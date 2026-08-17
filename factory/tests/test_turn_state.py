from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from harnessd import (
    addressing,
    clock,
    daemon,
    executor,
    ipc,
    ledger,
    return_contract,
    turn_state,
)


NODE = "project/work#exec"
TOKEN = "owner-token-current"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    node_dir = addressing.node_dir(NODE, tmp_path)
    node_dir.mkdir(parents=True)
    binding = {
        "node_address": NODE,
        "state": "running",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": TOKEN,
        "level": "L5",
        "runtime": "claude-code",
        "parent_address": None,
    }
    ledger.write_binding({NODE: binding}, _lock_held=True)
    addressing.signoff_path(NODE, tmp_path).write_text(
        json.dumps({"owner_token": TOKEN}) + "\n",
        encoding="utf-8",
    )
    return tmp_path, binding, node_dir


def _signal(root: Path, signal: str, *, token: str = TOKEN) -> None:
    addressing.signal_path(NODE, root).write_text(
        json.dumps(
            {
                "signal": signal,
                "ts": "2026-07-24T12:00:00+00:00",
                "owner_token": token,
                "evidence": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _current(root: Path) -> dict:
    return json.loads(addressing.turn_state_path(NODE, root).read_text(encoding="utf-8"))


def _events(root: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in addressing.turn_events_path(NODE, root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _claim_report(body="Delivered R-1\n") -> str:
    return (
        body.rstrip()
        + "\n\n## Drove and Watched\n\nFixture drove the claim.\n"
        + "\n## Inferred\n\nSupporting evidence only.\n"
        + "\n## Residual Uncertainty\n\nNone beyond fixture scope.\n"
        + "\n## Inventory\n\nNone.\n"
    )


def _activate_surface(root: Path, binding: dict, profile: str) -> dict:
    surface = turn_state.seed(
        NODE,
        binding,
        runtime_root=root,
        profile=profile,
    )
    binding.update({key: value for key, value in surface.items() if key != "turn_runtime_root"})
    ledger.write_binding({NODE: binding}, _lock_held=True)
    return binding


def test_return_contract_walk_and_public_verdict_are_the_same(runtime):
    root, binding, node_dir = runtime
    walk = return_contract.walk_done_contract(NODE, binding)
    verdict = return_contract.check_done_contract(NODE, binding)
    assert verdict.ok == walk.ok
    assert verdict.defects == list(walk.defects)
    assert any(item.item_id == "return_report" and not item.ok for item in walk.items)

    (node_dir / "report.md").write_text(_claim_report(), encoding="utf-8")
    walk = return_contract.walk_done_contract(NODE, binding)
    verdict = return_contract.check_done_contract(NODE, binding)
    assert verdict.ok == walk.ok
    assert verdict.defects == list(walk.defects)


def test_test_author_red_run_log_floor_flows_into_the_shared_checklist(runtime):
    _root, binding, node_dir = runtime
    binding["child_purpose"] = "test_author"
    (node_dir / "report.md").write_text("# report\n\nAuthored acceptance.\n", encoding="utf-8")
    (node_dir / "tests").mkdir()

    walk = return_contract.walk_done_contract(NODE, binding)

    assert any("MISSING-RED-RUN-LOG" in defect for defect in walk.defects)
    item = next(item for item in walk.items if item.item_id == "test_author_red_run_log")
    assert item.ok is False
    (node_dir / "tests" / "red-run-log.md").write_text(
        "# Red run log\n\n`pytest tests/test_claim.py` failed on the unimplemented claim.\n",
        encoding="utf-8",
    )
    repaired = return_contract.walk_done_contract(NODE, binding)
    assert not any("MISSING-RED-RUN-LOG" in defect for defect in repaired.defects)
    assert next(
        item for item in repaired.items if item.item_id == "test_author_red_run_log"
    ).ok is True


def test_implementation_claim_account_heading_floor_flows_into_shared_checklist(runtime):
    _root, binding, node_dir = runtime
    (node_dir / "report.md").write_text("# report\n\nDelivered.\n", encoding="utf-8")

    walk = return_contract.walk_done_contract(NODE, binding)

    assert any("MISSING-CLAIM-ACCOUNT-SECTIONS" in defect for defect in walk.defects)
    item = next(item for item in walk.items if item.item_id == "claim_account")
    assert item.ok is False
    (node_dir / "report.md").write_text(
        "# report\n\n"
        "## Drove and Watched\n\n"
        "## Inferred\n\n"
        "## Residual Uncertainty\n\n"
        "## Inventory\n",
        encoding="utf-8",
    )
    repaired = return_contract.walk_done_contract(NODE, binding)
    assert not any(
        "MISSING-CLAIM-ACCOUNT-SECTIONS" in defect for defect in repaired.defects
    )
    assert next(item for item in repaired.items if item.item_id == "claim_account").ok is True


def test_seed_writes_seat_qualified_current_events_and_checklist(runtime):
    root, binding, _node_dir = runtime
    surface = turn_state.seed(
        NODE,
        binding,
        runtime_root=root,
        profile=turn_state.CLAUDE_FULL_EDGES,
    )
    assert surface["turn_hook_profile"] == turn_state.CLAUDE_FULL_EDGES
    assert Path(surface["turn_state_path"]) == addressing.turn_state_path(NODE, root)
    assert Path(surface["turn_events_path"]).read_text(encoding="utf-8") == ""
    assert _current(root)["state"] == turn_state.TURN_NOT_STARTED
    checklist = json.loads(
        addressing.owed_checklist_path(NODE, root).read_text(encoding="utf-8")
    )
    assert "return_report" in checklist["open_item_ids"]
    assert "terminal_signoff" in checklist["open_item_ids"]


def test_truthful_parallel_tool_edges_keep_in_flight_until_every_tool_finishes(runtime):
    root, binding, _node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )
    for tool_id in ("tool-a", "tool-b"):
        turn_state.handle_hook_event(
            runtime_root=root,
            node_address=NODE,
            owner_token=TOKEN,
            runtime="claude-code",
            payload={"hook_event_name": "PreToolUse", "tool_use_id": tool_id},
        )
    assert _current(root)["state"] == turn_state.TOOL_IN_FLIGHT
    assert _current(root)["in_flight_tools"] == ["tool-a", "tool-b"]

    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "PostToolUse", "tool_use_id": "tool-a"},
    )
    assert _current(root)["state"] == turn_state.TOOL_IN_FLIGHT
    assert _current(root)["in_flight_tools"] == ["tool-b"]

    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "PostToolUse", "tool_use_id": "tool-b"},
    )
    assert _current(root)["state"] == turn_state.TURN_RUNNING
    assert _current(root)["in_flight_tools"] == []


def test_failed_tool_edge_drains_only_its_matching_parallel_call(runtime):
    root, binding, node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )
    for tool_id in ("tool-fails", "tool-still-running"):
        turn_state.handle_hook_event(
            runtime_root=root,
            node_address=NODE,
            owner_token=TOKEN,
            runtime="claude-code",
            payload={
                "hook_event_name": "PreToolUse",
                "tool_use_id": tool_id,
                "tool_name": "Bash",
            },
        )
    (node_dir / "report.md").write_text(_claim_report(), encoding="utf-8")

    response = turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={
            "hook_event_name": "PostToolUseFailure",
            "tool_use_id": "tool-fails",
            "tool_name": "Bash",
            "error": "exit status 1",
        },
    )

    current = _current(root)
    assert current["state"] == turn_state.TOOL_IN_FLIGHT
    assert current["in_flight_tools"] == ["tool-still-running"]
    assert current["last_hook_event"] == "PostToolUseFailure"
    assert response["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"


def test_ask_user_question_is_explicit_human_wait_until_its_matching_edge_closes(runtime):
    root, binding, _node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )
    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={
            "hook_event_name": "PreToolUse",
            "tool_use_id": "question-1",
            "tool_name": "AskUserQuestion",
        },
    )
    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={
            "hook_event_name": "PreToolUse",
            "tool_use_id": "parallel-read",
            "tool_name": "Read",
        },
    )

    current = _current(root)
    assert current["state"] == turn_state.WAITING_ON_HUMAN
    assert current["waiting_on_human_tool_id"] == "question-1"
    assert current["waiting_on_human_tool_name"] == "AskUserQuestion"
    assert current["in_flight_tools"] == ["parallel-read", "question-1"]

    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "PostToolUse", "tool_use_id": "parallel-read"},
    )
    assert _current(root)["state"] == turn_state.WAITING_ON_HUMAN

    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "PostToolUse", "tool_use_id": "question-1"},
    )
    current = _current(root)
    assert current["state"] == turn_state.TURN_RUNNING
    assert current["waiting_on_human_tool_id"] is None
    assert current["waiting_on_human_tool_name"] is None


def test_post_tool_ack_is_emitted_only_when_an_owed_item_lands_or_reopens(runtime):
    root, binding, node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )
    report = node_dir / "report.md"
    report.write_text("Delivered R-1\n", encoding="utf-8")
    response = turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "PostToolUse", "tool_use_id": "write-report"},
    )
    assert response["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "return_report landed" in response["hookSpecificOutput"]["additionalContext"]

    no_change = turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "PostToolUse", "tool_use_id": "read-only"},
    )
    assert no_change is None

    report.unlink()
    reopened = turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "PostToolUse", "tool_use_id": "remove-report"},
    )
    assert "return_report reopened" in reopened["hookSpecificOutput"]["additionalContext"]
    acks = [row for row in _events(root) if row["hook_event"] == "owed_checklist_ack"]
    assert len(acks) == 2


def test_checklist_refresh_never_advances_the_hook_edge_clock(runtime, monkeypatch):
    root, binding, node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )
    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "UserPromptSubmit"},
    )
    hook_updated_at = _current(root)["updated_at"]
    monkeypatch.setattr(clock, "now_utc", lambda: "2099-01-01T00:00:00+00:00")

    unchanged = turn_state.refresh_checklist(NODE, binding, runtime_root=root)
    assert _current(root)["updated_at"] == hook_updated_at
    assert _current(root)["checklist_version"] == unchanged["version"]

    (node_dir / "report.md").write_text(_claim_report(), encoding="utf-8")
    changed = turn_state.refresh_checklist(NODE, binding, runtime_root=root)
    assert changed["version"] != unchanged["version"]
    assert _current(root)["checklist_version"] == changed["version"]
    assert _current(root)["updated_at"] == hook_updated_at


def test_raw_hook_append_is_ledger_blind_and_preserves_only_needed_payload(
    runtime, monkeypatch
):
    root, binding, _node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )
    monkeypatch.setattr(
        turn_state,
        "_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the hook append path must not read the ledger")
        ),
    )

    event_id = turn_state.append_raw_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={
            "hook_event_name": "UserPromptSubmit",
            "prompt": "must not be persisted",
            "session_id": "session-1",
        },
    )

    row = _events(root)[-1]
    assert row["row_kind"] == turn_state.RAW_HOOK_EVENT
    assert row["event_id"] == event_id
    assert row["payload"] == {"hook_event_name": "UserPromptSubmit"}
    assert row["adopted"] is False


def test_daemon_adopts_raw_edges_once_and_writes_exact_response_ids(runtime):
    root, binding, _node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    for payload in (
        {
            "hook_event_name": "PreToolUse",
            "tool_use_id": "tool-fails",
            "tool_name": "Bash",
        },
        {
            "hook_event_name": "PostToolUseFailure",
            "tool_use_id": "tool-fails",
            "tool_name": "Bash",
        },
    ):
        turn_state.append_raw_hook_event(
            runtime_root=root,
            node_address=NODE,
            owner_token=TOKEN,
            runtime="claude-code",
            payload=payload,
        )
    failed_event_id = _events(root)[-1]["event_id"]

    result = daemon._adopt_turn_state_for_seat(
        executor,
        object(),
        NODE,
        binding,
        target_event_id=failed_event_id,
    )

    assert result["found"] is True
    assert result["response_event_id"] == failed_event_id
    assert _current(root)["state"] == turn_state.TURN_RUNNING
    assert _current(root)["in_flight_tools"] == []
    rows = _events(root)
    raw_ids = [row["event_id"] for row in rows if row["row_kind"] == turn_state.RAW_HOOK_EVENT]
    adopted_ids = [
        row["detail"]["ingress_event_id"]
        for row in rows
        if row.get("row_kind") == turn_state.ADOPTED_HOOK_EVENT
    ]
    assert adopted_ids == raw_ids
    response = next(
        row
        for row in rows
        if row.get("row_kind") == turn_state.HOOK_RESPONSE
        and row.get("responds_to_event_id") == failed_event_id
    )
    assert response["response"] is None

    daemon._adopt_turn_state_for_seat(executor, object(), NODE, ledger.read_binding(NODE))
    assert [
        row["detail"]["ingress_event_id"]
        for row in _events(root)
        if row.get("row_kind") == turn_state.ADOPTED_HOOK_EVENT
    ] == adopted_ids


def test_poll_sweep_and_ipc_trigger_serialize_one_exact_raw_adoption(
    runtime, monkeypatch
):
    root, binding, _node_dir = runtime
    _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    event_id = turn_state.append_raw_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={
            "hook_event_name": "PostToolUseFailure",
            "tool_use_id": "failed-bash",
            "tool_name": "Bash",
        },
    )
    real_handle = turn_state.handle_hook_event
    first_selected = threading.Event()
    release_first = threading.Event()
    calls_lock = threading.Lock()
    calls = []

    def delayed_handle(*args, **kwargs):
        with calls_lock:
            calls.append(kwargs.get("ingress_event_id"))
            call_number = len(calls)
        if call_number == 1:
            first_selected.set()
            assert release_first.wait(timeout=2)
        return real_handle(*args, **kwargs)

    monkeypatch.setattr(turn_state, "handle_hook_event", delayed_handle)
    sweep = threading.Thread(
        target=daemon._turn_state_sweep_best_effort,
        args=(executor, object()),
    )
    ipc_result = {}

    def trigger_ipc():
        ipc_result["value"] = ipc._handle_turn_hook_adopt(
            {"addr": NODE, "event_id": event_id}
        )

    sweep.start()
    assert first_selected.wait(timeout=2)
    trigger = threading.Thread(target=trigger_ipc)
    trigger.start()
    time.sleep(0.05)
    with calls_lock:
        selected_before_release = list(calls)
    release_first.set()
    sweep.join(timeout=3)
    trigger.join(timeout=3)

    assert not sweep.is_alive()
    assert not trigger.is_alive()
    assert selected_before_release == [event_id]
    assert ipc_result["value"]["ok"] is True
    rows = _events(root)
    assert [
        row["detail"]["ingress_event_id"]
        for row in rows
        if row.get("row_kind") == turn_state.ADOPTED_HOOK_EVENT
    ] == [event_id]
    assert [
        row["responds_to_event_id"]
        for row in rows
        if row.get("row_kind") == turn_state.HOOK_RESPONSE
    ] == [event_id]


def test_hook_response_wait_requires_the_exact_raw_event_id(runtime):
    root, binding, _node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )
    turn_state.append_hook_response(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        ingress_event_id="different-event",
        response=None,
    )

    with pytest.raises(turn_state.HookResponseTimeout, match="wanted-event"):
        turn_state.wait_for_hook_response(
            runtime_root=root,
            node_address=NODE,
            ingress_event_id="wanted-event",
            timeout_s=0.02,
        )


def test_capture_appends_before_trigger_and_returns_exact_daemon_response(runtime):
    root, binding, _node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    seen = {}

    def trigger(event_id):
        raw = _events(root)[-1]
        assert raw["row_kind"] == turn_state.RAW_HOOK_EVENT
        assert raw["event_id"] == event_id
        seen["result"] = daemon._adopt_turn_state_for_seat(
            executor,
            object(),
            NODE,
            binding,
            target_event_id=event_id,
        )

    response = turn_state.capture_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={
            "hook_event_name": "PostToolUseFailure",
            "tool_use_id": "failed-bash",
            "tool_name": "Bash",
        },
        trigger=trigger,
        response_timeout_s=0.2,
    )

    assert seen["result"]["response_event_id"] is not None
    assert response is None


def test_capture_keeps_raw_event_and_fails_loud_when_ipc_and_recovery_miss(runtime):
    root, binding, _node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )

    with pytest.raises(turn_state.HookResponseTimeout):
        turn_state.capture_hook_event(
            runtime_root=root,
            node_address=NODE,
            owner_token=TOKEN,
            runtime="claude-code",
            payload={
                "hook_event_name": "Stop",
            },
            trigger=lambda _event_id: (_ for _ in ()).throw(
                ConnectionError("daemon unavailable")
            ),
            response_timeout_s=0.02,
        )

    rows = _events(root)
    assert len(rows) == 1
    assert rows[0]["row_kind"] == turn_state.RAW_HOOK_EVENT
    assert rows[0]["hook_event"] == "Stop"


def test_daemon_adopts_malformed_raw_callback_as_degraded_hook_state(runtime):
    root, binding, _node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    event_id = turn_state.append_raw_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={
            "type": "malformed_hook_payload",
            "fault_reason": "invalid JSON: torn callback",
        },
    )

    daemon._adopt_turn_state_for_seat(executor, object(), NODE, binding)

    current = turn_state.read_current(NODE, binding, runtime_root=root)
    live = ledger.read_binding(NODE)
    assert current.status == "malformed"
    assert "torn callback" in current.reason
    assert live["turn_hook_health"] == "degraded"
    assert [
        row["detail"]["ingress_event_id"]
        for row in _events(root)
        if row.get("row_kind") == turn_state.ADOPTED_HOOK_EVENT
    ] == [event_id]


def test_raw_event_appended_at_drain_boundary_remains_for_next_adoption(
    runtime, monkeypatch
):
    root, binding, _node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    first_id = turn_state.append_raw_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "UserPromptSubmit"},
    )
    real_read = turn_state.read_event_tail
    appended = {}

    def append_after_empty_read(path, offset):
        rows, new_offset, errors = real_read(path, offset)
        if not rows and "event_id" not in appended:
            appended["event_id"] = turn_state.append_raw_hook_event(
                runtime_root=root,
                node_address=NODE,
                owner_token=TOKEN,
                runtime="claude-code",
                payload={
                    "hook_event_name": "PreToolUse",
                    "tool_use_id": "boundary-tool",
                    "tool_name": "Read",
                },
            )
        return rows, new_offset, errors

    monkeypatch.setattr(turn_state, "read_event_tail", append_after_empty_read)
    daemon._adopt_turn_state_for_seat(executor, object(), NODE, binding)
    after_first = ledger.read_binding(NODE)
    assert after_first["turn_state"] == turn_state.TURN_RUNNING
    assert after_first["turn_event_acked_offset"] < addressing.turn_events_path(
        NODE, root
    ).stat().st_size

    daemon._adopt_turn_state_for_seat(executor, object(), NODE, after_first)

    adopted_ids = [
        row["detail"]["ingress_event_id"]
        for row in _events(root)
        if row.get("row_kind") == turn_state.ADOPTED_HOOK_EVENT
    ]
    assert adopted_ids == [first_id, appended["event_id"]]
    assert ledger.read_binding(NODE)["turn_in_flight_tools"] == ["boundary-tool"]


def test_stale_raw_event_is_journaled_and_cannot_replace_current(runtime):
    root, binding, _node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    before = _current(root)
    turn_state.append_raw_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token="stale-owner",
        runtime="claude-code",
        payload={"hook_event_name": "UserPromptSubmit"},
    )

    daemon._adopt_turn_state_for_seat(executor, object(), NODE, binding)

    assert _current(root) == before
    assert ledger.load_wal()[-1]["event"] == "turn_state_stale_ignored"


def test_turn_end_three_way_and_blocked_message_names_every_admissible_exit(runtime):
    root, binding, node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )
    decision = turn_state.evaluate_exit(NODE, binding, runtime_root=root)
    assert decision.decision == turn_state.PROD_REQUIRED
    assert "Produce the open items" in decision.message
    assert "write an explanation and sign FAILED" in decision.message
    assert "submit a `needs_answer` question to your parent and park" in decision.message
    assert "ESCALATED" not in decision.message
    terminal = next(
        item for item in decision.checklist["items"] if item["item_id"] == "terminal_signoff"
    )
    assert "DONE or FAILED" in terminal["label"]
    assert "ESCALATED" not in terminal["label"]
    assert all("ESCALATED" not in defect for defect in terminal["defects"])

    _signal(root, "FAILED")
    assert (
        turn_state.evaluate_exit(NODE, binding, runtime_root=root).decision
        == turn_state.PRODUCT_PRESENT
    )

    _signal(root, "ESCALATED")
    assert (
        turn_state.evaluate_exit(NODE, binding, runtime_root=root).decision
        == turn_state.LEDGER_WAIT
    )

    (node_dir / "report.md").write_text(_claim_report(), encoding="utf-8")
    _signal(root, "DONE")
    assert (
        turn_state.evaluate_exit(NODE, binding, runtime_root=root).decision
        == turn_state.PRODUCT_PRESENT
    )


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        ({"gate_state": "candidate_submitted"}, "pending_gate_verdict"),
        ({"gate_state": "gate_escalated"}, "held_gate_escalation"),
        (
            {"plan_alignment_state": "semantic_cell_pending"},
            "pending_plan_alignment_semantic_cell",
        ),
        ({"plan_alignment_state": "ready"}, "pending_plan_alignment_decision"),
        (
            {
                "coordination_handoffs": {
                    "h1": {
                        "state": "submitted",
                        "response_required": True,
                    }
                }
            },
            "pending_coordination_response:h1",
        ),
    ],
)
def test_waits_are_derived_only_from_existing_binding_state(runtime, delta, expected):
    root, binding, _node_dir = runtime
    binding.update(delta)
    ledger.write_binding({NODE: binding}, _lock_held=True)
    reasons = turn_state.ledger_wait_reasons(NODE, binding, runtime_root=root)
    assert expected in reasons


def test_live_nonterminal_descendant_is_a_ledger_wait(runtime):
    root, binding, _node_dir = runtime
    child = {
        "node_address": "project/work/child#exec",
        "state": "running",
        "generation": 0,
        "lease_epoch": 1,
        "owner_token": "child-token",
        "parent_address": NODE,
    }
    ledger.write_binding({NODE: binding, child["node_address"]: child}, _lock_held=True)
    assert "live_descendant:project/work/child#exec" in turn_state.ledger_wait_reasons(
        NODE, binding, runtime_root=root
    )


def test_stale_hook_event_is_logged_but_cannot_replace_current_state(runtime):
    root, binding, _node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )
    before = _current(root)
    result = turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token="stale-token",
        runtime="claude-code",
        payload={"hook_event_name": "UserPromptSubmit"},
    )
    assert result is None
    assert _current(root) == before
    assert _events(root)[-1]["adopted"] is False
    assert _events(root)[-1]["owner_token"] == "stale-token"


def test_codex_notify_records_end_and_daemon_prod_contract(runtime):
    root, binding, _node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CODEX_TURN_END_ONLY
    )
    response = turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="codex",
        payload={"type": "agent-turn-complete", "turn-id": "turn-1"},
    )
    assert response is None
    current = _current(root)
    assert current["state"] == turn_state.TURN_ENDED
    assert current["exit_decision"] == turn_state.PROD_REQUIRED
    assert current["exit_delivery"] == "daemon_prod"


def test_read_event_tail_leaves_a_partial_final_line_for_retry(runtime):
    root, binding, _node_dir = runtime
    turn_state.seed(
        NODE, binding, runtime_root=root, profile=turn_state.CLAUDE_FULL_EDGES
    )
    path = addressing.turn_events_path(NODE, root)
    path.write_bytes(b'{"event_id":"complete"}\n{"event_id":"partial"')
    rows, offset, errors = turn_state.read_event_tail(path, 0)
    assert rows == [{"event_id": "complete"}]
    assert offset == len(b'{"event_id":"complete"}\n')
    assert errors == []


def test_daemon_adopts_valid_hook_state_once_and_journals_stale_event_once(runtime):
    root, binding, _node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "UserPromptSubmit"},
    )
    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token="stale-owner",
        runtime="claude-code",
        payload={"hook_event_name": "Stop"},
    )

    daemon._adopt_turn_state_for_seat(executor, object(), NODE, binding)
    live = ledger.read_binding(NODE)
    assert live["turn_hook_health"] == "healthy"
    assert live["turn_state"] == turn_state.TURN_RUNNING
    assert live["turn_state_hook_event"] == "UserPromptSubmit"
    rows = ledger.load_wal()
    assert [row["event"] for row in rows].count("turn_state_stale_ignored") == 1

    daemon._adopt_turn_state_for_seat(executor, object(), NODE, live)
    rows = ledger.load_wal()
    assert [row["event"] for row in rows].count("turn_state_stale_ignored") == 1
    assert [row["event"] for row in rows].count("turn_state_observed") == 0


def test_daemon_checklist_ack_does_not_erase_the_post_tool_state(runtime):
    root, binding, node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    (node_dir / "report.md").write_text(_claim_report(), encoding="utf-8")
    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "PostToolUse", "tool_use_id": "write-report"},
    )
    assert _events(root)[-1]["hook_event"] == "owed_checklist_ack"

    daemon._adopt_turn_state_for_seat(executor, object(), NODE, binding)
    live = ledger.read_binding(NODE)
    assert live["turn_state_hook_event"] == "PostToolUse"
    assert live["turn_open_item_ids"] == ["terminal_signoff"]
    assert live["turn_in_flight_tools"] == []


def test_daemon_codex_prod_replays_exact_exit_contract_once(
    runtime, monkeypatch
):
    root, binding, _node_dir = runtime
    binding.update(
        {
            "tmux_target": "harness-proj-work-exec:0.0",
            "session_uuid": "session-current",
            "transcript_path": str(root / "codex.jsonl"),
        }
    )
    binding = _activate_surface(root, binding, turn_state.CODEX_TURN_END_ONLY)
    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="codex",
        payload={"type": "agent-turn-complete", "turn-id": "turn-1"},
    )

    delivered = []

    class Tmux:
        def send_keys(self, target, message):
            delivered.append((target, message))
            return True

    monkeypatch.setattr(daemon._watchdog_mod, "prod_precondition", lambda live: True)
    daemon._adopt_turn_state_for_seat(executor, Tmux(), NODE, binding)
    assert len(delivered) == 1
    message = delivered[0][1]
    assert "Produce the open items" in message
    assert "write an explanation and sign FAILED" in message
    assert "submit a `needs_answer` question to your parent and park" in message
    assert "ESCALATED" not in message
    event_id = _current(root)["last_event_id"]
    assert _current(root)["prod_dispatched_event_id"] == event_id

    daemon._adopt_turn_state_for_seat(
        executor,
        Tmux(),
        NODE,
        ledger.read_binding(NODE),
    )
    assert len(delivered) == 1
    assert [
        row["event"] for row in ledger.load_wal()
    ].count("turn_exit_prod_delivered") == 1


def test_daemon_marks_malformed_current_degraded_for_detector_fallback(runtime):
    root, binding, _node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    addressing.turn_state_path(NODE, root).write_text("{not-json\n", encoding="utf-8")
    daemon._adopt_turn_state_for_seat(executor, object(), NODE, binding)
    live = ledger.read_binding(NODE)
    assert live["turn_hook_health"] == "degraded"
    assert "current state malformed" in live["turn_hook_error"]
    assert ledger.load_wal()[-1]["event"] == "turn_hook_degraded"


def test_malformed_callback_stays_degraded_until_a_valid_edge_recovers(runtime):
    root, binding, _node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    turn_state.record_hook_fault(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        reason="invalid JSON: torn callback",
    )
    observation = turn_state.read_current(NODE, binding, runtime_root=root)
    assert observation.status == "malformed"
    assert "torn callback" in observation.reason

    daemon._adopt_turn_state_for_seat(executor, object(), NODE, binding)
    degraded = ledger.read_binding(NODE)
    assert degraded["turn_hook_health"] == "degraded"

    turn_state.handle_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "UserPromptSubmit"},
    )
    daemon._adopt_turn_state_for_seat(executor, object(), NODE, degraded)
    recovered = ledger.read_binding(NODE)
    assert recovered["turn_hook_health"] == "healthy"
    assert recovered["turn_state"] == turn_state.TURN_RUNNING
    assert ledger.load_wal()[-1]["event"] == "turn_hook_recovered"


# ==================================================================================================
# RECREATED-LOG GUARD (live, Run-5 2026-07-31) — a stored cursor PAST the log's end means the file
# was recreated under the consumer, not that the consumer is caught up.
#
# Every spawn recreates ``.turn-events.<seat>.jsonl`` (turn_state.seed truncates it), so a binding
# whose ``turn_event_acked_offset`` exceeds the file size is indexing an incarnation that no longer
# exists on disk. Seeking there lands past EOF, reads zero rows, and re-stamps the same offset — the
# seat is permanently blind. Reading from 0 in that case is SAFE: prior-incarnation rows carry the
# prior owner_token and are fenced by the existing 'stale runtime-hook event ignored' path, and a
# recreated log's rows are the CURRENT incarnation's by construction.
#
# The guard heals seats already damaged by an earlier respawn WITHOUT requiring another respawn.
#
# Mutants killed: treat offset > size as caught-up (the live bug) -> zero rows forever; use >= (a
# fully-consumed log would be re-read every tick, re-adopting every row).
# ==================================================================================================

def test_read_event_tail_reads_from_zero_when_the_cursor_is_past_a_recreated_log(runtime):
    root, binding, _node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    turn_state.append_raw_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "UserPromptSubmit"},
    )
    path = addressing.turn_events_path(NODE, root)
    size = path.stat().st_size

    rows, offset, errors = turn_state.read_event_tail(path, size + 400_000)

    assert [row["hook_event"] for row in rows] == ["UserPromptSubmit"], (
        "a cursor beyond the log's end means the log was RECREATED (every spawn truncates it) — the "
        f"tail must be re-read from 0, not skipped (got {len(rows)} rows)"
    )
    assert offset == size, "the healed read ends at the real end of the file"
    assert errors == []

    # A cursor EXACTLY at the end is the ordinary fully-consumed case: no re-read, no rows.
    caught_up_rows, caught_up_offset, _errors = turn_state.read_event_tail(path, size)
    assert caught_up_rows == [], "offset == size is caught-up, not recreated: it must NOT re-read"
    assert caught_up_offset == size


def test_daemon_heals_a_seat_whose_stored_cursor_outran_its_recreated_log(runtime):
    """An ALREADY-damaged live seat (stale cursor from an earlier respawn) recovers on the next
    sweep — no second respawn needed."""
    root, binding, _node_dir = runtime
    binding = _activate_surface(root, binding, turn_state.CLAUDE_FULL_EDGES)
    binding["turn_event_acked_offset"] = 398_659  # the live L1#exec value, against an ~18K log
    ledger.write_binding({NODE: binding}, _lock_held=True)
    turn_state.append_raw_hook_event(
        runtime_root=root,
        node_address=NODE,
        owner_token=TOKEN,
        runtime="claude-code",
        payload={"hook_event_name": "UserPromptSubmit"},
    )

    daemon._adopt_turn_state_for_seat(executor, object(), NODE, ledger.read_binding(NODE))

    live = ledger.read_binding(NODE)
    assert live["turn_state"] == turn_state.TURN_RUNNING, (
        "the guard must let a damaged live seat consume its own rows again (a stale cursor froze "
        f"turn_state at not_started; got {live.get('turn_state')!r})"
    )
    assert live["turn_event_acked_offset"] == addressing.turn_events_path(
        NODE, root
    ).stat().st_size, "the healed cursor lands on the real file size, not the stale value"
