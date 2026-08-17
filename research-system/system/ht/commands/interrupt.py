"""The frozen director-to-PC F11 interrupt."""

from __future__ import annotations

import re

from ..errors import HtUsageError
from ..pipeline import DocWrite, Plan
from ._common import Ctx, today


KIND = "cannot-be-completed-meaningfully-as-posed"


def _next_id(ctx: Ctx) -> str:
    numbers = []
    for path in (ctx.root.tier1_dir / "interrupts").glob("INT-*.json"):
        match = re.fullmatch(r"INT-([0-9]+)\.json", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return f"INT-{max(numbers, default=0) + 1}"


def create(
    ctx: Ctx,
    *,
    raised_by: str,
    issue_ref: str,
    sub_goal_ref: str,
    rationale: str,
    date: str | None,
) -> Plan:
    if any(not value.strip() for value in (raised_by, sub_goal_ref, rationale)):
        raise HtUsageError(
            "interrupt create requires non-empty --raised-by, --sub-goal-ref, and --rationale"
        )
    interrupt_id = _next_id(ctx)
    doc = {
        "id": interrupt_id,
        "raised_by": raised_by,
        "issue_ref": issue_ref,
        "sub_goal_ref": sub_goal_ref,
        "kind": KIND,
        "rationale": rationale,
        "date": date or today(),
    }
    return Plan(
        role=ctx.role,
        message=f"ht interrupt create: {interrupt_id}",
        writes=[DocWrite(ctx.root.interrupt_json(interrupt_id), "interrupt", None, doc)],
    )
