"""Deterministic brief DERIVATION (FORK-BRIEF-DERIVATION) — the canonical default.

The canonical model (WORKSPACE-SCHEMA:221, ARCHITECTURE.md:122): L4 PRE-AUTHORS the child's `brief.md`
(pointer-not-payload: the responsible-ID-set + spec references + constraints) and the acceptance-authoring path
authors the frozen `acceptance.md` INTO the child node — both BEFORE spawn. The spawn then just brings
the prepared node online: the child binding carries `spec_pointer -> <node>/brief.md` and
`frozen_acceptance_ref -> <node>/acceptance.md`, derived from the node's own files. A free-form
`brief_content` is the OVERRIDE (the exception), not the default.

These pin: (1) the register sets the two pointers; (2) a pre-authored brief.md is NOT overwritten by a
no-override spawn (the derivation default); (3) an override DOES write brief.md; (4) neither present ->
a manifest stub so the node is never empty.
"""

import copy

import pytest

import harnessd.config as config
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.addressing as addressing
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
        self.calls.append(neutral_brief)
        from harnessd.spawn.adapters.base import SpawnResult
        return SpawnResult(ok=True, session_uuid="s", model_used="m", role_variant="L3",
                           system_prompt_file="operational/shared/system-prompt.md",
                           system_prompt_file_hash="h", tmux_target=tmux_target,
                           transcript_path="/tmp/s.jsonl", failure_class=None)


def _install(fake):
    if hasattr(chokepoint, "set_adapter"):
        chokepoint.set_adapter(fake)
    else:
        chokepoint.ADAPTER = fake


PARENT = "proj/widget#exec"
CHILD = "proj/widget/parser#exec"


def _seed_live_parent(runtime):
    token = fencing.mint_owner_token(PARENT, "sa", "uuid", 2)
    ws = addressing.node_dir(PARENT, runtime); ws.mkdir(parents=True, exist_ok=True)
    rec = {"node_address": PARENT, "parent_address": "root#exec", "level": "L2", "subagent_id": "sa",
           "session_uuid": "uuid", "state": "running", "generation": 5, "lease_epoch": 2,
           "owner_token": token, "last_applied_seq": 0, "liveness_state": "working",
           "tmux_target": "harness:" + PARENT, "workspace": str(ws)}
    ledger.write_binding({PARENT: copy.deepcopy(rec)}, _lock_held=True)
    return token


def _spawn(brief_content):
    return chokepoint.register_and_spawn_child(
        PARENT, CHILD, child_level_config=config.get_level_config("L3"),
        brief_content=brief_content, expected_parent_owner_token=None)


def test_register_sets_brief_and_acceptance_pointers(runtime):
    """The child binding carries spec_pointer -> <node>/brief.md and frozen_acceptance_ref ->
    <node>/acceptance.md — the derivation source, read off the node's OWN files."""
    _seed_live_parent(runtime)
    _install(_FakeAdapter())
    res = _spawn(brief_content=None)
    assert getattr(res, "ok", False)
    child = ledger.read_binding(CHILD)
    node = addressing.node_dir(CHILD, runtime)
    assert child["spec_pointer"] == str(node / "brief.md")
    assert child["frozen_acceptance_ref"] == str(node / "acceptance.md")


def test_preauthored_brief_is_not_overwritten_by_default_spawn(runtime):
    """THE default: L4 pre-authored brief.md into the (nested) child node; a spawn with NO override
    must NOT clobber it — the node is already prepared, the spawn only brings it online."""
    _seed_live_parent(runtime)
    _install(_FakeAdapter())
    node = addressing.node_dir(CHILD, runtime); node.mkdir(parents=True, exist_ok=True)
    (node / "brief.md").write_text("# L4-AUTHORED pointer-not-payload brief\nserves: R-002.1.a\n", encoding="utf-8")
    (node / "acceptance.md").write_text("frozen tests by the acceptance-authoring path", encoding="utf-8")

    res = _spawn(brief_content=None)

    assert getattr(res, "ok", False)
    assert (node / "brief.md").read_text("utf-8").startswith("# L4-AUTHORED"), \
        "a pre-authored brief.md must NOT be overwritten by a no-override spawn (derivation default)"


def test_brief_override_writes_brief_md(runtime):
    """The OVERRIDE (the exception): a brief_content given writes brief.md = manifest + task."""
    _seed_live_parent(runtime)
    _install(_FakeAdapter())
    node = addressing.node_dir(CHILD, runtime)

    res = _spawn(brief_content="implement the manifest-ordering task")

    assert getattr(res, "ok", False)
    body = (node / "brief.md").read_text("utf-8")
    assert "implement the manifest-ordering task" in body, "the override task must land in brief.md"
    assert "## Launch Packet" in body, "pilot-role override briefs point at the generated launch packet"
    assert ".reference-map.md" in body, "pilot-role override briefs point at the optional reference map"
    assert "Parent task note:" in body
    assert "does not replace `.launch-packet.md`" in body
    assert "Start from `.launch-packet.md` and its `Task Package`" in body


def test_no_brief_and_no_preauthored_writes_a_manifest_stub(runtime):
    """Neither an override NOR a pre-authored brief -> a manifest-only stub so the node is never empty
    (an L4 that forgot to pre-author still gets the load-manifest; the agent can escalate the gap)."""
    _seed_live_parent(runtime)
    _install(_FakeAdapter())
    node = addressing.node_dir(CHILD, runtime)

    res = _spawn(brief_content=None)

    assert getattr(res, "ok", False)
    assert (node / "brief.md").exists(), "a stub brief.md must exist when nothing was pre-authored"
    body = (node / "brief.md").read_text("utf-8")
    assert "## Launch Packet" in body
    assert ".reference-map.md" in body
    assert "assignment is carried by `.launch-packet.md`" in body
    assert "escalate the gap to your parent rather than guessing the task" not in body


def test_neutral_contract_surfaces_the_derived_pointers(runtime):
    """The spawned actor's neutral brief carries the derived spec_pointer + frozen_acceptance_ref
    (so a real agent reads its node's brief.md + acceptance.md in place)."""
    _seed_live_parent(runtime)
    fake = _FakeAdapter(); _install(fake)
    node = addressing.node_dir(CHILD, runtime)

    _spawn(brief_content=None)

    assert len(fake.calls) == 1
    neutral = fake.calls[0]
    # the adapter receives a brief payload (dict) or NeutralContract carrying the pointers
    sp = neutral.get("spec_pointer") if isinstance(neutral, dict) else getattr(neutral, "spec_pointer", None)
    fa = neutral.get("frozen_acceptance_ref") if isinstance(neutral, dict) else getattr(neutral, "frozen_acceptance_ref", None)
    assert sp == str(node / "brief.md") and fa == str(node / "acceptance.md")
