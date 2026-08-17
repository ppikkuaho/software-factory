"""Single-incarnation B1 daemon for the normal fenced synthetic lifecycle."""

from __future__ import annotations

if __package__ in {None, ""}:  # trusted absolute isolated entry
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import time
from typing import Any
from uuid import UUID, uuid4

from ht.errors import HtError
from ht.paths import Root
from ht.runtime.atomic import (
    OperationInspection,
    inspect_operation_state,
    make_directory,
    publish_immutable,
    read_exact_file,
    recover_immutable_publication,
    replace_file,
)
from ht.runtime.custody import (
    audit_lock,
    create_custody,
    custody_is_free,
    ensure_custody_file,
    try_instance_lock,
)
from ht.runtime.gate import (
    ROLE_INIT_REQUIRED,
    RuntimeGate,
    inspect_runtime_gate,
    is_role_init_required,
)
from ht.runtime.launcher import child_options, construct_claim, wrapper_argv
from ht.runtime.inventory import (
    authorized_runtime_operations,
    validate_runtime_inventory,
    visible_queue_paths,
)
from ht.runtime.replay import (
    ReplayState,
    ProjectionEligibility,
    build_record,
    projection_eligibility,
    publish_projections,
    recover_tolerated_tail,
    replay,
)
from ht.runtime.repository import WorkSnapshot, revalidate_work
from ht.runtime.schema import canonical_json_bytes, strict_loads, validate
from ht.runtime.state import derive_dedup_key
from ht.runtime.wal import ParsedWal, frame_record, parse_bytes


POLL_SECONDS = 0.02
_popen_wrapper = subprocess.Popen


@dataclass(frozen=True)
class SessionSupervisor:
    """One live session supervised locally or adopted through custody."""

    process: subprocess.Popen[Any] | None
    adopted: bool


@dataclass(frozen=True)
class ResponseBootFact:
    path: Path
    canonical_bytes: bytes
    status: str
    existed_at_boot: bool


@dataclass(frozen=True)
class RequestBootFact:
    request_id: str
    path: Path
    canonical_bytes: bytes
    sha256: str
    value: dict[str, Any]
    durable_status: str | None


@dataclass(frozen=True)
class ControlRequestBootFact:
    control_id: str
    path: Path
    canonical_bytes: bytes
    sha256: str
    value: dict[str, Any]
    durable_status: str | None


@dataclass(frozen=True)
class ArtifactBootFact:
    name: str
    path: Path
    canonical_bytes: bytes
    sha256: str
    value: dict[str, Any]


@dataclass(frozen=True)
class SessionBootFact:
    session_id: str
    path: Path
    lifecycle: str
    directory_exists: bool
    packet_exists: bool
    launch_exists: bool
    custody_exists: bool
    custody_free: bool | None
    process_artifacts: tuple[str, ...]
    artifacts: tuple[ArtifactBootFact, ...]


