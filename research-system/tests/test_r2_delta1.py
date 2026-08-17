"""Checkpoint-1 additions: R2 dispatch-create status guard, and DELTA-1 claim
rejection encoding."""

from __future__ import annotations

import json

from conftest import (
    Sandbox,
    finalize_gate_decision,
    seed_worked_node,
    transcribe_engine_screen,
)


def _mint_root(sb: Sandbox, premise="A premise"):
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


# --- R2: dispatch create requires status in {unexplored, worked} --------------

def test_r2_dispatch_on_closed_rejected(tree: Sandbox):
    """director seam ruling R2, checkpoint-1 verdict 2026-07-07, recorded in
    RESEARCH-SYSTEM-HANDOFF-2026-07-07.md; A1 amendment pending. A closed node may
    not be dispatched."""
    sb = tree
    _mint_root(sb)
    assert sb.run("node", "close", "--node", "1", "--reason", "deprioritized",
                  role="director").returncode == 0
    r = sb.run("dispatch", "create", "--node", "1", "--question", "q",
               "--done-definition", "d", role="director")
    assert r.returncode != 0
    assert "closed" in r.stderr and "R2" in r.stderr


def test_r2_dispatch_on_merged_rejected(tree: Sandbox):
    """director seam ruling R2, checkpoint-1 verdict 2026-07-07, recorded in
    RESEARCH-SYSTEM-HANDOFF-2026-07-07.md; A1 amendment pending. A merged (terminal)
    node may not be dispatched."""
    sb = tree
    seed_worked_node(sb, "1")
    record_id = _land_merge_record(sb, "1")
    assert sb.run(
        "node", "merge", "--node", "1", "--merge-record", record_id,
        role="director",
    ).returncode == 0
    assert sb.load("trees/L4/nodes/1/node.json")["status"] == "merged"
    r = sb.run("dispatch", "create", "--node", "1", "--question", "q",
               "--done-definition", "d", role="director")
    assert r.returncode != 0
    assert "merged" in r.stderr and "R2" in r.stderr


def test_r2_dispatch_on_parked_rejected(tree: Sandbox):
    """director seam ruling R2, checkpoint-1 verdict 2026-07-07, recorded in
    RESEARCH-SYSTEM-HANDOFF-2026-07-07.md; A1 amendment pending. A parked node exits
    only via settle and may not be dispatched."""
    sb = tree
    seed_worked_node(sb, "1")
    assert sb.run("node", "park", "--node", "1", "--rationale", "swap",
                  role="director").returncode == 0
    r = sb.run("dispatch", "create", "--node", "1", "--question", "q",
               "--done-definition", "d", role="director")
    assert r.returncode != 0
    assert "parked" in r.stderr and "R2" in r.stderr


def test_r2_dispatch_on_worked_and_unexplored_allowed(tree: Sandbox):
    """Positive control: unexplored and worked nodes ARE dispatchable."""
    sb = tree
    _mint_root(sb)  # node 1 unexplored
    assert sb.run("dispatch", "create", "--node", "1", "--question", "q",
                  "--done-definition", "d", role="director").returncode == 0  # unexplored ok
    # drive to worked, then a second dispatch is also allowed
    sb.write_file("r.md", "a\nb\nc\n")
    assert sb.run("report", "submit", "--dispatch", "d-1-1", "--src",
                  str(sb.root / "r.md"), role="unit").returncode == 0
    assert sb.run("claim", "grant", "--dispatch", "d-1-1", "--text", "f",
                  "--proposed-tier", "1", "--granted-tier", "1", "--standing-class", "trunk",
                  "--anchor", "trees/L4/nodes/1/reports/d-1-1-report.md:1:2",
                  role="verifier").returncode == 0
    assert sb.load("trees/L4/nodes/1/node.json")["status"] == "worked"
    assert sb.run("dispatch", "create", "--node", "1", "--question", "q2",
                  "--done-definition", "d", role="director").returncode == 0  # worked ok


# --- DELTA-1: outright claim rejection (B4 §2 third verdict) ------------------

def _ready_dispatch(sb: Sandbox, node: str = "1") -> str:
    """Mint a node, create a dispatch, submit a report (no claim yet)."""
    _mint_root(sb)
    assert sb.run("dispatch", "create", "--node", node, "--question", "q",
                  "--done-definition", "d", role="director").returncode == 0
    sb.write_file("r.md", "line one\nline two\nline three\n")
    assert sb.run("report", "submit", "--dispatch", f"d-{node}-1", "--src",
                  str(sb.root / "r.md"), role="unit").returncode == 0
    return f"d-{node}-1"


