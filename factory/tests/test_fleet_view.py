"""F15 — the fleet/tree TEXT VIEW (REMEDIATION; review COMP-4).

harnessctl had only single-node machine reads (`show`) — no way to see the whole supervision tree at a
glance. That is the operator's ONLY situational-awareness surface during a live run (pair it with
attaching to a node's tmux pane to watch it work). This adds an IPC `tree` read + a harnessctl `tree`
subcommand that renders the binding map as an indented supervision tree (address, level, state,
liveness).
"""

import copy

import pytest

import harnessd.fencing as fencing
import harnessd.ipc as ipc
import harnessd.ledger as ledger
import harnessd.harnessctl as harnessctl


@pytest.fixture
def runtime(tmp_path):
    prev = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = prev


def _node(addr, parent, level, state):
    return {"node_address": addr, "parent_address": parent, "level": level, "state": state,
            "liveness_state": "working", "generation": 1, "lease_epoch": 1,
            "owner_token": fencing.mint_owner_token(addr, "sa", "u", 1), "last_applied_seq": 0,
            "subagent_id": "sa", "session_uuid": "u", "tmux_target": "harness:" + addr}


def _seed_tree(runtime):
    nodes = {
        "proj#exec": _node("proj#exec", None, "L1", "running"),
        "proj/widget#exec": _node("proj/widget#exec", "proj#exec", "L2", "running"),
        "proj/widget/parser#exec": _node("proj/widget/parser#exec", "proj/widget#exec", "L3", "running"),
        "proj/widget/render#exec": _node("proj/widget/render#exec", "proj/widget#exec", "L3", "done"),
    }
    ledger.write_binding({k: copy.deepcopy(v) for k, v in nodes.items()}, _lock_held=True)
    return nodes


# --------------------------------------------------------------------------- #
# IPC: a `tree` read returns the whole binding map
# --------------------------------------------------------------------------- #

def test_tree_is_a_known_ipc_read(runtime):
    assert "tree" in ipc._DISPATCH
    _seed_tree(runtime)
    resp = ipc.handle_request({"command": "tree"})
    assert resp["ok"] is True
    assert set(resp["nodes"].keys()) == {
        "proj#exec", "proj/widget#exec", "proj/widget/parser#exec", "proj/widget/render#exec"}


# --------------------------------------------------------------------------- #
# render_tree: the indented supervision tree (the user-facing value)
# --------------------------------------------------------------------------- #

def test_render_tree_shows_hierarchy_states_and_levels():
    assert hasattr(harnessctl, "render_tree") and callable(harnessctl.render_tree)
    nodes = {
        "proj#exec": {"node_address": "proj#exec", "parent_address": None, "level": "L1", "state": "running"},
        "proj/widget#exec": {"node_address": "proj/widget#exec", "parent_address": "proj#exec", "level": "L2", "state": "running"},
        "proj/widget/parser#exec": {"node_address": "proj/widget/parser#exec", "parent_address": "proj/widget#exec", "level": "L3", "state": "running"},
        "proj/widget/render#exec": {"node_address": "proj/widget/render#exec", "parent_address": "proj/widget#exec", "level": "L3", "state": "done"},
    }
    out = harnessctl.render_tree(nodes)
    # every node appears, with its state + level
    for addr in nodes:
        assert addr.split("#")[0].split("/")[-1] in out or addr in out, f"{addr} missing from the tree"
    assert "running" in out and "done" in out, "states must be shown"
    assert "L1" in out and "L3" in out, "levels must be shown"
    # the root is least-indented; a child is MORE indented than its parent (hierarchy is visible)
    lines = out.splitlines()
    def indent(substr):
        for ln in lines:
            if substr in ln:
                return len(ln) - len(ln.lstrip())
        return -1
    assert indent("proj#exec") < indent("proj/widget#exec") < indent("proj/widget/parser#exec"), (
        "the tree must indent children under their parents"
    )


def test_render_tree_handles_empty_and_orphan_gracefully():
    assert harnessctl.render_tree({}).strip() != "" or harnessctl.render_tree({}) == ""  # no crash
    # an orphan (parent not in the map) still renders (not dropped)
    orphan = {"a/b#exec": {"node_address": "a/b#exec", "parent_address": "ghost#exec", "level": "L3", "state": "running"}}
    out = harnessctl.render_tree(orphan)
    assert "a/b#exec" in out or "b" in out, "an orphan node must still appear (never silently dropped)"


# --------------------------------------------------------------------------- #
# harnessctl `tree` subcommand wiring (a CLIENT — serialize only)
# --------------------------------------------------------------------------- #

def test_harnessctl_exposes_tree_subcommand():
    parser = harnessctl.build_parser()
    args = parser.parse_args(["tree"])
    assert args.command == "tree"
    request = harnessctl._build_request(args)
    assert request == {"command": "tree"}
