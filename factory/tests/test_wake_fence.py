"""LT-2 / LT-3 / SWL-03 / LT-10 — the wake/prod send path: surfaced failures, the sender-side
fence, the pre-send ack watermark, and a pointer that names a real file.

The pre-live-run findings pinned here:

  * LT-2 — ``tmux.send_keys`` ran check=False and discarded the rc, so ``_deliver_keystroke``
    returned True on a FAILED send: the wake watermark advanced on undelivered nudges (a lost
    wake) and the ``wake_send_failed``/``prod_send_failed`` journal rows were unreachable with
    the real transport. Now ``send_keys`` returns False on a non-zero rc and the daemon journals
    + leaves the watermark unmoved (the next tick retries).

  * LT-3 — TRANSPORTS §3.2 precondition 2 (the sender-side fence) was wholly unimplemented: the
    wake/prod send used the loop's pre-tick binding snapshot. Deterministic trigger: the SAME
    tick that collapses a signed-off leaf (STEP A) then nudged its pane 'resume' off the stale
    snapshot and acked the inbox on a terminal binding. Now the live binding is re-read
    immediately before every send and the send aborts on terminal state / owner_token /
    lease_epoch / session_uuid drift.

  * SWL-03 — the ack watermark was stat'ed AFTER the send: a concurrent inbox append in the
    send->stat window was acked without ever being nudged (lost wake). Now the pre-send size is
    acked; a line landing mid-send stays above the watermark and re-triggers next tick (at worst
    one duplicate nudge — the edge-trigger design tolerates that).

  * LT-10 — ``wake_keystroke`` pointed at '<node_address>/.inbox.jsonl' — a NONEXISTENT path
    ('#' is not a path segment; the real file is seat-qualified). The pointer now names
    ``.inbox.<seat>.jsonl`` in the pane's own workspace, mirroring the kickoff's wording.

R-1 (tests/test_wake_verify.py) tightened the ack further: a DELIVERED send no longer acks on
rc=0 — it records a pending verification, and the watermark advances only after verify-new-turn
(transcript growth) on a later tick. The tests here exercise the fence/pre-send-size semantics
THROUGH that discipline (growing the transcript where an ack is expected).
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
from harnessd.spawn import tmux as tmux_mod

LEAF = "proj/widget/task#exec"


@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


def _seed_leaf(runtime, **overrides):
    token = fencing.mint_owner_token(LEAF, "sa", "uuid", 1)
    ws = addressing.node_dir(LEAF, runtime)
    ws.mkdir(parents=True, exist_ok=True)
    target = addressing.session_name_for(LEAF) + ":0.0"
    rec = {
        "node_address": LEAF, "parent_address": "proj/widget#exec", "level": "L5",
        "subagent_id": "sa", "session_uuid": "uuid", "state": "running", "generation": 1,
        "lease_epoch": 1, "owner_token": token, "last_applied_seq": 0,
        "liveness_state": "working", "terminal_signal": None, "terminal_signal_at": None,
        "gate_crossed_at": None, "paused_at": None, "transcript_path": str(ws / "t.jsonl"),
        "tmux_target": target, "workspace": str(ws),
        "stale_check_count": 0, "stale_grace_checks": 2,
        "last_inbox_acked_offset": 0,
    }
    rec.update(overrides)
    ledger.write_binding({LEAF: copy.deepcopy(rec)}, _lock_held=True)
    return rec, token, target


def _append_inbox(runtime, node_address, line: dict) -> int:
    p = addressing.inbox_path(node_address, runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return p.stat().st_size


class _Detector:
    def __init__(self, state, last_progress_at=None):
        self._state = state
        self._lp = last_progress_at

    def liveness(self, node_address):
        return SimpleNamespace(state=self._state, last_progress_at=self._lp)


class _FailingSendTmux:
    """send_keys 'completes' but reports delivery failure (the REAL transport's new contract)."""

    def __init__(self):
        self.sent = []

    def send_keys(self, target, text):
        self.sent.append((target, text))
        return False  # what the real send_keys now returns on a non-zero tmux rc

    def list_targets(self):
        return {}


class _RecordingTmux:
    def __init__(self, on_send=None):
        self.sent = []
        self._on_send = on_send

    def send_keys(self, target, text):
        self.sent.append((target, text))
        if self._on_send is not None:
            self._on_send()
        return True

    def list_targets(self):
        return {}


def _wal_events(node_address):
    return [r.get("event") for r in ledger.load_wal() if r.get("node_address") == node_address]


# ---------------------------------------------------------------------------------------
# (1) LT-2 — tmux.send_keys surfaces delivery failure; the daemon journals + does not ack.
# ---------------------------------------------------------------------------------------

def test_send_keys_returns_false_on_a_failed_send(monkeypatch):
    rcs = iter([1, 0])

    def _fake_run(args, **kwargs):
        return SimpleNamespace(returncode=next(rcs), stdout="", stderr="no such session")

    monkeypatch.setattr(tmux_mod, "_run", _fake_run)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert tmux_mod.send_keys("harness-gone:0.0", "hello") is False, (
        "a non-zero send-keys rc (dead/missing target) must surface as False — the watermark "
        "must not advance on an undelivered nudge (LT-2)"
    )


def test_send_keys_returns_true_when_both_sends_land(monkeypatch):
    def _fake_run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tmux_mod, "_run", _fake_run)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert tmux_mod.send_keys("harness-x:0.0", "hello") is True


