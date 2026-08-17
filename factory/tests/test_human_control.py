"""F16 — the human-control WRITE verbs (REMEDIATION; review COMP-3).

pause / resume / answer as harnessctl subcommands routed over IPC through the SINGLE-WRITER
executor, plus the missing WATCHDOG §3.4 STEP 0 read-point so a paused node is never
prodded / FAILED / escalated.

Pause SEMANTICS: setting ``paused_at`` on one node flags the whole subtree (node-or-ancestor
walk); it is a FLAG the spawner and watchdog respect — NOT a kill, NOT a SIGSTOP. An in-flight
agent keeps running; what stops is (a) admission of new children (chokepoint STEP0, already
built) and (b) all watchdog recovery actions. The agent's OWN fenced terminal sign-off (a
DONE/FAILED .signal) is still honored while paused — truth-recording is not a recovery action
(the ratified WATCHDOG §3.4 gate placement: AFTER the STEP A terminal-signal read).

Answer SEMANTICS (TRANSPORTS §5.3 primitive 3): the answer rides terminal_signal=ESCALATED +
terminal_note — the verb stamps terminal_note through the executor (leaving the ESCALATED
stamp IN PLACE: the parent reads both; clearing belongs to the round-trip completion), then
fires the human→parent wake hop by appending a pointer line to the PARENT's
.inbox.<seat>.jsonl (a legitimately multi-writer surface, not the ledger), which the existing
``inbox_has_unacked`` edge-trigger reads.

BIAS TO REAL: the executor + on-disk ledger + WAL are REAL; the .signal/.inbox files are REAL
files under the runtime tree; the ONLY injected seams are the liveness verdict (the watchdog
precedent) and the spawn adapter (the chokepoint precedent).
"""

from __future__ import annotations

import copy
import importlib
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.fencing as fencing
import harnessd.harnessctl as harnessctl
import harnessd.ipc as ipc
import harnessd.ledger as ledger
import harnessd.watchdog as watchdog
from harnessd.detector import Liveness
from harnessd.spawn import chokepoint


# ===========================================================================
# Fixtures — the suite-wide runtime root + the two injected seams (adapter, liveness),
# both reset after each test (no cross-test leak).
# ===========================================================================

@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


def _spawn_result_cls():
    base = importlib.import_module("harnessd.spawn.adapters.base")
    return base.SpawnResult


class FakeAdapter:
    """Records pin_and_open; returns a happy dry-run SpawnResult (no real pane)."""

    def __init__(self):
        self.calls = []

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        SpawnResult = _spawn_result_cls()
        return SpawnResult(
            ok=True,
            session_uuid="sess-human-control-0001",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L3"),
            system_prompt_file=getattr(level_config, "system_prompt_file", config.SYSTEM_PROMPT_FILE),
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path="/runtime/transcripts/sess-human-control-0001.jsonl",
            failure_class=None,
        )


@pytest.fixture
def adapter():
    previous = chokepoint.ADAPTER
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)
    try:
        yield fake
    finally:
        chokepoint.ADAPTER = previous


@pytest.fixture
def liveness_seam():
    """Inject a deterministic liveness verdict through watchdog.set_liveness; reset after."""
    try:
        yield watchdog.set_liveness
    finally:
        watchdog.set_liveness(None)


def _const_liveness(state, last_progress_at):
    def _fn(_node_address):
        return Liveness(state=state, last_progress_at=last_progress_at)
    return _fn


# ===========================================================================
# Seeding helpers — REAL bindings through the REAL ledger (test_watchdog house style).
# ===========================================================================

PARENT = "proj#exec"
LEAF = "proj/widget#exec"
L1_NODE = "root#exec"
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
    stale_check_count=0,
    stale_grace_checks=2,
    paused_at=None,
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
        "spec_pointer": "design/intent-spec.md",  # E1 fixture completion
        "frozen_acceptance_ref": "acceptance.md",  # E1 fixture completion
        "liveness_state": "idle",
        "last_progress_at": None,
        "last_inbox_acked_offset": 0,
        "stale_check_count": stale_check_count,
        "stale_grace_checks": stale_grace_checks,
        "recovery_attempts": 0,
        "gate_crossed_at": None,
        "paused_at": paused_at,
        "transcript_path": None,
        "terminal_signal": None,
    }
    if extra:
        rec.update(extra)
    return rec, token


