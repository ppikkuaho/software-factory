"""INT-5 — genesis authors the L1 root's brief.md (the kickoff pointer must name a real file).

The pre-live-run finding: the STEP6 kickoff tells every agent 'Read brief.md in your workspace'
— but only the parent-spawns-child path (_write_child_brief) ever authored one. The genesis-
spawned L1 root (the very node the live run exists to bring up) booted instruction-less: no
load-manifest delivery, no F19 Sign-off block, a pointer at a missing file.

genesis now authors the L1 brief at spawn time, reusing the SAME child-brief writer: the
manifest header (who you are + the read-in-place identity documents), the Sign-off block naming
the .sign-off.<seat>.json handshake, and a v1-honest TASK — an intake pointer (await/read the
inbox; never invent work). A pre-authored brief.md is left intact.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.executor as executor
import harnessd.genesis as genesis
import harnessd.ledger as ledger
from harnessd.spawn import chokepoint
from harnessd.spawn.adapters.base import SpawnResult

L1_ADDRESS = "L1#exec"


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
    def __init__(self):
        self.calls = []

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        return SpawnResult(
            ok=True,
            session_uuid="sess-l1-brief-0001",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L1"),
            system_prompt_file=config.SYSTEM_PROMPT_FILE,
            system_prompt_file_hash="deadbeef",
            tmux_target=tmux_target,
            transcript_path="/runtime/transcripts/sess-l1-brief-0001.jsonl",
            failure_class=None,
        )


class FakeTmux:
    def list_targets(self):
        return {}


def _cfg(runtime_root):
    return SimpleNamespace(
        env={
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok-present",
            "CLAUDE_CONFIG_DIR": str(runtime_root / ".cc-pinned/config"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        },
        l1_address=L1_ADDRESS,
        l1_level="L1",
        runtime_root=runtime_root,
        build_id="build-l1-brief",
        pinned_binary=config.PINNED_BINARY,
        level_config=config.LevelConfig.for_level("L1"),
    )


def _inbox_lines(runtime_root):
    inbox = addressing.inbox_path(L1_ADDRESS, runtime_root)
    if not inbox.exists():
        return []
    return [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_genesis_authors_the_l1_root_brief(runtime):
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)

    genesis.run_genesis(executor, FakeTmux(), _cfg(runtime))

    brief_path = addressing.node_dir(L1_ADDRESS, runtime) / "brief.md"
    assert brief_path.is_file(), (
        "genesis must author the L1 root's brief.md — the kickoff pointer ('Read brief.md in "
        "your workspace') must name a file that EXISTS for the first live agent (INT-5)"
    )
    text = brief_path.read_text(encoding="utf-8")

    # The launch pointer: L1 is a launch-surface pilot role, so identity arrives through the
    # generated startup packet rather than the legacy load-manifest heading.
    assert f"node_address: {L1_ADDRESS}" in text
    assert "## Launch Packet" in text, (
        "the L1 brief points at the generated launch packet — without it the root boots "
        "without its role-shaped startup surface"
    )
    assert ".reference-map.md" in text

    # The F19 Sign-off block: the handshake + signal artifact, discoverable at the root too.
    assert "## Sign-off" in text
    assert str(addressing.signoff_path(L1_ADDRESS, runtime)) in text, (
        "the Sign-off block names the .sign-off.<seat>.json handshake path (F19 belt-and-braces)"
    )
    assert str(addressing.signal_path(L1_ADDRESS, runtime)) in text

    # The v1-honest task: an INTAKE POINTER — await/read the inbox, never invent work.
    assert "## Task" in text
    assert ".inbox.exec.jsonl" in text, "the task points L1 at its intake inbox"
    low = text.lower()
    assert "intake" in low and ("await" in low or "wait" in low), (
        "v1 honesty: the L1 root has no parent-authored task — the brief says to await/read the "
        "human intake rather than inventing work"
    )


def test_genesis_leaves_a_preauthored_l1_brief_intact(runtime):
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)
    node_dir = addressing.node_dir(L1_ADDRESS, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "brief.md").write_text("# operator-authored L1 brief\nDO-NOT-CLOBBER\n",
                                       encoding="utf-8")

    genesis.run_genesis(executor, FakeTmux(), _cfg(runtime))

    text = (node_dir / "brief.md").read_text(encoding="utf-8")
    assert "DO-NOT-CLOBBER" in text, (
        "a pre-authored brief.md is the derivation default — genesis must never clobber it"
    )


def test_l1_brief_lands_before_the_actor_opens(runtime):
    """Ordering: the brief is on disk BEFORE pin_and_open — the agent's first read finds it."""
    seen = {}

    class OrderingAdapter(FakeAdapter):
        def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
            seen["brief_exists_at_open"] = (
                addressing.node_dir(L1_ADDRESS, runtime) / "brief.md"
            ).is_file()
            return super().pin_and_open(neutral_brief, level_config, tmux_target, env)

    chokepoint.set_adapter(OrderingAdapter())
    genesis.run_genesis(executor, FakeTmux(), _cfg(runtime))
    assert seen.get("brief_exists_at_open") is True, (
        "brief.md must exist BEFORE the actor opens (the kickoff pointer is its first instruction)"
    )


def test_l1_initial_intake_lands_before_the_actor_opens(runtime):
    """LR-24: commissioning/genesis can seed the L1 intake before the root actor's first turn."""
    seen = {}

    class IntakeOrderingAdapter(FakeAdapter):
        def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
            seen["inbox_lines_at_open"] = _inbox_lines(runtime)
            return super().pin_and_open(neutral_brief, level_config, tmux_target, env)

    cfg = _cfg(runtime)
    cfg.initial_intake = "Build the parser harness and report acceptance status."
    chokepoint.set_adapter(IntakeOrderingAdapter())

    genesis.run_genesis(executor, FakeTmux(), cfg)

    lines = seen.get("inbox_lines_at_open") or []
    intake = [line for line in lines if line.get("type") == "intake"]
    assert len(intake) == 1, (
        "the initial intake must be present before pin_and_open returns control to the actor; "
        "post-spawn wake lines are too late for LR-24"
    )
    assert intake[0]["from"] == "operator"
    assert intake[0]["message"] == cfg.initial_intake
    assert intake[0]["id"].startswith("genesis-initial-intake:")


def test_l1_initial_intake_preseed_is_idempotent(runtime):
    cfg = _cfg(runtime)
    cfg.initial_intake = "Build the parser harness and report acceptance status."

    genesis._seed_l1_initial_intake(L1_ADDRESS, runtime, cfg.initial_intake)
    genesis._seed_l1_initial_intake(L1_ADDRESS, runtime, cfg.initial_intake)

    intake = [line for line in _inbox_lines(runtime) if line.get("type") == "intake"]
    assert len(intake) == 1, "retries/relaunches must not duplicate the same genesis intake"


def test_no_l1_initial_intake_config_keeps_inbox_free_of_intake_lines(runtime):
    chokepoint.set_adapter(FakeAdapter())

    genesis.run_genesis(executor, FakeTmux(), _cfg(runtime))

    intake = [line for line in _inbox_lines(runtime) if line.get("type") == "intake"]
    assert intake == [], "without configured initial_intake, genesis must not invent L1 work"