def test_failed_wake_send_is_journaled_and_not_acked(runtime, monkeypatch):
    rec, _token, _target = _seed_leaf(runtime)
    _append_inbox(runtime, LEAF, {"from": "parent", "msg": "wake"})
    tmux = _FailingSendTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    daemon._watchdog_tick(executor, tmux, _Detector("working"))

    assert tmux.sent, "the send was attempted"
    assert ledger.read_binding(LEAF).get("last_inbox_acked_offset", 0) == 0, (
        "a send-keys that REPORTED failure must NOT advance the watermark — the next tick retries"
    )
    assert "wake_send_failed" in _wal_events(LEAF), (
        "the failed send must be journaled (wake_send_failed) — the commit-message guarantee "
        "'a failed send is journaled, never silently swallowed' must hold for the REAL transport"
    )


# ---------------------------------------------------------------------------------------
# (2) SWL-03 — the watermark acks the PRE-send size: a mid-send append is never lost.
# ---------------------------------------------------------------------------------------

def test_concurrent_append_during_send_is_not_acked_and_renudges(runtime, monkeypatch):
    rec, _token, _target = _seed_leaf(runtime)
    # The transcript exists so the R-1 verification can be satisfied by growing it.
    with open(rec["transcript_path"], "w", encoding="utf-8") as fh:
        fh.write('{"type": "summary", "boot": true}\n')
    size_a = _append_inbox(runtime, LEAF, {"from": "parent", "msg": "line A"})

    def _append_b():
        _append_inbox(runtime, LEAF, {"from": "human", "type": "answer_posted", "msg": "line B"})

    tmux = _RecordingTmux(on_send=_append_b)
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    daemon._watchdog_tick(executor, tmux, _Detector("working"))

    assert len(tmux.sent) == 1
    live = ledger.read_binding(LEAF)
    assert live.get("wake_pending_ack_offset") == size_a, (
        "the (pending) ack target must be the PRE-send size: line B (appended in the send->stat "
        "window) must stay ABOVE the eventual watermark; got "
        f"{live.get('wake_pending_ack_offset')!r}, want {size_a}"
    )
    assert live.get("last_inbox_acked_offset", 0) == 0, (
        "R-1: a delivered send records the pending verification — it does NOT ack on rc=0"
    )
    assert watchdog.inbox_has_unacked({"node_address": LEAF}, live) is True, (
        "line B still owes a nudge — 'deferred, never lost'"
    )

    # The agent consumes the nudge (a new turn grows the transcript) — the next tick acks the
    # PRE-send size (SWL-03), leaving line B above the watermark, and re-nudges for it.
    with open(rec["transcript_path"], "a", encoding="utf-8") as fh:
        fh.write('{"type": "assistant", "turn": "new"}\n')
    daemon._watchdog_tick(executor, tmux, _Detector("working"))
    live = ledger.read_binding(LEAF)
    assert live.get("last_inbox_acked_offset") == size_a, (
        "the verified ack must land at the PRE-send size — line B stays above the watermark"
    )
    assert len(tmux.sent) >= 2, "the resolving tick must re-nudge for line B (the wake is never lost)"


# ---------------------------------------------------------------------------------------
# (3) LT-3 — the sender-side fence (TRANSPORTS §3.2 P2).
# ---------------------------------------------------------------------------------------

def test_collapse_tick_does_not_nudge_the_collapsed_leaf(runtime, monkeypatch):
    """THE deterministic LT-3 trigger: a leaf signs off DONE; the SAME tick collapses it (STEP A)
    and then reaches the ③-wake with the kickoff line still unacked. The post-collapse binding is
    terminal — the fence must abort the send AND leave the watermark/ack off the terminal binding."""
    rec, token, _target = _seed_leaf(runtime)
    _append_inbox(runtime, LEAF, {"from": "harnessd", "type": "kickoff", "message": "begin"})
    # The fenced DONE sign-off (the real handshake shape — owner_token copied verbatim).
    sig = addressing.signal_path(LEAF, runtime)
    sig.parent.mkdir(parents=True, exist_ok=True)
    # E2 fixture completion: the return contract requires report.md at DONE.
    (sig.parent / "report.md").write_text(
        "# report\n\ndone per brief.\n\n"
        "## Drove and Watched\n\nFixture drove the claim.\n\n"
        "## Inferred\n\nSupporting checks passed.\n\n"
        "## Residual Uncertainty\n\nNone beyond fixture scope.\n\n"
        "## Inventory\n\nNone.\n",
        encoding="utf-8",
    )
    sig.write_text(json.dumps({
        "signal": "DONE", "ts": "2026-06-11T00:00:00+00:00", "owner_token": token,
        "evidence": {"report": "report.md"},
    }))
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    daemon._watchdog_tick(executor, tmux, _Detector("working"))

    live = ledger.read_binding(LEAF)
    assert live.get("state") == "done", "STEP A must have collapsed the signed-off leaf"
    assert tmux.sent == [], (
        "the SAME tick must NOT nudge the pane it just collapsed — a post-collapse 'resume' "
        "instructs a signed-off agent to start a new turn (LT-3's deterministic case)"
    )
    assert live.get("last_inbox_acked_offset", 0) == 0, (
        "no inbox-ack WAL write may land on the terminal binding"
    )


