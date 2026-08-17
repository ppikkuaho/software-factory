"""F4 — WAL replay applies the record's authoritative ``to_state`` (review executor-1 / WAL-01).

THE DEFECT (spec-review cluster ③, the most serious durability finding, 3/3 verified):
``reconcile._apply_one`` replayed a committed WAL event by ``applied.update(binding_delta)`` ONLY —
it never applied the record's authoritative ``to_state`` and never pinned ``node_address``. The
executor sets ``candidate["state"] = target_state`` authoritatively at commit time (executor.py,
the §4.2 candidate rule), so any transition whose caller delta omits ``state`` — the LIVE shape of
chokepoint STEP4 (``spawn_open``) and STEP5 (``spawn_running``, delta={}), ``chokepoint.collapse``
(delta = {terminal_signal, in_flight_release}), and the watchdog's ``watchdog_nonresponse`` — replays
to the UN-ADVANCED pre-image state after a §4.4-window crash (WAL appended, binding checkpoint never
landed). Recovery silently rolls a node BACKWARD: a collapse-to-done replays with
terminal_signal=DONE but state still ``running``; a STEP5 replays to a node stuck at ``spawning``.

THE FIX mirrored here: in the forward-apply branch of ``_apply_one``, AFTER the delta merge,
``applied["node_address"] = event["node_address"]`` and ``applied["state"] = event["to_state"]`` —
the exact replay-side mirror of the executor's commit-side candidate rule.

This file is the F-fix pattern of a NEW dedicated test file (cf. test_collapse_result.py,
test_failure_taxonomy.py): tests/test_reconcile.py and tests/test_integration_c.py are FROZEN
acceptance files and stay byte-stable.

BIAS TO REAL (Lesson 7): REAL ``ledger.build_wal_record`` records (the test_reconcile.py
``_wal_record`` pattern), REAL on-disk ledgers under a tmp RUNTIME_ROOT, and — for the loop-level
gate — a REAL subprocess SIGKILLed mid-life (the test_integration_c.py ``_crash_and_tear`` recipe)
whose pending transition's delta DELIBERATELY omits ``state`` (the path the existing integration-C
helper masks: its pending delta carries ``"state": "running"``).

LOAD-BEARING (each test kills a named mutant):
  * delta-omits-state replay must land on to_state (mutant: delta-only replay leaves the pre-image)
  * an EMPTY delta must still advance state (mutant: update({}) no-op freezes the lifecycle state)
  * a collapse-shaped delta must land state+terminal_signal TOGETHER (mutant: corrupt half-state)
  * a delta-smuggled node_address must NOT re-key the binding (mutant: update() overwrites identity)
  * the authoritative apply lives ONLY in the forward-apply branch (mutant: stamp to_state on the
    skip branches / before the watermark + CAS guards)
  * a REAL kill-9 + relaunch recovers the stateless-delta transition to the record's to_state
"""

from __future__ import annotations

import copy
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.reconcile as reconcile
import harnessd.states as states


REPO_ROOT = str(Path(__file__).resolve().parent.parent)

NODE = "proj/widget#exec"


# ---------------------------------------------------------------------------
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path so every pathless real
# ledger/executor call targets the test tree (the established suite pattern —
# mirrors test_reconcile.py::runtime exactly).
# ---------------------------------------------------------------------------

@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


# ---------------------------------------------------------------------------
# Minimal fake tmux — the FROZEN §2.11 surface, list_targets() ONLY (Lesson 6).
# ---------------------------------------------------------------------------

class FakeTmux:
    def __init__(self, targets):
        self._targets = targets

    def list_targets(self):
        return dict(self._targets)


# ---------------------------------------------------------------------------
# Real-record + real-binding helpers (the test_reconcile.py patterns).
# ---------------------------------------------------------------------------

