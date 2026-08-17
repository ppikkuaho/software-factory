"""`ht cursor move --tree C --to NODEID --rationale TEXT` (director).

The cursor is a capacity-1 list in v0 (A1 §5): a move SETS the single slot; a
second concurrent slot is rejected.
"""

from __future__ import annotations

from ..errors import HtError
from ..pipeline import DocWrite, Plan
from . import _common
from ._common import Ctx

_ALLOCATION = "a1"  # the single v0 cursor slot


def move(ctx: Ctx, component: str, to: str, rationale: str) -> Plan:
    _common.load_node(ctx, component, to)  # target must exist
    tree = _common.load_tree(ctx, component)

    prev = tree["cursor"][0]["node"] if tree["cursor"] else None
    new_cursor = [{"node": to, "allocation": _ALLOCATION}]
    # DELIBERATE capacity seam, not dead code: A1 §5 makes the cursor a "list-with-
    # capacity-1 — parallelism later = capacity change, no schema change". A single-
    # slot move can never exceed 1 today, but the guard is the one place the v0
    # capacity is enforced; raising capacity later is a change to this constant +
    # allocation logic, and the tree schema stays maxItems-free by that same ruling.
    if len(new_cursor) > 1:
        raise HtError("cursor capacity is 1 in v0 — cannot open a second slot (A1 §5)")

    new_tree = dict(tree)
    new_tree["cursor"] = new_cursor
    new_tree["decision_log"] = list(tree["decision_log"]) + [
        _common.decision_entry(
            "move", frm=prev, to=to, target=to, allocation=_ALLOCATION,
            rationale=rationale, epoch=_common.current_global_epoch(ctx),
        )
    ]

    return Plan(
        role=ctx.role,
        message=f"ht cursor move: {prev or '-'} -> {to} ({component})",
        writes=[DocWrite(ctx.root.tree_json(component), "tree", tree, new_tree)],
        regen_component=component,
    )