def test_delta1_rejection_records_reason_and_creates_no_claim(tree: Sandbox):
    sb = tree
    did = _ready_dispatch(sb, "1")
    r = sb.run("claim", "reject", "--dispatch", did, "--text", "over-scoped claim",
               "--reason", "the anchored evidence does not support the stated scope",
               role="verifier")
    assert r.returncode == 0, r.stderr  # rejection is a legal verifier act, not an error

    node = sb.load("trees/L4/nodes/1/node.json")
    assert node["claims"] == []  # NO claim created (A1 §4 has no 'rejected' status)
    assert node["status"] == "unexplored"  # a rejection moves nothing

    disp = sb.load("trees/L4/nodes/1/dispatches/d-1-1.json")
    assert len(disp["adjudications"]) == 1  # the rejection is referenced
    record_ref = disp["adjudications"][0]
    assert record_ref == "tree#L4/adjudication#d-1-1-a1"
    record = (
        sb.root / "trees/L4/nodes/1/adjudications/d-1-1-a1.md"
    ).read_text()
    header = json.loads(record.splitlines()[0].removeprefix("HT-ADJUDICATION "))
    assert header["verdict"] == "rejected"
    assert header["claim_ref"] is None
    assert "verdict: rejected" in record
    assert "the anchored evidence does not support the stated scope" in record


def test_delta1_reject_then_grant_still_flips(tree: Sandbox):
    """A rejection must not suppress a later grant's unexplored->worked flip."""
    sb = tree
    did = _ready_dispatch(sb, "1")
    assert sb.run("claim", "reject", "--dispatch", did, "--text", "bad claim",
                  "--reason", "insufficient", role="verifier").returncode == 0
    assert sb.load("trees/L4/nodes/1/node.json")["status"] == "unexplored"
    assert sb.run("claim", "grant", "--dispatch", did, "--text", "good claim",
                  "--proposed-tier", "1", "--granted-tier", "1", "--standing-class", "trunk",
                  "--anchor", "trees/L4/nodes/1/reports/d-1-1-report.md:1:2",
                  role="verifier").returncode == 0
    node = sb.load("trees/L4/nodes/1/node.json")
    assert node["status"] == "worked" and len(node["claims"]) == 1
    disp = sb.load("trees/L4/nodes/1/dispatches/d-1-1.json")
    assert len(disp["adjudications"]) == 2  # reject record + grant ref
    assert disp["adjudications"] == [
        "tree#L4/adjudication#d-1-1-a1",
        "tree#L4/adjudication#d-1-1-a2",
    ]
    grant_record = (
        sb.root / "trees/L4/nodes/1/adjudications/d-1-1-a2.md"
    ).read_text()
    grant_header = json.loads(
        grant_record.splitlines()[0].removeprefix("HT-ADJUDICATION ")
    )
    assert grant_header["verdict"] == "granted"
    assert grant_header["claim_ref"] == "tree#L4/claim#c-1-1"


def test_grant_demotion_uses_same_versioned_adjudication_record(tree: Sandbox):
    did = _ready_dispatch(tree, "1")
    demoted = tree.run(
        "claim", "grant", "--dispatch", did,
        "--text", "synthetic demoted claim",
        "--proposed-tier", "3", "--granted-tier", "1",
        "--reason", "synthetic tier reduction reason",
        "--standing-class", "trunk",
        "--anchor", "trees/L4/nodes/1/reports/d-1-1-report.md:1:2",
        role="verifier",
    )
    assert demoted.returncode == 0, demoted.stderr
    record = (
        tree.root / "trees/L4/nodes/1/adjudications/d-1-1-a1.md"
    ).read_text()
    header = json.loads(record.splitlines()[0].removeprefix("HT-ADJUDICATION "))
    assert header["schema_version"] == "ht-adjudication/1.0.0"
    assert header["verdict"] == "demoted"
    assert header["proposed_tier"] == 3
    assert header["granted_tier"] == 1
    assert header["reason"] == "synthetic tier reduction reason"


def test_delta1_reject_requires_submitted_report(tree: Sandbox):
    sb = tree
    _mint_root(sb)
    assert sb.run("dispatch", "create", "--node", "1", "--question", "q",
                  "--done-definition", "d", role="director").returncode == 0
    # no report submitted yet
    r = sb.run("claim", "reject", "--dispatch", "d-1-1", "--text", "x",
               "--reason", "y", role="verifier")
    assert r.returncode != 0
    assert "no report submitted" in r.stderr and "B4 §2" in r.stderr
