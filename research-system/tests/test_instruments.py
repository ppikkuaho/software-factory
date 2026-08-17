"""Case (5): epoch + instrument legality (B4 §9). U1 — prove the REAL registry
mechanism fires BOTH ways: rejects illegal, admits legal."""

from __future__ import annotations

import json

from conftest import Sandbox


def _ready_dispatch(sb: Sandbox, node: str = "1") -> tuple[str, str]:
    """Mint a root node, create a dispatch, submit a report. Return (dispatch, anchor)."""
    assert sb.run("node", "mint", "--tree", "L4", "--root", "--premise", "P",
                  "--rationale", "seed", role="director").returncode == 0
    assert sb.run("dispatch", "create", "--node", node, "--question", "q",
                  "--done-definition", "d", role="director").returncode == 0
    did = f"d-{node}-1"
    sb.write_file("r.md", "line one\nline two\nline three\n")
    assert sb.run("report", "submit", "--dispatch", did, "--src",
                  str(sb.root / "r.md"), role="unit").returncode == 0
    return did, f"trees/L4/nodes/{node}/reports/{did}-report.md:1:2"


def _write_registry(sb: Sandbox, suites: list[dict]) -> None:
    sb.write_file("system/instruments/registry.json", json.dumps({"suites": suites}))


def test_epoch_above_tree_epoch_rejected(tree: Sandbox):
    sb = tree
    did, anchor = _ready_dispatch(sb)
    r = sb.run("claim", "grant", "--dispatch", did, "--text", "x", "--proposed-tier", "1",
               "--granted-tier", "1", "--standing-class", "trunk", "--epoch", "5", "--anchor", anchor, role="verifier")
    assert r.returncode != 0
    assert "epoch stamp 5 outside legal range 0..0" in r.stderr


def test_instrument_absent_from_registry_rejected(tree: Sandbox):
    sb = tree  # registry ships empty
    did, anchor = _ready_dispatch(sb)
    r = sb.run("claim", "grant", "--dispatch", did, "--text", "x", "--proposed-tier", "1",
               "--granted-tier", "1", "--standing-class", "trunk", "--instrument", "suite-S@v3", "--anchor", anchor,
               role="verifier")
    assert r.returncode != 0
    assert "absent from registry" in r.stderr and "B4 §9" in r.stderr


def test_instrument_epoch_outside_suite_range_rejected(tree: Sandbox):
    sb = tree
    # synthetic suite legal only for epochs 2..5; claim at epoch 0 is outside it
    _write_registry(sb, [{
        "id": "suite-S", "version": "v3", "noise_floor": 0.01,
        "invocation": "run-suite-s", "epochs": {"from": 2, "to": 5},
    }])
    did, anchor = _ready_dispatch(sb)
    r = sb.run("claim", "grant", "--dispatch", did, "--text", "x", "--proposed-tier", "1",
               "--granted-tier", "1", "--standing-class", "trunk", "--instrument", "suite-S@v3", "--anchor", anchor,
               role="verifier")
    assert r.returncode != 0
    assert "not legal at epoch 0" in r.stderr and "2..5" in r.stderr


def test_instrument_legal_accepted_positive_control(tree: Sandbox):
    sb = tree
    _write_registry(sb, [{
        "id": "suite-S", "version": "v3", "noise_floor": 0.01,
        "invocation": "run-suite-s", "epochs": {"from": 0, "to": 5},
    }])
    did, anchor = _ready_dispatch(sb)
    r = sb.run("claim", "grant", "--dispatch", did, "--text", "legal claim",
               "--proposed-tier", "1", "--granted-tier", "1", "--standing-class", "trunk", "--instrument", "suite-S@v3",
               "--anchor", anchor, role="verifier")
    assert r.returncode == 0, r.stderr
    node = sb.load("trees/L4/nodes/1/node.json")
    assert node["claims"][0]["instruments"] == ["suite-S@v3"]
    assert node["status"] == "worked"
