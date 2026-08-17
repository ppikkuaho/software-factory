"""Shared parser for the canonical per-element trace stanza.

The return-contract boundary and the plan-alignment gate consume the same closed
``{id, serves, kind, level, node}`` record.  Domain checks remain with their
callers; this module owns the one syntax implementation so those boundaries
cannot silently accept different trace languages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


TRACE_FIELDS = {"id", "serves", "kind", "level", "node"}
TRACE_KINDS = {"requirement", "derived", "decision", "design", "test", "adr"}
TRACE_STANZA = re.compile(r"<!--\s*trace:\s*\{(.*?)\}\s*-->", re.DOTALL)
_SERVES_LIST = re.compile(r"serves:\s*\[([^\]]*)\]")


@dataclass(frozen=True)
class TraceElement:
    """One parsed in-package trace element with stable source provenance."""

    id: str
    serves: tuple[str, ...]
    kind: str
    level: str
    node: str
    artifact: Path
    line: int
    fields: frozenset[str]


def parse_stanza(raw: str) -> tuple[Optional[dict], Optional[str]]:
    """Parse the canonical relaxed object used inside ``<!-- trace: ... -->``.

    This deliberately preserves the pre-Q3 return-contract behavior: syntax is
    parsed and the field set is closed here; individual boundaries decide which
    fields/kinds are mandatory for their contract.
    """

    body = raw.strip()
    serves: list[str] = []
    match = _SERVES_LIST.search(body)
    if match:
        serves = [token.strip() for token in match.group(1).split(",") if token.strip()]
        body = body[: match.start()] + "serves: __SERVES__" + body[match.end():]
    fields: dict = {}
    for pair in body.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            return None, f"unparseable field {pair!r}"
        key, value = pair.split(":", 1)
        fields[key.strip()] = value.strip()
    if "serves" in fields:
        fields["serves"] = serves
    extra = set(fields) - TRACE_FIELDS
    if extra:
        return (
            None,
            f"non-canonical field(s) {sorted(extra)} "
            f"(closed set is {sorted(TRACE_FIELDS)})",
        )
    if not fields.get("id"):
        return None, "missing required field 'id'"
    if not fields.get("kind"):
        return None, "missing required field 'kind'"
    return fields, None


def parse_artifact(path: Path) -> tuple[list[TraceElement], list[str]]:
    """Parse every trace stanza in one artifact.

    Errors are source-local descriptions.  Callers add their own typed defect
    class because the return-contract and plan-alignment refusal vocabularies
    intentionally have different stable identities.
    """

    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [f"{path}: unreadable trace artifact: {exc}"]
    records: list[TraceElement] = []
    errors: list[str] = []
    for match in TRACE_STANZA.finditer(text):
        fields, error = parse_stanza(match.group(1))
        line = text.count("\n", 0, match.start()) + 1
        if error:
            errors.append(f"{path}:{line}: {error}")
            continue
        records.append(
            TraceElement(
                id=str(fields["id"]),
                serves=tuple(str(item) for item in (fields.get("serves") or ())),
                kind=str(fields.get("kind") or ""),
                level=str(fields.get("level") or ""),
                node=str(fields.get("node") or ""),
                artifact=Path(path),
                line=line,
                fields=frozenset(fields),
            )
        )
    return records, errors
