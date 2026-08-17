"""Read-only harness runtime capture for the opt-in observatory audit path.

This module deliberately does not import :mod:`harnessd`.  It implements the
small, frozen reader contract directly: capture the four runtime files under
the already-existing daemon lock, validate the framed WAL, and expose only the
safe provenance and normalized negative events needed by the observatory.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


AUDIT_SCHEMA_VERSION = "observatory-runtime-audit/1.0.0"
_RUNTIME_FILES = (
    "runtime.json",
    "binding-ledger.json",
    "run-ledger.jsonl",
    ".harnessd.lock",
)
_LEVELS = frozenset({"L1", "L2", "L3", "L4", "L5"})


class RuntimeAuditError(ValueError):
    """The requested runtime cannot be audited without guessing."""


class _DuplicateKey(ValueError):
    pass


class _NonFiniteNumber(ValueError):
    pass


def _reject_nonfinite_number(token: str) -> None:
    raise _NonFiniteNumber(token)


@dataclass(frozen=True)
class RuntimeAudit:
    """Validated audit material plus non-serializable write-safety identities."""

    events: list[dict[str, Any]]
    provenance: dict[str, Any]
    runtime_root: str
    protected_paths: tuple[str, ...]
    protected_inodes: frozenset[tuple[int, int]]


def _object_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _json_object(data: bytes, source: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_nonfinite_number,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKey,
        _NonFiniteNumber,
    ) as exc:
        raise RuntimeAuditError(
            f"{source} is not strict duplicate-free UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeAuditError(f"{source} must contain a JSON object")
    return value


def _read_fd(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _open_regular_once(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeAuditError(f"runtime input {path.name} is not safely readable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeAuditError(f"runtime input {path.name} must be a regular file")
        return fd, info
    except Exception:
        os.close(fd)
        raise


def _validate_inventory(root: Path) -> None:
    try:
        names = os.listdir(root)
    except OSError as exc:
        raise RuntimeAuditError("audit runtime root is not a readable directory") from exc
    required = set(_RUNTIME_FILES)
    missing = sorted(required.difference(names))
    if missing:
        raise RuntimeAuditError(
            "audit runtime is missing required exact child name(s): " + ", ".join(missing)
        )

    # An alias beside the exact file is ambiguous to humans and to filesystems
    # with different case/normalization rules.  Reject it rather than picking one.
    normalized = {
        unicodedata.normalize("NFC", expected).casefold(): expected
        for expected in _RUNTIME_FILES
    }
    for name in names:
        target = normalized.get(unicodedata.normalize("NFC", name).casefold())
        if target is not None and name != target:
            raise RuntimeAuditError(
                f"audit runtime contains an alias of required child {target}"
            )

    for name in _RUNTIME_FILES:
        path = root / name
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise RuntimeAuditError(f"runtime input {name} cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeAuditError(
                f"runtime input {name} must be an exact regular non-symlink child"
            )


def _validate_descriptor(value: dict[str, Any], root: Path) -> None:
    build_id = value.get("build_id")
    declared_root = value.get("runtime_root")
    if not isinstance(build_id, str) or not build_id:
        raise RuntimeAuditError("runtime.json build_id must be a non-empty string")
    if (
        not isinstance(declared_root, str)
        or not declared_root
        or not os.path.isabs(declared_root)
    ):
        raise RuntimeAuditError("runtime.json runtime_root must be a non-empty absolute string")
    if os.path.realpath(declared_root) != str(root):
        raise RuntimeAuditError("runtime.json runtime_root does not resolve to the audited root")


def _validate_bindings(value: dict[str, Any]) -> None:
    for key, binding in value.items():
        if not isinstance(binding, dict):
            raise RuntimeAuditError("binding-ledger.json values must be objects")
        address = binding.get("node_address")
        if not isinstance(address, str) or not address or address != key:
            raise RuntimeAuditError(
                "binding-ledger.json map key must equal its non-empty node_address"
            )
        for field in ("session_uuid", "level", "transcript_path"):
            if field in binding and not isinstance(binding[field], str):
                raise RuntimeAuditError(
                    f"binding-ledger.json optional {field} values must be strings"
                )


def _parse_frame(frame: bytes, offset: int, previous_seq: int) -> dict[str, Any]:
    if b"\t" not in frame:
        raise RuntimeAuditError("missing frame separator")
    prefix, payload = frame.split(b"\t", 1)
    if not prefix or any(byte < 48 or byte > 57 for byte in prefix):
        raise RuntimeAuditError("frame length is not ASCII decimal")
    if int(prefix) != len(payload):
        raise RuntimeAuditError("frame length does not equal UTF-8 payload byte length")
    record = _json_object(payload, "run-ledger.jsonl frame")
    crc = record.get("crc32")
    if isinstance(crc, bool) or not isinstance(crc, int):
        raise RuntimeAuditError("WAL crc32 must be an exact integer")
    non_crc = {key: value for key, value in record.items() if key != "crc32"}
    expected_crc = zlib.crc32(
        json.dumps(non_crc, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ) & 0xFFFFFFFF
    if crc != expected_crc:
        raise RuntimeAuditError("WAL crc32 does not match its payload")
    seq = record.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
        raise RuntimeAuditError("WAL seq must be an exact positive integer")
    if seq <= previous_seq:
        raise RuntimeAuditError("WAL seq must strictly increase")
    for field in ("event", "node_address"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise RuntimeAuditError(f"WAL {field} must be a non-empty string")
    if not isinstance(record.get("binding_delta"), dict):
        raise RuntimeAuditError("WAL binding_delta must be an object")
    return record


def _parse_wal(data: bytes) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not data:
        return [], None

    records: list[dict[str, Any]] = []
    torn_tail = None
    offset = 0
    # LF is the only physical record terminator in the writer contract.  Do not
    # use splitlines(): it also treats CR/VT/FF as boundaries and would invent
    # frames from corrupt payload bytes.
    parts = data.split(b"\n")
    unterminated = not data.endswith(b"\n")
    if not unterminated:
        parts.pop()  # the empty segment after the final, legitimate LF
    for index, frame in enumerate(parts):
        is_last = index == len(parts) - 1
        frame_offset = offset
        offset += len(frame) + (0 if is_last and unterminated else 1)
        if is_last and unterminated:
            torn_tail = {
                "reason": "unterminated final segment",
                "byte_offset": frame_offset,
            }
            break
        try:
            record = _parse_frame(frame, frame_offset, records[-1]["seq"] if records else 0)
        except RuntimeAuditError as exc:
            if not is_last:
                raise RuntimeAuditError(
                    f"run-ledger.jsonl corrupt non-final frame at byte offset {frame_offset}: {exc}"
                ) from exc
            torn_tail = {"reason": str(exc), "byte_offset": frame_offset}
            break
        records.append(record)
    return records, torn_tail


def _event_binding(
    record: dict[str, Any], bindings: dict[str, Any], required_level: str | None = None
) -> dict[str, Any]:
    binding = bindings.get(record["node_address"])
    if not isinstance(binding, dict):
        raise RuntimeAuditError("a ratified WAL event references a missing binding")
    level = binding.get("level")
    if level not in _LEVELS:
        raise RuntimeAuditError("a ratified WAL event binding has an unsupported level")
    if required_level is not None and level != required_level:
        raise RuntimeAuditError(
            f"a ratified WAL event must reference a {required_level} binding"
        )
    return binding


def _common_event(record, binding, caught_at):
    ts = record.get("ts")
    if not isinstance(ts, str) or not ts:
        raise RuntimeAuditError("a normalized WAL event requires a non-empty ts")
    return {
        "seq": record["seq"],
        "ts": ts,
        "event": record["event"],
        "node_address": record["node_address"],
        "caught_at": caught_at,
        "level": binding["level"],
        "source_anchor": f"run-ledger.jsonl#seq={record['seq']}",
    }


def _normalize_events(records, bindings):
    normalized = []
    for record in records:
        event = record["event"]
        delta = record["binding_delta"]
        if event == "gate_bounced":
            binding = _event_binding(record, bindings)
            level = binding["level"]
            if level == "L1":
                raise RuntimeAuditError("L1 producer gate_bounced is not a ratified audit source")
            if delta.get("gate_state") != "gate_bounced" or delta.get("gate_verdict") != "BOUNCE":
                raise RuntimeAuditError("gate_bounced WAL event has malformed safe gate tokens")
            out = _common_event(record, binding, level)
            out.update({"gate_state": "gate_bounced", "gate_verdict": "BOUNCE"})
            normalized.append(out)
        elif event == "plan_alignment_decision_posted":
            binding = _event_binding(record, bindings, required_level="L2")
            decision = delta.get("plan_alignment_decision")
            if (
                delta.get("plan_alignment_state") != "decision_posted"
                or decision not in {"pass", "fail"}
            ):
                raise RuntimeAuditError("plan-alignment WAL event has a malformed decision")
            if decision == "fail":
                out = _common_event(record, binding, "L1")
                out.update(
                    {
                        "plan_alignment_state": "decision_posted",
                        "plan_alignment_decision": "fail",
                    }
                )
                normalized.append(out)
    return normalized


def _join_bundle(bundle_path: str, bindings: dict[str, Any]) -> dict[str, Any]:
    canonical_bundle = os.path.realpath(os.path.abspath(bundle_path))
    matches = []
    for binding in bindings.values():
        if "transcript_path" not in binding:
            continue
        transcript = binding["transcript_path"]
        # Presence of transcript_path makes this a join candidate.  Malformed
        # candidates reject globally; they are never silently skipped merely
        # because their path does not happen to match this bundle.
        session = binding.get("session_uuid")
        level = binding.get("level")
        if (
            not transcript
            or not os.path.isabs(transcript)
            or not isinstance(session, str)
            or not session
            or level not in _LEVELS
        ):
            raise RuntimeAuditError("binding ledger contains a malformed bundle join candidate")
        if os.path.realpath(transcript) == canonical_bundle:
            matches.append(binding)
    if len(matches) != 1:
        raise RuntimeAuditError("canonical bundle path must match exactly one binding")
    match = matches[0]
    return {
        "match_basis": "canonical-transcript-path",
        "node_address": match["node_address"],
        "session_uuid": match["session_uuid"],
        "level": match["level"],
    }


def capture_runtime_audit(runtime_root: str, bundle_path: str) -> RuntimeAudit:
    """Capture and validate one runtime snapshot without creating or writing files."""

    try:
        root = Path(runtime_root).resolve(strict=True)
    except OSError as exc:
        raise RuntimeAuditError("audit runtime root does not exist") from exc
    if not root.is_dir():
        raise RuntimeAuditError("audit runtime root must resolve to a directory")
    _validate_inventory(root)

    bundle_canonical = os.path.realpath(os.path.abspath(bundle_path))
    try:
        bundle_info = os.stat(bundle_canonical)
    except OSError as exc:
        raise RuntimeAuditError("bundle must exist before runtime audit") from exc
    if not stat.S_ISREG(bundle_info.st_mode):
        raise RuntimeAuditError("bundle must resolve to a regular file")

    captures: dict[str, bytes] = {}
    identities: dict[str, os.stat_result] = {}
    lock_path = root / ".harnessd.lock"
    lock_fd, lock_info = _open_regular_once(lock_path)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        identities[".harnessd.lock"] = lock_info
        captures[".harnessd.lock"] = _read_fd(lock_fd)
        for name in _RUNTIME_FILES[:3]:
            fd, info = _open_regular_once(root / name)
            try:
                captures[name] = _read_fd(fd)
                identities[name] = info
            finally:
                os.close(fd)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    descriptor = _json_object(captures["runtime.json"], "runtime.json")
    _validate_descriptor(descriptor, root)
    bindings = _json_object(captures["binding-ledger.json"], "binding-ledger.json")
    _validate_bindings(bindings)
    records, torn_tail = _parse_wal(captures["run-ledger.jsonl"])
    events = _normalize_events(records, bindings)
    join = _join_bundle(bundle_path, bindings)

    source_hashes = {
        name: hashlib.sha256(captures[name]).hexdigest() for name in _RUNTIME_FILES
    }
    provenance = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "runtime_root": str(root),
        "runtime_build_id": descriptor["build_id"],
        "source_hashes": source_hashes,
        "lock_mode": "shared",
        "wal": {
            "clean_record_count": len(records),
            "last_sequence": records[-1]["seq"] if records else None,
            "torn_tail": torn_tail,
        },
        "bundle_join": join,
    }
    protected_paths = (bundle_canonical,) + tuple(str(root / name) for name in _RUNTIME_FILES)
    protected_inodes = {
        (bundle_info.st_dev, bundle_info.st_ino),
        *((info.st_dev, info.st_ino) for info in identities.values()),
    }
    return RuntimeAudit(
        events=events,
        provenance=provenance,
        runtime_root=str(root),
        protected_paths=protected_paths,
        protected_inodes=frozenset(protected_inodes),
    )


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _check_existing(path: str, audit: RuntimeAudit) -> None:
    canonical = str(Path(path).resolve(strict=False))
    if _is_within(canonical, audit.runtime_root):
        raise RuntimeAuditError("an output/work/statistics path resolves inside the runtime")
    try:
        info = os.stat(path)
    except OSError:
        return
    if (info.st_dev, info.st_ino) in audit.protected_inodes:
        raise RuntimeAuditError("a destination aliases a protected audit input inode")


def _check_tree(root: str, audit: RuntimeAudit) -> None:
    _check_existing(root, audit)
    if not os.path.isdir(root) or os.path.islink(root):
        return
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            _check_existing(os.path.join(directory, name), audit)


def validate_write_destinations(
    audit: RuntimeAudit, *, out_dir: str, work_dir: str, stats_path: str
) -> None:
    """Reject pathname and inode aliases before the pipeline performs any write."""

    protected = set(audit.protected_paths)
    for destination in (out_dir, work_dir, stats_path):
        canonical = str(Path(destination).resolve(strict=False))
        if canonical in protected or _is_within(canonical, audit.runtime_root):
            raise RuntimeAuditError("a destination resolves to a protected audit input")
    _check_tree(out_dir, audit)
    _check_tree(work_dir, audit)
    _check_existing(stats_path, audit)
