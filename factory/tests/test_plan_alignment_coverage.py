"""Owner-docket Q3 — deterministic plan-alignment coverage at readiness submission."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import harnessd.addressing as addressing
import harnessd.contracts as contracts
import harnessd.daemon as daemon
import harnessd.fencing as fencing
import harnessd.ledger as ledger
import harnessd.notary as notary
from harnessd.spawn import chokepoint


L1 = "proj#exec"
L2 = "proj/widget#exec"
SESSION = "sess-plan-coverage-0001"
SUBAGENT = "subagent-plan-coverage"


@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _binding(
    node_address: str,
    *,
    parent_address=None,
    level="L2",
    extra=None,
):
    token = fencing.mint_owner_token(node_address, SUBAGENT, SESSION, 1)
    rec = {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": level,
        "subagent_id": SUBAGENT,
        "session_uuid": SESSION,
        "state": "running",
        "generation": 0,
        "lease_epoch": 1,
        "owner_token": token,
        "last_applied_seq": 0,
        "spec_pointer": "design/intent-spec.md",
        "frozen_acceptance_ref": "acceptance.md",
        "last_inbox_acked_offset": 0,
        "stale_check_count": 0,
        "stale_grace_checks": 2,
        "recovery_attempts": 0,
        "paused_at": None,
        "terminal_signal": None,
        "transcript_path": None,
        "liveness_state": "idle",
        "last_progress_at": _now_iso(),
    }
    if extra:
        rec.update(extra)
    return rec, token


def _seed(bindings):
    ledger.write_binding({b["node_address"]: copy.deepcopy(b) for b in bindings}, _lock_held=True)


def _intent_text() -> str:
    return """# Intent Spec

## Requirements

| ID | Requirement | Tag | Priority | MNF | Parent | Fluency | Reflect-back status |
|---|---|---|---|---|---|---|---|
| R-001 | Ordinary decided behavior | decided | must | — | O-1 | plain | confirmed |
| R-002 | Safety boundary | decided | must | YES | O-1 | plain | confirmed |
| R-003 | Delegated implementation outcome | delegated | should | — | O-1 | technical | confirmed |
| R-004 | Parked later scope | deferred | could | — | O-1 | plain | pending |

## ID → intent-span map

| ID | Intent span |
|---|---|
| R-001 | Ordinary decided behavior. |
| R-002 | Safety boundary. |
| R-003 | Delegated implementation outcome. |
| R-004 | Parked later scope. |

## Reflect-back script

Status: confirmed

