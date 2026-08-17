"""Closed full-WAL reducer and checkpoint-last projection reconciliation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import re
from pathlib import Path
from typing import Any

from ht.errors import HtError

from .atomic import read_exact_file, replace_file
from .capability import CanonicalDocument
from .schema import canonical_json_bytes, strict_loads, validate
from .state import (
    CONTROL_INDEX_FIELDS,
    DEDUP_INDEX_FIELDS,
    KERNEL_ADDRESS,
    REQUEST_INDEX_FIELDS,
    SESSION_INDEX_FIELDS,
    TERMINAL_OUTCOMES,
    UpgradeContext,
    binding_ledger_bytes,
    checkpoint,
    derive_dedup_key,
    genesis_bindings,
    object_sha256,
    role_checkpoint,
    validate_binding_map,
    validate_indexes,
)
from .wal import ParsedWal, frame_record, parse_bytes


INDEX_NAMES = ("request_index", "dedup_index", "session_index", "control_index")
_POST_UPGRADE_B1_EVENTS = frozenset(
    {
        "daemon_started",
        "daemon_stopped",
        "control_stop_accepted",
        "control_stop_rejected",
        "wal_tail_truncated",
    }
)
_WORK_ADDRESS = re.compile(
    r"runtime/(?P<binding>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})#synthetic",
    re.ASCII,
)

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.ASCII,
)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_REASON = re.compile(r"[a-z0-9-]+", re.ASCII)
_STABLE_REASONS = frozenset(
    {
        "work-drift",
        "boot-recovery-pre-start",
        "starting-recovery-no-process",
        "accepted-response-missing-at-boot",
        "popen-failed",
        "custody-free-incomplete-closure",
        "wrapper-exit-missing",
        "helper-closure-incomplete",
        "process-exit-incoherent",
        "wrapper-exit-observed-custody-held",
        "stale-daemon-incarnation",
        "runtime-corrupt",
        "recovery-failed",
        "daemon-readiness-timeout",
        "wait-timeout",
    }
)


@dataclass(frozen=True)
class ReplayState:
    runtime_id: str
    last_seq: int
    clean_prefix: bytes
    bindings: dict[str, dict[str, Any]]
    request_index: dict[str, Any]
    dedup_index: dict[str, Any]
    session_index: dict[str, Any]
    control_index: dict[str, Any]
    final_tail: dict[str, Any] | None
    upgrade: UpgradeContext | None = None

    @property
    def clean_wal_sha256(self) -> str:
        return hashlib.sha256(self.clean_prefix).hexdigest()

    def binding_bytes(self) -> bytes:
        return binding_ledger_bytes(self.bindings)

    def checkpoint_object(self) -> dict[str, Any]:
        base = checkpoint(
            self.runtime_id,
            self.bindings,
            last_seq=self.last_seq,
            clean_wal_sha256=self.clean_wal_sha256,
            request_index=self.request_index,
            dedup_index=self.dedup_index,
            session_index=self.session_index,
            control_index=self.control_index,
            final_tail=self.final_tail,
        )
        if self.upgrade is not None:
            return role_checkpoint(base, self.upgrade)
        return base

    def checkpoint_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_object())


@dataclass(frozen=True)
class ProjectionEligibility:
    checkpoint_state: ReplayState
    full_state: ReplayState
    ledger_at_full_target: bool


def _uuid(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise HtError(f"{label} must be a canonical lowercase UUID (B1 addendum §1)")


def _sha(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise HtError(f"{label} must be a lowercase SHA-256 (B1 addendum §1)")


def _positive(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HtError(f"{label} must be an exact positive integer (B1 addendum §1)")


def _reason(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or _REASON.fullmatch(value) is None:
        raise HtError(f"{label} must be a stable reason code (B1 addendum §1)")


def _work_address(binding_id: Any) -> str:
    _uuid(binding_id, "binding_id")
    return f"runtime/{binding_id}#synthetic"


def _require_work_identity(record: dict[str, Any], work: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    address = _work_address(record["binding_id"])
    if work.get("node_address") != address:
        raise HtError("event binding ID differs from persistent work address (B1 addendum §1)")
    if record["request_id"] != work.get("request_id"):
        raise HtError("event request ID differs from persistent work binding (B1 addendum §1)")
    return address, work


def _require_session_identity(record: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    _positive(record["lease_epoch"], "lease_epoch")
    _uuid(record["session_id"], "session_id")
    if work.get("current_session_id") != record["session_id"]:
        raise HtError("event session is not the binding's current session (B1 addendum §1)")
    session = work.get("sessions", {}).get(record["session_id"])
    if not isinstance(session, dict):
        raise HtError("event session is absent from the persistent work binding (B1 addendum §1)")
    if session.get("lease_epoch") != record["lease_epoch"]:
        raise HtError("event lease differs from durable session fence (B1 addendum §1)")
    return session


def _kernel_target(
    prior: dict[str, Any], indexes: dict[str, dict[str, Any]], *, daemon: Any = ...
) -> dict[str, Any]:
    target = deepcopy(prior)
    if daemon is not ...:
        target["daemon_incarnation_id"] = daemon
    for name in INDEX_NAMES:
        stem = name.removesuffix("_index")
        target[f"{stem}_count"] = len(indexes[name])
        target[f"{name}_sha256"] = object_sha256(indexes[name])
    return target


def _request_entry(**updates: Any) -> dict[str, Any]:
    entry = {name: None for name in REQUEST_INDEX_FIELDS}
    entry.update(updates)
    return entry


def _session_entry(work: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    entry = {name: session[name] for name in SESSION_INDEX_FIELDS if name in session}
    entry.update(
        {
            "request_id": work["request_id"],
            "binding_id": work["binding_id"],
            "node_address": work["node_address"],
        }
    )
    if set(entry) != SESSION_INDEX_FIELDS:  # pragma: no cover - fixed inventories
        raise HtError("internal session index derivation is incomplete (B1 addendum §2)")
    return entry


def _validate_artifact(
    record: dict[str, Any], name: str, schema_name: str
) -> tuple[dict[str, Any], str, str]:
    value = record[name]
    canonical = record[f"{name}_canonical_json"]
    digest = record[f"{name}_sha256"]
    if not isinstance(value, dict) or not isinstance(canonical, str):
        raise HtError(f"{name} claim truth must be object plus canonical JSON (B1 addendum §1)")
    validate(schema_name, value)
    expected = canonical_json_bytes(value)
    try:
        supplied = canonical.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HtError(f"{name} canonical JSON is not UTF-8 encodable (B1 addendum §1)") from exc
    if supplied != expected:
        raise HtError(f"{name} canonical JSON differs from complete object (B1 addendum §1)")
    _sha(digest, f"{name} SHA-256")
    if hashlib.sha256(supplied).hexdigest() != digest:
        raise HtError(f"{name} canonical JSON hash differs from claim truth (B1 addendum §1)")
    return deepcopy(value), canonical, digest


def _derive_transition(
    runtime_id: str,
    bindings: dict[str, dict[str, Any]],
    indexes: dict[str, dict[str, Any]],
    record: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Derive the sole legal delta from prior state and event-origin fields."""

    event = record["event"]
    prior_kernel = bindings[KERNEL_ADDRESS]
    next_indexes = deepcopy(indexes)
    binding_images: dict[str, dict[str, Any]] = {}
    index_names: tuple[str, ...] = ()
    tail: dict[str, Any] | None = None

    if event == "wal_tail_truncated":
        if record["node_address"] != KERNEL_ADDRESS:
            raise HtError("wal_tail_truncated must be kernel-addressed (B1 addendum §1)")
        reason = record["tail_reason"]
        offset = record["tail_byte_offset"]
        count = record["tail_discarded_byte_count"]
        digest = record["tail_discarded_sha256"]
        if not isinstance(reason, str) or not reason:
            raise HtError("tail reason must be nonempty (B1 §10)")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise HtError("tail byte offset must be nonnegative (B1 §10)")
        _positive(count, "tail discarded byte count")
        _sha(digest, "tail discarded SHA-256")
        tail = {
            "reason": reason,
            "byte_offset": offset,
            "discarded_byte_count": count,
            "discarded_sha256": digest,
        }
    elif event == "daemon_started":
        if record["node_address"] != KERNEL_ADDRESS:
            raise HtError("daemon_started must be kernel-addressed (B1 addendum §1)")
        _uuid(record["daemon_incarnation_id"], "daemon incarnation")
        if prior_kernel["daemon_incarnation_id"] == record["daemon_incarnation_id"]:
            raise HtError("daemon_started requires a fresh daemon incarnation (B1 §14)")
        binding_images[KERNEL_ADDRESS] = _kernel_target(
            prior_kernel, next_indexes, daemon=record["daemon_incarnation_id"]
        )
    elif event == "daemon_stopped":
        if record["node_address"] != KERNEL_ADDRESS:
            raise HtError("daemon_stopped must be kernel-addressed (B1 addendum §1)")
        _uuid(record["daemon_incarnation_id"], "daemon incarnation")
        _uuid(record["control_id"], "control ID")
        control = indexes["control_index"].get(record["control_id"])
        if (
            prior_kernel["daemon_incarnation_id"] != record["daemon_incarnation_id"]
            or not isinstance(control, dict)
            or control.get("status") != "accepted"
            or control.get("target_daemon_incarnation_id") != record["daemon_incarnation_id"]
        ):
            raise HtError("daemon_stopped lacks its exact accepted control/current daemon (B1 addendum §1)")
        binding_images[KERNEL_ADDRESS] = _kernel_target(prior_kernel, next_indexes, daemon=None)
    elif event == "work_planned":
        address = _work_address(record["binding_id"])
        if record["node_address"] != address or address in bindings:
            raise HtError("work_planned must allocate its new named binding (B1 addendum §1)")
        _uuid(record["request_id"], "request ID")
        _sha(record["request_sha256"], "request SHA-256")
        _sha(record["dedup_key"], "dedup key")
        request = record["request"]
        if not isinstance(request, dict):
            raise HtError("work_planned request must be a complete object (B1 addendum §1)")
        validate("request.schema.json", request)
        if request["request_id"] != record["request_id"]:
            raise HtError("work_planned request ID differs from request object (B1 addendum §1)")
        if hashlib.sha256(canonical_json_bytes(request)).hexdigest() != record["request_sha256"]:
            raise HtError("work_planned request SHA differs from canonical request (B1 addendum §1)")
        if record["dedup_key"] != derive_dedup_key(request):
            raise HtError("work_planned dedup key differs from exact request identity (B1 §8)")
        if record["request_id"] in indexes["request_index"]:
            raise HtError("work_planned request already exists (B1 addendum §1)")
        if record["dedup_key"] in indexes["dedup_index"] or any(
            value.get("dedup_key") == record["dedup_key"]
            and value.get("admission_status") in {"pending", "accepted"}
            for key, value in bindings.items()
            if key != KERNEL_ADDRESS and isinstance(value, dict)
        ):
            raise HtError("work_planned duplicates pending or accepted work (B1 §8)")
        lineage = request["retry_lineage"]
        if request["attempt"] != len(lineage) + 1:
            raise HtError("retry attempt must equal complete lineage length plus one (B1 §7)")
        prior_by_request = {
            value.get("request_id"): value
            for key, value in bindings.items()
            if key != KERNEL_ADDRESS and isinstance(value, dict)
        }
        current_identity = {
            "role": request["role"],
            "work": {
                key: deepcopy(value)
                for key, value in request["work"].items()
                if key != "submission_repository_commit"
            },
        }
        for index, predecessor_id in enumerate(lineage):
            predecessor = prior_by_request.get(predecessor_id)
            if not isinstance(predecessor, dict):
                raise HtError("retry lineage predecessor is absent (B1 §7)")
            predecessor_identity = {
                "role": predecessor["role"],
                "work": {
                    key: deepcopy(value)
                    for key, value in predecessor["work"].items()
                    if key != "submission_repository_commit"
                },
            }
            if (
                predecessor["attempt"] != index + 1
                or predecessor["retry_lineage"] != lineage[:index]
                or predecessor["admission_status"] != "accepted"
                or predecessor["terminal_outcome"] not in {"FAILED", "crashed"}
                or predecessor_identity != current_identity
            ):
                raise HtError("retry lineage is not the complete failed/crashed identity chain (B1 §7)")
        work = {
            "node_address": address,
            "binding_kind": "synthetic",
            "runtime_id": runtime_id,
            "binding_id": record["binding_id"],
            "request_id": record["request_id"],
            "request_sha256": record["request_sha256"],
            "role": request["role"],
            "attempt": request["attempt"],
            "retry_lineage": deepcopy(request["retry_lineage"]),
            "work": deepcopy(request["work"]),
            "dedup_key": record["dedup_key"],
            "admission_status": "pending",
            "admission_repository_commit": None,
            "rejection_reason_code": None,
            "phase": "planned",
            "last_lease_epoch": 0,
            "current_session_id": None,
            "sessions": {},
            "terminal_outcome": None,
        }
        next_indexes["request_index"][record["request_id"]] = _request_entry(
            request_id=record["request_id"],
            request_sha256=record["request_sha256"],
            status="planned",
            binding_id=record["binding_id"],
            node_address=address,
        )
        index_names = ("request_index",)
        binding_images[address] = work
        binding_images[KERNEL_ADDRESS] = _kernel_target(prior_kernel, next_indexes)
    elif event == "work_claimed":
        address = _work_address(record["binding_id"])
        work = deepcopy(bindings.get(address))
        if not isinstance(work, dict):
            raise HtError("work_claimed names no persistent work binding (B1 addendum §1)")
        _require_work_identity(record, work)
        if record["node_address"] != address or work["phase"] != "planned" or work["admission_status"] != "pending":
            raise HtError("work_claimed requires pending planned work (B1 addendum §1)")
        _positive(record["lease_epoch"], "lease epoch")
        _uuid(record["session_id"], "session ID")
        if record["lease_epoch"] != work["last_lease_epoch"] + 1 or record["session_id"] in work["sessions"]:
            raise HtError("work_claimed must allocate the next lease and new session (B1 addendum §1)")
        admission_commit = record["admission_repository_commit"]
        if not isinstance(admission_commit, str) or _OID.fullmatch(admission_commit) is None:
            raise HtError("admission repository commit must be a Git OID (B1 addendum §1)")
        packet, packet_json, packet_sha = _validate_artifact(
            record, "packet", "session-packet.schema.json"
        )
        launch, launch_json, launch_sha = _validate_artifact(record, "launch", "launch.schema.json")
        identities = {
            "runtime_id": runtime_id,
            "request_id": work["request_id"],
            "binding_id": work["binding_id"],
            "node_address": address,
            "lease_epoch": record["lease_epoch"],
            "session_id": record["session_id"],
            "role": work["role"],
        }
        if any(packet.get(key) != value or launch.get(key) != value for key, value in identities.items()):
            raise HtError("claim packet/launch identity differs from durable work (B1 addendum §1)")
        if (
            packet["attempt"] != work["attempt"]
            or packet["retry_lineage"] != work["retry_lineage"]
            or packet["work"] != work["work"]
            or packet["admission_repository_commit"] != admission_commit
            or launch["packet_sha256"] != packet_sha
            or launch["wrapper_instance_id"] != packet["wrapper_instance_id"]
            or launch["helper_instance_id"] != packet["helper_instance_id"]
            or launch["packet_relative_path"] != f"sessions/{record['session_id']}/packet.json"
        ):
            raise HtError("claim artifacts disagree with frozen request/claim truth (B1 addendum §1)")
        supervisor = prior_kernel["daemon_incarnation_id"]
        _uuid(supervisor, "claim supervising daemon")
        if len({supervisor, packet["wrapper_instance_id"], packet["helper_instance_id"]}) != 3:
            raise HtError("daemon, wrapper, and helper instance IDs must be distinct (B1 §13)")
        session = {
            "session_id": record["session_id"],
            "lease_epoch": record["lease_epoch"],
            "fence": deepcopy(packet["fence"]),
            "lifecycle": "claimed",
            "outcome": None,
            "abandonment_reason_code": None,
            "terminal_reason_code": None,
            "packet": packet,
            "packet_canonical_json": packet_json,
            "packet_sha256": packet_sha,
            "launch": launch,
            "launch_canonical_json": launch_json,
            "launch_sha256": launch_sha,
            "wrapper_instance_id": packet["wrapper_instance_id"],
            "helper_instance_id": packet["helper_instance_id"],
            "supervising_daemon_incarnation_id": supervisor,
            "started_sha256": None,
            "ready_sha256": None,
            "result_sha256": None,
            "terminal_sha256": None,
            "process_exit_sha256": None,
            "degraded_recorded": False,
        }
        work["admission_repository_commit"] = admission_commit
        work["phase"] = "claimed"
        work["last_lease_epoch"] = record["lease_epoch"]
        work["current_session_id"] = record["session_id"]
        work["sessions"][record["session_id"]] = session
        next_indexes["session_index"][record["session_id"]] = _session_entry(work, session)
        index_names = ("session_index",)
        binding_images[address] = work
        binding_images[KERNEL_ADDRESS] = _kernel_target(prior_kernel, next_indexes)
    elif event in {
        "claim_rolled_back", "session_starting", "session_running", "session_degraded",
        "session_terminal", "request_accepted", "daemon_adopted_session",
    }:
        address = _work_address(record["binding_id"])
        work = deepcopy(bindings.get(address))
        if not isinstance(work, dict):
            raise HtError(f"{event} names no persistent work binding (B1 addendum §1)")
        if event == "daemon_adopted_session":
            if work.get("node_address") != address:
                raise HtError("adoption binding ID differs from persistent work address (B1 addendum §1)")
        else:
            _require_work_identity(record, work)
        session = _require_session_identity(record, work)
        if event == "claim_rolled_back":
            if record["node_address"] != address or record["reason_code"] != "boot-recovery-pre-start" or work["phase"] != "claimed" or session["lifecycle"] != "claimed":
                raise HtError("claim_rolled_back requires a claimed pre-start session (B1 addendum §1)")
            session["lifecycle"] = "abandoned"
            session["abandonment_reason_code"] = record["reason_code"]
            work["phase"] = "planned"
            work["current_session_id"] = None
        elif event == "session_starting":
            if record["node_address"] != address or work["phase"] != "claimed" or session["lifecycle"] != "claimed":
                raise HtError("session_starting requires the current claimed session (B1 addendum §1)")
            work["phase"] = "starting"
            session["lifecycle"] = "starting"
        elif event == "session_running":
            if record["node_address"] != address or work["phase"] != "starting" or work["admission_status"] != "accepted" or session["lifecycle"] != "starting":
                raise HtError("session_running requires an accepted starting session (B1 addendum §1)")
            _sha(record["started_sha256"], "started receipt hash")
            _sha(record["ready_sha256"], "ready receipt hash")
            if session["started_sha256"] not in {None, record["started_sha256"]}:
                raise HtError("session_running started hash conflicts with frozen truth (B1 addendum §4)")
            work["phase"] = "running"
            session["lifecycle"] = "running"
            session["started_sha256"] = record["started_sha256"]
            session["ready_sha256"] = record["ready_sha256"]
        elif event == "session_degraded":
            if (
                record["node_address"] != address
                or work["phase"] not in {"starting", "running"}
                or work["admission_status"] != "accepted"
                or session["lifecycle"] not in {"starting", "running"}
                or session["degraded_recorded"]
                or record["reason_code"] != "wrapper-exit-observed-custody-held"
            ):
                raise HtError("session_degraded requires one live accepted observation (B1 addendum §4)")
            _sha(record["started_sha256"], "started receipt hash")
            if session["started_sha256"] not in {None, record["started_sha256"]}:
                raise HtError("session_degraded started hash conflicts with prior truth (B1 addendum §4)")
            session["started_sha256"] = record["started_sha256"]
            session["degraded_recorded"] = True
        elif event == "session_terminal":
            if (
                record["node_address"] != address
                or work["admission_status"] != "accepted"
                or work["phase"] not in {"starting", "running"}
                or session["lifecycle"] not in {"starting", "running"}
            ):
                raise HtError("session_terminal requires the current started session (B1 addendum §1)")
            outcome = record["outcome"]
            if outcome not in TERMINAL_OUTCOMES:
                raise HtError("session terminal outcome is invalid (B1 addendum §1)")
            hashes = [record[name] for name in ("started_sha256", "ready_sha256", "result_sha256", "terminal_sha256", "process_exit_sha256")]
            for name, value in zip(("started", "ready", "result", "terminal", "process-exit"), hashes):
                _sha(value, f"{name} receipt hash", nullable=True)
            if outcome in {"SUCCEEDED", "FAILED"}:
                if (
                    work["phase"] != "running"
                    or session["lifecycle"] != "running"
                    or record["reason_code"] is not None
                    or any(value is None for value in hashes)
                ):
                    raise HtError("role terminal outcome requires five hashes and null reason (B1 addendum §1)")
            else:
                _reason(record["reason_code"], "crash reason")
                if record["reason_code"] not in _STABLE_REASONS:
                    raise HtError("crash reason is outside the closed vocabulary (B1 addendum §6)")
            work["phase"] = "terminal"
            work["terminal_outcome"] = outcome
            session["lifecycle"] = "terminal"
            session["outcome"] = outcome
            session["terminal_reason_code"] = record["reason_code"]
            for field in ("started_sha256", "ready_sha256", "result_sha256", "terminal_sha256", "process_exit_sha256"):
                prior = session[field]
                if prior is not None and prior != record[field]:
                    raise HtError(f"session_terminal {field} conflicts with prior truth (B1 addendum §1)")
                session[field] = record[field]
        elif event == "request_accepted":
            if (
                record["node_address"] != address
                or work["admission_status"] != "pending"
                or work["phase"] != "starting"
                or session["lifecycle"] != "starting"
                or record["packet_sha256"] != session["packet_sha256"]
                or not isinstance(record["recovery_created"], bool)
            ):
                raise HtError("request_accepted differs from its starting claim (B1 addendum §1)")
            work["admission_status"] = "accepted"
            if work["dedup_key"] in indexes["dedup_index"]:
                raise HtError("request_accepted may not overwrite a dedup owner (B1 §8)")
            next_indexes["request_index"][work["request_id"]] = _request_entry(
                request_id=work["request_id"], request_sha256=work["request_sha256"],
                status="accepted", binding_id=work["binding_id"], node_address=address,
                lease_epoch=session["lease_epoch"], session_id=session["session_id"],
                packet_sha256=session["packet_sha256"], recovery_created=record["recovery_created"],
            )
            next_indexes["dedup_index"][work["dedup_key"]] = {
                "dedup_key": work["dedup_key"], "request_id": work["request_id"],
                "binding_id": work["binding_id"], "node_address": address,
                "lease_epoch": session["lease_epoch"], "session_id": session["session_id"],
                "packet_sha256": session["packet_sha256"],
            }
            # Required by the frozen table even though acceptance does not alter
            # the session entry itself.
            next_indexes["session_index"][session["session_id"]] = _session_entry(work, session)
            index_names = ("request_index", "dedup_index", "session_index")
        else:  # daemon_adopted_session
            if record["node_address"] != KERNEL_ADDRESS:
                raise HtError("daemon_adopted_session must be kernel-addressed (B1 addendum §1)")
            _uuid(record["daemon_incarnation_id"], "adopting daemon incarnation")
            if (
                prior_kernel["daemon_incarnation_id"] != record["daemon_incarnation_id"]
                or work["admission_status"] != "accepted"
                or session["lifecycle"] not in {"starting", "running"}
                or session["supervising_daemon_incarnation_id"]
                == record["daemon_incarnation_id"]
            ):
                raise HtError(
                    "daemon adoption differs from a not-yet-adopted live accepted session "
                    "(B1 addendum §5)"
                )
            request_entry = indexes["request_index"].get(work["request_id"])
            if (
                not isinstance(request_entry, dict)
                or request_entry.get("status") != "accepted"
                or request_entry.get("binding_id") != work["binding_id"]
                or request_entry.get("session_id") != session["session_id"]
                or request_entry.get("packet_sha256") != session["packet_sha256"]
                or request_entry.get("recovery_created") is not False
            ):
                raise HtError("daemon adoption requires a live-created accepted request index (B1 addendum §5)")
            session["supervising_daemon_incarnation_id"] = record["daemon_incarnation_id"]
            index_names = ("session_index",)
            next_indexes["session_index"][session["session_id"]] = _session_entry(work, session)
        work["sessions"][session["session_id"]] = session
        if event not in {"request_accepted", "daemon_adopted_session"}:
            next_indexes["session_index"][session["session_id"]] = _session_entry(work, session)
            index_names = ("session_index",)
        binding_images[address] = work
        binding_images[KERNEL_ADDRESS] = _kernel_target(prior_kernel, next_indexes)
    elif event == "request_duplicate":
        if record["node_address"] != KERNEL_ADDRESS:
            raise HtError("request_duplicate must be kernel-addressed (B1 addendum §1)")
        _uuid(record["request_id"], "duplicate request ID")
        _uuid(record["original_request_id"], "original request ID")
        _uuid(record["binding_id"], "binding ID")
        _positive(record["lease_epoch"], "lease epoch")
        _uuid(record["session_id"], "session ID")
        _sha(record["packet_sha256"], "packet SHA-256")
        original = indexes["request_index"].get(record["original_request_id"])
        if not isinstance(original, dict) or original.get("status") != "accepted":
            raise HtError("duplicate lacks an original accepted request (B1 addendum §1)")
        expected = {
            "binding_id": record["binding_id"], "lease_epoch": record["lease_epoch"],
            "session_id": record["session_id"], "packet_sha256": record["packet_sha256"],
        }
        if any(original.get(name) != value for name, value in expected.items()) or record["request_id"] in indexes["request_index"]:
            raise HtError("duplicate target differs from original accepted request (B1 addendum §1)")
        next_indexes["request_index"][record["request_id"]] = _request_entry(
            request_id=record["request_id"], request_sha256=None, status="duplicate",
            binding_id=record["binding_id"], node_address=original["node_address"],
            original_request_id=record["original_request_id"], lease_epoch=record["lease_epoch"],
            session_id=record["session_id"], packet_sha256=record["packet_sha256"],
        )
        index_names = ("request_index",)
        binding_images[KERNEL_ADDRESS] = _kernel_target(prior_kernel, next_indexes)
    elif event == "request_rejected":
        _uuid(record["request_id"], "rejected request ID")
        _reason(record["reason_code"], "rejection reason")
        if record["reason_code"] not in _STABLE_REASONS:
            raise HtError("rejection reason is outside the closed vocabulary (B1 addendum §6)")
        binding_id = record["binding_id"]
        if binding_id is None:
            if record["node_address"] != KERNEL_ADDRESS or record["request_id"] in indexes["request_index"]:
                raise HtError("pre-plan rejection must be new and kernel-addressed (B1 addendum §1)")
            entry = _request_entry(
                request_id=record["request_id"], request_sha256=None, status="rejected",
                reason_code=record["reason_code"],
            )
        else:
            address = _work_address(binding_id)
            work = deepcopy(bindings.get(address))
            if (
                not isinstance(work, dict)
                or record["node_address"] != address
                or work["request_id"] != record["request_id"]
                or work["admission_status"] != "pending"
                or work["phase"] != "planned"
            ):
                raise HtError("bound rejection requires its pending planned binding (B1 addendum §1)")
            work["admission_status"] = "rejected"
            work["rejection_reason_code"] = record["reason_code"]
            binding_images[address] = work
            entry = _request_entry(
                request_id=work["request_id"], request_sha256=work["request_sha256"],
                status="rejected", binding_id=work["binding_id"], node_address=address,
                reason_code=record["reason_code"],
            )
        next_indexes["request_index"][record["request_id"]] = entry
        index_names = ("request_index",)
        binding_images[KERNEL_ADDRESS] = _kernel_target(prior_kernel, next_indexes)
    elif event in {"control_stop_accepted", "control_stop_rejected"}:
        if record["node_address"] != KERNEL_ADDRESS:
            raise HtError("control disposition must be kernel-addressed (B1 addendum §1)")
        _uuid(record["control_id"], "control ID")
        _uuid(record["target_daemon_incarnation_id"], "target daemon incarnation")
        if record["control_id"] in indexes["control_index"]:
            raise HtError("control disposition reuses a control ID (B1 addendum §1)")
        accepted = event == "control_stop_accepted"
        if accepted and prior_kernel["daemon_incarnation_id"] != record["target_daemon_incarnation_id"]:
            raise HtError("accepted stop does not target current daemon (B1 addendum §1)")
        if not accepted:
            if record["reason_code"] != "stale-daemon-incarnation" or prior_kernel["daemon_incarnation_id"] == record["target_daemon_incarnation_id"]:
                raise HtError("rejected stop is not stale (B1 addendum §1)")
        next_indexes["control_index"][record["control_id"]] = {
            "control_id": record["control_id"],
            "target_daemon_incarnation_id": record["target_daemon_incarnation_id"],
            "status": "accepted" if accepted else "rejected",
            "reason_code": None if accepted else record["reason_code"],
        }
        index_names = ("control_index",)
        binding_images[KERNEL_ADDRESS] = _kernel_target(prior_kernel, next_indexes)
    else:  # pragma: no cover - wal parser closes the vocabulary
        raise HtError(f"unsupported runtime event {event!r} (B1 addendum §1)")

    index_images = {name: deepcopy(next_indexes[name]) for name in index_names}
    return binding_images, index_images, tail


