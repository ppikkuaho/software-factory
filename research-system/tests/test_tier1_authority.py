"""Tier-1 authority foundations: roles, paths, schemas, phase, and R1 lane rule."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from conftest import Sandbox
from ht import authority, classify, pipeline, schemas
from ht.errors import HtError
from ht.paths import Root
from ht.pipeline import Plan


def _issue(**updates):
    doc = {
        "id": "I-1",
        "title": "Test the primary explanation",
        "provenance": ["user-request"],
        "scope": "The first-stage attribution question.",
        "question": "Does the primary explanation survive its cheapest test?",
        "done_definition": "The named test has a reproducible result.",
        "lanes": ["L1"],
        "subgoals": ["Run the cheapest discriminating test."],
        "status": "proposed",
        "closure": None,
    }
    doc.update(updates)
    return doc


def _ratification_item(kind: str = "cgate-escalation", **updates):
    doc = {
        "id": "RQ-1",
        "kind": kind,
        "payload_ref": "merge-record#MR-1",
        "text": "Resolve the composition-gate escalation.",
        "queued_by": "cgate",
        "date": "2026-07-12",
        "disposition": None,
        "annotations": [],
    }
    doc.update(updates)
    return doc


def _merge_record(**updates):
    doc = {
        "id": "MR-1",
        "candidate_ref": "candidate#C-1",
        "lane_verdict": "land",
        "scope": {"lane": "L4", "seats": [], "surfaces": [], "globs": []},
        "screen": {
            "results": [
                {
                    "check": "lane-evidence",
                    "result": "pass",
                    "detail": "The cited evidence is reproducible.",
                }
            ],
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
    doc.update(updates)
    return doc


def _gate_verdict(verdict: str = "land", **updates):
    doc = {
        "verdict": verdict,
        "date": "2026-07-12",
        "review_ref": "GR-1",
        "review_sha256": "a" * 64,
        "note": "All composition checks passed.",
    }
    doc.update(updates)
    return doc


def _assert_write_rejected(
    doc_type: str,
    old: dict | None,
    new: dict,
    role: str,
    *,
    field: str,
    owner: str,
    phase: str = "sign-off",
    d10: bool = False,
):
    with pytest.raises(HtError) as rejected:
        authority.check_write(doc_type, old, new, role, phase=phase)

    message = str(rejected.value)
    assert field in message
    assert f"owner: {owner}" in message
    assert "A1 §10" in message
    if d10:
        assert "; D10)" in message


def test_pc_and_cgate_are_registered_roles():
    assert {"pc", "cgate"} <= authority.ROLES


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        ("tier1/phase.json", "phase"),
        ("tier1/issues/I-1.json", "issue"),
        ("tier1/issues/I-2048.json", "issue"),
        ("tier1/decision-log/PCD-1.json", "pc_decision"),
        ("tier1/ratification-queue/RQ-12.json", "ratification_item"),
        ("tier1/merge-records/MR-99.json", "merge_record"),
    ],
)
def test_tier1_paths_classify_exactly(rel_path: str, expected: str):
    assert classify.doc_type(rel_path) == expected
    assert classify.in_state_lane(rel_path)


@pytest.mark.parametrize(
    "rel_path",
    [
        "tier1/phase.JSON",
        "tier1/phases.json",
        "tier1/issues/I-x.json",
        "tier1/issues/I-1.json.bak",
        "tier1/issues/nested/I-1.json",
        "tier1/decision-log/PCD-.json",
        "tier1/decision-log/pcd-1.json",
        "tier1/ratification-queue/RQ-1/extra.json",
        "tier1/merge-records/MR-1.json/extra",
        "prefix/tier1/phase.json",
    ],
)
def test_tier1_classification_rejects_near_misses(rel_path: str):
    assert classify.doc_type(rel_path) is None


def test_tier1_is_a_state_lane_without_prefix_bleed():
    assert classify.in_state_lane("tier1/.gitkeep")
    assert classify.in_state_lane("tier1/issues/not-state.txt")
    assert not classify.in_state_lane("tier1")
    assert not classify.in_state_lane("tier10/phase.json")
    assert not classify.in_state_lane("notes/tier1/phase.json")


def test_root_tier1_helpers_are_exact(tmp_path: Path):
    root = Root(tmp_path / "research")

    assert root.tier1_dir == root.path / "tier1"
    assert root.phase_json == root.path / "tier1/phase.json"
    assert root.issue_json("I-17") == root.path / "tier1/issues/I-17.json"
    assert (
        root.pc_decision_json("PCD-4")
        == root.path / "tier1/decision-log/PCD-4.json"
    )
    assert (
        root.ratification_item_json("RQ-9")
        == root.path / "tier1/ratification-queue/RQ-9.json"
    )
    assert (
        root.merge_record_json("MR-3")
        == root.path / "tier1/merge-records/MR-3.json"
    )


def test_phase_schema_accepts_valid_document(sandbox: Sandbox):
    schemas.validate(
        sandbox.root / "system/schemas",
        "phase",
        {"mode": "sign-off", "set_by": "user", "date": "2026-07-12"},
    )


@pytest.mark.parametrize(
    "doc",
    [
        {"mode": "pilot", "set_by": "user", "date": "2026-07-12"},
        {"mode": "autonomy", "set_by": "user"},
        {
            "mode": "autonomy",
            "set_by": "user",
            "date": "2026-07-12",
            "extra": True,
        },
    ],
)
def test_phase_schema_rejects_invalid_documents(sandbox: Sandbox, doc: dict):
    with pytest.raises(HtError, match="schema-nonconforming phase"):
        schemas.validate(sandbox.root / "system/schemas", "phase", doc)


def test_pc_decision_schema_accepts_valid_document(sandbox: Sandbox):
    schemas.validate(
        sandbox.root / "system/schemas",
        "pc_decision",
        {
            "id": "PCD-12",
            "date": "2026-07-12",
            "kind": "triage",
            "decision": "Chase the cheapest attribution first.",
            "context_refs": ["issue#I-2", "ledger#L-7"],
        },
    )


@pytest.mark.parametrize(
    "doc",
    [
        {
            "id": "PCD-x",
            "date": "2026-07-12",
            "kind": "triage",
            "decision": "Invalid id.",
            "context_refs": [],
        },
        {
            "id": "PCD-1",
            "date": "2026-07-12",
            "kind": "triage",
            "decision": "Invalid refs.",
            "context_refs": "issue#I-1",
        },
        {
            "id": "PCD-1",
            "date": "2026-07-12",
            "kind": "triage",
            "decision": "Unexpected field.",
            "context_refs": [],
            "extra": True,
        },
    ],
)
def test_pc_decision_schema_rejects_invalid_documents(
    sandbox: Sandbox, doc: dict
):
    with pytest.raises(HtError, match="schema-nonconforming pc_decision"):
        schemas.validate(sandbox.root / "system/schemas", "pc_decision", doc)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "PCD-2"),
        ("date", "2026-07-13"),
        ("kind", "re-rank"),
        ("decision", "Choose another issue."),
        ("context_refs", ["issue#I-2"]),
    ],
)
def test_pc_decision_fields_reject_wrong_role(field: str, value):
    old = {
        "id": "PCD-1",
        "date": "2026-07-12",
        "kind": "triage",
        "decision": "Activate issue I-1.",
        "context_refs": ["issue#I-1"],
    }
    new = {**old, field: value}

    with pytest.raises(HtError) as rejected:
        authority.check_write("pc_decision", old, new, "unit")

    message = str(rejected.value)
    assert f"pc_decision.{field}" in message
    assert "owner: pc" in message
    assert "A1 §10" in message


def test_issue_schema_accepts_minimal_document(sandbox: Sandbox):
    schemas.validate(sandbox.root / "system/schemas", "issue", _issue())


def test_issue_schema_accepts_closed_issue_with_nonempty_closure(
    sandbox: Sandbox,
):
    schemas.validate(
        sandbox.root / "system/schemas",
        "issue",
        _issue(
            status="closed",
            closure={"text": "The discriminating test passed.", "refs": []},
        ),
    )


@pytest.mark.parametrize(
    "doc",
    [
        _issue(id="I-x"),
        _issue(status="unknown"),
        _issue(closure={"text": "Premature closure.", "refs": []}),
        _issue(status="closed", closure=None),
        _issue(status="closed", closure={"text": "", "refs": []}),
        {k: v for k, v in _issue().items() if k != "closure"},
        _issue(extra=True),
    ],
)
def test_issue_schema_rejects_invalid_documents(sandbox: Sandbox, doc: dict):
    with pytest.raises(HtError, match="schema-nonconforming issue"):
        schemas.validate(sandbox.root / "system/schemas", "issue", doc)


def test_ratification_item_schema_accepts_null_undisposed_document(
    sandbox: Sandbox,
):
    schemas.validate(
        sandbox.root / "system/schemas",
        "ratification_item",
        _ratification_item(),
    )
    item_without_optional_annotations = _ratification_item()
    item_without_optional_annotations.pop("annotations")
    schemas.validate(
        sandbox.root / "system/schemas",
        "ratification_item",
        item_without_optional_annotations,
    )


def test_ratification_item_schema_accepts_terminal_disposition(
    sandbox: Sandbox,
):
    schemas.validate(
        sandbox.root / "system/schemas",
        "ratification_item",
        _ratification_item(
            disposition={
                "status": "accepted",
                "by": "user",
                "date": "2026-07-12",
                "note": "Approved after inspection.",
            },
            annotations=[
                {"date": "2026-07-12", "note": "PC context only."}
            ],
        ),
    )


@pytest.mark.parametrize(
    "doc",
    [
        _ratification_item(id="RQ-x"),
        _ratification_item(kind="unknown"),
        _ratification_item(
            disposition={
                "status": "open",
                "by": "user",
                "date": "2026-07-12",
            }
        ),
        _ratification_item(
            disposition={"status": "accepted", "date": "2026-07-12"}
        ),
        _ratification_item(
            disposition={"status": "accepted", "by": "", "date": "2026-07-12"}
        ),
        _ratification_item(
            annotations=[{"date": "2026-07-12", "note": ""}]
        ),
        _ratification_item(extra=True),
    ],
)
def test_ratification_item_schema_rejects_invalid_documents(
    sandbox: Sandbox, doc: dict
):
    with pytest.raises(HtError, match="schema-nonconforming ratification_item"):
        schemas.validate(
            sandbox.root / "system/schemas", "ratification_item", doc
        )


def test_merge_record_schema_accepts_canonical_document(sandbox: Sandbox):
    schemas.validate(
        sandbox.root / "system/schemas", "merge_record", _merge_record()
    )


def test_merge_record_schema_rejects_partial_screen_identity(sandbox: Sandbox):
    record = _merge_record()
    record["screen"] = {**record["screen"], "output_ref": "var/screen.json"}
    with pytest.raises(HtError, match="schema-nonconforming merge_record"):
        schemas.validate(sandbox.root / "system/schemas", "merge_record", record)


@pytest.mark.parametrize(
    "verdict",
    ["land", "escalate-stuck", "future-item-7-verdict"],
)
def test_merge_record_schema_accepts_any_nonempty_gate_verdict(
    sandbox: Sandbox, verdict: str
):
    schemas.validate(
        sandbox.root / "system/schemas",
        "merge_record",
        _merge_record(
            gate_verdict=_gate_verdict(
                verdict, note="User adjudication required."
            )
        ),
    )


@pytest.mark.parametrize(
    "doc",
    [
        _merge_record(id="MR-x"),
        _merge_record(lane_verdict=""),
        _merge_record(screen={"results": []}),
        _merge_record(
            screen={
                "results": [{"check": "lane-evidence", "result": "unknown"}],
                "log_ref": None,
            }
        ),
        _merge_record(gate_verdict={}),
        _merge_record(gate_verdict={"verdict": "", "date": "2026-07-12"}),
        _merge_record(gate_verdict={"verdict": "land"}),
        _merge_record(gate_verdict={"verdict": "land", "date": ""}),
        _merge_record(
            gate_verdict={
                "verdict": "land",
                "date": "2026-07-12",
                "extra": True,
            }
        ),
        {
            key: value
            for key, value in _merge_record().items()
            if key != "consumed_epoch"
        },
        _merge_record(consumed_epoch=-1),
        _merge_record(consumed_epoch="1"),
        _merge_record(created=None),
        _merge_record(extra=True),
    ],
)
def test_merge_record_schema_rejects_invalid_documents(
    sandbox: Sandbox, doc: dict
):
    with pytest.raises(HtError, match="schema-nonconforming merge_record"):
        schemas.validate(sandbox.root / "system/schemas", "merge_record", doc)


@pytest.mark.parametrize(
    ("field", "value", "owner"),
    [
        ("id", "I-2", "frozen-after-creation"),
        ("title", "A new title", "pc"),
        ("provenance", ["new-source"], "pc"),
        ("scope", "A new scope.", "pc"),
        ("question", "A new question?", "pc"),
        ("done_definition", "A new completion condition.", "pc"),
        ("lanes", ["L2"], "pc"),
        ("subgoals", ["A new subgoal."], "pc"),
        ("status", "parked", "pc"),
        ("closure", {"text": "Closed.", "refs": []}, "pc"),
    ],
)
def test_issue_field_rejection_matrix_names_field_owner_and_contract(
    field: str, value, owner: str
):
    old = _issue(status="active")
    new = {**old, field: value}
    _assert_write_rejected(
        "issue",
        old,
        new,
        "unit",
        field=f"issue.{field}",
        owner=owner,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "User override"),
        ("provenance", ["user-override"]),
        ("scope", "User scope override."),
        ("question", "User question override?"),
        ("done_definition", "User completion override."),
        ("lanes", ["L5"]),
        ("subgoals", ["User override subgoal."]),
        ("status", "parked"),
        ("closure", {"text": "User closed it.", "refs": []}),
    ],
)
def test_user_can_override_mutable_pc_owned_issue_fields(field: str, value):
    old = _issue(status="active")
    authority.check_write("issue", old, {**old, field: value}, "user")


def test_issue_id_is_immutable_for_pc_and_user():
    old = _issue()
    for role in ("pc", "user"):
        _assert_write_rejected(
            "issue",
            old,
            {**old, "id": "I-2"},
            role,
            field="issue.id",
            owner="frozen-after-creation",
            d10=True,
        )


@pytest.mark.parametrize(
    ("old_status", "new_status"),
    [("proposed", "ratified"), ("ratified", "active")],
)
def test_issue_phase_gates_both_ratification_and_activation_transitions(
    old_status: str, new_status: str
):
    old = _issue(status=old_status)
    new = _issue(status=new_status)

    _assert_write_rejected(
        "issue",
        old,
        new,
        "pc",
        field="issue.status",
        owner="user",
        phase="sign-off",
    )
    authority.check_write("issue", old, new, "pc", phase="autonomy")
    authority.check_write("issue", old, new, "user", phase="sign-off")
    authority.check_write("issue", old, new, "user", phase="autonomy")


@pytest.mark.parametrize("new_status", ["ratified", "active"])
def test_issue_creation_phase_gates_gated_destination_status(new_status: str):
    new = _issue(status=new_status)
    _assert_write_rejected(
        "issue",
        None,
        new,
        "pc",
        field="issue.status",
        owner="user",
        phase="sign-off",
    )
    authority.check_write("issue", None, new, "pc", phase="autonomy")
    authority.check_write("issue", None, new, "user", phase="sign-off")


@pytest.mark.parametrize("old", [None, _issue(status="proposed")])
def test_pc_cannot_bypass_sign_off_with_direct_active_write(old):
    _assert_write_rejected(
        "issue",
        old,
        _issue(status="active"),
        "pc",
        field="issue.status",
        owner="user",
        phase="sign-off",
    )


def test_pc_cannot_reenter_active_via_parked_in_sign_off():
    proposed = _issue(status="proposed")
    parked = _issue(status="parked")
    authority.check_write("issue", proposed, parked, "pc", phase="sign-off")

    _assert_write_rejected(
        "issue",
        parked,
        _issue(status="active"),
        "pc",
        field="issue.status",
        owner="user",
        phase="sign-off",
    )


def test_pc_cannot_reenter_active_after_creating_parked_issue_in_sign_off():
    parked = _issue(status="parked")
    authority.check_write("issue", None, parked, "pc", phase="sign-off")

    _assert_write_rejected(
        "issue",
        parked,
        _issue(status="active"),
        "pc",
        field="issue.status",
        owner="user",
        phase="sign-off",
    )


@pytest.mark.parametrize("old_status", ["withdrawn", "closed"])
def test_pc_cannot_reenter_active_from_terminal_status_in_sign_off(
    old_status: str,
):
    _assert_write_rejected(
        "issue",
        _issue(status=old_status),
        _issue(status="active"),
        "pc",
        field="issue.status",
        owner="user",
        phase="sign-off",
    )


@pytest.mark.parametrize("old_status", ["parked", "withdrawn", "closed"])
def test_phase_authorized_roles_can_reenter_active_from_any_terminal_status(
    old_status: str,
):
    old = _issue(status=old_status)
    active = _issue(status="active")
    authority.check_write("issue", old, active, "pc", phase="autonomy")
    authority.check_write("issue", old, active, "user", phase="sign-off")
    authority.check_write("issue", old, active, "user", phase="autonomy")


@pytest.mark.parametrize("terminal", ["closed", "parked", "withdrawn"])
def test_pc_can_write_terminal_issue_status(terminal: str):
    old = _issue(status="active")
    authority.check_write("issue", old, _issue(status=terminal), "pc")


@pytest.mark.parametrize(
    ("kind", "creator"),
    [
        ("cgate-escalation", "cgate"),
        ("tier3-ratification", "verifier"),
        ("improvement-note", "harness"),
        ("activation-request", "pc"),
    ],
)
def test_ratification_kind_selects_exact_creator(kind: str, creator: str):
    item = _ratification_item(kind=kind, queued_by=creator)
    authority.check_write("ratification_item", None, item, creator)

    wrong_role = next(
        role
        for role in ("cgate", "verifier", "harness", "pc")
        if role != creator
    )
    with pytest.raises(HtError) as rejected:
        authority.check_write("ratification_item", None, item, wrong_role)
    message = str(rejected.value)
    assert f"creator: {creator}" in message
    assert "A1 §10" in message

    if creator == "pc":
        with pytest.raises(HtError) as user_rejected:
            authority.check_write("ratification_item", None, item, "user")
        assert "creator: pc" in str(user_rejected.value)
        assert "A1 §10" in str(user_rejected.value)


def test_ratification_unknown_kind_is_rejected_before_creation():
    item = _ratification_item(kind="not-a-kind")
    with pytest.raises(HtError) as rejected:
        authority.check_write("ratification_item", None, item, "pc")
    assert "ratification_item.kind 'not-a-kind'" in str(rejected.value)
    assert "A1 §10" in str(rejected.value)


def test_ratification_queued_by_must_match_kind_selected_creator():
    item = _ratification_item(queued_by="pc")
    _assert_write_rejected(
        "ratification_item",
        None,
        item,
        "cgate",
        field="ratification_item.queued_by",
        owner="cgate",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "RQ-2"),
        ("kind", "activation-request"),
        ("payload_ref", "issue#I-2"),
        ("text", "Rewritten payload."),
        ("queued_by", "pc"),
        ("date", "2026-07-13"),
    ],
)
def test_ratification_payload_rows_are_frozen_after_creation(field: str, value):
    old = _ratification_item()
    _assert_write_rejected(
        "ratification_item",
        old,
        {**old, field: value},
        "cgate",
        field=f"ratification_item.{field}",
        owner="frozen-after-creation",
        d10=True,
    )


@pytest.mark.parametrize(
    ("kind", "creator"),
    [
        ("cgate-escalation", "cgate"),
        ("tier3-ratification", "verifier"),
        ("improvement-note", "harness"),
        ("activation-request", "pc"),
    ],
)
def test_ratification_appender_cannot_birth_user_disposition(
    kind: str, creator: str
):
    item = _ratification_item(
        kind=kind,
        queued_by=creator,
        disposition={
            "status": "accepted",
            "by": "user",
            "date": "2026-07-12",
        },
    )
    _assert_write_rejected(
        "ratification_item",
        None,
        item,
        creator,
        field="ratification_item.disposition",
        owner="user",
        d10=True,
    )


def test_user_must_fill_ratification_disposition_atomically():
    old = _ratification_item()
    _assert_write_rejected(
        "ratification_item",
        old,
        {
            **old,
            "disposition": {
                "status": "accepted",
                "by": "user",
            },
        },
        "user",
        field="ratification_item.disposition",
        owner="user",
        d10=True,
    )


def test_user_can_fill_null_ratification_disposition_once():
    old = _ratification_item()
    disposed = {
        **old,
        "disposition": {
            "status": "accepted",
            "by": "user",
            "date": "2026-07-12",
        },
    }
    authority.check_write("ratification_item", old, disposed, "user")

    _assert_write_rejected(
        "ratification_item",
        disposed,
        {
            **disposed,
            "disposition": {
                "status": "rejected",
                "by": "user",
                "date": "2026-07-13",
            },
        },
        "user",
        field="ratification_item.disposition",
        owner="user",
        d10=True,
    )


@pytest.mark.parametrize("role", ["pc", "cgate", "verifier", "harness"])
def test_only_user_can_write_ratification_disposition(role: str):
    old = _ratification_item()
    _assert_write_rejected(
        "ratification_item",
        old,
        {
            **old,
            "disposition": {
                "status": "deferred",
                "by": "user",
                "date": "2026-07-12",
            },
        },
        role,
        field="ratification_item.disposition",
        owner="user",
    )


def test_ratification_annotations_are_optional_and_pc_append_only():
    old_without_annotations = _ratification_item()
    old_without_annotations.pop("annotations")
    annotated = {
        **old_without_annotations,
        "annotations": [
            {"date": "2026-07-12", "note": "PC context."}
        ],
    }
    authority.check_write(
        "ratification_item", old_without_annotations, annotated, "pc"
    )
    _assert_write_rejected(
        "ratification_item",
        old_without_annotations,
        annotated,
        "user",
        field="ratification_item.annotations",
        owner="pc",
    )

    second = {
        **annotated,
        "annotations": [
            *annotated["annotations"],
            {"date": "2026-07-13", "note": "More PC context."},
        ],
    }
    authority.check_write("ratification_item", annotated, second, "pc")

    for invalid in (
        {**second, "annotations": second["annotations"][1:]},
        {**second, "annotations": list(reversed(second["annotations"]))},
    ):
        _assert_write_rejected(
            "ratification_item",
            second,
            invalid,
            "pc",
            field="ratification_item.annotations",
            owner="pc",
            d10=True,
        )


@pytest.mark.parametrize("role", ["cgate", "verifier", "harness"])
def test_non_pc_appenders_cannot_write_ratification_annotations(role: str):
    old = _ratification_item()
    _assert_write_rejected(
        "ratification_item",
        old,
        {
            **old,
            "annotations": [
                {
                    "date": "2026-07-12",
                    "note": "Unauthorized context.",
                }
            ],
        },
        role,
        field="ratification_item.annotations",
        owner="pc",
    )


@pytest.mark.parametrize(
    ("field", "value", "role", "owner"),
    [
        ("id", "MR-2", "harness", "frozen-after-creation"),
        (
            "created",
            "2026-07-13",
            "harness",
            "frozen-after-creation",
        ),
        (
            "candidate_ref",
            "candidate#C-2",
            "cgate",
            "frozen-after-creation",
        ),
        (
            "lane_verdict",
            "A changed transcript.",
            "cgate",
            "frozen-after-creation",
        ),
        (
            "screen",
            {
                "results": [{"check": "screen", "result": "pass"}],
                "log_ref": "log#2",
            },
            "cgate",
            "harness",
        ),
        ("watch_link", "watch#W-1", "cgate", "harness"),
        (
            "gate_verdict",
            {
                "verdict": "land",
                "date": "2026-07-12",
                "note": "Passed.",
            },
            "harness",
            "cgate",
        ),
        ("consumed_epoch", 1, "cgate", "harness"),
    ],
)
def test_merge_record_rejection_matrix_is_strict(
    field: str, value, role: str, owner: str
):
    old = _merge_record()
    _assert_write_rejected(
        "merge_record",
        old,
        {**old, field: value},
        role,
        field=f"merge_record.{field}",
        owner=owner,
        d10=owner == "frozen-after-creation",
    )


def test_merge_record_harness_and_cgate_can_only_write_their_own_rows():
    old = _merge_record()
    authority.check_write(
        "merge_record",
        old,
        {
            **old,
            "screen": {
                "results": [{"check": "screen", "result": "pass"}],
                "log_ref": "log#1",
            },
            "watch_link": "watch#W-1",
        },
        "harness",
    )
    authority.check_write(
        "merge_record",
        old,
        {
            **old,
            "gate_verdict": {
                **_gate_verdict(),
            },
        },
        "cgate",
    )


def test_merge_record_harness_cannot_birth_non_null_gate_verdict():
    record = _merge_record(
        gate_verdict=_gate_verdict(note="Smuggled at creation.")
    )
    _assert_write_rejected(
        "merge_record",
        None,
        record,
        "harness",
        field="merge_record.gate_verdict",
        owner="cgate",
    )


def test_merge_record_harness_cannot_birth_consumed():
    record = _merge_record(consumed_epoch=9)
    with pytest.raises(HtError, match="consumed_epoch must be null at creation"):
        authority.check_write("merge_record", None, record, "harness")


@pytest.mark.parametrize("verdict", ["land", "future-item-7-verdict"])
def test_merge_record_cgate_fills_any_nonempty_gate_verdict_atomically(
    verdict: str,
):
    old = _merge_record()
    filled = {
        **old,
        "gate_verdict": _gate_verdict(verdict),
    }
    authority.check_write("merge_record", old, filled, "cgate")


def test_merge_record_second_cgate_verdict_write_is_frozen():
    old = _merge_record()
    filled = {
        **old,
        "gate_verdict": _gate_verdict("escalate-stuck"),
    }
    rewritten = {
        **filled,
        "gate_verdict": {
            "verdict": "land",
            "date": "2026-07-13",
        },
    }
    _assert_write_rejected(
        "merge_record",
        filled,
        rewritten,
        "cgate",
        field="merge_record.gate_verdict",
        owner="cgate",
        d10=True,
    )


def test_merge_record_screen_is_frozen_after_gate_verdict():
    old = _merge_record(gate_verdict=_gate_verdict("hold"))
    rewritten = {
        **old,
        "screen": {
            "results": [{"check": "scope-overlap", "result": "fail"}],
            "log_ref": "var/recomputed.json",
        },
    }
    with pytest.raises(HtError, match=r"screen is frozen after gate verdict.*D2"):
        authority.check_write("merge_record", old, rewritten, "harness")


def test_merge_record_non_cgate_cannot_fill_gate_verdict():
    old = _merge_record()
    filled = {
        **old,
        "gate_verdict": _gate_verdict("escalate-stuck"),
    }
    _assert_write_rejected(
        "merge_record",
        old,
        filled,
        "harness",
        field="merge_record.gate_verdict",
        owner="cgate",
    )


@pytest.mark.parametrize(
    "gate_verdict",
    [
        {},
        {"verdict": "land"},
        {"verdict": "land", "date": ""},
        {"verdict": "", "date": "2026-07-12"},
    ],
)
def test_merge_record_cgate_incomplete_or_empty_fill_is_rejected(
    gate_verdict: dict,
):
    old = _merge_record()
    with pytest.raises(
        HtError,
        match=(
            "merge_record.gate_verdict must be filled atomically from null to "
            "a non-empty verdict with date"
        ),
    ):
        authority.check_write(
            "merge_record",
            old,
            {**old, "gate_verdict": gate_verdict},
            "cgate",
        )


def test_merge_record_is_created_only_by_harness():
    record = _merge_record()
    authority.check_write("merge_record", None, record, "harness")

    with pytest.raises(HtError) as rejected:
        authority.check_write("merge_record", None, record, "cgate")
    message = str(rejected.value)
    assert "creator: harness" in message
    assert "A1 §10" in message


def test_merge_record_identity_and_creation_dates_are_immutable():
    old = _merge_record()
    for role in ("harness", "cgate", "user"):
        for field, value in (
            ("id", "MR-2"),
            ("lane_verdict", "A rewritten lane transcript."),
            (
                "scope",
                {"lane": "L4", "seats": ["other"], "surfaces": [], "globs": []},
            ),
            ("created", "2026-07-13"),
        ):
            _assert_write_rejected(
                "merge_record",
                old,
                {**old, field: value},
                role,
                field=f"merge_record.{field}",
                owner="frozen-after-creation",
            )


@pytest.mark.parametrize("role", sorted(authority.ROLES - {"harness", "director"}))
def test_merge_record_consumed_epoch_rejects_non_mechanical_roles(
    role: str,
):
    old = _merge_record()
    with pytest.raises(HtError) as rejected:
        authority.check_write(
            "merge_record",
            old,
            {**old, "consumed_epoch": 1},
            role,
        )

    assert "merge_record.consumed_epoch" in str(rejected.value)


@pytest.mark.parametrize("role", ["harness"])
def test_merge_record_consumed_epoch_rejects_hand_mutation_without_derived_seam(
    role: str,
):
    old = _merge_record()
    with pytest.raises(HtError, match="derived mechanical-write seam"):
        authority.check_write(
            "merge_record",
            old,
            {**old, "consumed_epoch": 1},
            role,
        )


@pytest.mark.parametrize("role", ["director"])
def test_merge_record_consumed_epoch_rejects_non_harness_mutation(role: str):
    old = _merge_record()
    with pytest.raises(HtError, match="derived mechanical-write seam"):
        authority.check_write(
            "merge_record",
            old,
            {**old, "consumed_epoch": 1},
            role,
        )


def test_merge_record_consumed_epoch_accepts_director_triggered_derived_write():
    old = _merge_record()
    authority.check_write(
        "merge_record",
        old,
        {**old, "consumed_epoch": 1},
        "director",
        mechanical_fields=frozenset({"consumed_epoch"}),
    )


def test_phase_set_by_user_creates_and_updates_phase(sandbox: Sandbox):
    first = sandbox.run("phase", "set", "sign-off", role="user")
    assert first.returncode == 0, first.stderr
    assert sandbox.load("tier1/phase.json") == {
        "mode": "sign-off",
        "set_by": "user",
        "date": datetime.date.today().isoformat(),
    }

    second = sandbox.run("phase", "set", "autonomy", role="user")
    assert second.returncode == 0, second.stderr
    assert sandbox.load("tier1/phase.json")["mode"] == "autonomy"


@pytest.mark.parametrize("role", ["pc", "cgate"])
def test_phase_set_by_non_user_is_rejected(sandbox: Sandbox, role: str):
    result = sandbox.run("phase", "set", "autonomy", role=role)
    assert result.returncode != 0
    assert role in result.stderr
    assert "user" in result.stderr
    assert "A1 §10" in result.stderr
    assert not (sandbox.root / "tier1/phase.json").exists()


def test_absent_phase_defaults_to_sign_off(tmp_path: Path):
    root = Root(tmp_path)
    plan = Plan(role="pc", message="no writes")

    assert not root.phase_json.exists()
    assert pipeline._phase_mode(root, plan) == "sign-off"


def test_verifier_lane_r1_contract_and_docstring():
    assert not authority.verifier_lane_authorized(
        "top", authority.UNASSIGNED_LANE
    )
    assert not authority.verifier_lane_authorized("top")
    assert not authority.verifier_lane_authorized("top", "")
    assert not authority.verifier_lane_authorized("top", "  \t")
    assert authority.verifier_lane_authorized("top", "L4")
    assert authority.verifier_lane_authorized("top", "  L4  ")
    assert (
        "absent HT_LANE is unassigned and most-restrictive; "
        "item 1 activates book routing."
        in (authority.verifier_lane_authorized.__doc__ or "")
    )


def test_fresh_root_scaffolds_tier1_and_validates(sandbox: Sandbox):
    for store in (
        "issues",
        "decision-log",
        "ratification-queue",
        "merge-records",
    ):
        directory = sandbox.root / "tier1" / store
        assert directory.is_dir()
        assert (directory / ".gitkeep").is_file()

    assert not (sandbox.root / "tier1/phase.json").exists()

    result = sandbox.run("validate", "--all")
    assert result.returncode == 0, result.stderr
    assert "OK: all state files schema-valid" in result.stdout
