"""Deterministic runtime genesis and legal state vocabulary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import re
from typing import Any

from ht.references import canonical_json_sha256

from .schema import canonical_json_bytes, validate


KERNEL_ADDRESS = "runtime#kernel"
EVENTS = frozenset(
    {
        "wal_tail_truncated",
        "daemon_started",
        "daemon_stopped",
        "daemon_adopted_session",
        "work_planned",
        "work_claimed",
        "claim_rolled_back",
        "session_starting",
        "session_running",
        "session_degraded",
        "session_terminal",
        "request_accepted",
        "request_duplicate",
        "request_rejected",
        "control_stop_accepted",
        "control_stop_rejected",
    }
)
LIFECYCLE_PHASES = ("planned", "claimed", "starting", "running", "terminal")
TERMINAL_OUTCOMES = frozenset({"SUCCEEDED", "FAILED", "crashed"})

KERNEL_FIELDS = frozenset(
    {
        "node_address",
        "binding_kind",
        "runtime_id",
        "daemon_incarnation_id",
        "request_count",
        "request_index_sha256",
        "dedup_count",
        "dedup_index_sha256",
        "session_count",
        "session_index_sha256",
        "control_count",
        "control_index_sha256",
    }
)
WORK_FIELDS = frozenset(
    {
        "node_address",
        "binding_kind",
        "runtime_id",
        "binding_id",
        "request_id",
        "request_sha256",
        "role",
        "attempt",
        "retry_lineage",
        "work",
        "dedup_key",
        "admission_status",
        "admission_repository_commit",
        "rejection_reason_code",
        "phase",
        "last_lease_epoch",
        "current_session_id",
        "sessions",
        "terminal_outcome",
    }
)
SESSION_FIELDS = frozenset(
    {
        "session_id",
        "lease_epoch",
        "fence",
        "lifecycle",
        "outcome",
        "abandonment_reason_code",
        "terminal_reason_code",
        "packet",
        "packet_canonical_json",
        "packet_sha256",
        "launch",
        "launch_canonical_json",
        "launch_sha256",
        "wrapper_instance_id",
        "helper_instance_id",
        "supervising_daemon_incarnation_id",
        "started_sha256",
        "ready_sha256",
        "result_sha256",
        "terminal_sha256",
        "process_exit_sha256",
        "degraded_recorded",
    }
)
REQUEST_INDEX_FIELDS = frozenset(
    {
        "request_id",
        "request_sha256",
        "status",
        "binding_id",
        "node_address",
        "original_request_id",
        "lease_epoch",
        "session_id",
        "packet_sha256",
        "reason_code",
        "recovery_created",
    }
)
DEDUP_INDEX_FIELDS = frozenset(
    {
        "dedup_key",
        "request_id",
        "binding_id",
        "node_address",
        "lease_epoch",
        "session_id",
        "packet_sha256",
    }
)
SESSION_INDEX_FIELDS = frozenset(
    {
        "session_id",
        "request_id",
        "binding_id",
        "node_address",
        "lease_epoch",
        "fence",
        "lifecycle",
        "outcome",
        "abandonment_reason_code",
        "terminal_reason_code",
        "packet_sha256",
        "launch_sha256",
        "wrapper_instance_id",
        "helper_instance_id",
        "supervising_daemon_incarnation_id",
        "started_sha256",
        "ready_sha256",
        "result_sha256",
        "terminal_sha256",
        "process_exit_sha256",
        "degraded_recorded",
    }
)
CONTROL_INDEX_FIELDS = frozenset(
    {"control_id", "target_daemon_incarnation_id", "status", "reason_code"}
)

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.ASCII,
)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ADDRESS = re.compile(
    r"runtime/(?P<binding>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})#synthetic",
    re.ASCII,
)
_WORK_FIELDS = frozenset(
    {
        "type",
        "canonical_ref",
        "repository_relpath",
        "canonical_object_sha256",
        "raw_file_sha256",
        "head_blob_oid",
        "submission_repository_commit",
        "git_object_format",
    }
)


@dataclass(frozen=True)
class UpgradeContext:
    """Immutable marker identity selecting the additive role checkpoint branch."""

    runtime_id: str
    role_capability_sha256: str
    upgrade_base_seq: int
    upgrade_base_clean_wal_sha256: str
    upgrade_base_checkpoint_sha256: str
    upgrade_base_binding_ledger_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_id, str) or _UUID.fullmatch(self.runtime_id) is None:
            raise ValueError("upgrade runtime ID must be a canonical lowercase UUID")
        if (
            isinstance(self.upgrade_base_seq, bool)
            or not isinstance(self.upgrade_base_seq, int)
            or self.upgrade_base_seq < 0
        ):
            raise ValueError("upgrade base sequence must be an exact nonnegative integer")
        for name in (
            "role_capability_sha256",
            "upgrade_base_clean_wal_sha256",
            "upgrade_base_checkpoint_sha256",
            "upgrade_base_binding_ledger_sha256",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase SHA-256")

    def checkpoint_fields(self) -> dict[str, Any]:
        return {
            "role_capability_sha256": self.role_capability_sha256,
            "upgrade_base_seq": self.upgrade_base_seq,
            "upgrade_base_clean_wal_sha256": self.upgrade_base_clean_wal_sha256,
            "upgrade_base_checkpoint_sha256": self.upgrade_base_checkpoint_sha256,
            "upgrade_base_binding_ledger_sha256": (
                self.upgrade_base_binding_ledger_sha256
            ),
        }


def object_sha256(value: Any) -> str:
    return canonical_json_sha256(value)


def dedup_identity(request: dict[str, Any]) -> dict[str, Any]:
    """Return the exact B1 admission identity (commits/path evidence excluded)."""

    work = request["work"]
    return {
        "type": work["type"],
        "canonical_ref": work["canonical_ref"],
        "canonical_object_sha256": work["canonical_object_sha256"],
        "raw_file_sha256": work["raw_file_sha256"],
        "head_blob_oid": work["head_blob_oid"],
        "role": request["role"],
        "attempt": request["attempt"],
        "retry_lineage": request["retry_lineage"],
    }


def derive_dedup_key(request: dict[str, Any]) -> str:
    """Derive the exported exact B1 dedup key from a schema-valid request."""

    return object_sha256(dedup_identity(request))


def empty_index_hash() -> str:
    return object_sha256({})


def genesis_kernel(runtime_id: str) -> dict[str, Any]:
    empty_hash = empty_index_hash()
    return {
        "node_address": KERNEL_ADDRESS,
        "binding_kind": "kernel",
        "runtime_id": runtime_id,
        "daemon_incarnation_id": None,
        "request_count": 0,
        "request_index_sha256": empty_hash,
        "dedup_count": 0,
        "dedup_index_sha256": empty_hash,
        "session_count": 0,
        "session_index_sha256": empty_hash,
        "control_count": 0,
        "control_index_sha256": empty_hash,
    }


def genesis_bindings(runtime_id: str) -> dict[str, dict[str, Any]]:
    return {KERNEL_ADDRESS: genesis_kernel(runtime_id)}


def binding_ledger_bytes(bindings: dict[str, dict[str, Any]]) -> bytes:
    return canonical_json_bytes(bindings)


def checkpoint(
    runtime_id: str,
    bindings: dict[str, dict[str, Any]],
    *,
    last_seq: int,
    clean_wal_sha256: str,
    request_index: dict[str, Any] | None = None,
    dedup_index: dict[str, Any] | None = None,
    session_index: dict[str, Any] | None = None,
    control_index: dict[str, Any] | None = None,
    final_tail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "hypothesis-tree-runtime-checkpoint/1.0.0",
        "runtime_id": runtime_id,
        "last_seq": last_seq,
        "clean_wal_sha256": clean_wal_sha256,
        "daemon_incarnation_id": bindings[KERNEL_ADDRESS]["daemon_incarnation_id"],
        "request_index": request_index or {},
        "dedup_index": dedup_index or {},
        "session_index": session_index or {},
        "control_index": control_index or {},
        "bindings": bindings,
        "binding_ledger_sha256": hashlib.sha256(binding_ledger_bytes(bindings)).hexdigest(),
        "final_tail": final_tail,
    }


def role_checkpoint(
    base_checkpoint: dict[str, Any],
    upgrade: UpgradeContext,
) -> dict[str, Any]:
    """Select v2 by copying one exact v1 checkpoint plus immutable marker fields."""

    validate("checkpoint.schema.json", base_checkpoint)
    if base_checkpoint.get("schema_version") != "hypothesis-tree-runtime-checkpoint/1.0.0":
        raise ValueError("role checkpoint source must be the exact v1 checkpoint")
    if base_checkpoint.get("runtime_id") != upgrade.runtime_id:
        raise ValueError("role checkpoint runtime differs from upgrade context")
    last_seq = base_checkpoint["last_seq"]
    if last_seq < upgrade.upgrade_base_seq:
        raise ValueError("role checkpoint is before its immutable upgrade baseline")
    if last_seq == upgrade.upgrade_base_seq:
        if (
            base_checkpoint["clean_wal_sha256"]
            != upgrade.upgrade_base_clean_wal_sha256
        ):
            raise ValueError("role checkpoint clean WAL differs from upgrade baseline")
        if (
            hashlib.sha256(canonical_json_bytes(base_checkpoint)).hexdigest()
            != upgrade.upgrade_base_checkpoint_sha256
        ):
            raise ValueError("raw v1 checkpoint differs from upgrade baseline")
        if (
            base_checkpoint["binding_ledger_sha256"]
            != upgrade.upgrade_base_binding_ledger_sha256
        ):
            raise ValueError("binding ledger differs from upgrade baseline")
    result = deepcopy(base_checkpoint)
    result["schema_version"] = "hypothesis-tree-runtime-checkpoint/2.0.0"
    result.update(upgrade.checkpoint_fields())
    validate("checkpoint-role.schema.json", result)
    return result


def genesis_checkpoint(runtime_id: str) -> dict[str, Any]:
    bindings = genesis_bindings(runtime_id)
    return checkpoint(
        runtime_id,
        bindings,
        last_seq=0,
        clean_wal_sha256=hashlib.sha256(b"").hexdigest(),
    )


def _exact_object(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} must contain exactly {sorted(fields)}")
    return value


def _uuid(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical lowercase UUID")


def _sha(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")


def _positive(value: Any, label: str, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an exact integer >= {minimum}")


def _reason(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9-]+", value, re.ASCII) is None:
        raise ValueError(f"{label} must be a stable reason code")


def _address(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical synthetic node address")
    match = _ADDRESS.fullmatch(value)
    if match is None:
        raise ValueError(f"{label} must be a canonical synthetic node address")
    return match.group("binding")


def _oid_for_format(value: Any, object_format: Any, label: str) -> None:
    if object_format not in {"sha1", "sha256"}:
        raise ValueError(f"{label} Git object format is invalid")
    length = 40 if object_format == "sha1" else 64
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError(f"{label} must be a {length}-hex {object_format} Git OID")


def validate_frozen_work(value: Any, label: str = "frozen work") -> dict[str, Any]:
    """Validate the complete submit-time work identity, including OID format."""

    work = _exact_object(value, _WORK_FIELDS, label)
    if work["type"] not in {"issue", "node", "dispatch"}:
        raise ValueError(f"{label} type is outside the closed vocabulary")
    for name in ("canonical_ref", "repository_relpath"):
        if not isinstance(work[name], str) or not work[name]:
            raise ValueError(f"{label} {name} must be nonempty text")
    relpath = work["repository_relpath"]
    if relpath.startswith("/") or any(part == ".." for part in relpath.split("/")):
        raise ValueError(f"{label} repository path must be confined and relative")
    _sha(work["canonical_object_sha256"], f"{label} canonical object hash")
    _sha(work["raw_file_sha256"], f"{label} raw file hash")
    object_format = work["git_object_format"]
    _oid_for_format(work["head_blob_oid"], object_format, f"{label} HEAD blob")
    _oid_for_format(
        work["submission_repository_commit"], object_format, f"{label} submission commit"
    )
    return work


def validate_request_object(value: Any) -> dict[str, Any]:
    """Validate a complete canonical request plus its cross-field Git identity."""

    validate("request.schema.json", value)
    if not isinstance(value, dict):  # schema owns this; keeps the type narrow below
        raise ValueError("runtime request must be an object")
    validate_frozen_work(value["work"], "request frozen work")
    return value


def validate_session_packet_object(value: Any) -> dict[str, Any]:
    """Validate a complete packet plus all Git-object-format relationships."""

    validate("session-packet.schema.json", value)
    if not isinstance(value, dict):  # schema owns this; keeps the type narrow below
        raise ValueError("runtime session packet must be an object")
    work = validate_frozen_work(value["work"], "session packet frozen work")
    _oid_for_format(
        value["admission_repository_commit"],
        work["git_object_format"],
        "session packet admission commit",
    )
    return value


def _validate_session_artifacts(session: dict[str, Any], work: dict[str, Any]) -> None:
    packet = session["packet"]
    launch = session["launch"]
    validate_session_packet_object(packet)
    validate("launch.schema.json", launch)
    packet_bytes = canonical_json_bytes(packet)
    launch_bytes = canonical_json_bytes(launch)
    try:
        packet_text = packet_bytes.decode("utf-8")
        launch_text = launch_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - canonical encoder contract
        raise ValueError("session artifacts are not canonical UTF-8") from exc
    if session["packet_canonical_json"] != packet_text:
        raise ValueError("session packet canonical JSON differs from its complete object")
    if session["launch_canonical_json"] != launch_text:
        raise ValueError("session launch canonical JSON differs from its complete object")
    if hashlib.sha256(packet_bytes).hexdigest() != session["packet_sha256"]:
        raise ValueError("session packet hash differs from its canonical JSON")
    if hashlib.sha256(launch_bytes).hexdigest() != session["launch_sha256"]:
        raise ValueError("session launch hash differs from its canonical JSON")
    identity = {
        "runtime_id": work["runtime_id"],
        "request_id": work["request_id"],
        "binding_id": work["binding_id"],
        "node_address": work["node_address"],
        "lease_epoch": session["lease_epoch"],
        "session_id": session["session_id"],
        "role": work["role"],
    }
    if any(packet.get(name) != expected for name, expected in identity.items()):
        raise ValueError("session packet identity differs from its work/session")
    if any(launch.get(name) != expected for name, expected in identity.items()):
        raise ValueError("session launch identity differs from its work/session")
    if (
        packet["fence"] != session["fence"]
        or packet["attempt"] != work["attempt"]
        or packet["retry_lineage"] != work["retry_lineage"]
        or packet["work"] != work["work"]
        or packet["admission_repository_commit"] != work["admission_repository_commit"]
        or packet["wrapper_instance_id"] != session["wrapper_instance_id"]
        or packet["helper_instance_id"] != session["helper_instance_id"]
        or launch["wrapper_instance_id"] != session["wrapper_instance_id"]
        or launch["helper_instance_id"] != session["helper_instance_id"]
        or launch["packet_sha256"] != session["packet_sha256"]
        or launch["packet_relative_path"]
        != f"sessions/{session['session_id']}/packet.json"
    ):
        raise ValueError("session packet/launch relations differ from frozen work/session")


def _validate_session_lifecycle(value: dict[str, Any], label: str) -> None:
    lifecycle = value["lifecycle"]
    outcome = value["outcome"]
    abandonment = value["abandonment_reason_code"]
    terminal_reason = value["terminal_reason_code"]
    receipt_names = (
        "started_sha256",
        "ready_sha256",
        "result_sha256",
        "terminal_sha256",
        "process_exit_sha256",
    )
    receipts = {name: value[name] for name in receipt_names}
    if lifecycle == "abandoned":
        if outcome is not None or abandonment is None or terminal_reason is not None or any(
            item is not None for item in receipts.values()
        ):
            raise ValueError(f"{label} abandoned nullability is invalid")
    elif abandonment is not None:
        raise ValueError(f"{label} abandonment reason is only legal when abandoned")
    if lifecycle in {"claimed", "starting", "running"} and (
        outcome is not None or terminal_reason is not None
    ):
        raise ValueError(f"{label} live outcome/reason nullability is invalid")
    if lifecycle == "claimed" and any(item is not None for item in receipts.values()):
        raise ValueError(f"{label} claimed receipt nullability is invalid")
    if lifecycle == "starting" and any(
        receipts[name] is not None
        for name in ("ready_sha256", "result_sha256", "terminal_sha256", "process_exit_sha256")
    ):
        raise ValueError(f"{label} starting receipt nullability is invalid")
    if lifecycle == "running" and (
        receipts["started_sha256"] is None
        or receipts["ready_sha256"] is None
        or any(
            receipts[name] is not None
            for name in ("result_sha256", "terminal_sha256", "process_exit_sha256")
        )
    ):
        raise ValueError(f"{label} running receipt nullability is invalid")
    if lifecycle == "terminal":
        if outcome is None:
            raise ValueError(f"{label} terminal outcome is required")
        if outcome in {"SUCCEEDED", "FAILED"} and (
            terminal_reason is not None or any(item is None for item in receipts.values())
        ):
            raise ValueError(f"{label} role terminal hashes/reason are invalid")
        if outcome == "crashed" and terminal_reason is None:
            raise ValueError(f"{label} crashed terminal reason is required")


def validate_session(value: Any, *, work: dict[str, Any]) -> None:
    session = _exact_object(value, SESSION_FIELDS, "runtime session")
    _uuid(session["session_id"], "session.session_id")
    _positive(session["lease_epoch"], "session.lease_epoch")
    fence = _exact_object(
        session["fence"], frozenset({"binding_id", "lease_epoch", "session_id"}), "session fence"
    )
    _uuid(fence["binding_id"], "session fence binding ID")
    _positive(fence["lease_epoch"], "session fence lease epoch")
    _uuid(fence["session_id"], "session fence session ID")
    if fence != {
        "binding_id": work["binding_id"],
        "lease_epoch": session["lease_epoch"],
        "session_id": session["session_id"],
    }:
        raise ValueError("session fence differs from its binding/lease/session")
    if session["lifecycle"] not in {"claimed", "abandoned", "starting", "running", "terminal"}:
        raise ValueError("session lifecycle is outside the closed vocabulary")
    if session["outcome"] is not None and session["outcome"] not in TERMINAL_OUTCOMES:
        raise ValueError("session outcome is outside the closed vocabulary")
    _reason(session["abandonment_reason_code"], "session abandonment reason", nullable=True)
    _reason(session["terminal_reason_code"], "session terminal reason", nullable=True)
    if not isinstance(session["packet"], dict) or not isinstance(session["launch"], dict):
        raise ValueError("session packet and launch must be complete objects")
    if not isinstance(session["packet_canonical_json"], str) or not session["packet_canonical_json"].endswith("\n"):
        raise ValueError("session packet canonical JSON must include trailing LF")
    if not isinstance(session["launch_canonical_json"], str) or not session["launch_canonical_json"].endswith("\n"):
        raise ValueError("session launch canonical JSON must include trailing LF")
    _sha(session["packet_sha256"], "session packet hash")
    _sha(session["launch_sha256"], "session launch hash")
    _uuid(session["wrapper_instance_id"], "session wrapper instance")
    _uuid(session["helper_instance_id"], "session helper instance")
    _uuid(session["supervising_daemon_incarnation_id"], "session supervisor", nullable=True)
    for name in ("started_sha256", "ready_sha256", "result_sha256", "terminal_sha256", "process_exit_sha256"):
        _sha(session[name], f"session {name}", nullable=True)
    if not isinstance(session["degraded_recorded"], bool):
        raise ValueError("session degraded_recorded must be Boolean")
    if session["wrapper_instance_id"] == session["helper_instance_id"]:
        raise ValueError("session wrapper and helper identities must be distinct")
    _validate_session_lifecycle(session, "runtime session")
    _validate_session_artifacts(session, work)


def validate_work_binding(binding: Any, address: str, runtime_id: str) -> None:
    work = _exact_object(binding, WORK_FIELDS, "runtime work binding")
    if work["node_address"] != address or work["runtime_id"] != runtime_id:
        raise ValueError("work binding address/runtime identity mismatch")
    if work["binding_kind"] != "synthetic" or work["role"] != "synthetic-kernel-v1":
        raise ValueError("work binding kind/role mismatch")
    _uuid(work["binding_id"], "work binding ID")
    if address != f"runtime/{work['binding_id']}#synthetic":
        raise ValueError("work binding ID differs from its canonical address")
    _uuid(work["request_id"], "work request ID")
    _sha(work["request_sha256"], "work request hash")
    _positive(work["attempt"], "work attempt")
    if not isinstance(work["retry_lineage"], list) or len(set(work["retry_lineage"])) != len(work["retry_lineage"]):
        raise ValueError("work retry lineage must be a unique list")
    for request_id in work["retry_lineage"]:
        _uuid(request_id, "retry lineage request ID")
    validate_frozen_work(work["work"], "work binding frozen work")
    _sha(work["dedup_key"], "work dedup key")
    if work["admission_status"] not in {"pending", "accepted", "rejected"}:
        raise ValueError("work admission status is invalid")
    if work["admission_repository_commit"] is not None:
        _oid_for_format(
            work["admission_repository_commit"],
            work["work"]["git_object_format"],
            "work admission repository commit",
        )
    _reason(work["rejection_reason_code"], "work rejection reason", nullable=True)
    if work["phase"] not in LIFECYCLE_PHASES:
        raise ValueError("work phase is invalid")
    _positive(work["last_lease_epoch"], "work last lease", allow_zero=True)
    _uuid(work["current_session_id"], "work current session", nullable=True)
    if not isinstance(work["sessions"], dict):
        raise ValueError("work sessions must be an object")
    for session_id, session in work["sessions"].items():
        _uuid(session_id, "session map key")
        validate_session(session, work=work)
        if session["session_id"] != session_id:
            raise ValueError("session map key/body mismatch")
    if work["current_session_id"] is not None and work["current_session_id"] not in work["sessions"]:
        raise ValueError("work current session is missing from its session map")
    if work["terminal_outcome"] is not None and work["terminal_outcome"] not in TERMINAL_OUTCOMES:
        raise ValueError("work terminal outcome is invalid")
    if (work["phase"] == "terminal") != (work["terminal_outcome"] is not None):
        raise ValueError("work terminal phase/outcome nullability is invalid")
    if (work["admission_status"] == "rejected") != (
        work["rejection_reason_code"] is not None
    ):
        raise ValueError("work rejection status/reason nullability is invalid")
    if work["admission_status"] == "rejected" and (
        work["phase"] != "planned" or work["current_session_id"] is not None
    ):
        raise ValueError("rejected work must remain unclaimed planned work")
    if work["phase"] in {"claimed", "starting", "running", "terminal"} and (
        work["current_session_id"] is None or work["admission_repository_commit"] is None
    ):
        raise ValueError("claimed-or-later work lacks current session/admission commit")
    if work["admission_status"] == "accepted" and work["phase"] not in {
        "starting", "running", "terminal"
    }:
        raise ValueError("accepted work phase is invalid")


def validate_indexes(indexes: dict[str, Any]) -> None:
    definitions = {
        "request_index": REQUEST_INDEX_FIELDS,
        "dedup_index": DEDUP_INDEX_FIELDS,
        "session_index": SESSION_INDEX_FIELDS,
        "control_index": CONTROL_INDEX_FIELDS,
    }
    if set(indexes) != set(definitions):
        raise ValueError("runtime indexes must contain the exact four index maps")
    for index_name, fields in definitions.items():
        index = indexes[index_name]
        if not isinstance(index, dict):
            raise ValueError(f"{index_name} must be an object")
        for key, value in index.items():
            if not isinstance(key, str):
                raise ValueError(f"{index_name} keys must be strings")
            entry = _exact_object(value, fields, f"{index_name} entry")
            if index_name == "request_index":
                _uuid(key, "request index key")
                if entry["request_id"] != key or entry["status"] not in {"planned", "accepted", "duplicate", "rejected"}:
                    raise ValueError("request index key/status mismatch")
                _sha(entry["request_sha256"], "request index hash", nullable=True)
                _uuid(entry["binding_id"], "request binding ID", nullable=True)
                address_binding = _address(
                    entry["node_address"], "request node address", nullable=True
                )
                if entry["binding_id"] is not None and address_binding != entry["binding_id"]:
                    raise ValueError("request binding ID differs from its node address")
                if (entry["binding_id"] is None) != (entry["node_address"] is None):
                    raise ValueError("request binding ID/address nullability differs")
                _uuid(entry["original_request_id"], "original request ID", nullable=True)
                if entry["lease_epoch"] is not None:
                    _positive(entry["lease_epoch"], "request lease epoch")
                _uuid(entry["session_id"], "request session ID", nullable=True)
                _sha(entry["packet_sha256"], "request packet hash", nullable=True)
                _reason(entry["reason_code"], "request reason", nullable=True)
                if entry["recovery_created"] is not None and not isinstance(entry["recovery_created"], bool):
                    raise ValueError("request recovery_created must be Boolean or null")
                bound = entry["binding_id"] is not None
                if entry["status"] == "planned":
                    if not bound or entry["request_sha256"] is None or any(
                        entry[name] is not None
                        for name in (
                            "original_request_id", "lease_epoch", "session_id",
                            "packet_sha256", "reason_code", "recovery_created",
                        )
                    ):
                        raise ValueError("planned request index nullability is invalid")
                elif entry["status"] == "accepted":
                    if (
                        not bound
                        or entry["request_sha256"] is None
                        or any(entry[name] is None for name in ("lease_epoch", "session_id", "packet_sha256", "recovery_created"))
                        or entry["original_request_id"] is not None
                        or entry["reason_code"] is not None
                    ):
                        raise ValueError("accepted request index nullability is invalid")
                elif entry["status"] == "duplicate":
                    if (
                        not bound
                        or entry["request_sha256"] is not None
                        or any(entry[name] is None for name in ("original_request_id", "lease_epoch", "session_id", "packet_sha256"))
                        or entry["reason_code"] is not None
                        or entry["recovery_created"] is not None
                    ):
                        raise ValueError("duplicate request index nullability is invalid")
                elif (
                    entry["reason_code"] is None
                    or entry["original_request_id"] is not None
                    or entry["lease_epoch"] is not None
                    or entry["session_id"] is not None
                    or entry["packet_sha256"] is not None
                    or entry["recovery_created"] is not None
                    or (bound != (entry["request_sha256"] is not None))
                ):
                    raise ValueError("rejected request index nullability is invalid")
            elif index_name == "dedup_index":
                _sha(key, "dedup index key")
                if entry["dedup_key"] != key:
                    raise ValueError("dedup index key/body mismatch")
                _uuid(entry["request_id"], "dedup request ID")
                _uuid(entry["binding_id"], "dedup binding ID")
                if _address(entry["node_address"], "dedup node address") != entry["binding_id"]:
                    raise ValueError("dedup binding ID differs from its node address")
                _positive(entry["lease_epoch"], "dedup lease epoch")
                _uuid(entry["session_id"], "dedup session ID")
                _sha(entry["packet_sha256"], "dedup packet hash")
            elif index_name == "session_index":
                _uuid(key, "session index key")
                if entry["session_id"] != key:
                    raise ValueError("session index key/body mismatch")
                _uuid(entry["request_id"], "session request ID")
                _uuid(entry["binding_id"], "session binding ID")
                if _address(entry["node_address"], "session node address") != entry["binding_id"]:
                    raise ValueError("session binding ID differs from its node address")
                _positive(entry["lease_epoch"], "session lease epoch")
                fence = _exact_object(
                    entry["fence"],
                    frozenset({"binding_id", "lease_epoch", "session_id"}),
                    "session index fence",
                )
                _uuid(fence["binding_id"], "session index fence binding ID")
                _positive(fence["lease_epoch"], "session index fence lease epoch")
                _uuid(fence["session_id"], "session index fence session ID")
                if fence != {
                    "binding_id": entry["binding_id"],
                    "lease_epoch": entry["lease_epoch"],
                    "session_id": entry["session_id"],
                }:
                    raise ValueError("session index fence differs from its identity")
                if entry["lifecycle"] not in {"claimed", "abandoned", "starting", "running", "terminal"}:
                    raise ValueError("session index lifecycle is invalid")
                if entry["outcome"] is not None and entry["outcome"] not in TERMINAL_OUTCOMES:
                    raise ValueError("session index outcome is invalid")
                _reason(entry["abandonment_reason_code"], "session abandonment reason", nullable=True)
                _reason(entry["terminal_reason_code"], "session terminal reason", nullable=True)
                _sha(entry["packet_sha256"], "session packet hash")
                _sha(entry["launch_sha256"], "session launch hash")
                _uuid(entry["wrapper_instance_id"], "session wrapper instance")
                _uuid(entry["helper_instance_id"], "session helper instance")
                _uuid(entry["supervising_daemon_incarnation_id"], "session supervisor", nullable=True)
                for name in ("started_sha256", "ready_sha256", "result_sha256", "terminal_sha256", "process_exit_sha256"):
                    _sha(entry[name], f"session index {name}", nullable=True)
                if not isinstance(entry["degraded_recorded"], bool):
                    raise ValueError("session index degraded_recorded must be Boolean")
                if entry["wrapper_instance_id"] == entry["helper_instance_id"]:
                    raise ValueError("session index wrapper/helper identities must be distinct")
                _validate_session_lifecycle(entry, "session index")
            else:
                _uuid(key, "control index key")
                if entry["control_id"] != key or entry["status"] not in {"accepted", "rejected"}:
                    raise ValueError("control index key/status mismatch")
                _uuid(entry["target_daemon_incarnation_id"], "control target daemon")
                _reason(entry["reason_code"], "control reason", nullable=True)
                if (entry["status"] == "accepted") != (entry["reason_code"] is None):
                    raise ValueError("control status/reason nullability is invalid")


def validate_binding_map(bindings: Any, runtime_id: str) -> None:
    _uuid(runtime_id, "runtime identity")
    if not isinstance(bindings, dict) or KERNEL_ADDRESS not in bindings:
        raise ValueError("binding map must contain runtime#kernel")
    for address, binding in bindings.items():
        if not isinstance(address, str) or not isinstance(binding, dict):
            raise ValueError("binding map entries must be address/object pairs")
        if binding.get("node_address") != address:
            raise ValueError("binding map key must equal binding node_address")
    kernel = _exact_object(bindings[KERNEL_ADDRESS], KERNEL_FIELDS, "runtime kernel binding")
    if kernel["node_address"] != KERNEL_ADDRESS or kernel["binding_kind"] != "kernel" or kernel["runtime_id"] != runtime_id:
        raise ValueError("kernel binding identity differs from runtime descriptor")
    _uuid(kernel["daemon_incarnation_id"], "kernel daemon incarnation", nullable=True)
    for stem in ("request", "dedup", "session", "control"):
        _positive(kernel[f"{stem}_count"], f"kernel {stem} count", allow_zero=True)
        _sha(kernel[f"{stem}_index_sha256"], f"kernel {stem} index hash")
    for address, binding in bindings.items():
        if address != KERNEL_ADDRESS:
            validate_work_binding(binding, address, runtime_id)
