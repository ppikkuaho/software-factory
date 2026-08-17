"""LT-4 / INT-1 / INT-2 — spawn over a live same-named pane + the resume pause gate.

The pre-live-run findings:

  * LT-4/INT-1 — ``session_name_for`` is deterministic per address, so any (re)spawn at an address
    whose previous incarnation's pane is still alive hits tmux 'duplicate session'. ``_run`` defaults
    check=True, so ``create_detached`` raised a raw ``CalledProcessError`` — an exception class the
    chokepoint's §6.3 net (``except (SpawnFailure, ApiKeyForbidden)``) does NOT catch: the committed
    claim leaked in ``claimed`` and genesis crashed unstructured. And NO production path ever called
    ``tmux.kill``, so the colliding pane could never be cleared.

    Fixes pinned here: (1) ``create_detached`` converts the CalledProcessError into a
    ``SpawnFailure(failure_class='tmux_session_collision')`` (other failures -> 'runtime_down') so
    the existing release+escalation fires; (2) the resume / re-register paths tear down the FENCED
    prior incarnation's recorded ``tmux_target`` (idempotent, via the adapter's own tmux seam)
    before reopening.

  * INT-2 — ``chokepoint.resume`` had no §6.1 STEP-0 ``subtree_paused`` gate (claim_and_spawn does),
    so a paused node could be re-claimed/re-spawned/kicked off via the genesis RESUME leg. The same
    gate now runs first.
"""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import uuid as uuid_mod
from types import SimpleNamespace

import pytest

import harnessd.config as config
import harnessd.executor as executor  # noqa: F401 — the REAL single writer
import harnessd.ledger as ledger
from harnessd import addressing, fencing
from harnessd.spawn import chokepoint
from harnessd.spawn import tmux as tmux_mod
from harnessd.spawn import oauth_guard
from harnessd.spawn.adapters.base import SpawnResult

LEAF = "proj/widget/task#exec"
PARENT = "proj/widget#exec"

_HAS_TMUX = shutil.which("tmux") is not None
_real_tmux = pytest.mark.skipif(not _HAS_TMUX, reason="real-tmux test: tmux binary not installed")


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


class _RecordingTmux:
    """Adapter-side tmux seam recorder: kills + creates, in call order."""

    def __init__(self):
        self.killed = []
        self.events = []  # ordered ("kill"|"create", target)

    def kill(self, target):
        self.killed.append(target)
        self.events.append(("kill", target))

    def send_keys(self, target, text):
        self.events.append(("send", target))
        return True

    def capture_pane(self, target):
        from harnessd import watchdog
        return f"{watchdog.FORK_PROMPT} \n? for shortcuts"


class FakeAdapter:
    """Happy-path dry-run adapter carrying a recording tmux seam."""

    def __init__(self):
        self.calls = []
        self.tmux = _RecordingTmux()

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        self.tmux.events.append(("create", tmux_target))
        return SpawnResult(
            ok=True,
            session_uuid="sess-collision-0001",
            model_used="opus-4.8 / claude-code",
            role_variant=getattr(level_config, "role_variant", "L5"),
            system_prompt_file=config.SYSTEM_PROMPT_FILE,
            system_prompt_file_hash="deadbeef",
            tmux_target=addressing.session_name_for(tmux_target) + ":0.0",
            transcript_path="/runtime/transcripts/sess-collision-0001.jsonl",
            failure_class=None,
        )


class CollidingAdapter:
    """An adapter whose pin_and_open raises the CONVERTED collision SpawnFailure (what the real
    adapter now surfaces when create_detached hits 'duplicate session')."""

    def __init__(self):
        self.calls = []

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append(tmux_target)
        raise oauth_guard.SpawnFailure(
            "tmux session collision: a live session already holds the name",
            failure_class="tmux_session_collision",
        )


def _seed(node_address=LEAF, *, state="running", parent=PARENT, runtime=None,
          tmux_target=None, paused_at=None, generation=1, lease_epoch=1, level=None):
    # Level defaults by shape: the PARENT coordinator seeds as L4 (claude-code), leaves as L5.
    # A running L5-labeled parent would wrongly read as the active codex runner under the
    # 2026-07-16 single-warm-runner gate — real parents are L4, so seed them as L4.
    if level is None:
        level = "L4" if node_address == PARENT else "L5"
    token = fencing.mint_owner_token(node_address, "sa", "uuid", lease_epoch)
    ws = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
    ws.mkdir(parents=True, exist_ok=True)
    rec = {
        "node_address": node_address, "parent_address": parent, "level": level,
        "subagent_id": "sa", "session_uuid": "uuid", "state": state,
        "generation": generation, "lease_epoch": lease_epoch, "owner_token": token,
        "last_applied_seq": 0, "spec_pointer": "design/intent-spec.md", "frozen_acceptance_ref": "acceptance.md", "liveness_state": "working", "terminal_signal": None,
        "terminal_signal_at": None, "gate_crossed_at": None, "paused_at": paused_at,
        "transcript_path": None, "workspace": str(ws),
        "tmux_target": tmux_target or (addressing.session_name_for(node_address) + ":0.0"),
    }
    live_map = dict(ledger.all_nodes())
    live_map[node_address] = copy.deepcopy(rec)
    ledger.write_binding(live_map, _lock_held=True)
    return rec, token


