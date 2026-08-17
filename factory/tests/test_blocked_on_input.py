"""Owner-ratified S5 blocked-on-input classification and safe cancellation."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessd import addressing, daemon, executor, fencing, ledger, messages, reconcile, watchdog
from harnessd.spawn import tmux as tmux_transport
from harnessd.spawn.adapters.claude_code import ClaudeCodeAdapter
from harnessd.spawn.adapters.codex import CodexAdapter


NOW = "2026-07-28T12:10:00+00:00"
STALE = "2026-07-28T12:00:00+00:00"
ROOT = "L1#exec"
CHILD = "L1/area#exec"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    return tmp_path


def _binding(address: str, *, parent: str | None = None) -> dict:
    return {
        "node_address": address,
        "parent_address": parent,
        "level": "L2" if address == ROOT else "L3",
        "role_variant": "L2" if address == ROOT else "L3",
        "runtime": "claude-code",
        "state": "running",
        "liveness_state": "working",
        "generation": 4,
        "lease_epoch": 2,
        "owner_token": fencing.mint_owner_token(address, "subject", "session", 2),
        "session_uuid": f"session-{address}",
        "tmux_target": f"harness-{address.replace('/', '-')}:0.0",
        "last_applied_seq": 0,
        "stale_check_count": 1,
        "consecutive_failed_incarnations": 2,
        "failed_incarnation_causes": ["stream disconnect", "host crash"],
        "respawn_parked_at": None,
    }


def _turn(state: str = "tool_in_flight") -> dict:
    return {
        "state": state,
        "updated_at": STALE,
        "in_flight_tools": [{"tool_use_id": "tool-1", "tool_name": "Bash"}],
    }


def _blocked_probe(*, signature: str | None = "claude-choice-cancel") -> dict:
    return {
        "classification": (
            "blocked_on_input" if signature else "silent_in_flight_unconfirmed"
        ),
        "reason": "silent_zero_cpu_prompt" if signature else "prompt_signature_absent",
        "detected_at": NOW,
        "last_life_evidence_at": STALE,
        "silent_seconds": 600.0,
        "pane_pid": 412,
        "descendant_cpu": 0.0,
        "prompt_signature": signature,
        "pane_excerpt": "Do you want to proceed? | ❯ 1. Yes | Esc to cancel",
    }


def test_runtime_adapters_own_positive_prompt_signatures():
    fixture_root = Path(__file__).parent / "fixtures"
    approval = (fixture_root / "cc-2.1.152-tool-approval-pane.txt").read_text(
        encoding="utf-8"
    )
    trust = (fixture_root / "cc-2.1.152-trust-dialog-pane.txt").read_text(
        encoding="utf-8"
    )
    ask = "Choose one answer\n❯ 1. First\n  2. Second\nEnter to select · Esc to cancel\n"
    rate_limit = "Usage limit reached\n❯ Stop and wait\nAdd funds\nUpgrade\n"

    claude = ClaudeCodeAdapter.interactive_prompt_signature
    assert claude(approval) == "claude-choice-cancel"
    assert claude(trust) == "claude-choice-confirm-cancel"
    assert claude(ask) == "claude-choice-select-cancel"
    assert claude(rate_limit) == "claude-rate-limit-chooser"
    assert claude("❯ idle composer\n? for shortcuts\n") is None

    # The pinned Codex seat has approval_policy=never and no measured chooser
    # fixture. Its safe vocabulary is intentionally empty until one is captured.
    assert CodexAdapter.interactive_prompt_signature(approval) is None


def test_classifier_requires_all_three_facts_and_positive_prompt_signature():
    blocked = watchdog.classify_blocked_on_input(
        _turn(),
        latest_life_evidence_at=STALE,
        pane_alive=True,
        pane_pid=412,
        descendant_cpu=0.0,
        pane_text="Do you want to proceed?\n❯ 1. Yes\nEsc to cancel",
        prompt_signature="claude-choice-cancel",
        now=NOW,
    )
    assert blocked["classification"] == "blocked_on_input"
    assert blocked["silent_seconds"] == 600.0

    waiting = _turn("waiting_on_human")
    waiting["waiting_on_human_tool_name"] = "AskUserQuestion"
    asked = watchdog.classify_blocked_on_input(
        waiting,
        latest_life_evidence_at=STALE,
        pane_alive=True,
        pane_pid=412,
        descendant_cpu=0.0,
        pane_text="❯ 1. Answer\nEnter to select · Esc to cancel",
        prompt_signature="claude-choice-select-cancel",
        now=NOW,
    )
    assert asked["classification"] == "blocked_on_input"

    unconfirmed = watchdog.classify_blocked_on_input(
        _turn(),
        latest_life_evidence_at=STALE,
        pane_alive=True,
        pane_pid=412,
        descendant_cpu=0.0,
        pane_text="waiting on HTTPS response",
        prompt_signature=None,
        now=NOW,
    )
    assert unconfirmed["classification"] == "silent_in_flight_unconfirmed"


@pytest.mark.parametrize(
    ("latest", "alive", "pane_pid", "cpu", "expected_reason"),
    [
        ("2026-07-28T12:09:00+00:00", True, 412, 0.0, "life_evidence_within_window"),
        (STALE, True, 412, 1.5, "active_descendant_process"),
        (STALE, True, 412, None, "process_probe_unknown"),
        (STALE, False, None, None, "pane_not_live"),
    ],
)
def test_classifier_never_cancels_fresh_work_active_process_or_unknown_probe(
    latest, alive, pane_pid, cpu, expected_reason
):
    result = watchdog.classify_blocked_on_input(
        _turn(),
        latest_life_evidence_at=latest,
        pane_alive=alive,
        pane_pid=pane_pid,
        descendant_cpu=cpu,
        pane_text="Do you want to proceed?\n❯ 1. Yes\nEsc to cancel",
        prompt_signature="claude-choice-cancel",
        now=NOW,
    )
    assert result["classification"] in {"healthy", "unknown"}
    assert result["reason"] == expected_reason


def test_probe_skips_process_and_pane_reads_until_silence_prerequisites_hold(
    runtime, monkeypatch
):
    child = _binding(CHILD, parent=ROOT)
    monkeypatch.setattr(
        watchdog.turn_state,
        "read_current",
        lambda *_args, **_kwargs: SimpleNamespace(status="valid", payload=_turn()),
    )
    monkeypatch.setattr(
        watchdog,
        "_latest_life_evidence",
        lambda *_args, **_kwargs: ("turn_state", "2026-07-28T12:09:00+00:00"),
    )

    process_reads: list[str] = []

    def counted_process_read(*_args, **_kwargs):
        process_reads.append("pane_alive")
        return True, 412

    monkeypatch.setattr(watchdog.detector_signals, "pane_alive", counted_process_read)

    result = watchdog.probe_blocked_on_input(child, child, now=NOW)

    assert result == {
        "classification": "healthy",
        "reason": "life_evidence_within_window",
        "silent_seconds": 60.0,
    }
    assert process_reads == []


def test_daemon_tick_honors_terminal_truth_before_stall_probe(runtime, monkeypatch):
    child = _binding(CHILD, parent=ROOT)
    ledger.write_binding({CHILD: child}, _lock_held=True)
    monkeypatch.setattr(
        daemon._watchdog_mod,
        "check_terminal_signal",
        lambda *_args, **_kwargs: watchdog.WatchdogAction(
            kind=watchdog.COLLAPSE,
            node=CHILD,
            detail={"reason": "signal_DONE"},
        ),
    )
    monkeypatch.setattr(
        daemon._watchdog_mod,
        "probe_blocked_on_input",
        lambda *_args, **_kwargs: pytest.fail(
            "a terminal signal must win before blocked-input probing"
        ),
    )
    monkeypatch.setattr(daemon, "_wake_on_unacked_inbox", lambda *_args, **_kwargs: None)

    daemon._watchdog_tick(executor, _CancelTmux(), detector=None)


def test_daemon_tick_routes_stall_before_leaf_nonresponse_ladder(runtime, monkeypatch):
    child = _binding(CHILD, parent=ROOT)
    ledger.write_binding({CHILD: child}, _lock_held=True)
    calls: list[str] = []
    monkeypatch.setattr(
        daemon._watchdog_mod,
        "check_terminal_signal",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        daemon._watchdog_mod,
        "probe_blocked_on_input",
        lambda *_args, **_kwargs: _blocked_probe(),
    )
    monkeypatch.setattr(
        daemon,
        "_act_on_blocked_input",
        lambda *_args, **_kwargs: calls.append("stall") or True,
    )
    monkeypatch.setattr(
        daemon._watchdog_mod,
        "check_leaf",
        lambda *_args, **_kwargs: pytest.fail(
            "blocked input must not enter the idle/nonresponse ladder"
        ),
    )
    monkeypatch.setattr(daemon, "_wake_on_unacked_inbox", lambda *_args, **_kwargs: None)

    daemon._watchdog_tick(executor, _CancelTmux(), detector=None)

    assert calls == ["stall"]


class _CancelTmux:
    def __init__(self):
        self.cancelled: list[str] = []

    def send_cancel(self, target: str) -> bool:
        self.cancelled.append(target)
        return True


def _act(tmux, address: str, snapshot: dict, probe: dict) -> bool:
    return daemon._act_on_blocked_input(
        executor,
        tmux,
        address,
        snapshot,
        probe,
        recheck=lambda _live: probe,
    )


def test_positive_incident_cancels_once_and_delivers_factual_system_notice(runtime):
    root = _binding(ROOT)
    child = _binding(CHILD, parent=ROOT)
    ledger.write_binding({ROOT: root, CHILD: child}, _lock_held=True)
    tmux = _CancelTmux()

    handled = _act(tmux, CHILD, child, _blocked_probe())

    assert handled is True
    assert tmux.cancelled == [child["tmux_target"]]
    live = ledger.read_binding(CHILD)
    assert live["seat_stall_active"] is True
    assert live["seat_stall_cancel_status"] == "sent"
    assert live["seat_stall_positive_incident_count"] == 1
    assert live["stale_check_count"] == 1
    assert live["consecutive_failed_incarnations"] == 2
    assert live["failed_incarnation_causes"] == ["stream disconnect", "host crash"]
    assert live["respawn_parked_at"] is None

    stalls = [row for row in ledger.load_wal() if row.get("event") == "seat_stalled"]
    actions = [
        row for row in ledger.load_wal() if row.get("event") == "seat_stall_actioned"
    ]
    assert len(stalls) == 1
    assert len(actions) == 1
    assert stalls[0]["binding_delta"]["seat_stall_cancel_status"] == "pending"
    assert actions[0]["binding_delta"]["seat_stall_cancel_status"] == "sent"

    system_records = [
        row
        for row in (live.get("messages") or {}).values()
        if row.get("source") == messages.SYSTEM_SOURCE
    ]
    assert len(system_records) == 1
    record = system_records[0]
    assert record["direction"] == "system"
    artifact = addressing.node_dir(CHILD, runtime) / record["artifact"]
    content = artifact.read_text(encoding="utf-8")
    assert content.startswith("[HARNESS SYSTEM NOTICE]")
    assert "sent Escape" in content
    assert "cannot answer or approve on your behalf" in content
    assert "No option was selected" in content
    assert "`.tmp`" in content
    assert "you should" not in content.lower()

    inbox_rows = [
        json.loads(line)
        for line in addressing.inbox_path(CHILD, runtime).read_text(encoding="utf-8").splitlines()
    ]
    assert [(row["sender"], row["message_id"]) for row in inbox_rows] == [
        (messages.SYSTEM_SOURCE, record["message_id"])
    ]

    # A steady poll cannot repeat the event, key, durable record, or inbox pointer.
    assert _act(tmux, CHILD, live, _blocked_probe())
    assert tmux.cancelled == [child["tmux_target"]]
    assert len([row for row in ledger.load_wal() if row.get("event") == "seat_stalled"]) == 1
    assert len(
        addressing.inbox_path(CHILD, runtime).read_text(encoding="utf-8").splitlines()
    ) == 1


def test_unsigned_silent_wait_is_visible_but_never_cancelled_or_budgeted(runtime):
    child = _binding(CHILD, parent=ROOT)
    ledger.write_binding({ROOT: _binding(ROOT), CHILD: child}, _lock_held=True)
    tmux = _CancelTmux()

    assert _act(tmux, CHILD, child, _blocked_probe(signature=None))

    live = ledger.read_binding(CHILD)
    assert tmux.cancelled == []
    assert live["seat_stall_classification"] == "silent_in_flight_unconfirmed"
    assert live["seat_stall_cancel_status"] == "not_attempted"
    assert live["seat_stall_positive_incident_count"] == 0
    assert (live.get("messages") or {}) == {}


def test_pending_write_ahead_intent_never_replays_a_second_escape(runtime):
    child = _binding(CHILD, parent=ROOT)
    ledger.write_binding({ROOT: _binding(ROOT), CHILD: child}, _lock_held=True)
    result = executor.record_seat_stall(
        CHILD,
        expected_owner_token=child["owner_token"],
        incident_id="stall-pending",
        detected_at=NOW,
        classification="blocked_on_input",
        silent_seconds=600.0,
        pane_excerpt="approval prompt",
        prompt_signature="claude-choice-cancel",
        cancel_status="pending",
        positive_incident_count=1,
        retriggered=False,
        escalated=False,
    )
    assert result.ok
    tmux = _CancelTmux()

    assert _act(tmux, CHILD, result.binding, _blocked_probe())

    live = ledger.read_binding(CHILD)
    assert tmux.cancelled == []
    assert live["seat_stall_cancel_status"] == "pending"
    assert live["seat_stall_positive_incident_count"] == 1
    assert [row["event"] for row in ledger.load_wal()].count("seat_stalled") == 1


def test_two_recoveries_rearm_then_third_incident_escalates_without_cancel(runtime):
    root = _binding(ROOT)
    child = _binding(CHILD, parent=ROOT)
    ledger.write_binding({ROOT: root, CHILD: child}, _lock_held=True)
    tmux = _CancelTmux()

    for expected_count in (1, 2):
        snapshot = ledger.read_binding(CHILD)
        assert _act(tmux, CHILD, snapshot, _blocked_probe())
        assert ledger.read_binding(CHILD)["seat_stall_positive_incident_count"] == expected_count
        recovered = executor.recover_seat_stall(
            CHILD,
            expected_owner_token=child["owner_token"],
            incident_id=ledger.read_binding(CHILD)["seat_stall_incident_id"],
            recovered_at=f"2026-07-28T12:{10 + expected_count}:00+00:00",
            reason="life_evidence_resumed",
        )
        assert recovered.ok

    snapshot = ledger.read_binding(CHILD)
    assert _act(tmux, CHILD, snapshot, _blocked_probe())
    live = ledger.read_binding(CHILD)
    assert tmux.cancelled == [child["tmux_target"], child["tmux_target"]]
    assert live["seat_stall_positive_incident_count"] == 3
    assert live["seat_stall_escalated"] is True
    assert live["seat_stall_cancel_status"] == "not_attempted"

    parent_messages = [
        row
        for row in (ledger.read_binding(ROOT).get("messages") or {}).values()
        if row.get("source") == messages.SYSTEM_SOURCE
    ]
    assert len(parent_messages) == 1
    parent_content = (
        addressing.node_dir(ROOT, runtime) / parent_messages[0]["artifact"]
    ).read_text(encoding="utf-8")
    assert CHILD in parent_content
    assert "three blocked interactive prompts" in parent_content


def test_l1_third_incident_is_visible_root_limit_without_fabricated_owner_question(runtime):
    root = _binding(ROOT)
    root["seat_stall_positive_incident_count"] = 2
    ledger.write_binding({ROOT: root}, _lock_held=True)
    tmux = _CancelTmux()

    assert _act(tmux, ROOT, root, _blocked_probe())

    live = ledger.read_binding(ROOT)
    assert tmux.cancelled == []
    assert live["seat_stall_escalated"] is True
    assert live["seat_stall_root_limit"] is True
    assert (live.get("messages") or {}) == {}


def test_stall_edges_replay_the_binding_slice_byte_for_byte(runtime):
    child = _binding(CHILD, parent=ROOT)
    initial = json.loads(json.dumps(child))
    ledger.write_binding({CHILD: child}, _lock_held=True)

    first = executor.record_seat_stall(
        CHILD,
        expected_owner_token=child["owner_token"],
        incident_id="stall-replay",
        detected_at=NOW,
        classification="blocked_on_input",
        silent_seconds=600.0,
        pane_excerpt="approval prompt",
        prompt_signature="claude-choice-cancel",
        cancel_status="pending",
        positive_incident_count=1,
        retriggered=False,
        escalated=False,
    )
    assert first.ok
    acted = executor.record_seat_stall_action(
        CHILD,
        expected_owner_token=child["owner_token"],
        incident_id="stall-replay",
        actioned_at=NOW,
        cancel_status="sent",
    )
    assert acted.ok
    recovered = executor.recover_seat_stall(
        CHILD,
        expected_owner_token=child["owner_token"],
        incident_id="stall-replay",
        recovered_at=NOW,
        reason="life_evidence_resumed",
    )
    assert recovered.ok

    rebuilt = reconcile.replay_wal({CHILD: initial}, ledger.load_wal())
    live_binding = ledger.read_binding(CHILD)
    differences = {
        key: (rebuilt[CHILD].get(key), live_binding.get(key))
        for key in sorted(set(rebuilt[CHILD]) | set(live_binding))
        if rebuilt[CHILD].get(key) != live_binding.get(key)
    }
    assert differences == {}


def test_cancel_key_is_exclusive_with_literal_text_and_enter(monkeypatch):
    calls: list[tuple[str, ...]] = []
    literal_started = threading.Event()
    release_literal = threading.Event()

    def fake_run(args, *, check=True):
        call = tuple(args)
        calls.append(call)
        if "-l" in call:
            literal_started.set()
            assert release_literal.wait(timeout=2)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tmux_transport, "_run", fake_run)
    text_thread = threading.Thread(
        target=tmux_transport.send_keys,
        args=("harness-seat:0.0", "one durable wake"),
    )
    cancel_thread = threading.Thread(
        target=tmux_transport.send_cancel,
        args=("harness-seat:0.0",),
    )

    text_thread.start()
    assert literal_started.wait(timeout=1)
    cancel_thread.start()
    time.sleep(0.05)
    assert not any("Escape" in call for call in calls)
    release_literal.set()
    text_thread.join(timeout=2)
    cancel_thread.join(timeout=2)

    assert calls == [
        ("send-keys", "-t", "harness-seat:0.0", "-l", "one durable wake"),
        ("send-keys", "-t", "harness-seat:0.0", "Enter"),
        ("send-keys", "-t", "harness-seat:0.0", "Escape"),
    ]
