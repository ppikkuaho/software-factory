"""Tier-1 pre-commit guards for authority and append-only state."""

from __future__ import annotations

import json

import pytest

from conftest import Sandbox
from ht import authority, hook


def _decision(decision_id: str = "PCD-1", text: str = "Activate issue I-1") -> str:
    return json.dumps(
        {
            "id": decision_id,
            "date": "2026-07-12",
            "kind": "triage",
            "decision": text,
            "context_refs": ["issue#I-1"],
        }
    )


def _issue(*, status: str = "proposed") -> dict:
    return {
        "id": "I-1",
        "title": "Test the primary explanation",
        "provenance": ["user-request"],
        "scope": "The first-stage attribution question.",
        "question": "Does the primary explanation survive its cheapest test?",
        "done_definition": "The named test has a reproducible result.",
        "lanes": ["L1"],
        "subgoals": ["Run the cheapest discriminating test."],
        "status": status,
        "closure": None,
    }


def _ratification_item() -> dict:
    return {
        "id": "RQ-1",
        "kind": "cgate-escalation",
        "payload_ref": "merge-record#MR-1",
        "text": "Resolve the composition-gate escalation.",
        "queued_by": "cgate",
        "date": "2026-07-12",
        "disposition": None,
        "annotations": [],
    }


def _merge_record(*, lane_verdict: str = "land") -> dict:
    return {
        "id": "MR-1",
        "candidate_ref": "candidate#C-1",
        "lane_verdict": lane_verdict,
        "scope": {"lane": "L4", "seats": [], "surfaces": [], "globs": []},
        "screen": {
            "results": [{"check": "lane-evidence", "result": "pass"}],
            "output_ref": None,
            "log_ref": None,
            "log_sha256": None,
            "output_sha256": None,
            "computed": None,
            "head_commit": None,
            "head_tree": None,
            "config_hash": None,
            "engine_version": None,
        },
        "gate_verdict": None,
        "watch_link": None,
        "created": "2026-07-12",
        "consumed_epoch": None,
    }


def _gate_verdict(verdict: str = "land", **updates) -> dict:
    doc = {
        "verdict": verdict,
        "date": "2026-07-12",
        "review_ref": "GR-1",
        "review_sha256": "a" * 64,
        "note": "Composition-gate disposition.",
    }
    doc.update(updates)
    return doc


def _commit_document(
    sb: Sandbox,
    rel: str,
    document: dict,
    *,
    role: str,
    message: str,
) -> None:
    sb.write_file(rel, json.dumps(document))
    assert sb.git("add", rel).returncode == 0
    result = sb.git(
        "commit",
        "-m",
        message,
        env_extra={"HT_COMMIT": "1", "HT_ROLE": role},
    )
    assert result.returncode == 0, result.stderr


def _stage_document(sb: Sandbox, rel: str, document: dict) -> None:
    sb.write_file(rel, json.dumps(document))
    result = sb.git("add", rel)
    assert result.returncode == 0, result.stderr


def _commit_staged(sb: Sandbox, message: str, *, role: str):
    return sb.git(
        "commit",
        "-m",
        message,
        env_extra={"HT_COMMIT": "1", "HT_ROLE": role},
    )


def _seed_issue(sb: Sandbox) -> tuple[str, dict]:
    rel = "tier1/issues/I-1.json"
    document = _issue()
    _commit_document(sb, rel, document, role="pc", message="add issue I-1")
    return rel, document


def _seed_ratification_item(sb: Sandbox) -> tuple[str, dict]:
    rel = "tier1/ratification-queue/RQ-1.json"
    document = _ratification_item()
    _commit_document(
        sb,
        rel,
        document,
        role="cgate",
        message="queue ratification RQ-1",
    )
    return rel, document


def _seed_merge_record(sb: Sandbox) -> tuple[str, dict]:
    rel = "tier1/merge-records/MR-1.json"
    document = _merge_record()
    _commit_document(
        sb,
        rel,
        document,
        role="harness",
        message="add merge record MR-1",
    )
    return rel, document


def _commit_new_decision(sb: Sandbox, decision_id: str = "PCD-1") -> str:
    rel = f"tier1/decision-log/{decision_id}.json"
    sb.write_file(rel, _decision(decision_id))
    assert sb.git("add", rel).returncode == 0
    result = sb.git(
        "commit",
        "-m",
        f"add {decision_id}",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "pc"},
    )
    assert result.returncode == 0, result.stderr
    return rel


