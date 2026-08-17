"""Owner-docket Q4 — the physically blind plan-alignment semantic cell."""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.contracts as contracts
import harnessd.daemon as daemon
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.harnessctl as harnessctl
import harnessd.ipc as ipc
import harnessd.ledger as ledger
import harnessd.notary as notary
import harnessd.plan_alignment as plan_alignment
import harnessd.plan_alignment_cell as cell
import harnessd.return_contract as return_contract
from harnessd.spawn import blinders, chokepoint
from harnessd.spawn.adapters.base import SpawnResult


L2 = "proj/widget#exec"
L1 = "proj#exec"
SESSION = "sess-semantic-cell-0001"
SUBAGENT = "subagent-semantic-cell"


@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((neutral_brief, level_config, tmux_target, env))
        return SpawnResult(
            ok=True,
            session_uuid=f"sess-{len(self.calls):04d}-semantic",
            model_used=f"{level_config.model} / {level_config.runtime}",
            role_variant=level_config.role_variant,
            system_prompt_file=level_config.system_prompt_file,
            system_prompt_file_hash="f" * 64,
            tmux_target=tmux_target,
            transcript_path=f"/tmp/{len(self.calls):04d}-semantic.jsonl",
            failure_class=None,
        )


class _NoLiveTmux:
    def list_targets(self):
        return {}


@pytest.fixture
def adapter():
    previous = chokepoint.ADAPTER
    fake = FakeAdapter()
    chokepoint.set_adapter(fake)
    try:
        yield fake
    finally:
        chokepoint.ADAPTER = previous


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _binding(address: str, *, parent: str | None, level: str, extra=None):
    token = fencing.mint_owner_token(address, SUBAGENT, SESSION, 1)
    row = {
        "node_address": address,
        "parent_address": parent,
        "level": level,
        "role_variant": level,
        "subagent_id": SUBAGENT,
        "session_uuid": SESSION,
        "state": "running",
        "generation": 0,
        "lease_epoch": 1,
        "owner_token": token,
        "last_applied_seq": 0,
        "last_inbox_acked_offset": 0,
        "liveness_state": "idle",
        "last_progress_at": _now_iso(),
        "terminal_signal": None,
        "paused_at": None,
        "workspace": "",
        "spec_pointer": "design/intent-spec.md",
        "frozen_acceptance_ref": "acceptance.md",
    }
    row.update(extra or {})
    return row, token


def _seed(*bindings: dict) -> None:
    ledger.write_binding(
        {row["node_address"]: copy.deepcopy(row) for row in bindings},
        _lock_held=True,
    )


def _trace(element_id: str, *, serves: tuple[str, ...], kind: str) -> str:
    return (
        f"<!-- trace: {{ id: {element_id}, serves: [{', '.join(serves)}], "
        f"kind: {kind}, level: L3, node: proj/widget }} -->"
    )


def _intent_text() -> str:
    return """# Intent Spec

## Requirements

| ID | Requirement | Tag | Priority | MNF | Parent | Fluency | Reflect-back status |
|---|---|---|---|---|---|---|---|
| R-001 | Return the stored widget | decided | must | — | O-1 | plain | confirmed |
| R-002 | Refuse an unknown widget | decided | must | YES | O-1 | plain | confirmed |

## ID → intent-span map

| ID | Intent span |
|---|---|
| R-001 | Return the stored widget. |
| R-002 | Refuse an unknown widget. |

## Reflect-back script

Status: confirmed

The system returns a stored widget and refuses an unknown widget.
"""


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _prepare(tmp_path: Path, *, mixed: bool = False) -> dict:
    node_dir = tmp_path / "nodes" / "proj" / "widget"
    plan_dir = node_dir / "plan"
    plan_dir.mkdir(parents=True)
    brief_dir = node_dir / "client-brief"
    brief_dir.mkdir()

    raw_request = brief_dir / "raw-request.md"
    raw_request.write_text(
        "Build a widget reader. It must refuse unknown widgets.\n",
        encoding="utf-8",
    )
    intent = brief_dir / "intent-spec.md"
    intent.write_text(_intent_text(), encoding="utf-8")
    intent_receipt = contracts.contract_receipt(
        L2, "proj#exec", intent, notary.stamp(intent)
    )
    raw_receipt = contracts.contract_receipt(
        L2, "proj#exec", raw_request, notary.stamp(raw_request)
    )

    construction = plan_dir / "construction" / "widget-design.md"
    construction.parent.mkdir()
    construction.write_text(
        "# Widget design\n\n"
        + _trace("R-001.1", serves=("R-001",), kind="design")
        + "\n\n"
        + _trace("R-002.1", serves=("R-002",), kind="design")
        + ("\n\n" + _trace("TST-MIXED", serves=("R-001",), kind="test") if mixed else "")
        + "\n",
        encoding="utf-8",
    )
    verification = plan_dir / "verification" / "widget-criteria.md"
    verification.parent.mkdir()
    verification.write_text(
        "# Widget criteria\n\n"
        + _trace("TST-001", serves=("R-001",), kind="test")
        + "\n\n"
        + _trace("TST-002-FAIL", serves=("R-002",), kind="test")
        + "\n",
        encoding="utf-8",
    )
    package = plan_dir / "validated-plan-package.md"
    package.write_text("# Validated plan package\n", encoding="utf-8")
    coverage_manifest = _write_json(
        plan_dir / "plan-alignment-coverage.json",
        {
            "schema_version": 1,
            "artifacts": [
                {
                    "path": "plan/construction/widget-design.md",
                    "trace_ids": ["R-001.1", "R-002.1"]
                    + (["TST-MIXED"] if mixed else []),
                },
                {
                    "path": "plan/verification/widget-criteria.md",
                    "trace_ids": ["TST-001", "TST-002-FAIL"],
                },
            ],
            "failure_path_criteria": [
                {"requirement_id": "R-002", "test_id": "TST-002-FAIL"}
            ],
        },
    )
    semantic_manifest = _write_json(
        plan_dir / "plan-alignment-semantic.json",
        {
            "schema_version": 1,
            "verification_artifacts": [
                {"path": "plan/verification/widget-criteria.md", "module": "widget"}
            ],
            "construction_modules": [
                {
                    "module": "widget",
                    "artifacts": ["plan/construction/widget-design.md"],
                }
            ],
        },
    )
    marker = _write_json(
        node_dir / "plan-alignment-ready.json",
        {
            "type": "plan_alignment_ready",
            "package": "plan/validated-plan-package.md",
            "coverage_manifest": "plan/plan-alignment-coverage.json",
            "semantic_manifest": "plan/plan-alignment-semantic.json",
            "message": "ready",
        },
    )
    binding = {
        "intent_spec_receipt": intent_receipt,
        "raw_request_receipt": raw_receipt,
    }
    coverage = plan_alignment.evaluate_submission(
        node_address=L2,
        node_dir=node_dir,
        marker=marker,
        marker_payload=json.loads(marker.read_text(encoding="utf-8")),
        package=package,
        binding=binding,
    )
    return {
        "node_dir": node_dir,
        "intent": intent,
        "raw_request": raw_request,
        "binding": binding,
        "marker": marker,
        "marker_payload": json.loads(marker.read_text(encoding="utf-8")),
        "coverage": coverage,
        "coverage_manifest": coverage_manifest,
        "semantic_manifest": semantic_manifest,
        "construction": construction,
        "verification": verification,
    }


