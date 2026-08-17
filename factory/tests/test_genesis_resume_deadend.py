"""F21 — genesis RESUME-branch dead-end guard (review CFW-02).

Root cause: genesis STEP 5 treated "non-terminal and not adopted" as "resumable" and handed the L1
root to necro.resume_brief -> chokepoint.resume -> executor.claim(expected_state=<live state>). The
legality table (states.ALLOWED_TRANSITIONS) admits ->claimed only from {planned, running, dead}, so a
post-reconcile L1 in ``claimed``/``spawning``/``blocked`` produced an illegal-transition CAS abort ->
SpawnResult(ok=False, failure_class='claim_lost') -> genesis DISCARDED it and returned None: the
daemon reported a clean boot with NO L1 actor — the cascade root dead-ended silently. A ``planned``
survivor was a second flavor: planned->claimed is legal, but resume then attached
``--resume genesis-l1-root`` (the registration PLACEHOLDER session_uuid) — a nonsense continuation.

Reachability (the real path these tests drive): reconcile_on_restart adopts a live+uuid-MATCHED pane
and necros an ABSENT/dead pane; a present-but-uuid-MISMATCHED pane is neither (a different
incarnation, left untouched) — so an intermediate-state binding survives reconcile non-terminal and
unadopted, landing exactly on the old dead-end.

The fix (four-way routing, this suite pins each leg):
  * adopted                      -> done (unchanged; test_genesis pins the ordering);
  * running                      -> RESUME with the SpawnResult ROUTED (GenesisError on not-ok —
                                    the CFW-02 headline, mirroring the F2a spawn-branch surface);
  * planned survivor             -> claim_and_spawn the SURVIVING slot (never --resume the
                                    placeholder; never re-register = never reset lease_epoch);
  * claimed/spawning/blocked     -> reap to terminal via reconcile's single _terminal_necro write
                                    (DIED_INFRA -> state 'failed' per §3.6, post-F5 vocab), result
                                    routed, then fall through to the fresh SPAWN.

Style: real ledger/executor/reconcile/chokepoint on a tmp RUNTIME_ROOT; the ONLY fakes are the
RuntimeAdapter and the minimal tmux list_targets surface (the test_genesis/_spawn_surface pattern).
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import harnessd.config as config
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.genesis as genesis
import harnessd.ledger as ledger
import harnessd.states as states
from harnessd.spawn import chokepoint
from harnessd.spawn import oauth_guard


L1 = "L1#exec"
PRIOR_UUID = "sess-prior-0001"
OTHER_UUID = "sess-OTHER-incarnation"  # the uuid-MISMATCH leftover (not adopted, not necro'd)


# ---------------------------------------------------------------------------
# Fixtures + fakes (mirror tests/test_genesis.py).
# ---------------------------------------------------------------------------

@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


@pytest.fixture(autouse=True)
def _reset_chokepoint_adapter():
    previous = chokepoint.ADAPTER
    try:
        yield
    finally:
        chokepoint.ADAPTER = previous


class FakeAdapter:
    """Records pin_and_open calls; returns a happy SpawnResult with a FRESH session_uuid per open."""

    def __init__(self):
        self.calls = []
        self._n = 0

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        self._n += 1
        from harnessd.spawn.adapters.base import SpawnResult

        return SpawnResult(
            ok=True,
            session_uuid=f"sess-l1-fresh-{self._n:04d}",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L1"),
            system_prompt_file=getattr(level_config, "system_prompt_file", config.SYSTEM_PROMPT_FILE),
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path=f"/runtime/transcripts/sess-l1-fresh-{self._n:04d}.jsonl",
            failure_class=None,
        )


class FailingAdapter:
    """pin_and_open raises a model_unavailable-class SpawnFailure — a POST-claim resume failure,
    so chokepoint.resume returns ok=False AFTER a successful re-adopt claim (the routed case)."""

    def pin_and_open(self, *a, **k):
        raise oauth_guard.SpawnFailure("model unavailable at L1 resume")


class FakeTmux:
    def __init__(self, targets=None):
        self._targets = dict(targets or {})

    def list_targets(self):
        return dict(self._targets)


def _target_alive(session_uuid=None, pid=4321):
    t = {"pane_pid": pid, "pane_dead": 0, "window_activity": "0"}
    if session_uuid is not None:
        t["session_uuid"] = session_uuid
    return t


def _install(adapter):
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(adapter)
    else:
        chokepoint.ADAPTER = adapter


def _binding(node_address, *, state, session_uuid, lease_epoch, generation=4, extra=None):
    subagent = "subagent-" + node_address.replace("/", "-").replace("#", "-")
    token = fencing.mint_owner_token(node_address, subagent, session_uuid, lease_epoch)
    b = {
        "node_address": node_address,
        "parent_address": None,  # the parentless L1 root (DAEMON §7)
        "level": "L1",
        "subagent_id": subagent,
        "session_uuid": session_uuid,
        "tmux_target": "harness:" + node_address,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "working",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
    }
    if extra:
        b.update(extra)
    return b


def _seed(*bindings):
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _config(runtime_root):
    return SimpleNamespace(
        env={
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok-present",
            "CLAUDE_CONFIG_DIR": str(runtime_root / ".cc-pinned/config"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        },
        l1_address=L1,
        l1_level="L1",
        runtime_root=runtime_root,
        build_id="build-f21-0001",
        pinned_binary=config.PINNED_BINARY,
        level_config=config.LevelConfig.for_level("L1"),
    )


def _l1_opens(fake):
    return sum(1 for (_b, _l, tmux_target, _e) in fake.calls if L1 in str(tmux_target))


def _mismatch_tmux():
    """The reachability path: the L1 pane is PRESENT+alive but its uuid MISMATCHES the binding —
    reconcile neither adopts (uuid differs) nor necros (a pane IS present) -> the binding survives
    reconcile non-terminal and unadopted (the uuid-MISMATCH leftover branch)."""
    return FakeTmux({"harness:" + L1: _target_alive(session_uuid=OTHER_UUID)})


# ===========================================================================
# Intermediate states (claimed / spawning / blocked): the old code dead-ended —
# resume's re-adopt claim is ILLEGAL from these states (->claimed admits only
# {planned, running, dead}), the claim_lost result was DISCARDED, and run_genesis
# returned None with ZERO L1 actors (the silent dead boot). The fix REAPS the slot
# to terminal through reconcile's single _terminal_necro write (DIED_INFRA ->
# 'failed', §3.6 post-F5), routes the result, then spawns FRESH.
#
# Mutants killed: the old silent dead boot (zero opens, return None); any raw-ledger
# overwrite that skips the executor (no died_infrastructure WAL row before the spawn).
# ===========================================================================

@pytest.mark.parametrize("intermediate_state", ["claimed", "spawning", "blocked"])
def test_intermediate_state_reaped_then_fresh_l1_spawned(runtime, intermediate_state):
    fake = FakeAdapter()
    _install(fake)
    _seed(_binding(L1, state=intermediate_state, session_uuid=PRIOR_UUID, lease_epoch=3))

    # Must NOT raise and must NOT return a wedged boot.
    genesis.run_genesis(executor, _mismatch_tmux(), _config(runtime))

    # (a) Exactly ONE fresh L1 actor opened (reap-then-spawn, never a dead end, never a double-spawn).
    assert _l1_opens(fake) == 1, (
        f"a post-reconcile L1 in {intermediate_state!r} must be reaped and spawned FRESH — the old "
        "code dead-ended (illegal-transition claim_lost discarded, ZERO opens, silent dead boot; CFW-02)"
    )

    # (b) The root reaches 'running' with a FRESH session_uuid (a new incarnation, not the prior).
    rb = ledger.read_binding(L1)
    assert rb is not None and rb["state"] == "running", (
        "the fresh L1 root must reach 'running' through the real chokepoint after the reap"
    )
    assert rb["session_uuid"] != PRIOR_UUID, (
        "the fresh spawn must record a NEW session_uuid — not the reaped incarnation's"
    )

    # (c) Mutation-verified reap: the old slot was terminal'd THROUGH THE EXECUTOR (the single
    # terminal-necro write) BEFORE the fresh spawn rows — never silently overwritten raw.
    wal = ledger.load_wal()
    reap_rows = [
        i for i, r in enumerate(wal)
        if r["node_address"] == L1 and r["event"] == states.TERMINAL_VOCAB["died_infrastructure"].event
    ]
    assert reap_rows, (
        "the intermediate-state reap must append a died_infrastructure run-ledger row via the "
        "executor (the single-writer terminal write) — a raw overwrite that skips the executor fails here"
    )
    # The reaped slot landed on the §3.6 vocab state for DIED_INFRA ('failed' post-F5) — terminal.
    reaped_to = wal[reap_rows[0]].get("to_state")
    assert reaped_to == states.TERMINAL_VOCAB["died_infrastructure"].state and states.is_terminal(reaped_to), (
        f"the reap must land the §3.6 DIED_INFRA vocab state (terminal), got {reaped_to!r}"
    )
    spawn_rows = [
        i for i, r in enumerate(wal) if r["node_address"] == L1 and r["event"] == "claim"
    ]
    assert spawn_rows and reap_rows[0] < spawn_rows[0], (
        "the reap row must precede the fresh spawn's claim row — the old slot is terminal'd BEFORE "
        "any fresh spawn (F35 no-double-spawn ordering)"
    )


# ===========================================================================
# A PLANNED survivor (an interrupted prior boot left the planned slot): must be
# claimed FRESH through the normal planned->claimed edge — NEVER routed through
# chokepoint.resume (the slot has only the 'genesis-l1-root' registration
# PLACEHOLDER session_uuid; --resume'ing it is a nonsense continuation), and NEVER
# re-registered (a re-register resets lease_epoch to 1, un-fencing a prior
# incarnation).
#
# Mutants killed: routing planned through chokepoint.resume (resume_argv/resume_
# session land in the brief); the epoch-regressing re-register (lease_epoch <= 5).
# ===========================================================================

def test_planned_survivor_claimed_fresh_never_resumed_and_epoch_not_reset(runtime):
    fake = FakeAdapter()
    _install(fake)
    _seed(_binding(L1, state="planned", session_uuid="genesis-l1-root", lease_epoch=5, generation=2))

    genesis.run_genesis(executor, _mismatch_tmux(), _config(runtime))

    # ONE fresh L1 open.
    assert _l1_opens(fake) == 1, "the planned survivor must be spawned fresh — exactly ONE L1 open"

    # The recorded brief carries NO resume continuation: the placeholder session must never be
    # --resume'd (the single construction site, _attach_resume_continuation, stays unexercised).
    brief = fake.calls[0][0]
    assert "resume_argv" not in brief and "resume_session" not in brief, (
        "a planned survivor must go through claim_and_spawn (fresh brief), NEVER chokepoint.resume — "
        f"the brief carried a resume continuation: {brief.get('resume_session')!r}"
    )

    rb = ledger.read_binding(L1)
    assert rb is not None and rb["state"] == "running", "the planned survivor must reach 'running'"

    # FENCING NEVER REGRESSES: the claim bumped the SURVIVING slot's epoch (5 -> >5). A re-register
    # would have RESET lease_epoch to 1, un-fencing the prior incarnation.
    assert rb["lease_epoch"] > 5, (
        f"the claim must bump the SURVIVING planned slot's lease_epoch (>5), got {rb['lease_epoch']!r} "
        "— a re-register reset the epoch (fencing regression)"
    )


# ===========================================================================
# The CFW-02 HEADLINE: a failed RESUME of a running root must RAISE (GenesisError
# naming the failure_class), never return None as a clean boot. The old code
# discarded resume_brief's SpawnResult — the daemon reported a clean boot with no
# L1 actor.
#
# Mutant killed: the discarded resume result (return None on ok=False).
# ===========================================================================

def test_running_resume_failure_raises_genesis_error_not_silent_clean_boot(runtime):
    _install(FailingAdapter())
    _seed(_binding(L1, state="running", session_uuid=PRIOR_UUID, lease_epoch=3))

    with pytest.raises(genesis.GenesisError) as ei:
        genesis.run_genesis(executor, _mismatch_tmux(), _config(runtime))

    msg = str(ei.value)
    assert "model_unavailable" in msg, (
        f"the raised GenesisError must NAME the failure_class (got: {msg!r})"
    )
    assert "L1" in msg, f"the raised GenesisError must name the L1 root (got: {msg!r})"

    # No phantom running root: the failed resume rolled the claim back; the root is NOT 'running'.
    rb = ledger.read_binding(L1)
    assert rb is None or rb.get("state") != "running", (
        "a failed L1 resume must not leave a phantom 'running' root"
    )


# ===========================================================================
# CONTROL: the legitimate RESUME of a running root still works — run_genesis
# returns None, exactly ONE L1 open (the resume's continuation open), no raise.
# Proves the new routing did not break the F35 resume path.
# ===========================================================================

def test_running_resume_success_still_single_actor(runtime):
    fake = FakeAdapter()
    _install(fake)
    _seed(_binding(L1, state="running", session_uuid=PRIOR_UUID, lease_epoch=3))

    assert genesis.run_genesis(executor, _mismatch_tmux(), _config(runtime)) is None

    assert _l1_opens(fake) == 1, (
        "the legitimate resume of a running root must open exactly ONE L1 actor (F35: resume, "
        "not double-spawn)"
    )
    rb = ledger.read_binding(L1)
    assert rb is not None and rb["state"] == "running", "the resumed root must be running"
    assert rb["session_uuid"] != PRIOR_UUID, "the resume must mint a fresh incarnation session_uuid"
    assert rb["lease_epoch"] > 3, "the resume's re-adopt claim must bump the lease_epoch"