def build_record(state: ReplayState, event: str, timestamp: str, **event_fields: Any) -> dict[str, Any]:
    """Return the sole legal framing-ready record for one event.

    This is the process slice's only transition-construction API; replay calls
    the same pure derivation and rejects any supplied delta that differs.
    """

    if state.upgrade is not None:
        if event.startswith("role_"):
            raise HtError(
                "post-upgrade role event cannot be constructed before the role reducer "
                "is installed (B2 §16)"
            )
        if event not in _POST_UPGRADE_B1_EVENTS:
            raise HtError(
                "role-runtime-upgraded: post-upgrade B1 synthetic lifecycle event is "
                "forbidden before WAL construction (B2 §16)"
            )
    if "node_address" in event_fields or "binding_delta" in event_fields or "index_delta" in event_fields:
        raise HtError("runtime event callers may not supply derived addresses/deltas (B1 addendum §1)")
    work_events = {
        "work_planned", "work_claimed", "claim_rolled_back", "session_starting",
        "session_running", "session_degraded", "session_terminal", "request_accepted",
    }
    node_address = _work_address(event_fields.get("binding_id")) if event in work_events else KERNEL_ADDRESS
    if event == "request_rejected" and event_fields.get("binding_id") is not None:
        node_address = _work_address(event_fields["binding_id"])
    candidate = {
        "seq": state.last_seq + 1,
        "ts": timestamp,
        "event": event,
        "node_address": node_address,
        "binding_delta": {"post_images": {}},
        **deepcopy(event_fields),
    }
    if event not in {"wal_tail_truncated", "daemon_started", "daemon_stopped"}:
        candidate["index_delta"] = {"post_images": {}}
    indexes = {
        "request_index": state.request_index,
        "dedup_index": state.dedup_index,
        "session_index": state.session_index,
        "control_index": state.control_index,
    }
    binding_images, index_images, _ = _derive_transition(
        state.runtime_id, state.bindings, indexes, candidate
    )
    candidate["binding_delta"] = {"post_images": binding_images}
    if "index_delta" in candidate:
        candidate["index_delta"] = {"post_images": index_images}
    # Exercise the closed wire validator without importing private internals.
    frame_record(candidate)
    return candidate