# ---------------------------------------------------------------------------------------
# (1) The transport conversion: 'duplicate session' -> SpawnFailure(tmux_session_collision).
# ---------------------------------------------------------------------------------------

@_real_tmux
def test_real_duplicate_session_raises_spawn_failure_collision():
    """REAL tmux: a second create_detached with the SAME deterministic name must raise the
    typed SpawnFailure (failure_class='tmux_session_collision'), never a raw CalledProcessError."""
    sock = "harness-test-" + uuid_mod.uuid4().hex[:12]
    prior = tmux_mod._SOCKET
    tmux_mod.set_socket(sock)
    session = "harness-collide-" + uuid_mod.uuid4().hex[:8]
    try:
        tmux_mod.create_detached(session, ["sh", "-c", "sleep 30"], {"CLAUDE_CONFIG_DIR": "/x"})
        with pytest.raises(oauth_guard.SpawnFailure) as excinfo:
            tmux_mod.create_detached(session, ["sh", "-c", "sleep 30"], {"CLAUDE_CONFIG_DIR": "/x"})
        assert excinfo.value.failure_class == "tmux_session_collision", (
            f"a duplicate-session collision must carry its own class; got {excinfo.value.failure_class!r}"
        )
        assert not isinstance(excinfo.value, subprocess.CalledProcessError)
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], check=False, capture_output=True)
        uid = os.getuid()
        for base in (os.environ.get("TMPDIR") or "/tmp", "/tmp", "/private/tmp"):
            try:
                os.unlink(os.path.join(base, f"tmux-{uid}", sock))
            except OSError:
                pass
        tmux_mod.set_socket(prior)


def test_non_collision_create_failure_converts_to_runtime_down(monkeypatch):
    """Any OTHER new-session failure converts to SpawnFailure(runtime_down) — the §6.3 net is
    total for create_detached, never a raw CalledProcessError escaping past the chokepoint."""

    def _boom(args, **kwargs):
        raise subprocess.CalledProcessError(1, ["tmux"] + args, stderr="error connecting to socket")

    monkeypatch.setattr(tmux_mod, "_run", _boom)
    with pytest.raises(oauth_guard.SpawnFailure) as excinfo:
        tmux_mod.create_detached("harness-x", ["sh", "-c", "true"], {"CLAUDE_CONFIG_DIR": "/x"})
    assert excinfo.value.failure_class == "runtime_down"


# ---------------------------------------------------------------------------------------
# (2) The §6.3 net fires on the collision class: claim RELEASED + spawn_failed escalation.
# ---------------------------------------------------------------------------------------

def test_collision_releases_the_claim_and_escalates(runtime):
    rec, token = _seed(state="planned", generation=0)
    adapter = CollidingAdapter()
    chokepoint.set_adapter(adapter)

    result = chokepoint.claim_and_spawn(
        LEAF, expected_state="planned", expected_generation=0,
        expected_owner_token=token, level_config=config.LevelConfig.for_level("L5"),
    )

    assert result.ok is False and result.failure_class == "tmux_session_collision"
    live = ledger.read_binding(LEAF)
    assert live.get("state") == "planned", (
        f"the committed claim must be RELEASED on a collision (§6.3); got {live.get('state')!r}"
    )
    assert live.get("lease_epoch") > rec["lease_epoch"], "the release bumps the epoch (fences the loser)"
    rows = [r for r in ledger.load_wal() if r.get("event") == "spawn_failed"
            and r.get("node_address") == LEAF]
    assert rows, "a spawn_failed escalation row must land (§6.3)"
    assert "tmux_session_collision" in str(rows[-1]), (
        "the escalation names WHICH class fired (tmux_session_collision)"
    )


# ---------------------------------------------------------------------------------------
# (3) The resume path tears the stale prior pane down BEFORE reopening (LT-4 half 2).
# ---------------------------------------------------------------------------------------

def test_resume_kills_the_recorded_stale_pane_before_reopening(runtime):
    stale_target = addressing.session_name_for(LEAF) + ":0.0"
    rec, _token = _seed(state="running", tmux_target=stale_target)
    live_token = ledger.read_binding(LEAF)["owner_token"]
    adapter = FakeAdapter()
    chokepoint.set_adapter(adapter)

    result = chokepoint.resume(
        LEAF, expected_state="running", expected_generation=rec["generation"],
        expected_owner_token=live_token, delta_inputs={},
        level_config=config.LevelConfig.for_level("L5"),
    )

    assert result.ok, f"resume must succeed: {result!r}"
    assert stale_target.split(":", 1)[0] in adapter.tmux.killed, (
        "resume must tear down the FENCED prior incarnation's recorded pane (the uuid-mismatched "
        "leftover the genesis RESUME branch exists for) before reopening the deterministic name "
        "(LR-21: the recorded triple is normalized to its SESSION name — the kill target tmux "
        "actually resolves)"
    )
    kinds = [k for (k, _t) in adapter.tmux.events]
    assert kinds.index("kill") < kinds.index("create"), "the teardown precedes the fresh open"