@dataclass(frozen=True)
class BootFacts:
    """Immutable facts captured before any owner recovery mutates the estate."""

    parsed: ParsedWal
    eligibility: ProjectionEligibility
    gate: RuntimeGate
    operation_inspections: tuple[OperationInspection, ...]
    request_facts: tuple[RequestBootFact, ...]
    control_request_facts: tuple[ControlRequestBootFact, ...]
    response_facts: tuple[ResponseBootFact, ...]
    control_response_facts: tuple[ResponseBootFact, ...]
    session_facts: tuple[SessionBootFact, ...]
    accepted_responses_present_at_entry: frozenset[str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_uuid_filename(path: Path) -> str:
    if path.suffix != ".json":
        raise HtError(f"runtime queue entry has invalid name {path.name!r} (B1 §3)")
    try:
        value = str(UUID(path.stem))
    except (ValueError, AttributeError) as exc:
        raise HtError(f"runtime queue entry has invalid UUID {path.name!r} (B1 §3)") from exc
    if value != path.stem:
        raise HtError(f"runtime queue entry UUID is not canonical {path.name!r} (B1 §3)")
    return value


def _descriptor(estate: Path, root: Path) -> dict[str, Any]:
    if (estate / ".ht-runtime.genesis.json").exists():
        raise HtError("runtime genesis is incomplete (B1 §4)")
    value = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime.json")
    validate("descriptor.schema.json", value)
    if (
        value["runtime_root"] != str(estate.resolve())
        or value["repository_root"] != str(root.resolve())
    ):
        raise HtError("runtime descriptor path identity differs from invocation (B1 §4)")
    return value


def _prepare_boot(
    estate: Path,
    runtime_id: str,
    gate: RuntimeGate | None = None,
) -> tuple[ReplayState, BootFacts]:
    """Scan once, repair owner storage, and retain the immutable entry facts.

    The returned ``BootFacts`` remain the sole boot-entry observation used by
    reconciliation.  In particular, recovery never rescans after completing
    an interrupted immutable publication.
    """

    facts = _scan_boot(estate, runtime_id, gate=gate)
    target = _recover_boot_storage(estate, facts)
    # Storage repair may have completed a missing custody file.  Re-prove all
    # claimed locks before daemon_started so every presently knowable
    # contradiction fails without leaving a logically live incarnation.  The
    # mandatory immediate probe is repeated directly before each rollback.
    for entry in sorted(target.session_index.values(), key=lambda value: value["session_id"]):
        if entry["lifecycle"] != "claimed":
            continue
        custody_path = estate / "sessions" / entry["session_id"] / "custody.lock"
        if not custody_is_free(custody_path):
            raise HtError("boot recovery custody is held before daemon start (B1 §14)")
    return target, facts


def _boot_state(
    estate: Path,
    runtime_id: str,
    gate: RuntimeGate | None = None,
) -> ReplayState:
    """Compatibility entry for tests and direct clean-state operations."""

    return _prepare_boot(estate, runtime_id, gate=gate)[0]


def _append(estate: Path, state: ReplayState, event: str, **fields: Any) -> ReplayState:
    framed = frame_record(build_record(state, event, _now(), **fields))
    wal_path = estate / "run-ledger.jsonl"
    before = read_exact_file(wal_path)
    if before != state.clean_prefix:
        raise HtError("runtime WAL changed between replay and append (B1 §10)")
    fd = os.open(wal_path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    try:
        offset = 0
        while offset < len(framed):
            count = os.write(fd, framed[offset:])
            if count <= 0:
                raise OSError("short runtime WAL append")
            offset += count
        os.fsync(fd)
    finally:
        os.close(fd)
    after = read_exact_file(wal_path)
    if after != before + framed:
        raise HtError("runtime WAL append reread differs (B1 §10)")
    target = replay(parse_bytes(after), state.runtime_id, upgrade=state.upgrade)
    publish_projections(estate, target, allowed_prior=(state, target))
    return target


def _response_object(entry: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "hypothesis-tree-runtime-admission-response/1.0.0",
        "status": entry["status"],
        "request_id": entry["request_id"],
    }
    if entry["status"] == "accepted":
        for name in (
            "binding_id",
            "node_address",
            "lease_epoch",
            "session_id",
            "packet_sha256",
            "recovery_created",
        ):
            value[name] = entry[name]
    elif entry["status"] == "duplicate":
        for name in (
            "original_request_id",
            "binding_id",
            "node_address",
            "lease_epoch",
            "session_id",
            "packet_sha256",
        ):
            value[name] = entry[name]
    elif entry["status"] == "rejected":
        value["reason_code"] = entry["reason_code"]
    else:
        raise HtError("planned request has no response object (B1 §8)")
    validate("admission-response.schema.json", value)
    return value


def _publish_response(estate: Path, state: ReplayState, request_id: str) -> None:
    value = _response_object(state.request_index[request_id])
    publish_immutable(
        estate / "responses" / f"{request_id}.json",
        canonical_json_bytes(value),
    )


def _load_request(path: Path) -> tuple[dict[str, Any], bytes]:
    request_id = _canonical_uuid_filename(path)
    inspection = inspect_operation_state(path, operation="publish")
    if not inspection.final_exists or inspection.final_bytes is None:
        raise HtError("runtime request final is absent (B1 §6/§7)")
    data = inspection.final_bytes
    value = strict_loads(data, label=path.name)
    validate("request.schema.json", value)
    if value["request_id"] != request_id or canonical_json_bytes(value) != data:
        raise HtError("runtime request filename or bytes are not canonical (B1 §7)")
    return value, data


def _inspect_request_facts(
    estate: Path,
    state: ReplayState,
    parsed: ParsedWal,
    operations: dict[Path, str],
) -> tuple[RequestBootFact, ...]:
    """Validate every submitted request before any owner repair can run.

    A bound request's complete immutable truth comes only from its durable
    ``work_planned`` record.  Duplicate and pre-plan rejected requests do not
    carry a complete request in the WAL, so their own strict canonical bytes
    remain the submission truth unless a durable request SHA is available.
    """

    planned_records = {
        record["request_id"]: record
        for record in parsed.records
        if record["event"] == "work_planned"
    }
    facts: list[RequestBootFact] = []
    for path in visible_queue_paths(estate / "requests", operations):
        request, data = _load_request(path)
        request_id = request["request_id"]
        digest = hashlib.sha256(data).hexdigest()
        entry = state.request_index.get(request_id)
        planned = planned_records.get(request_id)
        if planned is not None:
            expected = canonical_json_bytes(planned["request"])
            if data != expected or digest != planned["request_sha256"]:
                raise HtError(
                    "immutable request differs from work_planned truth (B1 §8/§14)"
                )
        if entry is not None and entry["request_sha256"] is not None:
            if digest != entry["request_sha256"]:
                raise HtError(
                    "immutable request differs from durable request hash (B1 §8/§14)"
                )
        if entry is not None and entry["status"] == "duplicate":
            original = state.bindings.get(entry["node_address"])
            if (
                not isinstance(original, dict)
                or derive_dedup_key(request) != original["dedup_key"]
            ):
                raise HtError(
                    "duplicate request bytes differ from durable dedup identity (B1 §8/§14)"
                )
        facts.append(
            RequestBootFact(
                request_id=request_id,
                path=path,
                canonical_bytes=data,
                sha256=digest,
                value=request,
                durable_status=entry["status"] if entry is not None else None,
            )
        )
    return tuple(facts)


def _inspect_control_request_facts(
    estate: Path,
    state: ReplayState,
    operations: dict[Path, str],
) -> tuple[ControlRequestBootFact, ...]:
    """Validate every submitted control and bind indexed controls to WAL truth."""

    facts: list[ControlRequestBootFact] = []
    control_root = estate / "control" / "requests"
    for path in visible_queue_paths(control_root, operations):
        control_id = _canonical_uuid_filename(path)
        inspection = inspect_operation_state(path, operation="publish")
        if not inspection.final_exists or inspection.final_bytes is None:
            raise HtError("control request final is absent (B1 §6/§15)")
        data = inspection.final_bytes
        value = strict_loads(data, label=path.name)
        validate("control-request.schema.json", value)
        if value["control_id"] != control_id or canonical_json_bytes(value) != data:
            raise HtError("control request filename or bytes are not canonical (B1 §15)")
        entry = state.control_index.get(control_id)
        if entry is not None and (
            value["target_daemon_incarnation_id"]
            != entry["target_daemon_incarnation_id"]
            or value["operation"] != "stop"
        ):
            raise HtError(
                "immutable control request differs from durable control truth (B1 §14/§15)"
            )
        facts.append(
            ControlRequestBootFact(
                control_id=control_id,
                path=path,
                canonical_bytes=data,
                sha256=hashlib.sha256(data).hexdigest(),
                value=value,
                durable_status=entry["status"] if entry is not None else None,
            )
        )
    return tuple(facts)


def _append_duplicate_response(
    estate: Path,
    state: ReplayState,
    request_id: str,
    original: dict[str, Any],
) -> ReplayState:
    state = _append(
        estate,
        state,
        "request_duplicate",
        request_id=request_id,
        original_request_id=original["request_id"],
        binding_id=original["binding_id"],
        lease_epoch=original["lease_epoch"],
        session_id=original["session_id"],
        packet_sha256=original["packet_sha256"],
    )
    _publish_response(estate, state, request_id)
    return state


def _require_frozen_planned_request(
    state: ReplayState,
    binding_id: str,
    request: dict[str, Any],
    request_bytes: bytes,
) -> dict[str, Any]:
    """Bind one complete immutable submission to its replayed planned work."""

    address = f"runtime/{binding_id}#synthetic"
    binding = state.bindings.get(address)
    request_id = request["request_id"]
    request_entry = state.request_index.get(request_id)
    digest = hashlib.sha256(request_bytes).hexdigest()
    if (
        not isinstance(binding, dict)
        or not isinstance(request_entry, dict)
        or binding.get("binding_id") != binding_id
        or binding.get("request_id") != request_id
        or binding.get("phase") != "planned"
        or binding.get("admission_status") != "pending"
        or binding.get("current_session_id") is not None
        or request_entry.get("status") != "planned"
        or request_entry.get("binding_id") != binding_id
        or request_entry.get("node_address") != address
        or request_entry.get("request_sha256") != digest
        or binding.get("request_sha256") != digest
        or binding.get("dedup_key") != derive_dedup_key(request)
        or binding.get("role") != request.get("role")
        or binding.get("attempt") != request.get("attempt")
        or binding.get("retry_lineage") != request.get("retry_lineage")
        or binding.get("work") != request.get("work")
        or canonical_json_bytes(request) != request_bytes
    ):
        raise HtError(
            "planned binding differs from its complete immutable request truth "
            "(B1 §8/§14)"
        )
    return binding


def _continue_planned_request(
    root: Path,
    estate: Path,
    state: ReplayState,
    binding_id: str,
    request: dict[str, Any],
    request_bytes: bytes,
    supervisors: dict[str, SessionSupervisor],
) -> ReplayState:
    """Revalidate and claim an existing durable planned binding exactly once."""

    request_id = request["request_id"]
    binding = _require_frozen_planned_request(
        state, binding_id, request, request_bytes
    )
    original = state.dedup_index.get(binding["dedup_key"])
    if isinstance(original, dict):
        raise HtError(
            "planned binding became shadowed by an accepted dedup owner (B1 §8/§14)"
        )
    try:
        current = revalidate_work(Root(root), WorkSnapshot(**request["work"]))
    except HtError:
        state = _append(
            estate,
            state,
            "request_rejected",
            request_id=request_id,
            binding_id=binding_id,
            reason_code="work-drift",
        )
        _publish_response(estate, state, request_id)
        return state

    # Identity allocation occurs only after the current source proof passes.
    session_id = str(uuid4())
    lease_epoch = binding["last_lease_epoch"] + 1
    wrapper_id = str(uuid4())
    helper_id = str(uuid4())
    packet, packet_bytes, packet_sha, launch, launch_bytes, launch_sha = construct_claim(
        runtime_id=state.runtime_id,
        request=request,
        binding_id=binding_id,
        lease_epoch=lease_epoch,
        session_id=session_id,
        admission_repository_commit=current.submission_repository_commit,
        wrapper_instance_id=wrapper_id,
        helper_instance_id=helper_id,
        packet_created_at=_now(),
    )
    state = _append(
        estate,
        state,
        "work_claimed",
        request_id=request_id,
        binding_id=binding_id,
        lease_epoch=lease_epoch,
        session_id=session_id,
        admission_repository_commit=current.submission_repository_commit,
        packet=packet,
        packet_canonical_json=packet_bytes.decode("utf-8"),
        packet_sha256=packet_sha,
        launch=launch,
        launch_canonical_json=launch_bytes.decode("utf-8"),
        launch_sha256=launch_sha,
    )
    session_path = estate / "sessions" / session_id
    make_directory(session_path)
    publish_immutable(session_path / "packet.json", packet_bytes)
    publish_immutable(session_path / "launch.json", launch_bytes)
    custody_fd = create_custody(session_path / "custody.lock")
    state = _append(
        estate,
        state,
        "session_starting",
        request_id=request_id,
        binding_id=binding_id,
        lease_epoch=lease_epoch,
        session_id=session_id,
    )
    state = _append(
        estate,
        state,
        "request_accepted",
        request_id=request_id,
        binding_id=binding_id,
        lease_epoch=lease_epoch,
        session_id=session_id,
        packet_sha256=packet_sha,
        recovery_created=False,
    )
    # This exact immutable reread is the launch barrier.
    _publish_response(estate, state, request_id)
    try:
        wrapper = _popen_wrapper(
            wrapper_argv(root, session_id, custody_fd),
            **child_options(root, (custody_fd,)),
        )
    except OSError:
        os.close(custody_fd)
        return _append(
            estate,
            state,
            "session_terminal",
            request_id=request_id,
            binding_id=binding_id,
            lease_epoch=lease_epoch,
            session_id=session_id,
            outcome="crashed",
            reason_code="popen-failed",
            started_sha256=None,
            ready_sha256=None,
            result_sha256=None,
            terminal_sha256=None,
            process_exit_sha256=None,
        )
    os.close(custody_fd)
    supervisors[session_id] = SessionSupervisor(process=wrapper, adopted=False)
    return state


def _claim_request(
    root: Path,
    estate: Path,
    state: ReplayState,
    request: dict[str, Any],
    request_bytes: bytes,
    supervisors: dict[str, SessionSupervisor],
) -> ReplayState:
    """Plan unseen work, then enter the shared planned-binding continuation."""

    request_id = request["request_id"]
    dedup_key = derive_dedup_key(request)
    original = state.dedup_index.get(dedup_key)
    if isinstance(original, dict):
        return _append_duplicate_response(estate, state, request_id, original)

    if any(
        address != "runtime#kernel"
        and isinstance(binding, dict)
        and binding.get("dedup_key") == dedup_key
        and binding.get("admission_status") == "pending"
        for address, binding in state.bindings.items()
    ):
        # Boot resolves every replayed owner before unseen filenames.  Reaching
        # this branch would let a second submission race a durable planned key.
        raise HtError("unseen request is shadowed by pending planned work (B1 §8/§14)")

    binding_id = str(uuid4())
    state = _append(
        estate,
        state,
        "work_planned",
        request_id=request_id,
        request_sha256=hashlib.sha256(request_bytes).hexdigest(),
        binding_id=binding_id,
        dedup_key=dedup_key,
        request=request,
    )
    return _continue_planned_request(
        root,
        estate,
        state,
        binding_id,
        request,
        request_bytes,
        supervisors,
    )


_ARTIFACT_SCHEMAS = {
    "started": "started-receipt.schema.json",
    "ready": "ready-receipt.schema.json",
    "result": "result.schema.json",
    "terminal": "terminal-receipt.schema.json",
    "process-exit": "process-exit-receipt.schema.json",
}


def _artifact_fact(
    session_path: Path,
    name: str,
    work: dict[str, Any],
    session: dict[str, Any],
) -> ArtifactBootFact | None:
    path = session_path / f"{name}.json"
    inspection = inspect_operation_state(path, operation="publish")
    if not inspection.final_exists:
        return None
    if inspection.final_bytes is None:  # pragma: no cover - dataclass invariant
        raise HtError(f"{name} artifact inspection lacks final bytes (B1 §6)")
    data = inspection.final_bytes
    value = strict_loads(data, label=path.name)
    validate(_ARTIFACT_SCHEMAS[name], value)
    if canonical_json_bytes(value) != data:
        raise HtError(f"{name} artifact is not exact canonical JSON (B1 §5/§6)")
    expected = {
        "runtime_id": work["runtime_id"],
        "request_id": work["request_id"],
        "binding_id": work["binding_id"],
        "lease_epoch": session["lease_epoch"],
        "session_id": session["session_id"],
        "packet_sha256": session["packet_sha256"],
    }
    if any(value.get(key) != item for key, item in expected.items()):
        raise HtError(f"{name} artifact differs from durable fence (B1 §13)")
    if name in {"started", "process-exit"} and (
        value["wrapper_instance_id"] != session["wrapper_instance_id"]
        or value["helper_instance_id"] != session["helper_instance_id"]
    ):
        raise HtError(f"{name} artifact differs from durable process identity (B1 §13)")
    if (
        name in {"ready", "result", "terminal"}
        and value["helper_instance_id"] != session["helper_instance_id"]
    ):
        raise HtError(f"{name} artifact differs from durable helper identity (B1 §13)")
    return ArtifactBootFact(
        name=name,
        path=path,
        canonical_bytes=data,
        sha256=hashlib.sha256(data).hexdigest(),
        value=value,
    )


def _artifact(
    session_path: Path,
    name: str,
    work: dict[str, Any],
    session: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    """Compatibility shape for the live supervisor over centralized facts."""

    fact = _artifact_fact(session_path, name, work, session)
    return None if fact is None else (fact.value, fact.sha256)


def _session_context(
    estate: Path,
    state: ReplayState,
    session_id: str,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    Path,
    dict[str, ArtifactBootFact],
    bool,
]:
    """Observe one fenced session through exact artifacts and custody."""

    entry = state.session_index.get(session_id)
    if not isinstance(entry, dict):
        raise HtError("supervisor session is absent from replay (B1 §14)")
    work = state.bindings.get(entry["node_address"])
    if not isinstance(work, dict):
        raise HtError("supervisor binding is absent from replay (B1 §14)")
    session = work["sessions"].get(session_id)
    if not isinstance(session, dict):
        raise HtError("supervisor session differs from binding truth (B1 §14)")
    session_path = estate / "sessions" / session_id
    facts = _session_artifact_facts(session_path, work, session)
    _validate_artifact_coherence(facts, session)
    return (
        work,
        session,
        session_path,
        {fact.name: fact for fact in facts},
        custody_is_free(session_path / "custody.lock"),
    )


def _advance_session(
    estate: Path,
    state: ReplayState,
    session_id: str,
    artifacts: dict[str, ArtifactBootFact],
) -> ReplayState:
    """Advance starting to running from exact started+ready evidence."""

    entry = state.session_index[session_id]
    work = state.bindings[entry["node_address"]]
    session = work["sessions"][session_id]
    ready = artifacts.get("ready")
    if ready is None or session["lifecycle"] != "starting":
        return state
    started = artifacts.get("started")
    if started is None:
        raise HtError("helper ready exists before wrapper started receipt (B1 §13)")
    return _append(
        estate,
        state,
        "session_running",
        request_id=work["request_id"],
        binding_id=work["binding_id"],
        lease_epoch=session["lease_epoch"],
        session_id=session_id,
        started_sha256=started.sha256,
        ready_sha256=ready.sha256,
    )


def _closure_decision(
    artifacts: dict[str, ArtifactBootFact],
    *,
    process_status: int | None,
) -> tuple[str, str | None, dict[str, str | None]]:
    """Map a custody-free physical closure to the closed terminal vocabulary."""

    hashes = {
        "started_sha256": artifacts["started"].sha256
        if "started" in artifacts
        else None,
        "ready_sha256": artifacts["ready"].sha256
        if "ready" in artifacts
        else None,
        "result_sha256": artifacts["result"].sha256
        if "result" in artifacts
        else None,
        "terminal_sha256": artifacts["terminal"].sha256
        if "terminal" in artifacts
        else None,
        "process_exit_sha256": artifacts["process-exit"].sha256
        if "process-exit" in artifacts
        else None,
    }
    started = artifacts.get("started")
    ready = artifacts.get("ready")
    result = artifacts.get("result")
    terminal = artifacts.get("terminal")
    process_exit = artifacts.get("process-exit")
    if (
        started is not None
        and ready is not None
        and result is not None
        and terminal is not None
        and process_exit is not None
        and process_exit.value["wait_status"] == 0
        and process_status in {None, 0}
    ):
        return str(result.value["outcome"]), None, hashes
    if started is None:
        reason = "custody-free-incomplete-closure"
    elif process_exit is None:
        reason = "wrapper-exit-missing"
    elif ready is None or result is None or terminal is None:
        reason = "helper-closure-incomplete"
    else:
        reason = "process-exit-incoherent"
    return "crashed", reason, hashes


def _settle_session(
    estate: Path,
    state: ReplayState,
    session_id: str,
    artifacts: dict[str, ArtifactBootFact],
    *,
    process_status: int | None,
) -> ReplayState:
    """Append the one terminal transition authorized by custody-free evidence."""

    entry = state.session_index[session_id]
    work = state.bindings[entry["node_address"]]
    session = work["sessions"][session_id]
    outcome, reason, hashes = _closure_decision(
        artifacts,
        process_status=process_status,
    )
    return _append(
        estate,
        state,
        "session_terminal",
        request_id=work["request_id"],
        binding_id=work["binding_id"],
        lease_epoch=session["lease_epoch"],
        session_id=session_id,
        outcome=outcome,
        reason_code=reason,
        **hashes,
    )


def _pid_is_absent(pid: int) -> bool:
    """Return ESRCH as degradation evidence only; never as custody proof."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _supervise(
    estate: Path,
    state: ReplayState,
    supervisors: dict[str, SessionSupervisor],
) -> ReplayState:
    for session_id, supervisor in list(supervisors.items()):
        work, session, _path, artifacts, free = _session_context(
            estate, state, session_id
        )
        state = _advance_session(estate, state, session_id, artifacts)
        entry = state.session_index[session_id]
        work = state.bindings[entry["node_address"]]
        session = work["sessions"][session_id]
        process_status = (
            supervisor.process.poll() if supervisor.process is not None else None
        )
        if not free:
            started = artifacts.get("started")
            directly_known_exit = process_status is not None
            if (
                supervisor.adopted
                and started is not None
                and artifacts.get("process-exit") is None
            ):
                directly_known_exit = _pid_is_absent(started.value["wrapper_pid"])
            if (
                directly_known_exit
                and started is not None
                and artifacts.get("process-exit") is None
                and not session["degraded_recorded"]
            ):
                state = _append(
                    estate,
                    state,
                    "session_degraded",
                    request_id=work["request_id"],
                    binding_id=work["binding_id"],
                    lease_epoch=session["lease_epoch"],
                    session_id=session_id,
                    started_sha256=started.sha256,
                    reason_code="wrapper-exit-observed-custody-held",
                )
            continue
        state = _settle_session(
            estate,
            state,
            session_id,
            artifacts,
            process_status=process_status,
        )
        supervisors.pop(session_id)
    return state


def _control_response(entry: dict[str, Any]) -> dict[str, Any]:
    value = {
        "schema_version": "hypothesis-tree-runtime-control-response/1.0.0",
        "status": entry["status"],
        "control_id": entry["control_id"],
        "target_daemon_incarnation_id": entry["target_daemon_incarnation_id"],
    }
    if entry["status"] == "rejected":
        value["reason_code"] = entry["reason_code"]
    validate("control-response.schema.json", value)
    return value


def _boot_authorized_operations(estate: Path, state: ReplayState) -> dict[Path, str]:
    """Return exact durable-truth owners; names outside this map stay rogue."""

    return authorized_runtime_operations(estate, state)


def _inspect_response_facts(
    estate: Path,
    state: ReplayState,
) -> tuple[tuple[ResponseBootFact, ...], frozenset[str]]:
    facts: list[ResponseBootFact] = []
    accepted_at_entry: set[str] = set()
    for request_id, entry in sorted(state.request_index.items()):
        if entry["status"] == "planned":
            continue
        expected = canonical_json_bytes(_response_object(entry))
        path = estate / "responses" / f"{request_id}.json"
        inspection = inspect_operation_state(
            path,
            operation="publish",
            expected_final=expected,
        )
        if inspection.final_exists and entry["status"] == "accepted":
            accepted_at_entry.add(request_id)
        facts.append(
            ResponseBootFact(
                path=path,
                canonical_bytes=expected,
                status=entry["status"],
                existed_at_boot=inspection.final_exists,
            )
        )
    return tuple(facts), frozenset(accepted_at_entry)


def _inspect_control_response_facts(
    estate: Path,
    state: ReplayState,
) -> tuple[ResponseBootFact, ...]:
    facts: list[ResponseBootFact] = []
    for control_id, entry in sorted(state.control_index.items()):
        expected = canonical_json_bytes(_control_response(entry))
        path = estate / "control" / "responses" / f"{control_id}.json"
        inspection = inspect_operation_state(
            path,
            operation="publish",
            expected_final=expected,
        )
        facts.append(
            ResponseBootFact(
                path=path,
                canonical_bytes=expected,
                status=entry["status"],
                existed_at_boot=inspection.final_exists,
            )
        )
    return tuple(facts)


def _session_artifact_facts(
    session_path: Path,
    work: dict[str, Any],
    session: dict[str, Any],
) -> tuple[ArtifactBootFact, ...]:
    observed: list[ArtifactBootFact] = []
    for name in _ARTIFACT_SCHEMAS:
        fact = _artifact_fact(session_path, name, work, session)
        if fact is not None:
            observed.append(fact)
    return tuple(observed)


def _validate_artifact_coherence(
    facts: tuple[ArtifactBootFact, ...],
    session: dict[str, Any],
) -> None:
    """Fence process evidence into one cross-coherent immutable fact set."""

    by_name = {fact.name: fact for fact in facts}
    replay_hash_fields = {
        "started": "started_sha256",
        "ready": "ready_sha256",
        "result": "result_sha256",
        "terminal": "terminal_sha256",
        "process-exit": "process_exit_sha256",
    }
    for name, field in replay_hash_fields.items():
        fact = by_name.get(name)
        durable_hash = session[field]
        if durable_hash is not None and fact is None:
            raise HtError(
                f"{name} durable hash lacks physical artifact (B1 §13/§14)"
            )
        if (
            durable_hash is not None
            and fact is not None
            and fact.sha256 != durable_hash
        ):
            raise HtError(
                f"{name} artifact hash differs from replay-frozen truth (B1 §13/§14)"
            )

    started = by_name.get("started")
    ready = by_name.get("ready")
    result = by_name.get("result")
    terminal = by_name.get("terminal")
    process_exit = by_name.get("process-exit")

    if started is not None:
        if started.value["wrapper_pid"] == started.value["helper_pid"]:
            raise HtError("started artifact collapses wrapper/helper PIDs (B1 §13)")
        if ready is not None and ready.value["helper_pid"] != started.value["helper_pid"]:
            raise HtError("ready artifact helper PID differs from started (B1 §13/§14)")
        if process_exit is not None and (
            process_exit.value["wrapper_pid"] != started.value["wrapper_pid"]
            or process_exit.value["helper_pid"] != started.value["helper_pid"]
        ):
            raise HtError("process-exit PIDs differ from started (B1 §13/§14)")

    if terminal is not None:
        if result is None:
            raise HtError("terminal artifact lacks its result artifact (B1 §13/§14)")
        if (
            terminal.value["result_sha256"] != result.sha256
            or terminal.value["outcome"] != result.value["outcome"]
        ):
            raise HtError("terminal artifact differs from result truth (B1 §13/§14)")

    if process_exit is not None:
        expected_result = result.sha256 if result is not None else None
        expected_terminal = terminal.sha256 if terminal is not None else None
        if (
            process_exit.value["result_sha256"] != expected_result
            or process_exit.value["terminal_sha256"] != expected_terminal
        ):
            raise HtError(
                "process-exit closure hashes differ from observed artifacts (B1 §13/§14)"
            )


def _inspect_session_facts(
    estate: Path,
    state: ReplayState,
    accepted_at_entry: frozenset[str],
) -> tuple[SessionBootFact, ...]:
    provisional: list[
        tuple[
            str,
            Path,
            str,
            bool,
            bool,
            bool,
            bool,
            tuple[str, ...],
            tuple[ArtifactBootFact, ...],
            bool,
        ]
    ] = []
    for session_id, entry in sorted(state.session_index.items()):
        work = state.bindings[entry["node_address"]]
        session = work["sessions"][session_id]
        lifecycle = session["lifecycle"]
        session_path = estate / "sessions" / session_id
        directory_exists = session_path.exists() or session_path.is_symlink()
        packet_exists = False
        launch_exists = False
        custody_exists = False
        process_artifacts: tuple[str, ...] = ()
        artifacts: tuple[ArtifactBootFact, ...] = ()
        if directory_exists:
            packet_path = session_path / "packet.json"
            launch_path = session_path / "launch.json"
            if lifecycle == "claimed":
                packet_inspection = inspect_operation_state(
                    packet_path,
                    operation="publish",
                    expected_final=session["packet_canonical_json"].encode("utf-8"),
                )
                launch_inspection = inspect_operation_state(
                    launch_path,
                    operation="publish",
                    expected_final=session["launch_canonical_json"].encode("utf-8"),
                )
                packet_exists = packet_inspection.final_exists
                launch_exists = launch_inspection.final_exists
                if packet_inspection.temporary_names and launch_inspection.temporary_names:
                    raise HtError(
                        "claimed session has overlapping packet/launch publication phases "
                        "(B1 §12/§14)"
                    )
                if launch_inspection.temporary_names and not packet_exists:
                    raise HtError(
                        "claimed launch publication precedes complete packet (B1 §12/§14)"
                    )
                if packet_inspection.temporary_names and launch_exists:
                    raise HtError(
                        "claimed packet publication temp survives beyond launch (B1 §12/§14)"
                    )
            else:
                packet_exists = packet_path.exists() or packet_path.is_symlink()
                launch_exists = launch_path.exists() or launch_path.is_symlink()
                if not packet_exists or not launch_exists:
                    raise HtError(
                        "starting-or-later session lacks frozen preparation (B1 §12/§14)"
                    )
                if read_exact_file(packet_path) != session["packet_canonical_json"].encode("utf-8"):
                    raise HtError("session packet differs from durable claim truth (B1 §12/§14)")
                if read_exact_file(launch_path) != session["launch_canonical_json"].encode("utf-8"):
                    raise HtError("session launch differs from durable claim truth (B1 §12/§14)")
            custody_path = session_path / "custody.lock"
            custody_exists = custody_path.exists() or custody_path.is_symlink()
            artifacts = _session_artifact_facts(session_path, work, session)
            _validate_artifact_coherence(artifacts, session)
            process_artifacts = tuple(fact.name for fact in artifacts)

        if lifecycle != "claimed" and not directory_exists:
            raise HtError("starting-or-later session directory is absent (B1 §14)")
        if lifecycle != "claimed" and not custody_exists:
            raise HtError("starting-or-later session custody is absent (B1 §14)")

        request_entry = state.request_index[work["request_id"]]
        accepted = request_entry["status"] == "accepted"
        accepted_missing_at_entry = accepted and work["request_id"] not in accepted_at_entry
        recovery_created = accepted and request_entry["recovery_created"] is True
        starting_unaccepted = lifecycle in {"starting", "running"} and not accepted
        must_be_process_free = (
            starting_unaccepted or accepted_missing_at_entry or recovery_created
        )
        if must_be_process_free and process_artifacts:
            raise HtError(
                "no-process recovery boundary has child-owned artifacts (B1 addendum §5/§14)"
            )
        provisional.append(
            (
                session_id,
                session_path,
                lifecycle,
                directory_exists,
                packet_exists,
                launch_exists,
                custody_exists,
                process_artifacts,
                artifacts,
                must_be_process_free,
            )
        )

    facts: list[SessionBootFact] = []
    for (
        session_id,
        session_path,
        lifecycle,
        directory_exists,
        packet_exists,
        launch_exists,
        custody_exists,
        process_artifacts,
        artifacts,
        must_be_process_free,
    ) in provisional:
        free: bool | None = None
        # Every existing custody lock is observed at boot entry.  Claimed and
        # no-process boundaries additionally require it to be free before any
        # repair; an ordinary live-created accepted session retains held/free
        # as classification evidence for R3.
        if custody_exists:
            free = custody_is_free(session_path / "custody.lock")
            if not free and (lifecycle == "claimed" or must_be_process_free):
                raise HtError("boot recovery custody is held at a no-process boundary (B1 §14)")
        facts.append(
            SessionBootFact(
                session_id=session_id,
                path=session_path,
                lifecycle=lifecycle,
                directory_exists=directory_exists,
                packet_exists=packet_exists,
                launch_exists=launch_exists,
                custody_exists=custody_exists,
                custody_free=free,
                process_artifacts=process_artifacts,
                artifacts=artifacts,
            )
        )
    return tuple(facts)


def _scan_boot(
    estate: Path,
    runtime_id: str,
    gate: RuntimeGate | None = None,
) -> BootFacts:
    """Capture all §14 eligibility/physical facts without mutating the estate."""

    gate = gate or inspect_runtime_gate(estate, runtime_id)
    parsed = parse_bytes(read_exact_file(estate / "run-ledger.jsonl"))
    eligibility = projection_eligibility(
        parsed,
        runtime_id,
        read_exact_file(estate / "checkpoint.json"),
        read_exact_file(estate / "binding-ledger.json"),
        upgrade=gate.upgrade,
    )
    state = eligibility.full_state
    operations = _boot_authorized_operations(estate, state)
    validate_runtime_inventory(
        estate,
        state,
        authorized_operations=operations,
        capability_state=gate.capability,
    )
    request_facts = _inspect_request_facts(estate, state, parsed, operations)
    control_request_facts = _inspect_control_request_facts(
        estate,
        state,
        operations,
    )
    response_facts, accepted_at_entry = _inspect_response_facts(estate, state)
    control_facts = _inspect_control_response_facts(estate, state)
    session_facts = _inspect_session_facts(estate, state, accepted_at_entry)
    inspections: list[OperationInspection] = []
    for path, operation in sorted(operations.items(), key=lambda item: str(item[0])):
        if path.parent.exists():
            inspections.append(inspect_operation_state(path, operation=operation))
    return BootFacts(
        parsed=parsed,
        eligibility=eligibility,
        gate=gate,
        operation_inspections=tuple(inspections),
        request_facts=request_facts,
        control_request_facts=control_request_facts,
        response_facts=response_facts,
        control_response_facts=control_facts,
        session_facts=session_facts,
        accepted_responses_present_at_entry=accepted_at_entry,
    )


def _operation_inspection(facts: BootFacts, path: Path) -> OperationInspection | None:
    return next(
        (inspection for inspection in facts.operation_inspections if inspection.path == path),
        None,
    )


def _recover_boot_storage(
    estate: Path,
    facts: BootFacts,
    *,
    repair_accepted_lifecycles: frozenset[str] = frozenset({"terminal"}),
) -> ReplayState:
    """Apply only owner-scoped storage repair after the complete scan passes.

    Accepted responses at a nonterminal boundary are deliberately excluded
    unless the lifecycle classifier authorizes that boundary explicitly.  R2
    can therefore repair ordinary dispositions and completed sessions without
    destroying the boot-entry absence fact that R3 needs to repair and
    terminalize a starting/running session as one coherent recovery action.
    """

    invalid_lifecycles = repair_accepted_lifecycles - {
        "starting",
        "running",
        "terminal",
    }
    if invalid_lifecycles:
        raise ValueError(
            "invalid accepted-response repair lifecycle(s): "
            f"{sorted(invalid_lifecycles)}"
        )

    eligibility = facts.eligibility
    if facts.parsed.tail is not None:
        target = recover_tolerated_tail(
            estate,
            facts.parsed,
            eligibility.full_state.runtime_id,
            timestamp=_now(),
            upgrade=eligibility.full_state.upgrade,
        )
    else:
        target = eligibility.full_state
        wal_path = estate / "run-ledger.jsonl"
        wal_inspection = _operation_inspection(facts, wal_path)
        if wal_inspection is not None and wal_inspection.temporary_names:
            replace_file(wal_path, target.clean_prefix, expected_old=target.clean_prefix)
        projection_temps = any(
            inspection.temporary_names
            for inspection in facts.operation_inspections
            if inspection.path in {
                estate / "binding-ledger.json",
                estate / "checkpoint.json",
            }
        )
        if (
            eligibility.checkpoint_state.last_seq != target.last_seq
            or read_exact_file(estate / "checkpoint.json") != target.checkpoint_bytes()
            or read_exact_file(estate / "binding-ledger.json") != target.binding_bytes()
            or projection_temps
        ):
            publish_projections(
                estate,
                target,
                allowed_prior=(eligibility.checkpoint_state, target),
            )

    # A tail recovery already publishes both projections, but it may have
    # inherited an unrelated uncommitted projection temp from an earlier
    # owner attempt.  Re-entering the exact target operation cleans only that
    # named temp and re-proves the current bytes.
    if facts.parsed.tail is not None and any(
        inspection.temporary_names
        for inspection in facts.operation_inspections
        if inspection.path in {estate / "binding-ledger.json", estate / "checkpoint.json"}
    ):
        publish_projections(estate, target, allowed_prior=(target,))

    session_by_id = {fact.session_id: fact for fact in facts.session_facts}
    for session_id, entry in sorted(target.session_index.items()):
        if entry["lifecycle"] != "claimed":
            continue
        work = target.bindings[entry["node_address"]]
        session = work["sessions"][session_id]
        fact = session_by_id[session_id]
        if not fact.directory_exists:
            make_directory(fact.path)
        for filename, canonical_text in (
            ("packet.json", session["packet_canonical_json"]),
            ("launch.json", session["launch_canonical_json"]),
        ):
            path = fact.path / filename
            data = canonical_text.encode("utf-8")
            recover_immutable_publication(path, data)
            publish_immutable(path, data)
        ensure_custody_file(fact.path / "custody.lock")

    deferred_response_paths: set[Path] = set()
    for fact in facts.response_facts:
        if fact.status == "accepted":
            request_id = fact.path.stem
            request_entry = target.request_index[request_id]
            session = target.session_index[request_entry["session_id"]]
            if session["lifecycle"] not in repair_accepted_lifecycles:
                deferred_response_paths.add(fact.path)
                continue
        inspection = _operation_inspection(facts, fact.path)
        if (
            not fact.existed_at_boot
            or (inspection is not None and inspection.temporary_names)
        ):
            recover_immutable_publication(fact.path, fact.canonical_bytes)
            publish_immutable(fact.path, fact.canonical_bytes)

    # Control and non-accepted admission responses are never launch/adoption
    # barriers and remain fully owned by the R2 storage-reconciliation slice.
    for fact in facts.control_response_facts:
        inspection = _operation_inspection(facts, fact.path)
        if (
            not fact.existed_at_boot
            or (inspection is not None and inspection.temporary_names)
        ):
            recover_immutable_publication(fact.path, fact.canonical_bytes)
            publish_immutable(fact.path, fact.canonical_bytes)

    completed_gate = inspect_runtime_gate(estate, target.runtime_id)
    if completed_gate.upgrade != target.upgrade:
        raise HtError("runtime capability changed during boot recovery (B2 §3.1/§16)")
    validate_runtime_inventory(
        estate,
        target,
        authorized_operations={path: "publish" for path in deferred_response_paths},
        capability_state=completed_gate.capability,
    )
    return target


def _complete_accepted_response(
    estate: Path,
    state: ReplayState,
    request_id: str,
) -> None:
    """Complete one exact event-derived accepted-response publication."""

    entry = state.request_index.get(request_id)
    if not isinstance(entry, dict) or entry.get("status") != "accepted":
        raise HtError("response completion requires accepted replay truth (B1 §8/§14)")
    path = estate / "responses" / f"{request_id}.json"
    data = canonical_json_bytes(_response_object(entry))
    recover_immutable_publication(path, data)
    publish_immutable(path, data)


def _require_no_process_boundary(
    estate: Path,
    state: ReplayState,
    session_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-prove free custody and absence of every child-owned artifact."""

    work, session, _path, artifacts, free = _session_context(
        estate, state, session_id
    )
    if not free or artifacts:
        raise HtError(
            "no-process recovery boundary gained custody or process evidence "
            "(B1 addendum §5/§14)"
        )
    return work, session


def _terminalize_no_process(
    estate: Path,
    state: ReplayState,
    session_id: str,
    *,
    reason_code: str,
) -> ReplayState:
    work, session = _require_no_process_boundary(estate, state, session_id)
    return _append(
        estate,
        state,
        "session_terminal",
        request_id=work["request_id"],
        binding_id=work["binding_id"],
        lease_epoch=session["lease_epoch"],
        session_id=session_id,
        outcome="crashed",
        reason_code=reason_code,
        started_sha256=None,
        ready_sha256=None,
        result_sha256=None,
        terminal_sha256=None,
        process_exit_sha256=None,
    )


def classify_boot_sessions(
    estate: Path,
    state: ReplayState,
    facts: BootFacts,
    daemon_incarnation_id: str,
    supervisors: dict[str, SessionSupervisor],
) -> ReplayState:
    """Classify every boot-entry starting/running session exactly once."""

    for fact in sorted(facts.session_facts, key=lambda item: item.session_id):
        if fact.lifecycle not in {"starting", "running"}:
            continue
        entry = state.session_index.get(fact.session_id)
        if not isinstance(entry, dict) or entry.get("lifecycle") != fact.lifecycle:
            raise HtError("boot lifecycle differs from immutable session facts (B1 §14)")
        work = state.bindings[entry["node_address"]]
        request_id = work["request_id"]
        request_entry = state.request_index[request_id]

        if request_entry["status"] == "planned":
            if fact.lifecycle != "starting":
                raise HtError("running session lacks durable acceptance (B1 §14)")
            _require_no_process_boundary(estate, state, fact.session_id)
            state = _append(
                estate,
                state,
                "request_accepted",
                request_id=request_id,
                binding_id=work["binding_id"],
                lease_epoch=entry["lease_epoch"],
                session_id=fact.session_id,
                packet_sha256=entry["packet_sha256"],
                recovery_created=True,
            )
            _complete_accepted_response(estate, state, request_id)
            state = _terminalize_no_process(
                estate,
                state,
                fact.session_id,
                reason_code="starting-recovery-no-process",
            )
            continue
        if request_entry["status"] != "accepted":
            raise HtError("started session has non-accepted disposition (B1 §14)")

        response_preexisted = (
            request_id in facts.accepted_responses_present_at_entry
        )
        if not response_preexisted:
            _require_no_process_boundary(estate, state, fact.session_id)
            _complete_accepted_response(estate, state, request_id)
            state = _terminalize_no_process(
                estate,
                state,
                fact.session_id,
                reason_code="accepted-response-missing-at-boot",
            )
            continue
        if request_entry["recovery_created"] is True:
            _require_no_process_boundary(estate, state, fact.session_id)
            _complete_accepted_response(estate, state, request_id)
            state = _terminalize_no_process(
                estate,
                state,
                fact.session_id,
                reason_code="starting-recovery-no-process",
            )
            continue
        if request_entry["recovery_created"] is not False:
            raise HtError("accepted request lacks Boolean recovery provenance (B1 §14)")

        _complete_accepted_response(estate, state, request_id)
        _work, _session, _path, artifacts, free = _session_context(
            estate, state, fact.session_id
        )
        if fact.custody_free is True and not free:
            raise HtError("session custody became held after a free boot probe (B1 §14)")
        state = _advance_session(estate, state, fact.session_id, artifacts)
        if free:
            state = _settle_session(
                estate,
                state,
                fact.session_id,
                artifacts,
                process_status=None,
            )
            continue
        state = _append(
            estate,
            state,
            "daemon_adopted_session",
            daemon_incarnation_id=daemon_incarnation_id,
            binding_id=work["binding_id"],
            lease_epoch=entry["lease_epoch"],
            session_id=fact.session_id,
        )
        supervisors[fact.session_id] = SessionSupervisor(
            process=None,
            adopted=True,
        )
    return state


def _boot_request_fact(
    facts: BootFacts,
    request_id: str,
) -> RequestBootFact:
    matches = [fact for fact in facts.request_facts if fact.request_id == request_id]
    if len(matches) != 1:
        raise HtError("replayed planned work lacks one boot-frozen request (B1 §8/§14)")
    fact = matches[0]
    if fact.durable_status != "planned":
        raise HtError("boot request disposition differs from planned recovery (B1 §8/§14)")
    return fact


def reconcile_boot_once(
    root: Path,
    estate: Path,
    state: ReplayState,
    facts: BootFacts,
    supervisors: dict[str, SessionSupervisor],
) -> ReplayState:
    """Complete the R2 claimed rollback and planned-binding recovery phase.

    ``facts`` must be the original `_scan_boot` result used by
    `_recover_boot_storage`; this routine intentionally performs no second
    inventory/replay scan after owner repair.
    """

    # First abandon every pre-start claim.  Preparation was reconstructed only
    # from WAL-frozen bytes by `_recover_boot_storage`.  The exact custody probe
    # is repeated immediately before the rollback append and unlocks at once.
    claimed: list[tuple[str, str, str]] = []
    for session_id, entry in state.session_index.items():
        if entry["lifecycle"] != "claimed":
            continue
        work = state.bindings[entry["node_address"]]
        claimed.append((work["request_id"], work["binding_id"], session_id))
    boot_sessions = {fact.session_id: fact for fact in facts.session_facts}
    for request_id, binding_id, session_id in sorted(claimed):
        boot_fact = boot_sessions.get(session_id)
        if boot_fact is None or boot_fact.lifecycle != "claimed":
            raise HtError("claimed recovery differs from boot-frozen session truth (B1 §14)")
        custody_path = estate / "sessions" / session_id / "custody.lock"
        if not custody_is_free(custody_path):
            raise HtError("boot recovery custody became held before rollback (B1 §14)")
        current = state.bindings[f"runtime/{binding_id}#synthetic"]
        session = current["sessions"][session_id]
        state = _append(
            estate,
            state,
            "claim_rolled_back",
            request_id=request_id,
            binding_id=binding_id,
            lease_epoch=session["lease_epoch"],
            session_id=session_id,
            reason_code="boot-recovery-pre-start",
        )

    # Only now resume every replayed planned owner.  Sorting by immutable
    # request filename identity makes this phase deterministic and it finishes
    # before `_process_requests` can enumerate any unseen request.
    planned = sorted(
        (
            binding["request_id"],
            binding["binding_id"],
        )
        for address, binding in state.bindings.items()
        if address != "runtime#kernel"
        and isinstance(binding, dict)
        and binding["phase"] == "planned"
        and binding["admission_status"] == "pending"
    )
    for request_id, binding_id in planned:
        # B1 scheduler capacity is one.  A live wrapper keeps later recovered
        # owners durably planned; subsequent loop iterations return here before
        # any unseen request-directory enumeration.
        if supervisors:
            break
        fact = _boot_request_fact(facts, request_id)
        state = _continue_planned_request(
            root,
            estate,
            state,
            binding_id,
            fact.value,
            fact.canonical_bytes,
            supervisors,
        )
    return state


def _process_controls(estate: Path, state: ReplayState) -> tuple[ReplayState, str | None]:
    operations = authorized_runtime_operations(estate, state)
    for path in visible_queue_paths(
        estate / "control" / "requests",
        operations,
    ):
        control_id = _canonical_uuid_filename(path)
        if control_id in state.control_index:
            continue
        inspection = inspect_operation_state(path, operation="publish")
        if not inspection.final_exists or inspection.final_bytes is None:
            raise HtError("control request final is absent (B1 §6/§15)")
        data = inspection.final_bytes
        request = strict_loads(data, label=path.name)
        validate("control-request.schema.json", request)
        if request["control_id"] != control_id or canonical_json_bytes(request) != data:
            raise HtError("control request filename/bytes differ (B1 §15)")
        current = state.bindings["runtime#kernel"]["daemon_incarnation_id"]
        accepted = request["target_daemon_incarnation_id"] == current
        fields: dict[str, Any] = {
            "control_id": control_id,
            "target_daemon_incarnation_id": request["target_daemon_incarnation_id"],
        }
        if not accepted:
            fields["reason_code"] = "stale-daemon-incarnation"
        state = _append(
            estate,
            state,
            "control_stop_accepted" if accepted else "control_stop_rejected",
            **fields,
        )
        response = _control_response(state.control_index[control_id])
        publish_immutable(
            estate / "control" / "responses" / f"{control_id}.json",
            canonical_json_bytes(response),
        )
        if accepted:
            return state, control_id
    return state, None


def _process_requests(
    root: Path,
    estate: Path,
    state: ReplayState,
    supervisors: dict[str, SessionSupervisor],
) -> ReplayState:
    if supervisors:
        return state
    operations = authorized_runtime_operations(estate, state)
    for path in visible_queue_paths(estate / "requests", operations):
        request_id = _canonical_uuid_filename(path)
        if request_id in state.request_index:
            continue
        if state.upgrade is not None:
            raise HtError(
                "role-runtime-upgraded: unrecorded B1 synthetic request is forbidden "
                "after role activation (B2 §16)"
            )
        request, request_bytes = _load_request(path)
        return _claim_request(root, estate, state, request, request_bytes, supervisors)
    return state


def _write_readiness(fd: int, value: dict[str, Any]) -> None:
    data = canonical_json_bytes(value)
    if len(data) > 4096:
        raise HtError("runtime readiness exceeds fixed transport bound (B1 addendum §3)")
    try:
        offset = 0
        while offset < len(data):
            count = os.write(fd, data[offset:])
            if count <= 0:
                raise OSError("short readiness write")
            offset += count
    finally:
        os.close(fd)


def run(root: Path, readiness_fd: int) -> int:
    root = root.resolve()
    estate = root / "var" / "runtime"
    instance_fd: int | None = None
    ready_sent = False
    supervisors: dict[str, SessionSupervisor] = {}
    try:
        # State-free inventory validation is read-only and precedes even the
        # instance-lock decision, matching the genesis-first boot boundary.
        descriptor = _descriptor(estate, root)
        gate = inspect_runtime_gate(
            estate,
            descriptor["runtime_id"],
            allow_live_b1_operation_temps=True,
        )
        validate_runtime_inventory(
            estate,
            include_dynamic=False,
            capability_state=gate.capability,
            runtime_id=descriptor["runtime_id"],
        )
        instance_fd = try_instance_lock(estate / ".ht-runtime.instance.lock")
        if instance_fd is None:
            _write_readiness(
                readiness_fd,
                {
                    "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
                    "status": "already-running",
                },
            )
            ready_sent = True
            return 0
        with audit_lock(estate / ".harnessd.lock", exclusive=True):
            descriptor = _descriptor(estate, root)
            gate = inspect_runtime_gate(estate, descriptor["runtime_id"])
            state, boot_facts = _prepare_boot(
                estate,
                descriptor["runtime_id"],
                gate=gate,
            )
            incarnation = str(uuid4())
            state = _append(
                estate,
                state,
                "daemon_started",
                daemon_incarnation_id=incarnation,
            )
            state = classify_boot_sessions(
                estate,
                state,
                boot_facts,
                incarnation,
                supervisors,
            )
            state = reconcile_boot_once(
                root,
                estate,
                state,
                boot_facts,
                supervisors,
            )
        _write_readiness(
            readiness_fd,
            {
                "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
                "status": "ready",
                "daemon_incarnation_id": incarnation,
                "pid": os.getpid(),
            },
        )
        ready_sent = True
        while True:
            should_stop: str | None = None
            with audit_lock(estate / ".harnessd.lock", exclusive=True):
                descriptor = _descriptor(estate, root)
                gate = inspect_runtime_gate(estate, descriptor["runtime_id"])
                parsed = parse_bytes(read_exact_file(estate / "run-ledger.jsonl"))
                state = projection_eligibility(
                    parsed,
                    descriptor["runtime_id"],
                    read_exact_file(estate / "checkpoint.json"),
                    read_exact_file(estate / "binding-ledger.json"),
                    upgrade=gate.upgrade,
                ).full_state
                validate_runtime_inventory(
                    estate,
                    state,
                    capability_state=gate.capability,
                )
                state = _supervise(estate, state, supervisors)
                state, should_stop = _process_controls(estate, state)
                if should_stop is None:
                    state = reconcile_boot_once(
                        root,
                        estate,
                        state,
                        boot_facts,
                        supervisors,
                    )
                    pending_recovery = any(
                        address != "runtime#kernel"
                        and isinstance(binding, dict)
                        and binding["phase"] == "planned"
                        and binding["admission_status"] == "pending"
                        for address, binding in state.bindings.items()
                    )
                    if not pending_recovery:
                        state = _process_requests(root, estate, state, supervisors)
                else:
                    state = _append(
                        estate,
                        state,
                        "daemon_stopped",
                        daemon_incarnation_id=incarnation,
                        control_id=should_stop,
                    )
            if should_stop is not None:
                return 0
            time.sleep(POLL_SECONDS)
    except Exception as exc:
        if not ready_sent:
            _write_readiness(
                readiness_fd,
                {
                    "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
                    "status": "failed",
                    "reason_code": (
                        ROLE_INIT_REQUIRED
                        if is_role_init_required(exc)
                        else "recovery-failed"
                    ),
                },
            )
        return 2
    finally:
        if instance_fd is not None:
            os.close(instance_fd)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", required=True)
    parser.add_argument("--readiness-fd", required=True, type=int)
    args = parser.parse_args()
    return run(Path(args.root), args.readiness_fd)


if __name__ == "__main__":
    raise SystemExit(main())
