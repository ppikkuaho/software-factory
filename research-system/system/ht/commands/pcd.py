"""Append-only principal-coordinator decision log."""

from __future__ import annotations

import re

from ..errors import HtUsageError
from ..pipeline import DocWrite, Plan
from ._common import Ctx, today


KINDS = (
    "triage",
    "re-rank",
    "activation-proposal",
    "sub-goal",
    "close-settle",
    "merge-schedule",
    "escalation",
    "interrupt-receipt",
)


def next_id(ctx: Ctx) -> str:
    numbers = []
    for path in ctx.root.tier1_dir.glob("**/PCD-*.json"):
        match = re.fullmatch(r"PCD-([0-9]+)\.json", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return f"PCD-{max(numbers, default=0) + 1}"


def append(
    ctx: Ctx,
    *,
    kind: str,
    decision: str,
    refs: list[str],
    primary_ref: str | None,
    date: str | None,
) -> Plan:
    if not decision.strip():
        raise HtUsageError("pcd append requires non-empty --decision/--text")
    if kind == "interrupt-receipt":
        if primary_ref is None or re.fullmatch(r"INT-[0-9]+", primary_ref) is None:
            raise HtUsageError(
                "interrupt-receipt requires --ref INT-<n> (F11 receipt discipline)"
            )
        if not ctx.root.interrupt_json(primary_ref).exists():
            raise HtUsageError(
                f"interrupt-receipt references unknown interrupt '{primary_ref}' "
                "(F11 receipt discipline)"
            )
    decision_id = next_id(ctx)
    context_refs = list(refs)
    if primary_ref is not None and primary_ref not in context_refs:
        context_refs.append(primary_ref)
    doc = {
        "id": decision_id,
        "date": date or today(),
        "kind": kind,
        "decision": decision,
        "context_refs": context_refs,
    }
    if primary_ref is not None:
        doc["ref"] = primary_ref
    return Plan(
        role=ctx.role,
        message=f"ht pcd append: {decision_id} {kind}",
        writes=[DocWrite(ctx.root.pc_decision_json(decision_id), "pc_decision", None, doc)],
    )