def _valid_claim(requirement_id: str, claim_id: str) -> dict:
    return {
        "requirement_id": requirement_id,
        "claim_id": claim_id,
        "behavior": f"{requirement_id} behavior",
        "claim_kind": "input_output",
        "missing": "",
        "stimulus": f"{requirement_id} input",
        "observable": f"{requirement_id} output",
    }


def test_semantic_manifest_partitions_every_trace_bearing_q3_artifact(tmp_path):
    prepared = _prepare(tmp_path)
    assert prepared["coverage"].ok is True

    result = cell.prepare_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker_payload=prepared["marker_payload"],
        coverage=prepared["coverage"],
        binding=prepared["binding"],
        allow_uncalibrated=True,
    )

    assert result.ok is True
    assert result.defects == ()
    assert result.verification_artifacts == (prepared["verification"].resolve(),)
    assert result.construction_modules == {
        "widget": (prepared["construction"].resolve(),)
    }
    assert result.scope_requirement_ids == ("R-001", "R-002")
    assert result.mnf_tests == {"R-002": ("TST-002-FAIL",)}
    assert result.semantic_manifest_sha256 == notary.stamp(
        prepared["semantic_manifest"]
    )["sha256"]
    assert result.cell_sha256 != prepared["coverage"].bundle_sha256
    assert result.atomization_projection.is_file()
    projection = json.loads(
        result.atomization_projection.read_text(encoding="utf-8")
    )
    assert projection["raw_request"]["text"].startswith("Build a widget reader")
    assert "R-001 | Return the stored widget." in projection["id_to_span"]["text"]
    assert projection["reflect_back"]["status"] == "confirmed"
    assert result.element_index.is_file()


def test_mixed_construction_and_verification_file_is_gate_hard(tmp_path):
    prepared = _prepare(tmp_path, mixed=True)
    assert prepared["coverage"].ok is True

    result = cell.prepare_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker_payload=prepared["marker_payload"],
        coverage=prepared["coverage"],
        binding=prepared["binding"],
        allow_uncalibrated=True,
    )

    assert result.ok is False
    assert "SEMANTIC-CONSTRUCTION-CONTAINS-TEST:TST-MIXED" in result.defects


def test_trace_bearing_package_entrypoint_cannot_escape_both_windows(tmp_path):
    prepared = _prepare(tmp_path)
    package = prepared["node_dir"] / "plan" / "validated-plan-package.md"
    package.write_text(
        "# Validated plan package\n\n"
        + _trace("R-001.9", serves=("R-001",), kind="design")
        + "\n",
        encoding="utf-8",
    )
    coverage = plan_alignment.evaluate_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker=prepared["marker"],
        marker_payload=prepared["marker_payload"],
        package=package,
        binding=prepared["binding"],
    )
    assert coverage.ok is True

    result = cell.prepare_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker_payload=prepared["marker_payload"],
        coverage=coverage,
        binding=prepared["binding"],
        allow_uncalibrated=True,
    )

    assert result.ok is False
    assert any(
        "trace-bearing Q3 artifacts lack a window" in defect
        for defect in result.defects
    )


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda payload: payload["claims"].pop(),
            "RECONSTRUCTION-MISSING-REQUIREMENT:R-002",
        ),
        (
            lambda payload: payload["claims"][0].update(stimulus=""),
            "RECONSTRUCTION-EMPTY-STIMULUS:claim-001",
        ),
        (
            lambda payload: payload["claims"][0].update(claim_kind="topic"),
            "RECONSTRUCTION-INVALID-CLAIM-KIND:claim-001",
        ),
    ],
)
def test_reconstruction_specificity_is_a_closed_shape_not_a_seat(mutate, expected):
    payload = {
        "schema_version": 1,
        "bundle_sha256": "b" * 64,
        "window": "verification",
        "scope_prefixes": ["R-001", "R-002"],
        "claims": [
            _valid_claim("R-001", "claim-001"),
            _valid_claim("R-002", "claim-002"),
        ],
        "assumptions": [],
    }
    mutate(payload)

    defects = cell.validate_reconstruction_report(
        payload,
        expected_window="verification",
        expected_bundle_sha256="b" * 64,
        expected_requirement_ids=("R-001", "R-002"),
        expected_scope_prefixes=("R-001", "R-002"),
    )

    assert expected in defects


def test_semantic_exact_policy_forces_enforce_and_has_no_neighborhood(tmp_path):
    own = tmp_path / "nodes" / "proj" / "cell" / "seat"
    allowed = tmp_path / "nodes" / "proj" / "widget" / "plan" / "criteria.md"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("criteria\n", encoding="utf-8")

    policy = blinders.derive_exact_policy(
        node_address="proj/widget/plan-alignment/bundle/seats/verification#exec",
        runtime_root=str(tmp_path),
        workspace=str(own),
        exact_documents=[str(allowed)],
        runtime="claude-code",
        harness_root=str(Path(__file__).resolve().parents[1]),
    )

    assert policy["mode"] == blinders.ENFORCE
    assert policy["exact_declared"] is True
    assert policy["direct_surfaces"] == []
    assert str(allowed) in policy["allow_literals"]
    assert str(tmp_path / "nodes" / "proj") not in policy["allow_subpaths"]
    assert str(own) in policy["allow_subpaths"]