Build the decided and delegated behavior with the named safety boundary.
"""


def _trace(
    element_id: str,
    *,
    serves: tuple[str, ...],
    kind: str,
    level: str = "L3",
) -> str:
    served = ", ".join(serves)
    return (
        f"<!-- trace: {{ id: {element_id}, serves: [{served}], kind: {kind}, "
        "level: "
        f"{level}, node: proj/widget }} -->"
    )


def _valid_elements(*, include_dd: bool = True) -> list[str]:
    rows = [
        _trace("R-001.1", serves=("R-001",), kind="design"),
        _trace("TST-001", serves=("R-001",), kind="test"),
        _trace("R-002.1", serves=("R-002",), kind="design"),
        _trace("TST-002-FAIL", serves=("R-002",), kind="test"),
        _trace("R-003.1", serves=("R-003",), kind="design"),
        _trace("TST-003", serves=("R-003",), kind="test"),
    ]
    if include_dd:
        rows.append(_trace("DD-001", serves=(), kind="decision", level="L2"))
    return rows


def _prepare(
    runtime,
    *,
    elements=None,
    failure_path_criteria=None,
    artifact_refs=None,
    intent_text=None,
):
    node_dir = addressing.node_dir(L2, runtime)
    plan_dir = node_dir / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    intent = node_dir / "client-brief" / "intent-spec.md"
    intent.parent.mkdir(parents=True, exist_ok=True)
    authored_intent = intent_text or _intent_text()
    if "## ID → intent-span map" not in authored_intent:
        intent_ids = list(
            dict.fromkeys(re.findall(r"R-\d+(?:\.\d+)*", authored_intent))
        )
        authored_intent += (
            "\n## ID → intent-span map\n\n| ID | Intent span |\n|---|---|\n"
            + "".join(f"| {value} | Preserved {value} intent span. |\\n" for value in intent_ids)
            + "\n## Reflect-back script\n\nStatus: confirmed\n\nConfirmed test intent.\n"
        )
    intent.write_text(authored_intent, encoding="utf-8")
    raw_request = node_dir / "client-brief" / "raw-request.md"
    raw_request.write_text("Build the requested covered behavior safely.\n", encoding="utf-8")
    intent_receipt = contracts.contract_receipt(
        L2,
        L1,
        intent,
        notary.stamp(intent),
    )
    raw_request_receipt = contracts.contract_receipt(
        L2,
        L1,
        raw_request,
        notary.stamp(raw_request),
    )
    package = plan_dir / "validated-plan-package.md"
    package.write_text("# Validated Plan Package\n", encoding="utf-8")
    authored_elements = elements or _valid_elements()
    architecture = plan_dir / "construction.md"
    construction_elements = [
        row for row in authored_elements if "kind: test," not in row
    ]
    verification = plan_dir / "verification.md"
    verification_elements = [
        row for row in authored_elements if "kind: test," in row
    ]
    architecture.write_text(
        "# Construction\n\n" + "\n\n".join(construction_elements) + "\n",
        encoding="utf-8",
    )
    verification.write_text(
        "# Verification\n\n" + "\n\n".join(verification_elements) + "\n",
        encoding="utf-8",
    )
    construction_trace_ids = [
        row.split("id:", 1)[1].split(",", 1)[0].strip()
        for row in construction_elements
    ]
    verification_trace_ids = [
        row.split("id:", 1)[1].split(",", 1)[0].strip()
        for row in verification_elements
    ]
    artifact_rows = artifact_refs or [
        {"path": "plan/construction.md", "trace_ids": construction_trace_ids},
        {"path": "plan/verification.md", "trace_ids": verification_trace_ids},
    ]
    if artifact_refs is not None:
        artifact_rows = [
            row
            if isinstance(row, dict)
            else {
                "path": row,
                "trace_ids": construction_trace_ids + verification_trace_ids,
            }
            for row in artifact_refs
        ]
    manifest = plan_dir / "plan-alignment-coverage.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": artifact_rows,
                "failure_path_criteria": failure_path_criteria
                if failure_path_criteria is not None
                else [
                    {
                        "requirement_id": "R-002",
                        "test_id": "TST-002-FAIL",
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    semantic_manifest = plan_dir / "plan-alignment-semantic.json"
    semantic_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "verification_artifacts": [
                    {"path": "plan/verification.md", "module": "widget"}
                ],
                "construction_modules": [
                    {
                        "module": "widget",
                        "artifacts": ["plan/construction.md"],
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    marker = node_dir / "plan-alignment-ready.json"
    marker.write_text(
        json.dumps(
            {
                "type": "plan_alignment_ready",
                "package": "plan/validated-plan-package.md",
                "coverage_manifest": "plan/plan-alignment-coverage.json",
                "semantic_manifest": "plan/plan-alignment-semantic.json",
                "message": "Validated plan package is ready for L1 plan-alignment review.",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "node_dir": node_dir,
        "intent": intent,
        "intent_receipt": intent_receipt,
        "raw_request_receipt": raw_request_receipt,
        "package": package,
        "architecture": architecture,
        "manifest": manifest,
        "semantic_manifest": semantic_manifest,
        "verification": verification,
        "marker": marker,
    }


def _submit(runtime, prepared):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, token = _binding(
        L2,
        parent_address=L1,
        level="L2",
        extra={
            "intent_spec_receipt": prepared["intent_receipt"],
            "raw_request_receipt": prepared["raw_request_receipt"],
        },
    )
    _seed([l1, l2])
    result = chokepoint.submit_plan_alignment_ready(
        L2,
        marker_path=prepared["marker"],
        expected_owner_token=token,
    )
    return result


def _read_report(binding: dict) -> dict:
    report_path = binding["plan_alignment_coverage_report"]
    report = json.loads(open(report_path, encoding="utf-8").read())
    assert binding["plan_alignment_coverage_report_sha256"]
    assert binding["plan_alignment_bundle_sha256"] in report_path
    return report


def test_complete_graph_passes_and_lists_deferred_and_dd_exemptions(runtime):
    prepared = _prepare(runtime)

    result = _submit(runtime, prepared)

    assert result.ok is True
    live = ledger.read_binding(L2)
    assert live["plan_alignment_state"] == "semantic_cell_pending"
    report = _read_report(live)
    assert report["status"] == "pass"
    assert report["defects"] == []
    assert report["deferred_requirements"] == [
        {"id": "R-004", "exempt_reason": "deferred"}
    ]
    assert report["excluded_decisions"] == ["DD-001"]
    rows = {row["id"]: row for row in report["requirements"]}
    assert rows["R-001"]["design_ids"] == ["R-001.1"]
    assert rows["R-001"]["test_ids"] == ["TST-001"]
    assert rows["R-002"]["failure_path_test_ids"] == ["TST-002-FAIL"]
    assert rows["R-004"]["disposition"] == "exempt"
    assert addressing.inbox_path(L1, runtime).exists() is False


@pytest.mark.parametrize(
    ("remove_id", "expected"),
    [
        ("R-001.1", "UNCOVERED-R-001"),
        ("TST-003", "UNTESTED-R-003"),
    ],
)
def test_forward_coverage_defects_are_gate_hard(runtime, remove_id, expected):
    elements = [row for row in _valid_elements() if f"id: {remove_id}," not in row]
    prepared = _prepare(runtime, elements=elements)

    result = _submit(runtime, prepared)

    assert result.ok is False
    assert expected in result.errors
    live = ledger.read_binding(L2)
    assert live.get("plan_alignment_state") is None
    assert addressing.inbox_path(L1, runtime).exists() is False
    report_pointer = next(
        error.split("=", 1)[1]
        for error in result.errors
        if error.startswith("PLAN-ALIGNMENT-COVERAGE-REPORT=")
    )
    report = json.loads(open(report_pointer, encoding="utf-8").read())
    assert expected in report["defects"]


def test_every_mnf_needs_an_in_package_failure_path_mapping(runtime):
    prepared = _prepare(runtime, failure_path_criteria=[])

    result = _submit(runtime, prepared)

    assert result.ok is False
    assert "MNF-NO-FAILURE-PATH-R-002" in result.errors


def test_each_nested_mnf_id_needs_its_own_row_and_walks_the_id_graph(runtime):
    intent_text = """# Intent Spec