def _reset_after_rejection(sb: Sandbox) -> None:
    result = sb.git("reset", "--hard", "HEAD")
    assert result.returncode == 0, result.stderr


def _delete_marker_without_hook(sb: Sandbox, rel: str) -> None:
    (sb.root / rel).unlink()
    assert sb.git("add", "--", rel).returncode == 0
    result = sb.git("commit", "--no-verify", "-m", f"remove {rel}")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("raw_lane", ["", "   ", "\t\n"])
def test_hook_treats_blank_verifier_lane_as_unassigned(
    monkeypatch: pytest.MonkeyPatch,
    raw_lane: str,
):
    monkeypatch.setenv("HT_LANE", raw_lane)
    assert hook._verifier_lane("verifier") is authority.UNASSIGNED_LANE


def test_hook_strips_nonempty_verifier_lane(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HT_LANE", "  L4  ")
    assert hook._verifier_lane("verifier") == "L4"


def test_pc_decision_addition_by_pc_with_ht_commit_succeeds(sandbox: Sandbox):
    rel = _commit_new_decision(sandbox)

    committed = sandbox.git("show", f"HEAD:{rel}")
    assert committed.returncode == 0
    assert json.loads(committed.stdout)["id"] == "PCD-1"


def test_pc_decision_addition_without_ht_commit_is_out_of_band(sandbox: Sandbox):
    rel = "tier1/decision-log/PCD-1.json"
    sandbox.write_file(rel, _decision())
    assert sandbox.git("add", rel).returncode == 0

    result = sandbox.git(
        "commit",
        "-m",
        "out-of-band PC decision",
        env_extra={"HT_ROLE": "pc"},
    )
    assert result.returncode != 0
    assert "out-of-band" in result.stderr
    assert "use ht" in result.stderr
    _reset_after_rejection(sandbox)


def test_existing_pc_decision_mutation_is_rejected(sandbox: Sandbox):
    rel = _commit_new_decision(sandbox)
    sandbox.write_file(rel, _decision(text="Rewrite the prior decision"))
    assert sandbox.git("add", rel).returncode == 0

    result = sandbox.git(
        "commit",
        "-m",
        "rewrite PC decision",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "pc"},
    )
    assert result.returncode != 0
    assert "append-only" in result.stderr
    assert "PC decision" in result.stderr
    _reset_after_rejection(sandbox)
    assert json.loads((sandbox.root / rel).read_text())["decision"] == "Activate issue I-1"


def test_existing_pc_decision_deletion_is_rejected(sandbox: Sandbox):
    rel = _commit_new_decision(sandbox)
    (sandbox.root / rel).unlink()
    assert sandbox.git("add", "--", rel).returncode == 0

    result = sandbox.git(
        "commit",
        "-m",
        "delete PC decision",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "pc"},
    )
    assert result.returncode != 0
    assert "append-only" in result.stderr
    assert "PC decision" in result.stderr
    _reset_after_rejection(sandbox)
    assert (sandbox.root / rel).exists()


def test_pc_decision_rename_out_of_tier1_is_rejected(sandbox: Sandbox):
    rel = _commit_new_decision(sandbox)
    destination = "notes/PCD-1.json"
    (sandbox.root / "notes").mkdir()
    result = sandbox.git("mv", rel, destination)
    assert result.returncode == 0, result.stderr

    result = sandbox.git("commit", "-m", "move decision outside tier1")
    assert result.returncode != 0
    assert "append-only" in result.stderr
    assert "PC decision" in result.stderr
    _reset_after_rejection(sandbox)
    assert (sandbox.root / rel).exists()


def test_phase_deletion_is_rejected_for_every_role(sandbox: Sandbox):
    assert sandbox.run("phase", "set", "autonomy", role="user").returncode == 0
    rel = "tier1/phase.json"
    (sandbox.root / rel).unlink()
    assert sandbox.git("add", "--", rel).returncode == 0

    result = sandbox.git(
        "commit",
        "-m",
        "delete phase as harness",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "harness"},
    )
    assert result.returncode != 0
    assert "removes or renames a Tier-1 record" in result.stderr
    assert "may not vanish" in result.stderr
    _reset_after_rejection(sandbox)
    assert (sandbox.root / rel).exists()


