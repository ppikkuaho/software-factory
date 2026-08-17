"""Exact visible filesystem inventory for the independent runtime estate."""

from __future__ import annotations

from pathlib import Path
import stat
from typing import TYPE_CHECKING, Mapping
from uuid import UUID

from ht.errors import HtError

from .atomic import (
    inspect_operation_state,
    is_operation_temporary_name,
    read_exact_file,
    require_directory,
)
from .capability import (
    B1_TOP_NAMES,
    CAPABILITY_FILE,
    EXPECTED_ROLE_DIRECTORIES,
    INITIALIZATION_FILE,
    CapabilityState,
    inspect_capability_state,
)

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard for annotations only
    from .replay import ReplayState


_TOP_DIRECTORIES = frozenset({"requests", "responses", "control", "sessions"})
_TOP_FILES = frozenset(
    {
        "runtime.json",
        "binding-ledger.json",
        "run-ledger.jsonl",
        "checkpoint.json",
        ".harnessd.lock",
        ".ht-runtime.instance.lock",
    }
)
_SESSION_FILES = frozenset(
    {
        "packet.json",
        "launch.json",
        "custody.lock",
        "started.json",
        "ready.json",
        "result.json",
        "terminal.json",
        "process-exit.json",
    }
)
_SESSION_PUBLICATION_TARGETS = _SESSION_FILES - {"custody.lock"}
_RECEIPT_FOR_HASH = {
    "started_sha256": "started.json",
    "ready_sha256": "ready.json",
    "result_sha256": "result.json",
    "terminal_sha256": "terminal.json",
    "process_exit_sha256": "process-exit.json",
}
_NO_PROCESS_TERMINAL_REASONS = frozenset(
    {
        "accepted-response-missing-at-boot",
        "popen-failed",
        "starting-recovery-no-process",
    }
)
_TOP_ATOMIC_TEMP_TARGETS = {
    "publish": _TOP_FILES,
    "replace": frozenset(
        {"binding-ledger.json", "run-ledger.jsonl", "checkpoint.json"}
    ),
}


def _corrupt(message: str) -> HtError:
    return HtError(f"runtime filesystem inventory corruption: {message} (B1 §3)")


def _canonical_uuid(value: str, label: str) -> str:
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise _corrupt(f"{label} is not a UUID: {value!r}") from exc
    if canonical != value:
        raise _corrupt(f"{label} is not a canonical UUID: {value!r}")
    return value


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _names(path: Path) -> set[str]:
    try:
        return {entry.name for entry in path.iterdir()}
    except OSError as exc:
        raise _corrupt(f"cannot enumerate {path}: {exc}") from exc


def _merge_operations(
    target: dict[Path, str],
    additions: Mapping[Path, str],
) -> None:
    for path, operation in additions.items():
        prior = target.get(path)
        if prior is not None and prior != operation:
            raise _corrupt(
                f"conflicting atomic operation owners for {path}: "
                f"{prior!r} and {operation!r}"
            )
        target[path] = operation


def _queue_publish_target_name(name: str) -> str | None:
    """Decode only the exact temp grammar for one UUID-named queue target."""

    prefix = ".ht-publish-"
    target_length = 36 + len(".json")
    if not name.startswith(prefix) or len(name) <= len(prefix) + target_length:
        return None
    target_name = name[len(prefix) : len(prefix) + target_length]
    if (
        name[len(prefix) + target_length : len(prefix) + target_length + 1] != "-"
        or not target_name.endswith(".json")
        or not _is_canonical_uuid(target_name[:-5])
        or not is_operation_temporary_name(
            name,
            operation="publish",
            target_name=target_name,
        )
    ):
        return None
    return target_name


def _queue_publication_operations(path: Path) -> dict[Path, str]:
    require_directory(path)
    operations: dict[Path, str] = {}
    for name in _names(path):
        target_name = _queue_publish_target_name(name)
        if target_name is not None:
            operations[path / target_name] = "publish"
    return operations


def submission_publication_operations(estate: Path) -> dict[Path, str]:
    """Authorize exact public request/control publication attempts.

    These two owners can fail before any WAL event exists, so canonical target
    identity and physical atomic-operation shape are their only durable boot
    authority.  Response queues are intentionally excluded.
    """

    operations = _queue_publication_operations(estate / "requests")
    _merge_operations(
        operations,
        _queue_publication_operations(estate / "control" / "requests"),
    )
    return operations