def _binding(node_address=NODE, *, state="spawning", generation=4, lease_epoch=3,
             last_applied_seq=0, session_uuid="sess-aaaa-0001", liveness_state="booting"):
    """One CAS-bearing binding (the §3.2 field set replay + the executor read)."""
    subagent = "subagent-" + node_address.replace("/", "-").replace("#", "-")
    owner_token = fencing.mint_owner_token(node_address, subagent, session_uuid, lease_epoch)
    return {
        "node_address": node_address,
        "parent_address": None,
        "level": "L5",
        "subagent_id": subagent,
        "session_uuid": session_uuid,
        "tmux_target": "harness:" + node_address,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": owner_token,
        "last_applied_seq": last_applied_seq,
        "liveness_state": liveness_state,
        "terminal_signal": None,
        "terminal_signal_at": None,
    }


def _wal_record(*, seq, expected_generation, generation, binding_delta, owner_token,
                node_address=NODE, event="state_transition", from_state="spawning",
                to_state="running", lease_epoch=3):
    """One REAL WAL record via the real builder (carries crc32 + the transition pre/post image)."""
    return ledger.build_wal_record(
        node_address=node_address,
        event=event,
        from_state=from_state,
        to_state=to_state,
        expected_generation=expected_generation,
        generation=generation,
        lease_epoch=lease_epoch,
        owner_token=owner_token,
        binding_delta=binding_delta,
        summary="f4-replay-test",
        artifacts=[],
        seq=seq,
    )


# ===========================================================================
# Unit level — mutant-killing tests on real build_wal_record records.
# ===========================================================================

def test_replay_applies_authoritative_to_state_when_delta_omits_state(runtime):
    """A pending event whose delta OMITS ``state`` (the live STEP4 shape) must replay to the
    record's authoritative ``to_state``, not the un-advanced pre-image.

    Mutant killed: delta-only replay leaves state at the pre-image after a §4.4-window crash —
    the executor-1/WAL-01 corruption (recovery rolls the node backward).
    """
    binding = _binding(state="spawning", generation=4, last_applied_seq=0)
    post_token = "proj/widget#exec:sub:sess-aaaa-0001:3"
    pending = _wal_record(
        seq=5,
        expected_generation=4,        # pre-image MATCHES the live binding.generation
        generation=5,                 # post-commit generation
        owner_token=post_token,
        binding_delta={"liveness_state": "working"},  # NO 'state' key — the live STEP4 shape
        from_state="spawning",
        to_state="running",
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [pending])

    rb = out[NODE]
    assert rb["state"] == "running", (
        "replay must apply the record's authoritative to_state ('running') when the delta omits "
        "'state' — NOT leave the un-advanced pre-image ('spawning'); the executor sets state from "
        "the legality-checked target_state at commit time (executor-1 / WAL-01)"
    )
    assert rb["generation"] == 5, "replay must advance generation to the event's POST-commit value"
    assert rb["last_applied_seq"] == 5, "replay must stamp last_applied_seq = seq (the watermark)"
    assert rb["liveness_state"] == "working", "the binding_delta must STILL be applied (delta + to_state)"


def test_replay_empty_delta_still_advances_state(runtime):
    """The exact chokepoint STEP5 shape: ``binding_delta={}`` entirely, spawning -> running.

    Mutant killed: ``applied.update({})`` is a no-op, so an empty-delta transition replays as a
    pure generation bump with the lifecycle state frozen at 'spawning'.
    """
    binding = _binding(state="spawning", generation=4, last_applied_seq=0)
    pending = _wal_record(
        seq=7,
        expected_generation=4,
        generation=5,
        owner_token="t:post:5",
        binding_delta={},             # the live STEP5 spawn_running shape
        event="spawn_running",
        from_state="spawning",
        to_state="running",
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [pending])

    rb = out[NODE]
    assert rb["state"] == "running", (
        "an EMPTY-delta state-advancing transition (STEP5 spawn_running) must replay to the "
        "record's to_state — not stay frozen at the pre-image 'spawning'"
    )
    assert rb["generation"] == 5, "generation must be stamped to the post-commit value"
    assert rb["last_applied_seq"] == 7, "the watermark must advance to the event's seq"