def test_merge_record_deletion_by_verifier_is_rejected(sandbox: Sandbox):
    rel, _ = _seed_merge_record(sandbox)
    (sandbox.root / rel).unlink()
    assert sandbox.git("add", "--", rel).returncode == 0

    result = _commit_staged(sandbox, "delete merge record", role="verifier")
    assert result.returncode != 0
    assert "removes or renames a Tier-1 record" in result.stderr
    assert "may not vanish" in result.stderr
    _reset_after_rejection(sandbox)
    assert (sandbox.root / rel).exists()


def test_issue_deletion_by_unit_is_rejected(sandbox: Sandbox):
    rel, _ = _seed_issue(sandbox)
    (sandbox.root / rel).unlink()
    assert sandbox.git("add", "--", rel).returncode == 0

    result = _commit_staged(sandbox, "delete issue", role="unit")
    assert result.returncode != 0
    assert "removes or renames a Tier-1 record" in result.stderr
    assert "may not vanish" in result.stderr
    _reset_after_rejection(sandbox)
    assert (sandbox.root / rel).exists()


@pytest.mark.parametrize("doc_type", ["phase", "issue", "merge_record"])
def test_remaining_tier1_record_types_cannot_be_renamed_out(
    sandbox: Sandbox,
    doc_type: str,
):
    if doc_type == "phase":
        seeded = sandbox.run("phase", "set", "sign-off", role="user")
        assert seeded.returncode == 0, seeded.stderr
        rel = "tier1/phase.json"
    elif doc_type == "issue":
        rel, _ = _seed_issue(sandbox)
    else:
        rel, _ = _seed_merge_record(sandbox)

    destination = f"notes/{doc_type}.json"
    (sandbox.root / "notes").mkdir(exist_ok=True)
    moved = sandbox.git("mv", rel, destination)
    assert moved.returncode == 0, moved.stderr

    result = _commit_staged(sandbox, f"rename {doc_type} out", role="user")
    assert result.returncode != 0
    assert "removes or renames a Tier-1 record" in result.stderr
    assert "may not vanish" in result.stderr
    _reset_after_rejection(sandbox)
    assert (sandbox.root / rel).exists()
    assert not (sandbox.root / destination).exists()


def test_deleted_merge_record_path_cannot_be_recreated_to_launder_frozen_stamps(
    sandbox: Sandbox,
):
    rel, original = _seed_merge_record(sandbox)
    (sandbox.root / rel).unlink()
    assert sandbox.git("add", "--", rel).returncode == 0
    bypass = sandbox.git("commit", "--no-verify", "-m", "hypothetical bypass delete")
    assert bypass.returncode == 0, bypass.stderr

    rewritten = {
        **original,
        "lane_verdict": "reject",
        "created": "2026-07-13",
    }
    _stage_document(sandbox, rel, rewritten)
    result = _commit_staged(sandbox, "recreate rewritten merge record", role="harness")
    assert result.returncode != 0
    assert "re-creates a previously recorded Tier-1 path" in result.stderr
    assert "identity and creation stamps are immutable" in result.stderr
    assert "D4" in result.stderr
    _reset_after_rejection(sandbox)
    assert not (sandbox.root / rel).exists()


def test_harness_cannot_birth_merge_record_with_gate_verdict(sandbox: Sandbox):
    rel = "tier1/merge-records/MR-1.json"
    record = {
        **_merge_record(),
        "gate_verdict": _gate_verdict(note="Smuggled at creation."),
    }
    _stage_document(sandbox, rel, record)
    result = _commit_staged(
        sandbox,
        "birth merge record with verdict",
        role="harness",
    )
    assert result.returncode != 0
    assert "merge_record.gate_verdict" in result.stderr
    assert "owner: cgate" in result.stderr
    _reset_after_rejection(sandbox)
    assert not (sandbox.root / rel).exists()


def test_harness_cannot_birth_consumed_merge_record(sandbox: Sandbox):
    rel = "tier1/merge-records/MR-1.json"
    _stage_document(sandbox, rel, {**_merge_record(), "consumed_epoch": 9})
    result = _commit_staged(
        sandbox,
        "birth consumed merge record",
        role="harness",
    )
    assert result.returncode != 0
    assert "consumed_epoch must be null at creation" in result.stderr
    _reset_after_rejection(sandbox)
    assert not (sandbox.root / rel).exists()


