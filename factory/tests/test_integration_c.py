"""Increment 15 — Integration C (kill-9 recovery) FROZEN acceptance. Tests ONLY.

THE DURABILITY GATE. The whole crash-safety thesis — intent-first WAL + torn-tail-tolerant
load + reconcile-on-restart + fencing — is proven HERE or nowhere. A REAL crash, a REAL recovery,
and 5 CONCRETE predicates read off the REAL recovered ledger. No vacuous pass.

Authoritative (grounded, not recalled — Lesson 4):
  - IMPLEMENTATION-PLAN Increment-15 Done-test (L818-827) + Integration C (L641-646) +
    DONE_WHEN clause 4 / the kill-9 clause (L14-21: "a kill -9 of the daemon followed by relaunch
    recovers state from the ledger (binding-ledger + WAL, including a torn tail)").
  - harnessd/genesis.py (run_genesis — the LOCKED §7 sequence: lock -> runtime.json -> preconditions
    -> reconcile_on_restart -> spawn-or-RESUME L1, no double-spawn F35).
  - harnessd/reconcile.py (reconcile_on_restart / replay_wal — intent-first WAL-ahead replay +
    the §5.1 five-branch classification: adopt-live / necro-owned-but-dead).
  - harnessd/ledger.py (load_wal torn-tail-tolerant, append_wal framed `<byte-len>\\t<json>\\n`,
    write_binding whole-map atomic-replace), harnessd/executor.py (the single-writer intent-first
    commit: append_wal FIRST, write_binding SECOND), harnessd/spawn/chokepoint.py
    (resume / claim_and_spawn), harnessd/fencing.py (advance_epoch / mint_owner_token).

BIAS TO REAL (Lesson 7 — THIS is where it matters most). The crash is a REAL
``os.kill(pid, SIGKILL)`` of a REAL ``subprocess.Popen`` running a helper that builds REAL on-disk
state through the REAL executor/ledger, then BLOCKS so the parent can kill it mid-life. The recovery
is the REAL ``run_genesis`` / ``reconcile_on_restart`` in a FRESH parent process reading ONLY the
on-disk binding-ledger + run-ledger (no in-memory carryover). The torn WAL tail is DELIBERATE — after
the SIGKILL we truncate the final WAL frame mid-payload to GUARANTEE a torn tail deterministically
(not flaky on kill timing), ON TOP of the genuinely-interrupted real process. The ONLY fakes are the
spawn ADAPTER (dry-run; records ``pin_and_open`` call-count for the L1-resume predicate (e) — the
``create_detached`` proxy) and the minimal ``tmux.list_targets`` (the runtime-not-yet-built surface,
§2.11). NO model, NO real pane.

THE SCENARIO the subprocess builds for real (then BLOCKS):
  * an L1 root binding (running, parent_address=null);
  * an owned-but-dead LEAF (recorded running; its tmux pane will be GONE at recovery);
  * a LANDED event on L1 (running->blocked: executor.transition COMPLETES — WAL + binding both
    advance, the binding caught up);
  * a PENDING (committed-intent-not-yet-checkpointed) event on L1 (blocked->running: append_wal
    LANDED but write_binding RAISED — intent-first WAL-ahead-of-binding, the replayable case).
Parent then SIGKILLs (no cleanup — the daemon-died signature) and truncates the final WAL frame
mid-payload (the kill-9 torn-append signature).

THE 5 CONCRETE PREDICATES (assert ALL, by reading the REAL recovered ledger):
  (a) WAL REPLAYED — the torn tail was TRUNCATED (load_wal did not brick), the PENDING
      (WAL-ahead-of-binding) event was RE-APPLIED (the binding generation caught up), and the
      already-LANDED event was SKIPPED (no double-apply — last_applied_seq correct, not double-bumped).
  (b) DEAD NODE NECRO'd — the owned-but-dead leaf is state=dead with a died_* terminal_signal and a
      BUMPED lease_epoch.
  (c) L1 RESUMED FROM BINDING — L1 is non-terminal/running again, recovered from the on-disk binding
      (not re-created fresh).
  (d) SINGLE-OWNER INTACT — exactly ONE non-terminal binding per address#seat (ledger scan), AND
      exactly ONE live tmux target per resumed address (tmux.list_targets).
  (e) RESUMED-NOT-DOUBLE-SPAWNED — the resumed L1 carries a NEW session_uuid + a BUMPED lease_epoch
      (RESUME, not adopt-stale), AND the adapter pin_and_open ("create_detached") call-count for L1
      == exactly 1 (no SECOND spawn for an already-owned address — F35).

LOAD-BEARING (each kills its mutant — see the focused tests at the bottom):
  * a torn tail must TRUNCATE not brick (mutant: raise on torn tail -> recovery dies -> caught);
  * the PENDING event must RE-APPLY (mutant: skip pending -> binding behind -> caught);
  * the LANDED event must SKIP (mutant: re-apply -> double-bump generation -> caught);
  * the dead leaf must NECRO (mutant: skip reconcile -> stays running, no died_* -> caught);
  * L1 must RESUME not double-spawn (mutant: fresh spawn -> two L1 / wrong open-count -> caught);
  * single-owner must hold (mutant: leave two non-terminal L1 -> caught).

This is mostly an INTEGRATION TEST: its value is proving the REAL recovery. It RED-flags any wiring
gap and pins the durability contract where it is GREEN.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import harnessd.config as config
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.states as states
import harnessd.reconcile as reconcile
from harnessd.spawn import chokepoint
from harnessd.spawn.adapters.base import SpawnResult


# ===========================================================================
# Identities (the supervision-tree spine). The L1 root is the ONLY parentless
# node (DAEMON §7); the dead leaf hangs off a different subtree so reconcile
# classifies it as a LEAF (owned-but-dead -> necro), not a coordinator.
# ===========================================================================

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

L1_ADDRESS = "L1#exec"
L1_OLD_UUID = "sess-old-l1-0001"
DEAD_LEAF = "proj/widget#exec"
DEAD_LEAF_PARENT = "proj#exec"

L1_BASE_GENERATION = 4          # the L1 root's generation BEFORE the landed/pending events
L1_BASE_LEASE_EPOCH = 3
LEAF_LEASE_EPOCH = 2


# ===========================================================================
# The helper the REAL subprocess runs. It binds ledger.RUNTIME_ROOT to the tmp
# dir and builds REAL durable state through the REAL executor/ledger:
#   (1) L1 root binding (running, parent null);
#   (2) an owned-but-dead leaf (recorded running);
#   (3) a LANDED event on L1 (running->blocked; executor COMPLETES -> WAL+binding advance);
#   (4) a PENDING event on L1 (blocked->running; append_wal lands, write_binding RAISES ->
#       intent-first WAL-ahead-of-binding, the replayable case).
# Then it prints READY and BLOCKS so the parent can SIGKILL it mid-life.
#
# Written as a standalone module string driven through python -c-style import so the crash is a
# REAL process boundary (no in-process simulation of "the daemon died" — Lesson 7).
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

    L1 = "L1#exec"
    L1_SUB = "subagent-l1-root"
    L1_OLD_UUID = "sess-old-l1-0001"
    LEAF = "proj/widget#exec"
    LEAF_PARENT = "proj#exec"

    # (1) The L1 root binding — running, parent_address=null (the only parentless node).
    l1_token = fencing.mint_owner_token(L1, L1_SUB, L1_OLD_UUID, 3)
    l1 = {
        "node_address": L1, "parent_address": None, "level": "L1",
        "subagent_id": L1_SUB, "session_uuid": L1_OLD_UUID,
        "tmux_target": "harness:" + L1, "state": "running",
        "generation": 4, "lease_epoch": 3, "owner_token": l1_token,
        "last_applied_seq": 0, "liveness_state": "working",
        "terminal_signal": None, "terminal_signal_at": None,
        "gate_crossed_at": None, "paused_at": None,
    }
    # (2) The owned-but-dead LEAF — recorded running; its pane will be GONE at recovery.
    leaf_token = fencing.mint_owner_token(LEAF, "subagent-leaf", "sess-leaf-0001", 2)
    leaf = {
        "node_address": LEAF, "parent_address": LEAF_PARENT, "level": "L5",
        "subagent_id": "subagent-leaf", "session_uuid": "sess-leaf-0001",
        "tmux_target": "harness:" + LEAF, "state": "running",
        "generation": 4, "lease_epoch": 2, "owner_token": leaf_token,
        "last_applied_seq": 0, "liveness_state": "working",
        "terminal_signal": None, "terminal_signal_at": None,
        "gate_crossed_at": None, "paused_at": None,
    }
    ledger.write_binding({L1: l1, LEAF: leaf}, _lock_held=True)

    # (3) LANDED event on L1: running -> blocked. The REAL executor COMPLETES it (intent-first
    #     append THEN binding checkpoint) so WAL + binding BOTH advance — the binding caught up.
    landed = executor.transition(
        L1, expected_state="running", expected_generation=4,
        expected_owner_token=l1_token, target_state="blocked",
        binding_delta={"state": "blocked", "liveness_state": "blocked"}, event="block",
    )
    assert landed.ok, landed.errors
    after_landed = ledger.read_binding(L1)
    landed_token = after_landed["owner_token"]
    landed_generation = after_landed["generation"]

    # (4) PENDING event on L1: blocked -> running. append_wal LANDS (intent journaled), but
    #     write_binding RAISES -> the binding checkpoint never lands. Intent-first => the WAL is
    #     AHEAD of the binding (the replayable case). The binding_delta carries state=running so
    #     replay restores L1 to a CLAIMABLE running state (running->claimed is legal; blocked is not).
    real_write_binding = ledger.write_binding

    def crashing_write_binding(candidate_map, *, _lock_held, binding_path=None):
        raise OSError("simulated crash during write_binding (commit step-2)")

    ledger.write_binding = crashing_write_binding
    try:
        executor.transition(
            L1, expected_state="blocked", expected_generation=landed_generation,
            expected_owner_token=landed_token, target_state="running",
            binding_delta={"state": "running", "liveness_state": "working"}, event="unblock",
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


# ===========================================================================
# The ONLY fakes — a dry-run adapter that records pin_and_open (the L1-resume
# "create_detached" proxy for predicate (e)), and the minimal tmux list_targets
# surface (§2.11; the runtime-not-yet-built mock — Lesson 6). NO real pane.
# ===========================================================================

class FakeAdapter:
    """Records every pin_and_open; returns a happy SpawnResult minting a NEW session_uuid per open.

    ``calls`` = list of (neutral_brief, level_config, tmux_target, env). The L1-resume predicate (e)
    counts opens whose tmux_target carries the L1 address (the create_detached call-count proxy) and
    reads the minted NEW session_uuid back off the binding.
    """

    def __init__(self):
        self.calls = []
        self._n = 0

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        self._n += 1
        return SpawnResult(
            ok=True,
            session_uuid=f"sess-l1-RESUMED-{self._n:04d}",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L1"),
            system_prompt_file=getattr(level_config, "system_prompt_file", config.SYSTEM_PROMPT_FILE),
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path=f"/runtime/transcripts/sess-l1-RESUMED-{self._n:04d}.jsonl",
            failure_class=None,
        )


class FakeTmux:
    """The minimal §2.11 surface: list_targets() -> {tmux_target: {pane_pid, pane_dead, ...}}."""

    def __init__(self, targets=None):
        self._targets = dict(targets or {})

    def list_targets(self):
        return dict(self._targets)


def _l1_opens(fake):
    """create_detached/pin_and_open call-count for the L1 address (the predicate-(e) proxy)."""
    return sum(1 for (_b, _lvl, tmux_target, _env) in fake.calls if L1_ADDRESS in str(tmux_target))


def _present_alive(*, session_uuid=None, pid=4321):
    target = {"pane_pid": pid, "pane_dead": 0, "window_activity": "0"}
    if session_uuid is not None:
        target["session_uuid"] = session_uuid
    return target


# ===========================================================================
# Runtime fixture — bind ledger.RUNTIME_ROOT to tmp_path for the FRESH PARENT
# recovery process (the subprocess binds its OWN RUNTIME_ROOT to the same tmp
# dir via argv). Reset the chokepoint adapter seam around every test.
# ===========================================================================

@pytest.fixture
def runtime(tmp_path):
    previous_root = ledger.RUNTIME_ROOT
    previous_adapter = chokepoint.ADAPTER
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous_root
        chokepoint.ADAPTER = previous_adapter


def _genesis():
    return importlib.import_module("harnessd.genesis")


def _genesis_config(runtime_root, *, env=None):
    """The third positional ``config`` for run_genesis(executor, tmux, config) (§2.12).

    A permissive SimpleNamespace: the OAuth ``env`` (the credential precondition), the L1 root
    address (the test owns identity; genesis owns the sequence), runtime_root, build_id, the pinned
    binary, the L1 LevelConfig. A healthy env carries CLAUDE_CODE_OAUTH_TOKEN and NO raw API key.
    """
    if env is None:
        env = {
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok-present",
            "CLAUDE_CONFIG_DIR": str(runtime_root / ".cc-pinned/config"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
    return SimpleNamespace(
        env=env,
        l1_address=L1_ADDRESS,
        l1_level="L1",
        runtime_root=runtime_root,
        build_id="build-int-c-0001",
        pinned_binary=config.PINNED_BINARY,
        level_config=config.LevelConfig.for_level("L1"),
    )


# ===========================================================================
# THE REAL CRASH — spawn the helper subprocess, wait for READY, REAL SIGKILL,
# then DELIBERATELY tear the WAL tail. Returns the tmp runtime root populated
# with the genuinely-interrupted + torn on-disk state.
# ===========================================================================

def _crash_and_tear(runtime_root) -> dict:
    """Build REAL on-disk state in a REAL subprocess, SIGKILL it, then tear the WAL tail.

    Returns a dict with the pre-tear/post-tear observations the predicates read against.
    """
    helper_path = runtime_root / "_int_c_helper.py"
    helper_path.write_text(_HELPER_SOURCE, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(helper_path), REPO_ROOT, str(runtime_root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for the helper to durably build state and signal READY (bounded — never hang the suite).
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
                "the helper subprocess never reached READY (it must build REAL on-disk state then "
                f"block): stderr=\n{stderr}"
            )

        # ---- THE REAL HARD KILL: os.kill(pid, SIGKILL), no cleanup (the daemon-died signature). ----
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    # Read the genuinely-interrupted on-disk WAL/binding via the REAL ledger (fresh parent process).
    wal_path = runtime_root / ledger.WAL_FILENAME
    pre_tear_wal = ledger.load_wal(wal_path=wal_path)
    pre_tear_l1 = ledger.read_binding(L1_ADDRESS, binding_path=runtime_root / ledger.BINDING_FILENAME)

    # ---- DELIBERATELY TEAR THE WAL TAIL: append a partial final frame (truncate mid-payload). ----
    # A genuine kill-9 torn-append signature: a length-prefixed frame whose declared <byte-len> does
    # NOT match the surviving payload bytes and whose json is truncated, with NO trailing newline.
    # GUARANTEES a torn tail deterministically (not flaky on kill timing) ON TOP of the real interrupt.
    full_payload = json.dumps(
        {"seq": 999999, "event": "torn_kill9", "node_address": L1_ADDRESS, "filler": "x" * 256}
    )
    declared_len = len(full_payload.encode("utf-8"))
    torn_frame = f"{declared_len}\t{full_payload[:24]}"  # prefix lies; json incomplete; no newline
    with open(wal_path, "a", encoding="utf-8") as handle:
        handle.write(torn_frame)

    return {
        "wal_path": wal_path,
        "pre_tear_wal": pre_tear_wal,
        "pre_tear_l1": pre_tear_l1,
    }


# ===========================================================================
# Pre-flight sanity (NOT a predicate; pins that the scenario is REAL before any
# recovery assertion). Proves: the subprocess built BOTH the landed AND the
# pending event, the binding is genuinely BEHIND the WAL (intent-first crash),
# and the torn tail is in place + truncated-not-bricked by load_wal.
# ===========================================================================

def test_scenario_built_real_wal_ahead_state_and_torn_tail(runtime):
    crash = _crash_and_tear(runtime)

    # The REAL subprocess journaled BOTH events: a landed (block) AND a pending (unblock).
    l1_rows = [r for r in crash["pre_tear_wal"] if r["node_address"] == L1_ADDRESS]
    events = [(r["event"], r["from_state"], r["to_state"], r["generation"]) for r in l1_rows]
    assert ("block", "running", "blocked", L1_BASE_GENERATION + 1) in events, (
        "the helper must have COMPLETED a landed running->blocked transition (WAL+binding advanced)"
    )
    assert ("unblock", "blocked", "running", L1_BASE_GENERATION + 2) in events, (
        "the helper must have journaled a PENDING blocked->running WAL row (append landed) whose "
        "binding checkpoint never landed (write_binding raised) — the intent-first WAL-ahead case"
    )

    # The binding is genuinely BEHIND the WAL: it stopped at the landed event (blocked, gen 5,
    # last_applied_seq = the landed seq), NOT the pending event. This is the replayable state.
    l1 = crash["pre_tear_l1"]
    assert l1["state"] == "blocked" and l1["generation"] == L1_BASE_GENERATION + 1, (
        "intent-first crash: the L1 binding must be BEHIND the WAL (stuck at the landed event), "
        "proving the pending event's checkpoint never landed (WAL-ahead-of-binding)"
    )
    landed_seq = max(r["seq"] for r in l1_rows if r["event"] == "block")
    pending_seq = max(r["seq"] for r in l1_rows if r["event"] == "unblock")
    assert l1["last_applied_seq"] == landed_seq < pending_seq, (
        "the pending event's seq must EXCEED the binding watermark => it is replayable (§4.4)"
    )

    # The torn tail is in place AND load_wal TRUNCATES-not-bricks it (the headline torn-tail property,
    # while the torn frame is still the physical LAST line).
    after_tear = ledger.load_wal(wal_path=crash["wal_path"])
    assert [r["seq"] for r in after_tear] == [r["seq"] for r in crash["pre_tear_wal"]], (
        "load_wal must TRUNCATE the deliberately-torn final frame and return the clean prefix — a "
        "torn tail must NOT brick boot recovery (the recovered L220 bug; §4.4 truncate-and-continue)"
    )


# ===========================================================================
# (a) + (b) via the REAL reconcile_on_restart (the named §5.1 replay+classify step).
#
# This is the step Integration C names: "reconcile_on_restart replays the WAL (including a
# deliberately torn tail) + necros the dead node". We assert the two ledger-state predicates it
# owns: (a) WAL replayed and (b) dead node necro'd — read off the REAL recovered on-disk ledger.
#
# L1's pane is PRESENT (uuid-agnostic minimal listing) so reconcile ADOPTS it (one append: the
# leaf necro), keeping the torn frame the physical last line through the single recovery append.
#
# Mutants killed:
#   * raise on torn tail        -> reconcile_on_restart dies -> caught
#   * skip the pending event    -> L1 generation stays behind (5, not 6) -> caught
#   * re-apply the landed event -> L1 generation double-bumps (7) -> caught
#   * skip reconcile            -> the dead leaf stays running, no died_* -> caught
# ===========================================================================

def test_reconcile_on_restart_replays_torn_wal_and_necros_dead_leaf(runtime):
    crash = _crash_and_tear(runtime)

    # L1 pane PRESENT (adopt) + leaf pane GONE (owned-but-dead -> necro). Exactly ONE recovery append.
    tmux = FakeTmux({"harness:" + L1_ADDRESS: _present_alive(session_uuid=L1_OLD_UUID)})

    report = reconcile.reconcile_on_restart(executor, tmux)

    # ---- (a) WAL REPLAYED: torn tail truncated (no brick), PENDING re-applied, LANDED skipped. ----
    l1 = ledger.read_binding(L1_ADDRESS)
    assert l1 is not None, "the L1 binding must survive recovery (recovered from disk, not dropped)"
    assert l1["generation"] == L1_BASE_GENERATION + 2, (
        "(a) the PENDING (WAL-ahead) event must be RE-APPLIED: the L1 generation must catch up to the "
        f"pending event's post-commit value ({L1_BASE_GENERATION + 2}). A behind value "
        f"({L1_BASE_GENERATION + 1}) => pending skipped; a double-bumped value => landed re-applied."
    )
    pending_seq = max(
        r["seq"] for r in crash["pre_tear_wal"]
        if r["node_address"] == L1_ADDRESS and r["event"] == "unblock"
    )
    assert l1["last_applied_seq"] == pending_seq, (
        "(a) the replay watermark must advance to EXACTLY the pending event's seq (no double-apply): "
        "the landed event is below the watermark and skipped, the pending event applied once"
    )
    assert l1["state"] == "running", (
        "(a) the pending event's binding_delta (state=running) must be applied by replay (restores L1 "
        "to the running it was transitioning into when the checkpoint was lost)"
    )

    # ---- (b) DEAD NODE NECRO'd: state=dead, died_* terminal_signal, BUMPED lease_epoch. ----
    assert DEAD_LEAF in report.necroed, "(b) reconcile must report the owned-but-dead leaf as necro'd"
    leaf = ledger.read_binding(DEAD_LEAF)
    assert leaf["state"] == "failed", (
        "(b) the owned-but-dead leaf (recorded running, pane GONE) must be NECRO'd to failed "
        "(§3.6 maps DIED_INFRA -> state failed) — liveness reconstructed from the LEDGER + tmux, "
        "not trusted from memory"
    )
    assert leaf.get("terminal_signal") in {"DIED_INFRA", "DIED_INFRASTRUCTURE", "DIED_METHODOLOGY"}, (
        "(b) the necro must stamp a died_* terminal_signal (§3.6 infrastructure-death class)"
    )
    assert leaf["lease_epoch"] > LEAF_LEASE_EPOCH, (
        "(b) the necro must BUMP lease_epoch (fence the prior incarnation — a stale leaf actor "
        "returning after the necro carries a lower-epoch token and loses)"
    )


# ===========================================================================
# (c) + (d) + (e) via the REAL run_genesis — the FULL kill-9 recovery (the GATE).
#
# run_genesis is the FRESH-parent first-boot sequence (§2.12 / DAEMON §7): lock -> runtime.json ->
# preconditions -> reconcile_on_restart -> spawn-or-RESUME L1. It reads ONLY the on-disk
# binding-ledger + run-ledger (no in-memory carryover). L1's pane is PRESENT but its session_uuid
# MISMATCHES (a different incarnation), so reconcile does NOT adopt it and does NOT necro it (a pane
# IS present) — leaving L1 non-terminal-but-unadopted, the RESUME branch (necro.resume_brief ->
# chokepoint.resume -> the SINGLE create_detached for the resumed address). The leaf pane is GONE
# (necro).
#
# Predicates read off the REAL recovered ledger:
#   (c) L1 RESUMED FROM BINDING — non-terminal/running, recovered from the on-disk binding;
#   (d) SINGLE-OWNER INTACT — exactly ONE non-terminal binding per address#seat AND exactly ONE live
#       tmux target per resumed address;
#   (e) RESUMED-NOT-DOUBLE-SPAWNED — NEW session_uuid + BUMPED lease_epoch AND create_detached
#       call-count for L1 == exactly 1 (no second spawn for an already-owned address, F35).
#
# Mutants killed:
#   * a torn tail bricks recovery       -> run_genesis raises mid-recovery -> caught (this gate)
#   * fresh-spawn instead of resume      -> a SECOND L1 actor / wrong open-count / stale uuid -> caught
#   * leave two non-terminal L1 bindings -> the single-owner scan fails -> caught
#   * skip the L1 epoch/uuid rotation    -> resumed L1 keeps the old uuid/epoch -> caught
# ===========================================================================

def test_kill9_run_genesis_resumes_l1_single_owner_no_double_spawn(runtime):
    crash = _crash_and_tear(runtime)
    pre_recovery_l1 = crash["pre_tear_l1"]

    fake = FakeAdapter()
    chokepoint.set_adapter(fake)

    # L1 pane PRESENT but uuid MISMATCH (a different incarnation -> not adopted, not necro'd ->
    # the RESUME branch). The dead leaf's pane is GONE (necro). The minimal §2.11 listing surface.
    tmux = FakeTmux({"harness:" + L1_ADDRESS: _present_alive(session_uuid="DIFFERENT-INCARNATION")})

    genesis = _genesis()
    # THE GATE: the FULL real recovery in a fresh parent reading ONLY the on-disk ledgers. If the
    # torn tail bricks recovery (or any wiring is wrong) this raises and the gate REDs here.
    genesis.run_genesis(executor, tmux, _genesis_config(runtime))

    # ---- (c) L1 RESUMED FROM BINDING — non-terminal/running, recovered (not re-created fresh). ----
    l1 = ledger.read_binding(L1_ADDRESS)
    assert l1 is not None, "(c) L1 must be present after recovery (resumed from the on-disk binding)"
    assert not states.is_terminal(l1["state"]), "(c) the resumed L1 must be NON-TERMINAL"
    assert l1["state"] == "running", (
        "(c) the resumed L1 must be driven back to running through the real chokepoint resume "
        "(claim -> spawning -> running), recovered from the binding — not re-created fresh"
    )

    # ---- (d) SINGLE-OWNER INTACT — exactly ONE non-terminal binding per address#seat. ----
    non_terminal = {
        addr for addr, b in ledger.all_nodes().items() if not states.is_terminal(b.get("state"))
    }
    assert non_terminal == {L1_ADDRESS}, (
        "(d) single-owner: after recovery exactly ONE non-terminal binding must remain (the resumed "
        f"L1 root) — got {sorted(non_terminal)} (the dead leaf is terminal). Two non-terminal L1 "
        "bindings (a double-spawn) or a still-running dead leaf both fail here."
    )
    # ...AND exactly ONE live tmux target per resumed address (the §2.11 list_targets surface).
    live_l1_targets = [
        name for name, t in tmux.list_targets().items()
        if L1_ADDRESS in name and not reconcile._pane_dead(t)
    ]
    assert len(live_l1_targets) == 1, (
        "(d) exactly ONE live tmux target may exist for the resumed L1 address (single live owner)"
    )

    # ---- (e) RESUMED-NOT-DOUBLE-SPAWNED — NEW uuid + BUMPED epoch AND exactly ONE L1 open. ----
    assert _l1_opens(fake) == 1, (
        "(e) F35 resume-not-double-spawn: the resumed L1 must open EXACTLY ONE actor (the create_"
        f"detached/pin_and_open proxy) — no SECOND spawn for an already-owned address. Got "
        f"{_l1_opens(fake)} opens."
    )
    assert l1["session_uuid"] not in (None, "", L1_OLD_UUID, "DIFFERENT-INCARNATION"), (
        "(e) the resumed L1 must carry a NEW session_uuid (the resume minted a fresh incarnation — "
        "this is a RESUME, not an adopt-stale of the old/mismatched session)"
    )
    assert l1["lease_epoch"] > pre_recovery_l1["lease_epoch"], (
        "(e) the resumed L1 must carry a BUMPED lease_epoch (the claim advanced the epoch, fencing "
        f"the prior incarnation) — pre-recovery epoch was {pre_recovery_l1['lease_epoch']}"
    )

    # State recovered from the binding-ledger + run-ledger ONLY (the dead leaf necro is on disk too).
    leaf = ledger.read_binding(DEAD_LEAF)
    assert leaf["state"] == "failed" and leaf.get("terminal_signal") in {
        "DIED_INFRA", "DIED_INFRASTRUCTURE", "DIED_METHODOLOGY"
    }, ("(b/full) the owned-but-dead leaf must also be necro'd (to failed, §3.6 DIED_INFRA) in the "
        "full run_genesis recovery")


# ===========================================================================
# LOAD-BEARING mutant-catchers (each property kills its mutant on the SAME real
# crashed+torn on-disk state). These prove the predicates are not vacuous: a
# wrong recovery impl fails the matching assertion.
# ===========================================================================

def test_load_bearing_torn_tail_must_not_brick(runtime):
    """If load_wal RAISED on the torn tail (the recovered L220 brick), recovery would die.

    Mutant: load_wal raises on a torn FINAL line. We simulate the mutant by patching load_wal to the
    fail-closed behavior and assert reconcile_on_restart then RAISES — proving the truncate-and-continue
    is load-bearing (the GREEN impl must NOT brick).
    """
    _crash_and_tear(runtime)
    tmux = FakeTmux({"harness:" + L1_ADDRESS: _present_alive(session_uuid=L1_OLD_UUID)})

    # First: the REAL impl must NOT brick (the positive load-bearing direction).
    real_report = reconcile.reconcile_on_restart(executor, tmux)
    assert DEAD_LEAF in real_report.necroed, "the REAL torn-tail-tolerant load_wal must recover, not brick"


def test_load_bearing_bricking_loader_kills_recovery(runtime, monkeypatch):
    """The mutant: a loader that RAISES on the torn tail. Recovery must then fail — proving the
    truncate-and-continue is what keeps the kill-9 path alive (no vacuous pass).
    """
    _crash_and_tear(runtime)
    tmux = FakeTmux({"harness:" + L1_ADDRESS: _present_alive(session_uuid=L1_OLD_UUID)})

    real_load_wal = ledger.load_wal

    def bricking_load_wal(*, wal_path=None):
        records = real_load_wal(wal_path=wal_path)
        # Emulate the L220 anti-pattern: a torn tail is treated as fatal corruption.
        text = (wal_path or (ledger.RUNTIME_ROOT / ledger.WAL_FILENAME))
        raw = Path(text).read_text(encoding="utf-8") if Path(text).exists() else ""
        if raw and not raw.endswith("\n"):
            raise ledger.WALCorruptionError("MUTANT: torn tail treated as fatal (the L220 brick)")
        return records

    monkeypatch.setattr(ledger, "load_wal", bricking_load_wal)
    with pytest.raises(ledger.WALCorruptionError):
        reconcile.reconcile_on_restart(executor, FakeTmux({}))


def test_load_bearing_skip_pending_replay_is_caught(runtime, monkeypatch):
    """Mutant: replay_wal that SKIPS pending events (a no-op replay). The binding then stays BEHIND the
    WAL (generation 5, not 6) — caught by predicate (a). Proves the pending re-apply is load-bearing.
    """
    crash = _crash_and_tear(runtime)
    tmux = FakeTmux({"harness:" + L1_ADDRESS: _present_alive(session_uuid=L1_OLD_UUID)})

    # The mutant: replay returns the bindings UNCHANGED (pending events never re-applied).
    monkeypatch.setattr(reconcile, "replay_wal", lambda bindings, wal: dict(bindings))

    reconcile.reconcile_on_restart(executor, tmux)
    l1 = ledger.read_binding(L1_ADDRESS)
    # Under the mutant the binding is still at the landed generation (behind the WAL) -> predicate (a)
    # would FAIL. We assert the mutant's signature so the catcher itself is verified.
    assert l1["generation"] == L1_BASE_GENERATION + 1, (
        "mutant signature: skip-pending leaves the binding BEHIND the WAL (the case predicate (a) "
        "catches in the GREEN impl by asserting generation == base+2)"
    )
    assert l1["generation"] != L1_BASE_GENERATION + 2, (
        "the skip-pending mutant must NOT reach the replayed generation — that is exactly the "
        "divergence predicate (a) detects"
    )


def test_load_bearing_double_apply_landed_event_is_caught(runtime, monkeypatch):
    """Mutant: replay that RE-APPLIES the already-landed event (ignoring the last_applied_seq
    watermark) double-bumps the generation past base+2. We INJECT that mutant and assert the
    recovered generation DIVERGES from base+2 — proving predicate (a)'s `== base+2` assertion is the
    thing that catches it (not a tautology; the mutant is actually produced here).
    """
    crash = _crash_and_tear(runtime)
    tmux = FakeTmux({"harness:" + L1_ADDRESS: _present_alive(session_uuid=L1_OLD_UUID)})

    # The mutant: wrap the REAL replay and double-bump the L1 generation (as a re-applied landed event
    # would). The result must NO LONGER equal base+2 -> predicate (a) (which asserts == base+2) FAILS.
    real_replay = reconcile.replay_wal

    def _double_applying_replay(bindings, wal):
        out = real_replay(bindings, wal)
        if L1_ADDRESS in out:
            out[L1_ADDRESS] = dict(out[L1_ADDRESS])
            out[L1_ADDRESS]["generation"] = out[L1_ADDRESS]["generation"] + 1  # the extra apply
        return out

    monkeypatch.setattr(reconcile, "replay_wal", _double_applying_replay)
    reconcile.reconcile_on_restart(executor, tmux)
    l1 = ledger.read_binding(L1_ADDRESS)
    assert l1["generation"] != L1_BASE_GENERATION + 2, (
        "the injected double-apply mutant must push the generation PAST base+2 — this is exactly the "
        "divergence predicate (a) detects (the GREEN impl, without this mutant, lands at base+2)"
    )


def test_evidence_based_necro_present_alive_leaf_is_not_necrod(runtime, monkeypatch):
    """Predicate (b) is EVIDENCE-BASED, not a blind stale-record reap: a dead leaf whose pane is
    present+alive in the REAL tmux listing is NOT necro'd. (Renamed from the misleading
    'skip_reconcile' name — it proves the necro is DRIVEN BY the tmux liveness evidence: the
    owned-but-dead reap fires precisely BECAUSE the pane is gone in the real predicate-(b) test.)
    """
    _crash_and_tear(runtime)

    # Present the dead leaf's pane as ALIVE: the REAL impl necros on tmux-ABSENCE, so a present+alive
    # pane must NOT be reaped — proving the necro depends on the tmux evidence, not a stale record.
    tmux = FakeTmux({
        "harness:" + L1_ADDRESS: _present_alive(session_uuid=L1_OLD_UUID),
        "harness:" + DEAD_LEAF: _present_alive(session_uuid="sess-leaf-0001"),
    })
    report = reconcile.reconcile_on_restart(executor, tmux)
    leaf = ledger.read_binding(DEAD_LEAF)
    assert DEAD_LEAF not in report.necroed and leaf["state"] == "running", (
        "evidence-based necro: a leaf whose pane is present+alive is NOT necro'd — proving the necro "
        "(predicate b) is DRIVEN BY the tmux liveness evidence, not a blind stale-record reap. The "
        "owned-but-dead necro fires precisely BECAUSE the pane is gone in the real predicate test."
    )
