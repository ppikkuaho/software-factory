"""Daemon/state correctness — fencing monotonicity across re-registration (cluster B, part 2).

Pins three fixes:

  * SM-1 (CRITICAL) — re-registration NEVER resets ``lease_epoch`` to 1. The placeholder identity
    is deterministic and ``mint_owner_token`` is a pure composite, so the old reset meant the next
    ``executor.claim`` (epoch 2) re-minted the PRIOR incarnation's byte-identical owner_token: a
    leftover ``.signal.<seat>.json`` (which no production path ever deleted) passed the F19 fence
    and collapsed EVERY respawn at the same address — the only recovery path for a failed child
    was self-killing, and a daemon reboot after a terminal L1 killed each fresh root on the first
    tick. Now the re-register seeds epoch = prior+1 (DAEMON §8 monotonicity holds ACROSS
    incarnations) AND deletes the dead incarnation's seat artifacts (belt-and-braces).

  * SM-2 — re-registration NEVER regresses the replay watermark: ``last_applied_seq`` seeds at
    the current max WAL seq, so boot replay cannot re-apply the dead incarnation's entire chain
    (each old row pre-image-matched the re-registered gen-0 binding) and resurrect its terminal
    state over the fresh planned slot.

  * INT-4(a) — reconcile no longer classifies PRE-SPAWN ``planned`` slots owned-but-dead: their
    tmux_target is the bare-name registration placeholder, and real ``list_targets`` keys are
    always '<session>:<window>.<pane>' triples, so every planned survivor was necro'd on boot —
    making genesis's F21 claim-the-survivor branch unreachable through a real boot and forcing
    the (formerly epoch-resetting) re-register leg.

Style: real ledger/executor/reconcile/chokepoint/genesis on a tmp RUNTIME_ROOT; the only fakes
are the RuntimeAdapter and the tmux list_targets surface.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.detector_signals as detector_signals
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.genesis as genesis
import harnessd.ledger as ledger
import harnessd.reconcile as reconcile
from harnessd.spawn import chokepoint


L1 = "L1#exec"
PARENT = "proj#exec"
CHILD = "proj/widget#exec"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(detector_signals, "_size_cache", {}, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_adapter():
    previous = chokepoint.ADAPTER
    try:
        yield
    finally:
        chokepoint.ADAPTER = previous


class FakeAdapter:
    """Happy-path adapter: fresh session_uuid + a real transcript path per open."""

    def __init__(self):
        self.calls = []
        self._n = 0

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append(tmux_target)
        self._n += 1
        from harnessd.spawn.adapters.base import SpawnResult

        return SpawnResult(
            ok=True,
            session_uuid=f"sess-fresh-{self._n:04d}",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L5"),
            system_prompt_file=getattr(level_config, "system_prompt_file", config.SYSTEM_PROMPT_FILE),
            system_prompt_file_hash="deadbeef",
            tmux_target=f"harness:{self._n}.0",
            transcript_path=f"/runtime/transcripts/sess-fresh-{self._n:04d}.jsonl",
            failure_class=None,
        )


class FakeTmux:
    def __init__(self, targets=None):
        self._targets = dict(targets or {})

    def list_targets(self):
        return dict(self._targets)


def _seed(*bindings):
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _parent_binding(state="running"):
    token = fencing.mint_owner_token(PARENT, "subagent-parent", "sess-parent", 2)
    return {
        "node_address": PARENT, "parent_address": None, "level": "L2",
        "subagent_id": "subagent-parent", "session_uuid": "sess-parent",
        "tmux_target": "harness:p.0", "state": state, "generation": 1, "lease_epoch": 2,
        "owner_token": token, "last_applied_seq": 0, "liveness_state": "working",
        "terminal_signal": None, "terminal_signal_at": None, "gate_crossed_at": None,
        "paused_at": None, "transcript_path": "/dev/null", "workspace": "w",
    }


def _write_signal(node_address, *, signal, owner_token):
    p = addressing.signal_path(node_address, ledger.RUNTIME_ROOT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"signal": "%s", "ts": "2026-06-11T00:00:00+00:00", "owner_token": "%s", "evidence": {}}'
        % (signal, owner_token),
        encoding="utf-8",
    )
    return p


def _spawn_child(level_config=None):
    return chokepoint.register_and_spawn_child(
        PARENT, CHILD,
        child_level_config=level_config or config.LevelConfig.for_level("L5"),
    )


# ===========================================================================
# SM-1 — the unit seed + the end-to-end respawn-after-FAILED leg.
# ===========================================================================

def test_reregister_seed_is_fresh_for_a_new_address(runtime):
    assert chokepoint.reregister_identity_seed("never/seen#exec") == (1, 0)


def test_respawn_after_failed_child_is_not_self_killing(runtime):
    """THE SM-1 reproduction: a FAILED child's leftover fenced .signal must not collapse the
    fresh incarnation. Pre-fix: _register_child reset lease_epoch=1, the new claim re-minted the
    PRIOR claim's byte-identical token (deterministic placeholders, pure composite mint), and the
    leftover FAILED signal passed the fence on the first tick."""
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)
    _seed(_parent_binding())

    # Incarnation 1: register+spawn, then the agent signs off FAILED and the slot collapses.
    first = _spawn_child()
    assert first.ok, "setup: the first child spawn must succeed"
    live1 = ledger.read_binding(CHILD)
    prior_claim_token = live1["owner_token"]
    prior_epoch = live1["lease_epoch"]
    _write_signal(CHILD, signal="FAILED", owner_token=prior_claim_token)
    collapsed = chokepoint.collapse(CHILD, "FAILED", expected_owner_token=prior_claim_token)
    assert collapsed.ok and ledger.read_binding(CHILD)["state"] == "failed"

    # Incarnation 2: the ONLY recovery path for a failed child — re-register + respawn.
    second = _spawn_child()
    assert second.ok, "the respawn must succeed (failed->re-register->claim->spawn)"
    live2 = ledger.read_binding(CHILD)

    # (a) DAEMON §8 monotonicity holds ACROSS incarnations: the new epoch is strictly greater.
    assert live2["lease_epoch"] > prior_epoch, (
        f"re-registration reset the epoch ({prior_epoch} -> {live2['lease_epoch']}) — the fence "
        "regressed (SM-1)"
    )
    # (b) The new claim's token can NEVER equal the prior incarnation's.
    assert live2["owner_token"] != prior_claim_token, (
        "the fresh claim re-minted the PRIOR incarnation's byte-identical owner_token — the "
        "leftover .signal would pass the fence and collapse every respawn (SM-1)"
    )
    # (c) The leftover artifact is GONE (purged at re-register) — and the fence rejects the old
    # token regardless: the fresh node reads NO actionable terminal signal.
    assert detector_signals.read_terminal_signal(live2, live2) is None, (
        "the dead incarnation's FAILED signal still reads as the fresh node's terminal signal"
    )
    assert live2["state"] == "running", "the fresh incarnation must be alive, not collapsed"


def test_purge_is_seat_scoped(runtime):
    """Re-registering the #exec seat must not touch a co-located #review seat's artifacts."""
    review_addr = CHILD.split("#", 1)[0] + "#review"
    review_signal = _write_signal(review_addr, signal="DONE", owner_token="tok-review")
    exec_signal = _write_signal(CHILD, signal="FAILED", owner_token="tok-exec")

    chokepoint.purge_stale_seat_artifacts(CHILD)

    assert not exec_signal.exists(), "the re-registered seat's stale signal must be purged"
    assert review_signal.exists(), "the co-located #review seat's artifact must be untouched"