@pytest.mark.parametrize("role", ["harness", "director"])
def test_consumed_epoch_hand_mutation_requires_derived_merge_seam(
    sandbox: Sandbox,
    role: str,
):
    rel, record = _seed_merge_record(sandbox)
    _stage_document(sandbox, rel, {**record, "consumed_epoch": 1})
    result = _commit_staged(
        sandbox,
        "hand-stamp consumed merge record",
        role=role,
    )
    assert result.returncode != 0
    assert "derived mechanical-write seam" in result.stderr
    _reset_after_rejection(sandbox)
    assert sandbox.load(rel)["consumed_epoch"] is None


def test_consumed_epoch_marker_cannot_replace_staged_merge_cohort(
    sandbox: Sandbox,
):
    rel, record = _seed_merge_record(sandbox)
    _stage_document(sandbox, rel, {**record, "consumed_epoch": 1})
    result = sandbox.git(
        "commit",
        "-m",
        "forge derived merge marker",
        env_extra={
            "HT_COMMIT": "1",
            "HT_ROLE": "director",
            "HT_MECHANICAL_WRITES": json.dumps({rel: ["consumed_epoch"]}),
        },
    )
    assert result.returncode != 0
    assert "derived mechanical-write seam" in result.stderr
    _reset_after_rejection(sandbox)
    assert sandbox.load(rel)["consumed_epoch"] is None


def test_merge_record_scope_is_frozen_via_hook(sandbox: Sandbox):
    rel, record = _seed_merge_record(sandbox)
    rewritten = {
        **record,
        "scope": {
            "lane": "L4",
            "seats": ["rewritten-seat"],
            "surfaces": [],
            "globs": [],
        },
    }
    _stage_document(sandbox, rel, rewritten)
    result = _commit_staged(sandbox, "rewrite merge record scope", role="harness")
    assert result.returncode != 0
    assert "merge_record.scope" in result.stderr
    assert "frozen-after-creation" in result.stderr
    _reset_after_rejection(sandbox)
    assert sandbox.load(rel)["scope"] == record["scope"]


def test_cgate_fills_merge_gate_verdict_once_via_hook(sandbox: Sandbox):
    rel, record = _seed_merge_record(sandbox)
    filled = {
        **record,
        "gate_verdict": _gate_verdict(
            "escalate-stuck", note="Escalation remained stuck."
        ),
    }
    _commit_document(
        sandbox,
        rel,
        filled,
        role="cgate",
        message="fill merge gate verdict",
    )

    rewritten = {
        **filled,
        "gate_verdict": _gate_verdict("land", date="2026-07-13"),
    }
    _stage_document(sandbox, rel, rewritten)
    result = _commit_staged(sandbox, "rewrite merge gate verdict", role="cgate")
    assert result.returncode != 0
    assert "merge_record.gate_verdict is frozen" in result.stderr
    assert "owner: cgate" in result.stderr
    _reset_after_rejection(sandbox)
    assert sandbox.load(rel)["gate_verdict"] == filled["gate_verdict"]


def test_merge_record_screen_is_frozen_after_verdict_via_hook(sandbox: Sandbox):
    rel, record = _seed_merge_record(sandbox)
    filled = {
        **record,
        "gate_verdict": _gate_verdict("hold"),
    }
    _commit_document(
        sandbox,
        rel,
        filled,
        role="cgate",
        message="fill merge gate verdict",
    )
    rewritten = {
        **filled,
        "screen": {
            **filled["screen"],
            "results": [{"check": "scope-overlap", "result": "fail"}],
        },
    }
    _stage_document(sandbox, rel, rewritten)
    result = _commit_staged(sandbox, "rewrite screen after verdict", role="harness")
    assert result.returncode != 0
    assert "merge_record.screen is frozen after gate verdict" in result.stderr
    assert "D2" in result.stderr
    _reset_after_rejection(sandbox)
    assert sandbox.load(rel)["screen"] == filled["screen"]


