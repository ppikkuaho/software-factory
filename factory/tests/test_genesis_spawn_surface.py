"""F2a — a failed first-boot L1 spawn must SURFACE, not be silently swallowed (review genesis-1).

`chokepoint.claim_and_spawn` returns ``SpawnResult(ok=False, failure_class=...)`` on EVERY post-
precondition failure (claim_lost, paused_subtree, a post-claim SpawnFailure incl. auth_expired/
model_unavailable/runtime_down) — it does NOT raise. `run_genesis` called it and DISCARDED the result,
returning None (success) even when NO L1 actor opened. So a failed L1 boot looked like a clean boot —
the daemon comes up with no root and nothing happens, and the WHY is invisible. For the first e2e run
this is the worst failure mode: a silent dead boot. Fix: run_genesis checks the result and raises a
clear GenesisError naming the failure_class (L1 is the root — there is no parent to escalate to; a failed
L1 boot is fatal and must be loud).
"""

import copy

import pytest

import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.genesis as genesis
import harnessd.spawn.chokepoint as chokepoint
import harnessd.spawn.oauth_guard as oauth_guard


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


L1 = "L1#exec"


class _Tmux:
    def list_targets(self):
        return {}


def _config():
    from types import SimpleNamespace
    import harnessd.config as config
    return SimpleNamespace(
        env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "CLAUDE_CONFIG_DIR": "/cfg",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"},
        l1_address=L1, l1_level="L1", runtime_root=ledger.RUNTIME_ROOT, build_id="b",
        pinned_binary=config.PINNED_BINARY, level_config=config.LevelConfig.for_level("L1"),
    )


class _FailingAdapter:
    """pin_and_open raises AuthExpired — the L1 actor never opens (a post-claim spawn failure)."""
    def pin_and_open(self, *a, **k):
        raise oauth_guard.AuthExpired("token expired at first boot")


class _OkAdapter:
    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        from harnessd.spawn.adapters.base import SpawnResult
        return SpawnResult(ok=True, session_uuid="s", model_used="m", role_variant="L1",
                           system_prompt_file="x", system_prompt_file_hash="h",
                           tmux_target=tmux_target, transcript_path="/tmp/s.jsonl", failure_class=None)


def _install(a):
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(a)
    else:
        chokepoint.ADAPTER = a


def test_failed_l1_boot_raises_not_silently_succeeds(runtime):
    """A first-boot L1 spawn that fails (no actor opens) must raise a clear error — NOT return None
    (silent clean boot). The error should name the failure_class so the operator sees WHY."""
    _install(_FailingAdapter())
    import harnessd.executor as executor
    with pytest.raises(Exception) as ei:
        genesis.run_genesis(executor, _Tmux(), _config())
    msg = str(ei.value).lower()
    assert "auth_expired" in msg or "l1" in msg, (
        f"the raised error must name the L1 boot failure (got: {ei.value!r})"
    )
    # and the L1 binding must NOT be left as a phantom 'running' (no actor opened)
    b = ledger.read_binding(L1)
    assert b is None or b.get("state") != "running", "a failed L1 boot must not leave a phantom running L1"


def test_successful_l1_boot_still_returns_cleanly(runtime):
    """Control: a successful first-boot L1 spawn still completes (running L1, no raise)."""
    _install(_OkAdapter())
    import harnessd.executor as executor
    genesis.run_genesis(executor, _Tmux(), _config())
    assert ledger.read_binding(L1)["state"] == "running"
