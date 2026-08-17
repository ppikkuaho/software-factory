"""Dispatch commands: `ht dispatch create` (director), `ht dispatch outcome` (harness).

On a node's FIRST dispatch the harness writes git_binding — a synthesized branch
name + a fork history entry (J3: records only, no real subject-repo git ops).
Metering is passive and never enforced.
"""

from __future__ import annotations

import re

from ..errors import HtError, HtUsageError
from ..pipeline import DocWrite, Plan
from . import _common
from ._common import Ctx

# Director seam ruling R2 (checkpoint-1 verdict 2026-07-07, recorded in
# RESEARCH-SYSTEM-HANDOFF-2026-07-07.md; A1 amendment pending): a dispatch may only
# be created against a node that is unexplored or worked. closed/merged are terminal
# and parked exits only via settle — none may be dispatched. Mint stays unconstrained
# (children under closed parents remain legal).
_DISPATCHABLE = ("unexplored", "worked")


def create(
    ctx: Ctx,
    node_id: str,
    question: str,
    done_definition: str,
    plan_ref: str | None,
    tree_opt: str | None,
    issue_ref: str | None = None,
) -> Plan:
    component = _common.resolve_tree_for_node(ctx, node_id, tree_opt)
    node = _common.load_node(ctx, component, node_id)
    if issue_ref is not None:
        if re.fullmatch(r"I-[0-9]+", issue_ref) is None:
            raise HtUsageError(
                "--issue-ref must have the form I-<n> (coherence amendments §4)"
            )
        if not ctx.root.issue_json(issue_ref).exists():
            raise HtUsageError(f"no such issue '{issue_ref}'")
    if node["status"] not in _DISPATCHABLE:
        raise HtError(
            f"cannot dispatch node {node_id}: status '{node['status']}' — dispatch "
            f"requires status unexplored|worked (parked exits only via settle; "
            f"closed/merged are terminal) (director seam ruling R2)"
        )
    tree = _common.load_tree(ctx, component)
    dispatch_id, _ordinal = _common.next_dispatch_id(ctx, component, node_id)

    global_epoch = _common.current_global_epoch(ctx)
    dispatch_doc = {
        "id": dispatch_id,
        "node": node_id,
        "issue_ref": issue_ref,
        "question": question,
        "done_definition": done_definition,
        "plan_ref": plan_ref,
        "steers": [],
        "interrupt": None,
        "outcome": None,
        "metering": None,
        "epoch": global_epoch,
        "role_packet": None,
        "adjudications": [],
        "report_ref": None,
        "archive_ref": None,
        "report_hash": None,
    }

    writes = [
        DocWrite(
            ctx.root.dispatch_json(component, node_id, dispatch_id),
            "dispatch", None, dispatch_doc,
        )
    ]

    # git_binding created at FIRST dispatch (A1 §2 v2; J3 records only)
    if node.get("git_binding") is None:
        new_node = dict(node)
        slug = _common.slugify(node["premise"])
        new_node["git_binding"] = {
            "branch": f"ht/{node_id}-{slug}",
            "fork": [{"epoch": global_epoch, "reason": "original"}],
        }
        writes.append(
            DocWrite(ctx.root.node_json(component, node_id), "node", node, new_node)
        )

    new_tree = dict(tree)
    new_tree["decision_log"] = list(tree["decision_log"]) + [
        _common.decision_entry(
            "dispatch", target=node_id, rationale=question,
            refs=[dispatch_id], epoch=global_epoch,
        )
    ]
    writes.append(DocWrite(ctx.root.tree_json(component), "tree", tree, new_tree))

    return Plan(
        role=ctx.role,
        message=f"ht dispatch create: {dispatch_id}",
        writes=writes,
        regen_component=component,
    )


def outcome(
    ctx: Ctx,
    dispatch_id: str,
    outcome_value: str,
    tokens: int | None,
    wall_clock: float | None,
    tree_opt: str | None,
) -> Plan:
    component, node_id = _common.resolve_dispatch(ctx, dispatch_id, tree_opt)
    path = ctx.root.dispatch_json(component, node_id, dispatch_id)
    old = _common.jsonio.load(path)
    new = dict(old)
    new["outcome"] = outcome_value
    new["metering"] = {"tokens": tokens, "wall_clock": wall_clock}
    return Plan(
        role=ctx.role,
        message=f"ht dispatch outcome: {dispatch_id} -> {outcome_value}",
        writes=[DocWrite(path, "dispatch", old, new)],
    )