def test_genesis_reboot_after_terminal_root_does_not_remint_the_prior_token(runtime):
    """The SM-1 L1 leg: a daemon reboot after a terminal root re-registers — the fresh claim's
    token must differ from the prior incarnation's, and the leftover signal must be inert."""
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)

    # The prior incarnation: registered (epoch 1) + claimed (epoch 2) + collapsed FAILED. Seed the
    # terminal binding directly at the exact epoch-2 placeholder identity the old claim minted.
    prior_token = fencing.mint_owner_token(L1, "subagent-l1-root", "genesis-l1-root", 2)
    _seed({
        "node_address": L1, "parent_address": None, "level": "L1",
        "subagent_id": "subagent-l1-root", "session_uuid": "genesis-l1-root",
        "tmux_target": addressing.session_name_for(L1), "state": "failed", "generation": 4,
        "lease_epoch": 2, "owner_token": prior_token, "last_applied_seq": 0,
        "liveness_state": "dead", "terminal_signal": "FAILED",
        "terminal_signal_at": "2026-06-11T00:00:00+00:00", "gate_crossed_at": None,
        "paused_at": None, "transcript_path": None,
    })
    _write_signal(L1, signal="FAILED", owner_token=prior_token)

    cfg = SimpleNamespace(
        env={"CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok", "CLAUDE_CONFIG_DIR": str(runtime),
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"},
        l1_address=L1, l1_level="L1", runtime_root=runtime, build_id="b-sm1",
        pinned_binary=config.PINNED_BINARY, level_config=config.LevelConfig.for_level("L1"),
    )
    genesis.run_genesis(executor, FakeTmux({}), cfg)

    rb = ledger.read_binding(L1)
    assert rb["state"] == "running" and rb["lease_epoch"] > 2, (
        f"the fresh root must run at an epoch ABOVE the prior incarnation's (got "
        f"{rb['lease_epoch']}) — a reset re-mints the prior token (SM-1)"
    )
    assert rb["owner_token"] != prior_token
    assert detector_signals.read_terminal_signal(rb, rb) is None, (
        "the prior incarnation's FAILED signal must be inert against the fresh root — pre-fix it "
        "collapsed every relaunch on the first tick"
    )


# ===========================================================================
# SM-2 — the replay watermark never regresses across re-registration.
# ===========================================================================

def test_reregistered_slot_is_not_resurrected_by_boot_replay(runtime):
    """The SM-2 crash window: re-register lands (binding written) but the daemon dies before the
    new claim checkpoints. Boot replay must NOT re-apply the dead incarnation's chain (every old
    row used to pre-image-match the reset gen-0/watermark-0 binding) — the fresh planned slot
    stays planned, not resurrected to the prior terminal state."""
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)
    _seed(_parent_binding())

    # Incarnation 1: full chain in the WAL (claim / spawn_open / spawn_running / collapse FAILED).
    assert _spawn_child().ok
    live1 = ledger.read_binding(CHILD)
    chokepoint.collapse(CHILD, "FAILED", expected_owner_token=live1["owner_token"])
    assert ledger.read_binding(CHILD)["state"] == "failed"
    terminal_map = ledger.all_nodes()
    terminal_map[CHILD]["messages"] = {
        "history-1": {
            "message_id": "history-1",
            "source": CHILD,
            "target": PARENT,
            "needs_answer": True,
            "question_state": "withdrawn",
        }
    }
    terminal_map[CHILD]["message_last_id"] = "history-1"
    ledger.write_binding(terminal_map, _lock_held=True)

    # The re-register write (the crash happens right after this, before any new claim).
    registered = chokepoint._register_child(
        CHILD, PARENT, config.LevelConfig.for_level("L5"), runtime)
    assert registered["state"] == "planned"
    assert registered["messages"]["history-1"]["question_state"] == "withdrawn"
    max_seq = max(r["seq"] for r in ledger.load_wal())
    assert registered["last_applied_seq"] == max_seq, (
        f"the re-register must seed last_applied_seq at the current max WAL seq ({max_seq}), got "
        f"{registered['last_applied_seq']!r} — a regressed watermark replays the dead chain (SM-2)"
    )

    # Boot replay over the full WAL: the fresh planned slot must be a fixed point.
    replayed = reconcile.replay_wal(ledger.all_nodes(), ledger.load_wal())
    after = replayed[CHILD]
    assert after["state"] == "planned" and after["generation"] == 0, (
        f"boot replay resurrected the dead incarnation over the fresh slot "
        f"(state={after['state']!r}, generation={after['generation']!r}) — SM-2"
    )