def test_calibrated_instructions_and_models_pass_without_test_seam(tmp_path):
    prepared = _prepare(tmp_path)

    result = cell.prepare_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker_payload=prepared["marker_payload"],
        coverage=prepared["coverage"],
        binding=prepared["binding"],
        allow_uncalibrated=False,
    )

    assert result.ok is True
    assert result.defects == ()
    assert cell.instruction_calibration_defects() == ()
    configured = chokepoint._semantic_level_config("coherence")
    assert configured.role_variant == "plan-alignment#coherence"
    assert (configured.model, configured.runtime) == ("opus-5.0", "claude-code")


def test_semantic_brief_names_the_seat_role_instruction(tmp_path):
    """The cell brief must NAME the seat's role instruction, or the seat has no role.

    A cell seat's identity is DESIGNED as two halves (see `brief._assemble_load_manifest`): the
    ordinary L2+ identity auto-loaded into `.identity-prompt.md`, PLUS the bounded semantic
    instruction delivered read-in-place through the load-manifest. The hand-rolled cell brief
    rendered neither the manifest section every other child's brief carries nor any other pointer to
    the instruction — while telling the seat to "read the role instruction in your load manifest"
    and while the auto-loaded prompt's own trailer pointed at "your brief's load-manifest". Both
    pointers dangled, so the seat kept only the first half and worked as an L2+ product-composition
    reviewer. Absolute paths per LR-3: the pane boots in the node workspace.
    """
    report = tmp_path / "atomization.json"
    text = chokepoint._semantic_brief_text(
        role="atomization",
        control={"cell_sha256": "a" * 64, "scope_prefixes": ["R-001"]},
        input_manifest=tmp_path / "input-manifest.json",
        report_path=report,
    )

    assert "## Identity — Load These Documents (read in place)" in text
    instruction = str(
        Path(chokepoint.__file__).resolve().parents[2]
        / "operational/plan-alignment/atomization.md"
    )
    assert f"- {instruction}" in text, f"the atomization instruction must be named: {text!r}"
    # The generic L2+ trio still rides the same manifest (the identity half is unchanged).
    assert "operational/L2+/role.md" in text
    # A different role gets a different instruction — the manifest is genuinely role-selected.
    other = chokepoint._semantic_brief_text(
        role="coherence",
        control={"cell_sha256": "a" * 64, "scope_prefixes": ["R-001"]},
        input_manifest=None,
        report_path=report,
    )
    assert "operational/plan-alignment/coherence.md" in other
    assert "operational/plan-alignment/atomization.md" not in other


def test_semantic_level_config_takes_the_production_blinders_resolution(monkeypatch):
    """The cell's LevelConfig carries the PRODUCTION read-policy resolution, not a hardcode.

    Blinders mode is an owner decision resolved once, at the launch-path assemblers
    (``commissioning.build_runtime`` / ``config.get_level_config``); default observe, enforce only
    on an explicit env opt-in. A hardcoded ``enforce`` here silently overrode that ruling for every
    cell seat. NOTE (and this is why the fix is not the run-unblocker): the cell's ACTUAL read
    window comes from ``blinders.derive_exact_policy``, which is unconditionally enforce because
    blindness is the instrument — see ``_produce_containment``. This field governs only the
    general-policy fallback branch, so it must tell the truth about the deployment posture.
    """
    monkeypatch.delenv(config.BLINDERS_MODE_ENV, raising=False)
    assert chokepoint._semantic_level_config("coherence").blinders_mode == blinders.OBSERVE

    monkeypatch.setenv(config.BLINDERS_MODE_ENV, "enforce")
    assert chokepoint._semantic_level_config("coherence").blinders_mode == blinders.ENFORCE


def test_comparator_requires_per_mnf_failure_mechanism_and_typed_findings():
    report = {
        "schema_version": 1,
        "bundle_sha256": "c" * 64,
        "scope_prefixes": ["R-001", "R-002"],
        "window_splits": [],
        "intent_findings": [
            {
                "type": "DRIFT",
                "requirement_id": "R-001",
                "intended_behavior": "return stored widget",
                "reconstructed_behavior": "return any widget",
                "evidence_refs": ["verification:claim-001"],
                "owning_module": "widget",
                "owning_level": "L3",
                "confidence": "high",
            }
        ],
        "mnf_adequacy": [
            {
                "requirement_id": "R-002",
                "test_id": "TST-002-FAIL",
                "failure_exercised": "",
                "assertion_catches": "",
                "adequate": True,
                "defect_reason": "",
            }
        ],
    }

    defects = cell.validate_comparator_report(
        report,
        expected_bundle_sha256="c" * 64,
        expected_scope_prefixes=("R-001", "R-002"),
        expected_mnf_tests={"R-002": ("TST-002-FAIL",)},
    )

    assert "COMPARATOR-VACUOUS-MNF:R-002:TST-002-FAIL" in defects
    elevations = cell.required_elevations(
        comparator_report=report,
        coherence_report={
            "schema_version": 1,
            "bundle_sha256": "c" * 64,
            "modules_read": ["widget"],
            "shared_assumptions": [],
            "contradictions": [],
        },
        atomization_report={
            "schema_version": 1,
            "intent_fingerprint": "i" * 64,
            "findings": [],
        },
    )
    assert [row["type"] for row in elevations] == ["DRIFT", "MNF-ADEQUACY"]
    assert all(row["fingerprint"] for row in elevations)


