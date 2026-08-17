"""PC-owned current-state issue queue commands."""

from __future__ import annotations

import json
from pathlib import Path

from .. import jsonio
from ..errors import HtError, HtUsageError
from ..paths import Root
from ..pipeline import DocWrite, Plan
from ._common import Ctx, today


RIDER = (
    "CURRENT-STATE only; history lives in the decision log; material re-ranks "
    "are logged decisions per PC §5."
)


def _load_source(src: str) -> dict:
    path = Path(src)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HtUsageError(f"cannot read issue-queue source '{path}': {exc}") from exc
    if isinstance(value, list):
        value = {"entries": value}
    if not isinstance(value, dict):
        raise HtUsageError("issue-queue source must be an object or an entries array")
    return value


def _parse_entries(values: list[str], date: str | None) -> dict:
    entries = []
    for value in values:
        parts = value.split(":", 2)
        if len(parts) < 2:
            raise HtUsageError("--entry must be ISSUE_REF:RANK[:TRIAGE_NOTE]")
        issue_ref, rank_text = parts[:2]
        try:
            rank = int(rank_text)
        except ValueError as exc:
            raise HtUsageError("--entry rank must be an integer") from exc
        entry = {"issue_ref": issue_ref, "rank": rank, "date": date or today()}
        if len(parts) == 3 and parts[2]:
            entry["triage_note"] = parts[2]
        entries.append(entry)
    return {"entries": entries}


def set_queue(
    ctx: Ctx,
    *,
    src: str | None,
    entries: list[str],
    date: str | None,
) -> Plan:
    if bool(src) == bool(entries):
        raise HtUsageError("pcq set requires exactly one of --src or --entry")
    new = _load_source(src) if src else _parse_entries(entries, date)
    if isinstance(new.get("entries"), list) and all(
        isinstance(row, dict) and isinstance(row.get("rank"), int)
        for row in new["entries"]
    ):
        new["entries"] = sorted(new["entries"], key=lambda row: row["rank"])
    old = jsonio.load(ctx.root.issue_queue_json) if ctx.root.issue_queue_json.exists() else None

    old_entries = old.get("entries", []) if old else []
    new_entries = new.get("entries", []) if isinstance(new, dict) else []
    old_entries = old_entries if isinstance(old_entries, list) else []
    new_entries = new_entries if isinstance(new_entries, list) else []
    old_by_ref = {
        row.get("issue_ref"): row
        for row in old_entries
        if isinstance(row, dict) and isinstance(row.get("issue_ref"), str)
    }
    new_by_ref = {
        row.get("issue_ref"): row
        for row in new_entries
        if isinstance(row, dict) and isinstance(row.get("issue_ref"), str)
    }
    old_refs = set(old_by_ref)
    new_refs = set(new_by_ref)
    changed = sorted(
        ref
        for ref in old_refs & new_refs
        if old_by_ref[ref] != new_by_ref[ref]
    )
    summary = (
        "ISSUE-QUEUE FULL-DOCUMENT REPLACE: "
        f"old={len(old_entries)} new={len(new_entries)}; "
        f"added={sorted(new_refs - old_refs)}; removed={sorted(old_refs - new_refs)}; "
        f"changed={changed}. {RIDER}"
    )
    def semantic() -> None:
        issue_refs = [row["issue_ref"] for row in new["entries"]]
        ranks = [row["rank"] for row in new["entries"]]
        if len(issue_refs) != len(set(issue_refs)):
            raise HtError("issue queue contains duplicate issue_ref values (PC current-state queue)")
        if len(ranks) != len(set(ranks)):
            raise HtError("issue queue contains duplicate rank values (PC current-state queue)")

    return Plan(
        role=ctx.role,
        message="ht pcq set: replace current issue queue",
        writes=[DocWrite(ctx.root.issue_queue_json, "issue_queue", old, new)],
        warnings=[summary],
        semantic=semantic,
    )


def show(root: Root) -> int:
    if not root.issue_queue_json.exists():
        raise HtError("tier1/issue-queue.json is missing (PC current-state surface)")
    print(jsonio.dumps(jsonio.load(root.issue_queue_json)), end="")
    return 0