# ===========================================================================
# INT-4(a) — planned slots are never owned-but-dead; the F21 survivor branch is
# reachable through a REAL boot (triple-keyed listing, empty server).
# ===========================================================================

def test_planned_slot_is_not_necroed_by_reconcile(runtime):
    """A pre-spawn planned slot (bare-name placeholder target) against a REAL-shaped listing
    (triples only / empty server) must be LEFT for the claim path — not necro'd DIED_INFRA."""
    token = fencing.mint_owner_token(CHILD, "subagent-c", "registered-c", 5)
    _seed({
        "node_address": CHILD, "parent_address": PARENT, "level": "L5",
        "subagent_id": "subagent-c", "session_uuid": "registered-c",
        "tmux_target": addressing.session_name_for(CHILD),  # the bare-name placeholder
        "state": "planned", "generation": 0, "lease_epoch": 5, "owner_token": token,
        "last_applied_seq": 0, "liveness_state": "claimed", "terminal_signal": None,
        "terminal_signal_at": None, "gate_crossed_at": None, "paused_at": None,
        "transcript_path": None,
    })

    report = reconcile.reconcile_on_restart(executor, FakeTmux({}))

    assert CHILD not in report.necroed, (
        "a pre-spawn planned slot owns NO pane — necroing it owned-but-dead made the F21 "
        "claim-the-survivor branch unreachable (INT-4(a))"
    )
    after = ledger.read_binding(CHILD)
    assert after["state"] == "planned" and after["lease_epoch"] == 5, (
        f"the planned survivor must be untouched (state={after['state']!r}, "
        f"epoch={after['lease_epoch']!r})"
    )


