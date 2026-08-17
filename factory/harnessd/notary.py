"""One byte-identity primitive for frozen harness artifacts.

The notary owns only filesystem facts:

* ``stamp`` fingerprints one file or a caller-selected file set, optionally snapshots the
  exact bytes and/or removes every write bit;
* ``receipt`` records which holder was handed which stamped version;
* ``check`` compares a live file/file set with a prior stamp or receipt.

Selection and disposition stay with callers.  The notary does not know what a candidate,
intent-spec, test package, ledger row, refusal, or wake means.

Contract amendment policy (ownership, channels, ledger lineage, and wakes) stays outside this
filesystem primitive.  ``restamp`` validates the V1 revision artifact and returns byte lineage;
callers commit that lineage through the single-writer ledger.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


class NotaryError(RuntimeError):
    """Base class for explicit notary refusals."""


class RevisionRecordRequired(NotaryError):
    """A frozen artifact cannot be re-stamped without a revision-record reference."""


class RevisionSchemaDeferred(NotaryError):
    """Compatibility name retained for callers from before the V1 revision schema."""


class RevisionRecordInvalid(NotaryError):
    """A revision record is malformed or does not authorize this exact byte transition."""


@dataclass(frozen=True)
class CheckResult:
    """Structured byte/membership comparison; callers choose wording and disposition."""

    ok: bool
    expected: dict
    current: dict
    mismatches: tuple[dict, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.ok


def stamp(
    target: Path,
    *,
    members: Optional[Iterable[Path]] = None,
    root_label: Optional[str] = None,
    snapshot_to: Optional[Path] = None,
    read_only: bool = False,
) -> dict:
    """Return a deterministic raw-byte stamp for one file or a selected file set.

    ``members is None`` stamps ``target`` as one file and retains the legacy
    ``{"present", "sha256", "bytes"}`` shape used by existing call sites.

    With ``members``, ``target`` is the collection root.  Membership is supplied by the
    caller because domain-specific exclusions (candidate bookkeeping, test caches, and so
    on) are policy, not notary concerns.  The collection fingerprint is the sha256 of the
    same canonical JSON ``files`` mapping returned to the caller.
    """

    path = Path(target)
    if members is None:
        return _stamp_file(path, snapshot_to=snapshot_to, read_only=read_only)

    selected = _normalized_members(path, members)
    files: dict[str, dict] = {}
    for member in selected:
        rel = member.relative_to(path).as_posix()
        snapshot_path = (Path(snapshot_to) / rel) if snapshot_to is not None else None
        files[rel] = _stamp_file(
            member,
            snapshot_to=snapshot_path,
            read_only=read_only,
        )

    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "present": bool(files) and all(value.get("present") is True for value in files.values()),
        "root": root_label if root_label is not None else str(path),
        "files": files,
        "file_count": len(files),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def receipt(
    holder: str,
    artifact: Path,
    stamped: dict,
    *,
    owner_address: Optional[str] = None,
    revision_record_ref: Optional[Path] = None,
) -> dict:
    """Return a JSON-safe record of the exact stamped version handed to ``holder``."""

    held = {
        "holder": str(holder),
        "artifact": str(Path(artifact)),
        "stamp": copy.deepcopy(stamped),
    }
    # Additive contract metadata.  The legacy three-key receipt remains byte-for-byte when
    # callers do not opt into the contract layer.
    if owner_address is not None:
        held.update({
            "schema_version": 1,
            "owner_address": str(owner_address),
            "fingerprint": stamped.get("sha256"),
            "revision_record_ref": (
                str(Path(revision_record_ref))
                if revision_record_ref is not None
                else None
            ),
        })
    return held


def check(
    expected_or_receipt: dict,
    *,
    target: Optional[Path] = None,
    members: Optional[Iterable[Path]] = None,
    root_label: Optional[str] = None,
) -> CheckResult:
    """Compare live bytes/membership with a prior stamp or receipt.

    Expected keys that are absent are not invented as obligations.  This lets a caller pin
    only a manifest sha while another caller pins full file membership without duplicating
    byte-comparison logic.
    """

    expected, resolved_target = _unwrap_expected(expected_or_receipt, target)
    current = stamp(
        resolved_target,
        members=members,
        root_label=root_label,
    )
    mismatches: list[dict] = []

    expected_files = expected.get("files")
    current_files = current.get("files")
    if isinstance(expected_files, dict) and isinstance(current_files, dict):
        expected_names = set(expected_files)
        current_names = set(current_files)
        for rel in sorted(expected_names - current_names):
            mismatches.append({
                "kind": "removed",
                "path": rel,
                "expected": copy.deepcopy(expected_files[rel]),
                "current": {"present": False},
            })
        for rel in sorted(current_names - expected_names):
            mismatches.append({
                "kind": "added",
                "path": rel,
                "expected": {"present": False},
                "current": copy.deepcopy(current_files[rel]),
            })
        for rel in sorted(expected_names & current_names):
            _compare_file_stamp(
                expected_files[rel],
                current_files[rel],
                path=rel,
                mismatches=mismatches,
            )
    else:
        _compare_file_stamp(
            expected,
            current,
            path=str(resolved_target),
            mismatches=mismatches,
        )

    if expected.get("sha256") is not None and current.get("sha256") != expected.get("sha256"):
        if not any(
            item.get("kind") == "sha256" and item.get("path") == str(resolved_target)
            for item in mismatches
        ):
            mismatches.append({
                "kind": "sha256",
                "path": str(resolved_target),
                "expected": expected.get("sha256"),
                "current": current.get("sha256"),
            })
    if expected.get("file_count") is not None and (
        current.get("file_count") != expected.get("file_count")
    ):
        mismatches.append({
            "kind": "file_count",
            "path": str(resolved_target),
            "expected": expected.get("file_count"),
            "current": current.get("file_count"),
        })

    return CheckResult(
        ok=not mismatches,
        expected=copy.deepcopy(expected),
        current=current,
        mismatches=tuple(mismatches),
    )


def restamp(
    target: Path,
    *,
    prior_receipt: dict,
    revision_record_ref: Optional[Path] = None,
    members: Optional[Iterable[Path]] = None,
    root_label: Optional[str] = None,
    read_only: bool = True,
) -> dict:
    """Validate one V1 revision record and freeze the exact authorized new bytes.

    Ownership and ratification-channel checks belong to the caller.  The notary proves only
    that the record names this physical contract, starts at the handed prior fingerprint, and
    ends at the bytes currently present at ``target``.
    """

    if revision_record_ref is None:
        raise RevisionRecordRequired(
            "re-stamping frozen content requires an explicit revision-record artifact reference"
        )
    record_path = Path(revision_record_ref)
    record = _load_revision_record(record_path)
    resolved_target = Path(target).resolve()
    try:
        record_target = Path(str(record["contract_path"])).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RevisionRecordInvalid(
            f"revision record contract_path is invalid: {exc}"
        ) from exc
    if record_target != resolved_target:
        raise RevisionRecordInvalid(
            f"revision record contract_path {record_target} does not match target {resolved_target}"
        )

    prior_stamp, _prior_target = _unwrap_expected(prior_receipt, resolved_target)
    prior_fingerprint = prior_receipt.get("fingerprint") or prior_stamp.get("sha256")
    if not prior_fingerprint:
        raise RevisionRecordInvalid("prior receipt has no sha256 fingerprint")
    if record["prior_fingerprint"] != prior_fingerprint:
        raise RevisionRecordInvalid(
            "revision record prior_fingerprint does not match the prior receipt: "
            f"{record['prior_fingerprint']!r} != {prior_fingerprint!r}"
        )

    current = stamp(
        resolved_target,
        members=members,
        root_label=root_label,
    )
    current_fingerprint = current.get("sha256")
    if not current_fingerprint:
        raise RevisionRecordInvalid(
            f"revision target {resolved_target} is missing or has no fingerprint"
        )
    if record["new_fingerprint"] != current_fingerprint:
        raise RevisionRecordInvalid(
            "revision record new_fingerprint does not match current contract bytes: "
            f"{record['new_fingerprint']!r} != {current_fingerprint!r}"
        )
    if record["new_fingerprint"] == record["prior_fingerprint"]:
        raise RevisionRecordInvalid(
            "revision record does not change the contract fingerprint"
        )

    frozen = stamp(
        resolved_target,
        members=members,
        root_label=root_label,
        read_only=read_only,
    )
    return {
        "stamp": frozen,
        "lineage": {
            "revision_id": record["revision_id"],
            "revision_record_ref": str(record_path),
            "prior_fingerprint": record["prior_fingerprint"],
            "new_fingerprint": record["new_fingerprint"],
        },
    }


def _load_revision_record(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RevisionRecordInvalid(f"revision record {path} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise RevisionRecordInvalid("revision record must be a JSON object")
    required = {
        "schema_version",
        "revision_id",
        "contract_path",
        "owner_address",
        "prior_fingerprint",
        "new_fingerprint",
        "reason",
        "authored_at",
        "ratified_at",
        "channel",
        "channel_evidence",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise RevisionRecordInvalid(
            f"revision record is missing required fields: {', '.join(missing)}"
        )
    if payload.get("schema_version") != 1:
        raise RevisionRecordInvalid(
            f"revision record schema_version must be 1, got {payload.get('schema_version')!r}"
        )
    for key in (
        "revision_id",
        "contract_path",
        "owner_address",
        "prior_fingerprint",
        "new_fingerprint",
        "reason",
        "authored_at",
        "ratified_at",
        "channel",
    ):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise RevisionRecordInvalid(f"revision record {key} must be a non-empty string")
    if not isinstance(payload.get("channel_evidence"), dict):
        raise RevisionRecordInvalid("revision record channel_evidence must be an object")
    return payload


def _stamp_file(
    path: Path,
    *,
    snapshot_to: Optional[Path],
    read_only: bool,
) -> dict:
    try:
        raw = path.read_bytes()
    except OSError:
        return {"present": False}

    if snapshot_to is not None:
        _atomic_replace_bytes(Path(snapshot_to), raw)
    if read_only:
        path.chmod(0o444)
    return {
        "present": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def _normalized_members(root: Path, members: Iterable[Path]) -> list[Path]:
    selected: dict[str, Path] = {}
    for raw in members:
        path = Path(raw)
        try:
            rel = path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"notary member {path} is outside root {root}") from exc
        if not rel.parts or any(part == ".." for part in rel.parts):
            raise ValueError(f"invalid notary member {path} for root {root}")
        selected[rel.as_posix()] = path
    return [selected[rel] for rel in sorted(selected)]


def _unwrap_expected(expected_or_receipt: dict, target: Optional[Path]) -> tuple[dict, Path]:
    if not isinstance(expected_or_receipt, dict):
        raise TypeError("expected stamp or receipt must be a dict")
    if isinstance(expected_or_receipt.get("stamp"), dict):
        expected = expected_or_receipt["stamp"]
        artifact = expected_or_receipt.get("artifact")
        if target is None and not artifact:
            raise ValueError("receipt has no artifact path and check target was not supplied")
        resolved = Path(target) if target is not None else Path(str(artifact))
        return expected, resolved
    if target is None:
        raise ValueError("check target is required for a bare stamp")
    return expected_or_receipt, Path(target)


def _compare_file_stamp(
    expected: dict,
    current: dict,
    *,
    path: str,
    mismatches: list[dict],
) -> None:
    expected_present = expected.get("present")
    current_present = current.get("present")
    if expected_present is not None and current_present != expected_present:
        mismatches.append({
            "kind": "missing" if expected_present else "unexpected",
            "path": path,
            "expected": expected_present,
            "current": current_present,
        })
        return
    if expected.get("sha256") is not None and current.get("sha256") != expected.get("sha256"):
        mismatches.append({
            "kind": "sha256",
            "path": path,
            "expected": expected.get("sha256"),
            "current": current.get("sha256"),
        })
    if expected.get("bytes") is not None and current.get("bytes") != expected.get("bytes"):
        mismatches.append({
            "kind": "bytes",
            "path": path,
            "expected": expected.get("bytes"),
            "current": current.get("bytes"),
        })


def _atomic_replace_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise
