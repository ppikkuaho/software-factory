"""Ratification queue creation, annotation, user disposition, and listing."""

from __future__ import annotations

import re

from .. import jsonio
from ..errors import HtUsageError
from ..paths import Root
from ..pipeline import DocWrite, Plan
from ._common import Ctx, today


KINDS = (
    "activation-request",
    "cgate-escalation",
    "tier3-ratification",
    "improvement-note",
)


def _paths(root: Root):
    return sorted((root.tier1_dir / "ratification-queue").glob("RQ-*.json"))


def _load(ctx: Ctx, item_id: str) -> dict:
    if re.fullmatch(r"RQ-[0-9]+", item_id) is None:
        raise HtUsageError("ratification item id must have the form RQ-<n>")
    path = ctx.root.ratification_item_json(item_id)
    if not path.exists():
        raise HtUsageError(f"no such ratification item '{item_id}'")
    return jsonio.load(path)


def _next_id(root: Root) -> str:
    numbers = []
    for path in _paths(root):
        match = re.fullmatch(r"RQ-([0-9]+)\.json", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return f"RQ-{max(numbers, default=0) + 1}"


def append(ctx: Ctx, *, kind: str, payload_ref: str, text: str, date: str | None) -> Plan:
    if not payload_ref.strip() or not text.strip():
        raise HtUsageError("rq append requires non-empty --payload-ref and --text")
    item_id = _next_id(ctx.root)
    doc = {
        "id": item_id,
        "kind": kind,
        "payload_ref": payload_ref,
        "text": text,
        "queued_by": ctx.role,
        "date": date or today(),
        "disposition": None,
        "annotations": [],
    }
    return Plan(
        role=ctx.role,
        message=f"ht rq append: {item_id} {kind}",
        writes=[DocWrite(ctx.root.ratification_item_json(item_id), "ratification_item", None, doc)],
    )


def annotate(ctx: Ctx, *, item_id: str, note: str, date: str | None) -> Plan:
    if not note.strip():
        raise HtUsageError("rq annotate requires non-empty --note")
    old = _load(ctx, item_id)
    new = dict(old)
    new["annotations"] = list(old.get("annotations", [])) + [
        {"date": date or today(), "note": note}
    ]
    return Plan(
        role=ctx.role,
        message=f"ht rq annotate: {item_id}",
        writes=[DocWrite(ctx.root.ratification_item_json(item_id), "ratification_item", old, new)],
    )


def dispose(
    ctx: Ctx,
    *,
    item_id: str,
    status: str,
    by: str,
    note: str | None,
    date: str | None,
) -> Plan:
    if not by.strip():
        raise HtUsageError("rq dispose requires non-empty --by")
    old = _load(ctx, item_id)
    disposition = {"status": status, "by": by, "date": date or today()}
    if note is not None:
        if not note.strip():
            raise HtUsageError("--note must be non-empty when supplied")
        disposition["note"] = note
    new = dict(old)
    new["disposition"] = disposition
    return Plan(
        role=ctx.role,
        message=f"ht rq dispose: {item_id} {status}",
        writes=[DocWrite(ctx.root.ratification_item_json(item_id), "ratification_item", old, new)],
    )


def list_items(root: Root) -> int:
    items = [jsonio.load(path) for path in _paths(root)]
    items.sort(key=lambda item: (item.get("disposition") is not None, item.get("id", "")))
    print(jsonio.dumps(items), end="")
    return 0
