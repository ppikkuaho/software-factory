"""The B4 §9 illegal-write matrix — one case each, asserting non-zero exit and a
reasoned message that names the violated rule/section (executor brief 0.E)."""

from __future__ import annotations

import json

import pytest

from conftest import (
    Sandbox,
    finalize_gate_decision,
    seed_worked_node,
    transcribe_engine_screen,
)


def _mint_root(sb: Sandbox, premise="A premise", node="1"):
    assert sb.run("node", "mint", "--tree", "L4", "--root", "--premise", premise,
                  "--rationale", "seed", role="director").returncode == 0


def _land_merge_record(sb: Sandbox, node: str) -> str:
    created = sb.run(
        "mrec", "create", "--candidate-ref", f"tree#L4/node#{node}",
        "--lane-verdict", "lane-pass", "--scope-lane", "L4",
        "--lane-adjudication-ref", f"tree#L4/adjudication#d-{node}-1-a1",
        "--screen-result", "required-checks=pass",
        role="harness",
    )
    assert created.returncode == 0, created.stderr
    records = sorted((sb.root / "tier1/merge-records").glob("MR-*.json"))
    record_id = records[-1].stem
    screened = transcribe_engine_screen(sb, record_id)
    assert screened.returncode == 0, screened.stderr
    finalize_gate_decision(sb, record_id)
    return record_id


# --- (1) schema conformance ---------------------------------------------------

def test_schema_nonconforming_detected_by_validate(tree: Sandbox):
    sb = tree
    _mint_root(sb)
    # hand-corrupt to the removed 'active' status (A1 §2.1)
    p = sb.root / "trees/L4/nodes/1/node.json"
    d = json.loads(p.read_text())
    d["status"] = "active"
    p.write_text(json.dumps(d))
    r = sb.run("validate", "--tree", "L4", role="director")
    assert r.returncode == 2
    assert "schema" in r.stderr.lower() and "node.json" in r.stderr


# --- (2) role x field authority ----------------------------------------------

def test_unit_cannot_write_tree_cursor(tree: Sandbox):
    sb = tree
    _mint_root(sb)
    r = sb.run("cursor", "move", "--tree", "L4", "--to", "1", "--rationale", "x", role="unit")
    assert r.returncode != 0
    # both director-owned fields a cursor move touches are listed, sorted + complete
    assert "unit" in r.stderr and "A1 §10" in r.stderr
    assert "tree.cursor" in r.stderr and "tree.decision_log" in r.stderr


def test_unit_cannot_create_ledger(tree: Sandbox):
    r = tree.run("ledger", "create", "--section", "research", "--text", "x", role="unit")
    assert r.returncode != 0
    assert "unit" in r.stderr and "A1 §10" in r.stderr


def test_unit_cannot_write_node_nav(tree: Sandbox):
    sb = tree
    _mint_root(sb)
    r = sb.run("node", "close", "--node", "1", "--reason", "x", role="unit")
    assert r.returncode != 0
    assert "unit" in r.stderr and "status" in r.stderr


def test_director_cannot_set_standing(tree: Sandbox):
    sb = tree
    _mint_root(sb)
    r = sb.run("standing", "set", "--node", "1", "--standing", "supported",
               "--note", "x", role="director")
    assert r.returncode != 0
    assert "director" in r.stderr and "standing" in r.stderr


def test_verifier_cannot_edit_status_via_close(tree: Sandbox):
    sb = tree
    seed_worked_node(sb)  # node 1 is worked
    r = sb.run("node", "close", "--node", "1", "--reason", "x", role="verifier")
    assert r.returncode != 0
    assert "verifier" in r.stderr and "status" in r.stderr


def test_verifier_cannot_create_node_premise(tree: Sandbox):
    r = tree.run("node", "mint", "--tree", "L4", "--root", "--premise", "x",
                 "--rationale", "y", role="verifier")
    assert r.returncode != 0
    assert "verifier" in r.stderr and "create a node" in r.stderr


# --- (3) anchors --------------------------------------------------------------

def test_claim_zero_anchors_rejected(tree: Sandbox):
    sb = tree
    dispatch = seed_worked_node(sb, "1")
    # a second dispatch to grant a fresh claim with no anchors
    sb.run("dispatch", "create", "--node", "1", "--question", "q2",
           "--done-definition", "d", role="director")
    sb.write_file("r.md", "a\nb\nc\n")
    sb.run("report", "submit", "--dispatch", "d-1-2", "--src", str(sb.root / "r.md"), role="unit")
    r = sb.run("claim", "grant", "--dispatch", "d-1-2", "--text", "x",
               "--proposed-tier", "1", "--granted-tier", "1", "--standing-class", "trunk", role="verifier")
    assert r.returncode != 0
    assert "anchor" in r.stderr.lower()


