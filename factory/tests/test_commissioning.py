"""Commissioning entry — assemble the runtime descriptor + bind the global seams so daemon.run boots.

The review found daemon.run / boot read ledger.RUNTIME_ROOT + the tmux dedicated-server socket but never
SET them, and the launchd-named entry had no descriptor assembler. This is the commissioning gate to a
live run: build_runtime() wires the REAL ClaudeCodeAdapter + the 4-var OAuth floor and
non-credential PATH (read from the pinned install) + the L1 root config;
daemon._apply_global_seams binds ledger.RUNTIME_ROOT + the dedicated tmux
socket so the substrate (genesis/executor/tmux) is correctly anchored before boot.

These tests pin the descriptor SHAPE + the seam-binding (no real boot — the live run is the real oracle).
"""

import os

import pytest

import harnessd.commissioning as commissioning
import harnessd.config as config
import harnessd.daemon as daemon
import harnessd.ledger as ledger
import harnessd.spawn.tmux as tmux
from harnessd.spawn.adapters.claude_code import ClaudeCodeAdapter


def test_build_runtime_assembles_the_oauth_env_and_l1_config(tmp_path):
    """The descriptor carries the genesis config: OAuth-only floor, PATH, and L1 root config."""
    rt = commissioning.build_runtime(
        runtime_root=tmp_path / "runtime", build_id="build-test",
        oauth_token="test-ant-oat01-TESTTOKEN")
    cfg = rt.config
    assert cfg.env["CLAUDE_CODE_OAUTH_TOKEN"] == "test-ant-oat01-TESTTOKEN"
    assert cfg.env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") == "1"
    assert cfg.env.get("DISABLE_AUTOUPDATER") == "1"
    assert "CLAUDE_CONFIG_DIR" in cfg.env
    assert cfg.l1_address and cfg.l1_level == "L1"
    # E4: production ships adapter=None — the chokepoint resolves adapters from the
    # per-runtime REGISTRY (daemon._apply_global_seams); injecting a single adapter here
    # would override the registry and recreate the LT-8/O1 silent divergence.
    assert rt.adapter is None
    # the genesis collaborators are present
    assert rt.executor is not None and rt.tmux is not None
    assert cfg.level_config.blinders_mode == "observe"


def test_build_runtime_uses_the_single_production_pane_path(tmp_path):
    rt = commissioning.build_runtime(
        runtime_root=tmp_path / "runtime",
        build_id="build-test",
        oauth_token="test-ant-oat01-TESTTOKEN",
    )
    assert commissioning.PRODUCTION_PANE_PATH == (
        "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
    )
    assert rt.config.env["PATH"] == commissioning.PRODUCTION_PANE_PATH


