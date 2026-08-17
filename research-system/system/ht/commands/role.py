"""Sealed B1-to-B2 role-capability activation and crash repair."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sys
from typing import Any

from ht.errors import HtError
from ht.paths import Root
from ht.runtime import runtime_root
from ht.runtime.atomic import (
    fsync_directory,
    inspect_operation_state,
    make_directory,
    publish_immutable,
    read_exact_file,
    recover_immutable_publication,
    replace_file,
    require_directory,
)
from ht.runtime.capability import (
    B1_TOP_NAMES,
    CAPABILITY_FILE,
    EXPECTED_ROLE_DIRECTORIES,
    INITIALIZATION_FILE,
    CanonicalDocument,
    CapabilityState,
    inspect_capability_state,
)
from ht.runtime.custody import audit_lock, custody_is_free, try_instance_lock
from ht.runtime.inventory import (
    authorized_runtime_operations,
    validate_runtime_inventory,
    visible_queue_paths,
)
from ht.runtime.replay import (
    ReplayState,
    require_current_projections,
    upgrade_context_from_capability,
)
from ht.runtime.schema import canonical_json_bytes, strict_loads, validate
from ht.runtime.state import UpgradeContext, role_checkpoint
from ht.runtime.wal import parse_bytes


_BASE_CHECKPOINT_FIELDS = (
    "runtime_id",
    "last_seq",
    "clean_wal_sha256",
    "daemon_incarnation_id",
    "request_index",
    "dedup_index",
    "session_index",
    "control_index",
    "bindings",
    "binding_ledger_sha256",
    "final_tail",
)


@dataclass(frozen=True)
class _ActivationDocuments:
    capability: CanonicalDocument
    upgraded_checkpoint: CanonicalDocument
    initialization: CanonicalDocument
    baseline_checkpoint_bytes: bytes


@dataclass(frozen=True)
class _Preflight:
    descriptor: dict[str, Any]
    capability: CapabilityState
    replay: ReplayState
    checkpoint_bytes: bytes
    binding_ledger_bytes: bytes


def _created_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fault_point(_stage: str) -> None:
    """Module-private crash boundary used only by the deterministic corpus."""


def _document(
    value: dict[str, Any],
    *,
    schema_name: str,
) -> CanonicalDocument:
    validate(schema_name, value)
    data = canonical_json_bytes(value)
    return CanonicalDocument(value, data, _sha256(data))


def _build_activation_documents(
    *,
    runtime_id: str,
    checkpoint_bytes: bytes,
    binding_ledger_bytes: bytes,
    created_at: str,
) -> _ActivationDocuments:
    """Derive the three frozen documents from one exact eligible B1 state."""

    checkpoint_value = strict_loads(checkpoint_bytes, label="checkpoint.json")
    if not isinstance(checkpoint_value, dict):
        raise HtError("role initialization checkpoint is not an object (B2 §3.1)")
    validate("checkpoint.schema.json", checkpoint_value)
    if canonical_json_bytes(checkpoint_value) != checkpoint_bytes:
        raise HtError("role initialization checkpoint is not canonical (B2 §3.1)")
    if checkpoint_value["runtime_id"] != runtime_id:
        raise HtError("role initialization checkpoint names another runtime (B2 §3.1)")
    ledger_sha256 = _sha256(binding_ledger_bytes)
    if checkpoint_value["binding_ledger_sha256"] != ledger_sha256:
        raise HtError("role initialization binding ledger differs from checkpoint (B2 §3.1)")

    capability_value = {
        "schema_version": "hypothesis-tree-runtime-role-capability/1.0.0",
        "capability": "owned-role-runtime-v1",
        "runtime_id": runtime_id,
        "runtime_schema_version": "hypothesis-tree-runtime/1.0.0",
        "role_request_schema_version": (
            "hypothesis-tree-runtime-role-request/2.0.0"
        ),
        "upgrade_base_seq": checkpoint_value["last_seq"],
        "upgrade_base_clean_wal_sha256": checkpoint_value[
            "clean_wal_sha256"
        ],
        "upgrade_base_checkpoint_sha256": _sha256(checkpoint_bytes),
        "upgrade_base_binding_ledger_sha256": ledger_sha256,
        "created_at": created_at,
    }
    capability = _document(capability_value, schema_name="role-wire.schema.json")
    upgrade = UpgradeContext(
        runtime_id=runtime_id,
        role_capability_sha256=capability.sha256,
        upgrade_base_seq=capability_value["upgrade_base_seq"],
        upgrade_base_clean_wal_sha256=capability_value[
            "upgrade_base_clean_wal_sha256"
        ],
        upgrade_base_checkpoint_sha256=capability_value[
            "upgrade_base_checkpoint_sha256"
        ],
        upgrade_base_binding_ledger_sha256=capability_value[
            "upgrade_base_binding_ledger_sha256"
        ],
    )
    upgraded_checkpoint = _document(
        role_checkpoint(checkpoint_value, upgrade),
        schema_name="checkpoint-role.schema.json",
    )
    initialization_value = {
        "schema_version": "hypothesis-tree-runtime-role-initialization/1.0.0",
        "runtime_id": runtime_id,
        "capability_canonical_json": capability.canonical_bytes.decode("utf-8"),
        "capability_sha256": capability.sha256,
        "upgraded_checkpoint_canonical_json": (
            upgraded_checkpoint.canonical_bytes.decode("utf-8")
        ),
        "upgraded_checkpoint_sha256": upgraded_checkpoint.sha256,
        "expected_directories": list(EXPECTED_ROLE_DIRECTORIES),
        "created_at": created_at,
    }
    initialization = _document(
        initialization_value,
        schema_name="role-wire.schema.json",
    )
    return _ActivationDocuments(
        capability,
        upgraded_checkpoint,
        initialization,
        checkpoint_bytes,
    )


def _repair_documents(state: CapabilityState) -> _ActivationDocuments:
    capability = state.capability
    initialization = state.initialization
    upgraded = state.upgrade_target
    if capability is None or initialization is None or upgraded is None:
        raise HtError("role repair lacks its frozen activation documents (B2 §3.1)")
    baseline_value = {"schema_version": "hypothesis-tree-runtime-checkpoint/1.0.0"}
    baseline_value.update({name: upgraded.value[name] for name in _BASE_CHECKPOINT_FIELDS})
    validate("checkpoint.schema.json", baseline_value)
    baseline_bytes = canonical_json_bytes(baseline_value)
    if _sha256(baseline_bytes) != capability.value[
        "upgrade_base_checkpoint_sha256"
    ]:
        raise HtError("role repair baseline differs from frozen marker (B2 §3.1)")
    return _ActivationDocuments(
        capability,
        upgraded,
        initialization,
        baseline_bytes,
    )


def _load_descriptor(root: Root, estate: Path) -> dict[str, Any]:
    descriptor = strict_loads(
        read_exact_file(estate / "runtime.json"),
        label="runtime.json",
    )
    validate("descriptor.schema.json", descriptor)
    if (
        descriptor["runtime_root"] != str(estate.resolve())
        or descriptor["repository_root"] != str(root.path.resolve())
    ):
        raise HtError("runtime descriptor path identity differs from invocation (B1 §4)")
    return descriptor


def _busy(message: str) -> HtError:
    return HtError(f"role-init-runtime-busy: {message} (B2 §3.1)")


def _preflight_locked(root: Root, estate: Path) -> _Preflight:
    """Prove the complete stopped estate without cleanup or mutation."""

    descriptor = _load_descriptor(root, estate)
    runtime_id = descriptor["runtime_id"]
    capability = inspect_capability_state(
        estate,
        runtime_id=runtime_id,
        base_top_names=B1_TOP_NAMES,
    )
    wal_bytes = read_exact_file(estate / "run-ledger.jsonl")
    parsed = parse_bytes(wal_bytes)
    if parsed.tail is not None:
        raise HtError("role initialization rejects a tolerated WAL tail (B2 §3.1)")
    checkpoint_bytes = read_exact_file(estate / "checkpoint.json")
    if checkpoint_bytes != capability.checkpoint_replacement.final_bytes:
        raise HtError("role initialization checkpoint changed during inspection (B2 §3.1)")
    binding_ledger_bytes = read_exact_file(estate / "binding-ledger.json")
    upgrade = None
    if capability.checkpoint_stage == "upgraded":
        if capability.capability is None:
            raise HtError("upgraded role checkpoint lacks its capability (B2 §3.1)")
        upgrade = upgrade_context_from_capability(capability.capability)
    replay = require_current_projections(
        estate,
        parsed,
        runtime_id,
        upgrade=upgrade,
    )
    validate_runtime_inventory(
        estate,
        replay,
        capability_state=capability,
    )
    if replay.bindings["runtime#kernel"]["daemon_incarnation_id"] is not None:
        raise _busy("the replayed daemon is not stopped")
    for address, binding in sorted(replay.bindings.items()):
        if address == "runtime#kernel":
            continue
        if binding["admission_status"] == "pending" or binding["phase"] in {
            "claimed",
            "starting",
            "running",
        }:
            raise _busy(f"B1 binding {address} still requires lifecycle work")
        if binding["admission_status"] == "accepted" and binding["phase"] != "terminal":
            raise _busy(f"accepted B1 binding {address} is not terminal")
    b1_operations = authorized_runtime_operations(estate, replay)
    request_paths = visible_queue_paths(
        estate / "requests",
        b1_operations,
    )
    request_ids = {path.stem for path in request_paths}
    if request_ids != set(replay.request_index):
        raise _busy("an immutable B1 request has no replayed disposition")
    for session_id, session in sorted(replay.session_index.items()):
        if session["lifecycle"] != "terminal":
            raise _busy(
                f"B1 session {session_id} lifecycle is {session['lifecycle']}, not terminal"
            )
    for path, operation in sorted(b1_operations.items(), key=lambda item: str(item[0])):
        inspection = inspect_operation_state(path, operation=operation)
        if not inspection.temporary_names:
            continue
        if (
            path == estate / "checkpoint.json"
            and capability.branch == "repair-prefix"
            and inspection == capability.checkpoint_replacement
        ):
            continue
        raise HtError(
            f"role initialization rejects interrupted B1 operation at {path} "
            "(B2 §3.1)"
        )
    for session_id in sorted(replay.session_index):
        custody_path = estate / "sessions" / session_id / "custody.lock"
        if not custody_is_free(custody_path):
            raise _busy(f"B1 session {session_id} custody is held")
    return _Preflight(
        descriptor,
        capability,
        replay,
        checkpoint_bytes,
        binding_ledger_bytes,
    )


def _result(status: str, capability: CanonicalDocument) -> dict[str, Any]:
    value = {
        "schema_version": "hypothesis-tree-role-init-result/1.0.0",
        "status": status,
        "runtime_id": capability.value["runtime_id"],
        "capability": capability.value["capability"],
        "capability_sha256": capability.sha256,
        "created_at": capability.value["created_at"],
    }
    validate("role-wire.schema.json", value)
    return value


def _emit(value: dict[str, Any]) -> int:
    sys.stdout.buffer.write(canonical_json_bytes(value))
    return 0


def _finish_activation(
    root: Root,
    estate: Path,
    entry: _Preflight,
    documents: _ActivationDocuments,
) -> _Preflight:
    hidden_path = estate / INITIALIZATION_FILE
    public_path = estate / CAPABILITY_FILE

    recover_immutable_publication(
        hidden_path,
        documents.initialization.canonical_bytes,
    )
    publish_immutable(hidden_path, documents.initialization.canonical_bytes)
    if read_exact_file(hidden_path) != documents.initialization.canonical_bytes:
        raise HtError("role initialization marker reread differs (B2 §3.1)")
    _fault_point("hidden-published")

    for relative in EXPECTED_ROLE_DIRECTORIES:
        directory = estate / relative
        make_directory(directory)
        _fault_point(f"directory:{relative}:created")
        # A retry may find mkdir durable but its parent fsync interrupted.  The
        # repair owner completes that durability edge even for an existing dir.
        fsync_directory(directory.parent)
        inspect_capability_state(
            estate,
            runtime_id=entry.descriptor["runtime_id"],
            base_top_names=B1_TOP_NAMES,
        )
        _fault_point(f"directory:{relative}:fsynced")

    replace_file(
        estate / "checkpoint.json",
        documents.upgraded_checkpoint.canonical_bytes,
        expected_old=documents.baseline_checkpoint_bytes,
    )
    if read_exact_file(estate / "checkpoint.json") != (
        documents.upgraded_checkpoint.canonical_bytes
    ):
        raise HtError("role checkpoint reread differs after replacement (B2 §3.1)")
    _fault_point("checkpoint-replaced")

    recover_immutable_publication(public_path, documents.capability.canonical_bytes)
    publish_immutable(public_path, documents.capability.canonical_bytes)
    if read_exact_file(public_path) != documents.capability.canonical_bytes:
        raise HtError("role capability reread differs after publication (B2 §3.1)")
    _fault_point("public-published")

    # The complete public state is validated while the hidden repair authority
    # still exists, so a rejection never exposes an unowned upgraded branch.
    _preflight_locked(root, estate)
    _fault_point("validated-under-hidden")
    if read_exact_file(hidden_path) != documents.initialization.canonical_bytes:
        raise HtError("role initialization marker changed before removal (B2 §3.1)")
    os.unlink(hidden_path)
    _fault_point("hidden-unlinked")
    fsync_directory(estate)
    _fault_point("runtime-fsynced")
    completed = _preflight_locked(root, estate)
    if completed.capability.branch != "upgraded-complete":
        raise HtError("role initialization did not reach complete state (B2 §3.1)")
    return completed


def init(root: Root, *, as_json: bool) -> int:
    """Activate, resume, or idempotently verify the exact role capability."""

    if not as_json:  # parser makes this unreachable; keep the command API sealed.
        raise HtError("role init requires --json (B2 §21)")
    estate = runtime_root(root.path)
    require_directory(estate)
    instance_fd = try_instance_lock(estate / ".ht-runtime.instance.lock")
    if instance_fd is None:
        raise _busy("a runtime daemon owns the instance lock")
    try:
        with audit_lock(estate / ".harnessd.lock", exclusive=True):
            entry = _preflight_locked(root, estate)
            branch = entry.capability.branch
            if branch == "upgraded-complete":
                # Complete public state is also the only observable postimage
                # of a crash after hidden-marker unlink but before its parent
                # directory fsync.  Re-durabilize that unlink on every
                # idempotent entry, then revalidate while all three activation
                # locks remain held.  fsync changes no logical role/B1 state.
                fsync_directory(estate)
                entry = _preflight_locked(root, estate)
                assert entry.capability.capability is not None
                return _emit(_result("existing", entry.capability.capability))

            if branch == "unupgraded":
                # Only after every busy/corruption check may this owner discard
                # the sole uncommitted hidden-marker publication attempt.
                recover_immutable_publication(estate / INITIALIZATION_FILE, b"")
                documents = _build_activation_documents(
                    runtime_id=entry.descriptor["runtime_id"],
                    checkpoint_bytes=entry.checkpoint_bytes,
                    binding_ledger_bytes=entry.binding_ledger_bytes,
                    created_at=_created_at(),
                )
                status = "initialized"
            elif branch == "repair-prefix":
                documents = _repair_documents(entry.capability)
                status = "repaired"
            else:  # pragma: no cover - closed CapabilityState vocabulary
                raise HtError(f"unknown role capability branch {branch!r} (B2 §3.1)")

            completed = _finish_activation(root, estate, entry, documents)
            assert completed.capability.capability is not None
            return _emit(_result(status, completed.capability.capability))
    finally:
        os.close(instance_fd)
