"""Increment 11 — watchdog.* FROZEN acceptance (the liveness lifecycle).

Cluster ② — terminal-signal collapse + the leaf idle->prod->FAILED ladder + the
③-wake trigger + the coordinator-death probe.

Authoritative sources (grounded, not recalled):
  - IMPLEMENTATION-PLAN §2.9 (the FROZEN watchdog.py interface — transcribed below).
  - IMPLEMENTATION-PLAN Increment-11 Done-test (L779-788) + §4.1 test battery (L564-587).
  - design/WATCHDOG.md §3.5 (the two-counter discipline + stale-grace), §4 (the
    sign-off-or-fail path / prod gate / FAILED-via-executor + actor='harnessd'),
    §5.1 (the coordinator process-death probe; dead-pid + live-children = recoverable
    orphan -> ESCALATE).
  - design/DAEMON.md §3.6 (the TERMINAL_VOCAB mapping) — states.TERMINAL_VOCAB.

FROZEN INTERFACE (§2.9 — transcribed exactly):
    check_leaf(node, binding, *, now) -> WatchdogAction
        # STEP A (TERMINAL-SIGNAL FIRST): sig = detector_signals.read_terminal_signal(node, binding).
        #   sig present & fenced: DONE/FAILED -> COLLAPSE (via chokepoint.collapse / the executor);
        #   ESCALATED -> NOOP (holds its slot, NEVER collapses).
        #   sig present but STALE owner_token -> ignore (journal stale_return_ignored), fall through to liveness.
        # STEP B (no actionable signal): liveness(node); idle + age>W -> PROD (gated by prod_precondition)
        #   up to stale_grace_checks, else FAILED.
        # CLOSING ACTION on FAILED (v1 floor): mark running->failed via the executor (actor='harnessd',
        #   reason='watchdog_nonresponse') AND ESCALATE TO THE PARENT. v1 does NOT auto-respawn from harnessd.
    prod_precondition(node) -> bool       # capture-pane shows an idle input prompt (FORK-PROMPT)
    confirm_prod_worked(node, jsonl_size_before) -> bool  # re-read JSONL; True iff a new turn appeared
    inbox_has_unacked(node, binding) -> bool  # tail <node>/.inbox.jsonl; True iff a line was appended
                                              #   AFTER binding.last_inbox_acked_offset (edge-triggered)
    wake_keystroke(node) -> str           # the ③-wake send-keys POINTER ("...re-read <node>/.inbox.jsonl...")
    check_coordinator_death(node, binding, ledger) -> WatchdogAction
        # dead-pid + LIVE children -> ESCALATE (recoverable orphan); quiet pane-alive + live children -> waiting.

BIAS TO REAL (Lesson 7): the executor + on-disk ledger are REAL; the .signal.json and .inbox.jsonl are
REAL files read by the REAL detector_signals.read_terminal_signal / inbox tail; a COLLAPSE/FAILED routes
through the REAL executor (asserted via the REAL ledger). The ONLY injected mock is detector.liveness
(the verdict) — justified: the within-W TIMING was validated for real in Inc 6 + the Inc 9 tmux contract,
so the watchdog LADDER is tested deterministically by driving the verdict (working/idle/...).

NO IMPLEMENTATION here — harnessd/watchdog.py does not exist yet. RED first.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import harnessd.config as config
import harnessd.detector as detector
import harnessd.detector_signals as detector_signals
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.review_dispatch as review_dispatch
from harnessd.detector import Liveness
from harnessd.spawn import sandbox


# ===========================================================================
# Module-under-test loader (the module does not exist yet -> RED on import).
# ===========================================================================

def _wd():
    return importlib.import_module("harnessd.watchdog")


# ===========================================================================
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path so the REAL executor's
# pathless ledger calls (read_binding/append_wal/write_binding), the EX lock, AND
# detector_signals' .signal.json / .inbox.jsonl resolution all land under tmp_path.
# ===========================================================================

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    # Clear detector_signals' private size cache so a fresh tmp transcript reads a clean baseline.
    monkeypatch.setattr(detector_signals, "_size_cache", {}, raising=False)
    return tmp_path


# ===========================================================================
# The ONE justified mock: inject the liveness verdict deterministically.
#
# The §2.9 contract has check_leaf read liveness(node) in STEP B. The frozen
# interface does not fix HOW the watchdog reaches it; the established precedent in
# this codebase is a module-level injectable (ledger.RUNTIME_ROOT,
# chokepoint.set_adapter). We bind the verdict through whichever seam the impl
# exposes and ALSO patch detector.liveness as the belt-and-suspenders default, so a
# correct impl that calls detector.liveness directly is honored too.
# ===========================================================================

def _inject_liveness(monkeypatch, wd, verdict_fn):
    """Drive the watchdog's liveness verdict deterministically (the one justified mock).

    verdict_fn(node_address) -> Liveness. Bound through every plausible seam the frozen
    interface permits, so a conformant impl (whatever its injection style) is driven:
      * a watchdog module-level set_liveness(fn) / LIVENESS attribute, if present;
      * detector.liveness itself (the live module attribute the detector calls through).
    """
    if hasattr(wd, "set_liveness"):
        wd.set_liveness(verdict_fn)
    elif hasattr(wd, "LIVENESS"):
        monkeypatch.setattr(wd, "LIVENESS", verdict_fn, raising=False)
    # Belt-and-suspenders: patch the live detector.liveness attribute (the seam the
    # detector module itself exposes). check_leaf receives a single-arg node_address-or-node;
    # accept either shape.
    def _by_address(node_or_address):
        addr = node_or_address if isinstance(node_or_address, str) else node_or_address["node_address"]
        return verdict_fn(addr)

    monkeypatch.setattr(detector, "liveness", _by_address, raising=True)


def _const_liveness(state, last_progress_at):
    def _fn(_node_address):
        return Liveness(state=state, last_progress_at=last_progress_at)
    return _fn


# ===========================================================================
# Seeding helpers — write REAL bindings through the REAL ledger; write REAL
# .signal.json / .inbox.jsonl files under the REAL runtime tree.
# ===========================================================================

LEAF = "proj/widget#exec"
REVIEW = "proj/widget#review"
PARENT = "proj#exec"
COORD = "proj#exec"
SUBAGENT = "subagent-aaaa1111"
SESSION = "sess-uuid-seed-0001"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _binding(
    *,
    node_address=LEAF,
    parent_address=PARENT,
    state="running",
    generation=0,
    lease_epoch=1,
    subagent_id=SUBAGENT,
    session_uuid=SESSION,
    transcript_path=None,
    last_progress_at=None,
    last_inbox_acked_offset=0,
    stale_check_count=0,
    stale_grace_checks=2,
    level="L5",
    extra=None,
):
    token = fencing.mint_owner_token(node_address, subagent_id, session_uuid, lease_epoch)
    rec = {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": level,
        "subagent_id": subagent_id,
        "session_uuid": session_uuid,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "idle",
        "last_progress_at": last_progress_at,
        "last_inbox_acked_offset": last_inbox_acked_offset,
        "stale_check_count": stale_check_count,
        "stale_grace_checks": stale_grace_checks,
        "recovery_attempts": 0,
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": transcript_path,
        "terminal_signal": None,
    }
    if extra:
        rec.update(extra)
    return rec, token


def _seed(bindings):
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _gate_passed_lower_child(producer_addr: str, producer_level: str):
    expected_level = {"L2": "L3", "L3": "L4", "L4": "L5"}.get(producer_level)
    if expected_level is None:
        return None
    path, _seat = _addressing.split_address(producer_addr)
    child_addr = f"{path}/lower-evidence#exec"
    child, _token = _binding(
        node_address=child_addr,
        parent_address=producer_addr,
        level=expected_level,
        state="done",
        generation=1,
        lease_epoch=1,
        subagent_id=f"{expected_level.lower()}-lower",
        session_uuid=f"{expected_level.lower()}-lower-session",
        extra={"gate_state": "gate_passed"},
    )
    return child


def _read(node=LEAF):
    return ledger.read_binding(node)


import harnessd.addressing as _addressing


def _node_dir(runtime, node_address):
    d = _addressing.node_dir(node_address, runtime)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_signal(runtime, node_address, *, signal, owner_token, evidence=None, ts=None):
    # Same canonical derivation the reader uses (nested dir + per-seat .signal.<seat>.json).
    p = _addressing.signal_path(node_address, runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload_evidence = dict(evidence or {})
    binding = ledger.read_binding(node_address)
    if binding and binding.get("gate_for"):
        producer = ledger.read_binding(binding.get("gate_for")) or {}
        if producer.get("gate_id") and "gate_id" not in payload_evidence:
            payload_evidence["gate_id"] = producer.get("gate_id")
        if producer.get("gate_review_packet") and "review_packet" not in payload_evidence:
            payload_evidence["review_packet"] = producer.get("gate_review_packet")
    payload = {
        "signal": signal,
        "ts": ts or _now_iso(),
        "owner_token": owner_token,
        "evidence": payload_evidence,
    }
    p.write_text(json.dumps(payload))
    return payload


def _append_inbox(runtime, node_address, line: dict) -> int:
    """Append one JSONL line to the per-seat wake inbox (addressing.inbox_path). Return new size."""
    p = _addressing.inbox_path(node_address, runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return p.stat().st_size


def _node_from(binding):
    return {
        "node_address": binding["node_address"],
        "transcript_path": binding.get("transcript_path"),
        "tmux_target": binding.get("tmux_target", "harness:t"),
    }


def _write_wal_record(*, node_address, event, from_state="running", to_state="running"):
    """Append one REAL framed WAL row directly (used to seed a coordinator_died EVENT)."""
    rec = ledger.build_wal_record(
        node_address=node_address,
        event=event,
        from_state=from_state,
        to_state=to_state,
        expected_generation=None,
        generation=None,
        lease_epoch=None,
        owner_token=None,
        binding_delta={},
        summary=f"seeded {event} event",
        artifacts=[],
        seq=ledger.next_seq(),
    )
    ledger.append_wal(rec)
    return rec


# ===========================================================================
# WatchdogAction tag normalization — the result type is a TAGGED action
# (COLLAPSE / NOOP / PROD / FAILED / ESCALATE / WAKE / ...). We do NOT over-fix its
# concrete shape; we read a tag robustly so the tests bind to the BEHAVIOR (which
# action fired), not an incidental field name.
# ===========================================================================

def _tag(action) -> str:
    """Best-effort uppercase tag for a WatchdogAction (kind/tag/action attr, an enum .name, or repr)."""
    for attr in ("kind", "tag", "action", "name", "type", "verb"):
        val = getattr(action, attr, None)
        if isinstance(val, str) and val:
            return val.upper()
        # an Enum-valued attr
        inner = getattr(val, "name", None)
        if isinstance(inner, str) and inner:
            return inner.upper()
    # an Enum action itself
    inner = getattr(action, "name", None)
    if isinstance(inner, str) and inner:
        return inner.upper()
    return repr(action).upper()


def _is(action, *expected_tags) -> bool:
    t = _tag(action)
    return any(e.upper() in t for e in expected_tags)


def _claim_report(body="# report\n\nDone per brief; verified.\n"):
    return (
        body.rstrip()
        + "\n\n## Drove and Watched\n\nFixture drove the recipient-visible claim.\n"
        + "\n## Inferred\n\nSupporting checks passed.\n"
        + "\n## Residual Uncertainty\n\nNone beyond fixture scope.\n"
        + "\n## Inventory\n\nNone.\n"
    )


# ===========================================================================
# BATTERY 1 — the §4.1 terminal-signal-reader battery (TERMINAL-SIGNAL FIRST).
# DONE+live-token -> COLLAPSE; DONE+stale-token -> ignored (binding UNCHANGED,
# journal stale_return_ignored); ESCALATED -> NOOP; absent -> fall through.
# ===========================================================================

def test_done_signal_live_token_collapses(runtime):
    """A fenced DONE .signal.json -> COLLAPSE, routed through the REAL executor/ledger.

    LOAD-BEARING (terminal-signal FIRST): the node's liveness verdict is driven to `idle`
    — a mutant that read liveness FIRST would FAILED an idle node instead of COLLAPSE on
    the DONE signal. Asserting COLLAPSE (and the REAL terminal collapse on disk) kills it.
    """
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token, evidence={"report": "report.md"})
    # E2 fixture completion: the return contract requires report.md at DONE.
    _e2_report_dir = _addressing.node_dir(LEAF, runtime)
    _e2_report_dir.mkdir(parents=True, exist_ok=True)
    (_e2_report_dir / "report.md").write_text(
        _claim_report("# report\n\ndone per brief.\n"),
        encoding="utf-8",
    )

    # liveness would say idle (so a liveness-first impl would mis-FAIL) — but terminal-signal wins.
    import harnessd.watchdog as _mod
    monkeypatch_holder = pytest.MonkeyPatch()
    try:
        _inject_liveness(monkeypatch_holder, _mod, _const_liveness("idle", _ago_iso(9999)))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        monkeypatch_holder.undo()

    assert _is(action, "COLLAPSE"), f"a fenced DONE signal must COLLAPSE (got tag {_tag(action)!r})"
    # The REAL executor routed the terminal collapse: the live binding is now `done` (not failed).
    after = _read()
    assert after["state"] == "done", (
        "a DONE collapse routes running->done through the REAL executor (terminal-signal FIRST: "
        "an idle liveness verdict must NOT FAIL the node)"
    )
    assert after["terminal_signal"] == "DONE"


def test_failed_signal_live_token_collapses(runtime):
    """A fenced FAILED .signal.json -> COLLAPSE (running->failed via the REAL executor)."""
    wd = _wd()
    binding, token = _binding(state="running", generation=1, lease_epoch=1)
    _seed([binding])
    _write_signal(runtime, LEAF, signal="FAILED", owner_token=token)

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "COLLAPSE"), f"a fenced FAILED signal must COLLAPSE (got {_tag(action)!r})"
    assert _read()["state"] == "failed"


def test_stale_token_signal_ignored_binding_unchanged(runtime):
    """A DONE signal with a STALE owner_token is IGNORED — binding byte-for-byte UNCHANGED.

    LOAD-BEARING (the fence): a dead incarnation's leftover DONE (epoch 1) must NEVER
    collapse a re-spawned node (epoch 3). A mutant that honors a stale signal collapses the
    live node -> caught by the unchanged-binding assertion (state stays running, no collapse).
    The watchdog journals stale_return_ignored (or simply falls through to liveness); either
    way the LIVE binding is unchanged and is NOT collapsed.
    """
    wd = _wd()
    binding, live_token = _binding(state="running", generation=5, lease_epoch=3)
    _seed([binding])
    before = copy.deepcopy(_read())
    # leftover from a PRIOR incarnation (epoch 1), a DIFFERENT token than the live one.
    stale_token = fencing.mint_owner_token(LEAF, "sa-old", "uuid-old", 1)
    assert stale_token != live_token
    _write_signal(runtime, LEAF, signal="DONE", owner_token=stale_token)

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        # Drive liveness to `working` so STEP B (fall-through) is a NOOP — isolating the fence.
        _inject_liveness(mp, _mod, _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    after = _read()
    assert after["state"] == "running", "a stale-token signal must NOT collapse the live re-spawned node"
    assert after == before, (
        "a STALE-token terminal signal is IGNORED: the live binding must be byte-for-byte UNCHANGED "
        "(no collapse, no state/epoch mutation)"
    )
    assert not _is(action, "COLLAPSE"), "a stale-token signal must never produce a COLLAPSE action"


def test_escalated_signal_is_noop_never_collapses(runtime):
    """A fenced ESCALATED signal -> NOOP. ESCALATED HOLDS ITS SLOT — never collapses — AND the
    slot-hold is JOURNALED (SML-02): the binding carries terminal_signal=ESCALATED and the WAL
    gains the §3.6 signal_ESCALATED running->running row (exactly once across ticks).

    LOAD-BEARING: a mutant that routes ESCALATED to collapse tears a waiting node off its slot
    while it waits for the answer round-trip; a bare-NOOP mutant (no journal) leaves the durable
    ledger blind to the escalation. Both are killed here.
    """
    wd = _wd()
    binding, token = _binding(state="running", generation=2, lease_epoch=1)
    _seed([binding])
    _write_signal(runtime, LEAF, signal="ESCALATED", owner_token=token)
    wal_before = len(ledger.load_wal())

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("waiting", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "NOOP", "NONE", "WAIT"), (
        f"ESCALATED must be NOOP (holds its slot), never a collapse — got {_tag(action)!r}"
    )
    assert not _is(action, "COLLAPSE", "FAILED"), "ESCALATED must NEVER collapse or fail the node"
    after = _read()
    assert after["state"] == "running", "an ESCALATED node stays running (asymmetric §3.6)"
    # SML-02: the slot-hold is DURABLE — terminal_signal stamped + signal_ESCALATED journaled.
    assert after.get("terminal_signal") == "ESCALATED", (
        "check_leaf must stamp terminal_signal=ESCALATED on the binding (the §3.6 slot-hold fact)"
    )
    escalated_rows = [
        r for r in ledger.load_wal()[wal_before:]
        if r.get("node_address") == LEAF and r.get("event") == "signal_ESCALATED"
    ]
    assert len(escalated_rows) == 1, (
        f"the ESCALATED slot-hold must journal exactly ONE signal_ESCALATED WAL row; got {len(escalated_rows)}"
    )

    # A SECOND tick re-reads the same artifact: still NOOP, NO second journal row (exactly-once).
    mp2 = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp2, _mod, _const_liveness("waiting", _now_iso()))
        action2 = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp2.undo()
    assert _is(action2, "NOOP", "NONE", "WAIT"), "the second tick holds the slot too"
    escalated_rows_2 = [
        r for r in ledger.load_wal()[wal_before:]
        if r.get("node_address") == LEAF and r.get("event") == "signal_ESCALATED"
    ]
    assert len(escalated_rows_2) == 1, (
        "exactly-once: a second tick over the SAME ESCALATED artifact must NOT journal a second row"
    )


def test_escalated_signal_journal_failure_is_routed_not_reported_clean(runtime):
    """A FENCED/CAS abort on the ESCALATED journal write must be ROUTED (NOOP with
    reason=escalate_journal_failed + the errors), never read as a clean slot-hold.

    Setup: the binding handed to check_leaf is a STALE incarnation (old epoch/token) whose
    matching .signal.json passes the READER's fence, but the LIVE ledger binding has rotated
    (re-claimed at a higher epoch) — so the escalate write aborts on the executor's fencing
    precondition. Mutant killed: treat any escalate outcome as a clean 'escalated_holds_slot'
    (the result-swallowing branch).
    """
    wd = _wd()
    # The STALE incarnation (epoch 1) — the .signal.json carries ITS token, so the reader admits it.
    stale_binding, stale_token = _binding(state="running", generation=2, lease_epoch=1)
    # The LIVE binding: same address re-claimed at a HIGHER epoch (token rotated).
    live_binding, live_token = _binding(state="running", generation=5, lease_epoch=3,
                                        session_uuid="sess-uuid-live-0002")
    _seed([live_binding])
    _write_signal(runtime, LEAF, signal="ESCALATED", owner_token=stale_token)
    before = copy.deepcopy(_read())
    wal_before = len(ledger.load_wal())

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("waiting", _now_iso()))
        action = wd.check_leaf(_node_from(stale_binding), stale_binding, now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "NOOP", "NONE", "WAIT"), "the aborted journal write still NOOPs (next tick retries)"
    detail = getattr(action, "detail", {}) or {}
    assert detail.get("reason") == "escalate_journal_failed", (
        f"a fenced/CAS-aborted escalate must be routed as escalate_journal_failed, got {detail!r}"
    )
    assert detail.get("errors"), "the routed abort must carry the executor's errors"
    after = _read()
    assert after["state"] == "running" and after.get("terminal_signal") != "ESCALATED", (
        "the LIVE binding must be untouched by the stale incarnation's escalate (non-destructive fence)"
    )
    assert after == before, "the live binding is byte-for-byte unchanged (the §3.6 FENCED de-auth)"
    new_events = [r.get("event") for r in ledger.load_wal()[wal_before:]]
    assert "signal_ESCALATED" not in new_events, (
        "a fenced escalate must NOT land a signal_ESCALATED row for the stale incarnation"
    )


def test_absent_signal_falls_through_to_liveness(runtime):
    """No .signal.json -> STEP A yields nothing -> fall through to the STEP B liveness ladder.

    With liveness driven to `working`, the fall-through is a NOOP (the node is fine). This pins
    that an absent signal is NOT itself a terminal event (read_terminal_signal returns None).
    """
    wd = _wd()
    binding, _token = _binding(state="running", generation=0, lease_epoch=1)
    _seed([binding])
    _node_dir(runtime, LEAF)  # dir exists, NO .signal.json

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert not _is(action, "COLLAPSE"), "an absent signal is not a terminal event -> no collapse"
    assert _read()["state"] == "running"


# ===========================================================================
# BATTERY 2 — the watchdog-leaf battery (idle->prod->FAILED ladder, two-counter).
# idle+age>W -> PROD (gated); repeated idle past grace -> FAILED; FAILED -> marks
# failed via the executor (actor='harnessd') AND ESCALATES TO PARENT, NOT respawn.
# ===========================================================================

def _patch_prod_gate(mp, wd, allow: bool):
    """Force prod_precondition (the prompt-string/capture-pane gate) to a deterministic value.

    The gate reads a real captured pane in production; here it is the second-most natural seam to
    drive (alongside the liveness verdict). We patch the module attribute the impl exposes.
    """
    if hasattr(wd, "prod_precondition"):
        mp.setattr(wd, "prod_precondition", lambda _node: allow, raising=True)


def test_idle_beyond_w_prods_when_gated_open(runtime):
    """idle + age>W + prod gate OPEN + within grace -> PROD (a nudge, NOT a collapse/fail).

    LOAD-BEARING (the ladder respects grace before FAILED): with stale_check_count below
    stale_grace_checks, the first idle poll PRODS — it does NOT FAIL. A mutant that fails on
    first idle is caught (this asserts PROD, and the node stays running).
    """
    wd = _wd()
    binding, _token = _binding(
        state="running", generation=0, lease_epoch=1,
        last_progress_at=_ago_iso(9999), stale_check_count=0, stale_grace_checks=2,
    )
    _seed([binding])

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=True)
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "PROD"), f"idle within grace + gate open must PROD (got {_tag(action)!r})"
    assert not _is(action, "FAILED", "COLLAPSE"), "a first idle poll within grace must NOT fail the node"
    assert _read()["state"] == "running", "a prodded node is still running (not yet failed)"


def test_prod_gate_blocks_mid_tool_call(runtime):
    """prod_precondition False (pane NOT at the idle prompt) -> NO prod fired.

    LOAD-BEARING: a send-keys nudge that lands mid-tool-call corrupts the input line. The gate is
    what prevents it. A mutant that prods regardless of the gate -> caught (assert NOT a PROD).
    """
    wd = _wd()
    binding, _token = _binding(
        state="running", generation=0, lease_epoch=1,
        last_progress_at=_ago_iso(9999), stale_check_count=0, stale_grace_checks=2,
    )
    _seed([binding])

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=False)
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert not _is(action, "PROD"), (
        f"the prod gate (prod_precondition False) must SUPPRESS the prod — got {_tag(action)!r}"
    )


def test_repeated_idle_past_grace_marks_failed_via_executor(runtime):
    """idle past stale_grace_checks -> FAILED, marked through the REAL executor.

    LOAD-BEARING (bounded prods THEN failed): only once stale_check_count has reached the grace
    threshold does the ladder mark FAILED. The FAILED is written through the REAL executor — the
    live binding on disk goes running->failed. A mutant that fails on first idle is killed by the
    grace-respecting PROD test above; this one proves the terminal rung is reached AT grace.
    """
    wd = _wd()
    binding, _token = _binding(
        state="running", generation=0, lease_epoch=1,
        last_progress_at=_ago_iso(9999),
        stale_check_count=2, stale_grace_checks=2,   # already AT grace -> this poll fails
    )
    _seed([binding])

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=True)
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "FAILED", "ESCALATE"), (
        f"idle AT/over grace must mark FAILED (got {_tag(action)!r})"
    )
    after = _read()
    assert after["state"] == "failed", (
        "the watchdog marks running->failed through the REAL executor once the prod ladder exhausts"
    )


def test_watchdog_failed_row_marked_harnessd_nonresponse(runtime):
    """The watchdog-imposed FAILED row carries actor='harnessd' + reason watchdog_nonresponse.

    Distinguishes a watchdog-declared FAILED (no .signal.json) from an agent-self-emitted FAILED.
    Asserted against the REAL run-ledger (load_wal).
    """
    wd = _wd()
    binding, _token = _binding(
        state="running", generation=0, lease_epoch=1,
        last_progress_at=_ago_iso(9999),
        stale_check_count=2, stale_grace_checks=2,
    )
    _seed([binding])

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=True)
        wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    wal = ledger.load_wal()
    # The failing row: state landed in failed, written by the single writer (actor harnessd).
    fail_rows = [r for r in wal if r.get("to_state") == "failed" and r.get("node_address") == LEAF]
    assert fail_rows, "a watchdog FAILED must append a run-ledger row landing in `failed`"
    row = fail_rows[-1]
    assert row.get("actor") == "harnessd", "the watchdog FAILED row is written by actor='harnessd'"
    blob = json.dumps(row).lower()
    assert "watchdog_nonresponse" in blob or "watchdog" in blob, (
        "the watchdog-imposed FAILED must carry a watchdog_nonresponse reason (distinct from an "
        "agent-self-emitted FAILED)"
    )


def test_third_actor_death_parks_address_with_three_cause_event(runtime):
    """The third consecutive actor-bearing death parks the stable address.

    Two prior causes are durable binding truth. The current watchdog death is the
    third: its terminal transition must carry the park and one typed WAL event
    must name all three causes. A fourth actor is forbidden by the spawn tests.
    """
    wd = _wd()
    binding, _token = _binding(
        state="running",
        generation=4,
        lease_epoch=7,
        last_progress_at=_ago_iso(9999),
        stale_check_count=2,
        stale_grace_checks=2,
        extra={
            "consecutive_failed_incarnations": 2,
            "failed_incarnation_causes": [
                {"event": "died_infrastructure", "reason": "pane_gone"},
                {"event": "watchdog_runtime_failure", "reason": "auth_rate_limited"},
            ],
        },
    )
    _seed([binding])

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=True)
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "FAILED", "ESCALATE")
    after = _read()
    assert after["consecutive_failed_incarnations"] == 3
    assert after["respawn_parked_at"]
    causes = after["failed_incarnation_causes"]
    assert len(causes) == 3
    assert [cause["event"] for cause in causes] == [
        "died_infrastructure",
        "watchdog_runtime_failure",
        "watchdog_nonresponse",
    ]
    park_rows = [
        row
        for row in ledger.load_wal()
        if row.get("node_address") == LEAF
        and row.get("event") == "seat_respawn_parked"
    ]
    assert len(park_rows) == 1
    assert [cause["event"] for cause in park_rows[0]["binding_delta"]["causes"]] == [
        "died_infrastructure",
        "watchdog_runtime_failure",
        "watchdog_nonresponse",
    ]


def test_recent_turn_state_evidence_resets_idle_nonresponse_ladder(runtime):
    """A fresh fenced turn edge is life evidence even when the pane verdict is idle.

    Run 4 produced turn-end prods seconds before the idle ladder killed the seat.
    The existing healthy checkpoint must reset the accrued ladder rather than
    allowing pane idleness to declare nonresponse.
    """
    wd = _wd()
    binding, token = _binding(
        state="running",
        generation=2,
        lease_epoch=3,
        last_progress_at=_ago_iso(9999),
        stale_check_count=2,
        stale_grace_checks=2,
        extra={"turn_hook_profile": "claude_full_edges"},
    )
    _seed([binding])
    turn_path = _addressing.turn_state_path(LEAF, runtime)
    turn_path.parent.mkdir(parents=True, exist_ok=True)
    turn_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "node_address": LEAF,
                "owner_token": token,
                "hook_profile": "claude_full_edges",
                "state": "turn_ended",
                "in_flight_tools": [],
                "updated_at": _now_iso(),
                "last_hook_event": "Stop",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=True)
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "NOOP")
    assert action.detail["reason"] == "life_evidence_within_w"
    after = _read()
    assert after["state"] == "running"
    assert after["stale_check_count"] == 0
    assert after["last_evidence"] == "turn_state"


@pytest.mark.parametrize(
    ("signal", "expected_state"),
    [
        ("DONE", "done"),
        ("FAILED", "failed"),
        ("ESCALATED", "running"),
    ],
)
def test_consumed_fenced_signal_resets_consecutive_actor_deaths(
    runtime,
    signal,
    expected_state,
):
    """A successfully consumed fenced terminal signal proves a functioning seat."""
    wd = _wd()
    binding, token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={
            "consecutive_failed_incarnations": 2,
            "failed_incarnation_causes": [
                {"event": "died_infrastructure", "reason": "pane_gone"},
                {"event": "watchdog_nonresponse", "reason": "idle_ladder"},
            ],
        },
    )
    _seed([binding])
    _write_signal(
        runtime,
        LEAF,
        signal=signal,
        owner_token=token,
        evidence={"report": "report.md"},
    )
    if signal == "DONE":
        report_dir = _addressing.node_dir(LEAF, runtime)
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "report.md").write_text(_claim_report(), encoding="utf-8")

    action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())

    assert _is(action, "COLLAPSE", "NOOP")
    after = _read()
    assert after["state"] == expected_state
    assert after["consecutive_failed_incarnations"] == 0
    assert after["failed_incarnation_causes"] == []
    assert after.get("respawn_parked_at") is None


def test_recent_transcript_growth_resets_idle_nonresponse_ladder(runtime):
    transcript = runtime / "transcripts" / "active.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
    binding, _token = _binding(
        state="running",
        transcript_path=str(transcript),
        last_progress_at=_ago_iso(9999),
        stale_check_count=2,
        stale_grace_checks=2,
    )
    _seed([binding])

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        action = _wd().check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "NOOP")
    assert action.detail["reason"] == "life_evidence_within_w"
    assert _read()["last_evidence"] == "transcript"
    assert _read()["stale_check_count"] == 0


def test_recent_codex_runtime_log_growth_resets_idle_nonresponse_ladder(runtime):
    worker_home = runtime / ".codex-pinned" / "seats" / "seat-1"
    transcript = (
        worker_home
        / "sessions"
        / "2026"
        / "07"
        / "28"
        / "rollout.jsonl"
    )
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(seconds=9999)).timestamp()
    os.utime(transcript, (old, old))
    (worker_home / "logs_2.sqlite").write_bytes(b"sqlite activity")
    binding, _token = _binding(
        state="running",
        transcript_path=str(transcript),
        last_progress_at=_ago_iso(9999),
        stale_check_count=2,
        stale_grace_checks=2,
        extra={"runtime": "codex", "codex_seat_id": "seat-1"},
    )
    _seed([binding])

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        action = _wd().check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "NOOP")
    assert action.detail["reason"] == "life_evidence_within_w"
    assert _read()["last_evidence"] == "codex_runtime_log"
    assert _read()["stale_check_count"] == 0


def test_stale_runtime_files_do_not_reset_idle_nonresponse_ladder(runtime):
    transcript = runtime / "transcripts" / "stale.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(seconds=9999)).timestamp()
    os.utime(transcript, (old, old))
    binding, _token = _binding(
        state="running",
        transcript_path=str(transcript),
        last_progress_at=_ago_iso(9999),
        stale_check_count=2,
        stale_grace_checks=2,
    )
    _seed([binding])

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        action = _wd().check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "FAILED", "ESCALATE")
    assert _read()["state"] == "failed"


def test_dead_verdict_wins_over_fresh_runtime_file(runtime):
    transcript = runtime / "transcripts" / "last-write-before-death.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
    binding, _token = _binding(
        state="running",
        transcript_path=str(transcript),
        last_progress_at=_ago_iso(9999),
        stale_check_count=2,
        stale_grace_checks=2,
    )
    _seed([binding])

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("dead", _ago_iso(9999)))
        action = _wd().check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "NOOP")
    assert action.detail["liveness_state"] == "dead"
    assert _read()["stale_check_count"] == 2


def test_watchdog_failed_escalates_to_parent_not_respawn(runtime):
    """A FAILED leaf ESCALATES TO THE PARENT and does NOT auto-respawn from harnessd.

    LOAD-BEARING (the v1 closing action): the watchdog marks FAILED + escalates to the parent
    (who re-claims at the stable address); harnessd does NOT itself spawn/resume the leaf. A
    mutant that auto-respawns from harnessd is caught by:
      (1) the returned action escalates to the parent (carries the parent address), AND
      (2) NO fresh spawn/claim/resume WAL row appears for the leaf (no slot_claimed/spawn_open/
          spawn_running/release_claim after the FAILED).
    """
    wd = _wd()
    binding, _token = _binding(
        state="running", generation=0, lease_epoch=1, parent_address=PARENT,
        last_progress_at=_ago_iso(9999),
        stale_check_count=2, stale_grace_checks=2,
    )
    # Seed the parent too so the escalation has a real parent address to target.
    parent, _pt = _binding(node_address=PARENT, parent_address="", state="running", generation=3, lease_epoch=2)
    _seed([binding, parent])

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=True)
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    # (1) The action escalates to the PARENT (the parent re-claims at the stable address).
    assert _is(action, "ESCALATE", "FAILED"), f"a FAILED leaf must escalate to the parent (got {_tag(action)!r})"
    blob = (repr(action) + json.dumps(getattr(action, "__dict__", {}), default=str)).lower()
    assert PARENT.lower() in blob or "parent" in blob, (
        "the FAILED closing action must be parent-directed (the parent re-claims at the stable address)"
    )

    # (2) harnessd does NOT auto-respawn: no fresh spawn/claim/resume row for the leaf.
    wal = ledger.load_wal()
    respawn_events = {"slot_claimed", "claim", "spawn_open", "spawn_running", "release_claim", "resume"}
    leaf_respawn_rows = [
        r for r in wal
        if r.get("node_address") == LEAF and r.get("event") in respawn_events
    ]
    assert not leaf_respawn_rows, (
        "v1 does NOT auto-respawn from harnessd: a watchdog FAILED must NOT emit a fresh "
        f"spawn/claim/resume for the leaf (found {[r.get('event') for r in leaf_respawn_rows]})"
    )
    # The leaf is left `failed` (the parent acts next), never re-driven back to claimed/running here.
    assert _read()["state"] == "failed"


def test_watchdog_nonresponse_failed_notifies_parent_inbox_and_recovers(runtime):
    """A watchdog-imposed FAILED wakes the parent like an agent FAILED collapse.

    Live run build-logview-20260616T0429Z parked when a Codex-auth L5 failed via
    watchdog_nonresponse and its L4 parent received no inbox pointer. The WAL action carried a
    parent target, but no durable inbox notification was written, so the parent kept waiting for a
    child DONE nudge that could never arrive.
    """
    wd = _wd()
    binding, _token = _binding(
        state="running",
        generation=0,
        lease_epoch=7,
        parent_address=PARENT,
        last_progress_at=_ago_iso(9999),
        stale_check_count=2,
        stale_grace_checks=2,
    )
    parent, _pt = _binding(
        node_address=PARENT,
        parent_address="",
        state="running",
        level="L4",
        generation=3,
        lease_epoch=2,
    )
    _seed([binding, parent])
    _prepare_node(runtime)

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=True)
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "ESCALATE", "FAILED")
    inbox = _addressing.inbox_path(PARENT, runtime)
    notes = [
        line for line in _jsonl(inbox)
        if line.get("type") == "child_collapsed"
        and line.get("child") == LEAF
        and line.get("terminal_signal") == "FAILED"
    ]
    assert len(notes) == 1
    assert notes[0].get("collapse_lease_epoch") == 7

    inbox.unlink()
    from harnessd import daemon

    daemon._recover_terminal_notifications_best_effort()
    replayed = [
        line for line in _jsonl(inbox)
        if line.get("type") == "child_collapsed"
        and line.get("child") == LEAF
        and line.get("terminal_signal") == "FAILED"
    ]
    assert len(replayed) == 1
    assert replayed[0].get("collapse_lease_epoch") == 7

    daemon._recover_terminal_notifications_best_effort()
    assert len([
        line for line in _jsonl(inbox)
        if line.get("type") == "child_collapsed"
        and line.get("child") == LEAF
        and line.get("terminal_signal") == "FAILED"
    ]) == 1


def test_codex_midrun_auth_error_classifies_as_auth_expired_not_nonresponse(runtime):
    """A persisted Codex auth error is runtime-auth evidence, not generic nonresponse.

    LR-33 catches immediate auth errors during spawn discovery. This covers the
    later/mid-run path: if a Codex transcript records the same unauthorized
    refresh-token error and the idle ladder exhausts, the watchdog must preserve
    the typed auth class instead of reporting a vague watchdog_nonresponse.
    """
    wd = _wd()
    transcript = runtime / "transcripts" / "codex-auth.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-06-16T05:19:00.812Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "codex-auth-session",
                            "cwd": str(runtime),
                            "originator": "codex-tui",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-16T05:19:01.388Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "error",
                            "message": (
                                "Your access token could not be refreshed because your "
                                "refresh token was already used. Please log out and sign in again."
                            ),
                            "codex_error_info": "unauthorized",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    binding, _token = _binding(
        state="running",
        generation=0,
        lease_epoch=8,
        parent_address=PARENT,
        transcript_path=str(transcript),
        last_progress_at=_ago_iso(9999),
        stale_check_count=2,
        stale_grace_checks=2,
    )
    parent, _pt = _binding(
        node_address=PARENT,
        parent_address="",
        state="running",
        level="L4",
        generation=3,
        lease_epoch=2,
    )
    _seed([binding, parent])
    _prepare_node(runtime)

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=True)
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "ESCALATE", "FAILED")
    detail = getattr(action, "detail", None) or {}
    assert detail.get("reason") == "auth_expired"
    assert detail.get("failure_class") == "auth_expired"

    after = _read()
    assert after["state"] == "failed"
    assert after["terminal_signal"] == "FAILED"
    assert after["terminal_note"] == "auth_expired"
    assert after["failure_class"] == "auth_expired"
    assert after["runtime_failure"]["failure_class"] == "auth_expired"
    assert after["runtime_failure"]["error_code"] == "unauthorized"

    wal = ledger.load_wal()
    fail_rows = [r for r in wal if r.get("event") == "watchdog_runtime_failure"]
    assert fail_rows, "typed runtime-auth failures must be journaled separately from nonresponse"
    row = fail_rows[-1]
    assert row["binding_delta"]["failure_class"] == "auth_expired"
    assert row["binding_delta"]["runtime_failure"]["failure_class"] == "auth_expired"
    assert "watchdog_nonresponse" not in json.dumps(row)

    parent_lines = _jsonl(_addressing.inbox_path(PARENT, runtime))
    notes = [line for line in parent_lines if line.get("type") == "child_collapsed"]
    assert len(notes) == 1, "runtime-auth FAILED still wakes the parent as a terminal child failure"
    assert notes[0]["terminal_note"] == "auth_expired"


def test_codex_midrun_rate_limit_classifies_as_auth_rate_limited(runtime):
    """A persisted Codex 429/capacity error is infra evidence, not semantic agent failure."""
    wd = _wd()
    transcript = runtime / "transcripts" / "codex-rate-limit.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-06-16T05:20:00.812Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "codex-rate-session",
                            "cwd": str(runtime),
                            "originator": "codex-tui",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-16T05:20:01.388Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "error",
                            "message": "Too many requests. Please try again later.",
                            "codex_error_info": "rate_limit_exceeded",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    binding, _token = _binding(
        state="running",
        generation=0,
        lease_epoch=9,
        parent_address=PARENT,
        transcript_path=str(transcript),
        last_progress_at=_ago_iso(9999),
        stale_check_count=2,
        stale_grace_checks=2,
    )
    parent, _pt = _binding(
        node_address=PARENT,
        parent_address="",
        state="running",
        level="L4",
        generation=3,
        lease_epoch=2,
    )
    _seed([binding, parent])
    _prepare_node(runtime)

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("idle", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=True)
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "ESCALATE", "FAILED")
    detail = getattr(action, "detail", None) or {}
    assert detail.get("reason") == "auth_rate_limited"
    assert detail.get("failure_class") == "auth_rate_limited"

    after = _read()
    assert after["state"] == "failed"
    assert after["terminal_note"] == "auth_rate_limited"
    assert after["failure_class"] == "auth_rate_limited"
    assert after["runtime_failure"]["failure_class"] == "auth_rate_limited"
    assert after["runtime_failure"]["error_code"] == "rate_limit_exceeded"

    wal = ledger.load_wal()
    fail_rows = [r for r in wal if r.get("event") == "watchdog_runtime_failure"]
    assert fail_rows
    row = fail_rows[-1]
    assert row["binding_delta"]["failure_class"] == "auth_rate_limited"
    assert row["binding_delta"]["runtime_failure"]["failure_class"] == "auth_rate_limited"
    assert "watchdog_nonresponse" not in json.dumps(row)

    parent_lines = _jsonl(_addressing.inbox_path(PARENT, runtime))
    notes = [line for line in parent_lines if line.get("type") == "child_collapsed"]
    assert len(notes) == 1
    assert notes[0]["terminal_note"] == "auth_rate_limited"
    assert notes[0]["failure_class"] == "auth_rate_limited"


def test_escalated_leaf_is_never_prodded(runtime):
    """An ESCALATED leaf reads `waiting` and is NEVER prodded (it holds its slot, §2.4/§4.1).

    A fenced ESCALATED signal routes to NOOP at STEP A — the prod ladder is never entered, so no
    PROD/FAILED/COLLAPSE fires even though it is flat-beyond-W.
    """
    wd = _wd()
    binding, token = _binding(state="running", generation=0, lease_epoch=1, last_progress_at=_ago_iso(9999))
    _seed([binding])
    _write_signal(runtime, LEAF, signal="ESCALATED", owner_token=token)

    import harnessd.watchdog as _mod
    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _mod, _const_liveness("waiting", _ago_iso(9999)))
        _patch_prod_gate(mp, _mod, allow=True)
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert not _is(action, "PROD", "FAILED", "COLLAPSE"), (
        f"an ESCALATED (waiting) leaf must never be prodded or failed — got {_tag(action)!r}"
    )
    assert _read()["state"] == "running"


# ===========================================================================
# BATTERY 2b — prod helpers: prod_precondition / confirm_prod_worked (verify-new-turn).
# ===========================================================================

def test_confirm_prod_worked_true_only_on_new_turn(runtime, tmp_path):
    """confirm_prod_worked re-reads the JSONL: True iff a NEW turn appeared since the prod.

    send-keys is fire-and-forget (no ack); the watchdog confirms a prod 'worked' ONLY by observing
    forward progress (a grown transcript), never by assuming the keystroke landed.
    """
    wd = _wd()
    p = tmp_path / "transcript.jsonl"
    p.write_bytes(b'{"type":"summary","boot":true}\n')
    size_before = p.stat().st_size
    binding, _t = _binding(state="running", transcript_path=str(p))
    node = _node_from(binding)

    # No new turn yet -> not confirmed.
    assert wd.confirm_prod_worked(node, size_before) is False, (
        "no agent-progress JSONL row since the prod -> confirm_prod_worked False "
        "(no blind trust of send-keys)"
    )

    # A new assistant/model turn appended -> confirmed.
    p.write_bytes(b'{"type":"summary","boot":true}\n{"type":"assistant","message":{"role":"assistant"}}\n')
    assert wd.confirm_prod_worked(node, size_before) is True, (
        "a new agent-progress JSONL row after the prod -> confirm_prod_worked True "
        "(forward progress observed)"
    )


# ===========================================================================
# BATTERY 3 — the ③-wake battery (EDGE-TRIGGERED: one nudge per NEW inbox line).
# inbox_has_unacked True iff a line was appended AFTER last_inbox_acked_offset;
# wake_keystroke is a POINTER, never a payload.
# ===========================================================================

def test_inbox_unacked_true_for_line_past_watermark(runtime):
    """A line appended PAST last_inbox_acked_offset -> inbox_has_unacked True (the wake TRIGGER)."""
    wd = _wd()
    binding, _t = _binding(state="running", last_inbox_acked_offset=0)
    _seed([binding])
    _append_inbox(runtime, LEAF, {"from": "parent", "msg": "new message"})

    assert wd.inbox_has_unacked(_node_from(binding), _read()) is True, (
        "a line appended past last_inbox_acked_offset is UNACKED -> the wake trigger fires"
    )


def test_inbox_no_new_line_no_nudge(runtime):
    """No append past the watermark -> inbox_has_unacked False (EDGE-TRIGGERED, no storm).

    LOAD-BEARING (edge-triggered): with the watermark already at end-of-file (everything acked),
    a re-poll with NOTHING new returns False. A mutant that nudges every poll (or with nothing new)
    is caught here.
    """
    wd = _wd()
    # Seed one line, then set the acked watermark to the current end-of-file (all caught up).
    size = _append_inbox(runtime, LEAF, {"from": "parent", "msg": "already read"})
    binding, _t = _binding(state="running", last_inbox_acked_offset=size)
    _seed([binding])

    assert wd.inbox_has_unacked(_node_from(binding), _read()) is False, (
        "no line appended past the acked offset -> NO unacked -> NO nudge (edge-triggered, not per-poll)"
    )


def test_inbox_one_nudge_per_new_line_edge_triggered(runtime):
    """Exactly ONE nudge per NEW line: after acking, a re-poll with nothing new yields no further nudge.

    LOAD-BEARING (one per new line): append a line -> unacked True (one nudge owed). Advance the
    acked watermark to end-of-file (the nudge consumed it) -> a re-poll yields False (no second
    nudge for the same line). A mutant that re-nudges an already-acked line is caught.
    """
    wd = _wd()
    binding0, _t = _binding(state="running", last_inbox_acked_offset=0)
    _seed([binding0])
    size_after_line = _append_inbox(runtime, LEAF, {"from": "parent", "msg": "wake up"})

    # First poll: a new line is unacked -> the trigger fires (one nudge owed).
    assert wd.inbox_has_unacked(_node_from(binding0), _read()) is True

    # The nudge consumed the line: advance the watermark to end-of-file (the ack).
    acked, _t2 = _binding(state="running", last_inbox_acked_offset=size_after_line)
    _seed([acked])

    # Second poll, NOTHING new appended: no further nudge (edge-triggered, one per new line).
    assert wd.inbox_has_unacked(_node_from(acked), _read()) is False, (
        "after acking the line, a re-poll with nothing new must NOT fire a second nudge (one per new line)"
    )


def test_wake_keystroke_is_a_pointer_not_a_payload(runtime):
    """wake_keystroke returns a POINTER ('re-read your inbox / resume'), NEVER a fact/payload.

    The notification tells the agent when to read; the keystroke carries only
    sender/count metadata and the inbox pointer. A mutant that stuffs the
    message content into the keystroke is caught.
    """
    wd = _wd()
    binding, _t = _binding(state="running")
    _append_inbox(
        runtime,
        binding["node_address"],
        {"from": "proj#exec", "type": "message", "message": "SECRET-PAYLOAD"},
    )
    payload = wd.wake_keystroke(_node_from(binding))
    assert isinstance(payload, str) and payload.strip(), "wake_keystroke is a non-empty send-keys string"
    low = payload.lower()
    assert "inbox" in low and ("re-read" in low or "reread" in low or "read" in low), (
        "the wake keystroke must POINT at the inbox re-read (a pointer), never carry a payload/fact"
    )
    assert "1 new message: 1 from proj#exec" in payload
    assert "SECRET-PAYLOAD" not in payload


# ===========================================================================
# BATTERY 4 — the coordinator-death battery.
# dead-pid + live children -> ESCALATE (recoverable orphan, recover-vs-reap deferred);
# quiet pane-alive + live children -> waiting (not dead).
# ===========================================================================

def test_coordinator_dead_pid_live_children_escalates(runtime):
    """A dead-pid coordinator WITH live children -> ESCALATE (recoverable orphan, NOT reap).

    LOAD-BEARING (recover-vs-reap deferred): a coordinator whose process died but whose children
    are still alive is a recoverable orphan — v1 ESCALATES (the choice is deferred, §5.2/§5.5). A
    mutant that REAPS it (collapses to failed/dead unilaterally) is caught: the action must be
    ESCALATE, never a collapse/reap.
    """
    wd = _wd()
    coord, _ct = _binding(node_address=COORD, parent_address="", state="dead", level="L3", generation=2, lease_epoch=2)
    # A LIVE child below (running) — the disambiguator that makes the dead coordinator an orphan.
    child, _cht = _binding(node_address="proj/child#exec", parent_address=COORD, state="running", generation=0, lease_epoch=1)
    _seed([coord, child])
    # A coordinator_died EVENT in the run-ledger (the watchdog keys off the EVENT, not a phantom field).
    _write_wal_record(node_address=COORD, event="coordinator_died", from_state="running", to_state="dead")

    action = wd.check_coordinator_death(_node_from(coord), _read(COORD), ledger)

    assert _is(action, "ESCALATE"), (
        f"a dead-pid coordinator with live children is a recoverable ORPHAN -> ESCALATE (got {_tag(action)!r})"
    )
    assert not _is(action, "COLLAPSE", "FAILED", "REAP", "KILL"), (
        "a live coordinator with live children must NEVER be reaped (recover-vs-reap is deferred)"
    )
    # The coordinator and its live child are left intact (escalate, never reap).
    assert _read("proj/child#exec")["state"] == "running", "the live child must not be reaped"


def test_coordinator_quiet_alive_with_children_is_waiting(runtime):
    """A QUIET but pane-ALIVE coordinator with live children -> waiting (not dead, not actionable).

    LOAD-BEARING (the pane_pid probe disambiguates quiet-vs-dead): a coordinator that merely went
    quiet with live descendants is `waiting`, NOT an orphan. No coordinator_died event, state is
    not dead -> the probe returns waiting. A mutant that treats quiet-with-children as dead (and
    escalates/reaps) is caught: the action must be a benign waiting, never an escalate/reap.
    """
    wd = _wd()
    coord, _ct = _binding(node_address=COORD, parent_address="", state="running", level="L3", generation=2, lease_epoch=2)
    child, _cht = _binding(node_address="proj/child#exec", parent_address=COORD, state="running", generation=0, lease_epoch=1)
    _seed([coord, child])
    # NO coordinator_died event; the coordinator's state is running (not dead).

    action = wd.check_coordinator_death(_node_from(coord), _read(COORD), ledger)

    assert _is(action, "WAIT", "NOOP", "WAITING", "NONE"), (
        f"a quiet-but-alive coordinator with live children is WAITING, not dead -> got {_tag(action)!r}"
    )
    assert not _is(action, "ESCALATE", "COLLAPSE", "FAILED", "REAP"), (
        "a merely-quiet coordinator with live children must not be escalated or reaped (it is waiting)"
    )


# ===========================================================================
# E2 — the RETURN-CONTRACT walker on the sign-off path (enforcement spine).
#
# "The hook rejects it — you cannot report complete" (L1/L2 role docs,
# intent-spec-contract) made TRUE at runtime: a fenced DONE whose return
# artifacts fail the deterministic floor is REFUSED before collapse — one
# edge-triggered typed-defect WAL row + one inbox defect line; the agent fixes
# and re-signals. FAILED/ESCALATED are exempt (never trap).
# ===========================================================================

def _prepare_node(runtime, node_address=LEAF, *, report="# report\n\nDone per brief; verified.\n"):
    d = _addressing.node_dir(node_address, runtime)
    d.mkdir(parents=True, exist_ok=True)
    if report is not None:
        (d / "report.md").write_text(_claim_report(report), encoding="utf-8")
    return d


def _wal_rows(event):
    return [r for r in ledger.load_wal() if r.get("event") == event]


def _jsonl(path):
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_short_review_plan(runtime, review_addr, gate_id="gate-test"):
    d = _addressing.node_dir(review_addr, runtime) / "reviews" / gate_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (d / "review-plan.md").write_text(
        "# review plan\n\n"
        "**Review Mode:** SHORT\n"
        "**Short Review Exception:** YES\n\n"
        "## Role Selection\n\n"
        "Short review selected because the candidate is one right-sized review task and every "
        "exception row has direct evidence.\n\n"
        "## Short Review Exception\n\n"
        "| Condition | YES | Evidence Pointer | Rationale |\n"
        "|---|---|---|---|\n"
        "| Output count is 1-2 | YES | report.md | one submitted output |\n"
        "| No shared state or sequencing dependency | YES | report.md | no dependency named |\n"
        "| Obligations map to one output | YES | report.md | mapping is direct |\n"
        "| Local review has verdict and evidence | YES | report.md | verdict pointer present |\n"
        "| Boundary is unchanged or evidenced | YES | report.md | no boundary change named |\n"
        "| Handoff names evidence and risks | YES | report.md | handoff is explicit |\n",
        encoding="utf-8",
    )
    return d


def _write_full_l4_review_reports(runtime, review_addr, gate_id="gate-test"):
    d = _addressing.node_dir(review_addr, runtime) / "reviews" / gate_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (d / "review-plan.md").write_text(
        "# review plan\n\n**Review Mode:** FULL\n\n"
        "## Role Selection\n\n"
        "Use every L4 workstream check because this is a normal composition review: "
        "fidelity-coverage, composition-interface, evidence-credibility, risk-readiness.\n\n"
        "Reports To Read Before Synthesis: all L4 workstream checks.\n",
        encoding="utf-8",
    )
    for name in review_dispatch.required_check_report_names("L4+"):
        path = d / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {name}\n\n**Recommended Routing:** accept-note\n\nNo material finding.\n",
            encoding="utf-8",
        )
    return d


def _seed_done_review_check_bindings(review_addr, producer_addr, gate_dir, gate_id="gate-test"):
    live = dict(ledger.all_nodes())
    for spec in review_dispatch.required_review_check_specs("L4+"):
        slug = spec["slug"]
        report = review_dispatch.review_check_report_path(gate_dir, spec)
        address = f"{producer_addr.split('#', 1)[0]}/reviews/{gate_id}/reviewers/{slug}#exec"
        live[address] = {
            "node_address": address,
            "parent_address": review_addr,
            "level": "L4+",
            "state": "done",
            "review_check_for": review_addr,
            "review_check_candidate": producer_addr,
            "gate_id": gate_id,
            "review_check_axis": slug,
            "review_check_report": str(report),
            "verdict_authority": False,
        }
    ledger.write_binding(live, _lock_held=True)


def _write_gate_artifact(runtime, review_addr, name, text, gate_id="gate-test"):
    d = _addressing.node_dir(review_addr, runtime) / "reviews" / gate_id
    d.mkdir(parents=True, exist_ok=True)
    packet = d / "review-packet.md"
    if not packet.exists():
        packet.write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    path = d / name
    path.write_text(text, encoding="utf-8")
    return path


def test_e2_done_without_report_is_refused_edge_triggered(runtime):
    """DONE with NO report.md -> the collapse is REFUSED (NOOP return_contract_failed, the node
    stays running), ONE typed-defect WAL row + ONE inbox line land, and a steady-state re-poll
    journals NOTHING more. (Mutant: collapse without the walker -> state done -> caught.)"""
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    _prepare_node(runtime, report=None)  # node dir exists, NO report.md
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action, "NOOP"), f"a contract-failing DONE must NOT collapse (got {_tag(action)!r})"
        assert (getattr(action, "detail", None) or {}).get("reason") == "return_contract_failed"
        assert _read()["state"] == "running", "the node must STAY running (signal deferred, not lost)"
        rows = _wal_rows("return_contract_failed")
        assert len(rows) == 1, "exactly ONE typed-defect row on first detection"
        assert "MISSING-REPORT" in (rows[0].get("summary") or ""), "the row must NAME the defect"
        inbox = _addressing.inbox_path(LEAF, runtime)
        assert inbox.is_file() and "return_contract_defect" in inbox.read_text(encoding="utf-8")

        # steady-state re-poll: refused again, but NO second row / inbox line (edge-triggered)
        action2 = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action2, "NOOP")
        assert len(_wal_rows("return_contract_failed")) == 1, "re-poll must journal NOTHING more"
    finally:
        mp.undo()


def test_e2_return_contract_defect_idempotency_uses_signal_artifact_identity(runtime):
    """A fresh signal artifact is a new refusal edge even if the agent reuses the same ts.

    Agent-authored timestamps are evidence, not identity. The harness-observed artifact identity is
    the durable idempotency key.
    """
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    _prepare_node(runtime, report=None)
    reused_ts = "2026-06-15T00:00:00+00:00"
    _write_signal(
        runtime,
        LEAF,
        signal="DONE",
        owner_token=token,
        evidence={"attempt": "first"},
        ts=reused_ts,
    )

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action, "NOOP")
        assert len(_wal_rows("return_contract_failed")) == 1

        _write_signal(
            runtime,
            LEAF,
            signal="DONE",
            owner_token=token,
            evidence={"attempt": "second"},
            ts=reused_ts,
        )
        action2 = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action2, "NOOP")
        rows = _wal_rows("return_contract_failed")
        assert len(rows) == 2
        identities = [
            ((r.get("binding_delta") or {}).get("signal_artifact_seen_at") or "")
            for r in rows
        ]
        assert len(set(identities)) == 2
    finally:
        mp.undo()


def test_e2_done_with_report_collapses(runtime):
    """The floor satisfied (report.md present) -> the DONE collapse proceeds exactly as before."""
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    _prepare_node(runtime)
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()
    assert _is(action, "COLLAPSE")
    assert _read()["state"] == "done"


def test_e2_test_author_done_requires_nonempty_red_run_log(runtime):
    """A test-author cannot submit DONE until its package carries the observed-red record."""
    wd = _wd()
    binding, token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={"child_purpose": "test_author"},
    )
    _seed([binding])
    d = _addressing.node_dir(LEAF, runtime)
    (d / "tests").mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(
        "# report\n\nAuthored the acceptance package.\n",
        encoding="utf-8",
    )
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        refused = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(refused, "NOOP")
        assert "MISSING-RED-RUN-LOG" in " ".join(
            (getattr(refused, "detail", None) or {}).get("defects", [])
        )
        assert _read()["state"] == "running"

        (d / "tests" / "red-run-log.md").write_text(
            "# Red run log\n\nObserved the new check fail before implementation.\n",
            encoding="utf-8",
        )
        _write_signal(
            runtime,
            LEAF,
            signal="DONE",
            owner_token=token,
            evidence={"report": "report.md", "attempt": "red-log-repair"},
        )
        accepted = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(accepted, "COLLAPSE")
    finally:
        mp.undo()


def test_e2_implementation_done_requires_claim_account_headings(runtime):
    """An implementation L5 report must expose the four-part claim account before DONE."""
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    d = _addressing.node_dir(LEAF, runtime)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text("# report\n\nDelivered the claim.\n", encoding="utf-8")
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        refused = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(refused, "NOOP")
        assert "MISSING-CLAIM-ACCOUNT-SECTIONS" in " ".join(
            (getattr(refused, "detail", None) or {}).get("defects", [])
        )
        assert _read()["state"] == "running"

        (d / "report.md").write_text(
            _claim_report("# report\n\nDelivered the claim.\n"),
            encoding="utf-8",
        )
        _write_signal(
            runtime,
            LEAF,
            signal="DONE",
            owner_token=token,
            evidence={"report": "report.md", "attempt": "claim-account-repair"},
        )
        accepted = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(accepted, "COLLAPSE")
    finally:
        mp.undo()


def test_e2_given_ids_must_be_cited_then_fix_requires_fresh_signal(runtime):
    """An L5 node GIVEN minted IDs (acceptance.md carries R-007) must cite one in report.md.
    Uncited -> refused (MISSING-REQUIREMENT-CITATION). The agent fixes the report, then must
    rewrite the signal before collapse; the old refused signal identity stays refused."""
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2)  # level L5 default
    _seed([binding])
    d = _prepare_node(runtime, report="# report\n\nAll tests pass.\n")
    (d / "acceptance.md").write_text("- R-007: never double-charge\n", encoding="utf-8")
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action, "NOOP")
        assert any("MISSING-REQUIREMENT-CITATION" in (r.get("summary") or "")
                   for r in _wal_rows("return_contract_failed"))
        # The agent fixes the report, but the still-present refused signal identity cannot pass.
        (d / "report.md").write_text(
            _claim_report("# report\n\nDischarged R-007; suite green.\n"),
            encoding="utf-8",
        )
        action2 = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action2, "NOOP")
        detail2 = getattr(action2, "detail", None) or {}
        assert detail2.get("reason") == "return_contract_failed"
        assert detail2.get("signal_artifact_previously_refused") is True
        assert _read()["state"] == "running"

        _write_signal(runtime, LEAF, signal="DONE", owner_token=token,
                      evidence={"report": "report.md", "attempt": "citation-repair"})
        action3 = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action3, "COLLAPSE"), "a fixed report plus fresh signal should collapse"
        assert _read()["state"] == "done"
    finally:
        mp.undo()


def test_e2_trace_contradiction_refused(runtime):
    """A kind:requirement stanza whose dotted id does NOT truncate into its serves list is a
    TRACE-CONTRADICTION; a stanza with a non-canonical field is MALFORMED-TRACE — both refuse."""
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    d = _prepare_node(runtime)
    (d / "plan.md").write_text(
        "# plan\n\n"
        "<!-- trace: { id: R-1.2, serves: [R-9], kind: requirement, level: L4, node: proj/widget } -->\n",
        encoding="utf-8",
    )
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()
    assert _is(action, "NOOP")
    assert any("TRACE-CONTRADICTION" in (r.get("summary") or "")
               for r in _wal_rows("return_contract_failed"))
    assert _read()["state"] == "running"


def test_e2_ignores_hidden_harness_markdown_artifacts(runtime):
    """Harness-owned hidden markdown files such as .identity-prompt.md are not return artifacts.
    Their embedded operational text must not make a valid DONE refuse as MALFORMED-TRACE."""
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    d = _prepare_node(runtime)
    (d / ".identity-prompt.md").write_text(
        "# identity aggregate\n\n"
        "<!-- trace: { id } -->\n",
        encoding="utf-8",
    )
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()
    assert _is(action, "COLLAPSE"), (
        "hidden harness-owned markdown files are not agent return artifacts; got "
        f"{_tag(action)!r} and rows={_wal_rows('return_contract_failed')!r}"
    )
    assert _read()["state"] == "done"


def test_e2_ignores_trace_examples_in_frozen_input_docs(runtime):
    """Parent-authored input docs are not child return artifacts.

    A brief may show the trace schema as `<!-- trace: { id, serves, ... } -->`; that example is
    intentionally not a filled trace stanza and must not trap a correctly finished child.
    """
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    d = _prepare_node(runtime)
    (d / "brief.md").write_text(
        "# brief\n\n"
        "Use `<!-- trace: { id, serves, kind, level, node } -->` for authored elements.\n",
        encoding="utf-8",
    )
    (d / "acceptance.md").write_text(
        "# acceptance\n\n"
        "<!-- trace: { id } -->\n",
        encoding="utf-8",
    )
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()
    assert _is(action, "COLLAPSE"), (
        "frozen brief/acceptance files are input context, not child return artifacts; got "
        f"{_tag(action)!r} and rows={_wal_rows('return_contract_failed')!r}"
    )
    assert _read()["state"] == "done"


def test_e2_ignores_trace_examples_in_process_log(runtime):
    """Append-only process logs may quote defects or schema examples and are not return artifacts."""
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([binding])
    d = _prepare_node(runtime)
    (d / "log.md").write_text(
        "# log\n\n"
        "- DONE refused earlier on `<!-- trace: { id, serves, kind, level, node } -->`; "
        "recording this as process history, not output evidence.\n",
        encoding="utf-8",
    )
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()
    assert _is(action, "COLLAPSE"), (
        "log.md is process bookkeeping, not child return evidence; got "
        f"{_tag(action)!r} and rows={_wal_rows('return_contract_failed')!r}"
    )
    assert _read()["state"] == "done"


def test_e2_failed_signal_is_exempt(runtime):
    """FAILED collapses WITHOUT contract checks — an agent can always fail loud (never trap)."""
    wd = _wd()
    binding, token = _binding(state="running", generation=1, lease_epoch=1)
    _seed([binding])
    _prepare_node(runtime, report=None)  # no report.md — would refuse a DONE
    _write_signal(runtime, LEAF, signal="FAILED", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()
    assert _is(action, "COLLAPSE"), "FAILED must collapse with NO contract gate"
    assert _read()["state"] == "failed"


# ===========================================================================
# GATE LIFECYCLE — producer DONE submits a candidate; the review gate owns forward motion.
# ===========================================================================

def test_gated_l5_exec_done_submits_candidate_to_review_not_parent(runtime):
    """A gated producer's DONE is NOT parent-visible completion. It opens the review gate by
    writing one candidate pointer to the co-located #review inbox and leaves the producer running
    so a future BOUNCE can preserve context. The parent must not receive child_collapsed."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    review_rec, _review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    binding, token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={"gate_required": True},
    )
    _seed([parent_rec, review_rec, binding])
    candidate_dir = _prepare_node(runtime, report="# report\n\nDone per brief; verified R-007.\n")
    (candidate_dir / "brief.md").write_text("Build the widget slice for R-007.\n", encoding="utf-8")
    (candidate_dir / "acceptance.md").write_text(
        "- R-007: widget slice remains idempotent\n"
        "<!-- trace: { id: R-007.1, serves: [R-007], kind: test, level: L4, node: proj/widget } -->\n",
        encoding="utf-8",
    )
    parent_dir = _addressing.node_dir(PARENT, runtime)
    (parent_dir / "decisions").mkdir(parents=True, exist_ok=True)
    (parent_dir / "decisions" / "001-widget-adr.md").write_text(
        "# ADR-001 — Widget idempotency governs proj/widget\n",
        encoding="utf-8",
    )
    (candidate_dir / "interfaces").mkdir(parents=True, exist_ok=True)
    (candidate_dir / "interfaces" / "widget-contract.md").write_text(
        "# Widget Contract\n",
        encoding="utf-8",
    )
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token,
                  evidence={"report": "report.md", "notes": "candidate ready"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action, "NOOP"), f"candidate submission must not collapse; got {_tag(action)!r}"
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_candidate_submitted"
        assert (getattr(action, "detail", None) or {}).get("review") == REVIEW

        after = _read()
        assert after["state"] == "running", "producer context must survive review"
        assert after.get("gate_state") == "candidate_submitted"
        assert after.get("gate_review_address") == REVIEW

        parent_lines = _jsonl(_addressing.inbox_path(PARENT, runtime))
        assert not [l for l in parent_lines if l.get("type") in {"child_collapsed", "gate_passed"}], (
            "the parent must not be woken until the review gate passes"
        )
        review_lines = _jsonl(_addressing.inbox_path(REVIEW, runtime))
        candidates = [l for l in review_lines if l.get("type") == "candidate_submitted"]
        assert len(candidates) == 1
        assert candidates[0].get("candidate") == LEAF
        assert "report.md" in candidates[0].get("message", "")
        packet = Path(candidates[0].get("review_packet"))
        assert packet.is_file()
        packet_text = packet.read_text(encoding="utf-8")
        assert "Review Packet" in packet_text
        assert "## Trace / Coverage Slice Pointers" in packet_text
        assert "Starter IDs found in brief/acceptance, not the authoritative coverage target" in packet_text
        assert "Use the governing ADR/decision and interface contract pointers below" in packet_text
        assert "`R-007`" in packet_text
        assert "## Governing ADR / Decision Pointers" in packet_text
        assert "001-widget-adr.md" in packet_text
        assert "## Interface Contract Pointers" in packet_text
        assert "widget-contract.md" in packet_text
        assert "## Verification Runtime Pointers" in packet_text
        assert after.get("gate_review_packet") == str(packet)
        assert after.get("gate_review_dir") == str(packet.parent)
        assert after.get("gate_candidate_artifact_snapshot_dir") == str(packet.parent / "candidate-snapshot")
        snapshot_report = packet.parent / "candidate-snapshot" / "report.md"
        assert snapshot_report.read_text(encoding="utf-8") == (candidate_dir / "report.md").read_text(
            encoding="utf-8"
        )

        # Same signal artifact, next tick: idempotent, no second inbox line / WAL row.
        action2 = wd.check_leaf(_node_from(after), _read(), now=_now_iso())
        assert _is(action2, "NOOP")
        assert (getattr(action2, "detail", None) or {}).get("reason") == "gate_candidate_already_submitted"
        assert len(_wal_rows("gate_candidate_submitted")) == 1
        assert len([l for l in _jsonl(_addressing.inbox_path(REVIEW, runtime))
                    if l.get("type") == "candidate_submitted"]) == 1
    finally:
        mp.undo()


