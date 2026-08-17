"""Tests for cc_config.seed_trust (deterministic first-boot trust / kill-all-prompts) + the adapter
wiring that adds --dangerously-skip-permissions when jailed.

The mechanism (verified live, 2026-06-06): a spawned interactive Claude Code hits BLOCKING startup
dialogs (workspace trust, bypass-permissions warning, per-tool prompts) that freeze an unattended
agent. The harness ELIMINATES them deterministically — pre-seeding the config keys so no dialog appears
(the jail is the structural bound; prompts are superfluous). These pin that the keys are written + that
--dangerously-skip-permissions is added ONLY for a jailed spawn (the safety invariant).
"""

import json
import stat
import tempfile
from pathlib import Path

import pytest

import harnessd.spawn.cc_config as cc_config


def _seeded(workspace="/runtime/proj-widget-exec"):
    cfg = tempfile.mkdtemp()
    cc_config.seed_trust(cfg, workspace)
    claude_json = json.loads((Path(cfg) / ".claude.json").read_text())
    settings_json = json.loads((Path(cfg) / "settings.json").read_text())
    return cfg, workspace, claude_json, settings_json


def test_seed_trust_writes_all_dialog_suppression_keys():
    _cfg, ws, cj, sj = _seeded()
    proj = cj["projects"][ws]
    assert proj["hasTrustDialogAccepted"] is True, "the workspace TRUST dialog must be pre-accepted"
    assert proj["hasCompletedProjectOnboarding"] is True, "project onboarding must be pre-completed"
    assert cj["bypassPermissionsModeAccepted"] is True, "the BYPASS-permissions warning must be pre-accepted"
    assert cj["hasCompletedOnboarding"] is True, "first-run onboarding must be pre-completed"
    assert sj["skipDangerousModePermissionPrompt"] is True, "the bypass-prompt floor must be set"
    assert sj["showThinkingSummaries"] is True, "Claude Code reasoning summaries must be requested"


def test_seed_trust_is_per_workspace_and_preserves_other_projects():
    """The trust is per-folder; seeding a second workspace must NOT clobber the first (CC keys projects
    by path — the daemon seeds each node's WORKROOT)."""
    cfg = tempfile.mkdtemp()
    cc_config.seed_trust(cfg, "/runtime/node-a")
    cc_config.seed_trust(cfg, "/runtime/node-b")
    cj = json.loads((Path(cfg) / ".claude.json").read_text())
    assert "/runtime/node-a" in cj["projects"] and "/runtime/node-b" in cj["projects"], \
        "seeding a second workspace clobbered the first node's trust"


def test_seed_trust_idempotent():
    cfg = tempfile.mkdtemp()
    cc_config.seed_trust(cfg, "/runtime/node-a")
    cc_config.seed_trust(cfg, "/runtime/node-a")  # twice
    cj = json.loads((Path(cfg) / ".claude.json").read_text())
    assert cj["projects"]["/runtime/node-a"]["hasTrustDialogAccepted"] is True


def test_seed_trust_absent_config_dir_is_clean_noop():
    """A placeholder/non-existent config dir (the dry-run/test path) -> clean no-op, NOT a crash and
    NOT a mkdir of a fake tree. The pinned config dir always pre-exists in production."""
    cc_config.seed_trust("/HARNESS/.cc-pinned/config-does-not-exist", "/x")  # must not raise
    assert not Path("/HARNESS").exists(), "seed_trust must not create a fake config tree"


def test_seed_trust_preserves_unrelated_existing_settings():
    """Merging into an existing .claude.json/settings.json must not drop unrelated keys."""
    cfg = tempfile.mkdtemp()
    (Path(cfg) / ".claude.json").write_text(json.dumps({"theme": "x", "projects": {"/old": {"k": 1}}}))
    (Path(cfg) / "settings.json").write_text(json.dumps({"theme": "dark", "custom": True}))
    cc_config.seed_trust(cfg, "/runtime/new-node")
    cj = json.loads((Path(cfg) / ".claude.json").read_text())
    sj = json.loads((Path(cfg) / "settings.json").read_text())
    assert cj["theme"] == "x" and cj["projects"]["/old"]["k"] == 1, "unrelated .claude.json keys dropped"
    assert sj["custom"] is True and sj["theme"] == "dark", "unrelated settings.json keys dropped"


def test_turn_hook_settings_install_the_exact_truthful_five_edges(tmp_path):
    argv = [
        "/usr/bin/python3",
        "/repo/harnessd/turn_state_hook.py",
        "--node-address",
        "proj/work#exec",
    ]
    path = cc_config.write_turn_hook_settings(
        str(tmp_path),
        seat="exec",
        hook_argv=argv,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path == tmp_path / ".turn-hooks.exec.settings.json"
    assert set(payload["hooks"]) == {
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
    }
    commands = {
        handlers[0]["hooks"][0]["command"]
        for handlers in payload["hooks"].values()
    }
    assert commands == {
        "/usr/bin/python3 /repo/harnessd/turn_state_hook.py "
        "--node-address 'proj/work#exec'"
    }
    assert all(
        handlers[0]["hooks"][0]["type"] == "command"
        and handlers[0]["hooks"][0]["timeout"] == 30
        for handlers in payload["hooks"].values()
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o400


# --- adapter wiring: --dangerously-skip-permissions ONLY when jailed (the safety invariant) ----------

def _adapter_argv(containment):
    """Drive the real adapter argv assembly with/without a containment block; return the child argv."""
    import harnessd.spawn.adapters.claude_code as cca

    class _RecTmux:
        def __init__(self): self.opened = []
        def build_pane_argv(self, env, argv):
            return ["env", "-i", *[f"{k}={v}" for k, v in env.items()], *argv]
        def server_env(self): return {}
        def create_detached(self, name, pane_argv, env): self.opened.append((name, pane_argv, env)); return "%0"

    adapter = cca.ClaudeCodeAdapter(tmux=_RecTmux())
    env = {"CLAUDE_CONFIG_DIR": tempfile.mkdtemp(), "CLAUDE_CODE_OAUTH_TOKEN": "t",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
    brief = {"role_variant": "L1"}
    if containment is not None:
        brief["containment_profile"] = containment
        env.update({"CLAUDE_CODE_TMPDIR": "/tmp/x", "HOME": "/tmp/home"})

    class _LC:
        role_variant = "L1"; level = "L1"; reasoning_effort = "high"
    res = adapter.pin_and_open(brief, _LC(), "proj/w#exec", env)
    return list(res.argv)


def test_skip_permissions_added_only_when_jailed():
    wr = tempfile.mkdtemp()
    jailed = _adapter_argv({"WORKROOT": wr, "TMPDIR": wr + "/tmp", "CONFIG": tempfile.mkdtemp(),
                            "HOME": "/tmp/home", "READ_DENY_ROOT": ""})
    assert "--dangerously-skip-permissions" in jailed, \
        "a JAILED spawn must add --dangerously-skip-permissions (auto-approve; the jail is the bound)"

    unjailed = _adapter_argv(None)
    assert "--dangerously-skip-permissions" not in unjailed, \
        "an UNJAILED (dry-run) spawn must NOT auto-approve — skip-permissions is coupled to the jail"
