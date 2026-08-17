"""`ht settle --node ID --resolution closed|revived|demoted` (director act).

A1 §1 trigger/write split: the director settles a parked node here; for a
demotion the LEDGER write is NOT executed here — the verifier later runs
`ht ledger create --from-settlement`, which fills demoted_to (director queues,
owning author executes).

  closed  -> settlement closed,  status -> closed
  revived -> settlement revived, status -> worked, git_binding.fork += revived
  demoted -> settlement demoted (demoted_to left null), status -> closed
"""

from __future__ import annotations

from .. import transitions
from ..errors import HtError
from ..pipeline import DocWrite, Plan
from . import _common
from ._common import Ctx


def settle(ctx: Ctx, node_id: str, resolution: str, rationale: str | None, tree_opt: str | None) -> Plan:
    if resolution not in ("closed", "revived", "demoted"):
        raise HtError(f"unknown resolution '{resolution}' (closed|revived|demoted) (A1 §2.1)")
    component = _common.resolve_tree_for_node(ctx, node_id, tree_opt)
    old = _common.load_node(ctx, component, node_id)
    if old["status"] != "parked":
        raise HtError(
            f"cannot settle node {node_id}: status '{old['status']}', requires 'parked' (A1 §2.1)"
        )
    conflicts = old.get("conflicts", [])
    pending_idx = None
    for i in range(len(conflicts) - 1, -1, -1):
        if conflicts[i].get("settlement") == "pending":
            pending_idx = i
            break
    if pending_idx is None:
        raise HtError(f"node {node_id} has no pending conflict to settle (A1 §2.1)")

    tree = _common.load_tree(ctx, component)
    global_epoch = _common.current_global_epoch(ctx)
    new = dict(old)
    new_conflicts = [dict(c) for c in conflicts]

    if resolution == "closed":
        new_conflicts[pending_idx]["settlement"] = "closed"
        new_status = "closed"
    elif resolution == "demoted":
        new_conflicts[pending_idx]["settlement"] = "demoted"
        new_conflicts[pending_idx]["demoted_to"] = None  # filled later by verifier
        new_status = "closed"
    else:  # revived
        new_conflicts[pending_idx]["settlement"] = "revived"
        new_status = "worked"
        gb = dict(old["git_binding"]) if old.get("git_binding") else {"branch": f"ht/{node_id}", "fork": []}
        gb["fork"] = list(gb.get("fork", [])) + [{"epoch": global_epoch, "reason": "revived"}]
        new["git_binding"] = gb

    new["conflicts"] = new_conflicts
    new["status"] = new_status

    new_tree = dict(tree)
    new_tree["decision_log"] = list(tree["decision_log"]) + [
        _common.decision_entry(
            "settle", frm="parked", to=new_status, target=node_id,
            rationale=rationale or f"settle:{resolution}", epoch=global_epoch,
        )
    ]

    def semantic() -> None:
        transitions.check_transition("parked", new_status)

    return Plan(
        role=ctx.role,
        message=f"ht settle: {node_id} ({resolution})",
        writes=[
            DocWrite(ctx.root.node_json(component, node_id), "node", old, new),
            DocWrite(ctx.root.tree_json(component), "tree", tree, new_tree),
        ],
        regen_component=component,
        semantic=semantic,
    )