def test_replay_terminal_collapse_delta_without_state_lands_terminal(runtime):
    """The live ``chokepoint.collapse`` shape: delta = {terminal_signal, in_flight_release}, NO
    'state'. After replay, state AND terminal_signal must land TOGETHER — never the corrupt
    half-state (terminal_signal stamped, state still 'running') the delta-only replay produces.

    The event name + signal/state pair are derived from the §3.6 TERMINAL_VOCAB row (not
    hand-invented) so the fixture tracks the live vocabulary across the F-series.
    Mutant killed: the same delta-only mutant on the most dangerous real path — a crash
    mid-collapse leaves a done node recorded as still running.
    """
    done_row = states.TERMINAL_VOCAB["signal_DONE"]
    binding = _binding(state="running", generation=8, last_applied_seq=0, liveness_state="working")
    pending = _wal_record(
        seq=9,
        expected_generation=8,
        generation=9,
        owner_token="t:post:9",
        binding_delta={
            "terminal_signal": done_row.terminal_signal,  # the live collapse delta — NO 'state'
            "in_flight_release": True,
        },
        event=done_row.event,
        from_state="running",
        to_state=done_row.state,
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [pending])

    rb = out[NODE]
    assert rb["state"] == done_row.state and rb["terminal_signal"] == done_row.terminal_signal, (
        "a replayed collapse must land state AND terminal_signal TOGETHER "
        f"(state={done_row.state!r} + terminal_signal={done_row.terminal_signal!r}) — got "
        f"state={rb['state']!r}, terminal_signal={rb.get('terminal_signal')!r}: the half-state "
        "(signal stamped, state still 'running') is the executor-1/WAL-01 corruption"
    )
    assert rb["generation"] == 9
    assert rb["last_applied_seq"] == 9


def test_replay_pins_node_address_against_delta_smuggle(runtime):
    """A delta carrying ``node_address`` must NOT re-key the binding: identity is authoritative
    from the record (the mirror of executor.transition's ``candidate["node_address"]`` pin).

    Mutant killed: ``applied.update(delta)`` overwrites identity with a smuggled address.
    """
    binding = _binding(state="spawning", generation=4, last_applied_seq=0)
    pending = _wal_record(
        seq=5,
        expected_generation=4,
        generation=5,
        owner_token="t:post:5",
        binding_delta={"node_address": "evil/other#exec", "liveness_state": "working"},
        from_state="spawning",
        to_state="running",
    )

    out = reconcile.replay_wal({NODE: copy.deepcopy(binding)}, [pending])

    assert out[NODE]["node_address"] == NODE, (
        "replay must pin node_address to the RECORD's node_address — a delta-smuggled "
        "'evil/other#exec' must not re-key the binding (the executor's identity pin, mirrored)"
    )