## Requirements

| ID | Requirement | Tag | Priority | MNF | Parent | Fluency | Reflect-back status |
|---|---|---|---|---|---|---|---|
| R-010 | Parent safety boundary | decided | must | YES | O-1 | plain | confirmed |
| R-010.1 | Atomic safety obligation | decided | must | YES | R-010 | plain | confirmed |
"""
    elements = [
        _trace("R-010.1.1", serves=("R-010.1",), kind="design"),
        _trace("TST-NESTED-FAIL", serves=("R-010.1",), kind="test"),
    ]
    missing_parent_row = _prepare(
        runtime,
        intent_text=intent_text,
        elements=elements,
        failure_path_criteria=[
            {"requirement_id": "R-010.1", "test_id": "TST-NESTED-FAIL"}
        ],
    )
    refused = _submit(runtime, missing_parent_row)
    assert refused.ok is False
    assert "MNF-NO-FAILURE-PATH-R-010" in refused.errors
    assert "UNCOVERED-R-010" not in refused.errors
    assert "UNTESTED-R-010" not in refused.errors

    missing_parent_row["manifest"].write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [
                    {
                        "path": "plan/construction.md",
                        "trace_ids": ["R-010.1.1"],
                    },
                    {
                        "path": "plan/verification.md",
                        "trace_ids": ["TST-NESTED-FAIL"],
                    },
                ],
                "failure_path_criteria": [
                    {"requirement_id": "R-010", "test_id": "TST-NESTED-FAIL"},
                    {"requirement_id": "R-010.1", "test_id": "TST-NESTED-FAIL"},
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    live = ledger.read_binding(L2)
    accepted = chokepoint.submit_plan_alignment_ready(
        L2,
        marker_path=missing_parent_row["marker"],
        expected_owner_token=live["owner_token"],
    )
    assert accepted.ok is True
    rows = {row["id"]: row for row in _read_report(ledger.read_binding(L2))["requirements"]}
    assert rows["R-010"]["failure_path_test_ids"] == ["TST-NESTED-FAIL"]
    assert rows["R-010.1"]["failure_path_test_ids"] == ["TST-NESTED-FAIL"]


def test_dangling_sidecar_test_is_typed_and_never_counts_as_coverage(runtime):
    prepared = _prepare(
        runtime,
        failure_path_criteria=[
            {"requirement_id": "R-002", "test_id": "TST-DOES-NOT-EXIST"}
        ],
    )

    result = _submit(runtime, prepared)

    assert result.ok is False
    assert "SIDECAR-DANGLING-TST-DOES-NOT-EXIST" in result.errors
    assert "MNF-NO-FAILURE-PATH-R-002" in result.errors


def test_dangling_artifact_index_trace_id_is_typed(runtime):
    prepared = _prepare(
        runtime,
        artifact_refs=[
            {
                "path": "plan/construction.md",
                "trace_ids": [
                    *[
                        row.split("id:", 1)[1].split(",", 1)[0].strip()
                        for row in _valid_elements()
                        if "kind: test," not in row
                    ],
                    "R-999.1",
                ],
            },
            {
                "path": "plan/verification.md",
                "trace_ids": [
                    row.split("id:", 1)[1].split(",", 1)[0].strip()
                    for row in _valid_elements()
                    if "kind: test," in row
                ],
            },
        ],
    )

    result = _submit(runtime, prepared)

    assert result.ok is False
    assert "SIDECAR-DANGLING-R-999.1" in result.errors


def test_orphan_and_unserved_dr_are_gate_hard_while_dd_is_excluded(runtime):
    elements = _valid_elements()
    elements.extend(
        [
            _trace("ORPHAN-ELEMENT", serves=(), kind="design"),
            _trace("DR-004a", serves=("R-004",), kind="derived"),
        ]
    )
    prepared = _prepare(runtime, elements=elements)

    result = _submit(runtime, prepared)

    assert result.ok is False
    assert "ORPHAN-ORPHAN-ELEMENT" in result.errors
    assert "DR-UNSERVED-DR-004a" in result.errors
    assert not any(error == "ORPHAN-DD-001" for error in result.errors)


def test_valid_dr_serves_link_can_anchor_design_and_test_without_becoming_creep(runtime):
    elements = [
        _trace("DR-001a", serves=("R-001",), kind="derived"),
        _trace("DERIVED-DESIGN", serves=("DR-001a",), kind="design"),
        _trace("TST-DERIVED", serves=("DR-001a",), kind="test"),
        _trace("R-002.1", serves=("R-002",), kind="design"),
        _trace("TST-002-FAIL", serves=("R-002",), kind="test"),
        _trace("R-003.1", serves=("R-003",), kind="design"),
        _trace("TST-003", serves=("R-003",), kind="test"),
    ]
    prepared = _prepare(runtime, elements=elements)

    result = _submit(runtime, prepared)

    assert result.ok is True
    report = _read_report(ledger.read_binding(L2))
    assert not any(defect.startswith("ORPHAN-") for defect in report["defects"])
    row = next(row for row in report["requirements"] if row["id"] == "R-001")
    assert row["design_ids"] == ["DERIVED-DESIGN"]
    assert row["test_ids"] == ["TST-DERIVED"]


@pytest.mark.parametrize(
    "artifact_ref",
    ["../outside.md", "/tmp/outside.md", "plan/missing.md"],
)
def test_manifest_artifact_set_must_be_contained_and_present(runtime, artifact_ref):
    prepared = _prepare(runtime, artifact_refs=[artifact_ref])

    result = _submit(runtime, prepared)

    assert result.ok is False
    assert any(error.startswith("MALFORMED-PLAN-ALIGNMENT-MANIFEST") for error in result.errors)


def test_current_notary_receipt_is_the_only_requirement_metadata_source(runtime):
    prepared = _prepare(runtime)
    prepared["intent"].write_text(
        _intent_text().replace("R-003 | Delegated", "R-003 | Drifted delegated"),
        encoding="utf-8",
    )

    result = _submit(runtime, prepared)

    assert result.ok is False
    assert "STALE-INTENT-SPEC-RECEIPT" in result.errors


def test_same_marker_with_changed_package_content_gets_a_new_bundle_identity(runtime):
    prepared = _prepare(runtime)
    first = _submit(runtime, prepared)
    assert first.ok is True
    first_live = ledger.read_binding(L2)
    first_bundle = first_live["plan_alignment_bundle_sha256"]
    prepared["architecture"].write_text(
        prepared["architecture"].read_text(encoding="utf-8")
        + "\n"
        + _trace("DD-002", serves=(), kind="decision", level="L2")
        + "\n",
        encoding="utf-8",
    )

    second = chokepoint.submit_plan_alignment_ready(
        L2,
        marker_path=prepared["marker"],
        expected_owner_token=first_live["owner_token"],
    )

    assert second.ok is True
    second_live = ledger.read_binding(L2)
    assert second_live["plan_alignment_bundle_sha256"] != first_bundle
    assert _read_report(second_live)["excluded_decisions"] == ["DD-001", "DD-002"]
    assert second_live["plan_alignment_state"] == "semantic_cell_pending"
    assert addressing.inbox_path(L1, runtime).exists() is False


def test_daemon_refusal_uses_existing_marker_invalid_path_and_never_wakes_l1(runtime):
    prepared = _prepare(
        runtime,
        elements=[
            row for row in _valid_elements() if "id: R-001.1," not in row
        ],
    )
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, _ = _binding(
        L2,
        parent_address=L1,
        level="L2",
        extra={
            "intent_spec_receipt": prepared["intent_receipt"],
            "raw_request_receipt": prepared["raw_request_receipt"],
        },
    )
    _seed([l1, l2])

    daemon._submit_plan_alignment_markers_best_effort()
    daemon._submit_plan_alignment_markers_best_effort()

    live = ledger.read_binding(L2)
    assert live.get("plan_alignment_state") is None
    assert addressing.inbox_path(L1, runtime).exists() is False
    rows = [
        json.loads(raw)
        for raw in addressing.inbox_path(L2, runtime).read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["type"] == "nonterminal_marker_invalid"
    assert "UNCOVERED-R-001" in rows[0]["errors"]
    assert any(
        error.startswith("PLAN-ALIGNMENT-COVERAGE-REPORT=")
        for error in rows[0]["errors"]
    )


def test_authored_planning_surfaces_teach_the_machine_package_contract():
    root = Path(__file__).resolve().parents[1]
    l2_role = (root / "operational" / "L2" / "role.md").read_text(encoding="utf-8")
    l2_spawn = (root / "operational" / "L2" / "spawn-template.md").read_text(
        encoding="utf-8"
    )
    planning_surfaces = [
        (root / "operational" / "L3" / name).read_text(encoding="utf-8")
        for name in ("role.md", "config.md", "spawn-template.md", "planning-template.md")
    ]

    for text in (l2_role, l2_spawn):
        assert '"coverage_manifest"' in text
        assert '"semantic_manifest"' in text
        assert '"trace_ids"' in text
        assert '"failure_path_criteria"' in text
        assert "plan-alignment-semantic.json" in text
    assert "semantic_cell_pending" in l2_role
    assert "trace-neighbor graph" in l2_role
    assert "plan-alignment-coverage.<bundle-sha256>.json" in l2_role
    for text in planning_surfaces:
        assert "falsifiable acceptance criteria use `kind: test`" in text
        assert "acceptance criteria use `kind: requirement`" not in text
