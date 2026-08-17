"""Read-only composition-gate decision preparation over one committed snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .packet import (
    PACKET_FORMAT,
    DecisionSnapshot,
    PacketError,
    SnapshotReader,
)
from .rules import evaluate_rules


_RECORD_ID = re.compile(r"MR-(0|[1-9][0-9]*)")
_SCREEN_REF_FIELDS = (
    "output_ref",
    "log_ref",
    "log_sha256",
    "output_sha256",
    "computed",
    "head_commit",
    "head_tree",
    "config_hash",
    "engine_version",
)


class DecisionError(RuntimeError):
    """Stable fail-closed error from committed decision preparation."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)


def _packet_failure(exc: PacketError) -> DecisionError:
    return DecisionError(exc.kind, exc.message)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _committed_record(content: bytes, record_id: str) -> dict[str, Any]:
    try:
        decoded = content.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DecisionError(
            "merge-record-invalid",
            f"committed merge record {record_id} is not canonical readable JSON: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise DecisionError(
            "merge-record-invalid",
            f"committed merge record {record_id} must contain an object",
        )
    if value.get("id") != record_id:
        raise DecisionError(
            "merge-record-invalid",
            f"committed merge record id {value.get('id')!r} does not match {record_id}",
        )
    if value.get("gate_verdict") is not None or value.get("consumed_epoch") is not None:
        raise DecisionError(
            "merge-record-not-decidable",
            f"committed merge record {record_id} is already decided or consumed",
        )
    return value


def _screen_ref(record: Mapping[str, Any]) -> dict[str, Any]:
    screen = record.get("screen")
    source = screen if isinstance(screen, dict) else {}
    return {field: source.get(field) for field in _SCREEN_REF_FIELDS}


def _safe_evidence_path(root: Path, ref: str) -> tuple[str, Path] | None:
    candidate = Path(ref).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
        var_resolved = (root / "var").resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(var_resolved):
        return None
    try:
        normalized = resolved.relative_to(root_resolved).as_posix()
    except ValueError:
        return None
    if not normalized.startswith("var/"):
        return None
    return normalized, resolved


def _capture_screen_evidence(
    root: Path, record: Mapping[str, Any]
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    screen = record.get("screen")
    source = screen if isinstance(screen, dict) else {}
    captured: dict[str, bytes] = {}
    cache: dict[Path, bytes | None] = {}
    fingerprints: list[dict[str, Any]] = []
    for kind in ("output", "log"):
        raw_ref = source.get(f"{kind}_ref")
        observed_sha256: str | None = None
        if isinstance(raw_ref, str) and raw_ref:
            safe = _safe_evidence_path(root, raw_ref)
            if safe is not None:
                normalized, resolved = safe
                if resolved not in cache:
                    try:
                        cache[resolved] = resolved.read_bytes()
                    except OSError:
                        cache[resolved] = None
                content = cache[resolved]
                if content is not None:
                    captured[normalized] = content
                    observed_sha256 = hashlib.sha256(content).hexdigest()
        fingerprints.append(
            {
                "kind": kind,
                "ref": raw_ref if isinstance(raw_ref, str) else None,
                "sha256": observed_sha256,
            }
        )
    return captured, fingerprints


def _base_plan(
    record_id: str,
    decision: Mapping[str, Any],
    screen_ref: dict[str, Any],
    fingerprints: dict[str, Any],
) -> dict[str, Any]:
    route = decision.get("route")
    if route not in {"auto", "stage2", "stuck"}:
        raise DecisionError("rules-invalid", f"rules returned unknown route {route!r}")
    verdict = decision.get("verdict")
    if (
        (route == "auto" and verdict != "land")
        or (route == "stage2" and verdict is not None)
        or (route == "stuck" and verdict != "escalate-stuck")
    ):
        raise DecisionError(
            "rules-invalid", f"rules returned an ineligible verdict for route {route}"
        )
    note = decision.get("note")
    rules_fired = decision.get("rules_fired")
    if not isinstance(note, str) or not note.strip() or not isinstance(rules_fired, list):
        raise DecisionError("rules-invalid", "rules returned an incomplete decision")
    return {
        "record_id": record_id,
        "route": route,
        "stage": 2 if route == "stage2" else 1,
        "verdict": verdict,
        "note": note,
        "rules_fired": rules_fired,
        "screen_ref": screen_ref,
        "fingerprints": fingerprints,
    }


def prepare_decision(root: str | Path, record_id: str) -> dict[str, Any]:
    """Return a deterministic JSON-ready dry-run plan without any mutation."""

    if not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None:
        raise DecisionError(
            "record-id-invalid",
            f"record id must be canonical MR-N, got {record_id!r}",
        )
    try:
        snapshot = DecisionSnapshot.capture(root)
        reader = SnapshotReader(snapshot)
        record_ref = f"tier1/merge-records/{record_id}.json"
        artifact = reader.read(record_ref)
    except PacketError as exc:
        raise _packet_failure(exc) from exc

    record = _committed_record(artifact.content, record_id)
    evidence_bytes, evidence_fingerprints = _capture_screen_evidence(
        snapshot.root, record
    )
    decision = evaluate_rules(
        snapshot.root,
        record,
        evidence_bytes=evidence_bytes,
    )
    fingerprints = {
        "decision_head": snapshot.head_commit,
        "decision_tree": snapshot.head_tree,
        "merge_record": {
            "ref": artifact.source_ref,
            "git_oid": artifact.git_oid,
            "sha256": hashlib.sha256(artifact.content).hexdigest(),
        },
        "screen_evidence": evidence_fingerprints,
    }
    plan = _base_plan(
        record_id,
        decision,
        _screen_ref(record),
        fingerprints,
    )
    if plan["route"] == "stage2":
        plan["packet_requirements"] = {
            "format": PACKET_FORMAT,
            "source_snapshot": {
                "head_commit": snapshot.head_commit,
                "head_tree": snapshot.head_tree,
            },
            "input_hashes": None,
        }
    elif plan["route"] == "stuck":
        plan["rq"] = {
            "required": True,
            "kind": "cgate-escalation",
            "queued_by": "cgate",
            "payload_ref": None,
        }
    return plan


def render_decision(plan: Mapping[str, Any]) -> str:
    """Render one decision plan as canonical deterministic JSON."""

    try:
        return json.dumps(
            plan,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise DecisionError(
            "decision-serialization-failure",
            f"cannot serialize decision plan: {exc}",
        ) from exc


__all__ = ["DecisionError", "prepare_decision", "render_decision"]
