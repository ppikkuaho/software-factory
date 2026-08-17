"""Increment 11 — load-bearing STRENGTHENING (mutation-review gate).

Gaps:
  1. (HIGH, fixed) the two-counter ladder had NO increment site — both ladder tests hand-seeded the
     count (0 or 2), so nothing ever exercised the counter ADVANCING across calls. An unresponsive leaf
     would be prodded forever, never reaching FAILED. The executor now increments stale_check_count on
     an idle observation; check_leaf persists the advance on each prod. THIS test pins that the ladder
     genuinely converges: idle, grace=2 -> PROD, PROD, FAILED across three real calls.
  2. (mutant survivor) ESCALATED->NOOP: the original test drove liveness='waiting', so deleting the
     ESCALATED branch fell through to waiting->NOOP (same result). Drive liveness='idle' so a deleted
     ESCALATED branch would PROD -> the NOOP is genuinely load-bearing.
  3. (mutant survivor) FAILED-escalates-to-parent: the assertion matched the literal dict KEY
     'parent_address'; setting parent_address=None survived. Assert the actual parent ADDRESS VALUE.
"""

from datetime import datetime, timedelta, timezone

import pytest

import harnessd.detector as detector
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.watchdog as wd
from harnessd.detector import Liveness


LEAF = "proj/widget#exec"
PARENT = "proj#exec"


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ago(s):
    return (datetime.now(timezone.utc) - timedelta(seconds=s)).isoformat()


def _seed(*, stale_check_count=0, stale_grace_checks=2):
    token = fencing.mint_owner_token(LEAF, "sa", "uuid", 1)
    rec = {"node_address": LEAF, "parent_address": PARENT, "level": "L5", "subagent_id": "sa",
           "session_uuid": "uuid", "state": "running", "generation": 0, "lease_epoch": 1,
           "owner_token": token, "last_applied_seq": 0, "liveness_state": "idle",
           "last_progress_at": _ago(9999), "transcript_path": str(ledger.RUNTIME_ROOT / "t.jsonl"),
           "tmux_target": "harness:proj-widget-exec",
           "stale_check_count": stale_check_count, "stale_grace_checks": stale_grace_checks}
    ledger.write_binding({LEAF: rec}, _lock_held=True)
    return rec


def _node():
    b = ledger.read_binding(LEAF)
    return {"node_address": LEAF, "transcript_path": b.get("transcript_path"), "tmux_target": b.get("tmux_target")}


def _inject(mp, state):
    mp.setattr(detector, "liveness", lambda _a: Liveness(state=state, last_progress_at=_ago(9999)), raising=True)
    if hasattr(wd, "set_liveness"):
        wd.set_liveness(lambda _a: Liveness(state=state, last_progress_at=_ago(9999)))


def _tag(action):
    for attr in ("kind", "tag", "action", "name", "type"):
        v = getattr(action, attr, None)
        if isinstance(v, str):
            return v.upper()
    return repr(action).upper()


# --- THE missing load-bearing test: the ladder ACTUALLY ADVANCES across real calls ---------------

def test_ladder_advances_idle_prod_prod_failed(runtime):
    """idle + grace=2: three real check_leaf calls -> PROD, PROD, FAILED. The counter MUST advance
    (the HIGH bug: with no increment site the leaf prods forever and never fails)."""
    _seed(stale_check_count=0, stale_grace_checks=2)
    mp = pytest.MonkeyPatch()
    try:
        _inject(mp, "idle")
        mp.setattr(wd, "prod_precondition", lambda _n: True, raising=True)

        a1 = wd.check_leaf(_node(), ledger.read_binding(LEAF), now=_now())
        assert _tag(a1) == "PROD", f"call 1 must PROD (got {_tag(a1)})"
        assert ledger.read_binding(LEAF)["stale_check_count"] == 1, "prod must persist count->1"

        a2 = wd.check_leaf(_node(), ledger.read_binding(LEAF), now=_now())
        assert _tag(a2) == "PROD", f"call 2 must PROD (got {_tag(a2)})"
        assert ledger.read_binding(LEAF)["stale_check_count"] == 2, "prod must persist count->2"

        a3 = wd.check_leaf(_node(), ledger.read_binding(LEAF), now=_now())
        assert _tag(a3) in ("FAILED", "ESCALATE"), f"call 3 (count at grace) must FAIL (got {_tag(a3)})"
        assert ledger.read_binding(LEAF)["state"] == "failed", "the ladder must converge to FAILED"
    finally:
        mp.undo()


