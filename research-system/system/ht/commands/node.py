"""Node lifecycle commands (director): mint, park, close, merge.

Status-transition legality is A1 §2.1; every command names the transition it is
allowed to make. `merge` composes the retained mechanical subset with item 1's
merge-record, global-epoch, and federated-staleness gates; item 7 owns the full
merge-screen semantics. Coherence amendments §10 additionally require every
non-trunk backing claim to carry a verifier re-validation record.
"""

from __future__ import annotations

import re

from .. import transitions
from ..errors import HtError, HtUsageError
from ..pipeline import DocWrite, Plan
from ..references import resolve_ref
from . import _common, mrec
from ._common import Ctx


def mint(
    ctx: Ctx,
    component: str,
    parent: str | None,
    is_root: bool,
    premise: str,
    minted_from: str | None,
    supersedes: str | None,
    rationale: str,
    from_issue: str | None = None,
) -> Plan:
    _common.load_tree(ctx, component)  # existence
    if parent is not None:
        _common.load_node(ctx, component, parent)  # parent must exist
    if minted_from is not None and from_issue is not None:
        raise HtUsageError("--minted-from and --from-issue are mutually exclusive")
    if from_issue is not None:
        if re.fullmatch(r"I-[0-9]+", from_issue) is None:
            raise HtUsageError(
                "--from-issue must have the form I-<n> (coherence amendments §4)"
            )
        if not ctx.root.issue_json(from_issue).exists():
            raise HtUsageError(f"no such issue '{from_issue}'")
    effective_minted_from = (
        f"issue#{from_issue}" if from_issue is not None else minted_from or "user-direct"
    )
    node_id = _common.next_node_id(ctx, component, parent)

    node_doc = {
        "id": node_id,
        "parent": parent,
        "premise": premise,
        "minted_from": effective_minted_from,
        "supersedes": supersedes,
        "superseded_by_node": None,
        "status": "unexplored",
        "status_reason": None,
        "conflicts": [],
        "git_binding": None,
        "standing": "untested",
        "standing_note": None,
        "claims": [],
        "measurements": [],
        "learnings_out": [],
    }

    tree = _common.load_tree(ctx, component)
    global_epoch = _common.current_global_epoch(ctx)
    tree["decision_log"].append(
        _common.decision_entry(
            "mint", target=node_id, rationale=rationale, epoch=global_epoch,
            refs=[effective_minted_from] if effective_minted_from != "user-direct" else [],
        )
    )

    writes = [
        DocWrite(ctx.root.node_json(component, node_id), "node", None, node_doc),
        DocWrite(ctx.root.tree_json(component), "tree", _common.load_tree(ctx, component), tree),
    ]

    if supersedes is not None:
        old = _common.load_node(ctx, component, supersedes)
        new_old = dict(old)
        new_old["superseded_by_node"] = node_id
        writes.append(
            DocWrite(ctx.root.node_json(component, supersedes), "node", old, new_old)
        )

    return Plan(
        role=ctx.role,
        message=f"ht node mint: {node_id} in {component}",
        writes=writes,
        regen_component=component,
    )


def park(ctx: Ctx, node_id: str, rationale: str, superseded_by: str | None, tree_opt: str | None) -> Plan:
    component = _common.resolve_tree_for_node(ctx, node_id, tree_opt)
    old = _common.load_node(ctx, component, node_id)
    if old["status"] != "worked":
        raise HtError(
            f"cannot park node {node_id}: status is '{old['status']}', requires 'worked' "
            f"(A1 §2.1)"
        )
    tree = _common.load_tree(ctx, component)
    global_epoch = _common.current_global_epoch(ctx)

    new = dict(old)
    new["conflicts"] = list(old.get("conflicts", [])) + [
        {
            "parked_at_epoch": global_epoch,
            "superseded_by": superseded_by,
            "settlement": "pending",
            "demoted_to": None,
        }
    ]
    new["status"] = "parked"

    new_tree = dict(tree)
    new_tree["cursor"] = [c for c in tree["cursor"] if c["node"] != node_id]
    new_tree["decision_log"] = list(tree["decision_log"]) + [
        _common.decision_entry(
            "park", frm="worked", to="parked", target=node_id,
            rationale=rationale, epoch=global_epoch,
        )
    ]

    def semantic() -> None:
        transitions.check_transition("worked", "parked")

    return Plan(
        role=ctx.role,
        message=f"ht node park: {node_id}",
        writes=[
            DocWrite(ctx.root.node_json(component, node_id), "node", old, new),
            DocWrite(ctx.root.tree_json(component), "tree", tree, new_tree),
        ],
        regen_component=component,
        semantic=semantic,
    )


