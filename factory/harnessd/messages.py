"""Canonical durable parent-child messages and open questions.

Agents publish immutable ``messages/<message-id>.json`` markers in their own node. The daemon
validates the direct edge, adopts the sender-owned record through the executor, and appends only a
pointer to the recipient inbox. The binding ledger is canonical; inbox rows and checklist items are
derived delivery surfaces and may be replayed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Iterable, NamedTuple

from . import addressing, clock, executor, ledger, states

SCHEMA_VERSION = 1
MESSAGE_TYPE = "message"
QUESTION_OPEN = "open"
QUESTION_ANSWERED = "answered"
QUESTION_WITHDRAWN = "withdrawn"
SYSTEM_SOURCE = "harnessd"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class MessageError(ValueError):
    """A marker is malformed, unsafe, or violates the direct-edge contract."""


class AuthoredMessageResult(NamedTuple):
    """Canonical commit result plus the two paths authored by the convenience seam."""

    ok: bool
    errors: list
    warnings: list
    binding: dict | None
    artifact: Path
    marker: Path


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_message_id(value: object) -> str:
    message_id = str(value or "").strip()
    if not _SAFE_ID.fullmatch(message_id):
        raise MessageError(
            "message_id must be 1-128 characters from [A-Za-z0-9._-] and start alphanumeric"
        )
    return message_id


def escalation_message_id(signal_identity: object) -> str:
    """Deterministic compatibility id for one fenced ESCALATED signal identity."""
    digest = hashlib.sha256(str(signal_identity or "missing-signal-identity").encode("utf-8")).hexdigest()
    return f"legacy-escalation-{digest[:24]}"


def _direct_edge(sender: str, target: str, bindings: dict[str, dict]) -> str:
    if sender == target:
        raise MessageError("a message target must be the other endpoint of a direct edge")
    sender_binding = bindings.get(sender)
    target_binding = bindings.get(target)
    if sender_binding is None:
        raise MessageError(f"sender binding {sender!r} is absent")
    if target_binding is None:
        raise MessageError(f"target binding {target!r} is absent")
    if sender_binding.get("parent_address") == target:
        return "up"
    if target_binding.get("parent_address") == sender:
        return "down"
    raise MessageError(f"{sender!r} and {target!r} are not a direct parent-child edge")


def _owned_path(sender: str, raw: object, runtime_root: Path) -> tuple[Path, str]:
    text = str(raw or "").strip()
    if not text:
        raise MessageError("message artifact is required")
    node_dir = addressing.node_dir(sender, runtime_root).resolve()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = node_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MessageError(f"message artifact is unreadable: {exc}") from exc
    try:
        relative = resolved.relative_to(node_dir)
    except ValueError as exc:
        raise MessageError("message artifact must stay within the sender node") from exc
    if not resolved.is_file():
        raise MessageError("message artifact must be a regular file")
    return resolved, str(relative)


def _normalized_tags(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MessageError("tags must be a list of strings")
    return sorted(dict.fromkeys(item.strip() for item in value if item.strip()))


def _normalized_answer_ref(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MessageError("answers_question must be an object")
    asker = str(value.get("asker_address") or "").strip()
    message_id = safe_message_id(value.get("message_id"))
    if not asker:
        raise MessageError("answers_question.asker_address is required")
    return {"asker_address": asker, "message_id": message_id}


def build_record(
    sender: str,
    marker_path: str | Path,
    *,
    runtime_root: str | Path,
    bindings: dict[str, dict] | None = None,
) -> dict:
    """Parse and freeze one agent marker into its immutable canonical ledger shape."""
    root = Path(runtime_root)
    marker = Path(marker_path)
    try:
        raw = marker.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise MessageError(f"malformed message marker {marker}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MessageError("message marker payload must be an object")
    if payload.get("type") not in {None, MESSAGE_TYPE}:
        raise MessageError(f"unsupported message marker type {payload.get('type')!r}")
    declared_sender = str(payload.get("sender") or "").strip()
    if declared_sender and declared_sender != sender:
        raise MessageError(
            f"message marker sender {declared_sender!r} does not match binding {sender!r}"
        )

    message_id = safe_message_id(payload.get("message_id") or marker.stem)
    if marker.name != f"{message_id}.json":
        raise MessageError("message marker filename must be <message_id>.json")
    target = str(payload.get("to") or "").strip()
    direction = _direct_edge(sender, target, bindings or ledger.all_nodes())
    artifact_path, artifact = _owned_path(sender, payload.get("artifact"), root)
    artifact_raw = artifact_path.read_bytes()
    needs_answer = payload.get("needs_answer", False)
    if not isinstance(needs_answer, bool):
        raise MessageError("needs_answer must be boolean")
    answer_ref = _normalized_answer_ref(payload.get("answers_question"))
    if needs_answer and answer_ref:
        raise MessageError("a message cannot both ask a question and answer one")
    if answer_ref and (answer_ref["asker_address"] != target):
        raise MessageError("an answer must target the referenced asker address")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise MessageError("metadata must be an object")
    summary = str(payload.get("summary") or "").strip()
    tags = _normalized_tags(payload.get("tags"))

    immutable = {
        "schema_version": SCHEMA_VERSION,
        "message_id": message_id,
        "source": sender,
        "target": target,
        "direction": direction,
        "artifact": artifact,
        "artifact_sha256": _sha256_bytes(artifact_raw),
        "marker": str(marker.resolve().relative_to(addressing.node_dir(sender, root).resolve())),
        "marker_sha256": _sha256_bytes(raw),
        "summary": summary,
        "metadata": metadata,
        "tags": tags,
        "needs_answer": needs_answer,
        "answers_question": answer_ref,
    }
    record = dict(immutable)
    record["content_sha256"] = _sha256_bytes(_json_bytes(immutable))
    record["submitted_at"] = clock.now_utc()
    if needs_answer:
        record["question_state"] = (
            QUESTION_WITHDRAWN
            if states.is_terminal((bindings or ledger.all_nodes())[sender].get("state"))
            else QUESTION_OPEN
        )
        if record["question_state"] == QUESTION_WITHDRAWN:
            record["withdrawn_reason"] = "asker_terminal_at_adoption"
            record["withdrawn_at"] = record["submitted_at"]
    return record


def submit_marker(
    sender: str,
    marker_path: str | Path,
    *,
    runtime_root: str | Path,
) -> object:
    """Adopt one marker. Exact resend is idempotent; changed content under one id is refused."""
    bindings = ledger.all_nodes()
    record = build_record(
        sender,
        marker_path,
        runtime_root=runtime_root,
        bindings=bindings,
    )
    result = executor.record_message(
        sender,
        message_id=record["message_id"],
        record=record,
        answers_question=record.get("answers_question"),
    )
    if not result.ok:
        raise MessageError("; ".join(result.errors))
    if not (
        record.get("needs_answer")
        and record.get("question_state") == QUESTION_WITHDRAWN
    ):
        deliver(record, runtime_root=runtime_root)
    # Checklist files are derived caches, but refresh them in the same daemon handling turn so a
    # newly opened/closed question is visible without waiting for the recipient's next hook.
    from . import turn_state

    live = ledger.all_nodes()
    for address in {
        sender,
        str(record.get("target") or ""),
        str((record.get("answers_question") or {}).get("asker_address") or ""),
    }:
        binding = live.get(address)
        if not address or binding is None:
            continue
        try:
            turn_state.refresh_checklist(address, binding, runtime_root=runtime_root)
        except (OSError, ValueError):
            continue
    return result


def author_and_submit(
    sender: str,
    *,
    target: str,
    message_id: str,
    content: str,
    summary: str,
    needs_answer: bool = False,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    answers_question: dict | None = None,
    runtime_root: str | Path,
) -> object:
    """Author one sender-owned marker/artifact pair, then adopt it canonically.

    This is the daemon-side authoring seam used when a harness control action must emit the same
    immutable message an agent would write by hand. Exact replay is idempotent; changed content
    under one id is refused.
    """
    message_id = safe_message_id(message_id)
    directory = addressing.messages_dir(sender, runtime_root)
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / f"{message_id}.md"
    marker = directory / f"{message_id}.json"
    payload = {
        "type": MESSAGE_TYPE,
        "sender": sender,
        "message_id": message_id,
        "to": target,
        "artifact": f"{addressing.MESSAGES_DIRNAME}/{message_id}.md",
        "summary": str(summary or ""),
        "needs_answer": bool(needs_answer),
        "tags": list(tags or []),
        "metadata": dict(metadata or {}),
        "answers_question": answers_question,
    }
    desired_artifact = str(content)
    desired_marker = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if artifact.exists() and artifact.read_text(encoding="utf-8") != desired_artifact:
        raise MessageError(
            f"message id {message_id!r} already has different artifact content; mint a fresh id"
        )
    if marker.exists() and marker.read_text(encoding="utf-8") != desired_marker:
        raise MessageError(
            f"message id {message_id!r} already has different marker content; mint a fresh id"
        )
    if not artifact.exists():
        artifact.write_text(desired_artifact, encoding="utf-8")
    if not marker.exists():
        marker.write_text(desired_marker, encoding="utf-8")
    result = submit_marker(sender, marker, runtime_root=runtime_root)
    return AuthoredMessageResult(
        ok=result.ok,
        errors=result.errors,
        warnings=result.warnings,
        binding=result.binding,
        artifact=artifact,
        marker=marker,
    )


def author_system_message(
    *,
    target: str,
    message_id: str,
    content: str,
    summary: str,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    runtime_root: str | Path,
) -> AuthoredMessageResult:
    """Author one reserved harness-origin fact through the canonical message path.

    The record is housed on the target binding because ``harnessd`` is not a fabricated seat.
    Delivery still uses the ordinary immutable record, recipient inbox pointer, recovery sweep,
    checklist refresh, and wake machinery. Agent-authored direct-edge validation is untouched.
    """
    root = Path(runtime_root)
    message_id = safe_message_id(message_id)
    bindings = ledger.all_nodes()
    if target not in bindings:
        raise MessageError(f"system message target binding {target!r} is absent")
    directory = addressing.messages_dir(target, root)
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / f"{message_id}.md"
    marker = directory / f"{message_id}.json"
    payload = {
        "type": MESSAGE_TYPE,
        "sender": SYSTEM_SOURCE,
        "message_id": message_id,
        "to": target,
        "direction": "system",
        "artifact": f"{addressing.MESSAGES_DIRNAME}/{message_id}.md",
        "summary": str(summary or ""),
        "tags": list(tags or []),
        "metadata": {**dict(metadata or {}), "actor": SYSTEM_SOURCE},
    }
    desired_artifact = str(content)
    desired_marker = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if artifact.exists() and artifact.read_text(encoding="utf-8") != desired_artifact:
        raise MessageError(
            f"system message id {message_id!r} already has different artifact content"
        )
    if marker.exists() and marker.read_text(encoding="utf-8") != desired_marker:
        raise MessageError(
            f"system message id {message_id!r} already has different marker content"
        )
    if not artifact.exists():
        artifact.write_text(desired_artifact, encoding="utf-8")
    if not marker.exists():
        marker.write_text(desired_marker, encoding="utf-8")

    artifact_raw = artifact.read_bytes()
    marker_raw = marker.read_bytes()
    immutable = {
        "schema_version": SCHEMA_VERSION,
        "message_id": message_id,
        "source": SYSTEM_SOURCE,
        "target": target,
        "direction": "system",
        "artifact": f"{addressing.MESSAGES_DIRNAME}/{message_id}.md",
        "artifact_sha256": _sha256_bytes(artifact_raw),
        "marker": f"{addressing.MESSAGES_DIRNAME}/{message_id}.json",
        "marker_sha256": _sha256_bytes(marker_raw),
        "summary": str(summary or "").strip(),
        "metadata": {**dict(metadata or {}), "actor": SYSTEM_SOURCE},
        "tags": _normalized_tags(tags or []),
        "needs_answer": False,
        "answers_question": None,
    }
    record = dict(immutable)
    record["content_sha256"] = _sha256_bytes(_json_bytes(immutable))
    record["submitted_at"] = clock.now_utc()
    result = executor.record_message(
        target,
        message_id=message_id,
        record=record,
        event="message_recorded",
        summary=f"harness-system message {message_id} recorded for {target}",
    )
    if not result.ok:
        raise MessageError("; ".join(result.errors))
    deliver(record, runtime_root=root)
    from . import turn_state

    try:
        turn_state.refresh_checklist(
            target,
            ledger.read_binding(target) or bindings[target],
            runtime_root=root,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    return AuthoredMessageResult(
        ok=result.ok,
        errors=result.errors,
        warnings=result.warnings,
        binding=result.binding,
        artifact=artifact,
        marker=marker,
    )


def submit_compat_message(
    sender: str,
    *,
    target: str,
    message_id: str,
    artifact: str | Path,
    marker: str | Path,
    summary: str,
    needs_answer: bool = False,
    metadata: dict | None = None,
    tags: list[str] | None = None,
    answers_question: dict | None = None,
    deliver_pointer: bool = False,
    extra_sender_delta: dict | None = None,
    event: str | None = None,
    event_summary: str | None = None,
    runtime_root: str | Path,
) -> object:
    """Thin-shim an older transport into the canonical record without inventing parallel truth."""
    root = Path(runtime_root)
    bindings = ledger.all_nodes()
    message_id = safe_message_id(message_id)
    direction = _direct_edge(sender, target, bindings)
    artifact_path, artifact_rel = _owned_path(sender, artifact, root)
    marker_path, marker_rel = _owned_path(sender, marker, root)
    answer_ref = _normalized_answer_ref(answers_question)
    if needs_answer and answer_ref:
        raise MessageError("a compatibility message cannot both ask and answer")
    if answer_ref and answer_ref["asker_address"] != target:
        raise MessageError("compatibility answer target must be its referenced asker")
    artifact_raw = artifact_path.read_bytes()
    marker_raw = marker_path.read_bytes()
    immutable = {
        "schema_version": SCHEMA_VERSION,
        "message_id": message_id,
        "source": sender,
        "target": target,
        "direction": direction,
        "artifact": artifact_rel,
        "artifact_sha256": _sha256_bytes(artifact_raw),
        "marker": marker_rel,
        "marker_sha256": _sha256_bytes(marker_raw),
        "summary": str(summary or "").strip(),
        "metadata": dict(metadata or {}),
        "tags": _normalized_tags(tags or []),
        "needs_answer": bool(needs_answer),
        "answers_question": answer_ref,
        "compat_delivery": not deliver_pointer,
    }
    record = dict(immutable)
    record["content_sha256"] = _sha256_bytes(_json_bytes(immutable))
    record["submitted_at"] = clock.now_utc()
    if needs_answer:
        record["question_state"] = (
            QUESTION_WITHDRAWN
            if states.is_terminal(bindings[sender].get("state"))
            else QUESTION_OPEN
        )
        if record["question_state"] == QUESTION_WITHDRAWN:
            record["withdrawn_reason"] = "asker_terminal_at_adoption"
            record["withdrawn_at"] = record["submitted_at"]
    result = executor.record_message(
        sender,
        message_id=message_id,
        record=record,
        answers_question=answer_ref,
        extra_sender_delta=extra_sender_delta,
        event=event,
        summary=event_summary,
    )
    if not result.ok:
        raise MessageError("; ".join(result.errors))
    if deliver_pointer and not (
        needs_answer and record.get("question_state") == QUESTION_WITHDRAWN
    ):
        deliver(record, runtime_root=root)
    return result


def _inbox_has_message(path: Path, sender: str, message_id: str) -> bool:
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for raw in rows:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if (
            isinstance(row, dict)
            and row.get("type") == MESSAGE_TYPE
            and row.get("sender") == sender
            and row.get("message_id") == message_id
        ):
            return True
    return False


def deliver(record: dict, *, runtime_root: str | Path) -> bool:
    """Append the canonical pointer row if absent. Returns True only for a new append."""
    target = str(record["target"])
    inbox = addressing.inbox_path(target, runtime_root)
    if _inbox_has_message(inbox, str(record["source"]), str(record["message_id"])):
        return False
    inbox.parent.mkdir(parents=True, exist_ok=True)
    pointer = {
        "type": MESSAGE_TYPE,
        "sender": record["source"],
        "message_id": record["message_id"],
        "artifact": record["artifact"],
        "artifact_sha256": record["artifact_sha256"],
        "needs_answer": bool(record.get("needs_answer")),
        "answers_question": record.get("answers_question"),
        "tags": list(record.get("tags") or []),
    }
    with inbox.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(pointer, sort_keys=True, ensure_ascii=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def iter_records(bindings: dict[str, dict] | None = None) -> Iterable[dict]:
    for binding in (bindings or ledger.all_nodes()).values():
        records = binding.get("messages") or {}
        if not isinstance(records, dict):
            continue
        for record in records.values():
            if isinstance(record, dict):
                yield record


def recover_deliveries(*, runtime_root: str | Path) -> int:
    """Replay lost pointer appends from canonical records."""
    delivered = 0
    for record in iter_records():
        if record.get("compat_delivery"):
            continue
        if (
            record.get("needs_answer")
            and record.get("question_state") != QUESTION_OPEN
        ):
            continue
        try:
            delivered += int(deliver(record, runtime_root=runtime_root))
        except (OSError, KeyError, TypeError, ValueError):
            continue
    return delivered


def open_questions_for(
    recipient: str,
    *,
    bindings: dict[str, dict] | None = None,
) -> list[dict]:
    """Project address-bound recipient obligations from sender-owned canonical rows."""
    found = [
        record
        for record in iter_records(bindings)
        if record.get("target") == recipient
        and record.get("needs_answer")
        and record.get("question_state") == QUESTION_OPEN
    ]
    return sorted(found, key=lambda row: (str(row.get("source")), str(row.get("message_id"))))


def open_questions_asked_by(binding: dict) -> list[dict]:
    records = binding.get("messages") or {}
    if not isinstance(records, dict):
        return []
    return [
        record
        for record in records.values()
        if isinstance(record, dict)
        and record.get("needs_answer")
        and record.get("question_state") == QUESTION_OPEN
    ]


def withdraw_terminal_questions() -> int:
    """Sweep terminal asker bindings; questions are address-bound and survive respawn otherwise."""
    changed = 0
    for address, binding in ledger.all_nodes().items():
        if not states.is_terminal(binding.get("state")):
            continue
        if not open_questions_asked_by(binding):
            continue
        result = executor.withdraw_open_questions(address)
        if result.ok:
            changed += 1
    return changed


def marker_senders(marker_path: str | Path, bindings: dict[str, dict]) -> list[str]:
    """Resolve a shared-node marker to exactly one sender binding."""
    marker = Path(marker_path)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    declared = str(payload.get("sender") or "").strip() if isinstance(payload, dict) else ""
    candidates = [
        address
        for address in bindings
        if addressing.node_dir(address, ledger.RUNTIME_ROOT).resolve() == marker.parent.parent.resolve()
    ]
    if declared:
        return [declared] if declared in candidates else []
    exec_candidates = [
        address for address in candidates if addressing.split_address(address)[1] == "exec"
    ]
    if len(exec_candidates) == 1:
        return exec_candidates
    return candidates if len(candidates) == 1 else []
