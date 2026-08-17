"""R-1/T58 — the ③-wake ack requires VERIFY-NEW-TURN, never send-keys rc=0.

The defect this pins shut: ``daemon._wake_on_unacked_inbox`` acked the inbox watermark the
moment ``_deliver_keystroke`` returned True — but rc=0 only proves tmux accepted the bytes,
NOT that the agent consumed them. The sharpest live case: an operator's interactive tmux
attach in copy-mode (scrolling) EATS the keystrokes while capture-pane still shows the idle
prompt (gate open) and send-keys still exits 0. For a leaf the prod ladder eventually heals;
a COORDINATOR (L1!) has no prod-ladder healer, so a swallowed human-answer wake (the
``answer_posted`` line — exactly coordinator-bound) wedged the run silently and permanently.

TRANSPORTS §3.2 Precondition 3 (verify-new-turn) mandates the discipline. The original R-1 verifier
used transcript JSONL growth; T58/LR-130 tightens that to agent/model progress after the send
baseline because provider/API error rows also grow the transcript without proving wake consumption.
Now:

  * a DELIVERED send does NOT ack — it records a pending verification on the binding via the
    single-writer own-slice path (``wake_pending_ack_offset`` = the pre-send inbox size, the
    SWL-03 ack target; ``wake_sent_transcript_size`` = the transcript byte size at send time,
    the growth baseline);
  * a LATER tick resolves the pending FIRST: agent/model progress after the baseline -> ack_inbox
    to the recorded offset + clear the pending fields (ONE ack — never a double); absent,
    unreadable, unchanged, or provider/API-error-only transcript tails keep the watermark unmoved,
    and the still-unacked line permits a gated re-send that OVERWRITES the pending record (one
    pending verification at a time per node — no spin, one send per tick);
  * an absent/unreadable transcript reads as NOT-grown (conservative — no blind trust);
  * the baseline is captured AT SEND TIME: pre-existing transcript bytes never count as growth
    (the mutant that records a 0 baseline acks off boot content alone).

The revert-mutant (ack on rc=0 alone) fails every test here: the watermark would advance on
tick 1 with zero transcript growth.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

import harnessd.addressing as addressing
import harnessd.daemon as daemon
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.watchdog as watchdog

LEAF = "proj/widget/task#exec"
COORD = "proj/widget#exec"
COORD_CHILD = "proj/widget/task#exec"


@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


def _make_binding(runtime, node_address, **overrides):
    token = fencing.mint_owner_token(node_address, "sa", "uuid", 1)
    ws = addressing.node_dir(node_address, runtime)
    ws.mkdir(parents=True, exist_ok=True)
    target = addressing.session_name_for(node_address) + ":0.0"
    rec = {
        "node_address": node_address, "parent_address": "proj#exec", "level": "L5",
        "subagent_id": "sa", "session_uuid": "uuid", "state": "running", "generation": 1,
        "lease_epoch": 1, "owner_token": token, "last_applied_seq": 0,
        "liveness_state": "working", "terminal_signal": None, "terminal_signal_at": None,
        "gate_crossed_at": None, "paused_at": None, "transcript_path": str(ws / "t.jsonl"),
        "tmux_target": target, "workspace": str(ws),
        "stale_check_count": 0, "stale_grace_checks": 2,
        "last_inbox_acked_offset": 0,
    }
    rec.update(overrides)
    return rec, token, target


def _seed_leaf(runtime, **overrides):
    rec, token, target = _make_binding(runtime, LEAF, **overrides)
    ledger.write_binding({LEAF: copy.deepcopy(rec)}, _lock_held=True)
    return rec, token, target


def _append_inbox(runtime, node_address, line: dict) -> int:
    p = addressing.inbox_path(node_address, runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return p.stat().st_size


def _grow_transcript(rec, text="\n{\"type\": \"assistant\", \"turn\": \"new\"}\n"):
    """Simulate the agent taking a NEW turn: append bytes to the transcript JSONL."""
    from pathlib import Path

    p = Path(rec["transcript_path"])
    with p.open("a", encoding="utf-8") as fh:
        fh.write(text)
    return p.stat().st_size


class _Detector:
    def __init__(self, state, last_progress_at=None):
        self._state = state
        self._lp = last_progress_at

    def liveness(self, node_address):
        return SimpleNamespace(state=self._state, last_progress_at=self._lp)


class _RecordingTmux:
    def __init__(self):
        self.sent = []

    def send_keys(self, target, text):
        self.sent.append((target, text))
        return True

    def list_targets(self):
        return {}


def _tick(tmux):
    daemon._watchdog_tick(executor, tmux, _Detector("working"))


def _wal_events(node_address):
    return [r.get("event") for r in ledger.load_wal() if r.get("node_address") == node_address]


# ---------------------------------------------------------------------------------------
# (1) Delivered send + NO transcript growth -> watermark NOT acked; retry possible; no spin.
#     (The revert-mutant — ack on rc=0 alone — fails the very first assertion block.)
# ---------------------------------------------------------------------------------------

def test_delivered_send_without_transcript_growth_never_acks(runtime, monkeypatch):
    rec, _token, _target = _seed_leaf(runtime)
    # The transcript EXISTS with boot content and never grows — rc=0 alone must not ack,
    # and pre-send bytes must not count as growth (the baseline is captured at send time).
    boot_size = _grow_transcript(rec, text='{"type": "summary", "boot": true}\n')
    size = _append_inbox(runtime, LEAF, {"from": "human", "type": "answer_posted", "msg": "B"})
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    _tick(tmux)

    assert len(tmux.sent) == 1, "the nudge IS delivered (rc=0)"
    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset", 0) == 0, (
        "R-1: send-keys rc=0 is NOT consumption — the watermark must NOT advance until the "
        "transcript grows (a copy-mode attach eats the keystrokes while send-keys exits 0)"
    )
    assert live.get("wake_pending_ack_offset") == size, (
        "the delivered send must record the PRE-send inbox size as the pending ack target "
        f"(SWL-03 preserved); got {live.get('wake_pending_ack_offset')!r}"
    )
    assert live.get("wake_sent_transcript_size") == boot_size, (
        "the growth baseline is the transcript size AT SEND TIME — pre-existing boot bytes "
        f"must not count as growth; got {live.get('wake_sent_transcript_size')!r}"
    )

    # A later tick with NO growth: still not acked, and the retry (re-send) IS permitted —
    # exactly one send per tick, no storm, no spin.
    _tick(tmux)
    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset", 0) == 0, "still unverified -> still not acked"
    assert len(tmux.sent) == 2, (
        "the unacked watermark must permit the gated RE-SEND on the next tick (the swallowed "
        f"nudge is retried, never trusted); got {len(tmux.sent)} sends"
    )
    _tick(tmux)
    assert len(tmux.sent) == 3, "one send per tick — bounded retry, never a storm"
    assert ledger.read_binding(LEAF).get("last_inbox_acked_offset", 0) == 0
    assert "inbox_acked" not in _wal_events(LEAF), "no ack row may land without verified growth"

    _tick(tmux)
    live = ledger.read_binding(LEAF)
    assert len(tmux.sent) == 3, "the fourth prod must never be sent for one still-unconsumed receipt"
    assert live.get("wake_attempt_count") == 3
    assert _wal_events(LEAF).count("wake_cap") == 1, (
        "the third delivered wake must emit exactly one typed wake_cap event"
    )

    _tick(tmux)
    assert len(tmux.sent) == 3, "a capped unread receipt stays capped on later ticks"
    assert _wal_events(LEAF).count("wake_cap") == 1, "wake_cap is edge-triggered, never per-poll"

    _grow_transcript(rec)
    _tick(tmux)
    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset") == size
    assert live.get("wake_attempt_count") == 0, "only the verified receipt ack resets the wake budget"
    assert live.get("wake_cap_emitted_at") is None


def test_wake_notification_names_total_and_ordered_sender_counts(runtime, monkeypatch):
    rec, _token, _target = _seed_leaf(runtime)
    first = "L1/forge-queue#exec"
    second = "L2/forge-queue/persistence#exec"
    _append_inbox(runtime, LEAF, {"from": first, "type": "answer_posted"})
    _append_inbox(runtime, LEAF, {"sender": second, "type": "message"})
    _append_inbox(runtime, LEAF, {"sender": first, "type": "message"})
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    _tick(tmux)

    assert len(tmux.sent) == 1
    text = tmux.sent[0][1]
    assert (
        f"3 new messages: 2 from {first}, 1 from {second}" in text
    ), text
    assert ".inbox.exec.jsonl" in text


def test_no_inbox_wake_without_complete_unconsumed_rows(runtime, monkeypatch):
    _seed_leaf(runtime)
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    _tick(tmux)

    assert tmux.sent == [], "zero complete unconsumed rows means zero inbox notification"


def test_silent_only_unconsumed_rows_do_not_wake(runtime, monkeypatch):
    _seed_leaf(runtime)
    _append_inbox(
        runtime,
        LEAF,
        {
            "from": "proj/widget/quiet-child#exec",
            "type": "child_collapsed",
            "barrier_mode": True,
        },
    )
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    _tick(tmux)

    assert tmux.sent == [], "the existing silent cohort-member wake-table rule remains intact"


def test_row_appended_during_send_stays_above_the_eventual_ack(runtime, monkeypatch):
    rec, _token, _target = _seed_leaf(runtime)
    size_a = _append_inbox(
        runtime,
        LEAF,
        {"from": "L1/forge-queue#exec", "type": "message"},
    )

    class _AppendingTmux(_RecordingTmux):
        def send_keys(self, target, text):
            result = super().send_keys(target, text)
            self.size_after_append = _append_inbox(
                runtime,
                LEAF,
                {"from": "L2/forge-queue#exec", "type": "message"},
            )
            return result

    tmux = _AppendingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    _tick(tmux)
    live = ledger.read_binding(LEAF)
    assert live.get("wake_pending_ack_offset") == size_a, (
        "the pending receipt covers exactly the rows counted in the notification snapshot"
    )
    assert tmux.size_after_append > size_a

    _grow_transcript(rec)
    _tick(tmux)
    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset") == size_a
    assert watchdog.inbox_has_unacked({"node_address": LEAF}, live) is True, (
        "the row appended after notification composition remains unconsumed and wakeable"
    )


# ---------------------------------------------------------------------------------------
# (2) Delivered send + agent progress on a LATER tick -> acked to the pre-send offset,
#     pending cleared, exactly ONE ack (no double-ack), no further nudge.
# ---------------------------------------------------------------------------------------

def test_transcript_growth_on_a_later_tick_acks_to_the_presend_offset(runtime, monkeypatch):
    rec, _token, _target = _seed_leaf(runtime)
    _grow_transcript(rec, text='{"type": "summary", "boot": true}\n')
    size = _append_inbox(runtime, LEAF, {"from": "parent", "msg": "wake"})
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    _tick(tmux)
    assert len(tmux.sent) == 1
    assert ledger.read_binding(LEAF).get("last_inbox_acked_offset", 0) == 0

    _grow_transcript(rec)  # the agent consumed the nudge: a NEW turn lands in the JSONL

    _tick(tmux)
    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset") == size, (
        "verified growth must ack the watermark to the recorded PRE-send offset; got "
        f"{live.get('last_inbox_acked_offset')!r}, want {size}"
    )
    assert live.get("wake_pending_ack_offset") is None, "the pending record must be CLEARED"
    assert live.get("wake_sent_transcript_size") is None
    assert len(tmux.sent) == 1, "nothing else unacked -> the resolving tick sends NO new nudge"

    _tick(tmux)
    assert len(tmux.sent) == 1, "a re-tick with nothing new must not re-nudge"
    assert ledger.read_binding(LEAF).get("last_inbox_acked_offset") == size
    assert _wal_events(LEAF).count("inbox_acked") == 1, (
        "exactly ONE ack for one verified nudge — the resolver must never double-ack"
    )


# ---------------------------------------------------------------------------------------
# (2b) Delivered send + provider/API error transcript growth -> NOT consumed, retryable.
# ---------------------------------------------------------------------------------------

def test_provider_error_transcript_growth_does_not_ack_wake(runtime, monkeypatch):
    rec, _token, _target = _seed_leaf(runtime)
    _grow_transcript(rec, text='{"type": "summary", "boot": true}\n')
    size = _append_inbox(runtime, LEAF, {"from": "parent", "msg": "gate_passed"})
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    _tick(tmux)
    assert len(tmux.sent) == 1
    assert ledger.read_binding(LEAF).get("wake_pending_ack_offset") == size

    _grow_transcript(
        rec,
        text=json.dumps(
            {
                "type": "system",
                "subtype": "api_error",
                "level": "error",
                "timestamp": "2026-06-17T07:16:34.196Z",
                "error": {
                    "message": (
                        '529 {"type":"error","error":{"type":"overloaded_error",'
                        '"message":"Overloaded"}}'
                    ),
                    "status": 529,
                    "requestId": "req_test",
                    "formatted": "529 Overloaded",
                },
            }
        )
        + "\n",
    )

    _tick(tmux)
    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset", 0) == 0, (
        "API/provider-error transcript rows are not semantic consumption of the wake; the "
        "durable inbox pointer must remain retryable"
    )
    assert live.get("wake_pending_ack_offset") == size
    assert len(tmux.sent) == 2, "the next tick retries the still-unacked wake"
    assert "inbox_acked" not in _wal_events(LEAF)

    _grow_transcript(rec)
    _tick(tmux)
    assert ledger.read_binding(LEAF).get("last_inbox_acked_offset") == size, (
        "once real assistant progress appears after the provider error, the wake can be acked"
    )


# ---------------------------------------------------------------------------------------
# (3) Absent transcript -> reads NOT-grown (conservative) -> never acked.
# ---------------------------------------------------------------------------------------

def test_absent_transcript_reads_not_grown_and_never_acks(runtime, monkeypatch):
    rec, _token, _target = _seed_leaf(runtime)  # transcript_path set, file NEVER created
    _append_inbox(runtime, LEAF, {"from": "parent", "msg": "wake"})
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    _tick(tmux)
    _tick(tmux)
    _tick(tmux)

    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset", 0) == 0, (
        "an absent/unreadable transcript reads as NOT-grown — no blind trust, never acked"
    )
    assert (live.get("wake_pending_ack_offset") or 0) > 0, "the pending verification stays armed"
    assert "inbox_acked" not in _wal_events(LEAF)


# ---------------------------------------------------------------------------------------
# (4) One pending verification at a time: a re-send OVERWRITES the pending record with the
#     NEW pre-send size, so the eventual ack covers every line the LAST nudge pointed at.
# ---------------------------------------------------------------------------------------

def test_resend_overwrites_the_pending_record_then_one_ack_covers_it(runtime, monkeypatch):
    rec, _token, _target = _seed_leaf(runtime)
    size_a = _append_inbox(runtime, LEAF, {"from": "parent", "msg": "line A"})
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    _tick(tmux)  # nudge 1 -> pending = size_a (transcript absent -> baseline 0, not-grown)
    assert ledger.read_binding(LEAF).get("wake_pending_ack_offset") == size_a

    size_b = _append_inbox(runtime, LEAF, {"from": "human", "type": "answer_posted", "msg": "B"})

    _tick(tmux)  # not grown -> re-send; the pending record is OVERWRITTEN to the new pre-send size
    live = ledger.read_binding(LEAF)
    assert len(tmux.sent) == 2
    assert live.get("wake_pending_ack_offset") == size_b, (
        "one pending verification per node: the re-send must OVERWRITE the record with ITS "
        f"pre-send size; got {live.get('wake_pending_ack_offset')!r}, want {size_b}"
    )
    assert live.get("last_inbox_acked_offset", 0) == 0

    _grow_transcript(rec)  # the agent finally takes the turn (it re-reads the WHOLE inbox)

    _tick(tmux)
    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset") == size_b, "one ack covers the last nudge's scope"
    assert live.get("wake_pending_ack_offset") is None
    assert len(tmux.sent) == 2, "nothing unacked remains -> no further nudge"
    assert _wal_events(LEAF).count("inbox_acked") == 1, "never a double-ack"


# ---------------------------------------------------------------------------------------
# (5) THE MOTIVATING CASE — a COORDINATOR's human-answer wake: same verify discipline.
#     A coordinator has NO prod-ladder healer; trusting rc=0 wedged the run permanently.
# ---------------------------------------------------------------------------------------

def test_coordinator_answer_wake_requires_verify_new_turn(runtime, monkeypatch):
    coord_rec, _ct, coord_target = _make_binding(
        runtime, COORD, parent_address=None, level="L2",
    )
    child_rec, _lt, _child_target = _make_binding(
        runtime, COORD_CHILD, parent_address=COORD, level="L5",
    )
    ledger.write_binding(
        {COORD: copy.deepcopy(coord_rec), COORD_CHILD: copy.deepcopy(child_rec)},
        _lock_held=True,
    )
    _grow_transcript(coord_rec, text='{"type": "summary", "boot": true}\n')
    # The exactly-coordinator-bound line: the human's answer wakes the PARENT's inbox.
    size = _append_inbox(runtime, COORD, {"from": "human", "type": "answer_posted", "msg": "B"})
    # Keep the leaf child quiet: its own (empty) inbox owes nothing.
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    _tick(tmux)
    coord_sends = [s for s in tmux.sent if s[0] == coord_target]
    assert len(coord_sends) == 1, "the coordinator gets its ONE wake nudge"
    live = ledger.read_binding(COORD)
    assert live.get("last_inbox_acked_offset", 0) == 0, (
        "a coordinator wake must NOT ack on rc=0 — a swallowed answer wake has NO prod-ladder "
        "healer; trusting the send wedges the run silently and permanently (R-1's sharpest case)"
    )
    assert live.get("wake_pending_ack_offset") == size

    _tick(tmux)  # still not grown -> the retry keeps the answer deliverable
    assert ledger.read_binding(COORD).get("last_inbox_acked_offset", 0) == 0
    assert len([s for s in tmux.sent if s[0] == coord_target]) == 2

    _grow_transcript(coord_rec)  # the coordinator actually resumes (a new turn in its JSONL)

    _tick(tmux)
    live = ledger.read_binding(COORD)
    assert live.get("last_inbox_acked_offset") == size, "verified -> acked to the pre-send offset"
    assert live.get("wake_pending_ack_offset") is None