def _seed(bindings):
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _read(node=LEAF):
    return ledger.read_binding(node)


def _wal_events(node):
    return [r["event"] for r in ledger.load_wal() if r.get("node_address") == node]


def _node_from(binding):
    return {
        "node_address": binding["node_address"],
        "transcript_path": binding.get("transcript_path"),
        "tmux_target": binding.get("tmux_target", "harness:t"),
    }


def _write_signal(runtime, node_address, *, signal, owner_token, evidence=None):
    p = addressing.signal_path(node_address, runtime)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "signal": signal,
        "ts": _now_iso(),
        "owner_token": owner_token,
        "evidence": evidence or {},
    }
    p.write_text(json.dumps(payload))
    return payload


# ===========================================================================
# 1. The pause/resume WRITE verbs over IPC — single-writer, journaled, own-slice
#    (state + generation UNCHANGED: pause is orthogonal to the lifecycle axis).
# ===========================================================================

def test_pause_is_a_known_ipc_write_and_sets_paused_at(runtime):
    assert "pause" in ipc._DISPATCH, "pause must be a registered IPC command (COMP-3)"
    leaf, _token = _binding()
    _seed([leaf])

    resp = ipc.handle_request({"command": "pause", "addr": LEAF})

    assert resp["ok"] is True
    assert resp["binding"] is not None and resp["binding"]["paused_at"] is not None
    live = _read(LEAF)
    assert live["paused_at"] is not None, "pause must durably set paused_at on the binding"
    # journaled exactly once through the single writer (real WAL artifacts, no mocks)
    assert _wal_events(LEAF).count("paused") == 1
    # OWN-SLICE, not a lifecycle transition: state + generation UNCHANGED
    assert live["state"] == "running"
    assert live["generation"] == leaf["generation"]


def test_resume_clears_paused_at_and_journals(runtime):
    assert "resume" in ipc._DISPATCH
    leaf, _token = _binding()
    _seed([leaf])

    pause_resp = ipc.handle_request({"command": "pause", "addr": LEAF})
    resume_resp = ipc.handle_request({"command": "resume", "addr": LEAF})

    assert pause_resp["ok"] is True and resume_resp["ok"] is True
    assert _read(LEAF)["paused_at"] is None, "resume must clear paused_at"
    events = _wal_events(LEAF)
    assert "paused" in events and "resumed" in events
    assert events.index("paused") < events.index("resumed"), "WAL rows in order"


def test_pause_of_absent_node_routes_the_abort(runtime):
    resp = ipc.handle_request({"command": "pause", "addr": "ghost/never#exec"})
    assert resp["ok"] is False, "an absent node is a structured abort, never a silent no-op"
    assert resp["errors"], "the TransitionResult is ROUTED, not swallowed"
    assert resp["binding"] is None


# ===========================================================================
# 2. Loop-level wiring: the pause WRITE feeds the EXISTING chokepoint STEP0 read-point
#    (paused -> the child is refused; resume -> the same spawn proceeds).
# ===========================================================================

def test_pause_blocks_child_spawn_then_resume_readmits(runtime, adapter):
    parent, _ptoken = _binding(node_address=PARENT, parent_address=None, level="L2")
    child, ctoken = _binding(state="planned", generation=0, level="L3")
    _seed([parent, child])
    level_config = config.LevelConfig.for_level("L3")

    assert ipc.handle_request({"command": "pause", "addr": PARENT})["ok"] is True
    before = _read(LEAF)

    refused = chokepoint.claim_and_spawn(
        LEAF,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=ctoken,
        level_config=level_config,
    )
    assert refused.ok is False
    assert refused.failure_class == "paused_subtree"
    assert len(adapter.calls) == 0, "STEP0 aborts BEFORE the claim — no actor opened"
    assert _read(LEAF) == before, "the refused child binding must be byte-identical"

    assert ipc.handle_request({"command": "resume", "addr": PARENT})["ok"] is True

    admitted = chokepoint.claim_and_spawn(
        LEAF,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=ctoken,
        level_config=level_config,
    )
    assert admitted.ok is True, "after resume the SAME spawn must be re-admitted"
    assert len(adapter.calls) == 1


