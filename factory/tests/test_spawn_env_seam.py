"""LT-1 — the spawn-env injection seam: commissioning's REAL env must reach the pane.

The CRITICAL pre-live-run finding: ``chokepoint._spawn_env()`` carried only the structural
placeholders (``$HARNESS/.cc-pinned/config`` / ``<oauth-token-file>``) and NOTHING threaded
commissioning's assembled 4-var OAuth env (``runtime.config.env`` — the env the genesis
credential precondition validates) into the spawn path. Every production spawn would boot an
unauthenticated CC on an unseeded config dir, and the ungated kickoff Enter would answer the
resulting first-boot dialog.

The fix, pinned here:
  * ``chokepoint.set_spawn_env`` — a module-level injectable mirroring ``set_adapter``;
    ``_spawn_env()`` falls back to the structural placeholders ONLY when nothing is bound
    (the dry-run tests keep their shape).
  * ``daemon.boot`` binds ``runtime.config.env`` into the seam (the same boot step that wires
    the adapter), so the env that passed genesis preconditions IS the env the pane boots with.
  * the REAL transport refuses a placeholder launch: ``tmux.create_detached`` raises a
    ``SpawnFailure(failure_class='placeholder_env')`` when the env still carries the token
    sentinel — the §6.3 net releases the claim and escalates instead of opening a broken pane.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import harnessd.config as config
import harnessd.daemon as daemon
import harnessd.executor as executor  # noqa: F401 — the REAL single writer the spawn routes through
import harnessd.ledger as ledger
from harnessd import addressing, fencing
from harnessd.spawn import chokepoint
from harnessd.spawn import tmux as tmux_mod
from harnessd.spawn import oauth_guard
from harnessd.spawn.adapters.base import SpawnResult

L1_ADDRESS = "L1#exec"
LEAF = "proj/widget/task#exec"


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
    """Records pin_and_open calls (the env is calls[i][3]); returns a happy dry-run SpawnResult."""

    def __init__(self):
        self.calls = []

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        return SpawnResult(
            ok=True,
            session_uuid="sess-env-seam-0001",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L1"),
            system_prompt_file=config.SYSTEM_PROMPT_FILE,
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path="/runtime/transcripts/sess-env-seam-0001.jsonl",
            failure_class=None,
        )


class FakeTmux:
    def list_targets(self):
        return {}


def _real_env(runtime):
    return {
        "CLAUDE_CODE_OAUTH_TOKEN": "test-ant-oat01-REAL-TOKEN",
        "CLAUDE_CONFIG_DIR": str(runtime / ".cc-pinned/config"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def _seed_planned_leaf(runtime):
    token = fencing.mint_owner_token(LEAF, "sa", "uuid", 1)
    ws = addressing.node_dir(LEAF, runtime)
    ws.mkdir(parents=True, exist_ok=True)
    rec = {
        "node_address": LEAF, "parent_address": "proj/widget#exec", "level": "L5",
        "subagent_id": "sa", "session_uuid": "uuid", "state": "planned", "generation": 0,
        "lease_epoch": 1, "owner_token": token, "last_applied_seq": 0, "spec_pointer": "design/intent-spec.md", "frozen_acceptance_ref": "acceptance.md",
        "liveness_state": "claimed", "terminal_signal": None, "gate_crossed_at": None,
        "paused_at": None, "transcript_path": None,
        "tmux_target": addressing.session_name_for(LEAF), "workspace": str(ws),
    }
    ledger.write_binding({LEAF: copy.deepcopy(rec)}, _lock_held=True)
    return token


# ---------------------------------------------------------------------------------------
# (1) The structural fallback: nothing bound -> the placeholder shape (dry-run intact).
# ---------------------------------------------------------------------------------------

def test_spawn_env_unbound_falls_back_to_structural_placeholders():
    assert chokepoint.SPAWN_ENV is None, "the seam starts unbound (the conftest fixture restores it)"
    env = chokepoint._spawn_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == oauth_guard.PLACEHOLDER_OAUTH_TOKEN
    assert env["CLAUDE_CONFIG_DIR"] == oauth_guard.PLACEHOLDER_CONFIG_DIR
    assert set(env) == {
        "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_AUTOUPDATER", "PATH",
    }, "the structural fallback = the 4 isolation vars + PATH (LR-2; DAEMON §6.2 amended)"


# ---------------------------------------------------------------------------------------
# (2) A bound env reaches the adapter through the REAL chokepoint spawn.
# ---------------------------------------------------------------------------------------

def test_bound_spawn_env_reaches_the_adapter_through_claim_and_spawn(runtime):
    token = _seed_planned_leaf(runtime)
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)
    real_env = _real_env(runtime)
    chokepoint.set_spawn_env(real_env)

    result = chokepoint.claim_and_spawn(
        LEAF, expected_state="planned", expected_generation=0,
        expected_owner_token=token, level_config=config.LevelConfig.for_level("L5"),
    )

    assert result.ok, f"spawn must succeed: {result!r}"
    assert fake.calls, "the adapter must have been reached"
    handed_env = fake.calls[0][3]
    assert handed_env == real_env, (
        "the BOUND spawn env (commissioning's real 4-var OAuth env) must be what the adapter "
        f"opens the pane with — not the structural placeholder; got {handed_env!r}"
    )
    assert handed_env["CLAUDE_CODE_OAUTH_TOKEN"] != oauth_guard.PLACEHOLDER_OAUTH_TOKEN


def test_set_spawn_env_none_restores_the_placeholder_fallback(runtime):
    chokepoint.set_spawn_env(_real_env(runtime))
    chokepoint.set_spawn_env(None)
    assert chokepoint._spawn_env()["CLAUDE_CODE_OAUTH_TOKEN"] == oauth_guard.PLACEHOLDER_OAUTH_TOKEN


# ---------------------------------------------------------------------------------------
# (3) daemon.boot binds runtime.config.env -> the genesis L1 spawn boots with the REAL env.
# ---------------------------------------------------------------------------------------

def test_boot_binds_runtime_config_env_and_l1_spawns_with_it(runtime):
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)
    env = _real_env(runtime)
    cfg = SimpleNamespace(
        env=env,
        l1_address=L1_ADDRESS,
        l1_level="L1",
        runtime_root=runtime,
        build_id="build-env-seam",
        pinned_binary=config.PINNED_BINARY,
        level_config=config.LevelConfig.for_level("L1"),
    )
    rt = SimpleNamespace(
        runtime_root=runtime, build_id="build-env-seam", config=cfg,
        adapter=fake, tmux=FakeTmux(), executor=executor,
    )

    daemon.boot(rt)

    assert chokepoint.SPAWN_ENV == env, (
        "daemon.boot must bind runtime.config.env into chokepoint.set_spawn_env (LT-1) — "
        "the env that passed the genesis credential precondition is the env panes boot with"
    )
    l1_calls = [c for c in fake.calls if L1_ADDRESS in str(c[2])]
    assert l1_calls, "genesis must have spawned the L1 root through the chokepoint"
    assert l1_calls[0][3] == env, (
        f"the L1 pane env must be the commissioned env, not the placeholder; got {l1_calls[0][3]!r}"
    )


def test_boot_without_an_env_leaves_the_fallback_intact(runtime):
    """A sparse runtime descriptor (no config.env) must NOT bind an empty/None seam."""
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)
    cfg = SimpleNamespace(
        env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"},  # the precondition minimum
        l1_address=L1_ADDRESS, l1_level="L1", runtime_root=runtime,
        build_id="b", pinned_binary=config.PINNED_BINARY,
        level_config=config.LevelConfig.for_level("L1"),
    )
    # Drop env AFTER constructing — simulate a descriptor with env=None.
    cfg.env = None
    rt = SimpleNamespace(runtime_root=runtime, build_id="b", config=cfg,
                         adapter=fake, tmux=FakeTmux(), executor=executor)
    with pytest.raises(Exception):
        daemon.boot(rt)  # the credential precondition fails loudly (no env) — fine
    assert chokepoint.SPAWN_ENV is None, "boot must not bind a None/absent env into the seam"


# ---------------------------------------------------------------------------------------
# (4) The REAL transport refuses a placeholder launch (fail-loud, §6.3-catchable).
# ---------------------------------------------------------------------------------------

def test_real_create_detached_refuses_the_placeholder_token(monkeypatch):
    ran = []

    def _no_run(args, **kwargs):  # pragma: no cover — only fires on the mutant
        ran.append(args)
        raise AssertionError("create_detached must REFUSE the placeholder env BEFORE any tmux exec")

    monkeypatch.setattr(tmux_mod, "_run", _no_run)
    placeholder_env = {
        "CLAUDE_CONFIG_DIR": oauth_guard.PLACEHOLDER_CONFIG_DIR,
        "CLAUDE_CODE_OAUTH_TOKEN": oauth_guard.PLACEHOLDER_OAUTH_TOKEN,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    with pytest.raises(oauth_guard.SpawnFailure) as excinfo:
        tmux_mod.create_detached("harness-x", ["sh", "-c", "true"], placeholder_env)
    assert excinfo.value.failure_class == "placeholder_env", (
        "the refusal carries its own failure_class so the §6.3 escalation names it"
    )
    assert not ran, "no tmux subprocess may be reached with the placeholder env"


def test_real_create_detached_accepts_a_non_placeholder_env(monkeypatch):
    """The refusal keys ONLY on the sentinel — a real token env reaches tmux (recorded, not run)."""
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="harness-x:0.0\n", returncode=0, stderr="")

    monkeypatch.setattr(tmux_mod, "_run", _fake_run)
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "test-ant-oat01-X", "CLAUDE_CONFIG_DIR": "/x",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
    target = tmux_mod.create_detached("harness-x", ["sh", "-c", "true"], env)
    assert target == "harness-x:0.0"
    assert calls, "a clean env must reach tmux new-session"
