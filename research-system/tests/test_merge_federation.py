"""Item-1 merge-record gates, global epochs, and staleness federation."""

from __future__ import annotations

import json

import pytest

from conftest import (
    Sandbox,
    finalize_gate_decision,
    seed_mrec_candidate,
    seed_worked_node,
    transcribe_engine_screen,
)
from ht import schemas
from ht.commands import _common
from ht.commands._common import Ctx
from ht.errors import HtError
from ht.mutex import global_mutex
from ht.paths import Root


def _create_record(
    sb: Sandbox,
    *,
    candidate: str = "tree#L4/node#1",
    verdict: str | None = "land",
) -> str:
    adjudication_ref = seed_mrec_candidate(sb, candidate)
    created = sb.run(
        "mrec",
        "create",
        "--candidate-ref",
        candidate,
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        candidate.removeprefix("tree#").split("/node#", 1)[0],
        "--screen-result",
        "required-checks=pass",
        role="harness",
    )
    assert created.returncode == 0, created.stderr
    records = sorted((sb.root / "tier1/merge-records").glob("MR-*.json"))
    record_id = records[-1].stem
    screened = transcribe_engine_screen(sb, record_id)
    assert screened.returncode == 0, screened.stderr
    if verdict is not None:
        notes = {
            "hold": "Hold until all failed conditions are cleared to unblock merge.",
            "bounce-for-surface-rework": (
                "Bounce for surface rework; queue-adjacency must also be resolved."
            ),
            "consolidate-first": (
                "Consolidate pending merge records MR-1, MR-2, and MR-3 first."
            ),
        }
        finalize_gate_decision(
            sb,
            record_id,
            verdict=verdict,
            note=notes.get(verdict),
        )
    return record_id


def _merge(sb: Sandbox, record_id: str, *, tree: str = "L4", node: str = "1"):
    return sb.run(
        "node",
        "merge",
        "--tree",
        tree,
        "--node",
        node,
        "--merge-record",
        record_id,
        role="director",
    )


def test_merge_requires_existing_merge_record(tree: Sandbox):
    seed_worked_node(tree)
    result = _merge(tree, "MR-999")
    assert result.returncode != 0
    assert "no such merge record 'MR-999'" in result.stderr
    assert tree.load("trees/L4/nodes/1/node.json")["status"] == "worked"


@pytest.mark.parametrize("record_id", ["/tmp/evil", "../../evil", "MR-x"])
def test_merge_rejects_path_like_or_malformed_record_ids(
    tree: Sandbox, record_id: str
):
    seed_worked_node(tree)
    result = _merge(tree, record_id)
    assert result.returncode != 0
    assert "merge-record id must have the form MR-<n>" in result.stderr
    assert tree.load("trees/L4/nodes/1/node.json")["status"] == "worked"
    assert tree.load("trees/L4/tree.json")["epoch"] == 0


def test_merge_cli_requires_merge_record_argument(tree: Sandbox):
    result = tree.run("node", "merge", "--node", "1", role="director")
    assert result.returncode != 0
    assert "--merge-record" in result.stderr


def test_merge_rejects_canonical_candidate_mismatch(tree: Sandbox):
    seed_worked_node(tree)
    record_id = _create_record(tree, candidate="tree#other/node#1")
    result = _merge(tree, record_id)
    assert result.returncode != 0
    assert "candidate_ref" in result.stderr
    assert "tree#L4/node#1" in result.stderr
    assert tree.load(f"tier1/merge-records/{record_id}.json")["consumed_epoch"] is None


@pytest.mark.parametrize(
    "verdict", ["hold", "bounce-for-surface-rework", "consolidate-first"]
)
def test_merge_requires_gate_verdict_exactly_land(tree: Sandbox, verdict: str):
    dispatch_id = seed_worked_node(tree)
    completed = tree.run(
        "dispatch",
        "outcome",
        "--dispatch",
        dispatch_id,
        "--outcome",
        "completed",
        role="harness",
    )
    assert completed.returncode == 0, completed.stderr
    if verdict == "consolidate-first":
        _create_record(tree, candidate="tree#other-a/node#1", verdict=None)
        _create_record(tree, candidate="tree#other-b/node#1", verdict=None)
    else:
        _create_record(tree, verdict=None)
    record_id = _create_record(tree, verdict=verdict)
    result = _merge(tree, record_id)
    assert result.returncode != 0
    assert "requires exactly 'land'" in result.stderr
    assert verdict in result.stderr
    assert tree.load(f"tier1/merge-records/{record_id}.json")["consumed_epoch"] is None


def test_merge_rejects_record_awaiting_gate_verdict(tree: Sandbox):
    seed_worked_node(tree)
    record_id = _create_record(tree, verdict=None)
    result = _merge(tree, record_id)
    assert result.returncode != 0
    assert "requires exactly 'land'" in result.stderr


