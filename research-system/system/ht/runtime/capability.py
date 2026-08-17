"""Read-only physical classification of the B2 role-capability overlay.

This module does not activate or repair an estate.  It supplies the closed
physical/document state that the inventory and command layers need in order to
decide whether they may remain on B1, resume the one marker-owned prefix, or
enter the upgraded branch.  B1 inventory and replay validity remain the
caller's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Collection, Literal
from uuid import UUID

from ht.errors import HtError

from .atomic import (
    DIRECTORY_MODE,
    OperationInspection,
    inspect_operation_state,
    is_operation_temporary_name,
    read_exact_file,
)
from .schema import canonical_json_bytes, strict_loads, validate


CAPABILITY_FILE = "role-capability.json"
INITIALIZATION_FILE = ".ht-role-init.json"
B1_TOP_NAMES = frozenset(
    {
        ".harnessd.lock",
        ".ht-runtime.instance.lock",
        "binding-ledger.json",
        "checkpoint.json",
        "control",
        "requests",
        "responses",
        "run-ledger.jsonl",
        "runtime.json",
        "sessions",
    }
)
EXPECTED_ROLE_DIRECTORIES = (
    "role-approval-requests",
    "role-approval-responses",
    "producers",
    "producers/codex",
    "producers/codex/bootstrap-home",
    "producers/codex/bootstrap-sqlite",
    "producers/codex/bootstrap-tmp",
    "producers/codex/prompt-probe-home",
)

_B1_TOP_ATOMIC_TEMP_TARGETS = {
    "publish": B1_TOP_NAMES - {"control", "requests", "responses", "sessions"},
    "replace": frozenset(
        {"binding-ledger.json", "run-ledger.jsonl", "checkpoint.json"}
    ),
}

_B1_CHECKPOINT_FIELDS = (
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
_MARKER_BASELINE_FIELDS = (
    "upgrade_base_seq",
    "upgrade_base_clean_wal_sha256",
    "upgrade_base_checkpoint_sha256",
    "upgrade_base_binding_ledger_sha256",
)

RoleCapabilityBranch = Literal[
    "unupgraded",
    "repair-prefix",
    "upgraded-complete",
]
CheckpointStage = Literal["baseline", "upgraded"]


@dataclass(frozen=True)
class CanonicalDocument:
    """One exact canonical runtime JSON document and its raw-byte identity."""

    value: dict[str, Any]
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class CapabilityState:
    """Closed role overlay classification returned without filesystem writes."""

    branch: RoleCapabilityBranch
    capability: CanonicalDocument | None
    initialization: CanonicalDocument | None
    checkpoint: CanonicalDocument | None
    checkpoint_stage: CheckpointStage | None
    created_directories: tuple[str, ...]
    public_committed: bool
    checkpoint_replacement: OperationInspection
    upgrade_target: CanonicalDocument | None


def _fail(message: str) -> HtError:
    return HtError(f"role capability state corruption: {message} (B2 §3.1)")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_runtime_id(runtime_id: str) -> None:
    try:
        canonical = str(UUID(runtime_id))
    except (ValueError, AttributeError) as exc:
        raise _fail("expected runtime identity is not a canonical UUID") from exc
    if canonical != runtime_id:
        raise _fail("expected runtime identity is not a canonical UUID")


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
    )


def _stable_directory_names(path: Path) -> set[str]:
    """Enumerate one exact 0700 directory through a stable open descriptor."""

    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise _fail(f"missing directory {path}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or stat.S_IMODE(before.st_mode) != DIRECTORY_MODE
    ):
        raise _fail(f"{path} is not an exact non-symlink 0700 directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _fail(f"cannot open exact directory {path}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        names = set(os.listdir(fd))
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        current = path.lstat()
    except FileNotFoundError as exc:
        raise _fail(f"directory {path} disappeared during enumeration") from exc
    if not (
        _directory_identity(before)
        == _directory_identity(opened)
        == _directory_identity(after)
        == _directory_identity(current)
    ):
        raise _fail(f"directory {path} changed during stable enumeration")
    return names


def _canonical_document(data: bytes, *, label: str, schema_name: str) -> CanonicalDocument:
    value = strict_loads(data, label=label)
    if not isinstance(value, dict):
        raise _fail(f"{label} is not an object")
    validate(schema_name, value)
    if canonical_json_bytes(value) != data:
        raise _fail(f"{label} is not exact canonical JSON with one trailing LF")
    return CanonicalDocument(value, data, _sha256(data))


def _embedded_document(
    text: Any,
    expected_sha256: Any,
    *,
    label: str,
    schema_name: str,
) -> CanonicalDocument:
    if not isinstance(text, str):
        raise _fail(f"{label} canonical JSON is not text")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _fail(f"{label} canonical JSON is not UTF-8") from exc
    document = _canonical_document(data, label=label, schema_name=schema_name)
    if document.sha256 != expected_sha256:
        raise _fail(f"{label} hash differs from its exact embedded bytes")
    return document


def _read_final_document(
    inspection: OperationInspection,
    *,
    label: str,
    schema_name: str,
    require_complete: bool,
) -> CanonicalDocument | None:
    if not inspection.final_exists:
        return None
    if require_complete and inspection.temporary_names:
        raise _fail(f"complete {label} retains an activation publication temporary")
    assert inspection.final_bytes is not None
    return _canonical_document(
        inspection.final_bytes,
        label=label,
        schema_name=schema_name,
    )


def _decode_capability(
    document: CanonicalDocument,
    *,
    runtime_id: str,
) -> None:
    if document.value["runtime_id"] != runtime_id:
        raise _fail("role capability names a different runtime")


def _decode_initialization(
    document: CanonicalDocument,
    *,
    runtime_id: str,
) -> tuple[CanonicalDocument, CanonicalDocument]:
    value = document.value
    if value["runtime_id"] != runtime_id:
        raise _fail("role initialization marker names a different runtime")
    if tuple(value["expected_directories"]) != EXPECTED_ROLE_DIRECTORIES:
        raise _fail("role initialization directory order differs from the frozen prefix")
    capability = _embedded_document(
        value["capability_canonical_json"],
        value["capability_sha256"],
        label="embedded role capability",
        schema_name="role-wire.schema.json",
    )
    upgraded = _embedded_document(
        value["upgraded_checkpoint_canonical_json"],
        value["upgraded_checkpoint_sha256"],
        label="embedded upgraded checkpoint",
        schema_name="checkpoint-role.schema.json",
    )
    _decode_capability(capability, runtime_id=runtime_id)
    if value["created_at"] != capability.value["created_at"]:
        raise _fail("role initialization timestamp differs from capability timestamp")
    checkpoint = upgraded.value
    if checkpoint["runtime_id"] != runtime_id:
        raise _fail("embedded upgraded checkpoint names a different runtime")
    if checkpoint["role_capability_sha256"] != capability.sha256:
        raise _fail("embedded upgraded checkpoint is not bound to the capability bytes")
    for name in _MARKER_BASELINE_FIELDS:
        if checkpoint[name] != capability.value[name]:
            raise _fail(f"embedded upgraded checkpoint differs from capability {name}")
    if (
        checkpoint["last_seq"] != capability.value["upgrade_base_seq"]
        or checkpoint["clean_wal_sha256"]
        != capability.value["upgrade_base_clean_wal_sha256"]
        or checkpoint["binding_ledger_sha256"]
        != capability.value["upgrade_base_binding_ledger_sha256"]
        or checkpoint["daemon_incarnation_id"] is not None
        or checkpoint["final_tail"] is not None
    ):
        raise _fail("embedded upgraded checkpoint is not the stopped clean B1 baseline")

    # The hidden marker freezes enough information to reconstruct the exact v1
    # checkpoint preimage.  This proves the v2 target is additive rather than a
    # marker-consistent but otherwise changed projection.
    baseline = {"schema_version": "hypothesis-tree-runtime-checkpoint/1.0.0"}
    baseline.update({name: checkpoint[name] for name in _B1_CHECKPOINT_FIELDS})
    validate("checkpoint.schema.json", baseline)
    baseline_bytes = canonical_json_bytes(baseline)
    if _sha256(baseline_bytes) != capability.value["upgrade_base_checkpoint_sha256"]:
        raise _fail("embedded upgraded checkpoint does not derive the named B1 checkpoint")
    return capability, upgraded


def _expected_child_names(
    relative: str,
    observed: Collection[str],
) -> set[str]:
    prefix = relative + "/"
    return {
        candidate[len(prefix) :]
        for candidate in observed
        if candidate.startswith(prefix) and "/" not in candidate[len(prefix) :]
    }


def _inspect_directory_prefix(estate: Path) -> tuple[str, ...]:
    observed: list[str] = []
    for relative in EXPECTED_ROLE_DIRECTORIES:
        path = estate / relative
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        _stable_directory_names(path)
        observed.append(relative)
    observed_tuple = tuple(observed)
    if observed_tuple != EXPECTED_ROLE_DIRECTORIES[: len(observed_tuple)]:
        raise _fail("role directory inventory is not an exact creation prefix")
    observed_set = set(observed_tuple)
    for relative in observed_tuple:
        actual = _stable_directory_names(estate / relative)
        expected = _expected_child_names(relative, observed_set)
        if actual != expected:
            raise _fail(
                f"role directory {relative} is not the exact empty/child prefix; "
                f"unexpected entries are {sorted(actual - expected)}"
            )
    return observed_tuple


def _base_names(base_top_names: Collection[str]) -> frozenset[str]:
    result = frozenset(base_top_names)
    if any(
        not isinstance(name, str)
        or not name
        or "/" in name
        or name in {CAPABILITY_FILE, INITIALIZATION_FILE}
        for name in result
    ):
        raise ValueError("base_top_names must be exact non-role top-level names")
    if result != B1_TOP_NAMES:
        raise ValueError(
            "base_top_names must equal the exact B1 top-level inventory; "
            "caller data cannot widen runtime truth"
        )
    return result


def _top_without_operation_temps(
    estate: Path,
    hidden: OperationInspection,
    public: OperationInspection,
    *,
    allow_live_b1_operation_temps: bool,
) -> set[str]:
    actual = _stable_directory_names(estate)
    owned_temps = set(hidden.temporary_names) | set(public.temporary_names)
    if allow_live_b1_operation_temps:
        owned_temps.update(
            name
            for name in actual
            if any(
                is_operation_temporary_name(
                    name,
                    operation=operation,
                    target_name=target_name,
                )
                for operation, target_names in _B1_TOP_ATOMIC_TEMP_TARGETS.items()
                for target_name in target_names
            )
        )
    return actual - owned_temps


def _require_at_most_one_temporary(
    inspection: OperationInspection,
    *,
    label: str,
) -> None:
    if len(inspection.temporary_names) > 1:
        raise _fail(f"{label} has multiple concurrent operation temporaries")


def _validate_top_names(
    actual: set[str],
    expected: set[str],
    *,
    branch: str,
) -> None:
    if actual != expected:
        raise _fail(
            f"{branch} top-level inventory differs; "
            f"expected {sorted(expected)}, got {sorted(actual)}"
        )


def _current_checkpoint(estate: Path, *, role: bool) -> CanonicalDocument:
    path = estate / "checkpoint.json"
    data = read_exact_file(path)
    return _canonical_document(
        data,
        label="checkpoint.json",
        schema_name="checkpoint-role.schema.json" if role else "checkpoint.schema.json",
    )


def _validate_public_checkpoint(
    checkpoint: CanonicalDocument,
    capability: CanonicalDocument,
    runtime_id: str,
) -> None:
    value = checkpoint.value
    if value["runtime_id"] != runtime_id:
        raise _fail("upgraded checkpoint names a different runtime")
    if value["role_capability_sha256"] != capability.sha256:
        raise _fail("upgraded checkpoint differs from the public capability marker")
    for name in _MARKER_BASELINE_FIELDS:
        if value[name] != capability.value[name]:
            raise _fail(f"upgraded checkpoint differs from public capability {name}")


def inspect_capability_state(
    estate: Path,
    *,
    runtime_id: str,
    base_top_names: Collection[str],
    allow_live_b1_operation_temps: bool = False,
) -> CapabilityState:
    """Classify the exact role overlay without repairing or changing it.

    ``base_top_names`` is supplied by the B1 inventory owner.  This keeps the
    additive overlay independent from, and unable to silently widen, B1's own
    exact top-level contract.
    """

    _validate_runtime_id(runtime_id)
    base = _base_names(base_top_names)
    hidden_inspection = inspect_operation_state(
        estate / INITIALIZATION_FILE,
        operation="publish",
    )
    public_inspection = inspect_operation_state(
        estate / CAPABILITY_FILE,
        operation="publish",
    )
    checkpoint_replacement = inspect_operation_state(
        estate / "checkpoint.json",
        operation="replace",
    )
    _require_at_most_one_temporary(
        hidden_inspection,
        label="role initialization marker publication",
    )
    _require_at_most_one_temporary(
        public_inspection,
        label="role capability publication",
    )
    _require_at_most_one_temporary(
        checkpoint_replacement,
        label="role checkpoint replacement",
    )
    actual_top = _top_without_operation_temps(
        estate,
        hidden_inspection,
        public_inspection,
        allow_live_b1_operation_temps=allow_live_b1_operation_temps,
    )
    has_hidden = hidden_inspection.final_exists
    has_public = public_inspection.final_exists

    if not has_hidden and not has_public:
        if (
            checkpoint_replacement.temporary_names
            and not allow_live_b1_operation_temps
        ):
            raise _fail(
                "checkpoint replacement temporary has no hidden initialization owner"
            )
        if public_inspection.temporary_names:
            raise _fail(
                "capability publication temporary has no hidden marker owner"
            )
        _validate_top_names(actual_top, set(base), branch="unupgraded")
        return CapabilityState(
            "unupgraded",
            None,
            None,
            None,
            None,
            (),
            False,
            checkpoint_replacement,
            None,
        )

    if has_hidden:
        initialization = _read_final_document(
            hidden_inspection,
            label="role initialization marker",
            schema_name="role-wire.schema.json",
            require_complete=False,
        )
        assert initialization is not None
        capability, upgraded_target = _decode_initialization(
            initialization,
            runtime_id=runtime_id,
        )
        prefix = _inspect_directory_prefix(estate)
        expected_top = set(base) | {INITIALIZATION_FILE}
        expected_top.update(
            relative for relative in prefix if "/" not in relative
        )
        if has_public:
            expected_top.add(CAPABILITY_FILE)

        current_bytes = checkpoint_replacement.final_bytes
        if current_bytes is None:  # pragma: no cover - B1 inventory requires it
            raise _fail("repair checkpoint is missing")
        if current_bytes == upgraded_target.canonical_bytes:
            current = _canonical_document(
                current_bytes,
                label="checkpoint.json",
                schema_name="checkpoint-role.schema.json",
            )
            checkpoint_stage: CheckpointStage = "upgraded"
        else:
            current = _canonical_document(
                current_bytes,
                label="checkpoint.json",
                schema_name="checkpoint.schema.json",
            )
            if current.sha256 != capability.value["upgrade_base_checkpoint_sha256"]:
                raise _fail("repair checkpoint is neither the frozen B1 nor v2 target")
            checkpoint_stage = "baseline"

        complete_prefix = prefix == EXPECTED_ROLE_DIRECTORIES
        if checkpoint_stage == "upgraded" and not complete_prefix:
            raise _fail("upgraded checkpoint precedes the complete role directory prefix")
        if checkpoint_replacement.temporary_names:
            if not (
                complete_prefix
                and not has_public
                and checkpoint_stage == "baseline"
            ):
                raise _fail(
                    "checkpoint replacement temporary is outside its exact "
                    "marker-owned publication stage"
                )
            actual_top.difference_update(checkpoint_replacement.temporary_names)
        _validate_top_names(actual_top, expected_top, branch="repair-prefix")
        if public_inspection.temporary_names and not (
            checkpoint_stage == "upgraded" and complete_prefix
        ):
            raise _fail("capability publication temporary precedes its frozen repair stage")
        public = _read_final_document(
            public_inspection,
            label="public role capability",
            schema_name="role-wire.schema.json",
            require_complete=False,
        )
        if public is not None:
            if not complete_prefix or checkpoint_stage != "upgraded":
                raise _fail("public capability precedes its complete marker-derived prefix")
            if public.canonical_bytes != capability.canonical_bytes:
                raise _fail("public capability differs from the initialization target")

        return CapabilityState(
            "repair-prefix",
            capability,
            initialization,
            current,
            checkpoint_stage,
            prefix,
            public is not None,
            checkpoint_replacement,
            upgraded_target,
        )

    # Public capability without the hidden repair authority must already be a
    # completely cleaned, upgraded estate.  No temp or missing suffix is
    # repairable in this branch.
    if hidden_inspection.temporary_names or public_inspection.temporary_names:
        raise _fail("upgraded-complete state retains an activation temporary")
    if (
        checkpoint_replacement.temporary_names
        and not allow_live_b1_operation_temps
    ):
        raise _fail("upgraded-complete state retains a checkpoint replacement temporary")
    public = _read_final_document(
        public_inspection,
        label="public role capability",
        schema_name="role-wire.schema.json",
        require_complete=True,
    )
    assert public is not None
    _decode_capability(public, runtime_id=runtime_id)
    prefix = _inspect_directory_prefix(estate)
    if prefix != EXPECTED_ROLE_DIRECTORIES:
        raise _fail("public capability lacks the complete role directory prefix")
    expected_top = set(base) | {
        CAPABILITY_FILE,
        "role-approval-requests",
        "role-approval-responses",
        "producers",
    }
    _validate_top_names(actual_top, expected_top, branch="upgraded-complete")
    checkpoint = _current_checkpoint(estate, role=True)
    _validate_public_checkpoint(checkpoint, public, runtime_id)
    return CapabilityState(
        "upgraded-complete",
        public,
        None,
        checkpoint,
        "upgraded",
        prefix,
        True,
        checkpoint_replacement,
        None,
    )
