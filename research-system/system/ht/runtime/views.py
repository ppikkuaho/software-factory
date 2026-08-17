"""Shared-lock, full-replay public read surfaces for the B1 runtime."""

from __future__ import annotations

import hashlib
from pathlib import Path
import time
from typing import Any, Callable, TypeVar
from uuid import UUID

from ht.errors import HtError

from .atomic import read_exact_file, require_directory
from .custody import audit_lock
from .gate import inspect_runtime_gate
from .inventory import validate_runtime_inventory
from .replay import ReplayState, require_current_projections
from .schema import strict_loads, validate
from .wal import parse_bytes


T = TypeVar("T")


def canonical_uuid(value: str, label: str) -> str:
    try:
        parsed = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise HtError(f"{label} must be a canonical UUID (B1 §5)") from exc
    if parsed != value:
        raise HtError(f"{label} must be a canonical UUID (B1 §5)")
    return value


def _descriptor(estate: Path, root: Path) -> dict[str, Any]:
    value = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime.json")
    validate("descriptor.schema.json", value)
    if (
        value["runtime_root"] != str(estate.resolve())
        or value["repository_root"] != str(root.resolve())
    ):
        raise HtError("runtime descriptor path identity differs from repository (B1 §4)")
    return value


def read_state_unlocked(root: Path) -> tuple[Path, dict[str, Any], ReplayState]:
    """Full replay and exact inventory proof for a caller holding the audit lock."""

    root = root.resolve()
    estate = root / "var" / "runtime"
    descriptor = _descriptor(estate, root)
    gate = inspect_runtime_gate(estate, descriptor["runtime_id"])
    validate_runtime_inventory(
        estate,
        capability_state=gate.capability,
        runtime_id=descriptor["runtime_id"],
    )
    state = require_current_projections(
        estate,
        parse_bytes(read_exact_file(estate / "run-ledger.jsonl")),
        descriptor["runtime_id"],
        upgrade=gate.upgrade,
    )
    validate_runtime_inventory(
        estate,
        state,
        capability_state=gate.capability,
    )
    return estate, descriptor, state


def _under_lock(root: Path, operation: Callable[[Path, dict[str, Any], ReplayState], T]) -> T:
    root = root.resolve()
    estate = root / "var" / "runtime"
    require_directory(estate)
    with audit_lock(estate / ".harnessd.lock", exclusive=False):
        estate, descriptor, state = read_state_unlocked(root)
        return operation(estate, descriptor, state)


def read_state(root: Path) -> ReplayState:
    return _under_lock(root, lambda _estate, _descriptor, state: state)


def status(root: Path) -> dict[str, Any]:
    def build(_estate: Path, descriptor: dict[str, Any], state: ReplayState) -> dict[str, Any]:
        incarnation = state.bindings["runtime#kernel"]["daemon_incarnation_id"]
        daemon: dict[str, Any] = {"status": "stopped"}
        if incarnation is not None:
            daemon = {"status": "running", "daemon_incarnation_id": incarnation}
        return {
            "schema_version": "hypothesis-tree-runtime-status/1.0.0",
            "runtime_id": descriptor["runtime_id"],
            "last_seq": state.last_seq,
            "daemon": daemon,
            "final_tail": state.final_tail,
            "request_index": state.request_index,
            "dedup_index": state.dedup_index,
            "session_index": state.session_index,
            "control_index": state.control_index,
            "bindings": state.bindings,
        }

    return _under_lock(root, build)


def response(root: Path, request_id: str) -> dict[str, Any]:
    canonical_uuid(request_id, "request ID")

    def load(estate: Path, _descriptor: dict[str, Any], state: ReplayState) -> dict[str, Any]:
        entry = state.request_index.get(request_id)
        if not isinstance(entry, dict) or entry.get("status") == "planned":
            raise HtError("runtime request has no settled admission response (B1 §8)")
        path = estate / "responses" / f"{request_id}.json"
        value = strict_loads(read_exact_file(path), label=path.name)
        validate("admission-response.schema.json", value)
        if value.get("request_id") != request_id or value.get("status") != entry["status"]:
            raise HtError("admission response differs from replay truth (B1 §8)")
        for name in (
            "binding_id",
            "node_address",
            "lease_epoch",
            "session_id",
            "packet_sha256",
            "original_request_id",
            "reason_code",
            "recovery_created",
        ):
            if name in value and value[name] != entry[name]:
                raise HtError("admission response fields differ from replay truth (B1 §8)")
        return value

    return _under_lock(root, load)


