"""Owner-final fidelity playback for the L1 delivery edge.

L1 writes a preliminary, recipient-visible fidelity judgment.  This module freezes one
content-addressed pointer package for that judgment, records the owner's confirm/reject answer
through the existing human ``answer`` channel, and supplies the deterministic promotion blockers.
It does not deliver bytes: ``harnessd.promote`` remains the only cross-jail writer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Optional

from . import (
    addressing,
    clock,
    executor,
    ledger,
    messages,
    notary,
    states,
    store,
)


SCHEMA_VERSION = 1
QUESTION_PREFIX = "fidelity-playback-"
AUTHORITY_OWNER = "owner"
AUTHORITY_DELEGATE = "operator-delegate"
AUTHORITIES = {AUTHORITY_OWNER, AUTHORITY_DELEGATE}

_PRELIMINARY_VERDICT = re.compile(
    r"^Preliminary\s+Verdict\s*:\s*(accept|reject)\b",
    re.IGNORECASE,
)
_LEGACY_VERDICT = re.compile(r"^Verdict\s*:\s*(accept|reject)\b", re.IGNORECASE)
_OUTCOME_ID = re.compile(r"^O-\d+(?:\.\d+)*$", re.IGNORECASE)
_REQUIREMENT_ID = re.compile(r"^R-\d+(?:\.\d+)*$", re.IGNORECASE)


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _node_dir(node_address: str) -> Path:
    if ledger.RUNTIME_ROOT is None:
        raise ValueError("fidelity playback runtime root is not configured")
    return addressing.node_dir(node_address, ledger.RUNTIME_ROOT)


def find_intent_spec(node_address: str) -> tuple[Optional[Path], Optional[str]]:
    """Locate the one current intent spec using promotion's root/single-project rule."""
    try:
        node_dir = _node_dir(node_address)
    except (OSError, ValueError):
        return None, None
    for rel in ("client-brief/intent-spec.md", "intent-spec.md"):
        candidate = node_dir / rel
        if candidate.is_file():
            return candidate, None
    try:
        nested = sorted(
            path
            for path in node_dir.glob("*/client-brief/intent-spec.md")
            if path.is_file()
        )
    except OSError:
        return None, None
    if len(nested) == 1:
        return nested[0], None
    if len(nested) > 1:
        names = ", ".join(path.parent.parent.name for path in nested)
        return None, (
            f"AMBIGUOUS-INTENT-SPEC: {len(nested)} project subtrees under "
            f"{node_address} each carry client-brief/intent-spec.md ({names})"
        )
    return None, None


def find_judgment(
    node_address: str,
) -> tuple[Optional[Path], Optional[str], bool]:
    """Locate the one current fidelity judgment: (path, ambiguity, source_absent)."""
    try:
        node_dir = _node_dir(node_address)
    except (OSError, ValueError):
        return None, None, True
    if not node_dir.is_dir():
        return None, None, True
    for rel in ("client-brief/fidelity-judgment.md", "fidelity-judgment.md"):
        candidate = node_dir / rel
        if candidate.is_file():
            return candidate, None, False
    try:
        nested = sorted(
            path
            for path in node_dir.glob("*/client-brief/fidelity-judgment.md")
            if path.is_file()
        )
    except OSError:
        return None, None, False
    if len(nested) == 1:
        return nested[0], None, False
    if len(nested) > 1:
        names = ", ".join(path.parent.parent.name for path in nested)
        return None, (
            f"AMBIGUOUS-FIDELITY-JUDGMENT: {len(nested)} project subtrees under "
            f"{node_address} each carry client-brief/fidelity-judgment.md ({names})"
        ), False
    return None, None, False


def extract_preliminary_verdict(text: str) -> Optional[str]:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(("-", "*")):
            line = line[1:].strip()
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        line = line.replace("**", "").strip()
        match = _PRELIMINARY_VERDICT.match(line)
        if match:
            return match.group(1).lower()
    return None


