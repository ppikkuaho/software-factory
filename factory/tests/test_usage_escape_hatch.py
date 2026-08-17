"""The usage escape hatch — three layers, RED first (DEFERRED-REGISTER "Usage escape hatch RULED",
owner 2026-08-01, deliberated with steering over the r5 IPC-wedge incident).

The motivating incident: a 14h half-dead daemon kept waking seats into a world where oversight was
blind. The owner goal pair — never accidentally kill an autonomous end-to-end run, AND never let
agents burn usage against a broken substrate — is served by three layers whose strongest automated
action is "hold and tell someone":

  1. SEAT-SIDE FAIL-FAST LAW (prose) — the same substrate command failing three consecutive times
     with the same error class stops the retry loop, records a canonical substrate-fault marker,
     and ends the turn (ending the turn stops the meter). Carried by the all-seat
     decision-delivery-signoff block, so every level and the review handbook get it verbatim.

  2. SELF-CLEARING WAKE HOLD — once per sweep the daemon probes its OWN control socket; a failed
     probe gates ISSUING NEW WAKES and nothing else (mid-turn seats finish, nothing is cancelled,
     no binding is mutated, non-wake daemon work continues); the moment the probe passes, wakes
     resume with no human involved. WAL rows land ONLY on the two transitions — the standing
     over-logging diet forbids a row per probe.

  3. FREEZE DETECTOR + ESCALATION LADDER — run-level quiescence (no live seat producing life
     evidence, no deliverable wake outstanding, no spawn in flight, run neither terminal nor
     paused) persisting 45 minutes is a freeze. The ladder fires each rung only if the one below
     fails: durable ``run_frozen`` + evidence snapshot -> bus-lite notice to the director peers ->
     a bounded 15-minute ack window served by the new ``escalation-ack`` verb -> unacked expiry
     raises a macOS notification and sets the pause flag through the EXISTING pause machinery.
     One-shot per episode: a new episode requires quiescence to have cleared and re-persisted.

Style: real ledger/executor on a tmp RUNTIME_ROOT and real AF_UNIX sockets (the
test_control_plane_hardening.py pattern). No model usage, no real tmux, no real bus-lite/osascript.
"""

from __future__ import annotations

import copy
import json
import socket
import threading
from pathlib import Path

import pytest

import harnessd.daemon as daemon
import harnessd.detector_signals as detector_signals
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.harnessctl as harnessctl
import harnessd.ipc as ipc
import harnessd.ledger as ledger
import harnessd.wal_policy as wal_policy
import harnessd.watchdog as watchdog


REPO_ROOT = Path(__file__).resolve().parent.parent
L1 = "L1#exec"
LEAF = "proj/widget#exec"


# ===========================================================================
# Fixtures / helpers
# ===========================================================================

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(detector_signals, "_size_cache", {}, raising=False)
    monkeypatch.setattr(daemon, "_WAKE_HOLD", {"degraded": False, "reason": None}, raising=False)
    monkeypatch.setattr(
        daemon, "_FREEZE", {"quiescent_since": None, "episode": None}, raising=False
    )
    monkeypatch.setattr(daemon, "_LIFE_EVIDENCE_SEEN", {}, raising=False)
    monkeypatch.setattr(daemon, "_IPC_SERVING", False, raising=False)
    return tmp_path


def _binding(node_address=LEAF, *, state="running", parent=None, level="L5",
             transcript_path=None, extra=None):
    token = fencing.mint_owner_token(node_address, "subagent-x", "sess-x", 1)
    rec = {
        "node_address": node_address,
        "parent_address": parent,
        "level": level,
        "subagent_id": "subagent-x",
        "session_uuid": "sess-x",
        "tmux_target": "harness:t.0",
        "state": state,
        "generation": 0,
        "lease_epoch": 1,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "working",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": str(transcript_path) if transcript_path else None,
    }
    if extra:
        rec.update(extra)
    return rec


