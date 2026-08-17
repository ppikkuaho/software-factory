"""Read-only committed merge-record views.

The loader deliberately delegates committed-object enumeration to the
composition-gate Discovery boundary.  The values below are the only
membership, partition, ordering, and limiting surface needed by later
merge-record consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from composition_gate.discovery import Discovery, git_read_environment
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from . import jsonio
from .errors import HtError, HtUsageError


_MERGE_RECORD_PREFIX = "tier1/merge-records/"
_RAW_RECORD_NAME = re.compile(r"(?P<id>MR-[0-9]+)\.json")
_CANONICAL_RECORD_NAME = re.compile(r"(?P<id>MR-(?:0|[1-9][0-9]*))\.json")
_GIT_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_MERGE_RECORD_SCHEMA_PATH = "system/schemas/merge_record.schema.json"
_GIT_TIMEOUT_SECONDS = 30.0
_STATUSES = frozenset({"pending", "landed", "consumed", "all"})
_PARTITION_ORDER = {
    "awaiting-verdict": 0,
    "land-ready": 1,
    "verdict-issued/unconsumed": 2,
    "consumed": 3,
}


@dataclass(frozen=True, slots=True)
class MergeRecordEntry:
    """One immutable committed merge record plus selector-only metadata."""

    id: str
    ordinal: int
    partition: str
    pending: bool
    landed: bool
    consumed: bool
    canonical_bytes: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_bytes, bytes):
            object.__setattr__(self, "canonical_bytes", bytes(self.canonical_bytes))

    @property
    def record_bytes(self) -> bytes:
        """Compatibility name for the immutable canonical JSON bytes."""

        return self.canonical_bytes

    def as_dict(self) -> dict[str, Any]:
        """Return a fresh full merge-record object without selector metadata."""

        value = json.loads(self.canonical_bytes.decode("utf-8"))
        if not isinstance(value, dict):  # pragma: no cover - loader invariant
            raise RuntimeError("internal merge-record bytes are not an object")
        return value


@dataclass(frozen=True, slots=True)
class MergeRecordSnapshot:
    """One immutable tuple of records captured from one Discovery snapshot."""

    entries: tuple[MergeRecordEntry, ...]

    def __post_init__(self) -> None:
        # Discovery's stable raw-name ordering is deliberately not the public
        # selector order: MR-10 sorts before MR-2 lexically.  Normalize at the
        # value boundary so every later consumer sees numeric order even when
        # it reads ``entries`` directly instead of calling ``select``.
        object.__setattr__(
            self,
            "entries",
            tuple(sorted(self.entries, key=lambda entry: entry.ordinal)),
        )

    def select(
        self,
        status: str = "all",
        last: int | None = None,
    ) -> tuple[MergeRecordEntry, ...]:
        """Select records using the frozen membership/order/limit contract."""

        _validate_selection(status, last)

        if status == "pending":
            records = _by_ordinal(
                entry for entry in self.entries if entry.pending
            )
            return _last_highest_ordinals(records, last)

        if status == "landed":
            records = _by_ordinal(
                entry for entry in self.entries if entry.landed
            )
            return _last_highest_ordinals(records, last)

        if status == "consumed":
            records = _by_consumed_order(
                entry for entry in self.entries if entry.consumed
            )
            if last is None:
                return records
            return records[:last]

        if last is None:
            return _by_all_order(self.entries)
        if last == 0:
            return ()
        selected = sorted(self.entries, key=lambda entry: entry.ordinal, reverse=True)[
            :last
        ]
        return _by_all_order(selected)

    def unconsumed(self) -> tuple[MergeRecordEntry, ...]:
        """Return all unconsumed records in numeric MR order.

        This is intentionally a view over this snapshot, so later consumers
        can share its committed capture without recapturing Discovery.
        """

        return _by_ordinal(entry for entry in self.entries if not entry.consumed)

    def select_unconsumed(self) -> tuple[MergeRecordEntry, ...]:
        """Explicit alias for consumers that prefer selector-style naming."""

        return self.unconsumed()


def load_merge_record_snapshot(root: str | Path) -> MergeRecordSnapshot:
    """Load and validate every committed merge record from one Git snapshot."""

    try:
        root_path = Path(root).expanduser().resolve()
        discovery = Discovery.capture(root_path)
        documents = discovery.merge_records()
    except Exception as exc:
        raise HtError(f"merge-record snapshot discovery failed: {exc}") from exc

    validator = _committed_merge_record_validator(discovery)

    # JSON Schema treats mathematically integral JSON numbers (for example
    # 1.0) as integers.  Epochs are a serialization and ordering boundary and
    # require an exact Python integer.  Complete this preflight before
    # constructing even one public entry so no caller can observe a partial
    # snapshot or reach a defensive RuntimeError through a selector.
    for document in documents:
        record = document.value
        if "consumed_epoch" in record:
            consumed_epoch = record["consumed_epoch"]
            if consumed_epoch is not None and (
                type(consumed_epoch) is not int or consumed_epoch < 0
            ):
                raise HtError(
                    "merge-record snapshot rejected: consumed_epoch must be null "
                    "or an exact non-negative integer "
                    f"at {document.path}"
                )

    entries: list[MergeRecordEntry] = []
    seen_ordinals: dict[int, str] = {}

    for document in documents:
        path_id = _path_record_id(document.path)
        record = document.value

        _validate_merge_record(validator, record, document.path)

        if not isinstance(record, dict):  # pragma: no cover - Discovery invariant
            raise HtError(
                f"merge-record snapshot rejected: {document.path} is not an object"
            )
        if record.get("id") != path_id:
            raise HtError(
                "merge-record snapshot rejected: committed path/body id mismatch "
                f"at {document.path}"
            )

        canonical_name = _CANONICAL_RECORD_NAME.fullmatch(f"{path_id}.json")
        if canonical_name is None:
            raise HtError(
                "merge-record snapshot rejected: non-canonical numeric id "
                f"{path_id!r} at {document.path}"
            )
        try:
            ordinal = int(canonical_name.group("id")[3:])
        except ValueError as exc:
            raise HtError(
                "merge-record snapshot rejected: numeric id is too large "
                f"at {document.path}"
            ) from exc
        previous = seen_ordinals.get(ordinal)
        if previous is not None:
            raise HtError(
                "merge-record snapshot rejected: duplicate numeric MR ordinal "
                f"{ordinal} at {previous} and {document.path}"
            )
        seen_ordinals[ordinal] = document.path

        gate_verdict = record["gate_verdict"]
        consumed_epoch = record["consumed_epoch"]
        if consumed_epoch is not None and (
            not isinstance(gate_verdict, dict)
            or gate_verdict.get("verdict") != "land"
        ):
            raise HtError(
                "merge-record snapshot rejected: consumed record "
                f"{path_id} must have exact gate verdict 'land'"
            )

        is_consumed = consumed_epoch is not None
        is_unconsumed = not is_consumed
        is_pending = is_unconsumed and (
            gate_verdict is None
            or (
                isinstance(gate_verdict, dict)
                and gate_verdict.get("verdict") == "land"
            )
        )
        is_landed = is_unconsumed and gate_verdict is not None
        if is_consumed:
            partition = "consumed"
        elif gate_verdict is None:
            partition = "awaiting-verdict"
        elif gate_verdict.get("verdict") == "land":
            partition = "land-ready"
        else:
            partition = "verdict-issued/unconsumed"

        try:
            canonical_bytes = jsonio.dumps(record).encode("utf-8")
        except Exception as exc:
            raise HtError(
                f"merge-record snapshot serialization failed at {document.path}: {exc}"
            ) from exc

        entries.append(
            MergeRecordEntry(
                id=path_id,
                ordinal=ordinal,
                partition=partition,
                pending=is_pending,
                landed=is_landed,
                consumed=is_consumed,
                canonical_bytes=canonical_bytes,
            )
        )

    return MergeRecordSnapshot(tuple(entries))


def _run_snapshot_git(discovery: Discovery, args: list[str], *, label: str) -> bytes:
    """Run one bounded read against the repository captured by ``discovery``."""

    try:
        process = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "-C",
                str(discovery.snapshot.root),
                *args,
            ],
            cwd=discovery.snapshot.root,
            env=git_read_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HtError(
            f"merge-record committed schema read failed while {label}: "
            f"{type(exc).__name__}"
        ) from exc
    if process.returncode != 0:
        raise HtError(
            f"merge-record committed schema read failed while {label}: "
            f"git exited {process.returncode}"
        )
    return process.stdout


def _committed_schema_blob(discovery: Discovery) -> bytes:
    """Read the exact merge-record schema blob from the captured commit.

    The worktree path is never consulted.  In particular, this cannot inherit
    ``ht.schemas``' path-keyed validator cache or observe a dirty/deleted schema
    after the commit snapshot has been captured.
    """

    raw = _run_snapshot_git(
        discovery,
        [
            "ls-tree",
            "-z",
            "--full-tree",
            discovery.snapshot.head_tree,
            "--",
            _MERGE_RECORD_SCHEMA_PATH,
        ],
        label="resolving the schema path",
    )
    if not raw:
        raise HtError(
            "merge-record committed schema rejected: missing exact committed path "
            f"{_MERGE_RECORD_SCHEMA_PATH}"
        )
    if not raw.endswith(b"\0"):
        raise HtError(
            "merge-record committed schema rejected: malformed ls-tree output"
        )
    rows = raw[:-1].split(b"\0")
    if len(rows) != 1:
        raise HtError(
            "merge-record committed schema rejected: ambiguous committed path "
            f"{_MERGE_RECORD_SCHEMA_PATH}"
        )
    metadata, separator, raw_path = rows[0].partition(b"\t")
    fields = metadata.split(b" ")
    if not separator or len(fields) != 3 or any(not field for field in fields):
        raise HtError(
            "merge-record committed schema rejected: malformed ls-tree record"
        )
    try:
        mode, object_type, oid = (
            field.decode("ascii", errors="strict") for field in fields
        )
        path = raw_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HtError(
            "merge-record committed schema rejected: non-UTF-8 tree metadata"
        ) from exc
    if path != _MERGE_RECORD_SCHEMA_PATH:
        raise HtError(
            "merge-record committed schema rejected: git returned an unexpected path"
        )
    if mode != "100644" or object_type != "blob":
        raise HtError(
            "merge-record committed schema rejected: exact path must be 100644 blob, "
            f"got {mode} {object_type}"
        )
    if _GIT_OID.fullmatch(oid) is None:
        raise HtError(
            "merge-record committed schema rejected: invalid schema blob object id"
        )
    actual_type = _run_snapshot_git(
        discovery,
        ["cat-file", "-t", oid],
        label="checking the schema object type",
    ).strip()
    if actual_type != b"blob":
        raise HtError(
            "merge-record committed schema rejected: schema object is not a blob"
        )
    return _run_snapshot_git(
        discovery,
        ["cat-file", "blob", oid],
        label="reading the schema blob",
    )


def _deny_uncaptured_schema_resource(uri: str) -> Resource:
    """Closed-registry retrieval seam: never touch a URI outside the blob."""

    raise NoSuchResource(ref=uri)


def _closed_schema_registry(schema: dict[str, Any]) -> Registry:
    """Register only the captured schema itself; deterministically deny more."""

    resource = Resource.from_contents(schema)
    registry = Registry(retrieve=_deny_uncaptured_schema_resource)
    schema_id = schema.get("$id")
    if isinstance(schema_id, str) and schema_id:
        registry = registry.with_resource(schema_id, resource)
    return registry


def _committed_merge_record_validator(discovery: Discovery) -> Draft202012Validator:
    raw = _committed_schema_blob(discovery)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HtError(
            "merge-record committed schema rejected: schema blob is not UTF-8"
        ) from exc
    try:
        schema = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HtError(
            "merge-record committed schema rejected: malformed JSON"
        ) from exc
    if not isinstance(schema, dict):
        raise HtError(
            "merge-record committed schema rejected: schema must be a JSON object"
        )
    try:
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(
            schema,
            registry=_closed_schema_registry(schema),
        )
    except Exception as exc:
        raise HtError(
            "merge-record committed schema rejected: invalid Draft 2020-12 schema "
            f"({type(exc).__name__})"
        ) from exc


def _validate_merge_record(
    validator: Draft202012Validator,
    record: object,
    document_path: str,
) -> None:
    try:
        errors = sorted(
            validator.iter_errors(record),
            key=lambda error: tuple(str(part) for part in error.path),
        )
    except Exception as exc:
        # This includes unresolved or unsupported references.  A schema whose
        # semantics cannot be resolved entirely from the captured blob cannot
        # authorize a partial merge-record view.
        raise HtError(
            "merge-record snapshot validation failed: committed schema could not "
            f"validate {document_path} ({type(exc).__name__})"
        ) from exc
    if not errors:
        return
    error = errors[0]
    location = "/".join(str(part) for part in error.path) or "<root>"
    raise HtError(
        "merge-record snapshot validation failed: schema-nonconforming "
        f"merge_record at '{location}' in {document_path}: {error.message}"
    )


def _path_record_id(path: str) -> str:
    if not path.startswith(_MERGE_RECORD_PREFIX):
        raise HtError(
            "merge-record snapshot rejected: document is outside the direct "
            f"merge-record store ({path})"
        )
    local_name = path[len(_MERGE_RECORD_PREFIX) :]
    if "/" in local_name:
        raise HtError(
            "merge-record snapshot rejected: nested merge-record path "
            f"{path}"
        )
    match = _RAW_RECORD_NAME.fullmatch(local_name)
    if match is None:
        raise HtError(
            "merge-record snapshot rejected: non-canonical merge-record path "
            f"{path}"
        )
    return match.group("id")


def _validate_selection(status: str, last: int | None) -> None:
    if status not in _STATUSES:
        raise HtUsageError(f"unknown merge-record status {status!r}")
    if last is not None and type(last) is not int:
        raise HtUsageError("merge-record --last must be a non-negative integer")
    if last is not None and last < 0:
        raise HtUsageError("merge-record --last must be a non-negative integer")


def _by_ordinal(entries: Any) -> tuple[MergeRecordEntry, ...]:
    return tuple(sorted(entries, key=lambda entry: entry.ordinal))


def _consumed_epoch(entry: MergeRecordEntry) -> int:
    epoch = entry.as_dict()["consumed_epoch"]
    if type(epoch) is not int:  # pragma: no cover - loader/schema invariant
        raise RuntimeError("internal consumed merge record has no integer epoch")
    return epoch


def _by_consumed_order(entries: Any) -> tuple[MergeRecordEntry, ...]:
    return tuple(
        sorted(
            entries,
            key=lambda entry: (_consumed_epoch(entry), entry.ordinal),
            reverse=True,
        )
    )


def _by_all_order(entries: Any) -> tuple[MergeRecordEntry, ...]:
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                _PARTITION_ORDER[entry.partition],
                -_consumed_epoch(entry)
                if entry.partition == "consumed"
                else 0,
                -entry.ordinal
                if entry.partition == "consumed"
                else entry.ordinal,
            ),
        )
    )


def _last_highest_ordinals(
    records: tuple[MergeRecordEntry, ...], last: int | None
) -> tuple[MergeRecordEntry, ...]:
    if last is None:
        return records
    if last == 0:
        return ()
    if last >= len(records):
        return records
    return tuple(sorted(records[-last:], key=lambda entry: entry.ordinal))


__all__ = [
    "MergeRecordEntry",
    "MergeRecordSnapshot",
    "load_merge_record_snapshot",
]