def extract_legacy_verdict(text: str) -> Optional[str]:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(("-", "*")):
            line = line[1:].strip()
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        line = line.replace("**", "").strip()
        match = _LEGACY_VERDICT.match(line)
        if match:
            return match.group(1).lower()
    return None


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            current = stripped.lstrip("#").strip().lower()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(raw)
    return sections


def _section_lines(text: str, name: str) -> list[str]:
    wanted = name.strip().lower()
    for heading, lines in _sections(text).items():
        if heading == wanted or heading.endswith(f" {wanted}"):
            return lines
    return []


def _table(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    rows: list[list[str]] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
            continue
        rows.append(cells)
    if not rows:
        return [], []
    width = len(rows[0])
    return rows[0], [row for row in rows[1:] if len(row) == width]


def _intent_roster(spec_text: str) -> tuple[list[str], list[str], list[str]]:
    defects: list[str] = []
    outcome_header, outcome_rows = _table(_section_lines(spec_text, "outcomes"))
    outcome_ids = [
        row[0].upper()
        for row in outcome_rows
        if row and _OUTCOME_ID.fullmatch(row[0].strip())
    ]
    if not outcome_header or not outcome_ids:
        defects.append(
            "FIDELITY-PLAYBACK-MISSING-OUTCOMES: frozen intent-spec requires an "
            "Outcomes table whose first column carries stable O-* ids"
        )
    if len(outcome_ids) != len(set(outcome_ids)):
        defects.append("FIDELITY-PLAYBACK-DUPLICATE-OUTCOME-ID")

    requirement_header, requirement_rows = _table(
        _section_lines(spec_text, "requirements")
        or _section_lines(spec_text, "hierarchically-ided requirements table")
    )
    mnf_ids: list[str] = []
    if requirement_header:
        normalized = [cell.strip().lower() for cell in requirement_header]
        id_index = next(
            (index for index, cell in enumerate(normalized) if cell in {"id", "requirement id"}),
            0,
        )
        mnf_index = next(
            (index for index, cell in enumerate(normalized) if cell == "mnf"),
            None,
        )
        if mnf_index is not None:
            for row in requirement_rows:
                if (
                    len(row) > max(id_index, mnf_index)
                    and _REQUIREMENT_ID.fullmatch(row[id_index].strip())
                    and row[mnf_index].strip().upper() == "YES"
                ):
                    mnf_ids.append(row[id_index].strip().upper())
    if len(mnf_ids) != len(set(mnf_ids)):
        defects.append("FIDELITY-PLAYBACK-DUPLICATE-MNF-ID")
    return outcome_ids, mnf_ids, defects


def _playback_rows(
    judgment_text: str,
    *,
    section: str,
    expected_ids: list[str],
    node_dir: Path,
) -> tuple[list[dict], list[str]]:
    defects: list[str] = []
    header, raw_rows = _table(_section_lines(judgment_text, section))
    if not expected_ids:
        return [], []
    expected_header = [
        "id",
        "drove",
        "observed",
        "evidence",
        "preliminary result",
    ]
    normalized_header = [
        ("id" if cell.lower() in {"outcome id", "mnf id"} else cell.lower())
        for cell in header
    ]
    if normalized_header != expected_header:
        return [], [
            f"FIDELITY-PLAYBACK-{section.upper().replace(' ', '-')}-TABLE-SCHEMA"
        ]
    indexes = {
        ("id" if cell.lower() in {"outcome id", "mnf id"} else cell.lower()): index
        for index, cell in enumerate(header)
    }
    rows: list[dict] = []
    for raw in raw_rows:
        row = {key: raw[index].strip() for key, index in indexes.items()}
        row["id"] = row["id"].upper()
        rows.append(row)
    actual_ids = [row["id"] for row in rows]
    for identifier in expected_ids:
        count = actual_ids.count(identifier)
        if count == 0:
            defects.append(f"FIDELITY-PLAYBACK-MISSING-EVIDENCE-ROW:{identifier}")
        elif count > 1:
            defects.append(f"FIDELITY-PLAYBACK-DUPLICATE-EVIDENCE-ROW:{identifier}")
    for identifier in actual_ids:
        if identifier not in expected_ids:
            defects.append(f"FIDELITY-PLAYBACK-UNKNOWN-EVIDENCE-ROW:{identifier}")
    for row in rows:
        identifier = row["id"]
        if not row["drove"] or not row["observed"]:
            defects.append(f"FIDELITY-PLAYBACK-INCOMPLETE-OBSERVATION:{identifier}")
        if row["preliminary result"].lower() not in {"accept", "reject"}:
            defects.append(f"FIDELITY-PLAYBACK-BAD-ROW-RESULT:{identifier}")
        raw_pointer = row["evidence"]
        candidate = Path(raw_pointer)
        resolved = candidate if candidate.is_absolute() else node_dir / candidate
        try:
            resolved = resolved.resolve(strict=True)
            resolved.relative_to(node_dir.resolve())
        except (OSError, RuntimeError, ValueError):
            defects.append(
                f"FIDELITY-PLAYBACK-EVIDENCE-POINTER-INVALID:{identifier}:{raw_pointer}"
            )
        else:
            evidence_stamp = notary.stamp(resolved)
            row["evidence_path"] = str(resolved)
            row["evidence_sha256"] = evidence_stamp.get("sha256")
            row["evidence_bytes"] = evidence_stamp.get("bytes")
    return rows, defects


def inspect_artifacts(node_address: str) -> tuple[Optional[dict], list[str]]:
    """Validate current intent/judgment and return the pointer facts used by questions."""
    try:
        if not _node_dir(node_address).is_dir():
            # Preserve promotion's existing absent-source failure classification: the delivery
            # path, not the fidelity gate, journals this as delivery-failed.
            return None, []
    except (OSError, ValueError):
        return None, []
    spec, spec_ambiguity = find_intent_spec(node_address)
    if spec_ambiguity:
        return None, [spec_ambiguity]
    if spec is None:
        return None, [
            "MISSING-FIDELITY-PLAYBACK-INTENT: owner playback requires the frozen intent-spec"
        ]
    judgment, judgment_ambiguity, source_absent = find_judgment(node_address)
    if source_absent:
        return None, []
    if judgment_ambiguity:
        return None, [judgment_ambiguity]
    if judgment is None:
        return None, [
            "MISSING-FIDELITY-JUDGMENT: accept-promote requires "
            "client-brief/fidelity-judgment.md"
        ]
    try:
        spec_text = spec.read_text(encoding="utf-8", errors="replace")
        judgment_text = judgment.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, [f"UNREADABLE-FIDELITY-PLAYBACK-ARTIFACT:{exc}"]

    verdict = extract_preliminary_verdict(judgment_text)
    if verdict is None:
        if extract_legacy_verdict(judgment_text) is not None:
            return None, [
                "FIDELITY-PLAYBACK-REVISION-REQUIRED: Verdict is now preliminary; "
                "write Preliminary Verdict plus per-outcome/per-MNF playback evidence"
            ]
        return None, [
            "MISSING-PRELIMINARY-FIDELITY-VERDICT: fidelity-judgment.md must carry "
            "Preliminary Verdict: accept|reject"
        ]

    outcomes, mnfs, defects = _intent_roster(spec_text)
    node_dir = _node_dir(node_address)
    outcome_rows, outcome_defects = _playback_rows(
        judgment_text,
        section="Outcome Playback",
        expected_ids=outcomes,
        node_dir=node_dir,
    )
    mnf_rows, mnf_defects = _playback_rows(
        judgment_text,
        section="MNF Playback",
        expected_ids=mnfs,
        node_dir=node_dir,
    )
    defects.extend(outcome_defects)
    defects.extend(mnf_defects)
    if defects:
        return None, sorted(dict.fromkeys(defects))
    return {
        "node_address": node_address,
        "intent_spec": str(spec),
        "intent_stamp": notary.stamp(spec),
        "judgment": str(judgment),
        "judgment_stamp": notary.stamp(judgment),
        "preliminary_verdict": verdict,
        "outcome_ids": outcomes,
        "mnf_ids": mnfs,
        "outcome_rows": outcome_rows,
        "mnf_rows": mnf_rows,
    }, []


def _authority_binding(node_address: str) -> dict:
    bindings = ledger.all_nodes()
    current = node_address
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        binding = bindings.get(current) or {}
        if binding.get("fidelity_playback_authority"):
            return binding
        current = str(binding.get("parent_address") or "")
    return bindings.get(node_address) or {}


def authority_for(node_address: str) -> dict:
    binding = _authority_binding(node_address)
    authority = str(
        binding.get("fidelity_playback_authority") or AUTHORITY_OWNER
    )
    return {
        "authority": authority,
        "delegate": binding.get("fidelity_playback_delegate"),
        "delegation_reason": binding.get("fidelity_playback_delegation_reason"),
        "authority_build_id": binding.get("fidelity_playback_authority_build_id"),
    }


def _question_payload(facts: Mapping, authority: Mapping) -> dict:
    identity = {
        "node_address": facts["node_address"],
        "intent_sha256": facts["intent_stamp"]["sha256"],
        "judgment_sha256": facts["judgment_stamp"]["sha256"],
    }
    question_id = f"{QUESTION_PREFIX}{_canonical_sha(identity)[:20]}"
    return {
        "schema_version": SCHEMA_VERSION,
        "question_id": question_id,
        "node_address": facts["node_address"],
        "preliminary_verdict": facts["preliminary_verdict"],
        "intent_spec": facts["intent_spec"],
        "intent_sha256": facts["intent_stamp"]["sha256"],
        "fidelity_judgment": facts["judgment"],
        "fidelity_judgment_sha256": facts["judgment_stamp"]["sha256"],
        "outcome_ids": list(facts["outcome_ids"]),
        "mnf_ids": list(facts["mnf_ids"]),
        "evidence_pointers": [
            {
                "id": row["id"],
                "evidence": row["evidence"],
                "evidence_path": row["evidence_path"],
                "evidence_sha256": row["evidence_sha256"],
                "evidence_bytes": row["evidence_bytes"],
            }
            for row in list(facts["outcome_rows"]) + list(facts["mnf_rows"])
        ],
        "question": (
            "Do you confirm that the preliminary fidelity claims for "
            f"{', '.join(facts['outcome_ids'] + facts['mnf_ids'])} match the frozen intent?"
        ),
        "configured_authority": authority["authority"],
        "configured_delegate": authority["delegate"],
        "delegation_reason": authority["delegation_reason"],
        "authority_build_id": authority["authority_build_id"],
    }


def create_question(node_address: str) -> dict:
    binding = ledger.read_binding(node_address)
    if binding is None:
        return {
            "ok": False,
            "errors": [f"no binding for node {node_address!r}"],
            "binding": None,
        }
    if binding.get("level") != "L1":
        return {
            "ok": False,
            "errors": ["fidelity playback questions are owned by an L1 project binding"],
            "binding": binding,
        }
    facts, defects = inspect_artifacts(node_address)
    if defects or facts is None:
        return {"ok": False, "errors": defects, "binding": binding}
    authority = authority_for(node_address)
    payload = _question_payload(facts, authority)
    question_id = payload["question_id"]
    judgment_path = Path(facts["judgment"])
    question_path = (
        judgment_path.parent
        / "playback"
        / "owner-questions"
        / f"{question_id}.json"
    )
    desired = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        question_path.parent.mkdir(parents=True, exist_ok=True)
        if question_path.exists():
            current = question_path.read_text(encoding="utf-8")
            if current != desired:
                return {
                    "ok": False,
                    "errors": [
                        f"FIDELITY-PLAYBACK-QUESTION-COLLISION:{question_id}"
                    ],
                    "binding": binding,
                }
        else:
            store.atomic_replace(
                question_path,
                lambda handle: handle.write(desired),
            )
        question_stamp = notary.stamp(question_path, read_only=True)
    except OSError as exc:
        return {
            "ok": False,
            "errors": [f"could not freeze fidelity playback question: {exc}"],
            "binding": binding,
        }
    existing = copy.deepcopy(
        binding.get("fidelity_playback_owner_questions") or {}
    )
    current = existing.get(question_id)
    if isinstance(current, dict):
        if (
            current.get("question_sha256") == question_stamp.get("sha256")
            and current.get("fidelity_judgment_sha256")
            == facts["judgment_stamp"]["sha256"]
            and current.get("intent_sha256") == facts["intent_stamp"]["sha256"]
        ):
            return {
                "ok": True,
                "errors": [],
                "binding": binding,
                "question_id": question_id,
                "question_artifact": str(question_path),
                "status": current.get("status"),
            }
        return {
            "ok": False,
            "errors": [f"FIDELITY-PLAYBACK-QUESTION-DRIFTED:{question_id}"],
            "binding": binding,
        }
    row = {
        **payload,
        "question_artifact": str(question_path),
        "question_sha256": question_stamp["sha256"],
        "status": "open",
        "adopted_at": clock.now_utc(),
        "answered_at": None,
        "answer_artifact": None,
        "answer_sha256": None,
        "decision": None,
        "answer_authority": None,
        "answer_actor": None,
    }
    existing[question_id] = row
    result = executor.record_admission(
        node_address,
        expected_owner_token=binding.get("owner_token"),
        delta={
            "fidelity_playback_owner_questions": existing,
            "fidelity_playback_current_question_id": question_id,
        },
        event="fidelity_playback_question_posted",
        summary=(
            f"L1 posted preliminary fidelity playback {question_id} for owner confirmation"
        ),
    )
    return {
        "ok": bool(result.ok),
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "binding": result.binding,
        "question_id": question_id,
        "question_artifact": str(question_path),
        "status": "open",
    }


def _question_current(question: Mapping) -> list[str]:
    defects: list[str] = []
    for label, path_key, hash_key in (
        ("INTENT", "intent_spec", "intent_sha256"),
        ("JUDGMENT", "fidelity_judgment", "fidelity_judgment_sha256"),
        ("QUESTION", "question_artifact", "question_sha256"),
    ):
        path = Path(str(question.get(path_key) or ""))
        expected = str(question.get(hash_key) or "")
        if not path.is_file() or not expected:
            defects.append(f"FIDELITY-PLAYBACK-{label}-MISSING")
            continue
        if notary.stamp(path).get("sha256") != expected:
            defects.append(f"FIDELITY-PLAYBACK-{label}-DRIFTED")
    for row in question.get("evidence_pointers") or []:
        if not isinstance(row, Mapping):
            defects.append("FIDELITY-PLAYBACK-EVIDENCE-STAMP-MALFORMED")
            continue
        identifier = str(row.get("id") or "unknown")
        path = Path(str(row.get("evidence_path") or ""))
        expected = str(row.get("evidence_sha256") or "")
        if (
            not path.is_file()
            or not expected
            or notary.stamp(path).get("sha256") != expected
        ):
            defects.append(
                f"FIDELITY-PLAYBACK-EVIDENCE-DRIFTED:{identifier}"
            )
    return defects


def _write_answer(
    question: Mapping,
    *,
    decision: str,
    note: str,
    authority: str,
    actor: Optional[str],
) -> tuple[Path, dict]:
    question_path = Path(str(question["question_artifact"]))
    answer_dir = question_path.parent.parent / "owner-answers"
    answer_path = answer_dir / f"{question['question_id']}.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "question_id": question["question_id"],
        "intent_sha256": question["intent_sha256"],
        "fidelity_judgment_sha256": question["fidelity_judgment_sha256"],
        "decision": decision,
        "note": note,
        "answer_authority": authority,
        "answer_actor": actor,
        "delegation_reason": (
            question.get("delegation_reason")
            if authority == AUTHORITY_DELEGATE
            else None
        ),
        "answered_at": clock.now_utc(),
    }
    answer_dir.mkdir(parents=True, exist_ok=True)
    store.atomic_replace(
        answer_path,
        lambda handle: (
            handle.write(json.dumps(payload, indent=2, sort_keys=True)),
            handle.write("\n"),
        ),
    )
    stamp = notary.stamp(answer_path, read_only=True)
    payload["answer_sha256"] = stamp["sha256"]
    return answer_path, payload