def test_coherence_contradictions_are_keyed_by_module_pair_not_shared_id():
    report = {
        "schema_version": 1,
        "bundle_sha256": "d" * 64,
        "modules_read": ["api", "worker"],
        "shared_assumptions": [
            {
                "assumption_key": "retry-owner",
                "modules": ["api", "worker"],
                "interpretations": {
                    "api": "API retries",
                    "worker": "worker retries",
                },
                "evidence": {
                    "api": "plan/construction/api.md",
                    "worker": "plan/construction/worker.md",
                },
            }
        ],
        "contradictions": [
            {
                "type": "CONTRADICTION",
                "modules": ["api", "worker"],
                "assumption_key": "retry-owner",
                "incompatible_claims": ["API retries", "worker retries"],
                "evidence_paths": [
                    "plan/construction/api.md",
                    "plan/construction/worker.md",
                ],
                "affected_trace_prefixes": [],
            }
        ],
    }

    assert cell.validate_coherence_report(
        report,
        expected_bundle_sha256="d" * 64,
        expected_modules=("api", "worker"),
    ) == ()
    finding = cell.required_elevations(
        comparator_report={
            "schema_version": 1,
            "bundle_sha256": "d" * 64,
            "scope_prefixes": [],
            "window_splits": [],
            "intent_findings": [],
            "mnf_adequacy": [],
        },
        coherence_report=report,
        atomization_report={
            "schema_version": 1,
            "intent_fingerprint": "i" * 64,
            "findings": [],
        },
    )[0]
    assert finding["type"] == "CONTRADICTION"
    assert finding["key"] == "api::worker::retry-owner"


def test_incremental_neighbors_come_from_trace_graph_not_seat_prose():
    prior = {
        "elements": [
            {
                "id": "R-001.1",
                "kind": "design",
                "serves": ["R-001"],
                "module": "api",
                "artifact_sha256": "old",
            },
            {
                "id": "TST-001",
                "kind": "test",
                "serves": ["R-001"],
                "module": "worker",
                "artifact_sha256": "same",
            },
        ]
    }
    current = {
        "elements": [
            {
                "id": "R-001.1",
                "kind": "design",
                "serves": ["R-001"],
                "module": "api",
                "artifact_sha256": "new",
            },
            {
                "id": "TST-001",
                "kind": "test",
                "serves": ["R-001"],
                "module": "worker",
                "artifact_sha256": "same",
            },
        ]
    }

    scope = cell.incremental_scope(
        prior,
        current,
        prior_neighbors={},
        seat_assumption_claims={"api": ["a prose claim naming unrelated-module"]},
    )

    assert scope["changed_ids"] == ["R-001.1"]
    assert scope["scope_prefixes"] == ["R-001"]
    assert scope["modules"] == ["api", "worker"]
    assert "unrelated-module" not in scope["modules"]


def test_repaired_submission_scopes_incrementally_reuses_atomization_and_intent_resets(
    runtime,
):
    prepared = _prepare(runtime)
    first = cell.prepare_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker_payload=prepared["marker_payload"],
        coverage=prepared["coverage"],
        binding=prepared["binding"],
        allow_uncalibrated=True,
    )
    assert first.ok is True
    atomization_report = _write_json(
        first.cell_dir / "accepted-atomization.json",
        {
            "schema_version": 1,
            "intent_fingerprint": first.intent_fingerprint,
            "findings": [],
        },
    )
    atomization_stamp = notary.stamp(atomization_report, read_only=True)
    prior_binding = {
        **prepared["binding"],
        "plan_alignment_intent_fingerprint": first.intent_fingerprint,
        "plan_alignment_element_index": str(first.element_index),
        "plan_alignment_element_index_sha256": notary.stamp(first.element_index)[
            "sha256"
        ],
        "plan_alignment_atomization_cache": {
            "intent_fingerprint": first.intent_fingerprint,
            "report": str(atomization_report),
            "report_sha256": atomization_stamp["sha256"],
        },
    }

    prepared["construction"].chmod(0o644)
    prepared["construction"].write_text(
        prepared["construction"].read_text(encoding="utf-8")
        + "\nThe repair makes the stored-widget lookup explicit.\n",
        encoding="utf-8",
    )
    repaired_coverage = plan_alignment.evaluate_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker=prepared["marker"],
        marker_payload=prepared["marker_payload"],
        package=prepared["node_dir"] / "plan" / "validated-plan-package.md",
        binding=prior_binding,
    )
    repaired = cell.prepare_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker_payload=prepared["marker_payload"],
        coverage=repaired_coverage,
        binding=prior_binding,
        allow_uncalibrated=True,
    )

    assert repaired.ok is True
    repaired_control = cell.load_control(repaired.control_record)
    assert repaired_control["gating_mode"] == "incremental"
    assert repaired_control["changed_element_ids"] == ["R-001.1", "R-002.1"]
    assert repaired_control["atomization_cache"]["report"] == str(
        atomization_report
    )
    l1, _ = _binding(L1, parent=None, level="L1")
    l2, _ = _binding(
        L2,
        parent=L1,
        level="L2",
        extra=prior_binding,
    )
    _seed(l1, l2)
    seats = chokepoint._register_semantic_cell(L2, l2, repaired_control)
    assert seats["atomization"]["state"] == "done"
    assert seats["atomization"]["semantic_cached"] is True
    assert seats["atomization"]["semantic_report"] == str(atomization_report)

    prepared["intent"].chmod(0o644)
    prepared["intent"].write_text(
        _intent_text().replace(
            "Return the stored widget",
            "Return the stored widget with its current version",
        ),
        encoding="utf-8",
    )
    revised_binding = {
        **prior_binding,
        "intent_spec_receipt": contracts.contract_receipt(
            L2,
            L1,
            prepared["intent"],
            notary.stamp(prepared["intent"]),
        ),
        "plan_alignment_element_index": str(repaired.element_index),
        "plan_alignment_element_index_sha256": notary.stamp(
            repaired.element_index
        )["sha256"],
    }
    revised_coverage = plan_alignment.evaluate_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker=prepared["marker"],
        marker_payload=prepared["marker_payload"],
        package=prepared["node_dir"] / "plan" / "validated-plan-package.md",
        binding=revised_binding,
    )
    revised = cell.prepare_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker_payload=prepared["marker_payload"],
        coverage=revised_coverage,
        binding=revised_binding,
        allow_uncalibrated=True,
    )

    assert revised.ok is True
    revised_control = cell.load_control(revised.control_record)
    assert revised_control["gating_mode"] == "full_intent_revision"
    assert revised_control["atomization_cache"] is None