def test_replay_to_state_not_stamped_on_skip_branches(runtime):
    """Guard isolation: the authoritative to_state apply lives ONLY in the forward-apply branch.

    (a) an ALREADY-LANDED event (live generation == event.generation) whose to_state DIFFERS from
        the live state must leave the binding byte-for-byte unchanged;
    (b) a PRE-IMAGE-MISMATCHED event (expected_generation matches neither) likewise.

    Mutant killed: stamping to_state unconditionally / before the watermark + CAS guards.
    """
    # (a) already-landed: binding at the event's POST-commit generation; to_state differs.
    landed_binding = _binding(state="running", generation=5, last_applied_seq=0,
                              liveness_state="working")
    before_landed = copy.deepcopy(landed_binding)
    landed = _wal_record(
        seq=5,
        expected_generation=4,
        generation=5,                 # == live generation -> already-landed skip
        owner_token="t:landed:5",
        binding_delta={},
        from_state="running",
        to_state="blocked",           # DIFFERS from the live 'running' — must NOT be stamped
    )
    out = reconcile.replay_wal({NODE: copy.deepcopy(landed_binding)}, [landed])
    assert out[NODE] == before_landed, (
        "an already-landed event must be a byte-for-byte NO-OP — its to_state must NOT be stamped "
        "(the authoritative apply lives ONLY in the forward-apply branch)"
    )

    # (b) pre-image mismatch: neither expected_generation nor post-commit generation matches.
    mismatched_binding = _binding(state="running", generation=4, last_applied_seq=0,
                                  liveness_state="working")
    before_mismatched = copy.deepcopy(mismatched_binding)
    mismatched = _wal_record(
        seq=6,
        expected_generation=99,       # matches neither -> NOT applicable, NOT already-landed
        generation=100,
        owner_token="t:bogus:0",
        binding_delta={},
        from_state="running",
        to_state="dead",              # must NOT be stamped
    )
    out = reconcile.replay_wal({NODE: copy.deepcopy(mismatched_binding)}, [mismatched])
    assert out[NODE] == before_mismatched, (
        "a pre-image-mismatched event must be a byte-for-byte NO-OP — its to_state must NOT be "
        "stamped (the watermark + CAS guards fire BEFORE the authoritative apply)"
    )


# ===========================================================================
# Loop level — a REAL kill-9 + relaunch recovery for a state-advancing
# transition whose binding_delta omits 'state' (the path the existing
# integration-C helper masks: its pending delta carries "state": "running").
# Modeled exactly on test_integration_c._crash_and_tear (real process boundary,
# Lesson 7).
# ===========================================================================

_HELPER_SOURCE = textwrap.dedent(
    '''
    import sys, time
    from pathlib import Path

    REPO_ROOT = sys.argv[1]
    RUNTIME_ROOT = sys.argv[2]
    sys.path.insert(0, REPO_ROOT)

    import harnessd.executor as executor
    import harnessd.fencing as fencing
    import harnessd.ledger as ledger

    ledger.RUNTIME_ROOT = Path(RUNTIME_ROOT)

    NODE = "proj/widget#exec"
    SUB = "subagent-proj-widget-exec"
    UUID = "sess-widget-0001"

    # Seed the node SPAWNING at generation 4 — a real fencing token, the full CAS-bearing shape.
    token = fencing.mint_owner_token(NODE, SUB, UUID, 3)
    binding = {
        "node_address": NODE, "parent_address": "proj#exec", "level": "L5",
        "subagent_id": SUB, "session_uuid": UUID,
        "tmux_target": "harness:" + NODE, "state": "spawning",
        "generation": 4, "lease_epoch": 3, "owner_token": token,
        "last_applied_seq": 0, "liveness_state": "booting",
        "terminal_signal": None, "terminal_signal_at": None,
    }
    ledger.write_binding({NODE: binding}, _lock_held=True)

    # The PENDING state-advancing transition: spawning -> running with a delta that OMITS 'state'
    # (the live STEP5/STEP4 shape). append_wal LANDS (intent journaled) but write_binding RAISES ->
    # the binding checkpoint never lands. Intent-first (§4.4) => WAL-ahead-of-binding, replayable.
    real_write_binding = ledger.write_binding

    def crashing_write_binding(candidate_map, *, _lock_held, binding_path=None):
        raise OSError("simulated crash during write_binding (commit step-2)")

    ledger.write_binding = crashing_write_binding
    try:
        executor.transition(
            NODE, expected_state="spawning", expected_generation=4,
            expected_owner_token=token, target_state="running",
            binding_delta={"liveness_state": "working"},   # DELTA OMITS STATE — the F4 path
            event="spawn_running",
            summary="pending spawning->running whose delta omits state (F4 kill-9 scenario)",
        )
    except OSError:
        pass
    finally:
        ledger.write_binding = real_write_binding

    # READY: the durable state is on disk. Block forever so the parent kills us mid-life.
    print("READY", flush=True)
    while True:
        time.sleep(3600)
    '''
)