def _seed(*bindings):
    ledger.write_binding(
        {b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True
    )


def _events(event=None):
    rows = ledger.load_wal()
    if event is not None:
        return [r for r in rows if r.get("event") == event]
    return [r.get("event") for r in rows]


def _minutes_ago(minutes: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


class _Serving:
    """A real AF_UNIX control socket served by ``ipc.serve_one`` on a background thread."""

    def __init__(self, runtime_root, *, answer=True):
        self.listener = daemon.make_ipc_listener(runtime_root)
        self.answer = answer
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                if self.answer:
                    ipc.serve_one(self.listener)
                else:
                    conn, _ = self.listener.accept()
                    # Accept and hold: the peer gets a connection and never a response — the
                    # stalled-control-plane shape the probe must survive on its own bound.
                    threading.Event().wait(30)
                    conn.close()
            except OSError:
                return

    def close(self):
        try:
            self.listener.close()
        except OSError:
            pass


# ===========================================================================
# Layer 1 — the seat-side fail-fast law lives in the all-seat shared block.
# ===========================================================================

_LAW_MARKERS = (
    "three consecutive",
    "same error class",
    "substrate-fault marker",
    "substrate-fault.json",
    "end the turn",
)


def test_fail_fast_law_lives_in_the_all_seat_shared_block():
    """The law is prose in the shared block source — not per-level craft anyone can drift."""
    source = (
        REPO_ROOT / "operational/shared/blocks/decision-delivery-signoff.md"
    ).read_text(encoding="utf-8")
    missing = [marker for marker in _LAW_MARKERS if marker not in source]
    assert not missing, (
        f"the substrate fail-fast law is not stated in the shared block: missing {missing}"
    )


def test_every_carrier_doc_carries_the_fail_fast_law():
    """Every registered carrier of the block must hold the rendered law (all seats, one voice)."""
    registry = json.loads(
        (REPO_ROOT / "operational/shared/blocks/registry.json").read_text(encoding="utf-8")
    )
    carriers = registry["blocks"]["decision-delivery-signoff"]["carriers"]
    assert carriers, "the block must still have carriers"
    for carrier in carriers:
        text = (REPO_ROOT / carrier["doc"]).read_text(encoding="utf-8")
        for marker in _LAW_MARKERS:
            assert marker in text, f"{carrier['doc']} is missing the fail-fast law ({marker!r})"


# ===========================================================================
# Layer 2 — the self-clearing wake hold.
# ===========================================================================

def test_probe_round_trips_the_real_control_socket(runtime):
    server = _Serving(runtime)
    try:
        ok, reason = daemon.probe_control_socket(runtime)
    finally:
        server.close()
    assert ok is True and reason is None, f"a served control socket must probe healthy: {reason}"


def test_probe_fails_when_nobody_serves_the_socket(runtime):
    ok, reason = daemon.probe_control_socket(runtime)
    assert ok is False and reason, "an unserved control socket must probe as a fault, with a reason"


def test_probe_is_bounded_by_its_own_short_timeout(runtime, monkeypatch):
    """A peer that accepts and never answers must not park the sweep — the probe owns its bound."""
    monkeypatch.setattr(daemon, "SELF_PROBE_TIMEOUT_S", 0.25)
    server = _Serving(runtime, answer=False)
    try:
        import time

        started = time.monotonic()
        ok, reason = daemon.probe_control_socket(runtime)
        elapsed = time.monotonic() - started
    finally:
        server.close()
    assert ok is False and reason
    assert elapsed < 5.0, f"the probe must be bounded; took {elapsed:.1f}s"


def test_default_probe_timeout_is_short(runtime):
    assert 0 < daemon.SELF_PROBE_TIMEOUT_S <= 5.0


def test_failed_probe_engages_the_hold_and_journals_exactly_one_transition_row(runtime):
    _seed(_binding())
    daemon._IPC_SERVING = True
    for _ in range(4):
        daemon._wake_hold_sweep_best_effort()
    assert daemon.wake_hold_engaged() is True
    assert len(_events("wake_hold_engaged")) == 1, (
        "the WAL diet allows a row on the TRANSITION only — never one per probe"
    )
    assert not _events("wake_hold_cleared")


def test_passing_probe_clears_the_hold_and_journals_exactly_one_transition_row(runtime):
    _seed(_binding())
    daemon._IPC_SERVING = True
    daemon._wake_hold_sweep_best_effort()  # degraded (nobody serving yet)
    assert daemon.wake_hold_engaged() is True
    server = _Serving(runtime)
    try:
        for _ in range(3):
            daemon._wake_hold_sweep_best_effort()
    finally:
        server.close()
    assert daemon.wake_hold_engaged() is False, "a passing probe resumes wakes with no human"
    assert len(_events("wake_hold_cleared")) == 1
    cleared = _events("wake_hold_cleared")[0]
    assert cleared.get("binding_delta", {}).get("reason"), "the cleared row records the prior reason"


def test_engaged_row_records_the_probe_failure_reason(runtime):
    _seed(_binding())
    daemon._IPC_SERVING = True
    daemon._wake_hold_sweep_best_effort()
    row = _events("wake_hold_engaged")[0]
    assert row.get("binding_delta", {}).get("reason"), "the degraded row must carry its reason"
    assert row.get("node_address") is None, "the hold is daemon-level, not node-level"


def test_the_probe_is_skipped_when_this_process_serves_no_control_socket(runtime):
    """The daemon probes its OWN socket: a process that serves none has nothing to probe."""
    _seed(_binding())
    daemon._wake_hold_sweep_best_effort()
    assert daemon.wake_hold_engaged() is False
    assert not _events("wake_hold_engaged")


def _wake_fixture(runtime):
    """A seat with an unacked inbox row and an idle pane — a wake is owed and deliverable."""
    transcript = runtime / "t.jsonl"
    transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
    binding = _binding(transcript_path=transcript)
    _seed(binding)
    return binding


class _RecordingTmux:
    def __init__(self):
        self.sent = []

    def send_keys(self, target, keys):
        self.sent.append((target, keys))
        return True


def test_the_hold_gates_new_wakes_and_mutates_nothing(runtime, monkeypatch):
    binding = _wake_fixture(runtime)
    monkeypatch.setattr(
        watchdog,
        "unconsumed_inbox_notification",
        lambda node, bnd: {"should_wake": True, "covered_offset": 42, "sender_counts": [("l4", 1)], "count": 1},
    )
    monkeypatch.setattr(watchdog, "prod_precondition", lambda node: True)
    tmux = _RecordingTmux()
    daemon._WAKE_HOLD["degraded"] = True
    before = copy.deepcopy(ledger.read_binding(LEAF))
    seq_before = ledger.next_seq()

    daemon._wake_on_unacked_inbox(executor, tmux, LEAF, binding)

    assert tmux.sent == [], "a degraded control plane must issue NO new wake"
    assert ledger.read_binding(LEAF) == before, "the hold mutates no binding"
    assert ledger.next_seq() == seq_before, "the hold appends no WAL row of its own"


def test_clearing_the_hold_resumes_wakes(runtime, monkeypatch):
    binding = _wake_fixture(runtime)
    monkeypatch.setattr(
        watchdog,
        "unconsumed_inbox_notification",
        lambda node, bnd: {"should_wake": True, "covered_offset": 42, "sender_counts": [("l4", 1)], "count": 1},
    )
    monkeypatch.setattr(watchdog, "prod_precondition", lambda node: True)
    tmux = _RecordingTmux()
    daemon._WAKE_HOLD["degraded"] = False

    daemon._wake_on_unacked_inbox(executor, tmux, LEAF, binding)

    assert tmux.sent, "with the probe passing the ordinary wake path is untouched"


def test_poll_once_runs_the_self_probe_once_per_sweep(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "_wake_hold_sweep_best_effort", lambda: calls.append("probe"))
    monkeypatch.setattr(daemon, "_freeze_sweep_best_effort", lambda: None)
    for name in (
        "_turn_state_sweep_best_effort", "_route_reconcile_escalations", "_watchdog_tick",
        "_service_outboxes_best_effort", "_redrive_planned_spawns_best_effort",
        "_dispatch_review_check_seats_best_effort", "_submit_message_markers_best_effort",
        "_service_contract_rebinds_best_effort", "_deliver_contract_amendments_best_effort",
        "_recover_message_state_best_effort", "_submit_coordination_handoff_markers_best_effort",
        "_recover_coordination_handoff_notifications_best_effort",
        "_submit_plan_alignment_markers_best_effort", "_reconcile_plan_alignment_cells_best_effort",
        "_reconcile_plan_alignment_elevations_best_effort",
        "_recover_plan_alignment_notifications_best_effort",
        "_recover_gate_notifications_best_effort", "_recover_terminal_notifications_best_effort",
        "_stamp_last_tick_best_effort",
    ):
        monkeypatch.setattr(daemon, name, lambda *a, **k: None)
    monkeypatch.setattr(daemon._reconcile_mod, "reconcile_tick", lambda *a, **k: None)

    daemon.poll_once(executor, None, None)

    assert calls == ["probe"], "exactly one control-socket self-probe per sweep"


# ===========================================================================
# Layer 3 — quiescence.
# ===========================================================================

def _quiescent_seat(runtime, **kwargs):
    transcript = runtime / "quiet.jsonl"
    transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
    return _binding(transcript_path=transcript, **kwargs)


def _no_wakes_owed(monkeypatch):
    monkeypatch.setattr(watchdog, "unconsumed_inbox_notification", lambda node, bnd: None)


def test_a_still_run_reads_quiescent(runtime, monkeypatch):
    _seed(_quiescent_seat(runtime))
    _no_wakes_owed(monkeypatch)
    daemon._run_quiescence(now=daemon._clock.now_utc())  # first read establishes the baseline
    quiescent, evidence = daemon._run_quiescence(now=daemon._clock.now_utc())
    assert quiescent is True, "no life evidence, no owed wake, nothing in flight -> quiescent"
    assert [seat["address"] for seat in evidence["seats"]] == [LEAF]


def test_growing_life_evidence_breaks_quiescence(runtime, monkeypatch):
    transcript = runtime / "busy.jsonl"
    transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
    _seed(_binding(transcript_path=transcript))
    _no_wakes_owed(monkeypatch)
    daemon._run_quiescence(now=daemon._clock.now_utc())
    daemon._run_quiescence(now=daemon._clock.now_utc())

    import os
    import time

    time.sleep(0.01)
    with open(transcript, "a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')
    os.utime(transcript, None)

    quiescent, _ = daemon._run_quiescence(now=daemon._clock.now_utc())
    assert quiescent is False, "a seat still producing life evidence is not a frozen run"


def test_a_deliverable_wake_breaks_quiescence(runtime, monkeypatch):
    _seed(_quiescent_seat(runtime))
    monkeypatch.setattr(
        watchdog,
        "unconsumed_inbox_notification",
        lambda node, bnd: {"should_wake": True, "covered_offset": 1, "sender_counts": [("l4", 1)], "count": 1},
    )
    daemon._run_quiescence(now=daemon._clock.now_utc())
    quiescent, _ = daemon._run_quiescence(now=daemon._clock.now_utc())
    assert quiescent is False, "the daemon still has a lever to pull — not frozen"


def test_an_exhausted_wake_budget_does_not_hide_the_freeze(runtime, monkeypatch):
    """The third-strike unattended stall is exactly the case the ladder exists to cover."""
    _seed(_quiescent_seat(runtime, extra={"wake_attempt_count": 3}))
    monkeypatch.setattr(
        watchdog,
        "unconsumed_inbox_notification",
        lambda node, bnd: {"should_wake": True, "covered_offset": 1, "sender_counts": [("l4", 1)], "count": 1},
    )
    daemon._run_quiescence(now=daemon._clock.now_utc())
    quiescent, evidence = daemon._run_quiescence(now=daemon._clock.now_utc())
    assert quiescent is True, "an undeliverable wake is not a lever — it is the freeze"
    assert evidence["owed"], "the owed item must still show up in the evidence snapshot"


def test_a_spawn_in_flight_breaks_quiescence(runtime, monkeypatch):
    _seed(_quiescent_seat(runtime), _binding("proj/other#exec", state="planned"))
    _no_wakes_owed(monkeypatch)
    daemon._run_quiescence(now=daemon._clock.now_utc())
    quiescent, _ = daemon._run_quiescence(now=daemon._clock.now_utc())
    assert quiescent is False


def test_a_paused_run_is_never_quiescent(runtime, monkeypatch):
    _seed(_quiescent_seat(runtime, extra={"paused_at": daemon._clock.now_utc()}))
    _no_wakes_owed(monkeypatch)
    daemon._run_quiescence(now=daemon._clock.now_utc())
    quiescent, _ = daemon._run_quiescence(now=daemon._clock.now_utc())
    assert quiescent is False, "pause is the proven-lossless state, not a freeze"


def test_a_terminal_run_is_never_quiescent(runtime, monkeypatch):
    _seed(_binding(state="done"))
    _no_wakes_owed(monkeypatch)
    daemon._run_quiescence(now=daemon._clock.now_utc())
    quiescent, _ = daemon._run_quiescence(now=daemon._clock.now_utc())
    assert quiescent is False, "a finished run is not a frozen one"


# ===========================================================================
# Layer 3 — the escalation ladder.
# ===========================================================================

@pytest.fixture
def frozen_run(runtime, monkeypatch):
    """A quiescent run whose quiescence has already persisted past the freeze window."""
    _seed(
        _binding(L1, level="L1", state="running"),
        _quiescent_seat(runtime, parent=L1),
    )
    _no_wakes_owed(monkeypatch)
    daemon._run_quiescence(now=daemon._clock.now_utc())
    daemon._FREEZE["quiescent_since"] = _minutes_ago(46)
    return runtime


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake_run(argv, **kwargs):
        recorded.append((list(argv), kwargs))

        class _Completed:
            returncode = 0
            stdout = b""
            stderr = b""

        return _Completed()

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    return recorded


def test_freeze_does_not_fire_before_the_window(runtime, monkeypatch, calls):
    _seed(_quiescent_seat(runtime))
    _no_wakes_owed(monkeypatch)
    daemon._run_quiescence(now=daemon._clock.now_utc())
    daemon._FREEZE["quiescent_since"] = _minutes_ago(44)
    daemon._freeze_sweep_best_effort()
    assert not _events("run_frozen"), "45 minutes is the constant; 44 is not a freeze"
    assert calls == []


def test_the_freeze_window_is_forty_five_minutes():
    assert daemon.FREEZE_QUIESCENCE_S == 45 * 60


def test_the_ack_window_is_fifteen_minutes():
    assert daemon.FREEZE_ACK_WINDOW_S == 15 * 60


def test_rung_a_writes_a_durable_run_frozen_event_with_an_evidence_snapshot(frozen_run, calls):
    daemon._freeze_sweep_best_effort()
    rows = _events("run_frozen")
    assert len(rows) == 1, "one durable run_frozen per episode"
    delta = rows[0].get("binding_delta", {})
    assert delta.get("quiescent_since"), "the snapshot names when quiescence began"
    assert [seat["address"] for seat in delta.get("seats", [])], "the snapshot names the seats"
    assert "owed" in delta, "the snapshot names the owed items"


def test_rung_a_is_one_shot_per_episode(frozen_run, calls):
    daemon._freeze_sweep_best_effort()
    daemon._freeze_sweep_best_effort()
    daemon._freeze_sweep_best_effort()
    assert len(_events("run_frozen")) == 1


def test_rung_b_notifies_each_configured_director_peer_through_bus_lite(frozen_run, calls):
    daemon._freeze_sweep_best_effort()
    sends = [argv for argv, _ in calls if len(argv) > 1 and argv[1] == "send"]
    assert [argv[2] for argv in sends] == list(daemon.DIRECTOR_PEERS)
    for argv in sends:
        assert argv[0] == daemon.BUS_LITE_PATH
        assert "run_frozen" in argv[3], "the notice must point at the durable WAL event"
        assert "\n" not in argv[3], "one line, pointer not payload"


def test_the_director_peers_are_the_ratified_pair():
    assert daemon.DIRECTOR_PEERS == ("fable-run5-director", "fable-l1-l5-director")


def test_a_bus_lite_send_failure_is_non_fatal_and_recorded(frozen_run, monkeypatch):
    def exploding_run(argv, **kwargs):
        raise OSError("bus-lite is not on this machine")

    monkeypatch.setattr(daemon.subprocess, "run", exploding_run)
    daemon._freeze_sweep_best_effort()  # must not raise
    assert _events("run_frozen"), "rung (a) still landed"
    assert len(_events("run_freeze_notify_failed")) == len(daemon.DIRECTOR_PEERS)
    assert daemon._FREEZE["episode"] is not None, "the ladder keeps climbing"


def test_rung_d_waits_out_the_ack_window(frozen_run, calls):
    daemon._freeze_sweep_best_effort()
    calls.clear()
    daemon._freeze_sweep_best_effort()
    assert not any("osascript" in argv[0] for argv, _ in calls), "the ack window is still open"
    assert ledger.read_binding(L1)["paused_at"] is None


def test_rung_d_notifies_the_user_and_pauses_through_the_existing_pause_machinery(
    frozen_run, calls
):
    daemon._freeze_sweep_best_effort()
    daemon._FREEZE["episode"]["declared_at"] = _minutes_ago(16)
    calls.clear()

    daemon._freeze_sweep_best_effort()

    assert any(argv[0] == "osascript" for argv, _ in calls), "the user gets a macOS notification"
    assert ledger.read_binding(L1)["paused_at"] is not None, (
        "the pause flag is set through the existing pause machinery"
    )
    assert "paused" in _events(), "the pause rides the real pause verb's WAL row"
    assert len(_events("run_freeze_paused")) == 1


def test_rung_d_is_one_shot(frozen_run, calls):
    daemon._freeze_sweep_best_effort()
    daemon._FREEZE["episode"]["declared_at"] = _minutes_ago(16)
    daemon._freeze_sweep_best_effort()
    daemon._freeze_sweep_best_effort()
    daemon._freeze_sweep_best_effort()
    assert len(_events("run_freeze_paused")) == 1
    assert len(_events("run_frozen")) == 1


def test_an_ack_closes_the_episode_durably_and_stops_the_ladder(frozen_run, calls):
    daemon._freeze_sweep_best_effort()
    response = ipc.handle_request({"command": "escalation-ack"})
    assert response["ok"] is True, response
    assert len(_events("run_freeze_acked")) == 1, "the ack is durable"

    daemon._FREEZE["episode"]["declared_at"] = _minutes_ago(30)
    calls.clear()
    daemon._freeze_sweep_best_effort()

    assert not any(argv[0] == "osascript" for argv, _ in calls), "an acked episode never escalates"
    assert ledger.read_binding(L1)["paused_at"] is None
    assert not _events("run_freeze_paused")


def test_acking_with_no_pending_escalation_is_a_structured_refusal(runtime):
    response = ipc.handle_request({"command": "escalation-ack"})
    assert response["ok"] is False and response["errors"]
    assert response.get("command") == "escalation-ack"
    assert any("escalation" in str(error) for error in response["errors"])
    assert not _events("run_freeze_acked")


def test_the_escalation_ack_verb_is_wired_end_to_end():
    assert "escalation-ack" in ipc._DISPATCH
    parser = harnessctl.build_parser()
    args = parser.parse_args(["escalation-ack"])
    assert harnessctl._build_request(args) == {"command": "escalation-ack"}


def test_a_new_episode_requires_quiescence_to_clear_and_re_persist(frozen_run, calls, monkeypatch):
    daemon._freeze_sweep_best_effort()
    assert len(_events("run_frozen")) == 1

    # Life returns: quiescence breaks, and the episode with it.
    monkeypatch.setattr(daemon, "_run_quiescence", lambda *, now: (False, {}))
    daemon._freeze_sweep_best_effort()
    assert daemon._FREEZE["episode"] is None and daemon._FREEZE["quiescent_since"] is None

    # Quiescence returns, but the clock restarts from zero.
    monkeypatch.setattr(daemon, "_run_quiescence", lambda *, now: (True, {"seats": [], "owed": []}))
    daemon._freeze_sweep_best_effort()
    assert len(_events("run_frozen")) == 1, "the new episode must earn its own 45 minutes"

    daemon._FREEZE["quiescent_since"] = _minutes_ago(46)
    daemon._freeze_sweep_best_effort()
    assert len(_events("run_frozen")) == 2


# ===========================================================================
# WAL vocabulary — the new events are classified by the single authority.
# ===========================================================================

def test_the_new_events_are_classified_as_evidence(runtime):
    for event in (
        "wake_hold_engaged",
        "wake_hold_cleared",
        "run_frozen",
        "run_freeze_notify_failed",
        "run_freeze_acked",
        "run_freeze_paused",
    ):
        assert wal_policy.EVENT_CLASSIFICATION[event] == wal_policy.EVIDENCE_CONSUMER
        assert not wal_policy.replay_changes_binding({"event": event})