# ===========================================================================
# 3. The MISSING consumer (the load-bearing half of COMP-3): the WATCHDOG §3.4 STEP 0
#    gate — a paused subtree gets NO recovery action (no suspicion, no counter advance,
#    no prod, no watchdog-imposed FAILED, no coordinator-death ESCALATE).
# ===========================================================================

def _failed_ripe_leaf(*, paused_at=None, parent_paused_at=None):
    """A leaf in the EXACT setup that today FAILs it: idle far beyond W, grace exhausted, no .signal."""
    parent, _ptoken = _binding(
        node_address=PARENT, parent_address=None, level="L2", paused_at=parent_paused_at,
    )
    leaf, token = _binding(
        stale_check_count=2, stale_grace_checks=2, paused_at=paused_at,
    )
    _seed([parent, leaf])
    return leaf, token


def test_watchdog_does_not_fail_a_paused_leaf__paired_control(runtime, liveness_seam):
    liveness_seam(_const_liveness("idle", _ago_iso(10 ** 6)))

    # CONTROL — unpaused: the exhausted ladder marches to the watchdog-imposed FAILED.
    leaf, _token = _failed_ripe_leaf(paused_at=None)
    action = watchdog.check_leaf(_node_from(leaf), leaf, now=_now_iso())
    assert action.kind == watchdog.FAILED, "control: the unpaused setup must FAIL (the gate is load-bearing)"
    assert _read(LEAF)["state"] == "failed"
    assert "watchdog_nonresponse" in _wal_events(LEAF)

    # PAUSED — the SAME setup with paused_at set: NO recovery action at all. (The WAL is
    # append-only and shared with the control phase above, so assert on the rows appended
    # AFTER this watermark.)
    watermark = len(ledger.load_wal())
    leaf, _token = _failed_ripe_leaf(paused_at=_now_iso())
    action = watchdog.check_leaf(_node_from(leaf), leaf, now=_now_iso())
    assert action.kind == watchdog.NOOP
    assert action.detail.get("reason") == "paused_subtree"
    live = _read(LEAF)
    assert live["state"] == "running", "a paused idle leaf is human-held quiet, NOT a stall"
    new_events = [r["event"] for r in ledger.load_wal()[watermark:] if r.get("node_address") == LEAF]
    assert "watchdog_nonresponse" not in new_events, "no watchdog-imposed FAILED while paused"
    assert "watchdog_checkpoint" not in new_events, "no suspicion / stale-counter movement while paused"
    assert new_events == [], "a paused leaf gets NO watchdog write at all"


def test_watchdog_skips_leaf_under_paused_ancestor(runtime, liveness_seam):
    liveness_seam(_const_liveness("idle", _ago_iso(10 ** 6)))

    # paused_at on the PARENT only — the leaf's own paused_at stays None.
    leaf, _token = _failed_ripe_leaf(paused_at=None, parent_paused_at=_now_iso())
    action = watchdog.check_leaf(_node_from(leaf), leaf, now=_now_iso())

    assert action.kind == watchdog.NOOP
    assert action.detail.get("reason") == "paused_subtree", (
        "the node-OR-ANCESTOR prefix semantics (DAEMON §3.2) must hold at the watchdog "
        "read-point, not just the spawn one"
    )
    assert _read(LEAF)["state"] == "running"


def test_paused_leaf_terminal_signal_still_collapses(runtime, liveness_seam):
    liveness_seam(_const_liveness("idle", _ago_iso(10 ** 6)))

    parent, _ptoken = _binding(node_address=PARENT, parent_address=None, level="L2")
    leaf, token = _binding(paused_at=_now_iso())
    _seed([parent, leaf])
    _write_signal(runtime, LEAF, signal="DONE", owner_token=token)
    # E2 fixture completion: the return contract requires report.md at DONE.
    _e2_report_dir = addressing.node_dir(LEAF, runtime)
    _e2_report_dir.mkdir(parents=True, exist_ok=True)
    (_e2_report_dir / "report.md").write_text(
        "# report\n\ndone per brief.\n\n"
        "## Drove and Watched\n\nFixture drove the claim.\n\n"
        "## Inferred\n\nSupporting checks passed.\n\n"
        "## Residual Uncertainty\n\nNone beyond fixture scope.\n\n"
        "## Inventory\n\nNone.\n",
        encoding="utf-8",
    )

    action = watchdog.check_leaf(_node_from(leaf), leaf, now=_now_iso())

    assert action.kind == watchdog.COLLAPSE, (
        "the agent's OWN fenced DONE sign-off is truth-recording, not a recovery action — "
        "it is honored while paused (the ratified §3.4 gate placement, AFTER STEP A)"
    )
    assert _read(LEAF)["state"] == "done"