def _crash_kill9(runtime_root):
    """Build the WAL-ahead state in a REAL subprocess, then REAL os.kill(pid, SIGKILL)."""
    helper_path = runtime_root / "_f4_kill9_helper.py"
    helper_path.write_text(_HELPER_SOURCE, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(helper_path), REPO_ROOT, str(runtime_root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for the helper to durably build state and signal READY (bounded — never hang).
        deadline = time.time() + 30.0
        ready_line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if line.strip() == "READY":
                ready_line = "READY"
                break
        if ready_line != "READY":
            stderr = proc.stderr.read()
            raise AssertionError(
                "the F4 helper subprocess never reached READY (it must build REAL on-disk "
                f"WAL-ahead state then block): stderr=\n{stderr}"
            )

        # THE REAL HARD KILL: os.kill(pid, SIGKILL), no cleanup (the daemon-died signature).
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_kill9_recovery_replays_state_advancing_transition_with_stateless_delta(runtime):
    """A REAL kill-9 + relaunch: the pending spawning->running transition (whose delta OMITS
    'state') must be replayed to the record's authoritative to_state by the fresh-parent recovery.

    Mutant killed: delta-only replay leaves the node rolled back to 'spawning' after a genuine
    kill-9 + relaunch (the executor-1/WAL-01 corruption, at the real process boundary).
    """
    _crash_kill9(runtime)

    node = "proj/widget#exec"

    # ---- SCENARIO VALIDITY (fresh parent, real ledger reads): pins that this test exercises ----
    # ---- the omitted-state path the existing integration-C helper masks.                    ----
    on_disk = ledger.read_binding(node)
    assert on_disk["state"] == "spawning" and on_disk["generation"] == 4, (
        "scenario validity: the on-disk binding must still be the PRE-image (spawning@4) — the "
        "pending event's checkpoint never landed (WAL-ahead-of-binding, the §4.4 window)"
    )
    wal = ledger.load_wal()
    assert wal, "scenario validity: the pending intent row must be journaled in the WAL"
    pending_row = wal[-1]
    assert pending_row["node_address"] == node and pending_row["to_state"] == "running", (
        "scenario validity: the last WAL row must be the pending spawning->running transition"
    )
    assert "state" not in (pending_row["binding_delta"] or {}), (
        "scenario validity: the pending row's binding_delta must OMIT 'state' — this is the exact "
        "path the existing integration-C fixture masks (its delta carries state='running')"
    )

    # ---- RECOVERY: the REAL reconcile_on_restart in the fresh parent. The pane is ALIVE and ----
    # ---- the minimal §2.11 listing carries no uuid, so the replayed node is ADOPTED —       ----
    # ---- isolating replay from the necro sweep.                                             ----
    tmux = FakeTmux({"harness:" + node: {"pane_pid": 4321, "pane_dead": 0, "window_activity": "0"}})
    report = reconcile.reconcile_on_restart(executor, tmux)

    # ---- THE F4 PREDICATE: the record's authoritative to_state landed on the binding. ----
    recovered = ledger.read_binding(node)
    assert recovered["state"] == "running", (
        "kill-9 recovery must replay the pending transition to the record's authoritative "
        "to_state ('running') — a node left at 'spawning' is the executor-1/WAL-01 rollback: "
        "delta-only replay silently rolled the node backward across the crash"
    )
    assert recovered["generation"] == 5, "the replayed binding must carry the post-commit generation"
    assert recovered["last_applied_seq"] == pending_row["seq"], (
        "the replay watermark must advance to exactly the pending record's seq"
    )
    assert node in report.adopted, "the replayed (running, pane-alive) node must be ADOPTED"
    assert node not in report.necroed, "the recovered node must NOT be necro'd"
    assert node not in [e.get("node_address") for e in report.escalations if isinstance(e, dict)], (
        "the recovered node must NOT be escalated"
    )