def control_response(root: Path, control_id: str) -> dict[str, Any] | None:
    canonical_uuid(control_id, "control ID")

    def load(estate: Path, _descriptor: dict[str, Any], state: ReplayState) -> dict[str, Any] | None:
        entry = state.control_index.get(control_id)
        if not isinstance(entry, dict):
            return None
        path = estate / "control" / "responses" / f"{control_id}.json"
        if not path.exists() and not path.is_symlink():
            return None
        value = strict_loads(read_exact_file(path), label=path.name)
        validate("control-response.schema.json", value)
        expected = {
            "schema_version": "hypothesis-tree-runtime-control-response/1.0.0",
            "status": entry["status"],
            "control_id": control_id,
            "target_daemon_incarnation_id": entry["target_daemon_incarnation_id"],
        }
        if entry["status"] == "rejected":
            expected["reason_code"] = entry["reason_code"]
        if value != expected:
            raise HtError("control response differs from replay truth (B1 §15)")
        return value

    return _under_lock(root, load)


_ARTIFACT_SCHEMAS = {
    "started": "started-receipt.schema.json",
    "ready": "ready-receipt.schema.json",
    "result": "result.schema.json",
    "terminal": "terminal-receipt.schema.json",
    "process-exit": "process-exit-receipt.schema.json",
}


def packet(root: Path, session_id: str) -> dict[str, Any]:
    canonical_uuid(session_id, "session ID")

    def load(estate: Path, descriptor: dict[str, Any], state: ReplayState) -> dict[str, Any]:
        session_entry = state.session_index.get(session_id)
        if not isinstance(session_entry, dict):
            raise HtError("runtime session is absent from replay (B1 §15)")
        address = session_entry["node_address"]
        work = state.bindings.get(address)
        if not isinstance(work, dict):
            raise HtError("runtime session has no persistent binding (B1 §15)")
        durable = work["sessions"].get(session_id)
        if not isinstance(durable, dict):
            raise HtError("runtime session differs from persistent binding (B1 §15)")
        session_path = estate / "sessions" / session_id
        require_directory(session_path)
        packet_path = session_path / "packet.json"
        packet_bytes = read_exact_file(packet_path)
        packet_value = strict_loads(packet_bytes, label="packet.json")
        validate("session-packet.schema.json", packet_value)
        packet_sha = hashlib.sha256(packet_bytes).hexdigest()
        launch_bytes = read_exact_file(session_path / "launch.json")
        launch = strict_loads(launch_bytes, label="launch.json")
        validate("launch.schema.json", launch)
        common = {
            "runtime_id": descriptor["runtime_id"],
            "request_id": work["request_id"],
            "binding_id": work["binding_id"],
            "lease_epoch": durable["lease_epoch"],
            "session_id": session_id,
            "packet_sha256": durable["packet_sha256"],
        }
        if (
            packet_value != durable["packet"]
            or packet_bytes.decode("utf-8") != durable["packet_canonical_json"]
            or packet_sha != durable["packet_sha256"]
            or launch != durable["launch"]
            or hashlib.sha256(launch_bytes).hexdigest() != durable["launch_sha256"]
            or any(packet_value.get(key) != value for key, value in common.items() if key != "packet_sha256")
            or launch["packet_sha256"] != packet_sha
        ):
            raise HtError("stored packet/launch differs from durable claim truth (B1 §12/§15)")
        validated = ["launch"]
        observed_hashes: dict[str, str] = {}
        for name, schema_name in _ARTIFACT_SCHEMAS.items():
            path = session_path / f"{name}.json"
            if not path.exists() and not path.is_symlink():
                continue
            data = read_exact_file(path)
            value = strict_loads(data, label=path.name)
            validate(schema_name, value)
            if any(value.get(key) != expected for key, expected in common.items()):
                raise HtError(f"{name} receipt differs from session fence (B1 §12/§13)")
            if name in {"started", "process-exit"} and (
                value["wrapper_instance_id"] != durable["wrapper_instance_id"]
                or value["helper_instance_id"] != durable["helper_instance_id"]
            ):
                raise HtError(f"{name} process identity differs from claim (B1 §13)")
            if name in {"ready", "result", "terminal"} and value["helper_instance_id"] != durable["helper_instance_id"]:
                raise HtError(f"{name} helper identity differs from claim (B1 §13)")
            observed_hashes[name] = hashlib.sha256(data).hexdigest()
            validated.append(name)
        for field, name in (
            ("started_sha256", "started"),
            ("ready_sha256", "ready"),
            ("result_sha256", "result"),
            ("terminal_sha256", "terminal"),
            ("process_exit_sha256", "process-exit"),
        ):
            if durable[field] is not None and observed_hashes.get(name) != durable[field]:
                raise HtError(f"{name} receipt hash differs from replay truth (B1 §15)")
        response_entry = state.request_index.get(work["request_id"])
        if isinstance(response_entry, dict) and response_entry.get("session_id") == session_id:
            if response_entry.get("packet_sha256") != packet_sha:
                raise HtError("accepted session response differs from stored packet (B1 §15)")
        elif durable["lifecycle"] != "abandoned":
            raise HtError("non-abandoned session lacks its accepted response pointer (B1 §15)")
        return {
            "schema_version": "hypothesis-tree-runtime-packet-audit-envelope/1.0.0",
            "packet": packet_value,
            "packet_relative_path": f"sessions/{session_id}/packet.json",
            "packet_sha256": packet_sha,
            "lifecycle": durable["lifecycle"],
            "outcome": durable["outcome"],
            "validated_artifacts": validated,
        }

    value = _under_lock(root, load)
    validate("packet-audit-envelope.schema.json", value)
    return value


