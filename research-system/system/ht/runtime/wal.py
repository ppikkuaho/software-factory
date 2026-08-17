"""Item-8-compatible framed WAL encoding and strict final-tail parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import zlib

from ht.errors import HtError

from .atomic import read_exact_file
from .schema import canonical_json_bytes, strict_loads, validate
from .state import (
    EVENTS,
    KERNEL_ADDRESS,
    validate_binding_map,
    derive_dedup_key,
    validate_indexes,
    validate_request_object,
    validate_session_packet_object,
)


_COMMON_FIELDS = frozenset({"seq", "ts", "event", "node_address", "binding_delta", "crc32"})
_EVENT_FIELDS = {
    "wal_tail_truncated": frozenset(
        {"tail_reason", "tail_byte_offset", "tail_discarded_byte_count", "tail_discarded_sha256"}
    ),
    "daemon_started": frozenset({"daemon_incarnation_id"}),
    "daemon_stopped": frozenset({"daemon_incarnation_id", "control_id"}),
    "daemon_adopted_session": frozenset(
        {"daemon_incarnation_id", "binding_id", "lease_epoch", "session_id"}
    ),
    "work_planned": frozenset(
        {"request_id", "request_sha256", "binding_id", "dedup_key", "request"}
    ),
    "work_claimed": frozenset(
        {
            "request_id", "binding_id", "lease_epoch", "session_id",
            "admission_repository_commit", "packet", "packet_canonical_json",
            "packet_sha256", "launch", "launch_canonical_json", "launch_sha256",
        }
    ),
    "claim_rolled_back": frozenset(
        {"request_id", "binding_id", "lease_epoch", "session_id", "reason_code"}
    ),
    "session_starting": frozenset({"request_id", "binding_id", "lease_epoch", "session_id"}),
    "session_running": frozenset(
        {"request_id", "binding_id", "lease_epoch", "session_id", "started_sha256", "ready_sha256"}
    ),
    "session_degraded": frozenset(
        {"request_id", "binding_id", "lease_epoch", "session_id", "started_sha256", "reason_code"}
    ),
    "session_terminal": frozenset(
        {
            "request_id", "binding_id", "lease_epoch", "session_id", "outcome",
            "reason_code", "started_sha256", "ready_sha256", "result_sha256",
            "terminal_sha256", "process_exit_sha256",
        }
    ),
    "request_accepted": frozenset(
        {"request_id", "binding_id", "lease_epoch", "session_id", "packet_sha256", "recovery_created"}
    ),
    "request_duplicate": frozenset(
        {"request_id", "original_request_id", "binding_id", "lease_epoch", "session_id", "packet_sha256"}
    ),
    "request_rejected": frozenset({"request_id", "binding_id", "reason_code"}),
    "control_stop_accepted": frozenset({"control_id", "target_daemon_incarnation_id"}),
    "control_stop_rejected": frozenset(
        {"control_id", "target_daemon_incarnation_id", "reason_code"}
    ),
}
_INDEX_EVENTS = {
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
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.ASCII,
)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z",
    re.ASCII,
)
_NODE_ADDRESS = re.compile(
    r"(?:runtime#kernel|runtime/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}#synthetic)",
    re.ASCII,
)
_REASON = re.compile(r"[a-z0-9-]+", re.ASCII)
_INDEX_NAMES = frozenset(
    {"request_index", "dedup_index", "session_index", "control_index"}
)
_INDEX_IMAGE_NAMES = {
    "daemon_adopted_session": frozenset({"session_index"}),
    "work_planned": frozenset({"request_index"}),
    "work_claimed": frozenset({"session_index"}),
    "claim_rolled_back": frozenset({"session_index"}),
    "session_starting": frozenset({"session_index"}),
    "session_running": frozenset({"session_index"}),
    "session_degraded": frozenset({"session_index"}),
    "session_terminal": frozenset({"session_index"}),
    "request_accepted": frozenset({"request_index", "dedup_index", "session_index"}),
    "request_duplicate": frozenset({"request_index"}),
    "request_rejected": frozenset({"request_index"}),
    "control_stop_accepted": frozenset({"control_index"}),
    "control_stop_rejected": frozenset({"control_index"}),
}


def _wire_uuid(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise HtError(f"WAL {label} must be a canonical lowercase UUID (B1 addendum §1)")


def _wire_sha(value: Any, label: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise HtError(f"WAL {label} must be a lowercase SHA-256 (B1 addendum §1)")


def _wire_positive(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HtError(f"WAL {label} must be an exact positive integer (B1 addendum §1)")


def _wire_event_structure(record: dict[str, Any]) -> None:
    """Validate event-local wire types before final-frame classification."""

    event = record["event"]
    uuid_fields = {
        "daemon_started": ("daemon_incarnation_id",),
        "daemon_stopped": ("daemon_incarnation_id", "control_id"),
        "daemon_adopted_session": ("daemon_incarnation_id", "binding_id", "session_id"),
        "work_planned": ("request_id", "binding_id"),
        "work_claimed": ("request_id", "binding_id", "session_id"),
        "claim_rolled_back": ("request_id", "binding_id", "session_id"),
        "session_starting": ("request_id", "binding_id", "session_id"),
        "session_running": ("request_id", "binding_id", "session_id"),
        "session_degraded": ("request_id", "binding_id", "session_id"),
        "session_terminal": ("request_id", "binding_id", "session_id"),
        "request_accepted": ("request_id", "binding_id", "session_id"),
        "request_duplicate": (
            "request_id", "original_request_id", "binding_id", "session_id"
        ),
        "control_stop_accepted": ("control_id", "target_daemon_incarnation_id"),
        "control_stop_rejected": ("control_id", "target_daemon_incarnation_id"),
    }.get(event, ())
    for name in uuid_fields:
        _wire_uuid(record[name], name)
    if event == "request_rejected":
        _wire_uuid(record["request_id"], "request_id")
        _wire_uuid(record["binding_id"], "binding_id", nullable=True)

    if "lease_epoch" in record:
        _wire_positive(record["lease_epoch"], "lease_epoch")
    for name in (
        "request_sha256", "dedup_key", "packet_sha256", "launch_sha256",
        "started_sha256", "ready_sha256",
    ):
        if name in record:
            _wire_sha(
                record[name], name,
                nullable=event == "session_terminal" and name in {"started_sha256", "ready_sha256"},
            )
    for name in ("result_sha256", "terminal_sha256", "process_exit_sha256"):
        if name in record:
            _wire_sha(record[name], name, nullable=True)

    if event == "wal_tail_truncated":
        if not isinstance(record["tail_reason"], str) or not record["tail_reason"]:
            raise HtError("WAL tail_reason must be nonempty text (B1 §10)")
        offset = record["tail_byte_offset"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise HtError("WAL tail_byte_offset must be a nonnegative integer (B1 §10)")
        _wire_positive(record["tail_discarded_byte_count"], "tail_discarded_byte_count")
        _wire_sha(record["tail_discarded_sha256"], "tail_discarded_sha256")
    elif event == "work_planned":
        if not isinstance(record["request"], dict):
            raise HtError("WAL work_planned request must be an object (B1 addendum §1)")
        try:
            validate_request_object(record["request"])
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise HtError(f"WAL request identity is malformed: {exc} (B1 addendum §1)") from exc
        request_bytes = canonical_json_bytes(record["request"])
        if hashlib.sha256(request_bytes).hexdigest() != record["request_sha256"]:
            raise HtError("WAL request hash differs from canonical request (B1 addendum §1)")
        if derive_dedup_key(record["request"]) != record["dedup_key"]:
            raise HtError("WAL dedup key differs from canonical request identity (B1 §8)")
    elif event == "work_claimed":
        if not isinstance(record["admission_repository_commit"], str) or _OID.fullmatch(
            record["admission_repository_commit"]
        ) is None:
            raise HtError("WAL admission repository commit must be a Git OID (B1 addendum §1)")
        for name, schema_name in (
            ("packet", "session-packet.schema.json"),
            ("launch", "launch.schema.json"),
        ):
            if not isinstance(record[name], dict):
                raise HtError(f"WAL {name} must be an object (B1 addendum §1)")
            try:
                if name == "packet":
                    validate_session_packet_object(record[name])
                else:
                    validate(schema_name, record[name])
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                raise HtError(f"WAL {name} identity is malformed: {exc} (B1 addendum §1)") from exc
            canonical = record[f"{name}_canonical_json"]
            expected_bytes = canonical_json_bytes(record[name])
            try:
                supplied_bytes = canonical.encode("utf-8") if isinstance(canonical, str) else None
            except UnicodeEncodeError as exc:
                raise HtError(
                    f"WAL {name} canonical JSON is not UTF-8 (B1 addendum §1)"
                ) from exc
            if supplied_bytes != expected_bytes:
                raise HtError(f"WAL {name} canonical JSON must exactly encode its object (B1 addendum §1)")
            if hashlib.sha256(expected_bytes).hexdigest() != record[f"{name}_sha256"]:
                raise HtError(f"WAL {name} hash differs from its canonical JSON (B1 addendum §1)")
        packet = record["packet"]
        launch = record["launch"]
        identity_names = (
            "runtime_id", "request_id", "binding_id", "node_address",
            "lease_epoch", "session_id", "role", "wrapper_instance_id",
            "helper_instance_id",
        )
        if any(packet.get(name) != launch.get(name) for name in identity_names):
            raise HtError("WAL claim packet/launch identities differ (B1 addendum §1)")
        expected_address = f"runtime/{record['binding_id']}#synthetic"
        if any(
            packet.get(name) != record[name]
            for name in ("request_id", "binding_id", "lease_epoch", "session_id")
        ) or packet.get("node_address") != expected_address or record["node_address"] != expected_address:
            raise HtError("WAL claim event differs from packet identity (B1 addendum §1)")
        if (
            packet["admission_repository_commit"] != record["admission_repository_commit"]
            or packet["fence"]
            != {
                "binding_id": record["binding_id"],
                "lease_epoch": record["lease_epoch"],
                "session_id": record["session_id"],
            }
            or launch["packet_sha256"] != record["packet_sha256"]
            or launch["packet_relative_path"]
            != f"sessions/{record['session_id']}/packet.json"
            or packet["wrapper_instance_id"] == packet["helper_instance_id"]
        ):
            raise HtError("WAL claim artifact relations are incoherent (B1 addendum §1)")
    elif event in {"claim_rolled_back", "session_degraded", "request_rejected", "control_stop_rejected"}:
        reason = record["reason_code"]
        if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
            raise HtError("WAL reason_code must be a stable reason token (B1 addendum §1)")
        fixed = {
            "claim_rolled_back": "boot-recovery-pre-start",
            "session_degraded": "wrapper-exit-observed-custody-held",
            "control_stop_rejected": "stale-daemon-incarnation",
        }.get(event)
        if fixed is not None and reason != fixed:
            raise HtError(f"WAL {event} reason_code is not exact (B1 addendum §1)")
    elif event == "session_terminal":
        if record["outcome"] not in {"SUCCEEDED", "FAILED", "crashed"}:
            raise HtError("WAL session terminal outcome is invalid (B1 addendum §1)")
        reason = record["reason_code"]
        if reason is not None and (
            not isinstance(reason, str) or _REASON.fullmatch(reason) is None
        ):
            raise HtError("WAL terminal reason_code is invalid (B1 addendum §1)")
    elif event == "request_accepted" and not isinstance(record["recovery_created"], bool):
        raise HtError("WAL recovery_created must be Boolean (B1 addendum §5)")

    images = record["binding_delta"]["post_images"]
    if event == "wal_tail_truncated":
        expected_addresses: set[str] = set()
    elif event in {
        "daemon_started", "daemon_stopped", "request_duplicate",
        "control_stop_accepted", "control_stop_rejected",
    } or (event == "request_rejected" and record["binding_id"] is None):
        expected_addresses = {KERNEL_ADDRESS}
    else:
        expected_addresses = {
            KERNEL_ADDRESS, f"runtime/{record['binding_id']}#synthetic"
        }
    if set(images) != expected_addresses:
        raise HtError(f"WAL {event} binding post-image addresses are not exact (B1 addendum §1)")
    expected_node = KERNEL_ADDRESS
    if event in {
        "work_planned", "work_claimed", "claim_rolled_back", "session_starting",
        "session_running", "session_degraded", "session_terminal", "request_accepted",
    } or (event == "request_rejected" and record["binding_id"] is not None):
        expected_node = f"runtime/{record['binding_id']}#synthetic"
    if record["node_address"] != expected_node:
        raise HtError(f"WAL {event} node_address is not exact (B1 addendum §1)")
    if images:
        kernel = images[KERNEL_ADDRESS]
        runtime_id = kernel.get("runtime_id") if isinstance(kernel, dict) else None
        try:
            validate_binding_map(images, runtime_id)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise HtError(f"WAL {event} binding post-images are malformed: {exc} (B1 addendum §1)") from exc

    if event in _INDEX_EVENTS:
        index_images = record["index_delta"]["post_images"]
        expected_indexes = _INDEX_IMAGE_NAMES[event]
        if set(index_images) != expected_indexes:
            raise HtError(f"WAL {event} index post-image names are not exact (B1 addendum §1)")
        structural_indexes = {
            name: index_images.get(name, {}) for name in _INDEX_NAMES
        }
        try:
            validate_indexes(structural_indexes)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise HtError(f"WAL {event} index post-images are malformed: {exc} (B1 addendum §1)") from exc


@dataclass(frozen=True)
class TailDisclosure:
    reason: str
    byte_offset: int
    discarded_byte_count: int
    discarded_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "byte_offset": self.byte_offset,
            "discarded_byte_count": self.discarded_byte_count,
            "discarded_sha256": self.discarded_sha256,
        }


@dataclass(frozen=True)
class ParsedWal:
    records: tuple[dict[str, Any], ...]
    clean_prefix: bytes
    tail: TailDisclosure | None
    frame_end_offsets: tuple[int, ...]


def crc32_for(record_without_crc: dict[str, Any]) -> int:
    try:
        encoded = json.dumps(
            record_without_crc,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HtError(f"WAL record is not finite JSON: {exc} (B1 §10)") from exc
    return zlib.crc32(encoded) & 0xFFFFFFFF


def frame_record(record_without_crc: dict[str, Any]) -> bytes:
    if not isinstance(record_without_crc, dict):
        raise HtError("WAL frame input must be an object (B1 §10)")
    if "crc32" in record_without_crc:
        raise HtError("WAL frame input must not predefine crc32 (B1 §10)")
    record = dict(record_without_crc)
    record["crc32"] = crc32_for(record_without_crc)
    _validate_record(record, previous_seq=0)
    try:
        payload = json.dumps(
            record, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:  # pragma: no cover - crc path rejects first
        raise HtError(f"WAL record is not finite JSON: {exc} (B1 §10)") from exc
    return str(len(payload)).encode("ascii") + b"\t" + payload + b"\n"


def _validate_record(record: Any, *, previous_seq: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise HtError("WAL payload must be an object (B1 §10)")
    event = record.get("event")
    if not isinstance(event, str) or event not in EVENTS:
        raise HtError(f"unknown runtime WAL event {event!r} (B1 §10)")
    expected_fields = _COMMON_FIELDS | _EVENT_FIELDS[event]
    if event in _INDEX_EVENTS:
        expected_fields |= {"index_delta"}
    if set(record) != expected_fields:
        raise HtError(
            f"runtime WAL {event} fields must be exactly {sorted(expected_fields)} (B1 addendum §1)"
        )
    seq = record["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0 or seq <= previous_seq:
        raise HtError("WAL seq must be an exact increasing positive integer (B1 §10)")
    timestamp = record["ts"]
    if not isinstance(timestamp, str) or _TIMESTAMP.fullmatch(timestamp) is None:
        raise HtError("WAL ts must be canonical UTC RFC-3339 ending in Z (B1 addendum §1)")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as exc:
        raise HtError("WAL ts must be canonical UTC RFC-3339 ending in Z (B1 addendum §1)") from exc
    if parsed_timestamp.tzinfo is None:
        raise HtError("WAL ts must be canonical UTC RFC-3339 ending in Z (B1 addendum §1)")
    if not isinstance(record["node_address"], str) or _NODE_ADDRESS.fullmatch(record["node_address"]) is None:
        raise HtError("WAL node_address must be a canonical runtime address (B1 §10)")
    delta = record["binding_delta"]
    if not isinstance(delta, dict) or set(delta) != {"post_images"} or not isinstance(delta["post_images"], dict):
        raise HtError("WAL binding_delta must contain only object post_images (B1 §9)")
    if any(
        not isinstance(key, str)
        or _NODE_ADDRESS.fullmatch(key) is None
        or not isinstance(value, dict)
        for key, value in delta["post_images"].items()
    ):
        raise HtError("WAL binding post-images must be canonical address/object pairs (B1 §9)")
    if event in _INDEX_EVENTS:
        index_delta = record["index_delta"]
        if (
            not isinstance(index_delta, dict)
            or set(index_delta) != {"post_images"}
            or not isinstance(index_delta["post_images"], dict)
        ):
            raise HtError("WAL index_delta must contain only object post_images (B1 addendum §1)")
        if any(
            key not in _INDEX_NAMES or not isinstance(value, dict)
            for key, value in index_delta["post_images"].items()
        ):
            raise HtError("WAL index post-images must be named index objects (B1 addendum §1)")
    _wire_event_structure(record)
    crc = record["crc32"]
    if isinstance(crc, bool) or not isinstance(crc, int) or not (0 <= crc <= 0xFFFFFFFF):
        raise HtError("WAL crc32 must be an unsigned exact integer (B1 §10)")
    non_crc = {key: value for key, value in record.items() if key != "crc32"}
    if crc != crc32_for(non_crc):
        raise HtError("WAL crc32 does not match payload (B1 §10)")
    return record


def _parse_frame(frame: bytes, *, previous_seq: int) -> dict[str, Any]:
    if b"\t" not in frame:
        raise HtError("missing WAL frame separator (B1 §10)")
    prefix, payload = frame.split(b"\t", 1)
    if not prefix or any(byte < ord("0") or byte > ord("9") for byte in prefix):
        raise HtError("WAL frame length is not ASCII decimal (B1 §10)")
    if int(prefix) != len(payload):
        raise HtError("WAL frame length differs from UTF-8 payload bytes (B1 §10)")
    return _validate_record(strict_loads(payload, label="WAL payload"), previous_seq=previous_seq)


def _tail_reason_code(message: str) -> str:
    checks = (
        ("separator", "missing-frame-separator"),
        ("not ASCII decimal", "invalid-frame-length"),
        ("length differs", "frame-length-mismatch"),
        ("strict WAL payload", "strict-json-invalid"),
        ("crc32 does not match", "crc32-mismatch"),
        ("seq", "invalid-sequence"),
        ("event", "invalid-event"),
        ("node_address", "invalid-node-address"),
        ("binding_delta", "invalid-binding-delta"),
        ("crc32", "invalid-crc32"),
    )
    return next((code for token, code in checks if token in message), "invalid-record")


def parse_bytes(data: bytes) -> ParsedWal:
    if not data:
        return ParsedWal((), b"", None, ())
    parts = data.split(b"\n")
    unterminated = not data.endswith(b"\n")
    if not unterminated:
        parts.pop()
    records: list[dict[str, Any]] = []
    frame_end_offsets: list[int] = []
    offset = 0
    clean_end = 0
    for index, frame in enumerate(parts):
        final = index == len(parts) - 1
        frame_offset = offset
        segment_length = len(frame) + (0 if final and unterminated else 1)
        offset += segment_length
        if final and unterminated:
            discarded = data[frame_offset:]
            return ParsedWal(
                tuple(records),
                data[:clean_end],
                TailDisclosure(
                    "unterminated-final-segment",
                    frame_offset,
                    len(discarded),
                    hashlib.sha256(discarded).hexdigest(),
                ),
                tuple(frame_end_offsets),
            )
        try:
            record = _parse_frame(frame, previous_seq=records[-1]["seq"] if records else 0)
        except HtError as exc:
            if not final:
                raise HtError(
                    f"corrupt non-final WAL frame at byte offset {frame_offset}: "
                    f"{exc.message}"
                ) from exc
            discarded = data[frame_offset:]
            return ParsedWal(
                tuple(records),
                data[:clean_end],
                TailDisclosure(
                    _tail_reason_code(exc.message),
                    frame_offset,
                    len(discarded),
                    hashlib.sha256(discarded).hexdigest(),
                ),
                tuple(frame_end_offsets),
            )
        records.append(record)
        clean_end = offset
        frame_end_offsets.append(clean_end)
    return ParsedWal(tuple(records), data[:clean_end], None, tuple(frame_end_offsets))


def read(path: Path) -> ParsedWal:
    return parse_bytes(read_exact_file(path))