def upgrade_context_from_capability(document: CanonicalDocument) -> UpgradeContext:
    """Reprove one canonical public marker before selecting role replay."""

    try:
        decoded = strict_loads(document.canonical_bytes, label="role-capability.json")
        if not isinstance(decoded, dict) or decoded != document.value:
            raise HtError(
                "role capability value differs from its canonical bytes (B2 §3.1/§16)"
            )
        validate("role-wire.schema.json", decoded)
        if canonical_json_bytes(decoded) != document.canonical_bytes:
            raise HtError(
                "role capability is not exact canonical serialization (B2 §3.1/§16)"
            )
        digest = hashlib.sha256(document.canonical_bytes).hexdigest()
        if digest != document.sha256:
            raise HtError("role capability hash differs from its exact bytes (B2 §3.1/§16)")
        return UpgradeContext(
            runtime_id=decoded["runtime_id"],
            role_capability_sha256=digest,
            upgrade_base_seq=decoded["upgrade_base_seq"],
            upgrade_base_clean_wal_sha256=decoded[
                "upgrade_base_clean_wal_sha256"
            ],
            upgrade_base_checkpoint_sha256=decoded[
                "upgrade_base_checkpoint_sha256"
            ],
            upgrade_base_binding_ledger_sha256=decoded[
                "upgrade_base_binding_ledger_sha256"
            ],
        )
    except HtError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HtError(f"invalid role capability upgrade context: {exc} (B2 §3.1/§16)") from exc