def _semantic_report(role: str, *, bundle: str, intent_fingerprint: str) -> dict:
    if role == "reconstruction-verification":
        return {
            "schema_version": 1,
            "bundle_sha256": bundle,
            "window": "verification",
            "scope_prefixes": ["R-001", "R-002"],
            "claims": [
                _valid_claim("R-001", "verify-001"),
                _valid_claim("R-002", "verify-002"),
            ],
            "assumptions": [],
        }
    if role == "reconstruction-construction":
        return {
            "schema_version": 1,
            "bundle_sha256": bundle,
            "window": "construction",
            "scope_prefixes": ["R-001", "R-002"],
            "claims": [
                _valid_claim("R-001", "construct-001"),
                _valid_claim("R-002", "construct-002"),
            ],
            "assumptions": [],
        }
    if role == "coherence":
        return {
            "schema_version": 1,
            "bundle_sha256": bundle,
            "modules_read": ["widget"],
            "shared_assumptions": [],
            "contradictions": [],
        }
    if role == "atomization":
        return {
            "schema_version": 1,
            "intent_fingerprint": intent_fingerprint,
            "findings": [],
        }
    if role == "comparator":
        return {
            "schema_version": 1,
            "bundle_sha256": bundle,
            "scope_prefixes": ["R-001", "R-002"],
            "window_splits": [],
            "intent_findings": [],
            "mnf_adequacy": [
                {
                    "requirement_id": "R-002",
                    "test_id": "TST-002-FAIL",
                    "failure_exercised": "request a widget key absent from storage",
                    "assertion_catches": "asserts the typed unknown-widget refusal",
                    "adequate": True,
                    "defect_reason": "",
                }
            ],
        }
    raise AssertionError(role)


def _complete_semantic_seat(address: str, *, bundle: str, intent_fingerprint: str):
    binding = ledger.read_binding(address)
    report = Path(binding["semantic_report"])
    _write_json(
        report,
        _semantic_report(
            binding["semantic_cell_role"],
            bundle=bundle,
            intent_fingerprint=intent_fingerprint,
        ),
    )
    walk = return_contract.walk_done_contract(address, binding)
    assert walk.defects == ()
    result = chokepoint.collapse(
        address,
        "DONE",
        expected_owner_token=binding["owner_token"],
    )
    assert result.ok is True


def test_q3_pass_delays_l1_wake_and_daemon_runs_one_preregistered_cell(
    runtime, adapter
):
    prepared = _prepare(runtime)
    l1, _ = _binding(L1, parent=None, level="L1")
    l2, token = _binding(
        L2,
        parent=L1,
        level="L2",
        extra=prepared["binding"],
    )
    _seed(l1, l2)

    admitted = chokepoint.submit_plan_alignment_ready(
        L2,
        marker_path=prepared["marker"],
        expected_owner_token=token,
    )

    assert admitted.ok is True
    pending = ledger.read_binding(L2)
    assert pending["plan_alignment_state"] == "semantic_cell_pending"
    assert pending["plan_alignment_semantic_control"]
    assert not addressing.inbox_path(L1, runtime).exists()

    daemon._reconcile_plan_alignment_cells_best_effort()
    seats = {
        row["semantic_cell_role"]: row
        for row in ledger.all_nodes().values()
        if row.get("semantic_cell_for") == L2
    }
    assert set(seats) == set(cell.ALL_ROLES)
    assert seats["comparator"]["state"] == "planned"
    assert seats["comparator"]["semantic_dependencies"] == sorted(
        [
            seats["reconstruction-construction"]["node_address"],
            seats["reconstruction-verification"]["node_address"],
        ]
    )
    assert {
        row["semantic_cell_role"]
        for row in seats.values()
        if row["state"] == "running"
    } == set(cell.FIRST_WAVE_ROLES)
    assert len(adapter.calls) == 4
    for neutral_brief, level_config, _target, _env in adapter.calls:
        # The seat config reports the DEPLOYMENT posture (observe by default); the seat's actual
        # read window is the exact blind policy below, which stays enforce regardless. This pair is
        # the proof that dropping the level-config enforce hardcode did not open the blind window.
        assert level_config.blinders_mode == config.production_blinders_mode()
        posture = neutral_brief["containment_profile"]["read_policy"]
        assert posture["mode"] == blinders.ENFORCE
        assert posture["exact_declared"] is True
        assert posture["direct_surfaces"] == []

    for role in cell.FIRST_WAVE_ROLES:
        _complete_semantic_seat(
            seats[role]["node_address"],
            bundle=pending["plan_alignment_semantic_bundle_sha256"],
            intent_fingerprint=pending["plan_alignment_intent_fingerprint"],
        )
    assert not addressing.inbox_path(L1, runtime).exists(), (
        "individual semantic completions stay silent"
    )

    daemon._reconcile_plan_alignment_cells_best_effort()
    comparator = ledger.read_binding(seats["comparator"]["node_address"])
    assert comparator["state"] == "running"
    assert len(adapter.calls) == 5
    for role in (
        "reconstruction-verification",
        "reconstruction-construction",
    ):
        assert Path(seats[role]["semantic_report"]).stat().st_mode & 0o777 == 0o444
    _complete_semantic_seat(
        comparator["node_address"],
        bundle=pending["plan_alignment_semantic_bundle_sha256"],
        intent_fingerprint=pending["plan_alignment_intent_fingerprint"],
    )

    daemon._reconcile_plan_alignment_cells_best_effort()
    ready = ledger.read_binding(L2)
    assert ready["plan_alignment_state"] == "ready"
    assert ready["plan_alignment_semantic_evidence_sha256"]
    evidence = json.loads(
        Path(ready["plan_alignment_semantic_evidence"]).read_text(encoding="utf-8")
    )
    assert evidence["required_elevations"] == []
    parent_rows = [
        json.loads(raw)
        for raw in addressing.inbox_path(L1, runtime)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["type"] for row in parent_rows] == ["design-submission"]
    assert parent_rows[0]["semantic_evidence_sha256"] == (
        ready["plan_alignment_semantic_evidence_sha256"]
    )
    assert parent_rows[0]["required_elevation_delta"] == {
        "new": [],
        "changed": [],
        "cleared": [],
    }
    assert ready["plan_alignment_atomization_cache"]["intent_fingerprint"] == (
        pending["plan_alignment_intent_fingerprint"]
    )