def test_resume_with_a_lost_claim_kills_nothing(runtime):
    """The teardown runs strictly AFTER a WINNING claim — a lost claim must not kill a pane a
    racing owner may hold."""
    rec, _token = _seed(state="running")
    adapter = FakeAdapter()
    chokepoint.set_adapter(adapter)

    result = chokepoint.resume(
        LEAF, expected_state="running", expected_generation=rec["generation"],
        expected_owner_token="stale-wrong-token", delta_inputs={},
        level_config=config.LevelConfig.for_level("L5"),
    )

    assert result.ok is False and result.failure_class == "claim_lost"
    assert adapter.tmux.killed == [], "a lost claim opens no actor AND kills no pane"


# ---------------------------------------------------------------------------------------
# (4) INT-2 — resume honors the §6.1 STEP-0 pause gate.
# ---------------------------------------------------------------------------------------

def test_resume_refuses_a_paused_subtree_before_claiming(runtime):
    rec, _token = _seed(state="running", paused_at="2026-06-10T00:00:00+00:00")
    before = ledger.read_binding(LEAF)
    adapter = FakeAdapter()
    chokepoint.set_adapter(adapter)

    result = chokepoint.resume(
        LEAF, expected_state="running", expected_generation=rec["generation"],
        expected_owner_token=before["owner_token"], delta_inputs={},
        level_config=config.LevelConfig.for_level("L5"),
    )

    assert result.ok is False and result.failure_class == "paused_subtree", (
        f"resume must refuse a paused subtree like claim_and_spawn STEP0 does; got {result!r}"
    )
    assert adapter.calls == [], "no actor may open for a paused node"
    after = ledger.read_binding(LEAF)
    assert after.get("lease_epoch") == before.get("lease_epoch") and \
        after.get("owner_token") == before.get("owner_token"), (
        "the refusal must land BEFORE the claim — no epoch bump / token re-mint on a paused node "
        "(the human paused to inspect THIS incarnation)"
    )
    assert adapter.tmux.killed == [], "no recovery teardown on a paused subtree either"


def test_resume_paused_ancestor_blocks_the_leaf(runtime):
    _seed(PARENT, state="running", parent=None, paused_at="2026-06-10T00:00:00+00:00")
    rec, _token = _seed(state="running")
    adapter = FakeAdapter()
    chokepoint.set_adapter(adapter)

    result = chokepoint.resume(
        LEAF, expected_state="running", expected_generation=rec["generation"],
        expected_owner_token=ledger.read_binding(LEAF)["owner_token"], delta_inputs={},
        level_config=config.LevelConfig.for_level("L5"),
    )

    assert result.ok is False and result.failure_class == "paused_subtree", (
        "the gate is the node-or-ancestor walk (subtree_paused) — a paused PARENT blocks the leaf"
    )


# ---------------------------------------------------------------------------------------
# (5) The re-register path tears down a terminal child's warm pane (LT-4 case b).
# ---------------------------------------------------------------------------------------

def test_reregister_terminal_child_kills_its_warm_pane(runtime):
    _seed(PARENT, state="running", parent=None)
    stale_target = addressing.session_name_for(LEAF) + ":0.0"
    _seed(LEAF, state="failed", tmux_target=stale_target)
    adapter = FakeAdapter()
    chokepoint.set_adapter(adapter)

    result = chokepoint.register_and_spawn_child(
        PARENT, LEAF, child_level_config=config.LevelConfig.for_level("L5"),
        brief_content="re-run the task",
        # A test_author errand is the L4->L5 shape that legitimately needs no accepted test
        # package (the errand AUTHORS it). The old fixture ducked that gate by mislabeling the
        # parent L5; with the parent honestly L4 the exemption must be honest too.
        child_metadata={"child_purpose": chokepoint.CHILD_PURPOSE_TEST_AUTHOR},
    )

    assert result.ok, f"the re-spawn must succeed: {result!r}"
    assert stale_target.split(":", 1)[0] in adapter.tmux.killed, (
        "re-registering a TERMINAL child (a watchdog-FAILED warm leaf) must tear its recorded "
        "stale pane down before the fresh claim_and_spawn — else 'duplicate session' wedges the "
        "child in an invisible register->claim->crash->necro loop (LR-21: normalized to the "
        "SESSION name tmux actually resolves)"
    )