def close(ctx: Ctx, node_id: str, reason: str, refs: list[str], tree_opt: str | None) -> Plan:
    component = _common.resolve_tree_for_node(ctx, node_id, tree_opt)
    old = _common.load_node(ctx, component, node_id)
    if old["status"] not in ("unexplored", "worked"):
        raise HtError(
            f"cannot close node {node_id}: status '{old['status']}' "
            f"(legal from unexplored|worked only; parked nodes exit via settle) (A1 §2.1)"
        )
    tree = _common.load_tree(ctx, component)
    global_epoch = _common.current_global_epoch(ctx)

    new = dict(old)
    new["status"] = "closed"
    new["status_reason"] = reason

    new_tree = dict(tree)
    new_tree["cursor"] = [c for c in tree["cursor"] if c["node"] != node_id]
    new_tree["decision_log"] = list(tree["decision_log"]) + [
        _common.decision_entry(
            "close", frm=old["status"], to="closed", target=node_id,
            rationale=reason, refs=refs, epoch=global_epoch,
        )
    ]

    def semantic() -> None:
        transitions.check_transition(old["status"], "closed")

    return Plan(
        role=ctx.role,
        message=f"ht node close: {node_id}",
        writes=[
            DocWrite(ctx.root.node_json(component, node_id), "node", old, new),
            DocWrite(ctx.root.tree_json(component), "tree", tree, new_tree),
        ],
        regen_component=component,
        semantic=semantic,
    )


def _next_watch_id(tree: dict) -> str:
    """Mint the next tree-local watch id without reusing sparse ordinals."""
    ordinals = []
    for row in tree.get("watch_queue", []):
        match = re.fullmatch(r"W-([0-9]+)", str(row.get("id", "")))
        if match is not None:
            ordinals.append(int(match.group(1)))
    return f"W-{max(ordinals, default=0) + 1}"