def test_generic_redrive_does_not_open_dependency_held_semantic_comparator(
    runtime, adapter
):
    prepared = _prepare(runtime)
    l1, _ = _binding(L1, parent=None, level="L1")
    l2, token = _binding(
        L2,
        parent=L1,
        level="L2",
        extra=prepared["binding"],
    )
    _seed(l1, l2)
    admitted = chokepoint.submit_plan_alignment_ready(
        L2,
        marker_path=prepared["marker"],
        expected_owner_token=token,
    )
    assert admitted.ok is True

    daemon._reconcile_plan_alignment_cells_best_effort()
    comparator = next(
        row
        for row in ledger.all_nodes().values()
        if row.get("semantic_cell_for") == L2
        and row.get("semantic_cell_role") == cell.COMPARATOR_ROLE
    )
    assert comparator["state"] == "planned"
    assert comparator["semantic_input_manifest"] is None
    assert len(adapter.calls) == len(cell.FIRST_WAVE_ROLES)

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())

    held = ledger.read_binding(comparator["node_address"])
    assert held["state"] == "planned"
    assert held["semantic_input_manifest"] is None
    assert len(adapter.calls) == len(cell.FIRST_WAVE_ROLES)
    assert not any(
        row.get("event") == "semantic_comparator_released"
        for row in ledger.load_wal()
    )


def test_pending_cell_replaces_failed_seats_and_keeps_comparator_held_until_release(
    runtime, adapter
):
    prepared = _prepare(runtime)
    l1, _ = _binding(L1, parent=None, level="L1")
    l2, token = _binding(
        L2,
        parent=L1,
        level="L2",
        extra=prepared["binding"],
    )
    _seed(l1, l2)
    admitted = chokepoint.submit_plan_alignment_ready(
        L2,
        marker_path=prepared["marker"],
        expected_owner_token=token,
    )
    assert admitted.ok is True
    daemon._reconcile_plan_alignment_cells_best_effort()
    pending = ledger.read_binding(L2)
    seats = {
        row["semantic_cell_role"]: row
        for row in ledger.all_nodes().values()
        if row.get("semantic_cell_for") == L2
    }

    _complete_semantic_seat(
        seats["atomization"]["node_address"],
        bundle=pending["plan_alignment_semantic_bundle_sha256"],
        intent_fingerprint=pending["plan_alignment_intent_fingerprint"],
    )
    done_atomization = copy.deepcopy(
        ledger.read_binding(seats["atomization"]["node_address"])
    )

    failed_reconstructions = {}
    for role in (
        "reconstruction-verification",
        "reconstruction-construction",
    ):
        before = ledger.read_binding(seats[role]["node_address"])
        failed = chokepoint.collapse(
            before["node_address"],
            "FAILED",
            expected_owner_token=before["owner_token"],
        )
        assert failed.ok is True
        failed_reconstructions[role] = copy.deepcopy(failed.binding)

    foreign_address = "proj/widget/plan-alignment/foreign-bundle#exec"
    foreign = copy.deepcopy(failed_reconstructions["reconstruction-verification"])
    foreign.update(
        {
            "node_address": foreign_address,
            "tmux_target": addressing.session_name_for(foreign_address),
            "semantic_cell_bundle_sha256": "0" * 64,
        }
    )
    live_map = dict(ledger.all_nodes())
    live_map[foreign_address] = foreign
    ledger.write_binding(live_map, _lock_held=True)

    daemon._reconcile_plan_alignment_cells_best_effort()

    assert ledger.read_binding(seats["atomization"]["node_address"]) == done_atomization
    assert ledger.read_binding(foreign_address) == foreign
    for role, failed in failed_reconstructions.items():
        reopened = ledger.read_binding(failed["node_address"])
        assert reopened["state"] == "running"
        assert reopened["lease_epoch"] > failed["lease_epoch"]
        assert reopened["owner_token"] != failed["owner_token"]
        assert reopened["model_used"] == failed["model_used"]

    comparator_address = seats[cell.COMPARATOR_ROLE]["node_address"]
    comparator = ledger.read_binding(comparator_address)
    assert comparator["state"] == "planned"
    assert comparator["semantic_input_manifest"] is None

    _complete_semantic_seat(
        failed_reconstructions["reconstruction-verification"]["node_address"],
        bundle=pending["plan_alignment_semantic_bundle_sha256"],
        intent_fingerprint=pending["plan_alignment_intent_fingerprint"],
    )
    daemon._reconcile_plan_alignment_cells_best_effort()
    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    still_held = ledger.read_binding(comparator_address)
    assert still_held["state"] == "planned"
    assert still_held["semantic_input_manifest"] is None

    for role in ("reconstruction-construction", "coherence"):
        _complete_semantic_seat(
            seats[role]["node_address"],
            bundle=pending["plan_alignment_semantic_bundle_sha256"],
            intent_fingerprint=pending["plan_alignment_intent_fingerprint"],
        )
    daemon._reconcile_plan_alignment_cells_best_effort()

    released = ledger.read_binding(comparator_address)
    assert released["state"] == "running"
    assert released["semantic_input_manifest"]
    assert sum(
        row.get("event") == "semantic_comparator_released"
        and row.get("node_address") == comparator_address
        for row in ledger.load_wal()
    ) == 1


def test_pending_cell_does_not_replace_a_three_death_parked_seat(runtime, adapter):
    """The T-15 replacement seam honors the same address-local three-death park."""
    prepared = _prepare(runtime)
    l1, _ = _binding(L1, parent=None, level="L1")
    l2, token = _binding(
        L2,
        parent=L1,
        level="L2",
        extra=prepared["binding"],
    )
    _seed(l1, l2)
    admitted = chokepoint.submit_plan_alignment_ready(
        L2,
        marker_path=prepared["marker"],
        expected_owner_token=token,
    )
    assert admitted.ok is True
    daemon._reconcile_plan_alignment_cells_best_effort()
    target = next(
        row
        for row in ledger.all_nodes().values()
        if row.get("semantic_cell_for") == L2
        and row.get("semantic_cell_role") == "reconstruction-verification"
    )
    failed = chokepoint.collapse(
        target["node_address"],
        "FAILED",
        expected_owner_token=target["owner_token"],
    )
    assert failed.ok is True
    parked = copy.deepcopy(failed.binding)
    parked.update(
        {
            "consecutive_failed_incarnations": 3,
            "failed_incarnation_causes": [
                {"event": "died_infrastructure", "reason": "one"},
                {"event": "watchdog_nonresponse", "reason": "two"},
                {"event": "watchdog_runtime_failure", "reason": "three"},
            ],
            "respawn_parked_at": "2026-07-28T10:00:00+00:00",
        }
    )
    live_map = dict(ledger.all_nodes())
    live_map[target["node_address"]] = parked
    ledger.write_binding(live_map, _lock_held=True)
    calls_before = len(adapter.calls)

    daemon._reconcile_plan_alignment_cells_best_effort()

    assert ledger.read_binding(target["node_address"]) == parked
    assert len(adapter.calls) == calls_before