def _validate_upgrade_baseline(
    parsed: ParsedWal,
    runtime_id: str,
    upgrade: UpgradeContext,
) -> ReplayState:
    if upgrade.runtime_id != runtime_id:
        raise HtError("upgrade baseline runtime identity differs from replay runtime (B2 §16)")
    base_seq = upgrade.upgrade_base_seq
    if base_seq > len(parsed.records):
        raise HtError("upgrade baseline sequence is ahead of clean WAL (B2 §16)")
    for record in parsed.records[:base_seq]:
        event = record.get("event")
        if isinstance(event, str) and event.startswith("role_"):
            raise HtError("role event occurs at or before the upgrade baseline (B2 §16)")
    base = replay(parsed, runtime_id, through_seq=base_seq)
    if base.clean_wal_sha256 != upgrade.upgrade_base_clean_wal_sha256:
        raise HtError("upgrade baseline clean-WAL hash drift (B2 §16)")
    if hashlib.sha256(base.checkpoint_bytes()).hexdigest() != (
        upgrade.upgrade_base_checkpoint_sha256
    ):
        raise HtError("upgrade baseline raw v1 checkpoint hash drift (B2 §16)")
    if hashlib.sha256(base.binding_bytes()).hexdigest() != (
        upgrade.upgrade_base_binding_ledger_sha256
    ):
        raise HtError("upgrade baseline raw binding-ledger hash drift (B2 §16)")
    return base