def test_paused_dead_coordinator_is_not_escalated__paired_control(runtime):
    coord, _ctoken = _binding(
        node_address=PARENT, parent_address=None, level="L2", state="dead",
    )
    child, _token = _binding(state="running")
    _seed([coord, child])

    # CONTROL — unpaused: dead coordinator + live child = recoverable orphan -> ESCALATE.
    action = watchdog.check_coordinator_death(_node_from(coord), _read(PARENT), ledger)
    assert action.kind == watchdog.ESCALATE
    assert action.detail.get("reason") == "recoverable_orphan"

    # PAUSED: the ESCALATE is deferred (NOOP, paused_subtree).
    assert ipc.handle_request({"command": "pause", "addr": PARENT})["ok"] is True
    action = watchdog.check_coordinator_death(_node_from(coord), _read(PARENT), ledger)
    assert action.kind == watchdog.NOOP
    assert action.detail.get("reason") == "paused_subtree"

    # RESUME: the next call re-evaluates from durable state — nothing is lost.
    assert ipc.handle_request({"command": "resume", "addr": PARENT})["ok"] is True
    action = watchdog.check_coordinator_death(_node_from(coord), _read(PARENT), ledger)
    assert action.kind == watchdog.ESCALATE


# ===========================================================================
# 4. The answer verb — fail-loud ESCALATED guard, terminal_note stamped through the
#    executor (the ESCALATED stamp left IN PLACE), then the human->parent wake hop.
# ===========================================================================

def test_answer_requires_content(runtime):
    leaf, _token = _binding(extra={"terminal_signal": "ESCALATED"})
    _seed([leaf])

    missing = ipc.handle_request({"command": "answer", "addr": LEAF})
    empty = ipc.handle_request(
        {"command": "answer", "addr": LEAF, "answer_content": ""}
    )

    assert missing["ok"] is False
    assert empty["ok"] is False
    assert any("answer_content" in error for error in missing["errors"])
    assert any("answer_content" in error for error in empty["errors"])
    assert _read(LEAF).get("terminal_note") is None, "nothing half-written"


def test_answer_refuses_a_non_escalated_node(runtime):
    leaf, _token = _binding()  # running, terminal_signal None, NO .signal artifact
    _seed([leaf])

    resp = ipc.handle_request({"command": "answer", "addr": LEAF, "answer_content": "go left"})

    assert resp["ok"] is False
    assert any("ESCALATED" in e for e in resp["errors"]), "fail-loud: names the not-ESCALATED refusal"
    assert _read(LEAF).get("terminal_note") is None, "no half-written note"
    assert "human_answer_posted" not in _wal_events(LEAF)
    assert list(runtime.rglob(".inbox.*")) == [], "no wake line written anywhere on a refusal"