def test_nonpending_semantic_cell_does_not_replace_failed_seat(runtime, adapter):
    prepared = _prepare(runtime)
    l1, _ = _binding(L1, parent=None, level="L1")
    l2, token = _binding(
        L2,
        parent=L1,
        level="L2",
        extra=prepared["binding"],
    )
    _seed(l1, l2)
    admitted = chokepoint.submit_plan_alignment_ready(
        L2,
        marker_path=prepared["marker"],
        expected_owner_token=token,
    )
    assert admitted.ok is True
    daemon._reconcile_plan_alignment_cells_best_effort()
    failed_address = next(
        row["node_address"]
        for row in ledger.all_nodes().values()
        if row.get("semantic_cell_for") == L2
        and row.get("semantic_cell_role") == "reconstruction-verification"
    )
    running = ledger.read_binding(failed_address)
    failed = chokepoint.collapse(
        failed_address,
        "FAILED",
        expected_owner_token=running["owner_token"],
    )
    assert failed.ok is True

    live_map = dict(ledger.all_nodes())
    live_map[L2] = {
        **live_map[L2],
        "plan_alignment_state": chokepoint.PLAN_ALIGNMENT_STATE_READY,
    }
    ledger.write_binding(live_map, _lock_held=True)
    before = copy.deepcopy(ledger.read_binding(failed_address))

    result = chokepoint.reconcile_plan_alignment_cell(L2)

    assert result.ok is True
    assert ledger.read_binding(failed_address) == before


def test_semantic_return_contract_reuses_done_walker_for_json_shape(runtime):
    prepared = _prepare(runtime)
    result = cell.prepare_submission(
        node_address=L2,
        node_dir=prepared["node_dir"],
        marker_payload=prepared["marker_payload"],
        coverage=prepared["coverage"],
        binding=prepared["binding"],
        allow_uncalibrated=True,
    )
    address = "proj/widget/plan-alignment/cell/seats/window-a#exec"
    report = addressing.node_dir(address, runtime) / "reconstruction.json"
    report.parent.mkdir(parents=True)
    _write_json(report, {"schema_version": 1})
    binding = {
        "node_address": address,
        "level": "L2+",
        "semantic_cell_role": "reconstruction-verification",
        "semantic_cell_control": str(result.control_record),
        "semantic_report": str(report),
    }

    refused = return_contract.walk_done_contract(address, binding)

    assert any(
        defect.startswith("SEMANTIC-SEAT-OUTPUT:")
        for defect in refused.defects
    )


def _seed_elevation_case(runtime, *, finding_count: int = 1):
    l1, _ = _binding(L1, parent=None, level="L1")
    evidence_dir = (
        addressing.node_dir(L2, runtime) / "plan-alignment" / "cell-elevate"
    )
    findings = []
    for index in range(finding_count):
        evidence = {
            "type": "DRIFT",
            "requirement_id": f"R-{index + 1:03d}",
            "detail": f"drift {index + 1}",
        }
        fingerprint = cell._canonical_sha(
            {
                "type": "DRIFT",
                "key": evidence["requirement_id"],
                "evidence": evidence,
            }
        )
        findings.append(
            {
                "type": "DRIFT",
                "key": evidence["requirement_id"],
                "evidence": evidence,
                "fingerprint": fingerprint,
            }
        )
    evidence_path = _write_json(
        evidence_dir / "evidence-index.json",
        {
            "schema_version": 1,
            "node_address": L2,
            "cell_sha256": "e" * 64,
            "q3_bundle_sha256": "q" * 64,
            "intent_fingerprint": "i" * 64,
            "scope_prefixes": ["R-001"],
            "reports": {},
            "required_elevations": findings,
        },
    )
    evidence_stamp = notary.stamp(evidence_path, read_only=True)
    l2, _ = _binding(
        L2,
        parent=L1,
        level="L2",
        extra={
            "plan_alignment_state": "ready",
            "plan_alignment_semantic_bundle_sha256": "e" * 64,
            "plan_alignment_semantic_evidence": str(evidence_path),
            "plan_alignment_semantic_evidence_sha256": evidence_stamp["sha256"],
            "plan_alignment_required_elevations": findings,
        },
    )
    _seed(l1, l2)
    return evidence_path, evidence_stamp["sha256"], findings


def _write_elevation_marker(
    evidence_path: Path,
    evidence_sha256: str,
    finding: dict,
):
    path = cell.elevation_directory(evidence_path) / (
        finding["fingerprint"] + ".json"
    )
    return _write_json(
        path,
        {
            "schema_version": 1,
            "finding_fingerprint": finding["fingerprint"],
            "semantic_evidence_sha256": evidence_sha256,
            "proposed_disposition": (
                f"Accept the exact {finding['key']} drift as intentional."
            ),
            "question": (
                f"Do you confirm the exact {finding['key']} drift for this plan?"
            ),
        },
    )