def merge(
    ctx: Ctx,
    node_id: str,
    tree_opt: str | None,
    merge_record_id: str,
) -> Plan:
    component = _common.resolve_tree_for_node(ctx, node_id, tree_opt)
    old = _common.load_node(ctx, component, node_id)
    tree = _common.load_tree(ctx, component)
    if (
        not isinstance(merge_record_id, str)
        or re.fullmatch(r"MR-\d+", merge_record_id) is None
    ):
        raise HtUsageError(
            "merge-record id must have the form MR-<n>; path-like ids are forbidden "
            "(item 1 W6/W7)"
        )
    resolved_record = resolve_ref(
        ctx.root,
        f"merge-record#{merge_record_id}",
        expected={"merge-record"},
    )
    if (
        resolved_record.object_id != merge_record_id
        or resolved_record.path != ctx.root.merge_record_json(merge_record_id)
    ):
        raise HtError(
            f"merge record {merge_record_id!r} did not resolve to its canonical path"
        )
    merge_record = resolved_record.document
    canonical_node_ref = f"tree#{component}/node#{node_id}"

    # This read and the resulting writes execute under the caller-held global
    # mutex.  Global epoch v1 is a single counter allocated as max(all trees)+1;
    # only the merging tree advances, leaving every other tree's last-merge
    # epoch as its stale-baseline (macro §6; item 1 Q7).
    new_epoch = _common.current_global_epoch(ctx) + 1

    def semantic() -> None:
        current_record = resolve_ref(
            ctx.root,
            resolved_record.canonical,
            expected={"merge-record"},
        )
        if (
            current_record.path != resolved_record.path
            or current_record.document != merge_record
        ):
            raise HtError(
                f"merge record {resolved_record.canonical} changed before merge commit"
            )
        if merge_record.get("candidate_ref") != canonical_node_ref:
            raise HtError(
                f"cannot merge node {node_id}: merge record {merge_record_id} "
                f"candidate_ref is '{merge_record.get('candidate_ref')}', requires "
                f"'{canonical_node_ref}' (item 1 W7/Q5)"
            )
        gate_verdict = merge_record.get("gate_verdict")
        if not isinstance(gate_verdict, dict) or gate_verdict.get("verdict") != "land":
            actual = None if not isinstance(gate_verdict, dict) else gate_verdict.get("verdict")
            raise HtError(
                f"cannot merge node {node_id}: merge record {merge_record_id} gate "
                f"verdict is {actual!r}, requires exactly 'land' (item 1 W7)"
            )
        if merge_record.get("consumed_epoch") is not None:
            raise HtError(
                f"cannot merge node {node_id}: merge record {merge_record_id} was "
                f"already consumed at epoch {merge_record['consumed_epoch']} "
                "(item 1 W7 replay guard)"
            )
        mrec.verify_backing_snapshot(ctx.root, merge_record)

        # Mechanical subset retained from B4 §9; item 1 adds the merge-record
        # gate without weakening status, evidence, or sibling-conflict checks.
        if old["status"] != "worked":
            raise HtError(
                f"cannot merge node {node_id}: status '{old['status']}', requires 'worked' "
                f"(B4 §9)"
            )
        granted = [c for c in old.get("claims", []) if c.get("status") == "granted"]
        if not granted:
            raise HtError(
                f"cannot merge node {node_id}: no granted claim backs it (B4 §9)"
            )
        for claim in granted:
            standing_class = claim.get("standing_class")
            if standing_class != "trunk" and claim.get("revalidation") is None:
                class_label = (
                    standing_class if "standing_class" in claim else "absent"
                )
                raise HtError(
                    f"cannot merge node {node_id}: granted claim {claim.get('id')} "
                    f"has standing_class '{class_label}' without trunk re-validation "
                    "evidence (coherence amendments §10)"
                )
        parent = old.get("parent")
        for other_id in _common.all_node_ids(ctx, component):
            if other_id == node_id:
                continue
            other = _common.load_node(ctx, component, other_id)
            if other.get("parent") != parent:
                continue
            if any(c.get("settlement") == "pending" for c in other.get("conflicts", [])):
                raise HtError(
                    f"cannot merge node {node_id}: sibling {other_id} has a pending "
                    f"conflict entry (B4 §9 merge-block)"
                )
        transitions.check_transition("worked", "merged")

    new = dict(old)
    new["status"] = "merged"

    new_tree = dict(tree)
    new_tree["epoch"] = new_epoch  # harness / merge-gate authorship (A1 §5)
    new_tree["epoch_history"] = list(tree["epoch_history"]) + [
        {
            "epoch": new_epoch,
            "merged_node": node_id,
            "date": _common.today(),
            "user_ratified": "n/a",
        }
    ]
    new_tree["cursor"] = [c for c in tree["cursor"] if c["node"] != node_id]
    new_tree["decision_log"] = list(tree["decision_log"]) + [
        _common.decision_entry(
            "merge", frm="worked", to="merged", target=node_id,
            rationale="merge to trunk (v0 mechanical subset)", epoch=new_epoch,
        )
    ]
    consumed_record = dict(merge_record)
    # Preserve an existing stamp in the proposed document so the semantic
    # replay guard reports the attempted reuse before authority's generic
    # frozen-field backstop.  A fresh record is stamped with this merge epoch.
    consumed_record["consumed_epoch"] = (
        new_epoch
        if merge_record.get("consumed_epoch") is None
        else merge_record["consumed_epoch"]
    )

    writes = [
        DocWrite(ctx.root.node_json(component, node_id), "node", old, new),
        DocWrite(ctx.root.tree_json(component), "tree", tree, new_tree),
        DocWrite(
            resolved_record.path,
            "merge_record",
            merge_record,
            consumed_record,
            mechanical_fields=frozenset({"consumed_epoch"}),
        ),
    ]

    # Settlement-trigger seam (A1 §9): the merge mechanically queues one
    # comparison against the new global epoch in every other tree.  Its owning
    # lane director performs the later assessment (item 2+); this plan only
    # records the derived work.
    for other_component in sorted(_common.all_components(ctx)):
        if other_component == component:
            continue
        other_tree = _common.load_tree(ctx, other_component)
        stale_row = {
            "id": _next_watch_id(other_tree),
            "merged_node": canonical_node_ref,
            "prediction_claim": None,
            "observed": None,
            "verdict": None,
            "severity": None,
            "status": "queued",
            "kind": "staleness-assessment",
            "epoch": new_epoch,
        }
        updated_other_tree = dict(other_tree)
        updated_other_tree["watch_queue"] = list(other_tree["watch_queue"]) + [stale_row]
        writes.append(
            DocWrite(
                ctx.root.tree_json(other_component),
                "tree",
                other_tree,
                updated_other_tree,
            )
        )

    return Plan(
        role=ctx.role,
        message=f"ht node merge: {node_id} -> epoch {new_epoch}",
        writes=writes,
        regen_component=component,
        semantic=semantic,
    )