def test_planned_survivor_reaches_genesis_claim_as_is_through_a_real_boot(runtime):
    """The F21 branch end-to-end on REAL boot shapes: an interrupted prior boot's planned slot +
    an EMPTY tmux server -> genesis claims the SURVIVING slot (epoch bumped from the survivor's,
    never reset; exactly one open). Pre-fix, reconcile necro'd the slot first and genesis took
    the re-register leg instead."""
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)
    survivor_token = fencing.mint_owner_token(L1, "subagent-l1-root", "genesis-l1-root", 5)
    _seed({
        "node_address": L1, "parent_address": None, "level": "L1",
        "subagent_id": "subagent-l1-root", "session_uuid": "genesis-l1-root",
        "tmux_target": addressing.session_name_for(L1), "state": "planned", "generation": 2,
        "lease_epoch": 5, "owner_token": survivor_token, "last_applied_seq": 0,
        "liveness_state": "claimed", "terminal_signal": None, "terminal_signal_at": None,
        "gate_crossed_at": None, "paused_at": None, "transcript_path": None,
        "workspace": str(addressing.node_dir(L1, runtime)),
    })

    cfg = SimpleNamespace(
        env={"CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok", "CLAUDE_CONFIG_DIR": str(runtime),
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"},
        l1_address=L1, l1_level="L1", runtime_root=runtime, build_id="b-int4",
        pinned_binary=config.PINNED_BINARY, level_config=config.LevelConfig.for_level("L1"),
    )
    genesis.run_genesis(executor, FakeTmux({}), cfg)

    rb = ledger.read_binding(L1)
    assert rb["state"] == "running" and len(fake.calls) == 1
    assert rb["lease_epoch"] == 6, (
        f"the claim must bump the SURVIVING slot's epoch (5 -> 6), got {rb['lease_epoch']!r} — "
        "any other value means the slot was necro'd + re-registered, not claimed as-is (INT-4(a))"
    )
    wal_events = [r.get("event") for r in ledger.load_wal() if r.get("node_address") == L1]
    assert "died_infrastructure" not in wal_events, (
        "a real boot must not reap the planned survivor before genesis STEP 5"
    )
