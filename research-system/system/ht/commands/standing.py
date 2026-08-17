"""`ht standing set --node ID --standing S --note TEXT` (verifier).

Standing is verifier judgment with mandatory rationale citing the driving
claims/children (B4 §5) — the note is required unless standing=untested.
"""

from __future__ import annotations

from ..errors import HtError
from ..pipeline import DocWrite, Plan
from . import _common
from ._common import Ctx

_STANDINGS = {"untested", "supported", "weakened", "refuted", "contested"}


def set_standing(ctx: Ctx, node_id: str, standing: str, note: str | None, tree_opt: str | None) -> Plan:
    if standing not in _STANDINGS:
        raise HtError(f"unknown standing '{standing}' (valid: {sorted(_STANDINGS)}) (A1 §2 DP-3)")
    component = _common.resolve_tree_for_node(ctx, node_id, tree_opt)
    old = _common.load_node(ctx, component, node_id)

    new = dict(old)
    new["standing"] = standing
    new["standing_note"] = note

    def semantic() -> None:
        if standing != "untested" and not note:
            raise HtError(
                f"standing '{standing}' requires a --note (mandatory rationale, B4 §5)"
            )

    return Plan(
        role=ctx.role,
        message=f"ht standing set: {node_id} -> {standing}",
        writes=[DocWrite(ctx.root.node_json(component, node_id), "node", old, new)],
        regen_component=component,
        semantic=semantic,
    )