def test_non_cgate_merge_gate_fill_is_rejected_via_hook(sandbox: Sandbox):
    rel, record = _seed_merge_record(sandbox)
    filled = {
        **record,
        "gate_verdict": _gate_verdict("escalate-stuck"),
    }
    _stage_document(sandbox, rel, filled)
    result = _commit_staged(sandbox, "harness fills gate verdict", role="harness")
    assert result.returncode != 0
    assert "merge_record.gate_verdict" in result.stderr
    assert "owner: cgate" in result.stderr
    _reset_after_rejection(sandbox)
    assert sandbox.load(rel)["gate_verdict"] is None


def test_non_object_phase_rejects_cleanly_without_traceback(sandbox: Sandbox):
    rel = "tier1/phase.json"
    sandbox.write_file(rel, "[]")
    assert sandbox.git("add", rel).returncode == 0

    result = sandbox.git(
        "commit",
        "-m",
        "malformed phase",
        env_extra={"HT_COMMIT": "1", "HT_ROLE": "user"},
    )
    assert result.returncode != 0
    assert "REJECTED" in result.stderr
    assert "JSON object" in result.stderr
    assert "Traceback" not in result.stderr
    _reset_after_rejection(sandbox)


@pytest.mark.parametrize(
    ("rel", "recreate"),
    [
        ("tier1/merge-records/.gitkeep", True),
        ("trees/.gitkeep", False),
    ],
)
def test_sanctioned_scaffold_marker_rejects_nonempty_staged_blob(
    sandbox: Sandbox,
    rel: str,
    recreate: bool,
):
    if recreate:
        _delete_marker_without_hook(sandbox, rel)
    sandbox.write_file(rel, "smuggled state\n")
    assert sandbox.git("add", rel).returncode == 0

    result = _commit_staged(
        sandbox,
        "smuggle state through scaffold marker",
        role="harness",
    )

    assert result.returncode != 0
    assert "sanctioned scaffold marker" in result.stderr
    assert "must be empty bytes" in result.stderr
    assert "[R-i7-7]" in result.stderr
    assert rel in result.stderr
    _reset_after_rejection(sandbox)


def test_empty_scaffold_marker_recreation_is_accepted(sandbox: Sandbox):
    rel = "tier1/merge-records/.gitkeep"
    _delete_marker_without_hook(sandbox, rel)
    sandbox.write_file(rel, "")
    assert sandbox.git("add", rel).returncode == 0

    result = _commit_staged(sandbox, "restore empty scaffold marker", role="harness")

    assert result.returncode == 0, result.stderr
    size = sandbox.git("cat-file", "-s", f"HEAD:{rel}")
    assert size.returncode == 0, size.stderr
    assert size.stdout.strip() == "0"


@pytest.mark.parametrize("mode", ["100755", "120000"])
def test_scaffold_marker_wrong_staged_mode_is_rejected(
    sandbox: Sandbox,
    mode: str,
):
    rel = "tier1/merge-records/.gitkeep"
    oid = sandbox.git("rev-parse", f"HEAD:{rel}")
    assert oid.returncode == 0, oid.stderr
    staged = sandbox.git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"{mode},{oid.stdout.strip()},{rel}",
    )
    assert staged.returncode == 0, staged.stderr

    result = _commit_staged(sandbox, f"marker mode {mode}", role="harness")

    assert result.returncode != 0
    assert "mode/type must be 100644 blob" in result.stderr
    assert f"got {mode} blob" in result.stderr
    assert "[R-i7-11] D4+D8" in result.stderr
    _reset_after_rejection(sandbox)


def test_scaffold_marker_deletion_is_accepted(sandbox: Sandbox):
    rel = "trees/.gitkeep"
    (sandbox.root / rel).unlink()
    assert sandbox.git("add", "--", rel).returncode == 0

    result = _commit_staged(sandbox, "remove optional scaffold marker", role="harness")

    assert result.returncode == 0, result.stderr
    assert sandbox.git("cat-file", "-e", f"HEAD:{rel}").returncode != 0


@pytest.mark.parametrize("role", sorted(authority.ROLES))
def test_scaffold_marker_content_is_rejected_for_every_role(
    sandbox: Sandbox,
    role: str,
):
    rel = "tier1/merge-records/.gitkeep"
    sandbox.write_file(rel, "role bypass attempt\n")
    assert sandbox.git("add", rel).returncode == 0

    result = _commit_staged(sandbox, "try role bypass through marker", role=role)

    assert result.returncode != 0
    assert "must be empty bytes" in result.stderr
    assert "[R-i7-7]" in result.stderr
    assert rel in result.stderr
    _reset_after_rejection(sandbox)


