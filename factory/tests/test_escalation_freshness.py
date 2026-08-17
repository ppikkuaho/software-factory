"""SM-4 — escalation exactly-once is PER ARTIFACT, and the answer expires the slot-hold.

The pre-fix escalate() keyed its exactly-once on the bare ``terminal_signal=ESCALATED`` binding
stamp, which nothing ever clears (v1 has no round-trip-completion verb; the F16 answer verb
deliberately leaves it in place). Two breaks followed:

  (a) a SECOND escalation (a NEW question written to .signal after the human answered the first)
      silently never journaled — escalate() early-returned on the stale stamp, so no fresh
      signal_ESCALATED WAL row landed and terminal_signal_at stayed stale (the SML-02
      'exactly once per artifact' contract degraded to at-most-once per incarnation);
  (b) the detector read the lingering stamp as a PERMANENT legit-waiting reason — a node that
      ever escalated could never again read 'idle', so the idle->prod->FAILED ladder
      (WATCHDOG §3.5) was dead for it post-answer: a wedged post-answer agent was never prodded.

The fix: escalate() re-journals IFF the harness-observed artifact identity differs from the
recorded ``signal_artifact_seen_at``; executor.post_answer stamps ``answered_at``; an escalation
whose answer is fresher than the stamp and whose artifact identity is already journaled no longer
shields the node — the watchdog's STEP A falls through to the ladder and the detector's waiting
reason expires. A fresh artifact re-arms everything. Agent-authored ``ts`` is not trusted.

Style: real ledger/executor/chokepoint/watchdog on a tmp RUNTIME_ROOT; liveness injected through
the watchdog seam (the one justified mock, the test_watchdog.py pattern).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

import harnessd.addressing as addressing
import harnessd.clock as clock
import harnessd.detector as detector
import harnessd.detector_signals as detector_signals
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.watchdog as watchdog
from harnessd.detector import Liveness
from harnessd.spawn import chokepoint


LEAF = "proj/widget#exec"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(detector_signals, "_size_cache", {}, raising=False)
    return tmp_path


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _binding(*, lease_epoch=2, generation=1):
    token = fencing.mint_owner_token(LEAF, "subagent-x", "sess-x", lease_epoch)
    return {
        "node_address": LEAF, "parent_address": "proj#exec", "level": "L5",
        "subagent_id": "subagent-x", "session_uuid": "sess-x", "tmux_target": "harness:t.0",
        "state": "running", "generation": generation, "lease_epoch": lease_epoch,
        "owner_token": token, "last_applied_seq": 0, "liveness_state": "working",
        "terminal_signal": None, "terminal_signal_at": None, "gate_crossed_at": None,
        "paused_at": None, "transcript_path": "/dev/null",
        "stale_check_count": 0, "stale_grace_checks": 2,
    }


def _seed(binding):
    ledger.write_binding({binding["node_address"]: copy.deepcopy(binding)}, _lock_held=True)


def _live():
    return ledger.read_binding(LEAF)


def _write_signal(*, owner_token, ts=None, question="which option?"):
    p = addressing.signal_path(LEAF, ledger.RUNTIME_ROOT)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"signal": "ESCALATED", "ts": ts or clock.now_utc(),
               "owner_token": owner_token, "evidence": {"notes": question}}
    p.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _escalation_rows():
    return [r for r in ledger.load_wal()
            if r.get("node_address") == LEAF and r.get("event") == "signal_ESCALATED"]


def _tick(now=None):
    live = _live()
    return watchdog.check_leaf(live, live, now=now or clock.now_utc())


# ===========================================================================
# Exactly-once is PER ARTIFACT (re-poll no-op; post-answer second question journals).
# ===========================================================================

def test_same_artifact_is_journaled_exactly_once_across_ticks(runtime):
    _seed(_binding())
    _write_signal(owner_token=_live()["owner_token"])

    first = _tick()
    assert getattr(first, "kind", None) == watchdog.NOOP
    assert (first.detail or {}).get("reason") == "escalated_holds_slot"
    assert len(_escalation_rows()) == 1

    second = _tick()  # the steady re-poll of the SAME artifact
    assert (second.detail or {}).get("reason") == "escalated_holds_slot"
    assert len(_escalation_rows()) == 1, "a re-poll of the same artifact must not re-journal"
    assert (_live().get("signal_artifact_seen_at") or "").startswith("sha256:"), (
        "the journal-once guard must record the harness-observed artifact identity"
    )


def test_future_agent_ts_does_not_rejournal_same_artifact(runtime):
    _seed(_binding())
    _write_signal(owner_token=_live()["owner_token"], ts="2999-01-01T00:00:00+00:00")

    _tick()
    first_identity = _live().get("signal_artifact_seen_at")
    assert len(_escalation_rows()) == 1

    _tick()
    assert len(_escalation_rows()) == 1, (
        "agent-authored future ts must not turn a steady re-poll into an infinite journal loop"
    )
    assert _live().get("signal_artifact_seen_at") == first_identity


def test_second_escalation_after_answer_journals_a_fresh_row(runtime):
    """The SM-4 headline (a): question 1 -> answer -> question 2 must journal a SECOND
    signal_ESCALATED row and re-stamp terminal_signal_at — pre-fix it was silently dropped."""
    _seed(_binding())
    _write_signal(owner_token=_live()["owner_token"], question="q1")
    _tick()
    assert len(_escalation_rows()) == 1
    first_stamp = _live()["terminal_signal_at"]

    assert executor.post_answer(LEAF, answer="use option B").ok
    assert _live()["answered_at"], "post_answer must stamp answered_at (the expiry instant)"

    # The agent resumes, then hits a NEW question — a fresh artifact (different content identity).
    _write_signal(owner_token=_live()["owner_token"], question="q2")
    action = _tick()

    rows = _escalation_rows()
    assert len(rows) == 2, (
        f"the SECOND escalation must journal its own signal_ESCALATED row (got {len(rows)}) — "
        "the stamp-keyed idempotency made it at-most-once per incarnation (SM-4)"
    )
    assert (action.detail or {}).get("reason") == "escalated_holds_slot"
    assert _live()["terminal_signal_at"] > first_stamp, (
        "the re-journal must re-stamp terminal_signal_at (the freshness key + the re-armed hold)"
    )


# ===========================================================================
# The answer EXPIRES the slot-hold: the ladder revives; a new question re-arms.
# ===========================================================================

def test_idle_ladder_revives_after_the_answer(runtime, monkeypatch):
    """The SM-4 headline (b): post-answer, an idle-beyond-W node must be PRODDED again — the
    lingering ESCALATED stamp + artifact must not shield it forever."""
    _seed(_binding())
    _write_signal(owner_token=_live()["owner_token"])
    _tick()  # journal the slot-hold

    watchdog.set_liveness(lambda addr: Liveness(state="idle", last_progress_at=_ago(10_000)))
    monkeypatch.setattr(watchdog, "prod_precondition", lambda node: True)
    try:
        held = _tick()
        assert (held.detail or {}).get("reason") == "escalated_holds_slot", (
            "control: BEFORE the answer the slot-hold must shield the idle node"
        )

        assert executor.post_answer(LEAF, answer="go with plan A").ok
        revived = _tick()
    finally:
        watchdog.set_liveness(None)

    assert getattr(revived, "kind", None) == watchdog.PROD, (
        f"post-answer, the idle ladder must REVIVE (expected PROD, got {revived!r}) — the "
        "unexpiring waiting reason permanently disabled idle->prod->FAILED (SM-4)"
    )


def test_detector_waiting_reason_expires_and_rearms(runtime):
    _seed(_binding())
    binding = _live()
    _write_signal(owner_token=binding["owner_token"])
    _tick()  # stamp + journal

    assert detector._has_legit_waiting_reason(LEAF, _live()) is True, (
        "control: an unanswered escalation IS a legit waiting reason"
    )

    assert executor.post_answer(LEAF, answer="answered").ok
    assert detector._has_legit_waiting_reason(LEAF, _live()) is False, (
        "an ANSWERED escalation must no longer read as waiting (SM-4 expiry)"
    )

    # A NEW question (fresh artifact, re-journaled stamp) re-arms the waiting reason.
    _write_signal(owner_token=_live()["owner_token"], question="q2")
    _tick()
    assert detector._has_legit_waiting_reason(LEAF, _live()) is True, (
        "a re-escalation after the answer must re-arm the waiting reason"
    )


def test_answer_alone_never_collapses_or_fails_a_busy_node(runtime):
    """Spec consistency: the answered expiry only stops the SHIELD — a working node stays NOOP
    through STEP B (not_idle), and the ESCALATED stamp itself is never cleared by the answer."""
    _seed(_binding())
    _write_signal(owner_token=_live()["owner_token"])
    _tick()
    assert executor.post_answer(LEAF, answer="answered").ok

    watchdog.set_liveness(lambda addr: Liveness(state="working", last_progress_at=clock.now_utc()))
    try:
        action = _tick()
    finally:
        watchdog.set_liveness(None)

    assert getattr(action, "kind", None) == watchdog.NOOP
    assert (action.detail or {}).get("reason") == "not_idle"
    live = _live()
    assert live["terminal_signal"] == "ESCALATED" and live["state"] == "running", (
        "the answer RIDES the stamp (TRANSPORTS §5.3) — expiry must not clear or collapse anything"
    )