def replay(
    parsed: ParsedWal,
    runtime_id: str,
    *,
    through_seq: int | None = None,
    upgrade: UpgradeContext | None = None,
) -> ReplayState:
    if through_seq is not None and (
        isinstance(through_seq, bool) or not isinstance(through_seq, int) or through_seq < 0
    ):
        raise HtError("checkpoint replay sequence must be a nonnegative integer (B1 §9)")
    if through_seq is not None and through_seq > len(parsed.records):
        raise HtError("checkpoint sequence is ahead of clean WAL (B1 §9)")
    selected = parsed.records if through_seq is None else parsed.records[:through_seq]
    if upgrade is not None:
        if len(selected) < upgrade.upgrade_base_seq:
            raise HtError("selected replay target is before the upgrade baseline (B2 §16)")
        _validate_upgrade_baseline(parsed, runtime_id, upgrade)
    bindings = deepcopy(genesis_bindings(runtime_id))
    indexes: dict[str, dict[str, Any]] = {name: {} for name in INDEX_NAMES}
    final_tail = None
    for expected_seq, record in enumerate(selected, start=1):
        try:
            if record["seq"] != expected_seq:
                raise HtError("runtime WAL sequence must be contiguous from one (B1 §10)")
            if upgrade is not None:
                event = record.get("event")
                if (
                    expected_seq <= upgrade.upgrade_base_seq
                    and isinstance(event, str)
                    and event.startswith("role_")
                ):
                    raise HtError(
                        "role event occurs at or before the upgrade baseline (B2 §16)"
                    )
                if expected_seq > upgrade.upgrade_base_seq:
                    if isinstance(event, str) and event.startswith("role_"):
                        raise HtError(
                            "post-upgrade role event encountered before the role reducer "
                            "is installed (B2 §16)"
                        )
                    if event not in _POST_UPGRADE_B1_EVENTS:
                        raise HtError(
                            "post-upgrade B1 synthetic lifecycle event is forbidden (B2 §16)"
                        )
            expected_bindings, expected_indexes, tail = _derive_transition(
                runtime_id, bindings, indexes, record
            )
            supplied_bindings = record["binding_delta"]["post_images"]
            supplied_indexes = record.get("index_delta", {"post_images": {}})["post_images"]
            # Do not let Python's bool/int equality make a malformed supplied
            # post-image compare equal to the legal target.  The WAL parser
            # performs the same structural validation, but replay is itself a
            # trust boundary for callers holding an already-decoded ParsedWal.
            if supplied_bindings:
                validate_binding_map(supplied_bindings, runtime_id)
            validate_indexes(
                {name: supplied_indexes.get(name, {}) for name in INDEX_NAMES}
            )
            if supplied_bindings != expected_bindings:
                raise HtError(
                    f"{record['event']} binding post-images differ from the sole legal transition (B1 addendum §1)"
                )
            if supplied_indexes != expected_indexes:
                raise HtError(
                    f"{record['event']} index post-images differ from the sole legal transition (B1 addendum §1)"
                )
            for address, image in expected_bindings.items():
                bindings[address] = deepcopy(image)
            for name, image in expected_indexes.items():
                indexes[name] = deepcopy(image)
            validate_binding_map(bindings, runtime_id)
            validate_indexes(indexes)
            kernel = bindings[KERNEL_ADDRESS]
            for name in INDEX_NAMES:
                stem = name.removesuffix("_index")
                if kernel[f"{stem}_count"] != len(indexes[name]) or kernel[f"{name}_sha256"] != object_sha256(indexes[name]):
                    raise HtError(f"kernel {name} summary differs from replay index (B1 §9)")
            if tail is not None:
                final_tail = tail
        except HtError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise HtError(
                f"malformed runtime WAL transition at seq {expected_seq}: {exc} (B1 addendum §1)"
            ) from exc
    last_seq = len(selected)
    prefix_end = 0 if not last_seq else parsed.frame_end_offsets[last_seq - 1]
    clean_prefix = parsed.clean_prefix[:prefix_end]
    return ReplayState(
        runtime_id,
        last_seq,
        clean_prefix,
        bindings,
        indexes["request_index"],
        indexes["dedup_index"],
        indexes["session_index"],
        indexes["control_index"],
        final_tail,
        upgrade,
    )