def _wake_l1(
    node_address: str,
    *,
    question_id: str,
    decision: str,
    answer_path: Path,
    authority: str,
) -> Optional[str]:
    inbox = addressing.inbox_path(node_address, ledger.RUNTIME_ROOT)
    line = {
        "from": "human",
        "type": (
            "fidelity_playback_owner_question_answered"
            if authority == AUTHORITY_OWNER
            else "fidelity_playback_commissioning_delegate_question_answered"
        ),
        "question_id": question_id,
        "decision": decision,
        "answer_authority": authority,
        "answer_artifact": str(answer_path),
        "message": (
            f"Fidelity playback {question_id} was {decision}ed by {authority}; "
            "resume the owner-final playback flow."
        ),
        "ts": clock.now_utc(),
    }
    try:
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError as exc:
        return str(exc)
    return None


def _route_repair(
    node_address: str,
    question: Mapping,
    *,
    note: str,
    answer_path: Path,
    authority: str,
) -> Optional[str]:
    children = [
        binding
        for binding in ledger.all_nodes().values()
        if binding.get("parent_address") == node_address
        and binding.get("level") == "L2"
        and not states.is_terminal(binding.get("state"))
    ]
    if len(children) != 1:
        return (
            "FIDELITY-PLAYBACK-REPAIR-TARGET-AMBIGUOUS:"
            f"{node_address}:expected-one-live-direct-L2:found-{len(children)}"
        )
    target = str(children[0]["node_address"])
    digest = hashlib.sha256(
        f"{question['question_id']}\0{note}".encode("utf-8")
    ).hexdigest()[:20]
    message_id = f"fidelity-repair-{digest}"
    directory = addressing.messages_dir(node_address, ledger.RUNTIME_ROOT)
    artifact = directory / f"{message_id}.md"
    marker = directory / f"{message_id}.json"
    content = (
        "# Fidelity playback repair\n\n"
        f"Question: `{question['question_id']}`\n\n"
        f"Playback answer: `{answer_path}`\n\n"
        f"Answer authority: `{authority}`\n\n"
        "Reason (verbatim):\n\n"
        f"{note}\n"
    )
    payload = {
        "type": "message",
        "sender": node_address,
        "message_id": message_id,
        "to": target,
        "artifact": f"messages/{message_id}.md",
        "summary": f"{authority} rejected final fidelity playback; repair and return.",
        "needs_answer": False,
        "tags": ["fidelity-playback-repair"],
        "metadata": {
            "kind": "fidelity_playback_repair",
            "question_id": question["question_id"],
            "answer_artifact": str(answer_path),
        },
        "answers_question": None,
    }
    desired_marker = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if artifact.exists() and artifact.read_text(encoding="utf-8") != content:
            return f"FIDELITY-PLAYBACK-REPAIR-MESSAGE-COLLISION:{message_id}"
        if marker.exists() and marker.read_text(encoding="utf-8") != desired_marker:
            return f"FIDELITY-PLAYBACK-REPAIR-MESSAGE-COLLISION:{message_id}"
        if not artifact.exists():
            artifact.write_text(content, encoding="utf-8")
        if not marker.exists():
            marker.write_text(desired_marker, encoding="utf-8")
        result = messages.submit_marker(
            node_address,
            marker,
            runtime_root=ledger.RUNTIME_ROOT,
        )
    except (OSError, messages.MessageError) as exc:
        return f"FIDELITY-PLAYBACK-REPAIR-MESSAGE-FAILED:{exc}"
    if not getattr(result, "ok", False):
        return "FIDELITY-PLAYBACK-REPAIR-MESSAGE-FAILED"
    return None


