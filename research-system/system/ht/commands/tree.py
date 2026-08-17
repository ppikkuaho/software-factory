"""`ht tree init <component> --root-question TEXT` (director)."""

from __future__ import annotations

from ..errors import HtUsageError
from ..pipeline import DocWrite, Plan
from . import _common
from ._common import Ctx


def init(ctx: Ctx, component: str, root_question: str) -> Plan:
    if ctx.root.tree_json(component).exists():
        raise HtUsageError(f"tree '{component}' already exists")

    tree_doc = {
        "component": component,
        "root_question": root_question,
        "epoch": 0,
        "epoch_history": [],
        "cursor": [],
        "decision_log": [],
        "global_learnings": [],
        "watch_queue": [],
    }
    return Plan(
        role=ctx.role,
        message=f"ht tree init: {component} (epoch 0)",
        writes=[DocWrite(ctx.root.tree_json(component), "tree", None, tree_doc)],
        regen_component=component,
    )