def test_review_accept_refuses_candidate_artifact_drift_after_submission(runtime):
    """A review verdict cannot pass artifacts that changed after candidate submission."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    review_rec, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    producer, producer_token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={"gate_required": True},
    )
    _seed([parent_rec, review_rec, producer])
    candidate_dir = _prepare_node(runtime, report="# report\n\nCandidate v1.\n")
    (candidate_dir / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_signal(runtime, LEAF, signal="DONE", owner_token=producer_token,
                  evidence={"report": "report.md", "notes": "candidate ready"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        submit = wd.check_leaf(_node_from(producer), ledger.read_binding(LEAF), now=_now_iso())
        assert _is(submit, "NOOP")
        assert (getattr(submit, "detail", None) or {}).get("reason") == "gate_candidate_submitted"

        submitted = ledger.read_binding(LEAF)
        gate_id = submitted.get("gate_id")
        manifest = Path(submitted.get("gate_candidate_artifact_manifest"))
        assert manifest.is_file()
        assert submitted.get("gate_candidate_artifact_manifest_sha256")

        (candidate_dir / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        _write_gate_artifact(
            runtime,
            REVIEW,
            "gate-report.md",
            "# review\n\nVERDICT: ACCEPT. Reviewed candidate v1.\n",
            gate_id=gate_id,
        )
        _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                      evidence={"report": "gate-report.md",
                                "notes": "VERDICT: ACCEPT — suite green"})

        action = wd.check_leaf(_node_from(review_rec), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "candidate_artifact_drift"
        defects = (getattr(action, "detail", None) or {}).get("defects", [])
        assert any("CANDIDATE-ARTIFACT-DRIFT" in str(defect) for defect in defects)

        producer_after = ledger.read_binding(LEAF)
        review_after = ledger.read_binding(REVIEW)
        assert producer_after["state"] == "running"
        assert producer_after.get("gate_state") == "gate_failed"
        assert producer_after.get("gate_failure_class") == "candidate_artifact_drift"
        assert review_after["state"] == "running"
        assert not [l for l in _jsonl(_addressing.inbox_path(PARENT, runtime))
                    if l.get("type") == "gate_passed"]
        failed = [l for l in _jsonl(_addressing.inbox_path(PARENT, runtime))
                  if l.get("type") == "gate_failed"]
        assert len(failed) == 1
        assert failed[0].get("failure_class") == "candidate_artifact_drift"
        defect_rows = _wal_rows("return_contract_failed")
        assert defect_rows
        assert any(
            "CANDIDATE-ARTIFACT-DRIFT" in " ".join(map(str, row.get("binding_delta", {}).get("defects") or []))
            for row in defect_rows
        )
        review_defects = [l for l in _jsonl(_addressing.inbox_path(REVIEW, runtime))
                          if l.get("type") == "return_contract_defect"]
        assert len(review_defects) == 1
        defect_message = review_defects[0].get("message", "")
        assert "candidate identity is invalid" in defect_message
        assert "new candidate_submitted pointer or an explicit retry" in defect_message
        assert "re-write your .signal file" not in defect_message
    finally:
        mp.undo()


def test_review_accept_ignores_process_runtime_and_message_surface_drift_after_submission(runtime):
    """The candidate manifest freezes deliverables, not process/generated bookkeeping.

    LR-150: an L5 toggled the final checkbox in ``plan.md`` after candidate submission. That is
    process bookkeeping, not a changed candidate deliverable, so it should not turn an otherwise
    current review ACCEPT into ``candidate_artifact_drift``.

    The same rule applies to logs, outbox files, generated packaging metadata, the harness-
    provisioned ``.tmp`` runtime scratch tree, and the canonical ``messages/`` communication
    surface. Those are runtime, process, or communication side effects, not the submitted
    candidate identity.
    """
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    review_rec, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    producer, producer_token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={"gate_required": True},
    )
    _seed([parent_rec, review_rec, producer])
    candidate_dir = _prepare_node(runtime, report="# report\n\nCandidate v1.\n")
    (candidate_dir / "plan.md").write_text(
        "# plan\n\n- [x] fill report.md\n- [ ] sign off\n",
        encoding="utf-8",
    )
    (candidate_dir / "log.md").write_text(
        "# log\n\n- started candidate assembly\n",
        encoding="utf-8",
    )
    outbox = candidate_dir / ".harness-outbox"
    outbox.mkdir()
    (outbox / "01-task.json.done").write_text("{}", encoding="utf-8")
    runtime_scratch = (
        candidate_dir
        / sandbox.RUNTIME_SCRATCH_DIRNAME
        / "claude-501"
        / "tasks"
    )
    runtime_scratch.mkdir(parents=True)
    (runtime_scratch / "existing.output").write_text(
        "background task pending\n",
        encoding="utf-8",
    )
    message_dir = _addressing.messages_dir(LEAF, runtime)
    message_dir.mkdir()
    (message_dir / "existing-message.json").write_text(
        '{"type":"message"}\n',
        encoding="utf-8",
    )
    (candidate_dir / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_signal(runtime, LEAF, signal="DONE", owner_token=producer_token,
                  evidence={"report": "report.md", "notes": "candidate ready"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        submit = wd.check_leaf(_node_from(producer), ledger.read_binding(LEAF), now=_now_iso())
        assert _is(submit, "NOOP")
        assert (getattr(submit, "detail", None) or {}).get("reason") == "gate_candidate_submitted"

        submitted = ledger.read_binding(LEAF)
        gate_id = submitted.get("gate_id")
        manifest = Path(submitted.get("gate_candidate_artifact_manifest"))
        artifact_paths = {
            entry["path"]
            for entry in json.loads(manifest.read_text(encoding="utf-8"))["artifacts"]
        }
        assert "plan.md" not in artifact_paths
        assert "log.md" not in artifact_paths
        assert ".harness-outbox/01-task.json.done" not in artifact_paths
        assert not any(
            path.startswith(f"{sandbox.RUNTIME_SCRATCH_DIRNAME}/")
            for path in artifact_paths
        )
        assert not (
            Path(submitted["gate_candidate_artifact_snapshot_dir"])
            / sandbox.RUNTIME_SCRATCH_DIRNAME
        ).exists()
        assert not any(
            path.startswith(f"{message_dir.name}/")
            for path in artifact_paths
        )
        assert not (
            Path(submitted["gate_candidate_artifact_snapshot_dir"])
            / message_dir.name
        ).exists()
        assert "report.md" in artifact_paths
        assert "app.py" in artifact_paths

        (candidate_dir / "plan.md").write_text(
            "# plan\n\n- [x] fill report.md\n- [x] sign off\n",
            encoding="utf-8",
        )
        (candidate_dir / "log.md").write_text(
            "# log\n\n- started candidate assembly\n- submitted candidate\n",
            encoding="utf-8",
        )
        (outbox / "02-review-note.json.done").write_text("{}", encoding="utf-8")
        (runtime_scratch / "existing.output").write_text(
            "background task complete\n",
            encoding="utf-8",
        )
        (runtime_scratch / "post-submission.output").write_text(
            "async task result\n",
            encoding="utf-8",
        )
        (message_dir / "existing-message.json").write_text(
            '{"type":"message","status":"delivered"}\n',
            encoding="utf-8",
        )
        (message_dir / "a4-gate-findings-carry-forward.json").write_text(
            '{"type":"message","summary":"review findings"}\n',
            encoding="utf-8",
        )
        egg_info = candidate_dir / "demo.egg-info"
        egg_info.mkdir()
        (egg_info / "PKG-INFO").write_text("generated metadata\n", encoding="utf-8")
        _write_gate_artifact(
            runtime,
            REVIEW,
            "gate-report.md",
            "# review\n\nVERDICT: ACCEPT. Reviewed current deliverables.\n",
            gate_id=gate_id,
        )
        _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                      evidence={"report": "gate-report.md",
                                "notes": "VERDICT: ACCEPT — suite green"})

        action = wd.check_leaf(_node_from(review_rec), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "COLLAPSE")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_passed"
        producer_after = ledger.read_binding(LEAF)
        assert producer_after["state"] == "done"
        assert producer_after.get("gate_state") == "gate_passed"
        assert not [l for l in _jsonl(_addressing.inbox_path(PARENT, runtime))
                    if l.get("type") == "gate_failed"]
        passed = [l for l in _jsonl(_addressing.inbox_path(PARENT, runtime))
                  if l.get("type") == "gate_passed"]
        assert len(passed) == 1
    finally:
        mp.undo()


def test_rewritten_done_while_candidate_submitted_does_not_create_orphan_packet(runtime):
    """A fresh DONE artifact cannot create a second packet while a gate candidate is pending.

    Live LR-119: a producer rewrote `.signal.exec.json` after the first candidate had already
    committed. The old code created `reviews/<new-gate-id>/review-packet.md` before the
    `candidate_submitted -> candidate_submitted` transition rejected, leaving an orphan packet that
    was not in the binding, WAL, or review inbox.
    """
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    existing_gate_id = "gate-existing"
    node_dir = _prepare_node(runtime, report="# report\n\nCandidate ready; producer signal rewritten.\n")
    existing_gate_dir = node_dir / "reviews" / existing_gate_id
    existing_gate_dir.mkdir(parents=True, exist_ok=True)
    existing_packet = existing_gate_dir / "review-packet.md"
    existing_packet.write_text("# packet\n\nExisting committed candidate.\n", encoding="utf-8")
    producer, producer_token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": REVIEW,
            "gate_id": existing_gate_id,
            "gate_review_dir": str(existing_gate_dir),
            "gate_review_packet": str(existing_packet),
            "gate_candidate_signal_artifact_seen_at": "candidate-signal-before-rewrite",
        },
    )
    review_rec, _review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    _seed([parent_rec, review_rec, producer])
    _write_signal(runtime, LEAF, signal="DONE", owner_token=producer_token,
                  evidence={"report": "report.md", "notes": "rewritten producer signal"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(producer), ledger.read_binding(LEAF), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_candidate_already_submitted"

        after = ledger.read_binding(LEAF)
        assert after.get("gate_state") == "candidate_submitted"
        assert after.get("gate_id") == existing_gate_id
        assert after.get("gate_review_packet") == str(existing_packet)
        gate_dirs = sorted(p.name for p in (node_dir / "reviews").iterdir() if p.is_dir())
        assert gate_dirs == [existing_gate_id]
        assert len(_wal_rows("gate_candidate_submitted")) == 0
        assert not _jsonl(_addressing.inbox_path(REVIEW, runtime))
    finally:
        mp.undo()


def test_return_contract_refused_signal_identity_stays_refused_until_resignal(runtime):
    """A return-contract refusal sticks to the signal artifact identity, not the mutable files.

    Live-run LR-49: the producer repaired trace.md before rewriting .signal.exec.json, so a later
    tick saw clean artifacts and submitted the old refused signal as a gate candidate. The fix is
    that the refused signal identity remains refused until the agent rewrites the signal file.
    """
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    review_rec, _review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    binding, token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={"gate_required": True},
    )
    _seed([parent_rec, review_rec, binding])
    candidate_dir = _prepare_node(runtime, report="# report\n\nDone per brief; verified R-007.\n")
    (candidate_dir / "brief.md").write_text(
        "Build the widget slice for R-007.\n",
        encoding="utf-8",
    )
    (candidate_dir / "design.md").write_text(
        "<!-- trace: { id: TST-PA2-001, serves: [R-007], kind: test, level: L5, node: proj/widget#exec } -->\n",
        encoding="utf-8",
    )
    (candidate_dir / "trace.md").write_text(
        "<!-- trace: { id: TST-PA2-001, serves: [R-007], kind: test, level: L5, node: proj/widget#exec } -->\n",
        encoding="utf-8",
    )
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token,
                  evidence={"report": "report.md", "trace": "trace.md", "attempt": "first"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "return_contract_failed"
        assert len(_wal_rows("return_contract_failed")) == 1
        assert len(_wal_rows("gate_candidate_submitted")) == 0

        # Repair the pointed-to artifact but do NOT rewrite .signal.exec.json yet. The same
        # refused signal identity must not become a candidate merely because mutable files changed.
        (candidate_dir / "trace.md").write_text(
            "<!-- trace: { id: TST-PARSER-PA2-001, serves: [R-007], kind: test, level: L5, node: proj/widget#exec } -->\n",
            encoding="utf-8",
        )
        action2 = wd.check_leaf(_node_from(binding), ledger.read_binding(LEAF), now=_now_iso())
        assert _is(action2, "NOOP")
        detail2 = getattr(action2, "detail", None) or {}
        assert detail2.get("reason") == "return_contract_failed"
        assert detail2.get("signal_artifact_previously_refused") is True
        assert len(_wal_rows("return_contract_failed")) == 1
        assert len(_wal_rows("gate_candidate_submitted")) == 0
        assert not _jsonl(_addressing.inbox_path(REVIEW, runtime))

        _write_signal(runtime, LEAF, signal="DONE", owner_token=token,
                      evidence={"report": "report.md", "trace": "trace.md", "attempt": "repair"})
        action3 = wd.check_leaf(_node_from(binding), ledger.read_binding(LEAF), now=_now_iso())
        assert _is(action3, "NOOP")
        assert (getattr(action3, "detail", None) or {}).get("reason") == "gate_candidate_submitted"
        assert ledger.read_binding(LEAF).get("gate_state") == "candidate_submitted"
        assert len(_wal_rows("gate_candidate_submitted")) == 1
    finally:
        mp.undo()


def test_gated_candidate_missing_review_slot_marks_gate_failed(runtime):
    """A gated producer cannot submit into an absent review seat.

    Missing review substrate is parent-visible ``gate_failed`` immediately, not a quiet
    ``candidate_submitted`` hold with no actor able to review the packet.
    """
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    binding, token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={"gate_required": True, "gate_review_address": REVIEW},
    )
    _seed([parent_rec, binding])
    _prepare_node(runtime)
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token,
                  evidence={"report": "report.md", "notes": "candidate ready"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_failed"
        assert (getattr(action, "detail", None) or {}).get("failure_class") == "review_slot_missing"

        after = _read()
        assert after["state"] == "running"
        assert after.get("gate_state") == "gate_failed"
        assert after.get("gate_failure_reason") == "review_slot_missing"
        assert after.get("gate_failure_class") == "review_slot_missing"
        assert after.get("gate_failure_review") == REVIEW
        assert len(_wal_rows("gate_candidate_submitted")) == 0
        assert len(_wal_rows("gate_failed")) == 1
        assert not _jsonl(_addressing.inbox_path(REVIEW, runtime))

        parent_lines = _jsonl(_addressing.inbox_path(PARENT, runtime))
        failed = [l for l in parent_lines if l.get("type") == "gate_failed"]
        assert len(failed) == 1
        assert failed[0].get("candidate") == LEAF
        assert failed[0].get("review") == REVIEW
        assert failed[0].get("failure_class") == "review_slot_missing"
        assert not [l for l in parent_lines if l.get("type") in {"child_collapsed", "gate_passed"}]

        action2 = wd.check_leaf(_node_from(after), _read(), now=_now_iso())
        assert _is(action2, "NOOP")
        assert (getattr(action2, "detail", None) or {}).get("reason") == "gate_failed_already_committed"
        assert len(_wal_rows("gate_failed")) == 1
        assert len([l for l in _jsonl(_addressing.inbox_path(PARENT, runtime))
                    if l.get("type") == "gate_failed"]) == 1
    finally:
        mp.undo()


def test_gated_candidate_misbound_review_slot_marks_gate_failed(runtime):
    """A review slot for the wrong producer is not a valid owner for this boundary."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    review_rec, _review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        subagent_id="reviewer",
        session_uuid="review-session",
        state="planned",
        extra={"gate_for": "proj/other#exec"},
    )
    binding, token = _binding(
        state="running",
        generation=4,
        lease_epoch=2,
        extra={"gate_required": True, "gate_review_address": REVIEW},
    )
    _seed([parent_rec, review_rec, binding])
    _prepare_node(runtime)
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token,
                  evidence={"report": "report.md", "notes": "candidate ready"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_failed"
        assert (getattr(action, "detail", None) or {}).get("failure_class") == "review_slot_mismatch"

        after = _read()
        assert after.get("gate_state") == "gate_failed"
        assert after.get("gate_failure_reason") == "review_slot_mismatch"
        assert after.get("gate_failure_class") == "review_slot_mismatch"
        assert len(_wal_rows("gate_candidate_submitted")) == 0
        assert not _jsonl(_addressing.inbox_path(REVIEW, runtime))
        failed = [l for l in _jsonl(_addressing.inbox_path(PARENT, runtime))
                  if l.get("type") == "gate_failed"]
        assert len(failed) == 1
        assert failed[0].get("candidate") == LEAF
    finally:
        mp.undo()


def test_l5_review_accept_passes_gate_and_wakes_parent(runtime):
    """A review-seat ACCEPT is the first parent-visible completion. It finalizes both the held
    producer and the reviewer, writes one gate_passed pointer to the parent inbox, and does not use
    the old producer child_collapsed path."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    producer, _producer_token = _binding(
        state="running",
        generation=5,
        lease_epoch=2,
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": REVIEW,
        },
    )
    review, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime)
    _write_gate_artifact(
        runtime,
        REVIEW,
        "gate-report.md",
        "# review\n\nVERDICT: ACCEPT. Verified the candidate against the frozen criteria.\n",
    )
    _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md", "notes": "VERDICT: ACCEPT — suite green"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "COLLAPSE"), f"review ACCEPT should complete the gate; got {_tag(action)!r}"
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_passed"

        producer_after = ledger.read_binding(LEAF)
        review_after = ledger.read_binding(REVIEW)
        assert producer_after["state"] == "done"
        assert producer_after.get("gate_state") == "gate_passed"
        assert review_after["state"] == "done"
        assert review_after.get("gate_verdict") == "ACCEPT"

        lines = _jsonl(_addressing.inbox_path(PARENT, runtime))
        passed = [l for l in lines if l.get("type") == "gate_passed"]
        assert len(passed) == 1
        assert passed[0].get("candidate") == LEAF
        assert passed[0].get("review") == REVIEW
        inbox = _addressing.inbox_path(PARENT, runtime)
        inbox.unlink()
        from harnessd import daemon

        daemon._recover_gate_notifications_best_effort()
        assert len([l for l in _jsonl(inbox) if l.get("type") == "gate_passed"]) == 1
        daemon._recover_gate_notifications_best_effort()
        assert len([l for l in _jsonl(inbox) if l.get("type") == "gate_passed"]) == 1
        assert not [l for l in lines if l.get("type") == "child_collapsed"], (
            "gate-cleared completion should not be delivered as a generic child collapse"
        )
        assert len(_wal_rows("gate_passed")) == 1
        assert len(_wal_rows("gate_review_passed")) == 1
    finally:
        mp.undo()


def test_gate_pass_parent_wake_dedup_uses_gate_id(runtime):
    """A stale gate_passed pointer for the same candidate/review pair must not suppress the
    parent wake for a later gate incarnation. The committed gate_id is the candidate identity."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    producer, _producer_token = _binding(
        state="running",
        generation=5,
        lease_epoch=2,
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": REVIEW,
            "gate_id": "gate-new",
        },
    )
    review, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime)
    inbox = _addressing.inbox_path(PARENT, runtime)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        json.dumps({
            "from": "harnessd",
            "type": "gate_passed",
            "candidate": LEAF,
            "review": REVIEW,
            "gate_id": "gate-old",
            "ts": "2026-06-16T00:00:00Z",
        }) + "\n",
        encoding="utf-8",
    )
    _write_gate_artifact(
        runtime,
        REVIEW,
        "gate-report.md",
        "# review\n\nVERDICT: ACCEPT. Verified the candidate against the frozen criteria.\n",
        gate_id="gate-new",
    )
    _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md", "notes": "VERDICT: ACCEPT — suite green"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "COLLAPSE"), f"review ACCEPT should complete the gate; got {_tag(action)!r}"

        passed = [l for l in _jsonl(inbox) if l.get("type") == "gate_passed"]
        assert [l.get("gate_id") for l in passed] == ["gate-old", "gate-new"]
    finally:
        mp.undo()