def answer_question(
    node_address: str,
    *,
    question_id: str,
    decision: str,
    note: str,
    answer_authority: Optional[str] = None,
    answer_actor: Optional[str] = None,
) -> dict:
    binding = ledger.read_binding(node_address)
    if binding is None:
        return {"ok": False, "errors": [f"no binding for node {node_address!r}"]}
    questions = copy.deepcopy(
        binding.get("fidelity_playback_owner_questions") or {}
    )
    question = questions.get(question_id)
    if not isinstance(question, dict):
        return {
            "ok": False,
            "errors": [f"unknown fidelity playback owner question {question_id!r}"],
            "binding": binding,
        }
    decision = str(decision or "").strip().lower()
    if decision not in {"confirm", "reject"}:
        return {
            "ok": False,
            "errors": ["fidelity playback answer requires decision confirm|reject"],
            "binding": binding,
        }
    note = str(note or "")
    if decision == "reject" and not note.strip():
        return {
            "ok": False,
            "errors": ["FIDELITY-PLAYBACK-REJECT-REQUIRES-REASON"],
            "binding": binding,
        }
    authority = str(answer_authority or AUTHORITY_OWNER).strip().lower()
    if authority not in AUTHORITIES:
        return {
            "ok": False,
            "errors": [f"unknown fidelity playback answer authority {authority!r}"],
            "binding": binding,
        }
    if authority == AUTHORITY_DELEGATE:
        if question.get("configured_authority") != AUTHORITY_DELEGATE:
            return {
                "ok": False,
                "errors": ["FIDELITY-PLAYBACK-DELEGATE-NOT-PREAUTHORIZED"],
                "binding": binding,
            }
        expected_actor = str(question.get("configured_delegate") or "")
        if not answer_actor or str(answer_actor) != expected_actor:
            return {
                "ok": False,
                "errors": [
                    "FIDELITY-PLAYBACK-DELEGATE-ACTOR-MISMATCH:"
                    f"expected-{expected_actor or 'absent'}"
                ],
                "binding": binding,
            }
    defects = _question_current(question)
    if defects:
        return {"ok": False, "errors": defects, "binding": binding}
    if question.get("status") in {"confirmed", "rejected"}:
        if (
            question.get("decision") == decision
            and question.get("answer_authority") == authority
            and question.get("answer_actor") == answer_actor
        ):
            answer_path = Path(str(question.get("answer_artifact") or ""))
            try:
                prior_answer = json.loads(answer_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {
                    "ok": False,
                    "errors": [
                        f"FIDELITY-PLAYBACK-ANSWER-UNREADABLE:{question_id}"
                    ],
                    "binding": binding,
                }
            if str(prior_answer.get("note") or "") != note:
                return {
                    "ok": False,
                    "errors": [
                        f"fidelity playback owner question {question_id!r} is already "
                        "answered with different immutable content"
                    ],
                    "binding": binding,
                }
            route_error = None
            if decision == "reject":
                route_error = _route_repair(
                    node_address,
                    question,
                    note=note,
                    answer_path=answer_path,
                    authority=authority,
                )
            return {
                "ok": route_error is None,
                "errors": [route_error] if route_error else [],
                "binding": binding,
                "question_id": question_id,
                "decision": decision,
                "answer_artifact": question.get("answer_artifact"),
                "answer_authority": authority,
                "answer_actor": answer_actor,
                "wake_target": node_address,
            }
        return {
            "ok": False,
            "errors": [
                f"fidelity playback owner question {question_id!r} is already "
                f"{question.get('status')}"
            ],
            "binding": binding,
        }
    try:
        answer_path, answer = _write_answer(
            question,
            decision=decision,
            note=note,
            authority=authority,
            actor=answer_actor,
        )
    except OSError as exc:
        return {
            "ok": False,
            "errors": [f"could not write fidelity playback answer: {exc}"],
            "binding": binding,
        }
    question.update(
        {
            "status": "confirmed" if decision == "confirm" else "rejected",
            "decision": decision,
            "answered_at": answer["answered_at"],
            "answer_artifact": str(answer_path),
            "answer_sha256": answer["answer_sha256"],
            "answer_authority": authority,
            "answer_actor": answer_actor,
        }
    )
    questions[question_id] = question
    result = executor.record_admission(
        node_address,
        expected_owner_token=None,
        delta={
            "fidelity_playback_owner_questions": questions,
            "fidelity_playback_last_answer_authority": authority,
            "fidelity_playback_last_answer_actor": answer_actor,
        },
        event=(
            "fidelity_playback_owner_answered"
            if authority == AUTHORITY_OWNER
            else "fidelity_playback_commissioning_delegate_answered"
        ),
        summary=(
            f"{authority} {decision}ed fidelity playback {question_id}"
        ),
    )
    if not result.ok:
        return {
            "ok": False,
            "errors": list(result.errors),
            "binding": result.binding,
        }
    wake_error = _wake_l1(
        node_address,
        question_id=question_id,
        decision=decision,
        answer_path=answer_path,
        authority=authority,
    )
    route_error = None
    if decision == "reject":
        route_error = _route_repair(
            node_address,
            question,
            note=note,
            answer_path=answer_path,
            authority=authority,
        )
    errors = [error for error in (wake_error, route_error) if error]
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": [],
        "binding": ledger.read_binding(node_address),
        "question_id": question_id,
        "decision": decision,
        "answer_artifact": str(answer_path),
        "answer_authority": authority,
        "answer_actor": answer_actor,
        "wake_target": node_address,
    }