def test_answer_stamps_note_and_wakes_the_parent(runtime):
    assert "answer" in ipc._DISPATCH
    parent, _ptoken = _binding(node_address=PARENT, parent_address=None, level="L2")
    # The §3.6 ASYMMETRIC posture: terminal_signal=ESCALATED while state STAYS running.
    child, _token = _binding(extra={"terminal_signal": "ESCALATED"})
    _seed([parent, child])

    resp = ipc.handle_request({"command": "answer", "addr": LEAF, "answer_content": "use option B"})

    assert resp["ok"] is True
    assert resp["wake_target"] == PARENT

    live = _read(LEAF)
    assert live["terminal_note"] == "use option B", "the answer rides terminal_note (TRANSPORTS §5.3)"
    assert live["state"] == "running", "no lifecycle movement — the answer is an own-slice stamp"
    assert live["terminal_signal"] == "ESCALATED", (
        "the ESCALATED stamp stays IN PLACE: the answer RIDES terminal_signal=ESCALATED + "
        "terminal_note; clearing it belongs to the round-trip completion (and would let the "
        "watchdog re-journal the escalation off the still-present .signal artifact)"
    )
    assert "human_answer_posted" in _wal_events(LEAF)

    # The human->parent wake hop: one pointer line in the PARENT's wake inbox.
    inbox = addressing.inbox_path(PARENT, runtime)
    assert inbox.exists()
    lines = [json.loads(l) for l in inbox.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert LEAF in lines[0]["message"] and "decision-down" in lines[0]["message"]

    # …and the EXISTING ③-wake trigger actually fires off the real file (loop-level wiring).
    parent_binding = _read(PARENT)
    assert parent_binding.get("last_inbox_acked_offset", 0) == 0
    assert watchdog.inbox_has_unacked(_node_from(parent_binding), parent_binding) is True


def test_answer_down_writes_child_visible_decision_and_stamps_answer_state(runtime):
    assert "answer-down" in ipc._DISPATCH
    parent, _ptoken = _binding(node_address=PARENT, parent_address=None, level="L2")
    child, _token = _binding(extra={
        "terminal_signal": "ESCALATED",
        "terminal_signal_at": _ago_iso(60),
        "signal_artifact_seen_at": "sha256:question-1",
    })
    _seed([parent, child])

    resp = ipc.handle_request({
        "command": "answer-down",
        "addr": LEAF,
        "answer_content": "# verdict\nFix D1 and resubmit.\n",
    })

    assert resp["ok"] is True
    assert resp["wake_target"] == LEAF
    assert resp["answer_route"] == "parent_to_child_alive"

    live = _read(LEAF)
    assert live["terminal_note"] == "# verdict\nFix D1 and resubmit.\n"
    assert live["answered_at"], "Branch-A answer-down stamps the same canonical answered state"
    assert live["answer_route"] == "parent_to_child_alive"
    assert live["answer_actor"] == PARENT
    assert live["answer_artifact"] == resp["decision_artifact"]
    assert live["answered_signal_artifact_seen_at"] == "sha256:question-1"
    assert "parent_answer_posted" in _wal_events(LEAF)
    assert "human_answer_posted" not in _wal_events(LEAF)

    decision = runtime / resp["decision_artifact"]
    assert decision.exists()
    assert decision.read_text(encoding="utf-8") == "# verdict\nFix D1 and resubmit.\n"

    child_inbox = addressing.inbox_path(LEAF, runtime)
    assert child_inbox.exists()
    lines = [json.loads(l) for l in child_inbox.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0]["type"] == "message"
    assert lines[0]["answers_question"]["asker_address"] == LEAF
    assert lines[0]["sender"] == PARENT
    assert lines[0]["artifact"].startswith("messages/legacy-answer-")
    assert watchdog.inbox_has_unacked(_node_from(live), live) is True

    parent_inbox = addressing.inbox_path(PARENT, runtime)
    assert not parent_inbox.exists(), "answer-down wakes the child, not the parent"


def test_answer_down_refuses_a_non_escalated_node(runtime):
    parent, _ptoken = _binding(node_address=PARENT, parent_address=None, level="L2")
    child, _token = _binding()
    _seed([parent, child])

    resp = ipc.handle_request({
        "command": "answer-down",
        "addr": LEAF,
        "answer_content": "not yet",
    })

    assert resp["ok"] is False
    assert any("ESCALATED" in e for e in resp["errors"])
    assert _read(LEAF).get("answered_at") is None
    assert not addressing.inbox_path(LEAF, runtime).exists()


def test_answer_accepts_escalation_visible_only_in_signal_artifact(runtime):
    """The fenced .signal-artifact fallback: an agent that wrote ESCALATED but whose binding the
    watchdog has not yet journaled (the tick gap) is still answerable — the guard reads the
    durable artifact through the SAME fenced reader the watchdog uses."""
    parent, _ptoken = _binding(node_address=PARENT, parent_address=None, level="L2")
    child, token = _binding()  # binding terminal_signal None — only the artifact says ESCALATED
    _seed([parent, child])
    _write_signal(runtime, LEAF, signal="ESCALATED", owner_token=token)

    resp = ipc.handle_request({"command": "answer", "addr": LEAF, "answer_content": "ship it"})

    assert resp["ok"] is True
    assert _read(LEAF)["terminal_note"] == "ship it"


def test_answer_to_a_parentless_root_wakes_the_node_itself(runtime):
    root, _token = _binding(
        node_address=L1_NODE, parent_address=None, level="L1",
        extra={"terminal_signal": "ESCALATED"},
    )
    _seed([root])

    resp = ipc.handle_request({"command": "answer", "addr": L1_NODE, "answer_content": "approved"})

    assert resp["ok"] is True
    assert resp["wake_target"] == L1_NODE, "the human IS L1's parent — the answer wakes L1 itself"
    inbox = addressing.inbox_path(L1_NODE, runtime)
    assert inbox.exists()
    lines = [json.loads(l) for l in inbox.read_text().splitlines() if l.strip()]
    assert len(lines) == 1 and L1_NODE in lines[0]["message"]


# ===========================================================================
# 5. harnessctl — the CLIENT surface (pure arg-parse + serialization; F15 precedent).
# ===========================================================================

def test_harnessctl_exposes_pause_resume_answer_subcommands(tmp_path):
    parser = harnessctl.build_parser()

    args = parser.parse_args(["pause", LEAF])
    assert harnessctl._build_request(args) == {"command": "pause", "addr": LEAF}

    args = parser.parse_args(["resume", LEAF])
    assert harnessctl._build_request(args) == {"command": "resume", "addr": LEAF}

    assert "gate-retry" in ipc._DISPATCH
    args = parser.parse_args(["gate-retry", LEAF])
    assert harnessctl._build_request(args) == {"command": "gate-retry", "addr": LEAF, "expected_owner_token": None}
    args = parser.parse_args(["gate-retry", LEAF, "--expected-owner-token", "tok"])
    assert harnessctl._build_request(args) == {"command": "gate-retry", "addr": LEAF, "expected_owner_token": "tok"}

    assert "gate-accept" in ipc._DISPATCH
    args = parser.parse_args([
        "gate-accept", LEAF,
        "--resolver", PARENT,
        "--expected-parent-owner-token", "ptok",
        "--notes", "approved",
    ])
    assert harnessctl._build_request(args) == {
        "command": "gate-accept",
        "addr": LEAF,
        "resolver_address": PARENT,
        "expected_parent_owner_token": "ptok",
        "verdict_notes": "approved",
    }

    assert "gate-return" in ipc._DISPATCH
    args = parser.parse_args([
        "gate-return", LEAF,
        "--resolver", PARENT,
        "--expected-parent-owner-token", "ptok",
        "--notes", "repair the named contract mismatch",
    ])
    assert harnessctl._build_request(args) == {
        "command": "gate-return",
        "addr": LEAF,
        "resolver_address": PARENT,
        "expected_parent_owner_token": "ptok",
        "verdict_notes": "repair the named contract mismatch",
    }

    assert "test-refresh-approve" in ipc._DISPATCH
    args = parser.parse_args([
        "test-refresh-approve", PARENT,
        "--approver", "proj#exec",
        "--expected-parent-owner-token", "l3tok",
        "--notes", "faithful to the workstream spec",
    ])
    assert harnessctl._build_request(args) == {
        "command": "test-refresh-approve",
        "addr": PARENT,
        "approver_address": "proj#exec",
        "expected_parent_owner_token": "l3tok",
        "notes": "faithful to the workstream spec",
    }

    args = parser.parse_args(["answer", LEAF, "--text", "go"])
    assert harnessctl._build_request(args) == {"command": "answer", "addr": LEAF, "answer_content": "go"}

    args = parser.parse_args(["answer-down", LEAF, "--text", "resume with D1 fixed"])
    assert harnessctl._build_request(args) == {
        "command": "answer-down",
        "addr": LEAF,
        "answer_content": "resume with D1 fixed",
    }

    answer_file = tmp_path / "answer.md"
    answer_file.write_text("use option B\n", encoding="utf-8")
    args = parser.parse_args(["answer", LEAF, "--file", str(answer_file)])
    request = harnessctl._build_request(args)
    assert request["answer_content"] == "use option B\n", "--file ships the file CONTENTS"

    args = parser.parse_args(["answer-down", LEAF, "--file", str(answer_file)])
    request = harnessctl._build_request(args)
    assert request == {"command": "answer-down", "addr": LEAF, "answer_content": "use option B\n"}

    with pytest.raises(SystemExit):
        parser.parse_args(["answer", LEAF])  # neither --text nor --file: argparse required group

    # The pause help must carry the not-a-kill semantics (the operator must not be surprised
    # that a paused agent keeps typing).
    source = inspect.getsource(harnessctl)
    assert "NOT a kill" in source
