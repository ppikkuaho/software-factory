"""Public command layer for the independent B1 runtime."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import time
from typing import Any
from uuid import UUID, uuid4

from ht.errors import HtError, HtUsageError
from ht.paths import Root
from ht.runtime import BUILD_ID, RUNTIME_KIND, SCHEMA_VERSION, runtime_root
from ht.runtime.atomic import (
    DIRECTORY_MODE,
    FILE_MODE,
    fsync_directory,
    make_directory,
    publish_immutable,
    read_exact_file,
    recover_immutable_publication,
    require_directory,
)
from ht.runtime.schema import canonical_json_bytes, strict_loads, validate
from ht.runtime.custody import audit_lock
from ht.runtime.capability import CAPABILITY_FILE, INITIALIZATION_FILE
from ht.runtime.gate import (
    inspect_runtime_gate,
    require_b1_submission_allowed,
)
from ht.runtime.launcher import READINESS_LIMIT, child_options, daemon_argv
from ht.runtime.repository import WorkSnapshot, revalidate_work, snapshot_work
from ht.runtime.state import (
    binding_ledger_bytes,
    genesis_bindings,
    genesis_checkpoint,
)
from ht.runtime.views import packet as packet_view
from ht.runtime.views import control_response as control_response_view
from ht.runtime.views import read_state_unlocked, response as response_view
from ht.runtime.views import status as status_view
from ht.runtime.views import wait as wait_view


_MARKER = ".ht-runtime.genesis.json"
_DIRECTORY_ORDER = (
    "requests",
    "responses",
    "control",
    "control/requests",
    "control/responses",
    "sessions",
)
_FILE_ORDER = (
    ".harnessd.lock",
    ".ht-runtime.instance.lock",
    "run-ledger.jsonl",
    "binding-ledger.json",
    "checkpoint.json",
    "runtime.json",
)
_ORDER = _DIRECTORY_ORDER + _FILE_ORDER


def _created_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _descriptor(root: Root, runtime_id: str, created_at: str) -> dict[str, Any]:
    estate = runtime_root(root.path).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "runtime_kind": RUNTIME_KIND,
        "build_id": BUILD_ID,
        "runtime_id": runtime_id,
        "runtime_root": str(estate),
        "repository_root": str(root.path.resolve()),
        "created_at": created_at,
    }


def _expected_files(descriptor: dict[str, Any]) -> dict[str, bytes]:
    runtime_id = descriptor["runtime_id"]
    bindings = genesis_bindings(runtime_id)
    return {
        ".harnessd.lock": b"",
        ".ht-runtime.instance.lock": b"",
        "run-ledger.jsonl": b"",
        "binding-ledger.json": binding_ledger_bytes(bindings),
        "checkpoint.json": canonical_json_bytes(genesis_checkpoint(runtime_id)),
        "runtime.json": canonical_json_bytes(descriptor),
    }


def _marker_object(descriptor: dict[str, Any]) -> dict[str, Any]:
    files = _expected_files(descriptor)
    return {
        "schema_version": "hypothesis-tree-runtime-genesis-marker/1.0.0",
        "descriptor": descriptor,
        "expected_files_base64": {
            name: base64.b64encode(files[name]).decode("ascii") for name in _FILE_ORDER
        },
    }


def _decode_marker(value: Any, root: Root) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "descriptor",
        "expected_files_base64",
    }:
        raise HtError("runtime genesis marker has an unknown shape (B1 §4)")
    if value["schema_version"] != "hypothesis-tree-runtime-genesis-marker/1.0.0":
        raise HtError("runtime genesis marker has an unknown version (B1 §4)")
    descriptor = value["descriptor"]
    validate("descriptor.schema.json", descriptor)
    expected_descriptor = _descriptor(
        root,
        descriptor["runtime_id"],
        descriptor["created_at"],
    )
    if descriptor != expected_descriptor:
        raise HtError("runtime genesis marker names a different repository/runtime (B1 §4)")
    encoded = value["expected_files_base64"]
    if not isinstance(encoded, dict) or set(encoded) != set(_FILE_ORDER):
        raise HtError("runtime genesis marker inventory differs from the build (B1 §4)")
    if any(not isinstance(encoded[name], str) for name in _FILE_ORDER):
        raise HtError("runtime genesis marker inventory is not canonical base64 text (B1 §4)")
    try:
        files = {
            name: base64.b64decode(encoded[name], validate=True) for name in _FILE_ORDER
        }
    except (ValueError, TypeError) as exc:
        raise HtError("runtime genesis marker contains invalid base64 (B1 §4)") from exc
    if files != _expected_files(descriptor):
        raise HtError("runtime genesis marker expected bytes are inconsistent (B1 §4)")
    return descriptor, files


def _entries(estate: Path) -> set[str]:
    return {
        path.relative_to(estate).as_posix()
        for path in estate.rglob("*")
        if not path.name.startswith(".ht-publish-")
        and not path.name.startswith(".ht-replace-")
        and path.name != _MARKER
    }


def _validate_prefix(estate: Path) -> None:
    actual = _entries(estate)
    prefixes = {frozenset(_ORDER[:index]) for index in range(len(_ORDER) + 1)}
    if frozenset(actual) not in prefixes:
        raise HtError(
            f"runtime genesis inventory is not an exact creation prefix: {sorted(actual)} (B1 §4)"
        )


def _validate_complete(estate: Path, descriptor: dict[str, Any]) -> None:
    require_directory(estate)
    expected_files = _expected_files(descriptor)
    interrupted = sorted(
        path.relative_to(estate).as_posix()
        for path in estate.rglob("*")
        if path.name.startswith(".ht-publish-") or path.name.startswith(".ht-replace-")
    )
    if interrupted:
        raise HtError(
            f"unowned interrupted runtime publications remain: {interrupted} (B1 §4/§6)"
        )
    actual = _entries(estate)
    if actual != set(_ORDER):
        raise HtError(
            f"runtime inventory differs from exact genesis: {sorted(actual)} (B1 §4)"
        )
    for relative in _DIRECTORY_ORDER:
        require_directory(estate / relative)
    for relative, expected in expected_files.items():
        if read_exact_file(estate / relative) != expected:
            raise HtError(f"runtime genesis file {relative} has conflicting bytes (B1 §4)")
    validate("descriptor.schema.json", descriptor)
    validate(
        "checkpoint.schema.json",
        strict_loads(read_exact_file(estate / "checkpoint.json"), label="checkpoint.json"),
    )


def _recover_marker_temp(estate: Path) -> None:
    marker = estate / _MARKER
    if marker.exists() or marker.is_symlink():
        marker_data = read_exact_file(marker, require_single_link=False)
        recover_immutable_publication(marker, marker_data)
    else:
        # An unlinked publication temp has no commit meaning, even if its
        # bytes happen to form a complete marker. Discard and mint genesis
        # again; a hard-linked commit always has the visible final above.
        recover_immutable_publication(marker, b"")


def _load_existing_descriptor(root: Root, estate: Path) -> dict[str, Any]:
    descriptor = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime.json")
    validate("descriptor.schema.json", descriptor)
    if descriptor != _descriptor(
        root,
        descriptor["runtime_id"],
        descriptor["created_at"],
    ):
        raise HtError("runtime descriptor names a different repository/runtime (B1 §4)")
    return descriptor


def init(root: Root, *, as_json: bool) -> int:
    """Create or idempotently verify the exact runtime genesis estate."""

    estate = runtime_root(root.path)
    parent = estate.parent
    if parent.exists() or parent.is_symlink():
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise HtError("runtime parent var/ must be a non-symlink directory (B1 §3)")
    else:
        os.mkdir(parent, DIRECTORY_MODE)
        os.chmod(parent, DIRECTORY_MODE, follow_symlinks=False)
        fsync_directory(parent.parent)
    if not estate.exists() and not estate.is_symlink():
        os.mkdir(estate, DIRECTORY_MODE)
        os.chmod(estate, DIRECTORY_MODE, follow_symlinks=False)
        fsync_directory(estate.parent)
    require_directory(estate)
    # The role state machine owns admission once either commit marker is
    # visible.  Gate before B1 genesis-temp recovery so an interrupted role
    # prefix makes this ordinary runtime command byte-for-byte read-only.
    if any(
        (estate / name).exists() or (estate / name).is_symlink()
        for name in (INITIALIZATION_FILE, CAPABILITY_FILE)
    ):
        descriptor = _load_existing_descriptor(root, estate)
        gate = inspect_runtime_gate(estate, descriptor["runtime_id"])
        if gate.upgrade is not None:
            with audit_lock(estate / ".harnessd.lock", exclusive=False):
                _verified_estate, verified_descriptor, _state = read_state_unlocked(
                    root.path
                )
            if verified_descriptor != descriptor:  # pragma: no cover
                raise HtError("runtime descriptor changed during role-aware init (B2 §3.1)")
            return _emit(
                {
                    "status": "ready",
                    "runtime_id": descriptor["runtime_id"],
                    "runtime_root": descriptor["runtime_root"],
                    "created": False,
                },
                as_json=as_json,
            )
    _recover_marker_temp(estate)
    marker_path = estate / _MARKER
    descriptor_path = estate / "runtime.json"

    if marker_path.exists() or marker_path.is_symlink():
        marker_bytes = read_exact_file(marker_path)
        marker_value = strict_loads(marker_bytes, label="genesis marker")
        descriptor, expected_files = _decode_marker(marker_value, root)
    elif descriptor_path.exists() or descriptor_path.is_symlink():
        descriptor = _load_existing_descriptor(root, estate)
        _validate_complete(estate, descriptor)
        return _emit(
            {
                "status": "ready",
                "runtime_id": descriptor["runtime_id"],
                "runtime_root": descriptor["runtime_root"],
                "created": False,
            },
            as_json=as_json,
        )
    else:
        _validate_prefix(estate)
        if _entries(estate):
            raise HtError("partial runtime genesis has no surviving marker (B1 §4)")
        descriptor = _descriptor(root, str(uuid4()), _created_at())
        expected_files = _expected_files(descriptor)
        publish_immutable(marker_path, canonical_json_bytes(_marker_object(descriptor)))

    _validate_prefix(estate)
    for relative in _DIRECTORY_ORDER:
        make_directory(estate / relative)
    for relative in _FILE_ORDER:
        recover_immutable_publication(estate / relative, expected_files[relative])
        publish_immutable(estate / relative, expected_files[relative])
    _validate_complete(estate, descriptor)
    marker_path.unlink()
    fsync_directory(estate)
    _validate_complete(estate, descriptor)
    return _emit(
        {
            "status": "ready",
            "runtime_id": descriptor["runtime_id"],
            "runtime_root": descriptor["runtime_root"],
            "created": True,
        },
        as_json=as_json,
    )


def _emit(value: dict[str, Any], *, as_json: bool) -> int:
    if as_json:
        sys.stdout.buffer.write(canonical_json_bytes(value))
    else:
        print(value.get("status", "ok"))
    return 0


def _new_request(
    work: WorkSnapshot,
    *,
    attempt: int = 1,
    lineage: list[str] | None = None,
) -> tuple[dict[str, Any], bytes]:
    value = {
        "schema_version": "hypothesis-tree-runtime-request/1.0.0",
        "request_id": str(uuid4()),
        "request_created_at": _created_at(),
        "role": "synthetic-kernel-v1",
        "attempt": attempt,
        "retry_lineage": lineage or [],
        "work": work.as_dict(),
    }
    validate("request.schema.json", value)
    return value, canonical_json_bytes(value)


def _same_retry_work_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    identity_fields = (
        "type",
        "canonical_ref",
        "repository_relpath",
        "canonical_object_sha256",
        "raw_file_sha256",
        "head_blob_oid",
        "git_object_format",
    )
    return all(left.get(name) == right.get(name) for name in identity_fields)


def request(root: Root, work_ref: str, *, as_json: bool) -> int:
    # This first proof supplies a frozen comparison target only.  No request
    # identity or canonical request bytes exist until the final proof below.
    snapshot = snapshot_work(root, work_ref)
    estate = runtime_root(root.path)
    with audit_lock(estate / ".harnessd.lock", exclusive=True):
        _estate, _descriptor, state = read_state_unlocked(root.path)
        require_b1_submission_allowed(state)
        current = revalidate_work(root, snapshot)
        value, data = _new_request(current)
        publish_immutable(estate / "requests" / f"{value['request_id']}.json", data)
    return _emit(
        {
            "status": "submitted",
            "request_id": value["request_id"],
            "request_sha256": hashlib.sha256(data).hexdigest(),
        },
        as_json=as_json,
    )


def retry(root: Root, request_id: str, *, as_json: bool) -> int:
    try:
        canonical = str(UUID(request_id))
    except (ValueError, AttributeError) as exc:
        raise HtError("runtime retry request ID must be a canonical UUID (B1 §5/§7)") from exc
    if canonical != request_id:
        raise HtError("runtime retry request ID must be a canonical UUID (B1 §5/§7)")
    estate = runtime_root(root.path)
    with audit_lock(estate / ".harnessd.lock", exclusive=True):
        _estate, _descriptor, state = read_state_unlocked(root.path)
        require_b1_submission_allowed(state)
        entry = state.request_index.get(request_id)
        if not isinstance(entry, dict) or entry.get("status") != "accepted":
            raise HtError("runtime retry requires an accepted non-duplicate request (B1 §7)")
        binding = state.bindings.get(entry["node_address"])
        if not isinstance(binding, dict) or binding.get("terminal_outcome") not in {
            "FAILED",
            "crashed",
        }:
            raise HtError("runtime retry requires terminal FAILED or crashed work (B1 §7)")
        lineage = binding["retry_lineage"]
        if binding["attempt"] != len(lineage) + 1:
            raise HtError("runtime retry lineage/attempt differs from complete chain (B1 §7)")
        for position, predecessor_id in enumerate(lineage):
            predecessor_entry = state.request_index.get(predecessor_id)
            predecessor = (
                state.bindings.get(predecessor_entry["node_address"])
                if isinstance(predecessor_entry, dict)
                and predecessor_entry.get("status") == "accepted"
                else None
            )
            if (
                not isinstance(predecessor, dict)
                or predecessor.get("terminal_outcome") not in {"FAILED", "crashed"}
                or predecessor.get("role") != binding["role"]
                or not _same_retry_work_identity(predecessor.get("work", {}), binding["work"])
                or predecessor.get("attempt") != position + 1
                or predecessor.get("retry_lineage") != lineage[:position]
            ):
                raise HtError("runtime retry lineage is not a complete ordered chain (B1 §7)")
        expected = WorkSnapshot(**binding["work"])
        current = revalidate_work(root, expected)
        next_lineage = [*lineage, request_id]
        value, data = _new_request(
            current,
            attempt=binding["attempt"] + 1,
            lineage=next_lineage,
        )
        publish_immutable(estate / "requests" / f"{value['request_id']}.json", data)
    return _emit(
        {
            "status": "submitted",
            "request_id": value["request_id"],
            "request_sha256": hashlib.sha256(data).hexdigest(),
        },
        as_json=as_json,
    )


def _read_readiness(fd: int, *, timeout: float) -> tuple[dict[str, Any], int]:
    deadline = time.monotonic() + timeout
    data = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
                    "status": "failed",
                    "reason_code": "daemon-readiness-timeout",
                }, 4
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, READINESS_LIMIT + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > READINESS_LIMIT:
                raise HtError("runtime daemon readiness exceeds 4096 bytes (B1 addendum §3)")
    finally:
        os.close(fd)
    value = strict_loads(bytes(data), label="daemon readiness")
    if not isinstance(value, dict):
        raise HtError("runtime daemon readiness is not an object (B1 addendum §3)")
    common = {"schema_version", "status"}
    status = value.get("status")
    if value.get("schema_version") != "hypothesis-tree-runtime-readiness/1.0.0":
        raise HtError("runtime daemon readiness version differs (B1 addendum §3)")
    if status == "ready":
        if set(value) != common | {"daemon_incarnation_id", "pid"}:
            raise HtError("runtime ready response has unknown fields (B1 addendum §3)")
        UUID(value["daemon_incarnation_id"])
        if isinstance(value["pid"], bool) or not isinstance(value["pid"], int) or value["pid"] <= 0:
            raise HtError("runtime ready response has invalid PID (B1 addendum §3)")
        return value, 0
    if status == "already-running" and set(value) == common:
        return value, 0
    if status == "failed" and set(value) == common | {"reason_code"}:
        return value, 2
    raise HtError("runtime daemon readiness has unknown shape (B1 addendum §3)")


def start(root: Root, *, background: bool, as_json: bool) -> int:
    if not background:
        raise HtUsageError("runtime start requires --background (B1 §15)")
    estate = runtime_root(root.path)
    require_directory(estate)
    read_fd, write_fd = os.pipe()
    try:
        subprocess.Popen(
            daemon_argv(root.path, write_fd),
            **child_options(root.path, (write_fd,)),
        )
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise HtError(f"runtime daemon launch failed: {exc} (B1 §15)") from exc
    os.close(write_fd)
    value, code = _read_readiness(read_fd, timeout=10.0)
    _emit(value, as_json=as_json)
    return code


def stop(root: Root, *, as_json: bool) -> int:
    estate = runtime_root(root.path)
    with audit_lock(estate / ".harnessd.lock", exclusive=True):
        _estate, _descriptor, state = read_state_unlocked(root.path)
        target = state.bindings["runtime#kernel"]["daemon_incarnation_id"]
        if target is None:
            raise HtError("runtime daemon is not running (B1 §15)")
        control_id = str(uuid4())
        value = {
            "schema_version": "hypothesis-tree-runtime-control-request/1.0.0",
            "control_id": control_id,
            "control_created_at": _created_at(),
            "target_daemon_incarnation_id": target,
            "operation": "stop",
        }
        validate("control-request.schema.json", value)
        publish_immutable(
            estate / "control" / "requests" / f"{control_id}.json",
            canonical_json_bytes(value),
        )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        response = control_response_view(root.path, control_id)
        if response is not None:
            return _emit(response, as_json=as_json)
        time.sleep(0.02)
    return _emit(
        {
            "schema_version": "hypothesis-tree-runtime-control-submitted/1.0.0",
            "status": "submitted",
            "control_id": control_id,
            "target_daemon_incarnation_id": target,
        },
        as_json=as_json,
    )


def status(root: Root, *, as_json: bool) -> int:
    return _emit(status_view(root.path), as_json=as_json)


def response_show(root: Root, request_id: str, *, as_json: bool) -> int:
    return _emit(response_view(root.path, request_id), as_json=as_json)


def packet_show(root: Root, session_id: str, *, as_json: bool) -> int:
    return _emit(packet_view(root.path, session_id), as_json=as_json)


def wait(root: Root, request_id: str, timeout: str, *, as_json: bool) -> int:
    try:
        seconds = float(timeout)
    except ValueError as exc:
        raise HtUsageError("runtime wait timeout must be a number (B1 §15)") from exc
    if not (seconds >= 0 and seconds < float("inf")):
        raise HtUsageError("runtime wait timeout must be finite and nonnegative (B1 §15)")
    value, code = wait_view(root.path, request_id, seconds)
    _emit(value, as_json=as_json)
    return code