def test_production_blinders_mode_defaults_observe_and_enforce_is_explicit(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(config.BLINDERS_MODE_ENV, raising=False)
    assert config.get_level_config("L3").blinders_mode == "observe"
    assert config.LevelConfig.for_level("L3").blinders_mode is None

    monkeypatch.setenv(config.BLINDERS_MODE_ENV, "enforce")
    assert config.get_level_config("L3").blinders_mode == "enforce"
    rt = commissioning.build_runtime(
        runtime_root=tmp_path / "runtime",
        build_id="b",
        oauth_token="test-ant-oat01-X",
    )
    assert rt.config.level_config.blinders_mode == "enforce"


def test_invalid_production_blinders_mode_fails_loudly(monkeypatch):
    monkeypatch.setenv(config.BLINDERS_MODE_ENV, "off")
    with pytest.raises(ValueError, match="observe.*enforce"):
        config.get_level_config("L2")


def test_build_runtime_carries_initial_intake_from_arg(tmp_path):
    rt = commissioning.build_runtime(
        runtime_root=tmp_path / "runtime",
        build_id="build-test",
        oauth_token="test-ant-oat01-TESTTOKEN",
        initial_intake="Build the payment parser and report status.",
    )
    assert rt.config.initial_intake == "Build the payment parser and report status."


def test_build_runtime_reads_initial_intake_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_L1_INTAKE", "Build from env intake.")
    rt = commissioning.build_runtime(
        runtime_root=tmp_path / "runtime",
        build_id="build-test",
        oauth_token="test-ant-oat01-TESTTOKEN",
    )
    assert rt.config.initial_intake == "Build from env intake."


def test_build_runtime_reads_the_pinned_token_when_not_supplied(tmp_path, monkeypatch):
    """When no token is passed, build_runtime reads it from the pinned install's .oauth_token (the real
    source). We point the token-file resolution at a temp file to avoid depending on the live token."""
    tokfile = tmp_path / ".oauth_token"
    tokfile.write_text("test-ant-oat01-FROMFILE", encoding="utf-8")
    monkeypatch.setattr(commissioning, "_pinned_token_file", lambda: tokfile)
    rt = commissioning.build_runtime(runtime_root=tmp_path / "runtime", build_id="b")
    assert rt.config.env["CLAUDE_CODE_OAUTH_TOKEN"] == "test-ant-oat01-FROMFILE"


def test_build_runtime_oauth_only_no_raw_api_key(tmp_path):
    """The HARD invariant: the assembled env carries the OAuth token and NO raw API key."""
    rt = commissioning.build_runtime(runtime_root=tmp_path / "runtime", build_id="b",
                                     oauth_token="test-ant-oat01-X")
    assert "ANTHROPIC_API_KEY" not in rt.config.env and "OPENAI_API_KEY" not in rt.config.env


def test_apply_global_seams_binds_runtime_root_and_tmux_socket(tmp_path):
    """daemon._apply_global_seams must bind ledger.RUNTIME_ROOT (the substrate's anchor) + the dedicated
    tmux server socket (so harness panes land on the daemon's own tmux server, not the user's), BEFORE
    boot. Without this, genesis/executor raise 'runtime_root not configured' and panes pollute the
    default tmux server."""
    prev_root = ledger.RUNTIME_ROOT
    prev_sock = tmux._SOCKET
    try:
        rt = commissioning.build_runtime(runtime_root=tmp_path / "runtime", build_id="b",
                                         oauth_token="test-ant-oat01-X")
        daemon._apply_global_seams(rt)
        assert str(ledger.RUNTIME_ROOT) == str(tmp_path / "runtime"), "RUNTIME_ROOT must be bound"
        assert tmux._SOCKET is not None, "the dedicated tmux server socket must be bound (visible-mode attach)"
    finally:
        ledger.RUNTIME_ROOT = prev_root
        tmux.set_socket(prev_sock)


def test_runtime_exposes_the_tmux_socket_name_for_observability(tmp_path):
    """The descriptor surfaces the tmux socket NAME so the operator can attach:
    `tmux -L <socket> attach -t harness:<addr>` (the visible-mode watch path, task #11)."""
    rt = commissioning.build_runtime(runtime_root=tmp_path / "runtime", build_id="b",
                                     oauth_token="test-ant-oat01-X")
    assert getattr(rt, "tmux_socket", None), "the runtime must name its tmux socket for attach"


def test_tmux_socket_name_is_per_build_and_shell_safe(tmp_path):
    """Each behavioral run needs its own tmux server namespace.

    Session names are address-derived and repeat across comparable runs, so a shared socket would let
    stale completed-run panes collide with the next clean run.
    """
    one = commissioning.build_runtime(runtime_root=tmp_path / "one", build_id="build-logview-20260617T095641Z",
                                      oauth_token="test-ant-oat01-X")
    two = commissioning.build_runtime(runtime_root=tmp_path / "two", build_id="build logview/next#run",
                                      oauth_token="test-ant-oat01-X")
    assert one.tmux_socket == "harnessd-build-logview-20260617t095641z"
    assert two.tmux_socket == "harnessd-build-logview-next-run"
    assert one.tmux_socket != two.tmux_socket
    assert "/" not in two.tmux_socket and "#" not in two.tmux_socket and " " not in two.tmux_socket


def test_pure_runtime_identity_is_shared_without_loading_oauth(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_WORKSPACES_ROOT", str(tmp_path / "family"))
    monkeypatch.delenv("HARNESS_RUNTIME_ROOT", raising=False)
    root, build_id = commissioning.resolve_runtime_identity(build_id="Build 42")
    assert root == (tmp_path / "family" / "Build 42").resolve()
    assert build_id == "Build 42"
    assert commissioning.tmux_socket_name(build_id) == "harnessd-build-42"

    explicit, explicit_id = commissioning.resolve_runtime_identity(
        runtime_root=tmp_path / "one" / ".." / "two",
        build_id="b",
    )
    assert explicit == (tmp_path / "two").resolve()
    assert explicit_id == "b"


def test_default_runtime_root_lands_in_the_external_workspaces_root(monkeypatch, tmp_path):
    """Workspaces live OUTSIDE the repo (user ruling 2026-06-12): with no explicit root and no
    per-run env override, the default lands under DEFAULT_WORKSPACES_ROOT/<build-id> — never
    inside the repo. $HARNESS_WORKSPACES_ROOT relocates the family. (Mutant: repo-nested
    default restored -> caught.)"""
    monkeypatch.delenv("HARNESS_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("HARNESS_WORKSPACES_ROOT", raising=False)
    rt = commissioning.build_runtime(build_id="b-default", oauth_token="test-ant-oat01-X")
    assert rt.runtime_root == commissioning.DEFAULT_WORKSPACES_ROOT / "b-default"
    assert "l1-l5-agent-harness" not in str(rt.runtime_root), (
        "the default workspace tree must not nest inside the repo"
    )

    monkeypatch.setenv("HARNESS_WORKSPACES_ROOT", str(tmp_path / "ws"))
    rt2 = commissioning.build_runtime(build_id="b-relocated", oauth_token="test-ant-oat01-X")
    assert rt2.runtime_root == tmp_path / "ws" / "b-relocated"