def test_ladder_resets_on_recovery(runtime):
    """A node that prods once then recovers to working must have its counter RESET (a blip must not
    march a recovered node toward FAILED)."""
    _seed(stale_check_count=0, stale_grace_checks=2)
    mp = pytest.MonkeyPatch()
    try:
        _inject(mp, "idle")
        mp.setattr(wd, "prod_precondition", lambda _n: True, raising=True)
        wd.check_leaf(_node(), ledger.read_binding(LEAF), now=_now())  # PROD -> count 1
        assert ledger.read_binding(LEAF)["stale_check_count"] == 1

        _inject(mp, "working")  # the node recovered
        wd.check_leaf(_node(), ledger.read_binding(LEAF), now=_now())
        assert ledger.read_binding(LEAF)["stale_check_count"] == 0, "recovery must RESET the counter"
    finally:
        mp.undo()


# --- mutant survivor 2: ESCALATED->NOOP isolated with idle liveness ------------------------------

def _write_signal(signal, owner_token):
    import json
    import harnessd.addressing as _addressing
    p = _addressing.signal_path(LEAF, ledger.RUNTIME_ROOT)  # nested dir + per-seat signal file
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"signal": signal, "ts": _now(), "owner_token": owner_token, "evidence": "x"}),
        encoding="utf-8")


def test_escalated_noop_isolated_against_idle(runtime):
    """An ESCALATED signal on an IDLE node must NOOP (hold its slot) — NOT prod/fail. Driving liveness
    to 'idle' isolates the STEP-A ESCALATED branch: deleting it would fall through to idle->PROD."""
    rec = _seed(stale_check_count=0, stale_grace_checks=2)
    _write_signal("ESCALATED", rec["owner_token"])  # fenced, live token
    mp = pytest.MonkeyPatch()
    try:
        _inject(mp, "idle")
        mp.setattr(wd, "prod_precondition", lambda _n: True, raising=True)
        action = wd.check_leaf(_node(), ledger.read_binding(LEAF), now=_now())
        assert _tag(action) not in ("PROD", "FAILED", "COLLAPSE"), \
            f"ESCALATED must hold its slot (NOOP), never prod/fail/collapse even when idle (got {_tag(action)})"
        assert ledger.read_binding(LEAF)["state"] == "running", "ESCALATED never collapses the node"
    finally:
        mp.undo()


# --- mutant survivor 3: FAILED escalation carries the actual parent ADDRESS VALUE -----------------

def test_failed_escalation_carries_parent_address_value(runtime):
    """The FAILED escalation must target the actual PARENT ADDRESS value (not merely contain the word
    'parent' — a parent_address=None mutant must be caught)."""
    _seed(stale_check_count=2, stale_grace_checks=2)  # at grace -> this poll fails
    mp = pytest.MonkeyPatch()
    try:
        _inject(mp, "idle")
        mp.setattr(wd, "prod_precondition", lambda _n: True, raising=True)
        action = wd.check_leaf(_node(), ledger.read_binding(LEAF), now=_now())
        assert _tag(action) in ("FAILED", "ESCALATE")
        # The ACTUAL parent address VALUE must appear as the escalation target.
        target = getattr(action, "target", None)
        detail = getattr(action, "detail", {}) or {}
        carries_parent = target == PARENT or detail.get("parent_address") == PARENT
        assert carries_parent, f"the FAILED escalation must target the parent address {PARENT!r} (got target={target!r})"
    finally:
        mp.undo()