def test_owner_questions_are_one_per_finding_and_pass_is_elevate_only(runtime):
    evidence_path, evidence_sha, findings = _seed_elevation_case(
        runtime,
        finding_count=2,
    )
    _write_elevation_marker(
        evidence_path,
        evidence_sha,
        findings[0],
    )

    daemon._reconcile_plan_alignment_elevations_best_effort()

    questions = ledger.read_binding(L1)["plan_alignment_owner_questions"]
    assert len(questions) == 1
    unanswered = ipc.handle_request(
        {
            "command": "plan-alignment-decision",
            "addr": L2,
            "decision": "pass",
            "decision_content": "PASS after owner arbitration.",
        }
    )
    assert unanswered["ok"] is False
    assert (
        f"PLAN-ALIGNMENT-ELEVATION-UNANSWERED:{findings[0]['fingerprint']}"
        in unanswered["errors"]
    )
    assert (
        f"PLAN-ALIGNMENT-ELEVATION-MISSING:{findings[1]['fingerprint']}"
        in unanswered["errors"]
    )


    _write_elevation_marker(
        evidence_path,
        evidence_sha,
        findings[1],
    )
    daemon._reconcile_plan_alignment_elevations_best_effort()
    questions = ledger.read_binding(L1)["plan_alignment_owner_questions"]
    by_finding = {
        row["finding_fingerprint"]: row for row in questions.values()
    }
    first = by_finding[findings[0]["fingerprint"]]
    second = by_finding[findings[1]["fingerprint"]]

    confirmed = ipc.handle_request(
        {
            "command": "answer",
            "addr": L1,
            "question_id": first["question_id"],
            "decision": "confirm",
            "answer_content": "Confirmed for this exact evidence item.",
        }
    )
    rejected = ipc.handle_request(
        {
            "command": "answer",
            "addr": L1,
            "question_id": second["question_id"],
            "decision": "reject",
            "answer_content": "Reject; return this item for repair.",
        }
    )
    assert confirmed["ok"] is True, confirmed
    assert rejected["ok"] is True, rejected
    assert Path(confirmed["answer_artifact"]).stat().st_mode & 0o777 == 0o444

    refused = ipc.handle_request(
        {
            "command": "plan-alignment-decision",
            "addr": L2,
            "decision": "pass",
            "decision_content": "PASS is not allowed.",
        }
    )
    assert refused["ok"] is False
    assert refused["errors"] == [
        f"PLAN-ALIGNMENT-ELEVATION-REJECTED:{findings[1]['fingerprint']}"
    ]
    fail = ipc.handle_request(
        {
            "command": "plan-alignment-decision",
            "addr": L2,
            "decision": "fail",
            "decision_content": "FAIL: repair the rejected drift.",
        }
    )
    assert fail["ok"] is True


def test_exact_finding_answer_reuses_across_regate_but_delta_names_changes(runtime):
    evidence_path, evidence_sha, findings = _seed_elevation_case(runtime)
    _write_elevation_marker(evidence_path, evidence_sha, findings[0])
    daemon._reconcile_plan_alignment_elevations_best_effort()
    question_id = f"plan-alignment-{findings[0]['fingerprint'][:20]}"
    answered = ipc.handle_request(
        {
            "command": "answer",
            "addr": L1,
            "question_id": question_id,
            "decision": "confirm",
            "answer_content": "Confirmed for this exact finding.",
        }
    )
    assert answered["ok"] is True
    prior_question = ledger.read_binding(L1)["plan_alignment_owner_questions"][
        question_id
    ]

    next_evidence = _write_json(
        evidence_path.parent.parent / "cell-regate" / "evidence-index.json",
        {
            "schema_version": 1,
            "node_address": L2,
            "cell_sha256": "f" * 64,
            "q3_bundle_sha256": "r" * 64,
            "intent_fingerprint": "i" * 64,
            "scope_prefixes": ["R-001"],
            "reports": {},
            "required_elevations": findings,
        },
    )
    next_stamp = notary.stamp(next_evidence, read_only=True)
    live = ledger.read_binding(L2)
    updated = executor.record_admission(
        L2,
        expected_owner_token=live["owner_token"],
        delta={
            "plan_alignment_semantic_bundle_sha256": "f" * 64,
            "plan_alignment_semantic_evidence": str(next_evidence),
            "plan_alignment_semantic_evidence_sha256": next_stamp["sha256"],
            "plan_alignment_required_elevations": findings,
        },
        event="test_semantic_regate_ready",
        summary="test fixture completed a same-intent re-gate",
    )
    assert updated is not None and updated.ok
    _write_elevation_marker(next_evidence, next_stamp["sha256"], findings[0])

    daemon._reconcile_plan_alignment_elevations_best_effort()

    reused = ledger.read_binding(L1)["plan_alignment_owner_questions"][question_id]
    assert reused["status"] == "confirmed"
    assert reused["answer_artifact"] == prior_question["answer_artifact"]
    assert reused["answer_reused_by_fingerprint"] is True
    changed = {
        **findings[0],
        "evidence": {**findings[0]["evidence"], "detail": "changed drift"},
    }
    changed["fingerprint"] = cell._canonical_sha(
        {
            "type": changed["type"],
            "key": changed["key"],
            "evidence": changed["evidence"],
        }
    )
    delta = cell.elevation_delta(findings, [changed])
    assert delta["new"] == []
    assert delta["changed"] == [changed]
    assert delta["cleared"] == []


def test_no_elevations_means_l1_pass_stands_alone(runtime):
    evidence_path, _evidence_sha, _findings = _seed_elevation_case(
        runtime,
        finding_count=0,
    )
    assert evidence_path.is_file()

    response = ipc.handle_request(
        {
            "command": "plan-alignment-decision",
            "addr": L2,
            "decision": "pass",
            "decision_content": "PASS: no owner arbitration was required.",
        }
    )

    assert response["ok"] is True
    assert "plan_alignment_owner_questions" not in ledger.read_binding(L1)


def test_answer_cli_serializes_current_question_without_changing_legacy_shape(tmp_path):
    answer_file = tmp_path / "owner-answer.md"
    answer_file.write_text("Confirm this exact finding.\n", encoding="utf-8")
    current = harnessctl._build_request(
        harnessctl.build_parser().parse_args(
            [
                "answer",
                L1,
                "--question-id",
                "plan-alignment-abc",
                "--decision",
                "confirm",
                "--file",
                str(answer_file),
            ]
        )
    )
    legacy = harnessctl._build_request(
        harnessctl.build_parser().parse_args(
            ["answer", L1, "--text", "legacy human answer"]
        )
    )

    assert current == {
        "command": "answer",
        "addr": L1,
        "question_id": "plan-alignment-abc",
        "decision": "confirm",
        "answer_content": "Confirm this exact finding.\n",
    }
    assert legacy == {
        "command": "answer",
        "addr": L1,
        "answer_content": "legacy human answer",
    }
