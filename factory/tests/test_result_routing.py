"""Daemon/state correctness — result routing (pre-live-run cluster B, part 2).

Pins four fixes:

  * RR-3 — ``watchdog._fail_and_escalate`` routes its ``executor.transition`` result: an ABORTED
    running->failed write (a CAS miss racing the concurrent IPC writer thread; a validate abort)
    returns NOOP (``fail_transition_aborted``) so the next tick retries — never a phantom
    kind=FAILED while the binding stays ``running`` with zero WAL trace.

  * RR-4 — ``poll_once`` consumes the ReconcileReport: each escalation (the v1 detect+escalate
    seat — orphan / necro_failed / coordinator_died) lands ONE edge-triggered
    ``reconcile_escalation`` WAL row; ``_watchdog_tick`` routes ``check_coordinator_death``'s
    pure ESCALATE verdict the same way. Pre-fix, both evaporated every tick.

  * RR-5 — ``_spawn_after_claim`` STEP3's exception net is TOTAL: any non-blessed exception from
    ``adapter.pin_and_open`` (tmux CalledProcessError, FileNotFoundError, AttributeError from a
    garbled config, …) releases the committed claim + emits the §6.3 escalation
    (failure_class ``spawn_exception:<Type>``) instead of escaping with the claim leaked.

  * RR-7 — ``release_claim`` / ``_rollback_spawning`` return their TransitionResult and the §6.3
    ``spawn_failed`` row records the TRUE rollback outcome (``claim_released``) — the journal
    must never assert 'claim released' for a release that aborted.

Style: real ledger/executor/chokepoint on a tmp RUNTIME_ROOT; the only fakes are the
RuntimeAdapter and a minimal tmux list_targets surface (the test_genesis/test_watchdog pattern).
"""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.daemon as daemon
import harnessd.detector_signals as detector_signals
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.watchdog as watchdog
from harnessd.detector import Liveness
from harnessd.spawn import chokepoint