def _operation_complete(path: Path) -> bool:
    inspection = inspect_operation_state(path, operation="publish")
    return inspection.final_exists and not inspection.temporary_names


def _child_publication_operations(
    estate: Path,
    state: ReplayState,
    session_id: str,
    entry: Mapping[str, object],
) -> dict[Path, str]:
    """Return only child publications reachable from frozen durable truth."""

    work = state.bindings.get(entry["node_address"])
    if not isinstance(work, dict):
        return {}
    session = work.get("sessions", {}).get(session_id)
    request = state.request_index.get(work.get("request_id"))
    if not isinstance(session, dict) or not isinstance(request, dict):
        return {}
    lifecycle = session.get("lifecycle")
    if lifecycle not in {"starting", "running", "terminal"}:
        return {}
    if (
        request.get("status") != "accepted"
        or request.get("recovery_created") is not False
        or (
            lifecycle == "terminal"
            and session.get("terminal_reason_code") in _NO_PROCESS_TERMINAL_REASONS
        )
    ):
        return {}

    response_path = estate / "responses" / f"{work['request_id']}.json"
    if not _operation_complete(response_path):
        return {}

    session_path = estate / "sessions" / session_id
    # Durable accepted+starting is the launch barrier.  From that point the
    # child processes may advance through every receipt before the daemon's
    # WAL catches up.  Authorize the complete closed target set at once so a
    # live child crossing from one publication to the next cannot race a
    # daemon inventory pass.  Visible finals remain strictly ordered by
    # `_validate_session_shape`; opaque pre-link temps are never parsed as
    # evidence of a completed predecessor.
    return {
        session_path / target_name: "publish"
        for target_name in _SESSION_PUBLICATION_TARGETS
        if target_name not in {"packet.json", "launch.json"}
    }


def authorized_runtime_operations(
    estate: Path,
    state: ReplayState,
) -> dict[Path, str]:
    """Derive every exact atomic-operation owner from durable runtime truth."""

    operations: dict[Path, str] = {
        estate / "run-ledger.jsonl": "replace",
        estate / "binding-ledger.json": "replace",
        estate / "checkpoint.json": "replace",
    }
    # Role activation closes the B1 synthetic submission lane.  No new final
    # or in-flight request publication can acquire physical ownership after
    # that boundary; historical replayed requests remain ordinary inventory.
    if state.upgrade is None:
        _merge_operations(operations, submission_publication_operations(estate))
    for request_id, entry in state.request_index.items():
        if entry["status"] != "planned":
            operations[estate / "responses" / f"{request_id}.json"] = "publish"
    for control_id in state.control_index:
        operations[
            estate / "control" / "responses" / f"{control_id}.json"
        ] = "publish"
    for session_id, entry in state.session_index.items():
        session_path = estate / "sessions" / session_id
        if entry["lifecycle"] == "claimed":
            operations[session_path / "packet.json"] = "publish"
            operations[session_path / "launch.json"] = "publish"
            continue
        _merge_operations(
            operations,
            _child_publication_operations(estate, state, session_id, entry),
        )
    return operations


def _provisional_runtime_operations(estate: Path) -> dict[Path, str]:
    """Physical-only temp owners for the first half of a two-pass read.

    A state-free public read cannot yet decide durable lifecycle authority.  It
    admits only exact target grammars for physical inspection; the mandatory
    stateful pass below re-derives the narrower durable owner set.
    """

    operations: dict[Path, str] = {}
    for operation, target_names in _TOP_ATOMIC_TEMP_TARGETS.items():
        for target_name in target_names:
            operations[estate / target_name] = operation
    for relative in (
        ("requests",),
        ("responses",),
        ("control", "requests"),
        ("control", "responses"),
    ):
        _merge_operations(
            operations,
            _queue_publication_operations(estate.joinpath(*relative)),
        )
    sessions_root = estate / "sessions"
    require_directory(sessions_root)
    for session_name in _names(sessions_root):
        if not _is_canonical_uuid(session_name):
            continue
        session_path = sessions_root / session_name
        try:
            info = session_path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue
        names = _names(session_path)
        for target_name in _SESSION_PUBLICATION_TARGETS:
            if any(
                is_operation_temporary_name(
                    name,
                    operation="publish",
                    target_name=target_name,
                )
                for name in names
            ):
                operations[session_path / target_name] = "publish"
    return operations