def test_wake_aborts_when_the_live_token_rotated(runtime, monkeypatch):
    """The general fence: the snapshot's owner_token/lease_epoch no longer match the live binding
    (a re-claim between snapshot and send) -> abort, no send, watermark unmoved."""
    rec, _token, _target = _seed_leaf(runtime)
    _append_inbox(runtime, LEAF, {"from": "parent", "msg": "wake"})
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    stale_snapshot = dict(rec)
    stale_snapshot["owner_token"] = "rotated-away-token"
    stale_snapshot["lease_epoch"] = 99

    daemon._wake_on_unacked_inbox(executor, tmux, LEAF, stale_snapshot)

    assert tmux.sent == [], "a token/epoch drift between snapshot and send must abort the wake"
    assert ledger.read_binding(LEAF).get("last_inbox_acked_offset", 0) == 0


def test_wake_aborts_when_the_session_uuid_rotated(runtime, monkeypatch):
    rec, _token, _target = _seed_leaf(runtime)
    _append_inbox(runtime, LEAF, {"from": "parent", "msg": "wake"})
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    stale_snapshot = dict(rec)
    stale_snapshot["session_uuid"] = "the-prior-incarnation"

    daemon._wake_on_unacked_inbox(executor, tmux, LEAF, stale_snapshot)

    assert tmux.sent == [], (
        "a session_uuid mismatch (respawned incarnation) must abort the send — the nudge was "
        "computed for a pane that no longer exists (TRANSPORTS §3.2 P2)"
    )


def test_healthy_wake_still_delivers_and_acks_through_the_fence(runtime, monkeypatch):
    """The fence is a guard, not a regression: an un-drifted live binding still gets its ONE nudge
    and the watermark advances once the turn is VERIFIED (R-1/T58: agent transcript progress)."""
    rec, _token, target = _seed_leaf(runtime)
    size = _append_inbox(runtime, LEAF, {"from": "parent", "msg": "wake"})
    tmux = _RecordingTmux()
    monkeypatch.setattr(watchdog, "prod_precondition", lambda _node: True)

    daemon._watchdog_tick(executor, tmux, _Detector("working"))

    assert len(tmux.sent) == 1 and tmux.sent[0][0] == target
    assert ledger.read_binding(LEAF).get("wake_pending_ack_offset") == size, (
        "the delivered nudge records its pending verification (R-1: no ack on rc=0 alone)"
    )

    # The agent takes its turn (the transcript grows) -> the next tick acks through the fence.
    with open(rec["transcript_path"], "w", encoding="utf-8") as fh:
        fh.write('{"type": "assistant", "turn": "new"}\n')
    daemon._watchdog_tick(executor, tmux, _Detector("working"))

    assert len(tmux.sent) == 1, "nothing else unacked -> no second nudge"
    assert ledger.read_binding(LEAF).get("last_inbox_acked_offset") == size


# ---------------------------------------------------------------------------------------
# (4) LT-10 — the wake pointer names the REAL seat-qualified inbox file.
# ---------------------------------------------------------------------------------------

def test_wake_keystroke_names_the_seat_qualified_inbox(runtime):
    _append_inbox(
        runtime,
        LEAF,
        {"from": "proj/widget#exec", "type": "message"},
    )
    payload = watchdog.wake_keystroke({"node_address": LEAF})
    assert ".inbox.exec.jsonl" in payload, (
        f"the pointer must name the REAL seat-qualified inbox file; got {payload!r}"
    )
    assert "1 new message: 1 from proj/widget#exec" in payload
    assert f"{LEAF}/.inbox.jsonl" not in payload, (
        "the old '<node_address>/.inbox.jsonl' names a NONEXISTENT path ('#' is not a path "
        "segment, and the inbox is seat-qualified) — kickoff and wake must agree (LT-10)"
    )
    low = payload.lower()
    assert "inbox" in low and "re-read" in low, "still a pointer at the inbox re-read, never a payload"


def test_wake_keystroke_names_review_seat_without_exec_ambiguity(runtime):
    review = "L1/demo/task#review"
    _append_inbox(
        runtime,
        review,
        {"from": "L1/demo#exec", "type": "message"},
    )
    payload = watchdog.wake_keystroke({"node_address": review})

    assert "1 new message: 1 from L1/demo#exec" in payload
    assert ".inbox.review.jsonl" in payload
    assert ".inbox.exec.jsonl" not in payload