def test_l5_review_accept_without_report_verdict_is_refused(runtime):
    """The terminal signal may restate a verdict, but the report is the gate artifact. A review
    signal saying ACCEPT cannot pass if report.md does not itself carry VERDICT: ACCEPT."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    producer, _producer_token = _binding(
        state="running",
        generation=5,
        lease_epoch=2,
        extra={"gate_required": True, "gate_state": "candidate_submitted",
               "gate_review_address": REVIEW},
    )
    review, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime)
    _write_gate_artifact(
        runtime,
        REVIEW,
        "gate-report.md",
        "# review\n\nI ran the suite and found the candidate acceptable.\n",
    )
    _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md", "notes": "VERDICT: ACCEPT — suite green"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "return_contract_failed"
        assert any("MISSING-GATE-VERDICT" in str(d)
                   for d in (getattr(action, "detail", None) or {}).get("defects", []))
        assert ledger.read_binding(LEAF)["state"] == "running"
        assert ledger.read_binding(REVIEW)["state"] == "running"
        assert not _jsonl(_addressing.inbox_path(PARENT, runtime))
    finally:
        mp.undo()


def test_l5_review_accept_report_signal_mismatch_is_refused(runtime):
    """The report's gate artifact is authoritative. A signal restating ACCEPT cannot override a
    report whose verdict table says BOUNCE."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    producer, _producer_token = _binding(
        state="running",
        generation=5,
        lease_epoch=2,
        extra={"gate_required": True, "gate_state": "candidate_submitted",
               "gate_review_address": REVIEW},
    )
    review, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime)
    _write_gate_artifact(
        runtime,
        REVIEW,
        "gate-report.md",
        "# review\n\nVERDICT: BOUNCE — 1 defect, see below.\n",
    )
    _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md", "notes": "VERDICT: ACCEPT — suite green"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_verdict_mismatch"
        assert ledger.read_binding(LEAF)["state"] == "running"
        assert ledger.read_binding(REVIEW)["state"] == "running"
        assert not _jsonl(_addressing.inbox_path(PARENT, runtime))
    finally:
        mp.undo()


