"""Acceptance contract for the non-executing B2 role foundation increment."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from jsonschema import Draft202012Validator

from ht.errors import HtError, HtUsageError
from ht.paths import Root
from ht.references import parse_ref, resolve_ref
from ht.role_validation import validate_role_wire
from ht.role_registry import (
    PROFILE_ORDER,
    PROJECTION_ORDER,
    load_reference_projections,
    load_runtime_profiles,
)
from ht.runtime.schema import validate as validate_runtime
from ht.schemas import validate as validate_repository


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "system" / "schemas"
RUNTIME_SCHEMAS = ROOT / "system" / "ht" / "runtime" / "schemas"
ROLES = ROOT / "system" / "roles"
SHA = "0" * 64
UUID = "00000000-0000-4000-8000-000000000001"
UUID2 = "00000000-0000-4000-8000-000000000002"
NOW = "2026-07-15T00:00:00Z"


B2_REPOSITORY_SCHEMAS = {
    "subgoal.schema.json",
    "task-package.schema.json",
    "inbox-delivery.schema.json",
    "inbox-receipt.schema.json",
    "action-receipt.schema.json",
    "producer-return.schema.json",
    "wave-b2-leak-closures.schema.json",
    "wave-b2-commissioning-result.schema.json",
    "wave-b2-commissioning-manifest.schema.json",
}


def _assert_rejected(doc_type: str, value: dict) -> None:
    with pytest.raises(HtError, match="schema-nonconforming"):
        validate_repository(SCHEMAS, doc_type, value)


def _valid_output_contract() -> dict:
    return {
        "schema_version": "hypothesis-tree-role-output-contract/1.0.0",
        "actions": [
            {"action_kind": action_kind, "required_output_slots": [], "optional_output_slots": []}
            for action_kind in (
                "pc.route-subgoal",
                "pc.rerank-issue-queue",
                "pc.annotate-ratification",
                "pc.no-op",
            )
        ],
        "slots": [],
        "terminal_outcomes": ["SUCCEEDED", "FAILED", "crashed"],
    }


def _valid_pc_package() -> dict:
    return {
        "schema_version": "hypothesis-tree-task-package/1.0.0",
        "package_id": "TP-1",
        "package_kind": "pc-wake",
        "role_profile_id": "principal-coordinator-v1",
        "target_ref": "pc#principal-coordinator",
        "issue_ref": None,
        "subgoal_ref": None,
        "dispatch_ref": None,
        "semantic_brief": {
            "title": "Wake",
            "objective": "Route the next bounded move",
            "done_definition": "Return one action",
            "constraints": [],
            "context_refs": ["issue-queue#current"],
        },
        "reference_map": [
            {
                "ref": "issue-queue#current",
                "repository_relpath": "tier1/issue-queue.json",
                "sha256": SHA,
                "bytes": 2,
                "projection_schema_version": "hypothesis-tree-semantic-issue-queue/1.0.0",
                "projection_sha256": SHA,
                "projection_bytes": 2,
                "use_when": "Ranking current work",
            }
        ],
        "workspace": {
            "repository_root": ".",
            "worktree": None,
            "branch": None,
            "plan_ref": None,
            "report_ref": None,
            "archive_ref": None,
            "qa_ref": None,
        },
        "output_contract": _valid_output_contract(),
        "verifier_fence_set": None,
        "coordination_precondition": {
            "issue_queue_ref": "issue-queue#current",
            "issue_queue_schema_version": "hypothesis-tree-issue-queue/1.0.0",
            "issue_queue_raw_sha256": SHA,
            "issue_queue_canonical_json": "{}\n",
        },
        "prompt_sha256": SHA,
        "action_contract_sha256": SHA,
        "source_action_ref": None,
        "created_at": NOW,
    }


def _role_packet() -> dict:
    return {
        "ref": "repository-file#system/roles/principal-coordinator.v1.md",
        "version": "v1",
        "sha256": SHA,
    }


def _valid_sealed_request() -> dict:
    return {
        "schema_version": "hypothesis-tree-runtime-role-request/2.0.0",
        "request_id": UUID,
        "task_package_ref": "task-package#TP-1",
        "task_package_repository_relpath": "tier1/task-packages/TP-1.json",
        "task_package_sha256": SHA,
        "task_package_head_blob_oid": "1" * 40,
        "submission_repository_commit": "2" * 40,
        "git_object_format": "sha1",
        "profile_id": "principal-coordinator-v1",
        "role_packet": _role_packet(),
        "procedure": None,
        "prompt_sha256": SHA,
        "action_contract_sha256": SHA,
        "verifier_fence_set_sha256": None,
        "inbox_delivery_ref": None,
        "inbox_delivery_sha256": None,
        "producer_adapter_token": "sealed-codex-fixture/1.0.0",
        "producer_launcher_token": None,
        "producer_launcher_sha256": None,
        "producer_return_schema_source_sha256": SHA,
        "producer_return_schema_copy_sha256": SHA,
        "transcript_format": "codex-exec-jsonl/0.144.1",
        "approval_required": False,
        "attempt": 1,
        "retry_lineage": [],
        "created_at": NOW,
    }


def _valid_sealed_packet() -> dict:
    return {
        "schema_version": "hypothesis-tree-runtime-role-session-packet/2.0.0",
        "runtime_id": UUID,
        "request_id": UUID,
        "binding_id": UUID2,
        "node_address": f"runtime/{UUID2}#role",
        "lease_epoch": 1,
        "session_id": UUID,
        "fence": "role:1",
        "profile_id": "principal-coordinator-v1",
        "authority_role": "pc",
        "level": "top",
        "attempt": 1,
        "retry_lineage": [],
        "task_package_ref": "task-package#TP-1",
        "task_package_sha256": SHA,
        "task_package_head_blob_oid": "1" * 40,
        "submission_repository_commit": "2" * 40,
        "admission_repository_commit": "3" * 40,
        "git_object_format": "sha1",
        "role_packet": _role_packet(),
        "procedure": None,
        "prompt_sha256": SHA,
        "action_contract_sha256": SHA,
        "verifier_fence_set_sha256": None,
        "inbox_delivery_ref": None,
        "inbox_delivery_sha256": None,
        "producer_adapter_token": "sealed-codex-fixture/1.0.0",
        "producer_launcher_token": None,
        "producer_launcher_sha256": None,
        "producer_return_schema_source_sha256": SHA,
        "producer_return_schema_copy_sha256": SHA,
        "producer_home_manifest_sha256": None,
        "transcript_format": "codex-exec-jsonl/0.144.1",
        "approval_required": False,
        "approval_request_sha256": None,
        "approved_at": None,
        "wrapper_instance_id": UUID,
        "runner_instance_id": UUID2,
        "packet_created_at": NOW,
    }


def _session_common(schema_version: str) -> dict:
    return {
        "schema_version": schema_version,
        "runtime_id": UUID,
        "request_id": UUID,
        "binding_id": UUID2,
        "lease_epoch": 1,
        "session_id": UUID,
        "fence": "role:1",
        "profile_id": "principal-coordinator-v1",
        "packet_sha256": SHA,
    }


def _valid_sealed_capture() -> dict:
    return {
        "schema_version": "hypothesis-tree-runtime-role-capture-manifest/1.0.0",
        "runtime_id": UUID,
        "session_uuid": UUID,
        "level": "top",
        "profile_id": "principal-coordinator-v1",
        "fence": "role:1",
        "transcript_format": "codex-exec-jsonl/0.144.1",
        "producer_adapter_token": "sealed-codex-fixture/1.0.0",
        "producer_binary_sha256": None,
        "producer_version": "ht-sealed-zero-model/1.0.0",
        "producer_native_id": f"sealed-fixture:{UUID}",
        "main_relative_path": "transcript/codex-exec.jsonl",
        "stderr_relative_path": "transcript/codex-stderr.bin",
        "final_relative_path": "transcript/producer-final.txt",
        "inventory": [
            {"relative_path": "transcript/codex-exec.jsonl", "sha256": SHA, "bytes": 1},
            {"relative_path": "transcript/codex-stderr.bin", "sha256": SHA, "bytes": 0},
            {"relative_path": "transcript/producer-final.txt", "sha256": SHA, "bytes": 1},
        ],
        "producer_process_exit_sha256": SHA,
        "final_response_sha256": SHA,
        "reference_snapshot_manifest_sha256": SHA,
        "reference_snapshot_ack_sha256": SHA,
        "capture_status": "complete",
        "created_at": NOW,
    }


def test_all_b2_schemas_are_json_schema_valid_and_committed_at_exact_paths() -> None:
    assert {path.name for path in SCHEMAS.glob("*.json")} >= B2_REPOSITORY_SCHEMAS
    for path in sorted(SCHEMAS.glob("*.json")):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
    for name in ("checkpoint-role.schema.json", "role-wire.schema.json"):
        path = RUNTIME_SCHEMAS / name
        assert path.is_file()
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_repository_schemas_accept_exact_objects_and_reject_unknown_nested_fields() -> None:
    subgoal = {
        "schema_version": "hypothesis-tree-subgoal/1.0.0",
        "id": "SG-1",
        "issue_ref": "I-1",
        "lane": "L4",
        "question": "What should be tested?",
        "done_definition": "A bounded answer exists",
        "created_at": NOW,
        "created_by_action_ref": "action-receipt#AR-1",
    }
    validate_repository(SCHEMAS, "subgoal", subgoal)
    _assert_rejected("subgoal", {**subgoal, "future": True})

    package = _valid_pc_package()
    validate_repository(SCHEMAS, "task_package", package)
    wrong_profile = copy.deepcopy(package)
    wrong_profile["role_profile_id"] = "senior-v1"
    _assert_rejected("task_package", wrong_profile)
    nested_unknown = copy.deepcopy(package)
    nested_unknown["semantic_brief"]["control_identity"] = UUID
    _assert_rejected("task_package", nested_unknown)
    boolean_integer = copy.deepcopy(package)
    boolean_integer["reference_map"][0]["bytes"] = True
    _assert_rejected("task_package", boolean_integer)
    wrong_target = copy.deepcopy(package)
    wrong_target["target_ref"] = "issue#I-99"
    _assert_rejected("task_package", wrong_target)
    wrong_source = copy.deepcopy(package)
    wrong_source["source_action_ref"] = "action-receipt#AR-1"
    _assert_rejected("task_package", wrong_source)


def test_producer_return_closes_outcome_action_and_payload_vocabulary() -> None:
    succeeded = {
        "schema_version": "hypothesis-tree-producer-return/1.0.0",
        "outcome": "SUCCEEDED",
        "action": {
            "action_kind": "pc.no-op",
            "target_ref": "pc#principal-coordinator",
            "payload": {"reason_code": "awaiting-user"},
            "outputs": [],
        },
        "failure": None,
    }
    validate_repository(SCHEMAS, "producer_return", succeeded)

    unknown = copy.deepcopy(succeeded)
    unknown["action"]["payload"]["request_id"] = UUID
    _assert_rejected("producer_return", unknown)
    mixed = copy.deepcopy(succeeded)
    mixed["failure"] = {"reason_code": "tool-failure", "summary": "mixed"}
    _assert_rejected("producer_return", mixed)
    wrong_pair = copy.deepcopy(succeeded)
    wrong_pair["action"]["action_kind"] = "director.create-dispatch"
    _assert_rejected("producer_return", wrong_pair)


def test_action_receipt_cross_schema_payload_is_closed() -> None:
    receipt = {
        "schema_version": "hypothesis-tree-action-receipt/1.0.0",
        "id": "AR-1",
        "action_id": UUID2,
        "action_kind": "pc.no-op",
        "target_ref": "pc#principal-coordinator",
        "payload": {"reason_code": "awaiting-user"},
        "profile_id": "principal-coordinator-v1",
        "role_packet": {"version": "v1", "sha256": SHA},
        "task_package_ref": "task-package#TP-1",
        "task_package_sha256": SHA,
        "verifier_fence_set_sha256": None,
        "runtime_id": UUID,
        "request_id": UUID,
        "binding_id": UUID2,
        "lease_epoch": 1,
        "session_id": UUID,
        "fence": "role:1",
        "packet_sha256": SHA,
        "source_action_sha256": SHA,
        "outputs": [],
        "created_at": NOW,
    }
    validate_repository(SCHEMAS, "action_receipt", receipt)
    bad = copy.deepcopy(receipt)
    bad["payload"]["session_id"] = UUID
    _assert_rejected("action_receipt", bad)


def test_v2_checkpoint_is_additive_and_role_wire_rejects_every_b1_token() -> None:
    v2 = {
        "schema_version": "hypothesis-tree-runtime-checkpoint/2.0.0",
        "runtime_id": UUID,
        "last_seq": 0,
        "clean_wal_sha256": SHA,
        "daemon_incarnation_id": None,
        "request_index": {},
        "dedup_index": {},
        "session_index": {},
        "control_index": {},
        "bindings": {"runtime#kernel": {"node_address": "runtime#kernel"}},
        "binding_ledger_sha256": SHA,
        "final_tail": None,
        "role_capability_sha256": SHA,
        "upgrade_base_seq": 0,
        "upgrade_base_clean_wal_sha256": SHA,
        "upgrade_base_checkpoint_sha256": SHA,
        "upgrade_base_binding_ledger_sha256": SHA,
    }
    validate_runtime("checkpoint-role.schema.json", v2)
    validate_runtime("role-wire.schema.json", v2)

    bad_integer = {**v2, "last_seq": True}
    with pytest.raises(HtError, match="checkpoint-role"):
        validate_runtime("checkpoint-role.schema.json", bad_integer)
    with pytest.raises(HtError, match="role-wire"):
        validate_runtime("role-wire.schema.json", {**v2, "unknown": 1})

    b1 = copy.deepcopy(v2)
    b1["schema_version"] = "hypothesis-tree-runtime-checkpoint/1.0.0"
    for key in (
        "role_capability_sha256",
        "upgrade_base_seq",
        "upgrade_base_clean_wal_sha256",
        "upgrade_base_checkpoint_sha256",
        "upgrade_base_binding_ledger_sha256",
    ):
        b1.pop(key)
    validate_runtime("checkpoint.schema.json", b1)
    with pytest.raises(HtError, match="role-wire"):
        validate_runtime("role-wire.schema.json", b1)


def test_role_wire_dispatch_is_closed_and_real_request_pair_is_structural() -> None:
    capability = {
        "schema_version": "hypothesis-tree-runtime-role-capability/1.0.0",
        "capability": "owned-role-runtime-v1",
        "runtime_id": UUID,
        "runtime_schema_version": "hypothesis-tree-runtime/1.0.0",
        "role_request_schema_version": "hypothesis-tree-runtime-role-request/2.0.0",
        "upgrade_base_seq": 0,
        "upgrade_base_clean_wal_sha256": SHA,
        "upgrade_base_checkpoint_sha256": SHA,
        "upgrade_base_binding_ledger_sha256": SHA,
        "created_at": NOW,
    }
    validate_runtime("role-wire.schema.json", capability)
    with pytest.raises(HtError, match="role-wire"):
        validate_runtime("role-wire.schema.json", {**capability, "launcher_sha256": SHA})
    with pytest.raises(HtError, match="role-wire"):
        validate_runtime(
            "role-wire.schema.json",
            {"schema_version": "hypothesis-tree-runtime-request/1.0.0"},
        )

    action = {
        "schema_version": "hypothesis-tree-runtime-role-action/1.0.0",
        "action_id": UUID2,
        "action_kind": "pc.no-op",
        "target_ref": "pc#principal-coordinator",
        "payload": {"reason_code": "awaiting-user"},
        "profile_id": "principal-coordinator-v1",
        "role_packet": {"version": "v1", "sha256": SHA},
        "task_package_ref": "task-package#TP-1",
        "task_package_sha256": SHA,
        "verifier_fence_set_sha256": None,
        "outputs": [],
        "created_at": NOW,
    }
    validate_runtime("role-wire.schema.json", action)
    with pytest.raises(HtError, match="role-wire"):
        validate_runtime(
            "role-wire.schema.json",
            {**action, "payload": {"reason_code": "awaiting-user", "future": True}},
        )
    with pytest.raises(HtError, match="role-wire"):
        validate_runtime(
            "role-wire.schema.json",
            {**action, "action_kind": "director.create-dispatch"},
        )


def test_review_exact_adapter_admission_packet_and_launch_pairs_are_closed() -> None:
    request = _valid_sealed_request()
    validate_role_wire(request)
    for changes in (
        {"producer_adapter_token": "sealed-zero-model/1.0.0"},
        {"transcript_format": "sealed-fixture-jsonl/1.0.0"},
        {"producer_adapter_token": "codex-exec-fixed/1.0.0"},
    ):
        with pytest.raises(HtError):
            validate_role_wire({**request, **changes})

    rejected = {
        "schema_version": "hypothesis-tree-runtime-role-admission-response/2.0.0",
        "status": "rejected",
        "request_id": UUID,
        "binding_id": UUID2,
        "node_address": f"runtime/{UUID2}#role",
        "original_request_id": None,
        "original_binding_id": None,
        "lease_epoch": None,
        "session_id": None,
        "packet_sha256": None,
        "reason_code": "invalid-task-package",
        "recovery_created": None,
    }
    validate_role_wire(rejected)
    impossible = {
        **rejected,
        "original_request_id": UUID,
        "session_id": UUID,
        "lease_epoch": 1,
        "packet_sha256": SHA,
        "reason_code": None,
        "recovery_created": True,
    }
    with pytest.raises(HtError):
        validate_role_wire(impossible)

    packet = _valid_sealed_packet()
    validate_role_wire(packet)
    with pytest.raises(HtError):
        validate_role_wire({**packet, "authority_role": "verifier", "level": "L4"})
    with pytest.raises(HtError):
        validate_role_wire({**packet, "approval_required": True, "approval_request_sha256": SHA})

    launch = {
        **_session_common("hypothesis-tree-runtime-role-launch/2.0.0"),
        "wrapper_instance_id": UUID,
        "runner_instance_id": UUID2,
        "packet_relative_path": "packet.json",
        "verifier_fence_set_sha256": None,
        "entrypoint_token": "ht-runtime-role-wrapper/1.0.0",
        "runner_entrypoint_token": "ht-runtime-profile-runner/1.0.0",
        "producer_launcher_token": None,
        "producer_launcher_sha256": None,
        "producer_return_schema_source_sha256": SHA,
        "producer_return_schema_copy_sha256": SHA,
        "producer_home_manifest_sha256": None,
        "custody_protocol": "inherited-flock-open-description/1.0.0",
        "barrier_protocol": "private-pipe-role-start-token/1.0.0",
        "runner_liveness_protocol": "private-pipe-wrapper-liveness-eof/1.0.0",
        "producer_exec_barrier_protocol": None,
        "sealed_fixture_transport_protocol": "private-socketpair-sealed-fixture/1.0.0",
    }
    validate_role_wire(launch)
    with pytest.raises(HtError):
        validate_role_wire(
            {
                **launch,
                "producer_launcher_token": "ht-runtime-role-blocked-exec/1.0.0",
                "producer_launcher_sha256": SHA,
            }
        )


def test_review_exact_producer_exit_result_process_and_capture_branches() -> None:
    started = {
        **_session_common("hypothesis-tree-runtime-role-producer-started/1.0.0"),
        "runner_instance_id": UUID2,
        "producer_pid": None,
        "producer_launcher_token": None,
        "producer_launcher_sha256": None,
        "producer_binary_sha256": None,
        "producer_version": "ht-sealed-zero-model/1.0.0",
        "argv_sha256": None,
        "environment_sha256": None,
        "producer_return_schema_source_sha256": SHA,
        "producer_return_schema_copy_sha256": SHA,
        "producer_home_manifest_sha256": None,
        "reference_snapshot_manifest_sha256": SHA,
        "reference_snapshot_ack_sha256": SHA,
        "started_at": NOW,
    }
    validate_role_wire(started)
    with pytest.raises(HtError):
        validate_role_wire({**started, "producer_pid": 99})

    producer_exit = {
        **_session_common("hypothesis-tree-runtime-role-producer-process-exit/1.0.0"),
        "runner_instance_id": UUID2,
        "producer_pid": None,
        "producer_started_sha256": SHA,
        "exit_kind": "sealed-fixture",
        "exit_code": None,
        "signal": None,
        "timed_out": False,
        "term_sent": False,
        "kill_sent": False,
        "reaped": False,
        "stdout_sha256": SHA,
        "stderr_sha256": SHA,
        "final_response_sha256": SHA,
        "reference_snapshot_manifest_sha256": SHA,
        "reference_snapshot_ack_sha256": SHA,
        "exited_at": NOW,
    }
    validate_role_wire(producer_exit)
    impossible_producer_exit = {
        **producer_exit,
        "exit_kind": "signaled",
        "signal": 9,
        "timed_out": True,
        "reaped": False,
    }
    with pytest.raises(HtError):
        validate_role_wire(impossible_producer_exit)

    result = {
        **_session_common("hypothesis-tree-runtime-role-result/1.0.0"),
        "runner_instance_id": UUID2,
        "outcome": "FAILED",
        "action_ids": [],
        "action_sha256s": [],
        "capture_manifest_sha256": SHA,
        "producer_return_sha256": SHA,
        "producer_process_exit_sha256": SHA,
        "reference_snapshot_manifest_sha256": SHA,
        "reference_snapshot_ack_sha256": SHA,
        "result_at": NOW,
    }
    validate_role_wire(result)
    with pytest.raises(HtError):
        validate_role_wire(
            {**result, "action_ids": [UUID2], "producer_return_sha256": None}
        )

    process_exit = {
        **_session_common("hypothesis-tree-runtime-role-process-exit/1.0.0"),
        "wrapper_instance_id": UUID,
        "runner_instance_id": UUID2,
        "wrapper_pid": 10,
        "runner_pid": 11,
        "runner_wait_status": 0,
        "runner_reaped": True,
        "exit_kind": "exited",
        "exit_code": 0,
        "signal": None,
        "started_sha256": SHA,
        "ready_sha256": SHA,
        "reference_snapshot_manifest_sha256": SHA,
        "reference_snapshot_ack_sha256": SHA,
        "result_sha256": SHA,
        "terminal_sha256": SHA,
        "exited_at": NOW,
    }
    validate_role_wire(process_exit)
    with pytest.raises(HtError, match="wait status"):
        validate_role_wire(
            {
                **process_exit,
                "runner_wait_status": 1,
                "exit_code": 0,
                "result_sha256": None,
                "terminal_sha256": None,
            }
        )
    with pytest.raises(HtError):
        validate_role_wire(
            {
                **process_exit,
                "runner_wait_status": -9,
                "exit_kind": "signaled",
                "exit_code": None,
                "signal": 9,
                "result_sha256": None,
                "terminal_sha256": SHA,
            }
        )

    capture = _valid_sealed_capture()
    validate_role_wire(capture)
    with pytest.raises(HtError, match="sealed native identity"):
        validate_role_wire({**capture, "producer_native_id": f"sealed-fixture:{UUID2}"})
    with pytest.raises(HtError):
        validate_role_wire(
            {
                **capture,
                "producer_adapter_token": "codex-exec-fixed/1.0.0",
                "producer_version": "ht-sealed-zero-model/1.0.0",
            }
        )


def test_review_exact_action_target_fence_and_duplicate_slot_constraints() -> None:
    action = {
        "schema_version": "hypothesis-tree-runtime-role-action/1.0.0",
        "action_id": UUID2,
        "action_kind": "junior.return-unit-artifact",
        "target_ref": "tree#L4/dispatch#d-1-1",
        "payload": {
            "dispatch_ref": "tree#L4/dispatch#d-1-1",
            "artifact_outputs": [
                {"slot": "artifact", "relative_path": "one", "sha256": SHA, "bytes": 1},
                {"slot": "artifact", "relative_path": "two", "sha256": "1" * 64, "bytes": 2},
            ],
            "completion_status": "done",
        },
        "profile_id": "junior-v1",
        "role_packet": {"version": "v1", "sha256": SHA},
        "task_package_ref": "task-package#TP-1",
        "task_package_sha256": SHA,
        "verifier_fence_set_sha256": None,
        "outputs": [
            {"slot": "artifact", "relative_path": "one", "sha256": SHA, "bytes": 1},
            {"slot": "artifact", "relative_path": "two", "sha256": "1" * 64, "bytes": 2},
        ],
        "created_at": NOW,
    }
    validate_runtime("role-wire.schema.json", action)
    with pytest.raises(HtError, match="repeats an output slot"):
        validate_role_wire(action)

    valid_action = copy.deepcopy(action)
    valid_action["payload"]["artifact_outputs"] = [action["outputs"][0]]
    valid_action["outputs"] = [action["outputs"][0]]
    validate_role_wire(valid_action)

    bad_envelope_path = copy.deepcopy(valid_action)
    bad_envelope_path["outputs"][0]["relative_path"] = "../escape"
    with pytest.raises(HtError):
        validate_role_wire(bad_envelope_path)
    bad_payload_path = copy.deepcopy(valid_action)
    bad_payload_path["payload"]["artifact_outputs"][0]["relative_path"] = (
        "embedded\nline"
    )
    with pytest.raises(HtError):
        validate_role_wire(bad_payload_path)
    bad_backslash = copy.deepcopy(valid_action)
    bad_backslash["outputs"][0]["relative_path"] = "role-outputs\\artifact"
    bad_backslash["payload"]["artifact_outputs"][0]["relative_path"] = (
        "role-outputs\\artifact"
    )
    with pytest.raises(HtError):
        validate_role_wire(bad_backslash)

    mismatched_target = copy.deepcopy(valid_action)
    mismatched_target["target_ref"] = "tree#L4/dispatch#d-1-2"
    with pytest.raises(HtError, match="target differs"):
        validate_role_wire(mismatched_target)

    receipt = {
        "schema_version": "hypothesis-tree-action-receipt/1.0.0",
        "id": "AR-1",
        "action_id": UUID2,
        "action_kind": "pc.no-op",
        "target_ref": "issue#I-99",
        "payload": {"reason_code": "awaiting-user"},
        "profile_id": "principal-coordinator-v1",
        "role_packet": {"version": "v1", "sha256": SHA},
        "task_package_ref": "task-package#TP-1",
        "task_package_sha256": SHA,
        "verifier_fence_set_sha256": SHA,
        "runtime_id": UUID,
        "request_id": UUID,
        "binding_id": UUID2,
        "lease_epoch": 1,
        "session_id": UUID,
        "fence": "role:1",
        "packet_sha256": SHA,
        "source_action_sha256": SHA,
        "outputs": [],
        "created_at": NOW,
    }
    _assert_rejected("action_receipt", receipt)

    junior_receipt = {
        **receipt,
        "action_kind": "junior.return-unit-artifact",
        "target_ref": "tree#L4/dispatch#d-1-1",
        "profile_id": "junior-v1",
        "verifier_fence_set_sha256": None,
        "payload": {
            "dispatch_ref": "tree#L4/dispatch#d-1-1",
            "artifact_outputs": action["outputs"],
            "completion_status": "done",
        },
    }
    with pytest.raises(HtError, match="repeats a slot identity"):
        validate_repository(SCHEMAS, "action_receipt", junior_receipt)


def test_profile_registry_rebinds_packet_manifest_and_procedure_bytes(tmp_path: Path) -> None:
    profiles = load_runtime_profiles(ROOT, _allow_uncommitted=True)
    assert tuple(row["profile_id"] for row in profiles) == PROFILE_ORDER
    assert profiles[-1]["procedure"] is not None
    assert all(row["procedure"] is None for row in profiles[:-1])

    copied_root = tmp_path / "repository"
    copied_roles = copied_root / "system" / "roles"
    shutil.copytree(ROLES, copied_roles)
    director = copied_roles / "l4-director.v1.md"
    director.write_bytes(director.read_bytes() + b"drift\n")
    with pytest.raises(HtError, match="role packet bytes drifted"):
        load_runtime_profiles(copied_root, _allow_uncommitted=True)


def test_projection_registry_is_exact_order_and_rejects_unknown_policy_fields(
    tmp_path: Path,
) -> None:
    entries = load_reference_projections(ROOT, _allow_uncommitted=True)
    assert tuple(row["source_kind"] for row in entries) == PROJECTION_ORDER

    copied_root = tmp_path / "repository"
    copied_roles = copied_root / "system" / "roles"
    shutil.copytree(ROLES, copied_roles)
    registry = copied_roles / "REFERENCE-PROJECTIONS.json"
    value = json.loads(registry.read_text(encoding="utf-8"))
    value["entries"][0]["field_policy"]["future"] = True
    registry.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(HtError, match="unknown=.*future"):
        load_reference_projections(copied_root, _allow_uncommitted=True)

    # A separate-process-style reread also rejects duplicate keys rather than
    # accepting the last spelling.
    registry.write_text(
        '{"schema_version":"hypothesis-tree-reference-projection-registry/1.0.0",'
        '"schema_version":"foreign","renderer_token":"ht-reference-projection/1.0.0",'
        '"entries":[]}',
        encoding="utf-8",
    )
    with pytest.raises(HtError, match="duplicate JSON key"):
        load_reference_projections(copied_root, _allow_uncommitted=True)


def test_review_exact_registry_rows_paths_and_git_identity(tmp_path: Path) -> None:
    repository = tmp_path / "committed"
    shutil.copytree(ROLES, repository / "system" / "roles")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "B2 Test"], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "system/roles"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "roles"], check=True)
    assert len(load_runtime_profiles(repository)) == 6
    assert len(load_reference_projections(repository)) == len(PROJECTION_ORDER)

    profiles_path = repository / "system" / "roles" / "RUNTIME-PROFILES.json"
    profiles_path.write_bytes(profiles_path.read_bytes() + b" ")
    with pytest.raises(HtError, match="worktree/index/HEAD bytes differ"):
        load_runtime_profiles(repository)

    copied = tmp_path / "mutated"
    shutil.copytree(ROLES, copied / "system" / "roles")
    manifest_path = copied / "system" / "roles" / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["packets"][-1]["future_control"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(HtError, match="future_control"):
        load_runtime_profiles(copied, _allow_uncommitted=True)

    shutil.rmtree(copied)
    shutil.copytree(ROLES, copied / "system" / "roles")
    profiles_path = copied / "system" / "roles" / "RUNTIME-PROFILES.json"
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
    profiles["profiles"][1]["role_packet"]["ref"] = (
        "repository-file#system/roles/../roles/l4-director.v1.md"
    )
    profiles_path.write_text(json.dumps(profiles), encoding="utf-8")
    with pytest.raises(HtError, match="l4-director-v1 role-packet binding drift"):
        load_runtime_profiles(copied, _allow_uncommitted=True)

    symlinked = tmp_path / "symlinked"
    outside_roles = tmp_path / "outside-roles"
    shutil.copytree(ROLES, outside_roles)
    (symlinked / "system").mkdir(parents=True)
    (symlinked / "system" / "roles").symlink_to(outside_roles, target_is_directory=True)
    with pytest.raises(HtError, match="symlinked or non-directory ancestor"):
        load_runtime_profiles(symlinked, _allow_uncommitted=True)


def test_role_registry_ignores_ambient_alternate_git_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "committed"
    shutil.copytree(ROLES, repository / "system" / "roles")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "B2 Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "add", "system/roles"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "roles"], check=True)

    alternate_index = tmp_path / "alternate-index"
    alternate_env = {**os.environ, "GIT_INDEX_FILE": str(alternate_index)}
    subprocess.run(
        ["git", "-C", str(repository), "read-tree", "HEAD"],
        env=alternate_env,
        check=True,
    )
    profiles = repository / "system" / "roles" / "RUNTIME-PROFILES.json"
    profiles.write_bytes(profiles.read_bytes() + b" ")
    subprocess.run(["git", "-C", str(repository), "add", str(profiles)], check=True)
    profiles.write_bytes(subprocess.run(
        ["git", "-C", str(repository), "show", "HEAD:system/roles/RUNTIME-PROFILES.json"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout)

    monkeypatch.setenv("GIT_INDEX_FILE", str(alternate_index))
    with pytest.raises(HtError, match="worktree/index/HEAD bytes differ"):
        load_runtime_profiles(repository)


@pytest.mark.parametrize(
    ("target_name", "loader"),
    [
        ("RUNTIME-PROFILES.json", load_runtime_profiles),
        ("MANIFEST.json", load_runtime_profiles),
        ("l4-director.v1.md", load_runtime_profiles),
        ("verifier-procedure.v1.md", load_runtime_profiles),
        ("REFERENCE-PROJECTIONS.json", load_reference_projections),
    ],
)
def test_role_registry_rejects_ancestor_swap_during_each_loader_file_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    loader: object,
) -> None:
    repository = tmp_path / "repository"
    roles = repository / "system" / "roles"
    outside = tmp_path / "outside-roles"
    held = repository / "system" / "roles-held"
    shutil.copytree(ROLES, roles)
    shutil.copytree(ROLES, outside)

    real_open = os.open
    swapped = False

    def swap_before_leaf_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(path).name == target_name:
            swapped = True
            roles.rename(held)
            roles.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_leaf_open)
    with pytest.raises(HtError):
        loader(repository, _allow_uncommitted=True)  # type: ignore[operator]
    assert swapped


def test_review_exact_profile_and_procedure_bindings_cannot_be_rebound(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "packet-rebind"
    shutil.copytree(ROLES, repository / "system" / "roles")
    profiles_path = repository / "system" / "roles" / "RUNTIME-PROFILES.json"
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
    manifest = json.loads(
        (repository / "system" / "roles" / "MANIFEST.json").read_text(
            encoding="utf-8"
        )
    )
    senior_draft = next(
        row
        for row in manifest["packets"]
        if row["role"] == "senior" and row["version"] == "v0-draft"
    )
    profiles["profiles"][2]["role_packet"] = {
        "ref": f"repository-file#system/roles/{senior_draft['file']}",
        "version": senior_draft["version"],
        "sha256": senior_draft["sha256"],
    }
    profiles_path.write_text(json.dumps(profiles), encoding="utf-8")
    with pytest.raises(HtError, match="senior-v1 role-packet binding drift"):
        load_runtime_profiles(repository, _allow_uncommitted=True)

    shutil.rmtree(repository)
    shutil.copytree(ROLES, repository / "system" / "roles")
    profiles_path = repository / "system" / "roles" / "RUNTIME-PROFILES.json"
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
    senior = profiles["profiles"][2]["role_packet"]
    profiles["profiles"][-1]["procedure"] = dict(senior)
    profiles_path.write_text(json.dumps(profiles), encoding="utf-8")
    with pytest.raises(HtError, match="verifier procedure binding drift"):
        load_runtime_profiles(repository, _allow_uncommitted=True)


def test_review_exact_projection_policy_is_not_caller_selectable(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    shutil.copytree(ROLES, repository / "system" / "roles")
    path = repository / "system" / "roles" / "REFERENCE-PROJECTIONS.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["entries"][4]["field_policy"] = {
        "mode": "copy",
        "fields": ["package_kind"],
        "nested": {},
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(HtError, match="policy drift"):
        load_reference_projections(repository, _allow_uncommitted=True)


def test_review_exact_path_slot_timestamp_and_commissioning_semantics() -> None:
    with pytest.raises(HtUsageError, match="repository-file path"):
        parse_ref("repository-file#system/roles/l4-director.v1.md\x00suffix")
    with pytest.raises(HtUsageError):
        parse_ref(
            "tree#L4/unit-artifact#d-1-1@TP-1@" + "a" * 65
        )

    invalid_time = {
        "schema_version": "hypothesis-tree-subgoal/1.0.0",
        "id": "SG-1",
        "issue_ref": "I-1",
        "lane": "L4",
        "question": "Question",
        "done_definition": "Done",
        "created_at": "2026-02-30T00:00:00Z",
        "created_by_action_ref": "action-receipt#AR-1",
    }
    _assert_rejected("subgoal", invalid_time)

    producer = {
        "schema_version": "hypothesis-tree-producer-return/1.0.0",
        "outcome": "SUCCEEDED",
        "action": {
            "action_kind": "junior.return-unit-artifact",
            "target_ref": "tree#L4/dispatch#d-1-1",
            "payload": {
                "dispatch_ref": "tree#L4/dispatch#d-1-1",
                "completion_status": "done",
            },
            "outputs": [{"slot": "a" * 65, "content_utf8": "x\n"}],
        },
        "failure": None,
    }
    _assert_rejected("producer_return", producer)

    result = {
        "schema_version": "hypothesis-tree-wave-b2-commissioning-result/1.0.0",
        "status": "COMPLETE",
        "commissioning_id": "wave-b2-owned-role-v1",
        "instrumented_adoption": True,
        "fixture_version": "synthetic-b2/1.0.0",
        "historical_item9_result_sha256": SHA,
        "role_capability_sha256": SHA,
        "command_log_sha256": SHA,
        "runtime_inventory_sha256": SHA,
        "leak_closures_sha256": SHA,
        "session_chain_sha256": SHA,
        "authority_negative_cases_sha256": SHA,
        "adoption_proof_sha256": SHA,
        "normal_path_proof_sha256": SHA,
        "zero_content_scan": {"status": "pass", "files_scanned": 1, "matches": []},
        "closed_leaks": [f"L9-0{number}" for number in range(1, 7)],
        "preserved_leaks": ["L9-09", "L9-11"],
        "completed_at": NOW,
    }
    validate_repository(SCHEMAS, "wave_b2_commissioning_result", result)
    _assert_rejected(
        "wave_b2_commissioning_result",
        {**result, "fixture_version": "caller-chosen/9.9.9"},
    )

    entries = [
        {"relative_path": "evidence.txt", "mode": "0444", "sha256": "1" * 64, "bytes": 1},
        {"relative_path": "result.json", "mode": "0444", "sha256": SHA, "bytes": 1},
    ]
    entries_sha = hashlib.sha256(
        (json.dumps(entries, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    manifest = {
        "schema_version": "hypothesis-tree-wave-b2-commissioning-manifest/1.0.0",
        "commissioning_id": "wave-b2-owned-role-v1",
        "result_sha256": SHA,
        "entries": entries,
        "entries_sha256": entries_sha,
        "created_at": NOW,
    }
    validate_repository(SCHEMAS, "wave_b2_commissioning_manifest", manifest)
    with pytest.raises(HtError, match="UTF-8 lexical"):
        validate_repository(
            SCHEMAS,
            "wave_b2_commissioning_manifest",
            {**manifest, "entries": list(reversed(entries))},
        )
    with pytest.raises(HtError, match="entries_sha256 mismatch"):
        validate_repository(
            SCHEMAS,
            "wave_b2_commissioning_manifest",
            {**manifest, "entries_sha256": "2" * 64},
        )
    duplicate_entries = [entries[0], entries[0], entries[1]]
    duplicate_sha = hashlib.sha256(
        (json.dumps(duplicate_entries, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    with pytest.raises(HtError):
        validate_repository(
            SCHEMAS,
            "wave_b2_commissioning_manifest",
            {**manifest, "entries": duplicate_entries, "entries_sha256": duplicate_sha},
        )
    wrong_mode_entries = copy.deepcopy(entries)
    wrong_mode_entries[0]["mode"] = "0600"
    wrong_mode_sha = hashlib.sha256(
        (json.dumps(wrong_mode_entries, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    with pytest.raises(HtError, match="wrong frozen mode"):
        validate_repository(
            SCHEMAS,
            "wave_b2_commissioning_manifest",
            {**manifest, "entries": wrong_mode_entries, "entries_sha256": wrong_mode_sha},
        )


def test_l4_packet_is_manifest_bound() -> None:
    manifest = json.loads((ROLES / "MANIFEST.json").read_text(encoding="utf-8"))
    row = next(
        item
        for item in manifest["packets"]
        if item["role"] == "l4-director" and item["version"] == "v1"
    )
    packet = (ROLES / row["file"]).read_bytes()
    assert row["bytes"] == len(packet) == 6998
    assert row["sha256"] == hashlib.sha256(packet).hexdigest()
    text = packet.decode("utf-8")
    assert [line.removeprefix("## ") for line in text.splitlines() if line.startswith("## ")] == [
        "1. Role & purpose",
        "2. Position",
        "3. The work",
        "4. Boundaries with rationale",
        "5. Calibration",
    ]

def test_b2_path_and_reference_parsers_are_additive_without_premature_resolution() -> None:
    root = Root(Path("/research"))
    assert root.subgoal_json("SG-3") == Path("/research/tier1/subgoals/SG-3.json")
    assert root.task_package_json("TP-4") == Path("/research/tier1/task-packages/TP-4.json")
    assert root.inbox_delivery_json("IB-5") == Path("/research/tier1/inbox/deliveries/IB-5.json")
    assert root.inbox_receipt_json("IB-5") == Path("/research/tier1/inbox/receipts/IB-5.json")
    assert root.action_receipt_json("AR-6") == Path("/research/tier1/action-receipts/AR-6.json")

    expected = {
        "subgoal#SG-1": ("subgoal", "SG-1"),
        "task-package#TP-2": ("task-package", "TP-2"),
        "inbox-delivery#IB-3": ("inbox-delivery", "IB-3"),
        "inbox-receipt#IB-3": ("inbox-receipt", "IB-3"),
        "action-receipt#AR-4": ("action-receipt", "AR-4"),
        "pc#principal-coordinator": ("pc", "principal-coordinator"),
        "issue-queue#current": ("issue-queue", "current"),
        "ratification-item#RQ-7": ("ratification-item", "RQ-7"),
        "repository-file#system/roles/l4-director.v1.md": (
            "repository-file",
            "system/roles/l4-director.v1.md",
        ),
        "tree#L4/plan#d-1-1": ("plan", "d-1-1"),
        "tree#L4/unit-artifact#d-1-1@TP-2@artifact": (
            "unit-artifact",
            "d-1-1@TP-2@artifact",
        ),
        "tree#L4/qa#d-1-1@TP-3@qa": ("qa", "d-1-1@TP-3@qa"),
        "tree#L4/archive#d-1-1": ("archive", "d-1-1"),
        "tree#L4/qa-set#d-1-1": ("qa-set", "d-1-1"),
    }
    for value, (kind, object_id) in expected.items():
        parsed = parse_ref(value)
        assert (parsed.kind, parsed.object_id, parsed.canonical) == (
            kind,
            object_id,
            value,
        )
    with pytest.raises(HtUsageError, match="repository-file path"):
        parse_ref("repository-file#system/../secret")
    with pytest.raises(HtUsageError, match="stable repository resolver is not installed"):
        resolve_ref(root, "subgoal#SG-1")
    assert resolve_ref(root, "pc#principal-coordinator").path is None


def test_b1_checkpoint_schema_and_runtime_validator_sources_are_unchanged() -> None:
    # R2 intentionally extends state.py; its fixed-runtime golden checkpoint
    # test preserves the accepted B1 output bytes without freezing implementation.
    frozen = {
        "system/ht/runtime/schemas/checkpoint.schema.json": "69a8f7e363092ad01c894201a53cda14e6936d64f8dbd449aa01d3080f648e7a",
        "system/ht/runtime/schema.py": "b489f33c59768faa7e44586ffd1d86814c9547c83d336a9a44a9132c00b052ba",
    }
    for relative, digest in frozen.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
