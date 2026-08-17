"""Parent-spawns-child bridge — load-bearing STRENGTHENING (verify gate).

The review found expected_parent_owner_token was threaded but DEAD (never compared to the parent's live
owner_token) -> any caller could spawn under any live parent, not only one it owns (a supervision-tree
integrity gap). Fixed: register_and_spawn_child now fences on the presented parent token (optional, like
deliver()). Pin both directions: a WRONG parent token is refused (no child registered); the CORRECT
token (and None) still spawns.
"""

import copy

import pytest

import harnessd.config as config
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.spawn.chokepoint as chokepoint


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


class _FakeAdapter:
    def __init__(self):
        self.calls = []

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append(tmux_target)
        from harnessd.spawn.adapters.base import SpawnResult
        return SpawnResult(ok=True, session_uuid="s", model_used="m", role_variant="L3",
                           system_prompt_file="operational/shared/system-prompt.md",
                           system_prompt_file_hash="h", tmux_target=tmux_target,
                           transcript_path="/tmp/s.jsonl", failure_class=None)


PARENT = "proj/widget#exec"
CHILD = "proj/widget/mod#exec"


def _seed_live_parent():
    token = fencing.mint_owner_token(PARENT, "sa", "uuid", 2)
    rec = {"node_address": PARENT, "parent_address": "root#exec", "level": "L2", "subagent_id": "sa",
           "session_uuid": "uuid", "state": "running", "generation": 5, "lease_epoch": 2,
           "owner_token": token, "last_applied_seq": 0, "liveness_state": "working",
           "tmux_target": "harness:proj-widget-exec"}
    ledger.write_binding({PARENT: copy.deepcopy(rec)}, _lock_held=True)
    return token


def _install(fake):
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(fake)
    else:
        chokepoint.ADAPTER = fake


def test_wrong_parent_token_is_refused_no_child_registered(runtime):
    """A caller presenting a WRONG parent owner_token must be REFUSED before any child is registered —
    an agent can only spawn under ITS OWN node, not a sibling/cousin subtree."""
    _seed_live_parent()
    fake = _FakeAdapter(); _install(fake)
    lc = config.get_level_config("L3")
    res = chokepoint.register_and_spawn_child(
        PARENT, CHILD, child_level_config=lc, brief_content="design the module",
        expected_parent_owner_token="NOT-THE-PARENTS-TOKEN")
    assert not getattr(res, "ok", True), "a wrong parent token must be refused"
    assert ledger.read_binding(CHILD) is None, "no child must be registered on a parent-fence refusal"
    assert len(fake.calls) == 0, "no actor opened on a parent-fence refusal"


def test_correct_parent_token_spawns_the_child(runtime):
    """The CORRECT parent owner_token (the parent owns itself) spawns the child."""
    parent_token = _seed_live_parent()
    fake = _FakeAdapter(); _install(fake)
    lc = config.get_level_config("L3")
    res = chokepoint.register_and_spawn_child(
        PARENT, CHILD, child_level_config=lc, brief_content="design the module",
        expected_parent_owner_token=parent_token)
    assert getattr(res, "ok", False), "the correct parent token must spawn the child"
    child = ledger.read_binding(CHILD)
    assert child is not None and child["parent_address"] == PARENT, "child registered under the parent"


def test_none_parent_token_still_spawns_daemon_internal(runtime):
    """A None parent token (daemon-internal / genesis-style) is unfenced and still spawns (the EX lock +
    local IPC are the bound) — the optional-fence pattern."""
    _seed_live_parent()
    fake = _FakeAdapter(); _install(fake)
    lc = config.get_level_config("L3")
    res = chokepoint.register_and_spawn_child(PARENT, CHILD, child_level_config=lc,
                                              brief_content="x", expected_parent_owner_token=None)
    assert getattr(res, "ok", False), "a None parent token (daemon-internal) still spawns"