def test_l5_review_accept_without_required_citation_is_refused(runtime):
    """A review PASS still owes the L5+ citation floor. If the shared acceptance packet gives
    requirement IDs, report.md must cite at least one before PASS can wake the parent."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    producer, _producer_token = _binding(
        state="running",
        generation=5,
        lease_epoch=2,
        extra={"gate_required": True, "gate_state": "candidate_submitted",
               "gate_review_address": REVIEW},
    )
    review, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime)
    review_dir = _node_dir(runtime, REVIEW)
    (review_dir / "acceptance.md").write_text("- R-007: never double-charge\n", encoding="utf-8")
    _write_gate_artifact(
        runtime,
        REVIEW,
        "gate-report.md",
        "# review\n\nVERDICT: ACCEPT. Suite green; implementation matches the brief.\n",
    )
    _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md", "notes": "VERDICT: ACCEPT — suite green"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "return_contract_failed"
        assert any("MISSING-REQUIREMENT-CITATION" in str(d)
                   for d in (getattr(action, "detail", None) or {}).get("defects", []))
        assert ledger.read_binding(LEAF)["state"] == "running"
        assert not _jsonl(_addressing.inbox_path(PARENT, runtime))
    finally:
        mp.undo()


def test_l5_review_bounce_wakes_producer_not_parent(runtime):
    """A review-seat BOUNCE returns typed defects to the producer and does not wake the parent.
    The producer keeps context to fix the defects; the reviewer closes so the next candidate gets
    a fresh review incarnation."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    producer, _producer_token = _binding(
        state="running",
        generation=5,
        lease_epoch=2,
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": REVIEW,
        },
    )
    review, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime)
    _write_gate_artifact(
        runtime,
        REVIEW,
        "gate-report.md",
        "# review\n\nVERDICT: BOUNCE — 2 defects. R-007 missing; retry path untested.\n",
    )
    _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md",
                            "notes": "VERDICT: BOUNCE — 2 defects, see report.md"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "NOOP"), f"review BOUNCE must not collapse; got {_tag(action)!r}"
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_bounced"

        producer_after = ledger.read_binding(LEAF)
        review_after = ledger.read_binding(REVIEW)
        assert producer_after["state"] == "running"
        assert producer_after.get("gate_state") == "gate_bounced"
        assert producer_after.get("gate_bounce_count") == 1
        assert producer_after.get("gate_bounce_audit_signal") == "gate_bounce"
        assert producer_after.get("gate_bounce_audit_label", "").startswith("LOOK HERE")
        assert review_after["state"] == "done"
        assert review_after.get("gate_verdict") == "BOUNCE"

        parent_lines = _jsonl(_addressing.inbox_path(PARENT, runtime))
        assert not [l for l in parent_lines if l.get("type") in {"child_collapsed", "gate_passed"}], (
            "a bounce must not be parent-visible completion"
        )
        producer_lines = _jsonl(_addressing.inbox_path(LEAF, runtime))
        bounced = [l for l in producer_lines if l.get("type") == "gate_bounced"]
        assert len(bounced) == 1
        assert bounced[0].get("review") == REVIEW
        assert "gate-report.md" in bounced[0].get("message", "")
        producer_inbox = _addressing.inbox_path(LEAF, runtime)
        producer_inbox.unlink()
        from harnessd import daemon

        daemon._recover_gate_notifications_best_effort()
        assert len([l for l in _jsonl(producer_inbox) if l.get("type") == "gate_bounced"]) == 1
        daemon._recover_gate_notifications_best_effort()
        assert len([l for l in _jsonl(producer_inbox) if l.get("type") == "gate_bounced"]) == 1

        bounce_rows = _wal_rows("gate_bounced")
        assert len(bounce_rows) == 1
        assert "LOOK CLOSER" in bounce_rows[0].get("summary", "")
        assert (
            bounce_rows[0].get("binding_delta", {}).get("gate_bounce_audit_signal")
            == "gate_bounce"
        )
        assert len([l for l in _jsonl(_addressing.inbox_path(LEAF, runtime))
                    if l.get("type") == "gate_bounced"]) == 1

        from harnessd.spawn import chokepoint

        old_review_token = review_after.get("owner_token")
        resubmitted = chokepoint.submit_gate_candidate(
            LEAF,
            expected_owner_token=producer_after.get("owner_token"),
            signal_artifact_seen_at="candidate-v2",
        )
        assert getattr(resubmitted, "ok", False) is True
        producer_resubmitted = ledger.read_binding(LEAF)
        review_resubmitted = ledger.read_binding(REVIEW)
        assert producer_resubmitted.get("gate_state") == "candidate_submitted"
        assert review_resubmitted["state"] == "planned"
        assert review_resubmitted.get("owner_token") != old_review_token
        assert review_resubmitted.get("lease_epoch") > review_after.get("lease_epoch")
        assert not _addressing.signal_path(REVIEW, runtime).exists()
    finally:
        mp.undo()