def test_generic_tier1_hand_edit_without_ht_commit_is_out_of_band(
    sandbox: Sandbox,
):
    rel = "tier1/issues/.gitkeep"
    sandbox.write_file(rel, "hand-edited\n")
    assert sandbox.git("add", rel).returncode == 0

    result = sandbox.git(
        "commit",
        "-m",
        "hand-edit tier1 state",
        env_extra={"HT_ROLE": "pc"},
    )
    assert result.returncode != 0
    assert "out-of-band" in result.stderr
    assert "use ht" in result.stderr
    assert rel in result.stderr
    _reset_after_rejection(sandbox)
    assert (sandbox.root / rel).read_text() == ""


def test_pc_issue_phase_transitions_are_rejected_in_sign_off(
    sandbox: Sandbox,
):
    rel, issue = _seed_issue(sandbox)

    ratified = {**issue, "status": "ratified"}
    _stage_document(sandbox, rel, ratified)
    result = _commit_staged(sandbox, "pc ratifies issue", role="pc")
    assert result.returncode != 0
    assert "issue.status" in result.stderr
    assert "owner: user" in result.stderr
    assert "A1 §10" in result.stderr
    _reset_after_rejection(sandbox)

    _commit_document(
        sandbox,
        rel,
        ratified,
        role="user",
        message="user ratifies issue",
    )
    active = {**ratified, "status": "active"}
    _stage_document(sandbox, rel, active)
    result = _commit_staged(sandbox, "pc activates issue", role="pc")
    assert result.returncode != 0
    assert "issue.status" in result.stderr
    assert "owner: user" in result.stderr
    assert "A1 §10" in result.stderr
    _reset_after_rejection(sandbox)
    assert sandbox.load(rel)["status"] == "ratified"


def test_pc_cannot_create_active_issue_in_sign_off(sandbox: Sandbox):
    rel = "tier1/issues/I-1.json"
    _stage_document(sandbox, rel, _issue(status="active"))
    result = _commit_staged(sandbox, "pc creates active issue", role="pc")
    assert result.returncode != 0
    assert "issue.status" in result.stderr
    assert "owner: user" in result.stderr
    assert "A1 §10" in result.stderr
    _reset_after_rejection(sandbox)


def test_pc_issue_phase_transitions_succeed_in_autonomy(sandbox: Sandbox):
    phase = sandbox.run("phase", "set", "autonomy", role="user")
    assert phase.returncode == 0, phase.stderr
    rel, issue = _seed_issue(sandbox)

    ratified = {**issue, "status": "ratified"}
    _commit_document(
        sandbox,
        rel,
        ratified,
        role="pc",
        message="pc ratifies issue in autonomy",
    )
    active = {**ratified, "status": "active"}
    _commit_document(
        sandbox,
        rel,
        active,
        role="pc",
        message="pc activates issue in autonomy",
    )
    assert sandbox.load(rel)["status"] == "active"


def test_user_issue_phase_transitions_succeed_in_sign_off(sandbox: Sandbox):
    rel, issue = _seed_issue(sandbox)

    ratified = {**issue, "status": "ratified"}
    _commit_document(
        sandbox,
        rel,
        ratified,
        role="user",
        message="user ratifies issue",
    )
    active = {**ratified, "status": "active"}
    _commit_document(
        sandbox,
        rel,
        active,
        role="user",
        message="user activates issue",
    )
    assert sandbox.load(rel)["status"] == "active"


def test_ratification_payload_mutation_is_rejected(sandbox: Sandbox):
    rel, item = _seed_ratification_item(sandbox)
    rewritten = {**item, "text": "Replace the queued request."}
    _stage_document(sandbox, rel, rewritten)

    result = _commit_staged(sandbox, "rewrite ratification payload", role="cgate")
    assert result.returncode != 0
    assert "ratification_item.text" in result.stderr
    assert "frozen-after-creation" in result.stderr
    assert "append-only" in result.stderr
    _reset_after_rejection(sandbox)
    assert sandbox.load(rel)["text"] == item["text"]