def test_merge_uses_global_epoch_consumes_record_and_queues_other_tree(tree: Sandbox):
    sb = tree
    seed_worked_node(sb)
    assert sb.run(
        "tree", "init", "L5", "--root-question", "Does Y hold?", role="director"
    ).returncode == 0

    # Synthetic second-tree baseline proves allocation scans the federation,
    # rather than incrementing only the merging tree's local epoch.
    l5_path = sb.root / "trees/L5/tree.json"
    l5 = json.loads(l5_path.read_text())
    l5["epoch"] = 7
    l5["epoch_history"] = [
        {
            "epoch": 7,
            "merged_node": "synthetic-baseline",
            "date": "2026-07-12",
            "user_ratified": "n/a",
        }
    ]
    existing_watch = {
        "id": "W-3",
        "merged_node": "synthetic-baseline",
        "prediction_claim": None,
        "observed": None,
        "verdict": None,
        "severity": None,
        "status": "open",
    }
    l5["watch_queue"] = [existing_watch]
    l5_path.write_text(json.dumps(l5, indent=2) + "\n")
    assert sb.git("add", "trees/L5/tree.json").returncode == 0
    seeded = sb.git(
        "commit",
        "-m",
        "test: seed synthetic global epoch\n\nHT-Role: harness\n",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "harness"},
    )
    assert seeded.returncode == 0, seeded.stderr

    minted = sb.run(
        "node",
        "mint",
        "--tree",
        "L5",
        "--root",
        "--premise",
        "Synthetic second-tree node",
        "--rationale",
        "prove global epoch reads",
        role="director",
    )
    assert minted.returncode == 0, minted.stderr
    assert sb.load("trees/L5/tree.json")["decision_log"][-1]["epoch"] == 7

    record_id = _create_record(sb)
    merged = _merge(sb, record_id)
    assert merged.returncode == 0, merged.stderr

    assert sb.load("trees/L4/nodes/1/node.json")["status"] == "merged"
    source_tree = sb.load("trees/L4/tree.json")
    assert source_tree["epoch"] == 8
    assert source_tree["epoch_history"][-1]["epoch"] == 8
    assert sb.load(f"tier1/merge-records/{record_id}.json")["consumed_epoch"] == 8

    other_tree = sb.load("trees/L5/tree.json")
    assert other_tree["epoch"] == 7
    assert other_tree["epoch_history"] == l5["epoch_history"]
    assert other_tree["watch_queue"] == [
        existing_watch,
        {
            "id": "W-4",
            "merged_node": "tree#L4/node#1",
            "prediction_claim": None,
            "observed": None,
            "verdict": None,
            "severity": None,
            "status": "queued",
            "kind": "staleness-assessment",
            "epoch": 8,
        }
    ]

    replay = _merge(sb, record_id)
    assert replay.returncode != 0
    assert "already consumed at epoch 8" in replay.stderr
    assert sb.load("trees/L4/tree.json")["epoch"] == 8
    assert sb.load("trees/L5/tree.json")["watch_queue"] == other_tree["watch_queue"]


def test_tree_schema_preserves_open_rows_and_requires_staleness_metadata(
    sandbox: Sandbox,
):
    base = {
        "component": "L4",
        "root_question": "q",
        "epoch": 0,
        "epoch_history": [],
        "cursor": [],
        "decision_log": [],
        "global_learnings": [],
        "watch_queue": [],
    }
    existing = {
        "id": "W-1",
        "merged_node": "1",
        "prediction_claim": None,
        "observed": None,
        "verdict": None,
        "severity": None,
        "status": "open",
    }
    schemas.validate(sandbox.root / "system/schemas", "tree", {**base, "watch_queue": [existing]})

    queued = {
        **existing,
        "status": "queued",
        "kind": "staleness-assessment",
        "epoch": 1,
    }
    schemas.validate(sandbox.root / "system/schemas", "tree", {**base, "watch_queue": [queued]})

    queued.pop("epoch")
    with pytest.raises(HtError, match="'epoch' is a required property"):
        schemas.validate(
            sandbox.root / "system/schemas", "tree", {**base, "watch_queue": [queued]}
        )


def test_current_global_epoch_ignores_uncommitted_merge_state(tree: Sandbox):
    """R-Q7: dirty pre-commit writes are not a visible trunk movement."""
    ctx = Ctx(Root(tree.root), "harness")
    assert _common.current_global_epoch(ctx) == 0

    path = tree.root / "trees/L4/tree.json"
    dirty = tree.load("trees/L4/tree.json")
    dirty["epoch"] = 7
    dirty["epoch_history"] = [
        {
            "epoch": 7,
            "merged_node": "uncommitted-merge",
            "date": "2026-07-12",
            "user_ratified": "n/a",
        }
    ]
    path.write_text(json.dumps(dirty, indent=2) + "\n", encoding="utf-8")

    assert _common.current_global_epoch(ctx) == 0

    assert tree.git("add", "trees/L4/tree.json").returncode == 0
    committed = tree.git(
        "commit",
        "-m",
        "test: commit global epoch\n\nHT-Role: harness\n",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "harness"},
    )
    assert committed.returncode == 0, committed.stderr
    assert _common.current_global_epoch(ctx) == 7


def test_node_merge_rejects_contended_global_mutex_cleanly(tree: Sandbox):
    seed_worked_node(tree)
    record_id = _create_record(tree)

    with global_mutex(Root(tree.root)):
        rejected = _merge(tree, record_id)

    assert rejected.returncode != 0
    assert "global merge/ledger mutex is contended" in rejected.stderr
    assert tree.load("trees/L4/nodes/1/node.json")["status"] == "worked"
    assert tree.load(f"tier1/merge-records/{record_id}.json")["consumed_epoch"] is None