def visible_queue_paths(
    path: Path,
    authorized_operations: Mapping[Path, str],
) -> tuple[Path, ...]:
    """Return validated canonical queue finals without owner temp names."""

    identifiers = _queue_inventory(path, authorized_operations)
    return tuple(path / f"{identifier}.json" for identifier in sorted(identifiers))


def _is_authorized_top_atomic_temporary(name: str) -> bool:
    return any(
        is_operation_temporary_name(
            name,
            operation=operation,
            target_name=target_name,
        )
        for operation, target_names in _TOP_ATOMIC_TEMP_TARGETS.items()
        for target_name in target_names
    )


def _require_regular(path: Path, *, empty: bool = False) -> None:
    data = read_exact_file(path)
    if empty and data:
        raise _corrupt(f"{path} must be empty")


def _operation_for(
    path: Path,
    authorized_operations: Mapping[Path, str],
) -> str | None:
    return authorized_operations.get(path)


def _visible_names(
    path: Path,
    authorized_operations: Mapping[Path, str],
) -> set[str]:
    """Return schema names after validating only explicitly owned op temps."""

    names = _names(path)
    targets = {
        target: operation
        for target, operation in authorized_operations.items()
        if target.parent == path
    }
    hidden: set[str] = set()
    for name in names:
        owners = [
            target
            for target, operation in targets.items()
            if is_operation_temporary_name(
                name,
                operation=operation,
                target_name=target.name,
            )
        ]
        if not owners:
            continue
        if len(owners) != 1:  # pragma: no cover - target names are unique
            raise _corrupt(f"ambiguous atomic temporary owner for {path / name}")
        hidden.add(name)
    for target, operation in targets.items():
        inspect_operation_state(target, operation=operation)
    return names - hidden


def _require_regular_shape(path: Path, *, empty: bool = False) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise _corrupt(f"missing file {path}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or (empty and info.st_size != 0)
    ):
        raise _corrupt(f"{path} is not an exact canonical 0600 regular file")


def _queue_inventory(
    path: Path,
    authorized_operations: Mapping[Path, str],
) -> set[str]:
    require_directory(path)
    identifiers: set[str] = set()
    for name in _visible_names(path, authorized_operations):
        candidate = path / name
        if not name.endswith(".json"):
            raise _corrupt(f"unexpected queue entry {candidate}")
        identifier = _canonical_uuid(name[:-5], f"queue filename in {path}")
        operation = _operation_for(candidate, authorized_operations)
        if operation is None:
            _require_regular(candidate)
        identifiers.add(identifier)
    return identifiers


def _session_inventory(
    path: Path,
    authorized_operations: Mapping[Path, str],
) -> set[str]:
    require_directory(path)
    names = _visible_names(path, authorized_operations)
    unexpected = names - _SESSION_FILES
    if unexpected:
        raise _corrupt(f"unexpected entries in {path}: {sorted(unexpected)}")
    for name in names:
        candidate = path / name
        operation = _operation_for(candidate, authorized_operations)
        if operation is None:
            _require_regular(candidate, empty=name == "custody.lock")
    return names


def _validate_session_shape(names: set[str], session: dict[str, object], path: Path) -> None:
    lifecycle = session["lifecycle"]
    base = {"packet.json", "launch.json", "custody.lock"}
    receipts = _SESSION_FILES - base
    if lifecycle == "claimed":
        prefixes = (
            set(),
            {"packet.json"},
            {"packet.json", "launch.json"},
            base,
        )
        if names not in prefixes:
            raise _corrupt(f"claimed session inventory is not a preparation prefix at {path}")
        return
    if not base <= names:
        raise _corrupt(f"{lifecycle} session lacks frozen preparation artifacts at {path}")
    observed_receipts = names & receipts
    if lifecycle == "abandoned" and observed_receipts:
        raise _corrupt(f"abandoned pre-start session has child receipts at {path}")
    if "ready.json" in names and "started.json" not in names:
        raise _corrupt(f"ready receipt precedes started receipt at {path}")
    if "result.json" in names and "ready.json" not in names:
        raise _corrupt(f"result receipt precedes ready receipt at {path}")
    if "terminal.json" in names and "result.json" not in names:
        raise _corrupt(f"terminal receipt precedes result receipt at {path}")
    if "process-exit.json" in names and "started.json" not in names:
        raise _corrupt(f"process-exit receipt lacks started receipt at {path}")
    if lifecycle == "running" and not {"started.json", "ready.json"} <= names:
        raise _corrupt(f"running session lacks started/ready receipts at {path}")
    if lifecycle == "terminal":
        expected_receipts = {
            filename
            for field, filename in _RECEIPT_FOR_HASH.items()
            if session[field] is not None
        }
        if observed_receipts != expected_receipts:
            raise _corrupt(f"terminal receipt inventory differs from replay at {path}")