def test_claim_bad_anchor_path_rejected(tree: Sandbox):
    sb = tree
    dispatch = seed_worked_node(sb, "1")
    sb.run("dispatch", "create", "--node", "1", "--question", "q2",
           "--done-definition", "d", role="director")
    sb.write_file("r.md", "a\nb\nc\n")
    sb.run("report", "submit", "--dispatch", "d-1-2", "--src", str(sb.root / "r.md"), role="unit")
    r = sb.run("claim", "grant", "--dispatch", "d-1-2", "--text", "x",
               "--proposed-tier", "1", "--granted-tier", "1", "--standing-class", "trunk",
               "--anchor", "trees/L4/nodes/1/reports/does-not-exist.md:1:2", role="verifier")
    assert r.returncode != 0
    assert "anchor" in r.stderr.lower() and "resolve" in r.stderr.lower()


def test_claim_bad_anchor_range_rejected(tree: Sandbox):
    sb = tree
    dispatch = seed_worked_node(sb, "1")
    # anchor into the existing report but past its line count
    r = sb.run("dispatch", "create", "--node", "1", "--question", "q2",
               "--done-definition", "d", role="director")
    sb.write_file("r.md", "only one line\n")
    sb.run("report", "submit", "--dispatch", "d-1-2", "--src", str(sb.root / "r.md"), role="unit")
    r = sb.run("claim", "grant", "--dispatch", "d-1-2", "--text", "x",
               "--proposed-tier", "1", "--granted-tier", "1", "--standing-class", "trunk",
               "--anchor", "trees/L4/nodes/1/reports/d-1-2-report.md:1:50", role="verifier")
    assert r.returncode != 0
    assert "range" in r.stderr.lower() or "resolve" in r.stderr.lower()


# --- (4) tier -----------------------------------------------------------------

def test_granted_tier_above_proposed_rejected(tree: Sandbox):
    sb = tree
    _mint_root(sb)
    sb.run("dispatch", "create", "--node", "1", "--question", "q",
           "--done-definition", "d", role="director")
    sb.write_file("r.md", "a\nb\nc\n")
    sb.run("report", "submit", "--dispatch", "d-1-1", "--src", str(sb.root / "r.md"), role="unit")
    r = sb.run("claim", "grant", "--dispatch", "d-1-1", "--text", "x",
               "--proposed-tier", "2", "--granted-tier", "3", "--standing-class", "trunk",
               "--anchor", "trees/L4/nodes/1/reports/d-1-1-report.md:1:2", role="verifier")
    assert r.returncode != 0
    assert "granted_tier 3 > proposed_tier 2" in r.stderr and "B4 §9" in r.stderr


# --- (6) illegal status transitions ------------------------------------------

def test_merge_unexplored_rejected(tree: Sandbox):
    sb = tree
    seed_worked_node(sb, "1")
    record_id = _land_merge_record(sb, "1")
    # A current merge record can only be created for a claim-backed candidate.
    # Put the otherwise valid synthetic candidate into the illegal pre-merge
    # state directly so this matrix row still isolates the B4 status guard.
    node_path = sb.root / "trees/L4/nodes/1/node.json"
    node = json.loads(node_path.read_text(encoding="utf-8"))
    node["status"] = "unexplored"
    node_path.write_text(json.dumps(node), encoding="utf-8")
    r = sb.run(
        "node", "merge", "--node", "1", "--merge-record", record_id,
        role="director",
    )
    assert r.returncode != 0
    assert "worked" in r.stderr and "B4 §9" in r.stderr


def test_park_unexplored_rejected(tree: Sandbox):
    sb = tree
    _mint_root(sb)
    r = sb.run("node", "park", "--node", "1", "--rationale", "x", role="director")
    assert r.returncode != 0
    assert "worked" in r.stderr and "A1 §2.1" in r.stderr


def test_close_parked_bypassing_settle_rejected(tree: Sandbox):
    sb = tree
    seed_worked_node(sb, "1")
    assert sb.run("node", "park", "--node", "1", "--rationale", "x", role="director").returncode == 0
    r = sb.run("node", "close", "--node", "1", "--reason", "y", role="director")
    assert r.returncode != 0
    assert "settle" in r.stderr and "A1 §2.1" in r.stderr


# --- (7) merge-block on a pending sibling conflict ---------------------------

