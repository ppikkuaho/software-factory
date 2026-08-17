"""Phase C1: committed-snapshot, packet-only stage-2 preparation."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from composition_gate.packet import (
    DecisionSnapshot,
    PROMPT_SHA256,
    PacketError,
    allocate_attempt,
    prepare_packet,
    review_prompt_bytes,
)


ATTEMPT = "0123456789abcdef0123456789abcdef"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPT = (
    PROJECT_ROOT
    / "system/instruments/composition-gate/prompts/composition-gate-review.v1.md"
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], text=True, capture_output=True, check=False
    )


def _write(root: Path, rel: str, content: dict | str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict):
        path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    elif isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)


def _screen() -> dict:
    return {
        "results": [
            {"check": "scope-overlap", "result": "fail", "detail": "synthetic", "inputs": {"x": 1}}
        ],
        "output_ref": "var/MR-1-screen.json",
        "log_ref": "var/MR-1-screen.json",
        "log_sha256": "a" * 64,
        "output_sha256": "a" * 64,
        "computed": "2026-07-13T00:00:00+00:00",
        "head_commit": "b" * 40,
        "head_tree": "c" * 40,
        "config_hash": "d" * 64,
        "engine_version": "composition-gate/1.1.0",
    }


def _record(
    record_id: str,
    *,
    node: str,
    consumed_epoch: int | None = None,
    verdict: str | None = None,
    watch_link: str | None = None,
    surfaces: list[str] | None = None,
    lane: str = "L4",
) -> dict:
    gate = None if verdict is None else {
        "verdict": verdict,
        "date": "2026-07-13",
        "note": "synthetic",
    }
    return {
        "id": record_id,
        "candidate_ref": f"tree#L4/node#{node}",
        "lane_verdict": "lane-pass",
        "scope": {
            "lane": lane,
            "seats": [lane],
            "surfaces": surfaces or ["actor/surface"],
            "globs": ["system/**/*.py"],
        },
        "screen": _screen(),
        "gate_verdict": gate,
        "watch_link": watch_link,
        "created": "2026-07-13",
        "consumed_epoch": consumed_epoch,
    }


def _tree() -> dict:
    return {
        "component": "L4",
        "root_question": "Synthetic only",
        "epoch": 3,
        "epoch_history": [],
        "cursor": [],
        "decision_log": [],
        "global_learnings": [],
        "watch_queue": [
            {
                "id": "W-1",
                "merged_node": "tree#L4/node#2",
                "prediction_claim": None,
                "observed": None,
                "verdict": None,
                "severity": None,
                "status": "open",
            },
            {
                "id": "W-10",
                "merged_node": "tree#L5/node#10",
                "prediction_claim": None,
                "observed": None,
                "verdict": None,
                "severity": None,
                "status": "queued",
                "kind": "staleness-assessment",
                "epoch": 10,
            },
            {
                "id": "W-2",
                "merged_node": "tree#L5/node#2",
                "prediction_claim": None,
                "observed": None,
                "verdict": None,
                "severity": None,
                "status": "queued",
                "kind": "staleness-assessment",
                "epoch": 2,
            },
        ],
    }


def _node(source_dispatch: str = "d-1-1") -> dict:
    return {
        "id": "1",
        "parent": None,
        "premise": "Synthetic candidate",
        "minted_from": "synthetic",
        "status": "worked",
        "standing": "supported",
        "conflicts": [
            {
                "parked_at_epoch": 2,
                "superseded_by": None,
                "settlement": "pending",
                "demoted_to": None,
            }
        ],
        "claims": [
            {
                "id": "c-1-1",
                "node": "1",
                "source_dispatch": source_dispatch,
                "text": "Synthetic granted claim",
                "proposed_tier": 2,
                "granted_tier": 2,
                "standing_class": "trunk",
                "revalidation": None,
                "anchors": [
                    {
                        "path": "trees/L4/nodes/1/reports/d-1-1-report.md",
                        "start_line": 1,
                        "end_line": 1,
                    }
                ],
                "epoch": 0,
                "status": "granted",
            }
        ],
    }


def _dispatch(report_ref: str, report_hash: str, *, dispatch_id: str = "d-1-1") -> dict:
    return {
        "id": dispatch_id,
        "node": "1",
        "question": "Synthetic question",
        "done_definition": "Synthetic answer",
        "epoch": 0,
        "outcome": "completed",
        "steers": [],
        "adjudications": [],
        "issue_ref": None,
        "report_ref": report_ref,
        "report_hash": report_hash,
    }


def _pcd(pcd_id: str, refs: object) -> dict:
    return {
        "id": pcd_id,
        "date": "2026-07-13",
        "kind": "merge-schedule",
        "decision": f"Synthetic schedule {pcd_id}",
        "context_refs": refs,
    }


def _init_repo(
    root: Path,
    *,
    report_ref: str = "trees/L4/nodes/1/reports/d-1-1-report.md",
    source_dispatch: str = "d-1-1",
    pcds: dict[str, dict] | None = None,
) -> tuple[Path, bytes]:
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "packet@test.invalid").returncode == 0
    assert _git(root, "config", "user.name", "packet-test").returncode == 0
    report = b"# Synthetic report\n\nNo epistemic content.\n"
    _write(root, ".gitignore", "var/\n")
    _write(root, "tier1/merge-records/.gitkeep", b"")
    _write(root, "tier1/decision-log/.gitkeep", b"")
    _write(root, "tier1/merge-records/MR-1.json", _record("MR-1", node="1"))
    _write(
        root,
        "tier1/merge-records/MR-2.json",
        _record("MR-2", node="2", consumed_epoch=2, verdict="land", watch_link=None),
    )
    _write(
        root,
        "tier1/merge-records/MR-3.json",
        _record("MR-3", node="3", consumed_epoch=3, verdict="land", watch_link="W-1"),
    )
    _write(
        root,
        "tier1/merge-records/MR-4.json",
        _record("MR-4", node="4", verdict=None, surfaces=["pending/surface"]),
    )
    _write(root, "trees/L4/tree.json", _tree())
    _write(root, "trees/L4/nodes/1/node.json", _node(source_dispatch))
    _write(root, report_ref, report)
    _write(
        root,
        "trees/L4/nodes/1/dispatches/d-1-1.json",
        _dispatch(report_ref, hashlib.sha256(report).hexdigest()),
    )
    _write(
        root,
        "system/observatory/spine-candidates.L4.v1.md",
        "# Synthetic L4 candidates\n\nid: SP-L4-2\n\nid: SP-L4-1\n",
    )
    for rel, document in (pcds or {}).items():
        _write(root, f"tier1/decision-log/{rel}", document)
    assert _git(root, "add", "-A").returncode == 0
    committed = _git(root, "commit", "-q", "-m", "synthetic packet fixture")
    assert committed.returncode == 0, committed.stderr
    return root, report


@pytest.fixture
def packet_repo(tmp_path: Path) -> tuple[Path, bytes]:
    return _init_repo(tmp_path / "packet-repo")


def _load_packet(prepared) -> tuple[dict, dict]:
    context = json.loads((prepared.packet_dir / "context.json").read_text())
    manifest = json.loads((prepared.packet_dir / "manifest.json").read_text())
    return context, manifest


def _replace_json(root: Path, rel: str, mutate) -> None:
    path = root / rel
    document = json.loads(path.read_text())
    mutate(document)
    _write(root, rel, document)


def _commit(root: Path, message: str = "synthetic mutation") -> None:
    assert _git(root, "add", "-A").returncode == 0
    committed = _git(root, "commit", "-q", "-m", message)
    assert committed.returncode == 0, committed.stderr


def test_prompt_is_hash_frozen_and_states_every_stage2_rail():
    raw = PROMPT.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PROMPT_SHA256
    assert review_prompt_bytes() == raw
    text = raw.decode()
    for required in (
        "interaction effects between individually clean merges",
        "cumulative directive accretion and surface budget",
        "sequencing or consolidation needs among pending merges",
        "cross-lane settlement adequacy and the global staleness posture",
        "portfolio honesty over time",
        "packet is the complete context",
        "Do not re-adjudicate claim tiers",
        "Do not redirect research",
        "normal proposal channel",
        "gates, never steers",
        "Absence of concerns is a fine outcome; there is no quota for concerns or observations.",
    ):
        assert required in text


def test_review_prompt_loader_enforces_the_frozen_hash(monkeypatch: pytest.MonkeyPatch):
    original = Path.read_bytes

    def tampered(path: Path) -> bytes:
        if path.name == PROMPT.name:
            return b"tampered prompt\n"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", tampered)
    with pytest.raises(PacketError) as caught:
        review_prompt_bytes()
    assert caught.value.kind == "prompt-hash-mismatch"
    assert PROMPT_SHA256 in caught.value.message


def test_packet_uses_committed_snapshot_not_dirty_worktree_and_copies_exact_bytes(
    packet_repo: tuple[Path, bytes],
):
    root, committed_report = packet_repo
    snapshot = DecisionSnapshot.capture(root)
    report_path = root / "trees/L4/nodes/1/reports/d-1-1-report.md"
    report_path.write_bytes(b"DIRTY REPORT MUST NEVER ENTER PACKET\n")
    _replace_json(
        root,
        "tier1/merge-records/MR-1.json",
        lambda doc: doc["scope"]["surfaces"].append("dirty/surface"),
    )

    prepared = prepare_packet(root, "MR-1", snapshot=snapshot, attempt_id=ATTEMPT)
    context, manifest = _load_packet(prepared)

    assert (prepared.packet_dir / "001-report.md").read_bytes() == committed_report
    assert context["effective_surfaces"] == {
        "before": None,
        "after": ["actor/surface"],
        "before_status": "unavailable-in-v1",
    }
    report_entry = next(
        entry for entry in manifest["artifacts"] if entry["artifact_kind"] == "candidate-report"
    )
    assert report_entry["source_ref"] == "trees/L4/nodes/1/reports/d-1-1-report.md"
    assert report_entry["git_oid"] == _git(
        root, "rev-parse", f"{snapshot.head_commit}:{report_entry['source_ref']}"
    ).stdout.strip()
    assert report_entry["sha256"] == hashlib.sha256(committed_report).hexdigest()


def test_packet_contains_frozen_joins_history_frontier_pcds_and_provisional_spine(
    tmp_path: Path,
):
    root, _report = _init_repo(
        tmp_path / "packet-repo",
        pcds={
            "PCD-2.json": _pcd("PCD-2", ["MR-1", "I-1"]),
            "PCD-9.json": _pcd("PCD-9", ["MR-1"]),
            "PCD-10.json": _pcd("PCD-10", ["MR-4"]),
        },
    )
    prepared = prepare_packet(
        root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT
    )
    context, manifest = _load_packet(prepared)

    assert context["merge_record"]["id"] == "MR-1"
    assert context["merge_record"]["lane_verdict"] == "lane-pass"
    assert context["candidate"]["node"]["id"] == "1"
    assert [claim["id"] for claim in context["candidate"]["granted_claims"]] == ["c-1-1"]
    assert context["candidate"]["source_dispatch"]["id"] == "d-1-1"
    assert [row["record"]["id"] for row in context["last_consumed_merge_records"]] == [
        "MR-3",
        "MR-2",
    ]
    assert [row["watch_link"] for row in context["last_consumed_merge_records"]] == [
        "W-1",
        None,
    ]
    assert [row["watch_outcome"] for row in context["last_consumed_merge_records"]] == [
        None,
        None,
    ]
    assert context["open_settlements"] == [
        {
            "tree_path": "trees/L4/tree.json",
            "component": "L4",
            "row_id": "W-10",
            "epoch": 10,
            "merged_node": "tree#L5/node#10",
            "row": _tree()["watch_queue"][1],
            "source_ref": "trees/L4/tree.json",
            "packet_ref": next(
                entry["packet_ref"]
                for entry in manifest["artifacts"]
                if entry["source_ref"] == "trees/L4/tree.json"
            ),
        },
        {
            "tree_path": "trees/L4/tree.json",
            "component": "L4",
            "row_id": "W-2",
            "epoch": 2,
            "merged_node": "tree#L5/node#2",
            "row": _tree()["watch_queue"][2],
            "source_ref": "trees/L4/tree.json",
            "packet_ref": next(
                entry["packet_ref"]
                for entry in manifest["artifacts"]
                if entry["source_ref"] == "trees/L4/tree.json"
            ),
        },
    ]
    assert [row["id"] for row in context["pending_merge_records"]] == ["MR-1", "MR-4"]
    assert [row["id"] for row in context["merge_schedule_pc_decisions"]] == [
        "PCD-2",
        "PCD-9",
    ]
    assert [row["operative"] for row in context["merge_schedule_pc_decisions"]] == [
        False,
        True,
    ]
    assert context["operative_merge_schedule_pc_decision"] == "PCD-9"
    assert context["l4_spine_entries"] == [
        {
            "id": "SP-L4-1",
            "label": "provisional pending RQ-7",
            "source_ref": "system/observatory/spine-candidates.L4.v1.md",
            "packet_ref": next(
                entry["packet_ref"]
                for entry in manifest["artifacts"]
                if entry["artifact_kind"] == "l4-spine"
            ),
        },
        {
            "id": "SP-L4-2",
            "label": "provisional pending RQ-7",
            "source_ref": "system/observatory/spine-candidates.L4.v1.md",
            "packet_ref": next(
                entry["packet_ref"]
                for entry in manifest["artifacts"]
                if entry["artifact_kind"] == "l4-spine"
            ),
        },
    ]
    assert manifest["artifact_refs"] == list(manifest["input_hashes"])
    assert manifest["artifact_refs"] == list(manifest["source_refs"])
    for entry in manifest["artifacts"]:
        packet_path = prepared.attempt_dir / entry["packet_ref"]
        assert hashlib.sha256(packet_path.read_bytes()).hexdigest() == entry["sha256"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.update({"id": ""}), "needs a non-empty id"),
        (lambda row: row.update({"epoch": True}), "needs an epoch"),
        (lambda row: row.update({"epoch": -1}), "needs an epoch"),
        (lambda row: row.update({"merged_node": ""}), "needs merged_node"),
    ],
)
def test_malformed_queued_settlement_rows_fail_closed(tmp_path: Path, mutation, message: str):
    root, _report = _init_repo(tmp_path / "packet-repo")
    _replace_json(
        root,
        "trees/L4/tree.json",
        lambda doc: mutation(doc["watch_queue"][1]),
    )
    _commit(root)
    with pytest.raises(PacketError) as caught:
        prepare_packet(root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT)
    assert caught.value.kind == "snapshot-object-failure"
    assert message in caught.value.message


def test_consumed_equal_epoch_tie_uses_numeric_mr_order(tmp_path: Path):
    root, _report = _init_repo(tmp_path / "packet-repo")
    _write(
        root,
        "tier1/merge-records/MR-10.json",
        _record("MR-10", node="10", consumed_epoch=2, verdict="land"),
    )
    _commit(root)
    prepared = prepare_packet(
        root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT
    )
    context, _manifest = _load_packet(prepared)
    assert [row["record"]["id"] for row in context["last_consumed_merge_records"]][:3] == [
        "MR-3",
        "MR-2",
        "MR-10",
    ]


def test_pending_frontier_uses_numeric_mr_order(tmp_path: Path):
    root, _report = _init_repo(tmp_path / "packet-repo")
    _replace_json(
        root,
        "tier1/merge-records/MR-2.json",
        lambda doc: doc.update({"consumed_epoch": None, "gate_verdict": None}),
    )
    _write(root, "tier1/merge-records/MR-10.json", _record("MR-10", node="10"))
    _commit(root)
    prepared = prepare_packet(
        root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT
    )
    context, _manifest = _load_packet(prepared)
    assert [row["id"] for row in context["pending_merge_records"]] == [
        "MR-1",
        "MR-2",
        "MR-4",
        "MR-10",
    ]


def test_non_l4_candidate_still_receives_committed_l4_spine_catalogue(tmp_path: Path):
    root, _report = _init_repo(tmp_path / "packet-repo")
    _replace_json(
        root,
        "tier1/merge-records/MR-1.json",
        lambda doc: doc.update({"scope": {**doc["scope"], "lane": "L3", "seats": ["L3"]}}),
    )
    _commit(root)
    prepared = prepare_packet(
        root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT
    )
    context, _manifest = _load_packet(prepared)
    assert [row["id"] for row in context["l4_spine_entries"]] == ["SP-L4-1", "SP-L4-2"]


def test_zero_matching_merge_schedule_decisions_is_legal(packet_repo: tuple[Path, bytes]):
    root, _report = packet_repo
    prepared = prepare_packet(
        root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT
    )
    context, _manifest = _load_packet(prepared)
    assert context["merge_schedule_pc_decisions"] == []
    assert context["operative_merge_schedule_pc_decision"] is None


def test_available_watch_outcome_is_joined_while_other_nulls_remain(tmp_path: Path):
    root, _report = _init_repo(tmp_path / "packet-repo")
    _replace_json(
        root,
        "trees/L4/tree.json",
        lambda doc: doc["watch_queue"][0].update(
            {"verdict": "confirmed", "status": "resolved"}
        ),
    )
    _commit(root)
    prepared = prepare_packet(
        root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT
    )
    context, _manifest = _load_packet(prepared)
    assert [row["watch_outcome"] for row in context["last_consumed_merge_records"]] == [
        "confirmed",
        None,
    ]


@pytest.mark.parametrize(
    ("mutation", "kind"),
    [
        (lambda doc: doc.update({"claims": []}), "report-join-missing"),
        (
            lambda doc: doc["claims"].append(
                {**copy.deepcopy(doc["claims"][0]), "id": "c-1-2", "source_dispatch": "d-1-2"}
            ),
            "report-join-ambiguous",
        ),
        (lambda doc: doc["claims"][0].update({"source_dispatch": "d-1-2"}), "report-join-missing"),
    ],
)
def test_missing_ambiguous_and_wrong_dispatch_source_joins_are_stable(
    tmp_path: Path, mutation, kind: str
):
    root, _report = _init_repo(tmp_path / "packet-repo")
    _replace_json(root, "trees/L4/nodes/1/node.json", mutation)
    _commit(root)
    with pytest.raises(PacketError) as caught:
        prepare_packet(root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT)
    assert caught.value.kind == kind
    assert "candidate-report join for MR-1" in caught.value.message
    attempt_dir = root / f"var/cgate/MR-1/attempts/{ATTEMPT}"
    assert attempt_dir.is_dir()
    assert not any(attempt_dir.iterdir())


def test_more_than_one_completed_report_dispatch_is_ambiguous(tmp_path: Path):
    root, report = _init_repo(tmp_path / "packet-repo")
    second_ref = "trees/L4/nodes/1/reports/d-1-2-report.md"
    _write(root, second_ref, report)
    _write(
        root,
        "trees/L4/nodes/1/dispatches/d-1-2.json",
        _dispatch(second_ref, hashlib.sha256(report).hexdigest(), dispatch_id="d-1-2"),
    )
    _commit(root)
    with pytest.raises(PacketError) as caught:
        prepare_packet(root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT)
    assert caught.value.kind == "report-join-ambiguous"
    assert "2 completed dispatches" in caught.value.message


@pytest.mark.parametrize(
    ("filename", "document", "kind"),
    [
        ("PCD-01.json", _pcd("PCD-01", ["MR-1"]), "pcd-order-invalid"),
        ("PCD-2.json", _pcd("PCD-3", ["MR-1"]), "pcd-order-invalid"),
        ("PCD-2.json", _pcd("PCD-2", ["MR-01"]), "pcd-context-invalid"),
        ("PCD-2.json", _pcd("PCD-2", ["MR-1", "MR-1"]), "pcd-context-invalid"),
        ("PCD-2.json", _pcd("PCD-2", "MR-1"), "pcd-context-invalid"),
    ],
)
def test_malformed_pcd_order_and_context_refs_fail_closed(
    tmp_path: Path, filename: str, document: dict, kind: str
):
    root, _report = _init_repo(
        tmp_path / "packet-repo", pcds={filename: document}
    )
    with pytest.raises(PacketError) as caught:
        prepare_packet(root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT)
    assert caught.value.kind == kind
    attempt_dir = root / f"var/cgate/MR-1/attempts/{ATTEMPT}"
    assert attempt_dir.is_dir()
    assert not any(attempt_dir.iterdir())


def test_attempt_reuse_rejects_without_overwriting_existing_bytes(packet_repo: tuple[Path, bytes]):
    root, _report = packet_repo
    chosen, attempt_dir = allocate_attempt(root, "MR-1", attempt_id=ATTEMPT)
    assert chosen == ATTEMPT
    sentinel = attempt_dir / "sentinel"
    sentinel.write_bytes(b"immutable")
    with pytest.raises(PacketError) as caught:
        allocate_attempt(root, "MR-1", attempt_id=ATTEMPT)
    assert caught.value.kind == "attempt-collision"
    assert sentinel.read_bytes() == b"immutable"


def test_default_attempt_id_is_caller_unique_shape(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    first, first_dir = allocate_attempt(root, "MR-1")
    second, second_dir = allocate_attempt(root, "MR-1")
    assert len(first) == len(second) == 32
    assert all(character in "0123456789abcdef" for character in first + second)
    assert first != second
    assert first_dir.is_dir() and second_dir.is_dir()


def test_unsafe_output_parent_is_a_stable_write_collision(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "var").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PacketError) as caught:
        allocate_attempt(root, "MR-1", attempt_id=ATTEMPT)
    assert caught.value.kind == "write-collision"
    assert list(outside.iterdir()) == []


def test_manifest_and_every_packet_file_are_byte_stable_for_same_snapshot_and_attempt(
    tmp_path: Path,
):
    first, _report = _init_repo(
        tmp_path / "first", pcds={"PCD-7.json": _pcd("PCD-7", ["MR-1"])}
    )
    second = tmp_path / "second"
    cloned = subprocess.run(
        ["git", "clone", "-q", str(first), str(second)], text=True, capture_output=True
    )
    assert cloned.returncode == 0, cloned.stderr
    one = prepare_packet(
        first, "MR-1", snapshot=DecisionSnapshot.capture(first), attempt_id=ATTEMPT
    )
    two = prepare_packet(
        second, "MR-1", snapshot=DecisionSnapshot.capture(second), attempt_id=ATTEMPT
    )
    one_files = {
        path.relative_to(one.packet_dir).as_posix(): path.read_bytes()
        for path in one.packet_dir.rglob("*") if path.is_file()
    }
    two_files = {
        path.relative_to(two.packet_dir).as_posix(): path.read_bytes()
        for path in two.packet_dir.rglob("*") if path.is_file()
    }
    assert one_files == two_files
    assert one.manifest_sha256 == two.manifest_sha256
    assert one.input_hashes == two.input_hashes


@pytest.mark.parametrize("quote_path", ["true", "false"])
def test_odd_committed_report_filename_is_quote_path_independent(
    tmp_path: Path, quote_path: str
):
    odd_ref = "trees/L4/nodes/1/reports/odd\tname\nreport.md"
    root, report = _init_repo(tmp_path / f"packet-{quote_path}", report_ref=odd_ref)
    assert _git(root, "config", "core.quotePath", quote_path).returncode == 0
    prepared = prepare_packet(
        root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT
    )
    context, manifest = _load_packet(prepared)
    assert (prepared.packet_dir / "001-report.md").read_bytes() == report
    assert context["candidate"]["report"]["source_ref"] == odd_ref
    assert next(
        row for row in manifest["artifacts"] if row["artifact_kind"] == "candidate-report"
    )["source_ref"] == odd_ref


def test_snapshot_commit_tree_mismatch_and_committed_symlink_fail_with_stable_kinds(
    tmp_path: Path,
):
    root, _report = _init_repo(tmp_path / "packet-repo")
    snapshot = DecisionSnapshot.capture(root)
    mismatch = DecisionSnapshot(snapshot.root, snapshot.head_commit, "0" * 40)
    with pytest.raises(PacketError) as caught:
        prepare_packet(root, "MR-1", snapshot=mismatch, attempt_id=ATTEMPT)
    assert caught.value.kind == "snapshot-object-failure"

    link = root / "tier1/decision-log/odd-link"
    link.symlink_to(".gitkeep")
    _commit(root, "synthetic symlink")
    with pytest.raises(PacketError) as caught:
        prepare_packet(root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT)
    assert caught.value.kind == "snapshot-object-failure"


def test_packet_module_has_no_worktree_glob_discovery_and_qa_root_is_disposable(packet_repo):
    root, _report = packet_repo
    source = (
        PROJECT_ROOT
        / "system/instruments/composition-gate/composition_gate/packet.py"
    ).read_text()
    assert ".glob(" not in source and ".rglob(" not in source
    assert root != PROJECT_ROOT and not PROJECT_ROOT.is_relative_to(root)
    before = _git(PROJECT_ROOT, "status", "--short").stdout
    prepare_packet(root, "MR-1", snapshot=DecisionSnapshot.capture(root), attempt_id=ATTEMPT)
    after = _git(PROJECT_ROOT, "status", "--short").stdout
    assert after == before