def wait(root: Path, request_id: str, timeout: float) -> tuple[dict[str, Any], int]:
    canonical_uuid(request_id, "request ID")
    if timeout < 0:
        raise HtError("runtime wait timeout must be nonnegative (B1 §15)")
    deadline = time.monotonic() + timeout
    while True:
        def inspect(_estate: Path, _descriptor: dict[str, Any], state: ReplayState) -> dict[str, Any] | None:
            entry = state.request_index.get(request_id)
            if not isinstance(entry, dict):
                return None
            if entry["status"] == "rejected":
                return {
                    "schema_version": "hypothesis-tree-runtime-wait/1.0.0",
                    "status": "rejected",
                    "request_id": request_id,
                    "reason_code": entry["reason_code"],
                }
            resolved = entry
            if entry["status"] == "duplicate":
                original = state.request_index.get(entry["original_request_id"])
                if not isinstance(original, dict):
                    raise HtError("duplicate request lacks original replay entry (B1 §15)")
                resolved = original
            if resolved["status"] == "accepted":
                session = state.session_index.get(resolved["session_id"])
                if isinstance(session, dict) and session["lifecycle"] == "terminal":
                    return {
                        "schema_version": "hypothesis-tree-runtime-wait/1.0.0",
                        "status": "terminal",
                        "request_id": request_id,
                        "resolved_request_id": resolved["request_id"],
                        "admission_status": entry["status"],
                        "session_id": resolved["session_id"],
                        "outcome": session["outcome"],
                    }
            return None

        settled = _under_lock(root, inspect)
        if settled is not None:
            return settled, 0
        if time.monotonic() >= deadline:
            return {
                "schema_version": "hypothesis-tree-runtime-wait/1.0.0",
                "status": "timeout",
                "request_id": request_id,
                "reason_code": "wait-timeout",
            }, 4
        time.sleep(0.02)