def _worked_child(sb: Sandbox, parent: str, node_id: str, ordinal: int):
    assert sb.run("node", "mint", "--tree", "L4", "--parent", parent,
                  "--premise", f"child {node_id}", "--rationale", "c", role="director").returncode == 0
    did = f"d-{node_id}-1"
    assert sb.run("dispatch", "create", "--node", node_id, "--question", "q",
                  "--done-definition", "d", role="director").returncode == 0
    sb.write_file(f"rep{ordinal}.md", "a\nb\nc\n")
    assert sb.run("report", "submit", "--dispatch", did, "--src",
                  str(sb.root / f"rep{ordinal}.md"), role="unit").returncode == 0
    assert sb.run("claim", "grant", "--dispatch", did, "--text", "f",
                  "--proposed-tier", "1", "--granted-tier", "1", "--standing-class", "trunk",
                  "--anchor", f"trees/L4/nodes/{node_id}/reports/{did}-report.md:1:2",
                  role="verifier").returncode == 0


def test_merge_blocked_by_pending_sibling_conflict(tree: Sandbox):
    sb = tree
    _mint_root(sb, node="1")
    _worked_child(sb, "1", "1.1", 1)
    _worked_child(sb, "1", "1.2", 2)
    # park 1.2 -> pending conflict
    assert sb.run("node", "park", "--node", "1.2", "--rationale", "swap", role="director").returncode == 0
    record_id = _land_merge_record(sb, "1.1")
    # merge 1.1 blocked by sibling 1.2's pending conflict
    r = sb.run(
        "node", "merge", "--node", "1.1", "--merge-record", record_id,
        role="director",
    )
    assert r.returncode != 0
    assert "sibling 1.2" in r.stderr and "merge-block" in r.stderr
    # positive control: settle the sibling, then the merge lands
    assert sb.run("settle", "--node", "1.2", "--resolution", "closed",
                  "--rationale", "done", role="director").returncode == 0
    ok = sb.run(
        "node", "merge", "--node", "1.1", "--merge-record", record_id,
        role="director",
    )
    assert ok.returncode == 0, ok.stderr
    assert sb.load("trees/L4/tree.json")["epoch"] == 1


# --- (8) report freeze (tool level) ------------------------------------------

def test_report_resubmission_rejected(tree: Sandbox):
    sb = tree
    _mint_root(sb)
    sb.run("dispatch", "create", "--node", "1", "--question", "q",
           "--done-definition", "d", role="director")
    sb.write_file("r.md", "a\nb\n")
    assert sb.run("report", "submit", "--dispatch", "d-1-1", "--src",
                  str(sb.root / "r.md"), role="unit").returncode == 0
    r = sb.run("report", "submit", "--dispatch", "d-1-1", "--src",
               str(sb.root / "r.md"), role="unit")
    assert r.returncode != 0
    assert "frozen" in r.stderr and ("U2" in r.stderr or "B4 §9" in r.stderr)


# --- (9) archive overwrite ----------------------------------------------------

def test_archive_overwrite_rejected(tree: Sandbox):
    sb = tree
    _mint_root(sb)
    sb.run("dispatch", "create", "--node", "1", "--question", "q",
           "--done-definition", "d", role="director")
    sb.write_file("a.txt", "one\n")
    assert sb.run("archive", "write", "--dispatch", "d-1-1", "--src",
                  str(sb.root / "a.txt"), "--name", "x.txt", role="unit").returncode == 0
    r = sb.run("archive", "write", "--dispatch", "d-1-1", "--src",
               str(sb.root / "a.txt"), "--name", "x.txt", role="unit")
    assert r.returncode != 0
    assert "write-once" in r.stderr and "U2" in r.stderr


# --- (10) ledger creation authority ------------------------------------------

def test_director_cannot_create_research_ledger(tree: Sandbox):
    r = tree.run("ledger", "create", "--section", "research", "--text", "idea", role="director")
    assert r.returncode != 0
    assert "director" in r.stderr and "verifier" in r.stderr and "A1 §10" in r.stderr


def test_unit_cannot_create_user_ledger(tree: Sandbox):
    r = tree.run("ledger", "create", "--section", "user", "--text", "idea", role="unit")
    assert r.returncode != 0
    assert "unit" in r.stderr


def test_user_section_create_succeeds(tree: Sandbox):
    r = tree.run("ledger", "create", "--section", "user", "--text", "a user idea", role="user")
    assert r.returncode == 0, r.stderr
    entry = tree.load("ledger/top/user/L-1.json")
    assert entry["section"] == "user" and entry["status"]["state"] == "open"
