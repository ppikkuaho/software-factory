"""T64 — first-class L2 -> L1 plan-alignment readiness handoff."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

import harnessd.addressing as addressing
import harnessd.contracts as contracts
import harnessd.daemon as daemon
import harnessd.executor as executor
import harnessd.fencing as fencing
import harnessd.harnessctl as harnessctl
import harnessd.ipc as ipc
import harnessd.ledger as ledger
import harnessd.notary as notary
from harnessd.spawn import chokepoint


L1 = "proj#exec"
L2 = "proj/logview#exec"
SESSION = "sess-plan-align-0001"
SUBAGENT = "subagent-plan-align"


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
    state="running",
    generation=0,
    lease_epoch=1,
    extra=None,
):
    token = fencing.mint_owner_token(node_address, SUBAGENT, SESSION, lease_epoch)
    rec = {
        "node_address": node_address,
        "parent_address": parent_address,
        "level": level,
        "subagent_id": SUBAGENT,
        "session_uuid": SESSION,
        "state": state,
        "generation": generation,
        "lease_epoch": lease_epoch,
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
    seeded = {b["node_address"]: copy.deepcopy(b) for b in bindings}
    for binding in seeded.values():
        if binding.get("level") != "L2":
            continue
        node_dir = addressing.node_dir(binding["node_address"], ledger.RUNTIME_ROOT)
        intent = node_dir / "client-brief" / "intent-spec.md"
        intent.parent.mkdir(parents=True, exist_ok=True)
        intent.write_text(
            """# Intent Spec

## Requirements

| ID | Requirement | Tag | Priority | MNF | Parent | Fluency | Reflect-back status |
|---|---|---|---|---|---|---|---|
| R-001 | Produce the validated plan | decided | must | — | O-1 | plain | confirmed |

## ID → intent-span map

| ID | Intent span |
|---|---|
| R-001 | Produce the validated plan. |

## Reflect-back script

Status: confirmed