def _decode_projection_checkpoint(
    checkpoint_bytes: bytes,
    runtime_id: str,
    *,
    upgrade: UpgradeContext | None,
) -> dict[str, Any]:
    stored_checkpoint = strict_loads(checkpoint_bytes, label="checkpoint.json")
    checkpoint_schema = (
        "checkpoint-role.schema.json" if upgrade is not None else "checkpoint.schema.json"
    )
    validate(checkpoint_schema, stored_checkpoint)
    if canonical_json_bytes(stored_checkpoint) != checkpoint_bytes:
        raise HtError("checkpoint.json is not exact canonical serialization (B1 §9)")
    if stored_checkpoint.get("runtime_id") != runtime_id:
        raise HtError("checkpoint runtime identity differs from descriptor (B1 §9)")
    if upgrade is not None and any(
        stored_checkpoint.get(name) != value
        for name, value in upgrade.checkpoint_fields().items()
    ):
        raise HtError("checkpoint capability marker fields differ from upgrade context (B2 §16)")
    return stored_checkpoint


def projection_eligibility(
    parsed: ParsedWal,
    runtime_id: str,
    checkpoint_bytes: bytes,
    ledger_bytes: bytes,
    *,
    upgrade: UpgradeContext | None = None,
) -> ProjectionEligibility:
    stored_checkpoint = _decode_projection_checkpoint(
        checkpoint_bytes,
        runtime_id,
        upgrade=upgrade,
    )
    prefix_state = replay(
        parsed,
        runtime_id,
        through_seq=stored_checkpoint["last_seq"],
        upgrade=upgrade,
    )
    if prefix_state.checkpoint_object() != stored_checkpoint:
        raise HtError("checkpoint does not equal replay of its WAL prefix (B1 §9)")
    full_state = replay(parsed, runtime_id, upgrade=upgrade)
    prefix_ledger = prefix_state.binding_bytes()
    full_ledger = full_state.binding_bytes()
    if ledger_bytes == prefix_ledger:
        at_full = prefix_ledger == full_ledger
    elif ledger_bytes == full_ledger:
        at_full = True
    else:
        raise HtError("binding ledger is neither checkpoint nor full-WAL target (B1 §9)")
    return ProjectionEligibility(prefix_state, full_state, at_full)