def test_l5_review_bounce_cap_exhaustion_escalates_to_parent(runtime):
    """When the producer has already reached its configured bounce cap, the next BOUNCE is no
    longer a local loop. It escalates to the parent and does not wake the producer again."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    producer, _producer_token = _binding(
        state="running",
        generation=5,
        lease_epoch=2,
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": REVIEW,
            "gate_bounce_count": 1,
            "gate_bounce_cap": 1,
        },
    )
    review, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime)
    _write_gate_artifact(
        runtime,
        REVIEW,
        "gate-report.md",
        "# review\n\nVERDICT: BOUNCE — still failing after prior bounce.\n",
    )
    _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md",
                            "notes": "VERDICT: BOUNCE — still failing, see report.md"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_escalated"

        producer_after = ledger.read_binding(LEAF)
        review_after = ledger.read_binding(REVIEW)
        assert producer_after["state"] == "running"
        assert producer_after.get("gate_state") == "gate_escalated"
        assert producer_after.get("gate_bounce_count") == 1
        assert review_after["state"] == "done"
        assert review_after.get("gate_verdict") == "BOUNCE"

        parent_lines = _jsonl(_addressing.inbox_path(PARENT, runtime))
        escalations = [l for l in parent_lines if l.get("type") == "gate_escalated"]
        assert len(escalations) == 1
        assert escalations[0].get("candidate") == LEAF
        assert escalations[0].get("review") == REVIEW
        parent_inbox = _addressing.inbox_path(PARENT, runtime)
        parent_inbox.unlink()
        from harnessd import daemon

        daemon._recover_gate_notifications_best_effort()
        assert len([l for l in _jsonl(parent_inbox) if l.get("type") == "gate_escalated"]) == 1
        daemon._recover_gate_notifications_best_effort()
        assert len([l for l in _jsonl(parent_inbox) if l.get("type") == "gate_escalated"]) == 1
        assert not [l for l in _jsonl(_addressing.inbox_path(LEAF, runtime))
                    if l.get("type") == "gate_bounced"], (
            "cap exhaustion should not send another local bounce"
        )
        assert len(_wal_rows("gate_escalated")) == 1
        assert len(_wal_rows("gate_bounced")) == 0

        assert len([l for l in _jsonl(_addressing.inbox_path(PARENT, runtime))
                    if l.get("type") == "gate_escalated"]) == 1

        _write_gate_artifact(
            runtime,
            REVIEW,
            "gate-report.md",
            "# review\n\nVERDICT: BOUNCE — repeated after cap escalation.\n",
        )
        _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                      evidence={"report": "report.md",
                                "notes": "VERDICT: BOUNCE — repeated after cap escalation"})
        assert len(_wal_rows("gate_escalated")) == 1
        assert len([l for l in _jsonl(_addressing.inbox_path(PARENT, runtime))
                    if l.get("type") == "gate_escalated"]) == 1
    finally:
        mp.undo()


def test_parent_reads_only_gate_cleared_work_across_submit_and_pass(runtime):
    """Across the happy L5/L5+ lifecycle, the parent sees no producer-completion nudge until
    the review gate passes. The only parent-visible completion line is gate_passed."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    review_rec, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    producer, producer_token = _binding(
        state="running",
        generation=5,
        lease_epoch=2,
        extra={"gate_required": True, "gate_review_address": REVIEW},
    )
    _seed([parent_rec, review_rec, producer])
    _prepare_node(runtime)
    _write_signal(runtime, LEAF, signal="DONE", owner_token=producer_token,
                  evidence={"report": "report.md", "notes": "candidate ready"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        submit_action = wd.check_leaf(_node_from(producer), ledger.read_binding(LEAF), now=_now_iso())
        assert _is(submit_action, "NOOP")
        assert (getattr(submit_action, "detail", None) or {}).get("reason") == "gate_candidate_submitted"
        assert not _jsonl(_addressing.inbox_path(PARENT, runtime)), (
            "candidate submission must not be parent-visible completion"
        )

        gate_id = ledger.read_binding(LEAF).get("gate_id")
        _write_gate_artifact(
            runtime,
            REVIEW,
            "gate-report.md",
            "# review\n\nVERDICT: ACCEPT. Verified the candidate against the frozen criteria.\n",
            gate_id=gate_id,
        )
        _write_signal(runtime, REVIEW, signal="DONE", owner_token=review_token,
                      evidence={"report": "report.md", "notes": "VERDICT: ACCEPT — suite green"})

        pass_action = wd.check_leaf(_node_from(review_rec), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(pass_action, "COLLAPSE")
        assert (getattr(pass_action, "detail", None) or {}).get("reason") == "gate_passed"

        parent_lines = _jsonl(_addressing.inbox_path(PARENT, runtime))
        assert [l.get("type") for l in parent_lines] == ["gate_passed"]
        assert parent_lines[0].get("candidate") == LEAF
        assert parent_lines[0].get("review") == REVIEW
    finally:
        mp.undo()


def test_review_verdict_must_match_current_gate_candidate_identity(runtime):
    """A review verdict is only valid for the candidate packet it names.

    The producer address is stable across iterations, so a stale review signal
    from an older packet must not ACCEPT/BOUNCE/ESCALATE the producer's newer
    candidate.
    """
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    producer, _producer_token = _binding(
        state="running",
        generation=5,
        lease_epoch=2,
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": REVIEW,
            "gate_id": "gate-current",
            "gate_review_packet": "/runtime/nodes/proj/widget/reviews/gate-current/review-packet.md",
            "gate_candidate_signal_artifact_seen_at": "candidate-current",
        },
    )
    review, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime)
    _write_gate_artifact(
        runtime,
        REVIEW,
        "gate-report.md",
        "# review\n\nVERDICT: ACCEPT. This was written against the old packet.\n",
        gate_id="gate-current",
    )
    _write_signal(
        runtime,
        REVIEW,
        signal="DONE",
        owner_token=review_token,
        evidence={
            "report": "report.md",
            "notes": "VERDICT: ACCEPT — old packet looked good",
            "gate_id": "gate-old",
            "review_packet": "/runtime/nodes/proj/widget/reviews/gate-old/review-packet.md",
        },
    )

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_candidate_identity_mismatch"
        defects = (getattr(action, "detail", None) or {}).get("defects", [])
        assert any("GATE-CANDIDATE-IDENTITY-MISMATCH" in str(d) for d in defects)

        assert ledger.read_binding(LEAF)["state"] == "running"
        assert ledger.read_binding(LEAF).get("gate_state") == "candidate_submitted"
        assert ledger.read_binding(REVIEW)["state"] == "running"
        assert not _jsonl(_addressing.inbox_path(PARENT, runtime))
        assert len(_wal_rows("gate_passed")) == 0
        rows = _wal_rows("return_contract_failed")
        assert len(rows) == 1
        assert "GATE-CANDIDATE-IDENTITY-MISMATCH" in (rows[0].get("summary") or "")
    finally:
        mp.undo()


def test_review_verdict_accepts_current_gate_candidate_identity(runtime):
    """The production identity-positive path accepts a verdict naming the current gate packet."""
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    producer, _producer_token = _binding(
        state="running",
        generation=5,
        lease_epoch=2,
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": REVIEW,
            "gate_id": "gate-current",
            "gate_review_packet": "/runtime/nodes/proj/widget/reviews/gate-current/review-packet.md",
            "gate_candidate_signal_artifact_seen_at": "candidate-current",
        },
    )
    review, review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime)
    _write_gate_artifact(
        runtime,
        REVIEW,
        "gate-report.md",
        "# review\n\nVERDICT: ACCEPT. This was written against the current packet.\n",
        gate_id="gate-current",
    )
    _write_signal(
        runtime,
        REVIEW,
        signal="DONE",
        owner_token=review_token,
        evidence={
            "report": "report.md",
            "notes": "VERDICT: ACCEPT — current packet is good",
            "gate_id": "gate-current",
        },
    )

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(REVIEW), now=_now_iso())
        assert _is(action, "COLLAPSE")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_passed"
        assert ledger.read_binding(LEAF)["state"] == "done"
        assert len(_wal_rows("return_contract_failed")) == 0
        assert len(_wal_rows("gate_passed")) == 1
    finally:
        mp.undo()