Produce the validated plan.
""",
            encoding="utf-8",
        )
        raw_request = node_dir / "client-brief" / "raw-request.md"
        raw_request.write_text("Produce the validated plan.\n", encoding="utf-8")
        binding["intent_spec_receipt"] = contracts.contract_receipt(
            binding["node_address"],
            binding.get("parent_address"),
            intent,
            notary.stamp(intent),
        )
        binding["raw_request_receipt"] = contracts.contract_receipt(
            binding["node_address"],
            binding.get("parent_address"),
            raw_request,
            notary.stamp(raw_request),
        )
    ledger.write_binding(seeded, _lock_held=True)


def _jsonl(path):
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        rows.append(json.loads(raw))
    return rows


def _write_plan_alignment_package(runtime, *, marker_extra=None, package_name="validated-plan-package.md"):
    node_dir = addressing.node_dir(L2, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    package = node_dir / package_name
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_text("# Validated Plan Package\n", encoding="utf-8")
    construction = node_dir / "plan-alignment" / "construction.md"
    construction.parent.mkdir(parents=True, exist_ok=True)
    construction.write_text(
        "# Design\n\n"
        "<!-- trace: { id: R-001.1, serves: [R-001], kind: design, "
        "level: L2, node: proj/logview } -->\n",
        encoding="utf-8",
    )
    verification = node_dir / "plan-alignment" / "verification.md"
    verification.write_text(
        "# Acceptance criterion\n\n"
        "<!-- trace: { id: TST-PLAN-001, serves: [R-001], kind: test, "
        "level: L3, node: proj/logview } -->\n",
        encoding="utf-8",
    )
    coverage_manifest = node_dir / "plan-alignment-coverage.json"
    coverage_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [
                    {
                        "path": "plan-alignment/construction.md",
                        "trace_ids": ["R-001.1"],
                    },
                    {
                        "path": "plan-alignment/verification.md",
                        "trace_ids": ["TST-PLAN-001"],
                    },
                ],
                "failure_path_criteria": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    semantic_manifest = node_dir / "plan-alignment-semantic.json"
    semantic_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "verification_artifacts": [
                    {
                        "path": "plan-alignment/verification.md",
                        "module": "logview",
                    }
                ],
                "construction_modules": [
                    {
                        "module": "logview",
                        "artifacts": ["plan-alignment/construction.md"],
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    marker = {
        "type": "plan_alignment_ready",
        "package": package_name,
        "coverage_manifest": "plan-alignment-coverage.json",
        "semantic_manifest": "plan-alignment-semantic.json",
        "message": "Validated plan package is ready for L1 plan-alignment review.",
    }
    if marker_extra:
        marker.update(marker_extra)
    marker_path = node_dir / "plan-alignment-ready.json"
    marker_path.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
    return marker_path, package


def _mark_semantic_ready() -> dict:
    live = ledger.read_binding(L2)
    result = executor.record_admission(
        L2,
        expected_owner_token=live["owner_token"],
        delta={
            "plan_alignment_state": "ready",
            "plan_alignment_required_elevations": [],
            "plan_alignment_semantic_evidence": "test-semantic-evidence.json",
            "plan_alignment_semantic_evidence_sha256": "e" * 64,
        },
        event="test_semantic_cell_ready",
        summary="test fixture completed the semantic cell",
    )
    assert result is not None and result.ok
    return result.binding


def _wal_events(node):
    return [row["event"] for row in ledger.load_wal() if row.get("node_address") == node]


def test_plan_alignment_submission_parks_semantic_cell_without_waking_l1(runtime):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, token = _binding(L2, parent_address=L1, level="L2")
    _seed([l1, l2])
    marker, package = _write_plan_alignment_package(runtime)

    result = chokepoint.submit_plan_alignment_ready(
        L2,
        marker_path=marker,
        expected_owner_token=token,
    )

    assert result.ok
    live = ledger.read_binding(L2)
    assert live["state"] == "running"
    assert live["plan_alignment_state"] == "semantic_cell_pending"
    assert live["plan_alignment_package"] == str(package)
    assert live["plan_alignment_ready_artifact"] == str(marker)
    assert _wal_events(L2) == ["plan_alignment_semantic_cell_pending"]
    rows = _jsonl(addressing.inbox_path(L1, runtime))
    assert rows == []


def test_daemon_marker_sweep_submits_plan_alignment_ready(runtime):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, _token = _binding(L2, parent_address=L1, level="L2")
    _seed([l1, l2])
    _write_plan_alignment_package(runtime)

    daemon._submit_plan_alignment_markers_best_effort()

    assert ledger.read_binding(L2)["plan_alignment_state"] == "semantic_cell_pending"
    rows = _jsonl(addressing.inbox_path(L1, runtime))
    assert rows == []


def test_plan_alignment_ready_is_idempotent_on_exact_bundle_identity(runtime):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, token = _binding(L2, parent_address=L1, level="L2")
    _seed([l1, l2])
    marker, _package = _write_plan_alignment_package(runtime)

    first = chokepoint.submit_plan_alignment_ready(L2, marker_path=marker, expected_owner_token=token)
    second = chokepoint.submit_plan_alignment_ready(L2, marker_path=marker, expected_owner_token=token)

    assert first.ok
    assert second is None
    assert _wal_events(L2) == ["plan_alignment_semantic_cell_pending"]
    rows = _jsonl(addressing.inbox_path(L1, runtime))
    assert rows == []


def test_plan_alignment_ready_lost_wake_recovery_replays_parent_pointer(runtime):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, token = _binding(L2, parent_address=L1, level="L2")
    _seed([l1, l2])
    marker, _package = _write_plan_alignment_package(runtime)
    assert chokepoint.submit_plan_alignment_ready(L2, marker_path=marker, expected_owner_token=token).ok
    _mark_semantic_ready()

    inbox = addressing.inbox_path(L1, runtime)
    assert inbox.exists() is False

    daemon._recover_plan_alignment_notifications_best_effort()

    rows = _jsonl(inbox)
    assert len(rows) == 1
    assert rows[0]["type"] == "design-submission"
    assert rows[0]["child"] == L2
    daemon._recover_plan_alignment_notifications_best_effort()
    assert len(_jsonl(inbox)) == 1


def test_plan_alignment_ready_refuses_absent_or_out_of_node_package(runtime):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, token = _binding(L2, parent_address=L1, level="L2")
    _seed([l1, l2])
    outside = runtime / "outside-plan.md"
    outside.write_text("# outside\n", encoding="utf-8")
    marker, _package = _write_plan_alignment_package(
        runtime,
        marker_extra={"package": str(outside)},
    )

    result = chokepoint.submit_plan_alignment_ready(
        L2,
        marker_path=marker,
        expected_owner_token=token,
    )

    assert result.ok is False
    assert "inside its node workspace" in result.errors[0]
    assert ledger.read_binding(L2).get("plan_alignment_state") is None
    assert _jsonl(addressing.inbox_path(L1, runtime)) == []


def test_malformed_plan_alignment_marker_journals_once_and_wakes_l2(runtime):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, _token = _binding(L2, parent_address=L1, level="L2")
    _seed([l1, l2])
    node_dir = addressing.node_dir(L2, runtime)
    node_dir.mkdir(parents=True, exist_ok=True)
    marker = node_dir / "plan-alignment-ready.json"
    marker.write_text(
        json.dumps(
            {
                "type": "plan_alignment_ready",
                "package": "missing-plan-package.md",
                "message": "This marker points at a missing package.",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    daemon._submit_plan_alignment_markers_best_effort()
    daemon._submit_plan_alignment_markers_best_effort()

    assert _wal_events(L2).count("plan_alignment_marker_invalid") == 1
    live = ledger.read_binding(L2)
    assert len(live["nonterminal_marker_errors"]) == 1
    rows = _jsonl(addressing.inbox_path(L2, runtime))
    marker_rows = [row for row in rows if row.get("type") == "nonterminal_marker_invalid"]
    assert len(marker_rows) == 1
    assert marker_rows[0]["marker_kind"] == "plan_alignment"
    assert marker_rows[0]["marker_artifact"] == str(marker)


def test_plan_alignment_ready_does_not_use_build_gate_state(runtime):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, token = _binding(
        L2,
        parent_address=L1,
        level="L2",
        extra={"gate_required": True, "gate_state": "gate_bounced", "gate_id": "build-gate-1"},
    )
    _seed([l1, l2])
    marker, _package = _write_plan_alignment_package(runtime)

    result = chokepoint.submit_plan_alignment_ready(L2, marker_path=marker, expected_owner_token=token)

    assert result.ok
    live = ledger.read_binding(L2)
    assert live["plan_alignment_state"] == "semantic_cell_pending"
    assert live["gate_state"] == "gate_bounced"


def test_plan_alignment_decision_pass_wakes_l2(runtime):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, token = _binding(L2, parent_address=L1, level="L2")
    _seed([l1, l2])
    marker, _package = _write_plan_alignment_package(runtime)
    assert chokepoint.submit_plan_alignment_ready(L2, marker_path=marker, expected_owner_token=token).ok
    _mark_semantic_ready()

    response = ipc.handle_request(
        {
            "command": "plan-alignment-decision",
            "addr": L2,
            "decision": "pass",
            "decision_content": "# PASS\n\nThe validated plan may proceed to freeze.\n",
        }
    )

    assert response["ok"] is True
    live = ledger.read_binding(L2)
    assert live["plan_alignment_state"] == "decision_posted"
    assert live["plan_alignment_decision"] == "pass"
    rows = _jsonl(addressing.inbox_path(L2, runtime))
    assert len(rows) == 1
    assert rows[0]["type"] == "plan_alignment_decision"
    assert rows[0]["phase"] == "plan_alignment"
    assert rows[0]["decision"] == "pass"
    assert rows[0]["decision_artifact"] == live["plan_alignment_decision_artifact"]


def test_plan_alignment_decision_is_idempotent_for_same_artifact(runtime):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, token = _binding(L2, parent_address=L1, level="L2")
    _seed([l1, l2])
    marker, _package = _write_plan_alignment_package(runtime)
    assert chokepoint.submit_plan_alignment_ready(L2, marker_path=marker, expected_owner_token=token).ok
    _mark_semantic_ready()
    request = {
        "command": "plan-alignment-decision",
        "addr": L2,
        "decision": "pass",
        "decision_content": "# PASS\n\nProceed.\n",
    }

    first = ipc.handle_request(request)
    second = ipc.handle_request(request)

    assert first["ok"] is True
    assert second["ok"] is True
    assert _wal_events(L2).count("plan_alignment_decision_posted") == 1
    rows = _jsonl(addressing.inbox_path(L2, runtime))
    assert len([row for row in rows if row.get("type") == "plan_alignment_decision"]) == 1


def test_plan_alignment_decision_fail_wakes_l2_with_repair_directions(runtime):
    l1, _ = _binding(L1, parent_address=None, level="L1")
    l2, token = _binding(L2, parent_address=L1, level="L2")
    _seed([l1, l2])
    marker, _package = _write_plan_alignment_package(runtime)
    assert chokepoint.submit_plan_alignment_ready(L2, marker_path=marker, expected_owner_token=token).ok
    _mark_semantic_ready()

    response = ipc.handle_request(
        {
            "command": "plan-alignment-decision",
            "addr": L2,
            "decision": "fail",
            "decision_content": "# FAIL\n\nRepair the untraced interface assumption and resubmit.\n",
        }
    )

    assert response["ok"] is True
    live = ledger.read_binding(L2)
    assert live["plan_alignment_decision"] == "fail"
    rows = _jsonl(addressing.inbox_path(L2, runtime))
    assert rows[0]["decision"] == "fail"
    assert "repair" in rows[0]["message"].lower()


def test_plan_alignment_decision_cli_serializes_file_content(tmp_path):
    decision = tmp_path / "verdict.md"
    decision.write_text("# PASS\n", encoding="utf-8")
    parser = harnessctl.build_parser()
    args = parser.parse_args(
        [
            "--socket",
            str(tmp_path / "daemon.sock"),
            "plan-alignment-decision",
            L2,
            "--decision",
            "pass",
            "--file",
            str(decision),
        ]
    )

    request = harnessctl._build_request(args)

    assert request["command"] == "plan-alignment-decision"
    assert request["addr"] == L2
    assert request["decision"] == "pass"
    assert request["decision_content"] == "# PASS\n"
