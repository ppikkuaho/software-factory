"""Committed gate-review Tier-1 foundation: schema, authority, hook, and layout."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from conftest import Sandbox
from ht import authority, classify, schemas
from ht.errors import HtError
from ht.paths import Root


def _screen_ref(*, error_shape: bool = False) -> dict:
    return {
        "output_ref": "var/MR-1-screen.json",
        "log_ref": "var/MR-1-screen.json",
        "log_sha256": "a" * 64,
        "output_sha256": "a" * 64,
        "computed": "2026-07-13T12:00:00+00:00",
        "head_commit": None if error_shape else "b" * 40,
        "head_tree": None if error_shape else "c" * 40,
        "config_hash": "d" * 64,
        "engine_version": "composition-gate/1.1.0",
    }


def _stage1_review(*, verdict: str = "land") -> dict:
    stuck = verdict == "escalate-stuck"
    return {
        "id": "GR-1",
        "merge_record_ref": "MR-1",
        "created": "2026-07-13",
        "stage": 1,
        "attempt_id": None,
        "screen_ref": _screen_ref(error_shape=stuck),
        "packet": None,
        "template": None,
        "generator": None,
        "rules_fired": [],
        "verdict": verdict,
        "note": "The committed screen is invalid." if stuck else "All checks passed.",
        "observations": [],
        "raw_output": None,
        "escalation_ref": "RQ-1" if stuck else None,
    }


def test_stage1_invalid_birth_review_accepts_exact_all_null_screen_identity(
    sandbox: Sandbox,
):
    review = _stage1_review(verdict="escalate-stuck")
    review["screen_ref"] = {key: None for key in _screen_ref()}
    schemas.validate(sandbox.root / "system/schemas", "gate_review", review)


def _stage2_review(*, verdict: str = "hold") -> dict:
    escalated = verdict in {"escalate-to-user", "escalate-stuck"}
    return {
        "id": "GR-1",
        "merge_record_ref": "MR-1",
        "created": "2026-07-13",
        "stage": 2,
        "attempt_id": "1" * 32,
        "screen_ref": _screen_ref(),
        "packet": {
            "manifest_ref": "var/cgate/MR-1/attempts/11111111111111111111111111111111/packet/manifest.json",
            "manifest_sha256": "e" * 64,
            "artifact_refs": ["packet/001-report.md"],
            "input_hashes": {"packet/001-report.md": "f" * 64},
        },
        "template": {
            "name": "composition-gate-review.v1.md",
            "sha256": "0" * 64,
        },
        "generator": {
            "mechanism": "injected-synthetic",
            "status": "synthetic",
            "requested_model": None,
            "actual_model": "<synthetic>",
            "session_ref": "synthetic-session-1",
            "error": None,
        },
        "rules_fired": [],
        "verdict": verdict,
        "note": "Hold until the cited mechanical condition clears.",
        "observations": [
            {
                "text": "The reports share an actor-visible coordination edge.",
                "anchors": [
                    {"ref": "packet/001-report.md", "sha256": "f" * 64}
                ],
            }
        ],
        "raw_output": {
            "ref": "var/cgate/MR-1/attempts/11111111111111111111111111111111/raw-output.json",
            "sha256": "2" * 64,
        },
        "escalation_ref": "RQ-1" if escalated else None,
    }


@pytest.mark.parametrize(
    "review",
    [
        _stage1_review(),
        _stage1_review(verdict="escalate-stuck"),
        _stage2_review(),
        _stage2_review(verdict="land-after-MR-7"),
        _stage2_review(verdict="consolidate-first"),
        _stage2_review(verdict="bounce-for-surface-rework"),
        _stage2_review(verdict="escalate-to-user"),
        _stage2_review(verdict="escalate-stuck"),
    ],
)
def test_gate_review_schema_accepts_coupled_stage_branches(
    sandbox: Sandbox,
    review: dict,
):
    schemas.validate(sandbox.root / "system/schemas", "gate_review", review)


def _mutated(review: dict, path: tuple[str, ...], value) -> dict:
    changed = deepcopy(review)
    target = changed
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    return changed


@pytest.mark.parametrize(
    "review",
    [
        _mutated(_stage1_review(), ("stage",), 2),
        _mutated(_stage1_review(), ("attempt_id",), "1" * 32),
        _mutated(_stage1_review(), ("verdict",), "hold"),
        _mutated(_stage1_review(), ("packet",), _stage2_review()["packet"]),
        _mutated(_stage2_review(), ("attempt_id",), None),
        _mutated(_stage2_review(), ("packet",), None),
        _mutated(_stage2_review(), ("verdict",), "land"),
        _mutated(_stage2_review(), ("verdict",), "future-token"),
        _mutated(_stage2_review(), ("escalation_ref",), "RQ-1"),
        _mutated(
            _stage2_review(),
            ("generator", "mechanism"),
            "claude-p",
        ),
        _mutated(_stage2_review(), ("observations",), [{"text": "unanchored"}]),
        {**_stage1_review(), "extra": True},
    ],
)
def test_gate_review_schema_rejects_cross_stage_or_unknown_shapes(
    sandbox: Sandbox,
    review: dict,
):
    with pytest.raises(HtError, match="schema-nonconforming gate_review"):
        schemas.validate(sandbox.root / "system/schemas", "gate_review", review)


def test_gate_review_path_helper_classification_and_schema_registry(tmp_path):
    root = Root(tmp_path / "research")
    assert root.gate_review_json("GR-9") == (
        root.path / "tier1/gate-reviews/GR-9.json"
    )
    assert classify.doc_type("tier1/gate-reviews/GR-9.json") == "gate_review"
    assert classify.doc_type("tier1/gate-reviews/GR-x.json") is None
    assert schemas.DOC_SCHEMA["gate_review"] == "gate_review.schema.json"


def test_gate_review_is_cgate_created_complete_and_frozen():
    review = _stage1_review()
    authority.check_write("gate_review", None, review, "cgate")

    with pytest.raises(HtError, match="creator: cgate"):
        authority.check_write("gate_review", None, review, "harness")

    rewritten = {**review, "note": "Rewritten after birth."}
    with pytest.raises(HtError, match="gate_review is frozen after creation"):
        authority.check_write("gate_review", review, rewritten, "cgate")


def _stage_review(sb: Sandbox, review: dict, rel: str = "tier1/gate-reviews/GR-1.json"):
    sb.write_file(rel, json.dumps(review, indent=2) + "\n")
    added = sb.git("add", "--", rel)
    assert added.returncode == 0, added.stderr


def _commit_staged(sb: Sandbox, message: str, *, role: str):
    return sb.git(
        "commit",
        "-m",
        message,
        env_extra={"HT_COMMIT": "1", "HT_ROLE": role},
    )


def _seed_review(sb: Sandbox) -> str:
    rel = "tier1/gate-reviews/GR-1.json"
    _stage_review(sb, _stage1_review(), rel)
    committed = _commit_staged(sb, "add gate review GR-1", role="cgate")
    assert committed.returncode == 0, committed.stderr
    return rel


def test_hook_accepts_gate_review_add_by_cgate(sandbox: Sandbox):
    rel = _seed_review(sandbox)
    assert sandbox.load(rel) == _stage1_review()


def test_hook_rejects_gate_review_add_by_non_creator(sandbox: Sandbox):
    rel = "tier1/gate-reviews/GR-1.json"
    _stage_review(sandbox, _stage1_review(), rel)
    rejected = _commit_staged(sandbox, "wrong-role gate review", role="harness")
    assert rejected.returncode != 0
    assert "gate_review" in rejected.stderr
    assert "creator: cgate" in rejected.stderr


def test_hook_rejects_existing_gate_review_modification(sandbox: Sandbox):
    rel = _seed_review(sandbox)
    _stage_review(sandbox, {**_stage1_review(), "note": "rewrite"}, rel)
    rejected = _commit_staged(sandbox, "rewrite gate review", role="cgate")
    assert rejected.returncode != 0
    assert "modifies an existing gate review" in rejected.stderr
    assert "frozen after creation" in rejected.stderr


def test_hook_rejects_gate_review_delete(sandbox: Sandbox):
    rel = _seed_review(sandbox)
    (sandbox.root / rel).unlink()
    assert sandbox.git("add", "--", rel).returncode == 0
    rejected = _commit_staged(sandbox, "delete gate review", role="cgate")
    assert rejected.returncode != 0
    assert "removes or renames a Tier-1 record" in rejected.stderr
    assert "gate review" in rejected.stderr


def test_hook_rejects_gate_review_rename(sandbox: Sandbox):
    rel = _seed_review(sandbox)
    destination = "notes/GR-1.json"
    (sandbox.root / "notes").mkdir()
    moved = sandbox.git("mv", rel, destination)
    assert moved.returncode == 0, moved.stderr
    rejected = _commit_staged(sandbox, "rename gate review", role="cgate")
    assert rejected.returncode != 0
    assert "removes or renames a Tier-1 record" in rejected.stderr
    assert "gate review" in rejected.stderr


def test_hook_rejects_gate_review_historical_path_recreation(sandbox: Sandbox):
    rel = _seed_review(sandbox)
    (sandbox.root / rel).unlink()
    assert sandbox.git("add", "--", rel).returncode == 0
    bypass = sandbox.git("commit", "--no-verify", "-m", "synthetic bypass delete")
    assert bypass.returncode == 0, bypass.stderr

    _stage_review(sandbox, _stage1_review(), rel)
    rejected = _commit_staged(sandbox, "recreate gate review", role="cgate")
    assert rejected.returncode != 0
    assert "re-creates a previously recorded Tier-1 path" in rejected.stderr


def test_hook_rejects_gate_review_filename_id_mismatch(sandbox: Sandbox):
    review = {**_stage1_review(), "id": "GR-2"}
    _stage_review(sandbox, review)
    rejected = _commit_staged(sandbox, "mismatched gate review id", role="cgate")
    assert rejected.returncode != 0
    assert "does not match filename" in rejected.stderr
    assert "union-global gate-review" in rejected.stderr


def test_hook_rejects_duplicate_union_global_gate_review_id(sandbox: Sandbox):
    _stage_review(sandbox, _stage1_review(), "tier1/gate-reviews/GR-1.json")
    duplicate = {**_stage1_review(), "id": "GR-1"}
    _stage_review(sandbox, duplicate, "tier1/gate-reviews/GR-2.json")
    rejected = _commit_staged(sandbox, "duplicate gate review id", role="cgate")
    assert rejected.returncode != 0
    assert "duplicates union-global gate-review id" in rejected.stderr


def test_fresh_root_scaffolds_gate_review_store_and_validates(sandbox: Sandbox):
    directory = sandbox.root / "tier1/gate-reviews"
    assert directory.is_dir()
    assert (directory / ".gitkeep").is_file()
    validated = sandbox.run("validate", "--all")
    assert validated.returncode == 0, validated.stderr
    assert "OK: all state files schema-valid" in validated.stdout
