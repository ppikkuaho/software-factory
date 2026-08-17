"""Increment 9 — load-bearing STRENGTHENING (mutation-review gate + bias-to-real).

Gap the verify gate found: the adapter DEFINED _ISOLATION_ENV_KEYS with a comment claiming it makes
"a missing/extra var fail the assembly loudly" — but nothing ENFORCED it. Since the pane is built
`env -i <K=V for each env var>`, an extra var passed by the caller would widen the pane env (the exact
leak env -i defends against). Now enforced (set(env) != the 4-var set -> SpawnFailure); lock it.
"""

import shutil

import pytest

import harnessd.spawn.adapters.claude_code as cca
import harnessd.spawn.oauth_guard as oauth_guard


def _clean_env():
    return {
        "CLAUDE_CONFIG_DIR": ".cc-pinned/config",
        "CLAUDE_CODE_OAUTH_TOKEN": "tok-xyz",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


class _MockTmux:
    """Minimal mock tmux for the dry-run (no real exec); records create_detached."""
    def __init__(self):
        self.opened = []

    def build_pane_argv(self, env, argv):
        pane = ["env", "-i"]
        for k, v in env.items():
            pane.append(f"{k}={v}")
        return pane + list(argv)

    def server_env(self):
        return {}

    def create_detached(self, name, pane_argv, env):
        self.opened.append((name, pane_argv, env))
        return "%0"


class _LevelCfg:
    role_variant = "L1"
    level = "L1"
    reasoning_effort = "high"


def test_adapter_rejects_extra_env_var():
    """An env with a 5th var must FAIL the assembly (SpawnFailure) — no widened pane opens."""
    adapter = cca.ClaudeCodeAdapter(tmux=_MockTmux())
    env = _clean_env()
    env["EXTRA_LEAK"] = "x"  # a 5th var that env -i would otherwise carry into the pane
    with pytest.raises(oauth_guard.SpawnFailure):
        adapter.pin_and_open({"role_variant": "L1"}, _LevelCfg(), "proj/a#exec", env)


def test_adapter_rejects_missing_required_env_var():
    """An env missing a required isolation var must FAIL the assembly (SpawnFailure)."""
    adapter = cca.ClaudeCodeAdapter(tmux=_MockTmux())
    env = _clean_env()
    del env["CLAUDE_CODE_OAUTH_TOKEN"]
    with pytest.raises(oauth_guard.SpawnFailure):
        adapter.pin_and_open({"role_variant": "L1"}, _LevelCfg(), "proj/a#exec", env)


def test_adapter_accepts_exact_four_var_env():
    """Control: the exact 4-var env opens a pane and records the facts (no over-rejection)."""
    mock = _MockTmux()
    adapter = cca.ClaudeCodeAdapter(tmux=mock)
    res = adapter.pin_and_open({"role_variant": "L1"}, _LevelCfg(), "proj/a#exec", _clean_env())
    assert res.ok is True
    assert len(mock.opened) == 1, "exactly one pane opened on a clean 4-var env"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs real tmux")
def test_list_targets_window_activity_present_with_real_value():
    """(c) strengthening: window_activity is present with a real (non-None) value, not just the key."""
    import os
    import subprocess
    import harnessd.spawn.tmux as tmux

    sock = "harness-strengthen-" + os.urandom(4).hex()
    tmux.set_socket(sock)
    try:
        tmux.create_detached("harness:wtest", ["env", "-i", "sh", "-c", "sleep 30"], {})
        targets = tmux.list_targets()
        assert targets, "a living pane must be listed"
        row = next(iter(targets.values()))
        assert "window_activity" in row and row["window_activity"] is not None, \
            "window_activity must be present with a real value (the §2.11 shape)"
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