LEAF = "proj/widget#exec"
COORD = "proj#exec"
CHILD = "proj/sub#exec"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(detector_signals, "_size_cache", {}, raising=False)
    monkeypatch.setattr(daemon, "_SWEEP_ERRORS_JOURNALED", {}, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_adapter():
    previous = chokepoint.ADAPTER
    try:
        yield
    finally:
        chokepoint.ADAPTER = previous


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _binding(node_address=LEAF, *, state="running", lease_epoch=1, generation=0,
             parent_address="proj#exec", extra=None):
    token = fencing.mint_owner_token(node_address, "subagent-x", "sess-x", lease_epoch)
    rec = {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": "L5",
        "subagent_id": "subagent-x",
        "session_uuid": "sess-x",
        "tmux_target": f"harness:{node_address}.0",
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "spec_pointer": "design/intent-spec.md",  # E1 fixture completion
        "frozen_acceptance_ref": "acceptance.md",  # E1 fixture completion
        "liveness_state": "working",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "transcript_path": None,
        "stale_check_count": 0,
        "stale_grace_checks": 2,
    }
    if extra:
        rec.update(extra)
    return rec


def _seed(*bindings):
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _rows(event, node=None):
    return [r for r in ledger.load_wal()
            if r.get("event") == event and (node is None or r.get("node_address") == node)]


class FakeTmux:
    def __init__(self, targets=None):
        self._targets = dict(targets or {})

    def list_targets(self):
        return dict(self._targets)


# ===========================================================================
# RR-3 — an aborted watchdog FAILED transition is NOOP'd + retried, never phantom-FAILED.
# ===========================================================================

def test_aborted_fail_transition_returns_noop_not_phantom_failed(runtime):
    """The exact RR-3 race: the snapshot binding the sweep computed its verdict from drifted
    (the concurrent IPC writer bumped the generation) before _fail_and_escalate acted. The CAS
    aborts with NO WAL row — the action must say so (NOOP fail_transition_aborted), never report
    kind=FAILED while the live binding stays running."""
    live = _binding(state="running", generation=5,
                    extra={"stale_check_count": 2, "stale_grace_checks": 2})
    _seed(live)
    stale_snapshot = dict(live, generation=4)  # the pre-drift snapshot the sweep holds

    watchdog.set_liveness(lambda addr: Liveness(state="idle", last_progress_at=_ago(10_000)))
    try:
        action = watchdog.check_leaf(stale_snapshot, stale_snapshot,
                                     now=datetime.now(timezone.utc).isoformat())
    finally:
        watchdog.set_liveness(None)

    assert getattr(action, "kind", None) == watchdog.NOOP, (
        f"an aborted running->failed transition must return NOOP (got {getattr(action, 'kind', None)!r}) "
        "— kind=FAILED on an aborted write is the F2 phantom-success class (RR-3)"
    )
    assert (getattr(action, "detail", None) or {}).get("reason") == "fail_transition_aborted"
    after = ledger.read_binding(LEAF)
    assert after["state"] == "running", "the live binding must be untouched by the aborted CAS"


def test_committed_fail_transition_still_returns_failed(runtime):
    """Control: a CLEAN ladder exhaustion still lands FAILED + escalate_to_parent (no regression)."""
    live = _binding(state="running", generation=5,
                    extra={"stale_check_count": 2, "stale_grace_checks": 2})
    _seed(live)
    watchdog.set_liveness(lambda addr: Liveness(state="idle", last_progress_at=_ago(10_000)))
    try:
        action = watchdog.check_leaf(live, live, now=datetime.now(timezone.utc).isoformat())
    finally:
        watchdog.set_liveness(None)
    assert getattr(action, "kind", None) == watchdog.FAILED
    assert ledger.read_binding(LEAF)["state"] == "failed"


# ===========================================================================
# RR-4 — the ReconcileReport's escalations land durable, edge-triggered rows.
# ===========================================================================

def test_orphan_escalation_is_journaled_once_per_node_kind(runtime):
    """An alive-but-unowned pane (the F35 double-spawn symptom) is detected EVERY tick; pre-fix
    the escalation evaporated at poll_once with zero trace. Now: ONE reconcile_escalation row,
    and steady-state re-detection does not spam a second."""
    tmux = FakeTmux({"harness-orphan:0.0": {"pane_pid": 999, "pane_dead": 0}})

    daemon.poll_once(executor, tmux, None)
    rows = _rows("reconcile_escalation", "harness-orphan:0.0")
    assert len(rows) == 1, (
        f"the orphan escalation must land exactly ONE WAL row (got {len(rows)}) — pre-fix the "
        "v1 escalation seat was a dead end (RR-4)"
    )
    assert (rows[0].get("binding_delta") or {}).get("kind") == "orphan"

    daemon.poll_once(executor, tmux, None)
    assert len(_rows("reconcile_escalation", "harness-orphan:0.0")) == 1, (
        "steady-state re-detection of the SAME orphan must not journal again (edge-triggered)"
    )


def test_orphan_escalation_row_never_reconstructs_a_phantom_binding(runtime):
    """The orphan row is keyed by tmux_target (no binding exists). Boot replay must NOT
    reconstruct a phantom binding from that journal-only row — the next sweep would necro it."""
    from harnessd import reconcile

    tmux = FakeTmux({"harness-orphan:0.0": {"pane_pid": 999, "pane_dead": 0}})
    daemon.poll_once(executor, tmux, None)

    replayed = reconcile.replay_wal(ledger.all_nodes(), ledger.load_wal())
    assert "harness-orphan:0.0" not in replayed, (
        "a journal-only WAL chain (expected_generation None) must never materialize a binding"
    )


def test_necro_failed_escalation_is_journaled(runtime, monkeypatch):
    """A leaf-necro whose terminal write aborts surfaces in the report as necro_failed — that
    escalation must land a WAL row, not be retried+dropped invisibly forever."""
    live = _binding(state="running", generation=3)
    _seed(live)

    real_transition = executor.transition

    def aborting_necro(node_address, **kwargs):
        if kwargs.get("event") == "died_infrastructure":
            return executor.TransitionResult(
                ok=False, errors=["injected CAS race"], warnings=[], binding=None)
        return real_transition(node_address, **kwargs)

    monkeypatch.setattr(executor, "transition", aborting_necro)
    daemon.poll_once(executor, FakeTmux({}), None)  # pane gone -> owned-but-dead -> necro aborts

    rows = _rows("reconcile_escalation", LEAF)
    assert rows and (rows[0].get("binding_delta") or {}).get("kind") == "necro_failed", (
        "an aborted leaf-necro must journal its necro_failed escalation (RR-4)"
    )


def test_coordinator_escalate_verdict_is_journaled(runtime):
    """check_coordinator_death is PURE — its ESCALATE verdict (recoverable orphan: dead pid over
    live children) was discarded at the _watchdog_tick call site. It must land one row."""
    coordinator = _binding(COORD, state="running", parent_address=None,
                           extra={"transcript_path": "/dev/null"})
    child = _binding(CHILD, state="running", parent_address=COORD,
                     extra={"transcript_path": "/dev/null"})
    _seed(coordinator, child)
    # The §5.1 death evidence: a coordinator_died EVENT in the run-ledger (the binding itself is
    # non-terminal, so the sweep still evaluates it).
    executor.journal(COORD, event="coordinator_died", summary="seeded death evidence")

    daemon._watchdog_tick(executor, tmux=None, detector=None)

    rows = _rows("reconcile_escalation", COORD)
    assert rows, "the coordinator ESCALATE verdict must be journaled, not dropped (RR-4)"
    assert (rows[0].get("binding_delta") or {}).get("kind") == "recoverable_orphan"

    daemon._watchdog_tick(executor, tmux=None, detector=None)
    assert len(_rows("reconcile_escalation", COORD)) == 1, "edge-triggered: one row per node+kind"


def test_dead_gate_review_lead_marks_held_candidate_gate_failed(runtime):
    """A review lead is a coordinator once it has review-check children. If that pane dies while
    the producer is held in candidate_submitted, the generic coordinator_died row is not enough:
    the producer gate must become parent-visible gate_failed so it can be retried or repaired."""
    parent = _binding("proj#exec", state="running", parent_address=None, generation=1)
    producer = _binding(
        "proj/work#exec",
        state="running",
        parent_address="proj#exec",
        generation=2,
        extra={
            "level": "L4",
            "gate_required": True,
            "gate_review_address": "proj/work#review",
            "gate_state": "candidate_submitted",
            "gate_id": "gate-work",
        },
    )
    review = _binding(
        "proj/work#review",
        state="running",
        parent_address="proj#exec",
        generation=3,
        extra={
            "level": "L4+",
            "role_variant": "L4+#review",
            "gate_for": "proj/work#exec",
        },
    )
    check = _binding(
        "proj/work/reviews/gate-work/reviewers/fidelity-coverage#exec",
        state="done",
        parent_address="proj/work#review",
        generation=4,
        extra={
            "level": "L4+#check",
            "terminal_signal": "DONE",
            "review_check_for": "proj/work#review",
            "review_check_candidate": "proj/work#exec",
            "gate_id": "gate-work",
        },
    )
    _seed(parent, producer, review, check)
    tmux = FakeTmux({
        parent["tmux_target"]: {"pane_pid": 1001, "pane_dead": 0},
        producer["tmux_target"]: {"pane_pid": 1002, "pane_dead": 0},
    })

    daemon.poll_once(executor, tmux, None)

    after = ledger.read_binding("proj/work#exec")
    assert after.get("gate_state") == "gate_failed"
    assert after.get("gate_failure_class") == "review_lead_died"
    assert ledger.read_binding("proj/work#review").get("state") == "dead"
    assert _rows("reconcile_escalation", "proj/work#review")
    assert _rows("coordinator_died", "proj/work#review")
    parent_inbox = addressing.inbox_path("proj#exec", runtime)
    failed = [
        json.loads(line)
        for line in parent_inbox.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failed = [line for line in failed if line.get("type") == "gate_failed"]
    assert len(failed) == 1

    daemon.poll_once(executor, tmux, None)
    assert len(_rows("gate_failed", "proj/work#exec")) == 1
    failed_again = [
        json.loads(line)
        for line in parent_inbox.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len([line for line in failed_again if line.get("type") == "gate_failed"]) == 1


# ===========================================================================
# RR-5 — the STEP3 rollback net is TOTAL (any exception releases the claim + escalates).
# ===========================================================================

class ExplodingAdapter:
    """pin_and_open raises a NON-blessed exception class (the tmux/config-file fault family)."""

    def __init__(self, exc):
        self._exc = exc

    def pin_and_open(self, *a, **k):
        raise self._exc


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(1, ["tmux", "new-session"]),
    FileNotFoundError("tmux binary missing"),
    AttributeError(".claude.json held a JSON non-dict"),
])
def test_non_blessed_spawn_exception_releases_claim_and_escalates(runtime, exc):
    chokepoint.set_adapter(ExplodingAdapter(exc))
    planned = _binding(state="planned", generation=0, lease_epoch=1)
    _seed(planned)

    result = chokepoint.claim_and_spawn(
        LEAF,
        expected_state="planned",
        expected_generation=0,
        expected_owner_token=planned["owner_token"],
        level_config=config.LevelConfig.for_level("L5"),
    )

    # (a) NO escaped exception, a structured not-ok result naming the class.
    assert result.ok is False
    assert result.failure_class == f"spawn_exception:{type(exc).__name__}", (
        f"the §6.3 'which class fired' surface must name the exception family "
        f"(got {result.failure_class!r})"
    )

    # (b) The claim is RELEASED (claimed->planned, epoch advanced past claim+release) — not leaked
    # for reconcile to mis-necro DIED_INFRA.
    after = ledger.read_binding(LEAF)
    assert after["state"] == "planned", (
        f"the committed claim must be ROLLED BACK on any STEP3 exception (state={after['state']!r} "
        "— a leaked `claimed` slot is the RR-5 defect)"
    )
    assert after["lease_epoch"] >= 3, "claim (1->2) + release (2->3) must both have advanced the epoch"

    # (c) The §6.3 escalation row exists and records the TRUE rollback outcome (RR-7 threading).
    rows = _rows("spawn_failed", LEAF)
    assert rows, "the spawn_failed escalation row must be emitted for the non-blessed class"
    delta = rows[-1].get("binding_delta") or {}
    assert delta.get("claim_released") is True
    assert delta.get("failure_class") == f"spawn_exception:{type(exc).__name__}"
    assert delta.get("failure_reason") == str(exc)


def test_blessed_spawn_failure_classes_still_specific(runtime):
    """Control: the specific classes (SpawnFailure carrying failure_class) are preserved — the
    catch-all must not flatten them into spawn_exception."""
    from harnessd.spawn.oauth_guard import SpawnFailure

    chokepoint.set_adapter(ExplodingAdapter(SpawnFailure("model gone", failure_class="model_unavailable")))
    planned = _binding(state="planned", generation=0, lease_epoch=1)
    _seed(planned)

    result = chokepoint.claim_and_spawn(
        LEAF, expected_state="planned", expected_generation=0,
        expected_owner_token=planned["owner_token"],
        level_config=config.LevelConfig.for_level("L5"),
    )
    assert result.failure_class == "model_unavailable"
    assert ledger.read_binding(LEAF)["state"] == "planned"
    rows = _rows("spawn_failed", LEAF)
    assert rows, "the spawn_failed escalation row must preserve the adapter's reason"
    delta = rows[-1].get("binding_delta") or {}
    assert delta.get("failure_class") == "model_unavailable"
    assert delta.get("failure_reason") == "model gone"
    assert "reason=model gone" in (rows[-1].get("summary") or "")


# ===========================================================================
# RR-7 — the escalation row records the TRUE rollback outcome.
# ===========================================================================

def test_aborted_release_is_not_journaled_as_released(runtime, monkeypatch):
    """If the claimed->planned rollback CAS aborts (raced by the concurrent IPC writer), the
    spawn_failed row must say the claim was NOT released — the pre-fix row hard-coded
    'claim released (§6.3)' regardless: a journaled phantom rollback."""
    from harnessd.spawn.oauth_guard import SpawnFailure

    chokepoint.set_adapter(ExplodingAdapter(SpawnFailure("model gone")))
    planned = _binding(state="planned", generation=0, lease_epoch=1)
    _seed(planned)

    real_transition = executor.transition

    def aborting_release(node_address, **kwargs):
        if kwargs.get("event") == "release_claim":
            return executor.TransitionResult(
                ok=False, errors=["injected rollback CAS race"], warnings=[], binding=None)
        return real_transition(node_address, **kwargs)

    monkeypatch.setattr(executor, "transition", aborting_release)

    result = chokepoint.claim_and_spawn(
        LEAF, expected_state="planned", expected_generation=0,
        expected_owner_token=planned["owner_token"],
        level_config=config.LevelConfig.for_level("L5"),
    )

    assert result.ok is False
    rows = _rows("spawn_failed", LEAF)
    assert rows, "the escalation row is still emitted (the failure must stay visible)"
    delta = rows[-1].get("binding_delta") or {}
    assert delta.get("claim_released") is False, (
        "the row must record the TRUE rollback outcome — claim_released=False (RR-7)"
    )
    assert "claim released (§6.3)" not in (rows[-1].get("summary") or ""), (
        "the summary must not assert a release that aborted (a journaled phantom rollback)"
    )
    assert ledger.read_binding(LEAF)["state"] == "claimed", (
        "sanity: the aborted rollback really did leave the slot claimed"
    )
