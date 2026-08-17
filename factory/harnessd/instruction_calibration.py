"""Notary-backed calibration facts for versioned agent instructions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

from harnessd import clock, notary, store


SCHEMA_VERSION = 1
_HARNESS_ROOT = Path(__file__).resolve().parents[1]


def calibration_receipt_path(instruction: Path) -> Path:
    path = Path(instruction)
    return path.with_suffix(path.suffix + ".calibration-receipt.json")


def calibration_record_path(instruction: Path) -> Path:
    path = Path(instruction)
    return path.with_suffix(path.suffix + ".calibration-record.json")


def _portable_path(path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(_HARNESS_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _record_contract_path(value: object) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        path = _HARNESS_ROOT / path
    return path.resolve()


def _canonical_sha(payload: Mapping) -> str:
    raw = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _write_frozen_json(path: Path, payload: Mapping) -> None:
    content = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    store.atomic_replace(Path(path), lambda handle: handle.write(content))
    Path(path).chmod(0o444)


def _load_json(path: Path, *, label: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is unreadable or malformed: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


def mint_instruction_calibration(
    *,
    instruction_path: Path,
    prior_fingerprint: str,
    required_channel: str,
    ratification_record: Path,
    participants: Iterable[str],
    reason: str,
    owner_address: str = "owner+director",
    additional_channel_evidence: Mapping | None = None,
) -> tuple[Path, Path]:
    """Mint or recover one exact instruction calibration record and receipt.

    Fixed adjacent paths ensure a later instruction revision cannot accumulate a
    second apparently-current calibration beside the first. Existing artifacts
    are accepted only while they still verify this exact instruction and exact
    ratification core; drift requires a newly ratified revision, never a
    convenient re-mint.
    """

    instruction = Path(instruction_path).resolve()
    ratification = Path(ratification_record).resolve()
    record_path = calibration_record_path(instruction)
    receipt_path = calibration_receipt_path(instruction)
    channel = str(required_channel or "").strip()
    reason_text = str(reason or "").strip()
    prior = str(prior_fingerprint or "").strip()
    participant_rows = [
        str(value).strip() for value in participants if str(value).strip()
    ]
    if not instruction.is_file():
        raise ValueError(f"instruction is absent: {instruction}")
    if not ratification.is_file():
        raise ValueError(f"ratification record is absent: {ratification}")
    if not channel or not reason_text or not owner_address:
        raise ValueError("channel, reason, and owner_address must be non-empty")
    if len(prior) != 64 or any(value not in "0123456789abcdef" for value in prior):
        raise ValueError("prior_fingerprint must be a lowercase SHA-256")
    if not participant_rows:
        raise ValueError("at least one calibration participant is required")
    additional = dict(additional_channel_evidence or {})
    reserved_evidence = {
        "ratification_record",
        "ratification_sha256",
        "participants",
    }
    if reserved_evidence & set(additional):
        raise ValueError("additional channel evidence cannot replace calibration authority")

    stamped = notary.stamp(instruction)
    current = str(stamped.get("sha256") or "")
    if not current or current == prior:
        raise ValueError("calibration must name a changed exact instruction version")
    ratification_sha = str(notary.stamp(ratification).get("sha256") or "")
    channel_evidence = {
        "ratification_record": _portable_path(ratification),
        "ratification_sha256": ratification_sha,
        "participants": participant_rows,
        **additional,
    }
    core = {
        "schema_version": SCHEMA_VERSION,
        "contract_path": _portable_path(instruction),
        "owner_address": str(owner_address),
        "prior_fingerprint": prior,
        "new_fingerprint": current,
        "reason": reason_text,
        "channel": channel,
        "channel_evidence": channel_evidence,
    }
    revision_id = "instruction-calibration-" + _canonical_sha(core)[:32]

    if record_path.exists() or receipt_path.exists():
        if not record_path.is_file() or not receipt_path.is_file():
            raise ValueError("existing calibration is partial")
        record = _load_json(record_path, label="existing calibration record")
        existing_core = {key: record.get(key) for key in core}
        defects = instruction_calibration_defects(
            instruction_paths=(instruction,),
            required_channel=channel,
            defect_code="INSTRUCTION-CALIBRATION-STALE",
        )
        if (
            record.get("revision_id") != revision_id
            or existing_core != core
            or defects
        ):
            raise ValueError("existing calibration is stale or names different authority")
        return record_path, receipt_path

    now = clock.now_utc()
    record = {
        **core,
        "revision_id": revision_id,
        "authored_at": now,
        "ratified_at": now,
    }
    _write_frozen_json(record_path, record)
    frozen = notary.stamp(instruction, read_only=True)
    receipt = {
        "holder": "production-dispatch",
        "artifact": _portable_path(instruction),
        "stamp": frozen,
        "schema_version": SCHEMA_VERSION,
        "owner_address": str(owner_address),
        "fingerprint": frozen.get("sha256"),
        "calibration_record_ref": record_path.name,
    }
    _write_frozen_json(receipt_path, receipt)
    return record_path, receipt_path


def instruction_calibration_defects(
    *,
    instruction_paths: Iterable[Path],
    required_channel: str,
    defect_code: str,
    allow_uncalibrated: bool = False,
) -> tuple[str, ...]:
    """Return defects unless every exact instruction version is ratified.

    ``allow_uncalibrated`` is a caller-visible test seam. Production callers
    must use the default and separately require explicit model/runtime rows.
    """

    paths = tuple(dict.fromkeys(Path(path).resolve() for path in instruction_paths))
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        return (
            defect_code,
            *(f"{defect_code}: missing instruction {path}" for path in missing),
        )
    if allow_uncalibrated:
        return ()
    defects: list[str] = []
    for path in paths:
        receipt_path = calibration_receipt_path(path)
        try:
            receipt = _load_json(
                receipt_path,
                label=f"calibration receipt for {path.name}",
            )
        except ValueError as exc:
            defects.append(f"{defect_code}: {exc}")
            continue
        record_ref = receipt.get("calibration_record_ref") or receipt.get(
            "revision_record_ref"
        )
        if not isinstance(record_ref, str) or not record_ref.strip():
            defects.append(
                f"{defect_code}: {receipt_path} has no calibration-record reference"
            )
            continue
        record_path = Path(record_ref)
        if not record_path.is_absolute():
            record_path = receipt_path.parent / record_path
        if not record_path.is_file():
            defects.append(
                f"{defect_code}: calibration record does not exist: {record_path}"
            )
            continue
        try:
            record = _load_json(
                record_path,
                label=f"calibration revision record for {path.name}",
            )
            required_record_fields = {
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
            if (
                not required_record_fields.issubset(record)
                or record.get("schema_version") != SCHEMA_VERSION
                or record.get("channel") != required_channel
                or _record_contract_path(record.get("contract_path")) != path
                or record.get("new_fingerprint")
                != (
                    receipt.get("fingerprint")
                    or (receipt.get("stamp") or {}).get("sha256")
                )
                or not record.get("ratified_at")
                or not isinstance(record.get("channel_evidence"), dict)
                or not record.get("channel_evidence")
            ):
                raise ValueError(
                    f"record is not a ratified {required_channel} revision "
                    "for this exact instruction version"
                )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            defects.append(f"{defect_code}: {record_path}: {exc}")
            continue
        try:
            checked = notary.check(receipt, target=path)
        except (OSError, TypeError, ValueError) as exc:
            defects.append(f"{defect_code}: {path}: {exc}")
            continue
        if not checked.ok:
            defects.append(f"{defect_code}: stale instruction receipt for {path}")
    return (defect_code, *defects) if defects else ()