def _typed_capability_overlay(
    estate: Path,
    capability: CapabilityState,
    *,
    runtime_id: str,
    allow_live_b1_operation_temps: bool,
) -> tuple[set[str], dict[Path, str]]:
    """Return only the exact top-level overlay already classified by R1.

    This deliberately accepts a closed ``CapabilityState`` rather than caller-
    supplied names.  The capability inspector owns every additive name and
    operation temporary; B1 inventory continues to own and validate all of its
    original files, queues, controls, and sessions below that overlay.
    """

    checkpoint = estate / "checkpoint.json"
    if capability.checkpoint_replacement.path != checkpoint:
        raise _corrupt("capability checkpoint inspection names another estate")
    current = inspect_capability_state(
        estate,
        runtime_id=runtime_id,
        base_top_names=B1_TOP_NAMES,
        allow_live_b1_operation_temps=allow_live_b1_operation_temps,
    )
    if current != capability:
        raise _corrupt("typed capability classification became stale")

    additions: set[str] = set()
    operations: dict[Path, str] = {
        estate / INITIALIZATION_FILE: "publish",
    }
    if capability.branch == "unupgraded":
        if (
            capability.capability is not None
            or capability.initialization is not None
            or capability.created_directories
            or capability.public_committed
        ):
            raise _corrupt("unupgraded capability state carries overlay contents")
        return additions, operations

    if capability.branch == "repair-prefix":
        if capability.initialization is None or capability.capability is None:
            raise _corrupt("repair capability state lacks its frozen marker documents")
        additions.add(INITIALIZATION_FILE)
        additions.update(
            relative
            for relative in capability.created_directories
            if "/" not in relative
        )
        operations[estate / CAPABILITY_FILE] = "publish"
        if capability.public_committed:
            additions.add(CAPABILITY_FILE)
        return additions, operations

    if capability.branch == "upgraded-complete":
        if (
            capability.initialization is not None
            or capability.capability is None
            or not capability.public_committed
            or capability.created_directories != EXPECTED_ROLE_DIRECTORIES
        ):
            raise _corrupt("complete capability state is internally inconsistent")
        additions.add(CAPABILITY_FILE)
        additions.update(
            relative
            for relative in EXPECTED_ROLE_DIRECTORIES
            if "/" not in relative
        )
        return additions, operations

    raise _corrupt(f"unknown typed capability branch {capability.branch!r}")


def validate_runtime_inventory(
    estate: Path,
    state: ReplayState | None = None,
    *,
    include_dynamic: bool = True,
    authorized_operations: Mapping[Path, str] | None = None,
    capability_state: CapabilityState | None = None,
    runtime_id: str | None = None,
) -> None:
    """Reject every unexpected visible entry and every wrong type/mode/link.

    The caller holds the audit lock for dynamic validation.  A state-free pass
    is suitable before the daemon instance-lock decision: it validates all
    names and physical shapes without requiring WAL-derived existence.
    """

    require_directory(estate)
    if frozenset(_TOP_DIRECTORIES | _TOP_FILES) != B1_TOP_NAMES:
        raise RuntimeError("B1 inventory constants differ from the role overlay boundary")
    if state is not None:
        if runtime_id is not None and runtime_id != state.runtime_id:
            raise ValueError("typed capability inventory runtime ID differs from replay")
        runtime_id = state.runtime_id
    if capability_state is not None and runtime_id is None:
        raise ValueError("typed capability inventory requires the descriptor runtime ID")
    if not include_dynamic:
        operations: dict[Path, str] = {}
    elif state is None:
        operations = _provisional_runtime_operations(estate)
    else:
        operations = authorized_runtime_operations(estate, state)
    overlay_names: set[str] = set()
    if capability_state is not None:
        assert runtime_id is not None
        overlay_names, overlay_operations = _typed_capability_overlay(
            estate,
            capability_state,
            runtime_id=runtime_id,
            allow_live_b1_operation_temps=not include_dynamic,
        )
        _merge_operations(operations, overlay_operations)
    _merge_operations(operations, authorized_operations or {})
    expected_top = set(_TOP_DIRECTORIES | _TOP_FILES) | overlay_names
    actual_top = _names(estate)
    # A live owner can be between creation and commit/removal of one exact
    # same-directory atomic temp when a contender performs its read-only
    # pre-lock topology pass.  Ignore only names produced by an authorized
    # operation/target pair in that pass.  Once this process wins the instance
    # lock, dynamic validation remains exact and rejects every surviving temp
    # before recovery or mutation.
    observed_top = (
        {
            name
            for name in actual_top
            if not _is_authorized_top_atomic_temporary(name)
        }
        if not include_dynamic
        else _visible_names(estate, operations)
    )
    if observed_top != expected_top:
        raise _corrupt(
            f"top-level entries differ: expected {sorted(expected_top)}, "
            f"got {sorted(observed_top)}"
        )
    for name in _TOP_DIRECTORIES:
        require_directory(estate / name)
    require_directory(estate / "control" / "requests")
    require_directory(estate / "control" / "responses")
    control_names = _names(estate / "control")
    if control_names != {"requests", "responses"}:
        raise _corrupt(f"control entries differ: {sorted(control_names)}")
    for name in _TOP_FILES:
        empty = name in {".harnessd.lock", ".ht-runtime.instance.lock"}
        if include_dynamic:
            _require_regular(estate / name, empty=empty)
        else:
            # A current daemon may atomically replace/append mutable top-level
            # files before this contender reaches the instance lock.  Prove
            # physical shape without demanding a stable content read here;
            # the winner performs the complete stable proof under audit EX.
            _require_regular_shape(estate / name, empty=empty)

    if not include_dynamic:
        return

    request_ids = _queue_inventory(estate / "requests", operations)
    response_ids = _queue_inventory(estate / "responses", operations)
    control_request_ids = _queue_inventory(estate / "control" / "requests", operations)
    control_response_ids = _queue_inventory(estate / "control" / "responses", operations)

    sessions_root = estate / "sessions"
    require_directory(sessions_root)
    actual_sessions: dict[str, set[str]] = {}
    for name in _visible_names(sessions_root, operations):
        session_id = _canonical_uuid(name, "session directory")
        path = sessions_root / name
        require_directory(path)
        actual_sessions[session_id] = _session_inventory(path, operations)

    if state is None:
        return

    known_requests = set(state.request_index)
    if state.upgrade is not None and request_ids != known_requests:
        raise _corrupt(
            "unrecorded B1 synthetic request exists after role activation"
        )
    if not known_requests <= request_ids:
        raise _corrupt("replayed request lacks its immutable request file")
    settled_requests = {
        request_id
        for request_id, entry in state.request_index.items()
        if entry["status"] != "planned"
    }
    if not response_ids <= settled_requests:
        raise _corrupt("admission response exists without a durable disposition")

    known_controls = set(state.control_index)
    if not known_controls <= control_request_ids:
        raise _corrupt("replayed control lacks its immutable control request")
    if not control_response_ids <= known_controls:
        raise _corrupt("control response exists without a durable disposition")

    known_sessions = set(state.session_index)
    if not set(actual_sessions) <= known_sessions:
        raise _corrupt("session directory exists without a durable claim")
    for session_id, entry in state.session_index.items():
        lifecycle = entry["lifecycle"]
        if session_id not in actual_sessions:
            if lifecycle == "claimed":
                continue
            raise _corrupt(f"replayed {lifecycle} session lacks its directory: {session_id}")
        _validate_session_shape(
            actual_sessions[session_id],
            entry,
            sessions_root / session_id,
        )