def test_gated_l4_exec_done_submits_candidate_to_review_not_l3_parent(runtime):
    """The gate-owned-forwarding lifecycle is not L5-specific. An L4 producer's DONE opens
    its L4#review gate and stays invisible to L3 until that gate ACCEPTS."""
    wd = _wd()
    parent = "proj/area#exec"
    producer_addr = "proj/area/workstream#exec"
    review_addr = "proj/area/workstream#review"
    parent_rec, _parent_token = _binding(
        node_address=parent,
        parent_address="proj#exec",
        level="L3",
        subagent_id="l3",
        session_uuid="l3-session",
        state="running",
        generation=1,
        lease_epoch=1,
    )
    review_rec, _review_token = _binding(
        node_address=review_addr,
        parent_address=parent,
        level="L4",
        subagent_id="l4-review",
        session_uuid="l4-review-session",
        extra={"gate_for": producer_addr},
    )
    producer, producer_token = _binding(
        node_address=producer_addr,
        parent_address=parent,
        level="L4",
        state="running",
        generation=2,
        lease_epoch=2,
        subagent_id="l4-exec",
        session_uuid="l4-exec-session",
        extra={"gate_required": True},
    )
    _seed([parent_rec, review_rec, producer])
    _prepare_node(runtime, producer_addr, report="# report\n\nWorkstream candidate ready.\n")
    _write_signal(runtime, producer_addr, signal="DONE", owner_token=producer_token,
                  evidence={"report": "report.md", "notes": "candidate ready"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(producer), ledger.read_binding(producer_addr), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_candidate_submitted"

        after = ledger.read_binding(producer_addr)
        assert after["state"] == "running"
        assert after.get("gate_state") == "candidate_submitted"
        assert after.get("gate_review_address") == review_addr

        assert not _jsonl(_addressing.inbox_path(parent, runtime))
        review_lines = _jsonl(_addressing.inbox_path(review_addr, runtime))
        candidates = [l for l in review_lines if l.get("type") == "candidate_submitted"]
        assert len(candidates) == 1
        assert candidates[0].get("candidate") == producer_addr
    finally:
        mp.undo()


@pytest.mark.parametrize(
    ("level", "producer_addr", "review_addr", "parent", "artifact"),
    [
        ("L4", "proj/area/workstream#exec", "proj/area/workstream#review",
         "proj/area#exec", "gate-composition-report.md"),
        ("L3", "proj/area#exec", "proj/area#review",
         "proj#exec", "gate-area-composition-review.md"),
        ("L2", "proj#exec", "proj#review",
         "client#exec", "gate-composition-review.md"),
    ],
)
def test_higher_review_accept_requires_level_specific_gate_artifact(
    runtime,
    level,
    producer_addr,
    review_addr,
    parent,
    artifact,
):
    """L4/L3/L2 gate review DONE must be grounded in the level's portfolio gate artifact,
    not just a generic report.md plus a terminal-signal note."""
    wd = _wd()
    parent_rec, _parent_token = _binding(
        node_address=parent,
        parent_address=None,
        level={"L4": "L3", "L3": "L2", "L2": "L1"}[level],
        subagent_id=f"{level.lower()}-parent",
        session_uuid=f"{level.lower()}-parent-session",
        state="running",
        generation=1,
        lease_epoch=1,
    )
    producer, _producer_token = _binding(
        node_address=producer_addr,
        parent_address=parent,
        level=level,
        state="running",
        generation=4,
        lease_epoch=2,
        subagent_id=f"{level.lower()}-exec",
        session_uuid=f"{level.lower()}-exec-session",
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": review_addr,
        },
    )
    review, review_token = _binding(
        node_address=review_addr,
        parent_address=parent,
        level=level,
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id=f"{level.lower()}-review",
        session_uuid=f"{level.lower()}-review-session",
        extra={"gate_for": producer_addr},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime, producer_addr, report="# report\n\nCandidate ready.\n")
    review_dir = _node_dir(runtime, review_addr)
    (review_dir / "report.md").write_text(
        "# report\n\nReview summary: acceptable, see portfolio gate artifact.\n",
        encoding="utf-8",
    )
    assert not (review_dir / artifact).exists()
    _write_signal(runtime, review_addr, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md", "notes": "VERDICT: ACCEPT — portfolio gate clear"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(review_addr), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "return_contract_failed"
        defects = (getattr(action, "detail", None) or {}).get("defects", [])
        assert any("MISSING-GATE-ARTIFACT" in str(d) and artifact in str(d) for d in defects)
        assert ledger.read_binding(producer_addr)["state"] == "running"
        assert ledger.read_binding(review_addr)["state"] == "running"
        assert not _jsonl(_addressing.inbox_path(parent, runtime))
    finally:
        mp.undo()


def test_l4_review_signal_cannot_override_composition_report_verdict(runtime):
    """At L4, composition-report.md is the authoritative gate artifact. A terminal signal
    restating ACCEPT cannot pass a composition report whose verdict is BOUNCE."""
    wd = _wd()
    parent = "proj/area#exec"
    producer_addr = "proj/area/workstream#exec"
    review_addr = "proj/area/workstream#review"
    parent_rec, _parent_token = _binding(
        node_address=parent,
        parent_address="proj#exec",
        level="L3",
        subagent_id="l3",
        session_uuid="l3-session",
        state="running",
        generation=1,
        lease_epoch=1,
    )
    producer, _producer_token = _binding(
        node_address=producer_addr,
        parent_address=parent,
        level="L4",
        state="running",
        generation=4,
        lease_epoch=2,
        subagent_id="l4-exec",
        session_uuid="l4-exec-session",
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": review_addr,
        },
    )
    review, review_token = _binding(
        node_address=review_addr,
        parent_address=parent,
        level="L4",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="l4-review",
        session_uuid="l4-review-session",
        extra={"gate_for": producer_addr},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime, producer_addr, report="# report\n\nCandidate ready.\n")
    review_dir = _node_dir(runtime, review_addr)
    (review_dir / "report.md").write_text(
        "# report\n\nReview summary; routing verdict is in composition-report.md.\n",
        encoding="utf-8",
    )
    gate_dir = _write_short_review_plan(runtime, review_addr)
    (gate_dir / "gate-composition-report.md").write_text(
        "# composition report\n\nVERDICT: BOUNCE — task interfaces do not compose.\n",
        encoding="utf-8",
    )
    _write_signal(runtime, review_addr, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md", "notes": "VERDICT: ACCEPT — ready"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(review_addr), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_verdict_mismatch"
        assert (getattr(action, "detail", None) or {}).get("report_verdict") == "BOUNCE"
        assert ledger.read_binding(producer_addr)["state"] == "running"
        assert ledger.read_binding(review_addr)["state"] == "running"
        rows = _wal_rows("return_contract_failed")
        assert len(rows) == 1
        assert "GATE-VERDICT-MISMATCH" in (rows[0].get("summary") or "")
        review_lines = _jsonl(_addressing.inbox_path(review_addr, runtime))
        defects = [l for l in review_lines if l.get("type") == "return_contract_defect"]
        assert len(defects) == 1
        assert "GATE-VERDICT-MISMATCH" in defects[0].get("message", "")
        assert not _jsonl(_addressing.inbox_path(parent, runtime))

        action2 = wd.check_leaf(_node_from(review), ledger.read_binding(review_addr), now=_now_iso())
        assert _is(action2, "NOOP")
        assert (getattr(action2, "detail", None) or {}).get("reason") == "gate_verdict_mismatch"
        assert len(_wal_rows("return_contract_failed")) == 1
        assert len([l for l in _jsonl(_addressing.inbox_path(review_addr, runtime))
                    if l.get("type") == "return_contract_defect"]) == 1
    finally:
        mp.undo()


def test_l4_review_accept_uses_composition_report_as_parent_visible_artifact(runtime):
    """An L4 review ACCEPT with composition-report.md passes the gate and wakes L3 with a
    pointer to the composition artifact, not just the generic review report."""
    wd = _wd()
    parent = "proj/area#exec"
    producer_addr = "proj/area/workstream#exec"
    review_addr = "proj/area/workstream#review"
    parent_rec, _parent_token = _binding(
        node_address=parent,
        parent_address="proj#exec",
        level="L3",
        subagent_id="l3",
        session_uuid="l3-session",
        state="running",
        generation=1,
        lease_epoch=1,
    )
    producer, _producer_token = _binding(
        node_address=producer_addr,
        parent_address=parent,
        level="L4",
        state="running",
        generation=4,
        lease_epoch=2,
        subagent_id="l4-exec",
        session_uuid="l4-exec-session",
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": review_addr,
        },
    )
    review, review_token = _binding(
        node_address=review_addr,
        parent_address=parent,
        level="L4",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="l4-review",
        session_uuid="l4-review-session",
        extra={"gate_for": producer_addr},
    )
    _seed([parent_rec, producer, review, _gate_passed_lower_child(producer_addr, "L4")])
    _prepare_node(runtime, producer_addr, report="# report\n\nCandidate ready.\n")
    review_dir = _node_dir(runtime, review_addr)
    (review_dir / "report.md").write_text(
        "# report\n\nReview summary; routing verdict is in composition-report.md.\n",
        encoding="utf-8",
    )
    gate_dir = _write_full_l4_review_reports(runtime, review_addr)
    _seed_done_review_check_bindings(review_addr, producer_addr, gate_dir)
    (gate_dir / "gate-composition-report.md").write_text(
        "# composition report\n\nVERDICT: ACCEPT — workstream interfaces compose.\n",
        encoding="utf-8",
    )
    _write_signal(runtime, review_addr, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md", "notes": "VERDICT: ACCEPT — ready"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(review_addr), now=_now_iso())
        assert _is(action, "COLLAPSE")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_passed"

        assert ledger.read_binding(producer_addr)["state"] == "done"
        assert ledger.read_binding(review_addr)["state"] == "done"
        parent_lines = _jsonl(_addressing.inbox_path(parent, runtime))
        passed = [l for l in parent_lines if l.get("type") == "gate_passed"]
        assert len(passed) == 1
        message = passed[0].get("message", "")
        assert str(gate_dir / "gate-composition-report.md") in message
        assert passed[0].get("gate_artifact") == str(gate_dir / "gate-composition-report.md")
    finally:
        mp.undo()


@pytest.mark.parametrize(
    ("producer_level", "review_level", "producer_addr", "review_addr", "parent", "producer_artifact", "gate_artifact"),
    [
        ("L5", "L5+", "proj/area/workstream/task#exec", "proj/area/workstream/task#review",
         "proj/area/workstream#exec", "report.md", "gate-report.md"),
        ("L4", "L4+", "proj/area/workstream#exec", "proj/area/workstream#review",
         "proj/area#exec", "composition-report.md", "gate-composition-report.md"),
        ("L3", "L3+", "proj/area#exec", "proj/area#review",
         "proj#exec", "area-composition-review.md", "gate-area-composition-review.md"),
        ("L2", "L2+", "proj#exec", "proj#review",
         "client#exec", "composition-review.md", "gate-composition-review.md"),
    ],
)
def test_review_gate_pass_preserves_producer_root_artifact_and_points_to_gate_artifact(
    runtime,
    producer_level,
    review_level,
    producer_addr,
    review_addr,
    parent,
    producer_artifact,
    gate_artifact,
):
    """A review PASS must not overwrite the producer's shared node-root evidence artifact."""
    wd = _wd()
    gate_id = "gate-preserve"
    parent_rec, _parent_token = _binding(
        node_address=parent,
        parent_address=None,
        level={"L5": "L4", "L4": "L3", "L3": "L2", "L2": "L1"}[producer_level],
        subagent_id=f"{producer_level.lower()}-parent",
        session_uuid=f"{producer_level.lower()}-parent-session",
        state="running",
        generation=1,
        lease_epoch=1,
    )
    producer, _producer_token = _binding(
        node_address=producer_addr,
        parent_address=parent,
        level=producer_level,
        state="running",
        generation=4,
        lease_epoch=2,
        subagent_id=f"{producer_level.lower()}-exec",
        session_uuid=f"{producer_level.lower()}-exec-session",
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": review_addr,
            "gate_id": gate_id,
        },
    )
    review, review_token = _binding(
        node_address=review_addr,
        parent_address=parent,
        level=review_level,
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id=f"{review_level.lower()}-review",
        session_uuid=f"{review_level.lower()}-review-session",
        extra={"gate_for": producer_addr},
    )
    lower_child = _gate_passed_lower_child(producer_addr, producer_level)
    bindings = [parent_rec, producer, review]
    if lower_child is not None:
        bindings.append(lower_child)
    _seed(bindings)

    node_dir = _node_dir(runtime, producer_addr)
    producer_path = node_dir / producer_artifact
    producer_text = f"# producer artifact\n\nCandidate evidence owned by {producer_addr}.\n"
    producer_path.write_text(producer_text, encoding="utf-8")
    gate_dir = node_dir / "reviews" / gate_id
    gate_dir.mkdir(parents=True, exist_ok=True)
    packet = gate_dir / "review-packet.md"
    packet.write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    manifest = gate_dir / "candidate-artifacts.json"
    producer_raw = producer_path.read_bytes()
    manifest.write_text(
        json.dumps({
            "schema_version": 1,
            "candidate": producer_addr,
            "gate_id": gate_id,
            "root": str(node_dir),
            "artifacts": [{
                "path": producer_artifact,
                "bytes": len(producer_raw),
                "sha256": hashlib.sha256(producer_raw).hexdigest(),
            }],
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if review_level != "L5+":
        _write_short_review_plan(runtime, review_addr, gate_id=gate_id)
    gate_path = gate_dir / gate_artifact
    gate_path.write_text(
        f"# gate artifact\n\nVERDICT: ACCEPT — reviewed {producer_artifact} without overwriting it.\n",
        encoding="utf-8",
    )
    live = ledger.read_binding(producer_addr)
    live["gate_review_dir"] = str(gate_dir)
    live["gate_review_packet"] = str(packet)
    live["gate_candidate_artifact_manifest"] = str(manifest)
    live["gate_candidate_artifact_manifest_sha256"] = manifest_sha
    live["gate_candidate_artifact_snapshot_dir"] = str(gate_dir / "candidate-snapshot")
    live["gate_candidate_signal_artifact_seen_at"] = "producer-signal-1"
    ledger.write_binding({
        **ledger.all_nodes(),
        producer_addr: live,
    }, _lock_held=True)

    _write_signal(
        runtime,
        review_addr,
        signal="DONE",
        owner_token=review_token,
        evidence={
            "verdict": "ACCEPT",
            "producer_artifact": str(producer_path),
            "gate_artifact": str(gate_path),
        },
    )

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(review_addr), now=_now_iso())
        assert _is(action, "COLLAPSE")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_passed"

        assert producer_path.read_text(encoding="utf-8") == producer_text
        assert gate_path.read_text(encoding="utf-8").startswith("# gate artifact")
        assert ledger.read_binding(producer_addr)["state"] == "done"
        assert ledger.read_binding(review_addr)["state"] == "done"

        parent_lines = _jsonl(_addressing.inbox_path(parent, runtime))
        passed = [l for l in parent_lines if l.get("type") == "gate_passed"]
        assert len(passed) == 1
        assert passed[0].get("gate_artifact") == str(gate_path)
        assert passed[0].get("gate_artifact_sha256")
        assert passed[0].get("review_packet") == str(packet)
        assert passed[0].get("candidate_artifact_manifest") == str(manifest)
        assert passed[0].get("candidate_artifact_manifest_sha256") == manifest_sha
        assert passed[0].get("candidate_artifact_snapshot_dir") == str(gate_dir / "candidate-snapshot")
        assert passed[0].get("signal_artifact_seen_at") == "producer-signal-1"
        assert str(producer_path) in (passed[0].get("producer_artifacts") or [])
        assert str(gate_path) in (passed[0].get("message") or "")
        assert str(producer_path) in (passed[0].get("message") or "")
    finally:
        mp.undo()


def test_l4_review_full_mode_requires_check_reports(runtime):
    """FULL review mode means decomposed reports are part of the sign-off contract. A review lead
    cannot write only the synthesis artifact and skip the declared check reports."""
    wd = _wd()
    parent = "proj/area#exec"
    producer_addr = "proj/area/workstream#exec"
    review_addr = "proj/area/workstream#review"
    parent_rec, _parent_token = _binding(
        node_address=parent,
        parent_address="proj#exec",
        level="L3",
        subagent_id="l3",
        session_uuid="l3-session",
        state="running",
        generation=1,
        lease_epoch=1,
    )
    producer, _producer_token = _binding(
        node_address=producer_addr,
        parent_address=parent,
        level="L4",
        state="running",
        generation=4,
        lease_epoch=2,
        subagent_id="l4-exec",
        session_uuid="l4-exec-session",
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": review_addr,
        },
    )
    review, review_token = _binding(
        node_address=review_addr,
        parent_address=parent,
        level="L4",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="l4-review",
        session_uuid="l4-review-session",
        extra={"gate_for": producer_addr},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime, producer_addr, report="# report\n\nCandidate ready.\n")
    review_dir = _node_dir(runtime, review_addr)
    gate_dir = review_dir / "reviews" / "gate-test"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / "review-packet.md").write_text("# packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(
        "# plan\n\n**Review Mode:** FULL\n\n"
        "## Role Selection\n\n"
        "Use all L4 checks for this normal workstream candidate: "
        "fidelity-coverage, composition-interface, evidence-credibility, risk-readiness.\n",
        encoding="utf-8",
    )
    (review_dir / "report.md").write_text(
        "# report\n\nReview summary; routing verdict is in composition-report.md.\n",
        encoding="utf-8",
    )
    (gate_dir / "gate-composition-report.md").write_text(
        "# composition report\n\nVERDICT: ACCEPT — workstream interfaces compose.\n",
        encoding="utf-8",
    )
    _write_signal(runtime, review_addr, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md", "notes": "VERDICT: ACCEPT — ready"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(review_addr), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "return_contract_failed"
        defects = (getattr(action, "detail", None) or {}).get("defects", [])
        assert any("MISSING-REVIEWER-REPORT" in str(d) for d in defects)
        assert ledger.read_binding(producer_addr)["state"] == "running"
        assert ledger.read_binding(review_addr)["state"] == "running"
        assert not _jsonl(_addressing.inbox_path(parent, runtime))
    finally:
        mp.undo()


def test_l4_review_escalate_routes_to_parent_not_producer(runtime):
    """A gate-owned ESCALATE is a parent-altitude question. It wakes the parent with
    gate_escalated and does not pass the candidate or send a local bounce."""
    wd = _wd()
    parent = "proj/area#exec"
    producer_addr = "proj/area/workstream#exec"
    review_addr = "proj/area/workstream#review"
    parent_rec, _parent_token = _binding(
        node_address=parent,
        parent_address="proj#exec",
        level="L3",
        subagent_id="l3",
        session_uuid="l3-session",
        state="running",
        generation=1,
        lease_epoch=1,
    )
    producer, _producer_token = _binding(
        node_address=producer_addr,
        parent_address=parent,
        level="L4",
        state="running",
        generation=4,
        lease_epoch=2,
        subagent_id="l4-exec",
        session_uuid="l4-exec-session",
        extra={
            "gate_required": True,
            "gate_state": "candidate_submitted",
            "gate_review_address": review_addr,
        },
    )
    review, review_token = _binding(
        node_address=review_addr,
        parent_address=parent,
        level="L4",
        state="running",
        generation=3,
        lease_epoch=2,
        subagent_id="l4-review",
        session_uuid="l4-review-session",
        extra={"gate_for": producer_addr},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime, producer_addr, report="# report\n\nCandidate ready.\n")
    review_dir = _node_dir(runtime, review_addr)
    (review_dir / "report.md").write_text(
        "# report\n\nReview summary; routing verdict is in composition-report.md.\n",
        encoding="utf-8",
    )
    gate_dir = _write_short_review_plan(runtime, review_addr)
    (gate_dir / "gate-composition-report.md").write_text(
        "# composition report\n\nVERDICT: ESCALATE — sibling workstream contract is ambiguous.\n",
        encoding="utf-8",
    )
    _write_signal(runtime, review_addr, signal="DONE", owner_token=review_token,
                  evidence={"report": "report.md",
                            "notes": "VERDICT: ESCALATE — sibling contract ambiguous"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(review), ledger.read_binding(review_addr), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_escalated"

        producer_after = ledger.read_binding(producer_addr)
        review_after = ledger.read_binding(review_addr)
        assert producer_after["state"] == "running"
        assert producer_after.get("gate_state") == "gate_escalated"
        assert producer_after.get("gate_verdict") == "ESCALATE"
        assert review_after["state"] == "done"
        assert review_after.get("gate_verdict") == "ESCALATE"

        parent_lines = _jsonl(_addressing.inbox_path(parent, runtime))
        escalated = [l for l in parent_lines if l.get("type") == "gate_escalated"]
        assert len(escalated) == 1
        assert escalated[0].get("candidate") == producer_addr
        assert escalated[0].get("review") == review_addr
        parent_inbox = _addressing.inbox_path(parent, runtime)
        parent_inbox.unlink()
        from harnessd import daemon

        daemon._recover_gate_notifications_best_effort()
        assert len([l for l in _jsonl(parent_inbox) if l.get("type") == "gate_escalated"]) == 1
        daemon._recover_gate_notifications_best_effort()
        assert len([l for l in _jsonl(parent_inbox) if l.get("type") == "gate_escalated"]) == 1
        assert not [l for l in parent_lines if l.get("type") == "gate_passed"]
        assert not _jsonl(_addressing.inbox_path(producer_addr, runtime))

        assert len([l for l in _jsonl(_addressing.inbox_path(parent, runtime))
                    if l.get("type") == "gate_escalated"]) == 1

        (gate_dir / "gate-composition-report.md").write_text(
            "# composition report\n\nVERDICT: ESCALATE — repeated while parent decision is pending.\n",
            encoding="utf-8",
        )
        _write_signal(runtime, review_addr, signal="DONE", owner_token=review_token,
                      evidence={"report": "report.md",
                                "notes": "VERDICT: ESCALATE — repeated while pending"})
        assert len([l for l in _jsonl(_addressing.inbox_path(parent, runtime))
                    if l.get("type") == "gate_escalated"]) == 1
    finally:
        mp.undo()


def test_producer_done_repoll_after_gate_escalate_does_not_gate_fail(runtime):
    """A reviewed ESCALATE is already parent-routed.

    Live LR-121: the producer's DONE signal remained on disk after the review seat closed. A
    later producer tick saw a fresh signal artifact identity and converted the valid
    ``gate_escalated`` state into ``gate_failed`` only because the single-use review seat was now
    terminal. A pending parent-altitude decision must not be overwritten by review-slot health.
    """
    wd = _wd()
    parent_token = fencing.mint_owner_token(PARENT, "psa", "puuid", 1)
    parent_rec = {"node_address": PARENT, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": parent_token,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    producer, producer_token = _binding(
        state="running",
        generation=6,
        lease_epoch=3,
        extra={
            "gate_required": True,
            "gate_state": "gate_escalated",
            "gate_review_address": REVIEW,
            "gate_id": "gate-already-routed",
            "gate_candidate_signal_artifact_seen_at": "candidate-signal-before-rewrite",
            "gate_escalation_reason": "review_escalated",
            "gate_last_escalation_review": REVIEW,
            "gate_verdict": "ESCALATE",
        },
    )
    review, _review_token = _binding(
        node_address=REVIEW,
        parent_address=PARENT,
        level="L5+",
        state="done",
        generation=4,
        lease_epoch=2,
        subagent_id="reviewer",
        session_uuid="review-session",
        extra={"gate_for": LEAF, "gate_verdict": "ESCALATE"},
    )
    _seed([parent_rec, producer, review])
    _prepare_node(runtime, report="# report\n\nCandidate ready; producer signal was rewritten.\n")
    _write_signal(runtime, LEAF, signal="DONE", owner_token=producer_token,
                  evidence={"report": "report.md", "notes": "rewritten producer signal"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(producer), ledger.read_binding(LEAF), now=_now_iso())
        assert _is(action, "NOOP")
        assert (getattr(action, "detail", None) or {}).get("reason") == "gate_escalation_already_committed"

        after = ledger.read_binding(LEAF)
        assert after["state"] == "running"
        assert after.get("gate_state") == "gate_escalated"
        assert after.get("gate_verdict") == "ESCALATE"
        assert len(_wal_rows("gate_failed")) == 0
        assert not [l for l in _jsonl(_addressing.inbox_path(PARENT, runtime))
                    if l.get("type") == "gate_failed"]
    finally:
        mp.undo()


# ===========================================================================
# LR-11 — COLLAPSE WAKES THE PARENT (completion flows UP, agent-lifecycle).
# Observed live 2026-06-11: L2 collapsed DONE and L1 sat unaware until an
# operator hand-delivered the notification; only the generic idle ladder would
# eventually prod a parent into rediscovering tree state.
# ===========================================================================

def test_lr11_collapse_appends_child_collapsed_to_parent_inbox(runtime):
    """A successful DONE collapse appends ONE child_collapsed pointer line to the PARENT's
    inbox (the ③-wake then delivers the nudge). (Mutant: no notification -> parent inbox
    stays empty -> caught.)"""
    wd = _wd()
    parent = PARENT
    ptoken = fencing.mint_owner_token(parent, "psa", "puuid", 1)
    parent_rec = {"node_address": parent, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": ptoken,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([parent_rec, binding])
    _prepare_node(runtime)
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token,
                  evidence={"report": "report.md", "notes": "suite green 9/9"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()

    assert _is(action, "COLLAPSE")
    inbox = _addressing.inbox_path(PARENT, runtime)
    assert inbox.is_file(), "the collapse must notify the PARENT's inbox (LR-11)"
    lines = [json.loads(l) for l in inbox.read_text(encoding="utf-8").splitlines()]
    notes = [l for l in lines if l.get("type") == "child_collapsed"]
    assert len(notes) == 1, f"exactly ONE child_collapsed line; got {len(notes)}"
    assert LEAF in notes[0]["message"] and "DONE" in notes[0]["message"]
    assert "suite green 9/9" in notes[0]["message"], "the sign-off notes ride the nudge"


def test_lr11_collapse_recovery_replays_lost_parent_pointer_once(runtime):
    """A committed non-gate collapse repairs a lost child_collapsed pointer from binding state."""
    wd = _wd()
    parent = PARENT
    ptoken = fencing.mint_owner_token(parent, "psa", "puuid", 1)
    parent_rec = {"node_address": parent, "parent_address": None, "level": "L4",
                  "subagent_id": "psa", "session_uuid": "puuid", "state": "running",
                  "generation": 1, "lease_epoch": 1, "owner_token": ptoken,
                  "last_applied_seq": 0, "liveness_state": "working",
                  "stale_check_count": 0, "stale_grace_checks": 2}
    binding, token = _binding(state="running", generation=4, lease_epoch=2)
    _seed([parent_rec, binding])
    _prepare_node(runtime)
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token,
                  evidence={"report": "report.md", "notes": "done for recovery"})

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()
    assert _is(action, "COLLAPSE")

    inbox = _addressing.inbox_path(PARENT, runtime)
    inbox.unlink()

    from harnessd import daemon

    daemon._recover_terminal_notifications_best_effort()
    notes = [l for l in _jsonl(inbox) if l.get("type") == "child_collapsed"]
    assert len(notes) == 1
    assert notes[0].get("child") == LEAF
    assert notes[0].get("terminal_signal") == "DONE"

    daemon._recover_terminal_notifications_best_effort()
    assert len([l for l in _jsonl(inbox) if l.get("type") == "child_collapsed"]) == 1


def test_review_check_collapse_recovery_skips_future_review_incarnation(runtime):
    """Old review-check completions must not wake a later #review actor at the same address.

    Review-check children are addressed under a gate-id review subtree, but their parent is the
    stable producer ``#review`` seat. Same-address review reuse after a gate pass must not let the
    recovery sweep reconstruct old check completions into the fresh review inbox.
    """
    from harnessd import daemon

    producer = "proj/work#exec"
    review = "proj/work#review"
    check = "proj/work/reviews/gate-old/reviewers/fidelity-coverage#exec"
    producer_rec, _ = _binding(
        node_address=producer,
        parent_address=PARENT,
        level="L4",
        state="running",
        lease_epoch=5,
        extra={
            "gate_state": "candidate_submitted",
            "gate_id": "gate-new",
            "gate_review_address": review,
        },
    )
    review_rec, _ = _binding(
        node_address=review,
        parent_address=producer,
        level="L4+",
        state="planned",
        lease_epoch=6,
        extra={"gate_for": producer},
    )
    check_rec, _ = _binding(
        node_address=check,
        parent_address=review,
        level="L4+",
        state="done",
        lease_epoch=2,
        extra={
            "terminal_signal": "DONE",
            "in_flight_release": True,
            "review_check_for": review,
            "review_check_candidate": producer,
            "gate_id": "gate-old",
            "review_check_axis": "fidelity-coverage",
        },
    )
    _seed([producer_rec, review_rec, check_rec])

    daemon._recover_terminal_notifications_best_effort()

    inbox = _addressing.inbox_path(review, runtime)
    assert not [l for l in _jsonl(inbox) if l.get("type") == "child_collapsed"]


def test_review_check_collapse_recovery_replays_for_current_running_gate(runtime):
    """The review-check fence preserves lost-wake recovery for the current running review lead."""
    from harnessd import daemon

    producer = "proj/work#exec"
    review = "proj/work#review"
    check = "proj/work/reviews/gate-current/reviewers/fidelity-coverage#exec"
    producer_rec, _ = _binding(
        node_address=producer,
        parent_address=PARENT,
        level="L4",
        state="running",
        lease_epoch=5,
        extra={
            "gate_state": "candidate_submitted",
            "gate_id": "gate-current",
            "gate_review_address": review,
        },
    )
    review_rec, _ = _binding(
        node_address=review,
        parent_address=producer,
        level="L4+",
        state="running",
        lease_epoch=6,
        extra={"gate_for": producer},
    )
    check_rec, _ = _binding(
        node_address=check,
        parent_address=review,
        level="L4+",
        state="done",
        lease_epoch=2,
        extra={
            "terminal_signal": "DONE",
            "in_flight_release": True,
            "review_check_for": review,
            "review_check_candidate": producer,
            "gate_id": "gate-current",
            "review_check_axis": "fidelity-coverage",
        },
    )
    _seed([producer_rec, review_rec, check_rec])

    daemon._recover_terminal_notifications_best_effort()

    notes = [
        l for l in _jsonl(_addressing.inbox_path(review, runtime))
        if l.get("type") == "child_collapsed"
    ]
    assert len(notes) == 1
    assert notes[0].get("child") == check


def test_lr11_parentless_root_collapse_is_a_silent_noop(runtime):
    """The L1 root (parent_address None) collapses with NO notification attempt and NO crash."""
    wd = _wd()
    binding, token = _binding(state="running", generation=4, lease_epoch=2,
                              extra={"parent_address": None})
    _seed([binding])
    _prepare_node(runtime)
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)

    mp = pytest.MonkeyPatch()
    try:
        _inject_liveness(mp, _wd(), _const_liveness("working", _now_iso()))
        action = wd.check_leaf(_node_from(binding), _read(), now=_now_iso())
    finally:
        mp.undo()
    assert _is(action, "COLLAPSE")
    assert _read()["state"] == "done"