def test_pc_ratification_annotation_append_succeeds_but_rewrite_and_removal_fail(
    sandbox: Sandbox,
):
    rel, item = _seed_ratification_item(sandbox)
    annotation = {
        "date": "2026-07-12",
        "note": "PC context for the user.",
    }
    annotated = {**item, "annotations": [annotation]}
    _commit_document(
        sandbox,
        rel,
        annotated,
        role="pc",
        message="append PC ratification annotation",
    )

    rewritten = {
        **annotated,
        "annotations": [{**annotation, "note": "Rewrite prior PC context."}],
    }
    _stage_document(sandbox, rel, rewritten)
    result = _commit_staged(sandbox, "rewrite PC annotation", role="pc")
    assert result.returncode != 0
    assert "ratification_item.annotations is append-only" in result.stderr
    assert "owner: pc" in result.stderr
    _reset_after_rejection(sandbox)

    removed = {**annotated, "annotations": []}
    _stage_document(sandbox, rel, removed)
    result = _commit_staged(sandbox, "remove PC annotation", role="pc")
    assert result.returncode != 0
    assert "ratification_item.annotations is append-only" in result.stderr
    assert "owner: pc" in result.stderr
    _reset_after_rejection(sandbox)
    assert sandbox.load(rel)["annotations"] == [annotation]


def test_ratification_disposition_is_user_only_and_frozen_after_write(
    sandbox: Sandbox,
):
    rel, item = _seed_ratification_item(sandbox)
    accepted = {
        **item,
        "disposition": {
            "status": "accepted",
            "by": "user",
            "date": "2026-07-12",
            "note": "Approved.",
        },
    }
    _stage_document(sandbox, rel, accepted)
    result = _commit_staged(sandbox, "pc disposes ratification", role="pc")
    assert result.returncode != 0
    assert "ratification_item.disposition" in result.stderr
    assert "owner: user" in result.stderr
    assert "A1 §10" in result.stderr
    _reset_after_rejection(sandbox)

    _commit_document(
        sandbox,
        rel,
        accepted,
        role="user",
        message="user accepts ratification",
    )
    rewritten = {
        **accepted,
        "disposition": {
            "status": "rejected",
            "by": "user",
            "date": "2026-07-13",
            "note": "Changed outcome.",
        },
    }
    _stage_document(sandbox, rel, rewritten)
    result = _commit_staged(sandbox, "rewrite terminal disposition", role="user")
    assert result.returncode != 0
    assert "ratification_item.disposition is frozen" in result.stderr
    assert "owner: user" in result.stderr
    _reset_after_rejection(sandbox)
    assert sandbox.load(rel)["disposition"] == accepted["disposition"]


def test_appender_cannot_birth_non_null_disposition(
    sandbox: Sandbox,
):
    rel = "tier1/ratification-queue/RQ-1.json"
    item = {
        **_ratification_item(),
        "disposition": {
            "status": "deferred",
            "by": "user",
            "date": "2026-07-12",
            "note": "Wait for another result.",
        },
    }
    _stage_document(sandbox, rel, item)
    result = _commit_staged(sandbox, "queue pre-disposed item", role="cgate")
    assert result.returncode != 0
    assert "ratification_item.disposition" in result.stderr
    assert "owner: user" in result.stderr
    _reset_after_rejection(sandbox)
    assert not (sandbox.root / rel).exists()


def test_ratification_deletion_and_rename_are_rejected(sandbox: Sandbox):
    rel, _ = _seed_ratification_item(sandbox)
    (sandbox.root / rel).unlink()
    assert sandbox.git("add", "--", rel).returncode == 0

    result = _commit_staged(sandbox, "delete ratification", role="cgate")
    assert result.returncode != 0
    assert "removes or renames" in result.stderr
    assert "append-only" in result.stderr
    _reset_after_rejection(sandbox)
    assert (sandbox.root / rel).exists()

    destination = "notes/RQ-1.json"
    (sandbox.root / "notes").mkdir()
    moved = sandbox.git("mv", rel, destination)
    assert moved.returncode == 0, moved.stderr
    result = _commit_staged(sandbox, "rename ratification", role="cgate")
    assert result.returncode != 0
    assert "removes or renames" in result.stderr
    assert "append-only" in result.stderr
    _reset_after_rejection(sandbox)
    assert (sandbox.root / rel).exists()
    assert not (sandbox.root / destination).exists()
