"""Q8 — ratified judgment-instruction calibration application."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from harnessd import (
    config,
    instruction_calibration,
    plan_alignment_cell as cell,
    product_probes,
)


ROOT = Path(__file__).resolve().parents[1]


def _reconstruction_claim(
    *,
    claim_kind: str,
    behavior: str,
    missing: str,
    stimulus: str = "",
    observable: str = "",
) -> dict:
    return {
        "requirement_id": "R-001",
        "claim_id": f"claim-{claim_kind}",
        "behavior": behavior,
        "claim_kind": claim_kind,
        "missing": missing,
        "stimulus": stimulus,
        "observable": observable,
    }


def _reconstruction_report(window: str, claim: dict) -> dict:
    return {
        "schema_version": 1,
        "bundle_sha256": "c" * 64,
        "window": window,
        "scope_prefixes": ["R-001"],
        "claims": [claim],
        "assumptions": [],
    }


def _empty_comparator() -> dict:
    return {
        "schema_version": 1,
        "bundle_sha256": "c" * 64,
        "scope_prefixes": ["R-001"],
        "window_splits": [],
        "intent_findings": [],
        "mnf_adequacy": [],
    }


def _empty_coherence() -> dict:
    return {
        "schema_version": 1,
        "bundle_sha256": "c" * 64,
        "modules_read": [],
        "shared_assumptions": [],
        "contradictions": [],
    }


def _empty_atomization() -> dict:
    return {
        "schema_version": 1,
        "intent_fingerprint": "i" * 64,
        "findings": [],
    }


def test_reconstruction_closed_shape_accepts_only_ruled_missing_semantics():
    determined = _reconstruction_claim(
        claim_kind="input_output",
        behavior="returns the stored widget",
        missing="",
        stimulus="known widget id",
        observable="stored widget",
    )
    undetermined = _reconstruction_claim(
        claim_kind="undetermined",
        behavior="UNDETERMINED by these artifacts",
        missing="the persistence owner is absent",
    )

    assert cell.validate_reconstruction_report(
        _reconstruction_report("verification", determined),
        expected_window="verification",
        expected_bundle_sha256="c" * 64,
        expected_requirement_ids=("R-001",),
        expected_scope_prefixes=("R-001",),
    ) == ()
    assert cell.validate_reconstruction_report(
        _reconstruction_report("construction", undetermined),
        expected_window="construction",
        expected_bundle_sha256="c" * 64,
        expected_requirement_ids=("R-001",),
        expected_scope_prefixes=("R-001",),
    ) == ()

    determined["missing"] = "should not be present"
    assert "RECONSTRUCTION-DETERMINED-MISSING:claim-input_output" in (
        cell.validate_reconstruction_report(
            _reconstruction_report("verification", determined),
            expected_window="verification",
            expected_bundle_sha256="c" * 64,
            expected_requirement_ids=("R-001",),
            expected_scope_prefixes=("R-001",),
        )
    )
    undetermined["missing"] = ""
    assert "RECONSTRUCTION-UNDETERMINED-MISSING:claim-undetermined" in (
        cell.validate_reconstruction_report(
            _reconstruction_report("construction", undetermined),
            expected_window="construction",
            expected_bundle_sha256="c" * 64,
            expected_requirement_ids=("R-001",),
            expected_scope_prefixes=("R-001",),
        )
    )


def test_comparator_intent_findings_require_ruled_confidence():
    finding = {
        "type": "DRIFT",
        "requirement_id": "R-001",
        "intended_behavior": "return stored widget",
        "reconstructed_behavior": "return any widget",
        "evidence_refs": ["verification:claim-001"],
        "owning_module": "widget",
        "owning_level": "L3",
        "confidence": "high",
    }
    report = {**_empty_comparator(), "intent_findings": [finding]}
    assert cell.validate_comparator_report(
        report,
        expected_bundle_sha256="c" * 64,
        expected_scope_prefixes=("R-001",),
        expected_mnf_tests={},
    ) == ()

    finding["confidence"] = "certain"
    assert "COMPARATOR-INTENT-FINDING-CONFIDENCE:0" in cell.validate_comparator_report(
        report,
        expected_bundle_sha256="c" * 64,
        expected_scope_prefixes=("R-001",),
        expected_mnf_tests={},
    )


def test_undetermined_claims_become_window_owned_required_elevations():
    reports = {
        "reconstruction-verification": _reconstruction_report(
            "verification",
            _reconstruction_claim(
                claim_kind="undetermined",
                behavior="UNDETERMINED by these artifacts",
                missing="the refusal assertion is absent",
            ),
        ),
        "reconstruction-construction": _reconstruction_report(
            "construction",
            _reconstruction_claim(
                claim_kind="undetermined",
                behavior="UNDETERMINED by these artifacts",
                missing="the component owner is absent",
            ),
        ),
    }
    element_index = {
        "elements": [
            {
                "id": "TST-001",
                "kind": "test",
                "serves": ["R-001"],
                "level": "L4",
            },
            {
                "id": "R-001.1",
                "kind": "design",
                "serves": ["R-001"],
                "level": "L3",
            },
        ]
    }

    elevations = cell.required_elevations(
        reconstruction_reports=reports,
        element_index=element_index,
        comparator_report=_empty_comparator(),
        coherence_report=_empty_coherence(),
        atomization_report=_empty_atomization(),
    )

    assert [(row["type"], row["evidence"]["owning_level"]) for row in elevations] == [
        ("UNDETERMINED-GAP", "L3"),
        ("UNDETERMINED-GAP", "L4"),
    ]
    assert {row["evidence"]["source_window"] for row in elevations} == {
        "construction",
        "verification",
    }
    assert all(row["evidence"]["missing"] for row in elevations)


def test_ambiguous_undetermined_ownership_is_still_required_and_blocks_pass():
    reports = {
        "reconstruction-verification": _reconstruction_report(
            "verification",
            _reconstruction_claim(
                claim_kind="undetermined",
                behavior="UNDETERMINED by these artifacts",
                missing="no single verification owner is determined",
            ),
        )
    }
    element_index = {
        "elements": [
            {"id": "TST-001", "kind": "test", "serves": ["R-001"], "level": "L4"},
            {"id": "TST-002", "kind": "test", "serves": ["R-001"], "level": "L3"},
        ]
    }

    elevations = cell.required_elevations(
        reconstruction_reports=reports,
        element_index=element_index,
        comparator_report=_empty_comparator(),
        coherence_report=_empty_coherence(),
        atomization_report=_empty_atomization(),
    )

    assert len(elevations) == 1
    finding = elevations[0]
    assert finding["type"] == "UNDETERMINED-GAP"
    assert finding["evidence"]["owning_level"] == "UNRESOLVED"
    assert finding["evidence"]["routing_defect"].startswith(
        "UNDETERMINED-GAP-OWNING-LEVEL-MULTIPLE:"
    )
    blockers = cell.plan_alignment_pass_blockers(
        {
            "node_address": "proj/widget#exec",
            "plan_alignment_semantic_evidence_sha256": "e" * 64,
            "plan_alignment_semantic_bundle_sha256": "c" * 64,
            "plan_alignment_required_elevations": elevations,
        },
        {"plan_alignment_owner_questions": {}},
    )
    assert blockers == (
        f"PLAN-ALIGNMENT-ELEVATION-MISSING:{finding['fingerprint']}",
    )


def test_minting_is_exact_idempotent_and_refuses_post_calibration_drift(tmp_path):
    instruction = tmp_path / "instruction.md"
    instruction.write_text("instruction_version: 1\n", encoding="utf-8")
    prior = hashlib.sha256(instruction.read_bytes()).hexdigest()
    instruction.write_text("instruction_version: 2\n", encoding="utf-8")
    ratification = tmp_path / "ratification.md"
    ratification.write_text("owner + director ratified\n", encoding="utf-8")

    record_path, receipt_path = instruction_calibration.mint_instruction_calibration(
        instruction_path=instruction,
        prior_fingerprint=prior,
        required_channel="semantic_cell_calibration",
        ratification_record=ratification,
        participants=("owner", "fable-l1-l5-director"),
        reason="test calibration",
    )
    assert record_path.name == "instruction.md.calibration-record.json"
    assert receipt_path.name == "instruction.md.calibration-receipt.json"
    assert instruction.stat().st_mode & 0o222 == 0
    assert record_path.stat().st_mode & 0o222 == 0
    assert receipt_path.stat().st_mode & 0o222 == 0
    assert instruction_calibration.instruction_calibration_defects(
        instruction_paths=(instruction,),
        required_channel="semantic_cell_calibration",
        defect_code="UNCALIBRATED",
    ) == ()
    assert instruction_calibration.mint_instruction_calibration(
        instruction_path=instruction,
        prior_fingerprint=prior,
        required_channel="semantic_cell_calibration",
        ratification_record=ratification,
        participants=("owner", "fable-l1-l5-director"),
        reason="test calibration",
    ) == (record_path, receipt_path)

    instruction.chmod(0o644)
    instruction.write_text("instruction_version: 3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="existing calibration is stale"):
        instruction_calibration.mint_instruction_calibration(
            instruction_path=instruction,
            prior_fingerprint=prior,
            required_channel="semantic_cell_calibration",
            ratification_record=ratification,
            participants=("owner", "fable-l1-l5-director"),
            reason="test calibration",
        )