def promotion_authorization(node_address: str) -> tuple[Optional[dict], list[str]]:
    facts, defects = inspect_artifacts(node_address)
    if defects or facts is None:
        return None, defects
    if facts["preliminary_verdict"] != "accept":
        return None, [
            "FIDELITY-JUDGMENT-NOT-ACCEPT: preliminary fidelity verdict must accept "
            "before promotion"
        ]
    payload = _question_payload(facts, authority_for(node_address))
    question_id = payload["question_id"]
    binding = ledger.read_binding(node_address) or {}
    question = (
        binding.get("fidelity_playback_owner_questions") or {}
    ).get(question_id)
    if not isinstance(question, dict):
        return None, [
            "OWNER-CONFIRMED-FIDELITY-PLAYBACK-REQUIRED:"
            f"missing-current-question:{question_id}"
        ]
    drift = _question_current(question)
    if drift:
        return None, drift
    if question.get("status") == "rejected":
        return None, [
            "OWNER-CONFIRMED-FIDELITY-PLAYBACK-REJECTED:"
            f"{question_id}"
        ]
    if question.get("status") != "confirmed" or question.get("decision") != "confirm":
        return None, [
            "OWNER-CONFIRMED-FIDELITY-PLAYBACK-REQUIRED:"
            f"unanswered:{question_id}"
        ]
    answer_path = Path(str(question.get("answer_artifact") or ""))
    if (
        not answer_path.is_file()
        or notary.stamp(answer_path).get("sha256")
        != question.get("answer_sha256")
    ):
        return None, [
            "OWNER-CONFIRMED-FIDELITY-PLAYBACK-ANSWER-DRIFTED:"
            f"{question_id}"
        ]
    authority = question.get("answer_authority")
    if authority == AUTHORITY_OWNER:
        label = "OWNER-CONFIRMED-FIDELITY-PLAYBACK"
    elif (
        authority == AUTHORITY_DELEGATE
        and question.get("configured_authority") == AUTHORITY_DELEGATE
        and question.get("answer_actor") == question.get("configured_delegate")
    ):
        label = "COMMISSIONING-DELEGATE-CONFIRMED-FIDELITY-PLAYBACK"
    else:
        return None, [
            "OWNER-CONFIRMED-FIDELITY-PLAYBACK-WRONG-AUTHORITY:"
            f"{question_id}:{authority}"
        ]
    return {
        "label": label,
        "question_id": question_id,
        "answer_artifact": str(answer_path),
        "answer_authority": authority,
        "answer_actor": question.get("answer_actor"),
    }, []


def promotion_blockers(node_address: str) -> list[str]:
    _authorization, defects = promotion_authorization(node_address)
    return defects