def require_current_projections(
    runtime_root: Path,
    parsed: ParsedWal,
    runtime_id: str,
    *,
    upgrade: UpgradeContext | None = None,
) -> ReplayState:
    eligibility = projection_eligibility(
        parsed,
        runtime_id,
        read_exact_file(runtime_root / "checkpoint.json"),
        read_exact_file(runtime_root / "binding-ledger.json"),
        upgrade=upgrade,
    )
    full = eligibility.full_state
    if (
        eligibility.checkpoint_state.last_seq != full.last_seq
        or read_exact_file(runtime_root / "checkpoint.json") != full.checkpoint_bytes()
        or read_exact_file(runtime_root / "binding-ledger.json") != full.binding_bytes()
    ):
        raise HtError("runtime projections require exclusive-lock recovery (B1 §9)")
    return full


def publish_projections(
    runtime_root: Path,
    target: ReplayState,
    *,
    allowed_prior: tuple[ReplayState, ...],
) -> None:
    """Publish replay state ledger first and checkpoint last under caller's EX lock."""

    if not allowed_prior:
        raise HtError("projection publication requires an allowed prior state (B1 §9)")
    if any(
        state.runtime_id != target.runtime_id or state.upgrade != target.upgrade
        for state in allowed_prior
    ):
        raise HtError("projection publication mixes upgrade contexts (B2 §16)")

    def materialize(state: ReplayState) -> tuple[bytes, bytes]:
        try:
            ledger = state.binding_bytes()
            checkpoint_data = state.checkpoint_bytes()
            checkpoint_object = _decode_projection_checkpoint(
                checkpoint_data,
                state.runtime_id,
                upgrade=state.upgrade,
            )
            indexes = {
                "request_index": state.request_index,
                "dedup_index": state.dedup_index,
                "session_index": state.session_index,
                "control_index": state.control_index,
            }
            validate_indexes(indexes)
            if checkpoint_object["bindings"] != state.bindings:
                raise HtError("projection checkpoint differs from target bindings (B1 §9)")
            if checkpoint_object["binding_ledger_sha256"] != hashlib.sha256(
                ledger
            ).hexdigest():
                raise HtError("projection checkpoint differs from target ledger hash (B1 §9)")
            return checkpoint_data, ledger
        except HtError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise HtError(f"invalid projection publication state: {exc} (B1 §9)") from exc

    target_checkpoint, target_ledger = materialize(target)
    prior_material: list[tuple[ReplayState, bytes, bytes]] = []
    for state in allowed_prior:
        if (
            state.last_seq > target.last_seq
            or not target.clean_prefix.startswith(state.clean_prefix)
        ):
            raise HtError("projection prior is not a prefix of the target state (B1 §9)")
        checkpoint_data, ledger = materialize(state)
        prior_material.append((state, checkpoint_data, ledger))

    ledger_path = runtime_root / "binding-ledger.json"
    checkpoint_path = runtime_root / "checkpoint.json"
    current_checkpoint = read_exact_file(checkpoint_path)
    current_ledger = read_exact_file(ledger_path)
    _decode_projection_checkpoint(
        current_checkpoint,
        target.runtime_id,
        upgrade=target.upgrade,
    )
    checkpoint_matches = [
        state
        for state, checkpoint_data, _ledger in prior_material
        if checkpoint_data == current_checkpoint
    ]
    if not checkpoint_matches:
        raise HtError("current checkpoint is not an allowed projection prior (B1 §9)")
    if not any(
        ledger == current_ledger and ledger_state.last_seq >= checkpoint_state.last_seq
        for checkpoint_state in checkpoint_matches
        for ledger_state, _checkpoint_data, ledger in prior_material
    ):
        raise HtError(
            "current checkpoint/ledger pair is not an allowed ledger-first state (B1 §9)"
        )

    replace_file(
        ledger_path,
        target_ledger,
        expected_old=current_ledger,
    )
    if read_exact_file(ledger_path) != target_ledger:  # pragma: no cover
        raise HtError("binding ledger reread differs after publication (B1 §9)")
    replace_file(
        checkpoint_path,
        target_checkpoint,
        expected_old=current_checkpoint,
    )
    if read_exact_file(checkpoint_path) != target_checkpoint:  # pragma: no cover
        raise HtError("checkpoint reread differs after publication (B1 §9)")


def recover_tolerated_tail(
    runtime_root: Path,
    parsed: ParsedWal,
    runtime_id: str,
    *,
    timestamp: str,
    upgrade: UpgradeContext | None = None,
) -> ReplayState:
    """Atomically disclose one tolerated final tail under caller's EX lock."""

    if parsed.tail is None:
        raise HtError("runtime WAL has no tolerated tail to recover (B1 §10)")
    wal_path = runtime_root / "run-ledger.jsonl"
    old_wal = read_exact_file(wal_path)
    if parse_bytes(old_wal) != parsed:
        raise HtError("runtime WAL changed after tolerated-tail parse (B1 §10)")
    eligibility = projection_eligibility(
        parsed,
        runtime_id,
        read_exact_file(runtime_root / "checkpoint.json"),
        read_exact_file(runtime_root / "binding-ledger.json"),
        upgrade=upgrade,
    )
    checkpoint_state = eligibility.checkpoint_state
    full_state = eligibility.full_state
    ledger_bytes = read_exact_file(runtime_root / "binding-ledger.json")
    # Only a distinguishable W-ledger/C-checkpoint crash state needs a write
    # before the WAL commit. C/C is already a prefix and remains byte-for-byte
    # untouched until the atomic WAL replacement.
    if (
        ledger_bytes == full_state.binding_bytes()
        and ledger_bytes != checkpoint_state.binding_bytes()
    ):
        replace_file(
            runtime_root / "checkpoint.json",
            full_state.checkpoint_bytes(),
            expected_old=checkpoint_state.checkpoint_bytes(),
        )
    tail = parsed.tail
    disclosure = frame_record(
        {
            "seq": eligibility.full_state.last_seq + 1,
            "ts": timestamp,
            "event": "wal_tail_truncated",
            "node_address": KERNEL_ADDRESS,
            "binding_delta": {"post_images": {}},
            "tail_reason": tail.reason,
            "tail_byte_offset": tail.byte_offset,
            "tail_discarded_byte_count": tail.discarded_byte_count,
            "tail_discarded_sha256": tail.discarded_sha256,
        }
    )
    replacement = parsed.clean_prefix + disclosure
    replace_file(wal_path, replacement, expected_old=old_wal)
    reread = read_exact_file(wal_path)
    if reread != replacement:  # pragma: no cover - replace_file already proves
        raise HtError("runtime WAL tail replacement reread differs (B1 §10)")
    recovered = parse_bytes(reread)
    if recovered.tail is not None:
        raise HtError("runtime WAL tail disclosure replacement is not clean (B1 §10)")
    target = replay(recovered, runtime_id, upgrade=upgrade)
    publish_projections(
        runtime_root,
        target,
        allowed_prior=(checkpoint_state, full_state),
    )
    return target
