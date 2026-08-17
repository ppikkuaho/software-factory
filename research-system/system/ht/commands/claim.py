"""Claim adjudication writes (verifier): `ht claim grant`, `ht claim reject`.

v0 collapses the Phase 3 adjudication pipeline (B4 §2) into these commands.

`grant` grants one claim and, on a dispatch's FIRST granted claim, appends an
adjudication ref to the dispatch and legally moves the node unexplored -> worked
(A1 §2.1). Enforces the B4 §9 mechanical rules: report submitted, granted_tier <=
proposed_tier, >=1 resolvable anchor, epoch + instrument legality (U1).

Coherence amendments §10 class evidence as trunk/sandbox/slice: sandbox/slice
claims measure a different system and `revalidate` records verifier evidence
against trunk before those claims may back a merge.

`reject` is the third B4 §2 per-claim verdict (grant / demote / reject) — an
outright rejection with a MANDATORY reason. A1 §4's claim.status enum has no
'rejected' state and we do not deviate from the note, so a rejection creates NO
claim: it is recorded as a verifier-authored adjudication record file (B4 §10's
home, nodes/<id>/adjudications/) carrying the reason, referenced from
dispatch.adjudications. Rejection is a legal verifier act, not an error (exit 0).
"""

from __future__ import annotations

import json
import re

from .. import anchors, gitutil, instruments
from ..errors import HtError, HtUsageError
from ..pipeline import DocWrite, Plan, RawFile
from ..references import (
    ADJUDICATION_HEADER_PREFIX,
    canonical_issue_ref,
    resolve_ref,
)
from . import _common
from ._common import Ctx


_CLAIM_ID = re.compile(r"c-(?P<node>[1-9]\d*(?:\.[1-9]\d*)*)-[1-9]\d*", re.ASCII)


def _assert_new_adjudication_path(
    ctx: Ctx, path, adjudication_ref: str
) -> None:
    root = ctx.root.path.resolve()
    lexical = path.absolute()
    if not lexical.is_relative_to(root):
        raise HtError(f"adjudication {adjudication_ref} destination escapes the root")
    cursor = root
    for part in lexical.relative_to(root).parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise HtError(
                f"adjudication {adjudication_ref} destination has symlinked ancestor "
                f"{cursor}"
            )
        if cursor.exists() and not cursor.is_dir():
            raise HtError(
                f"adjudication {adjudication_ref} destination ancestor is not a directory"
            )
    if path.exists() or path.is_symlink():
        raise HtError(
            f"adjudication {adjudication_ref} path already exists; records are write-once"
        )
    relative = lexical.relative_to(root).as_posix()
    history = gitutil.run(
        ctx.root.path,
        ["log", "--all", "--format=%H", "--", relative],
        check=False,
    )
    if history.returncode != 0 or history.stdout.strip():
        raise HtError(
            f"adjudication {adjudication_ref} reuses a historical path; records are "
            "write-once"
        )


def _resolve_containing_node(ctx: Ctx, component: str, node_id: str):
    resolved = resolve_ref(
        ctx.root,
        f"tree#{component}/node#{node_id}",
        expected={"node"},
    )
    if resolved.path != ctx.root.node_json(component, node_id):
        raise HtError(f"node {resolved.canonical} resolved outside its canonical path")
    return resolved


def _verify_dispatch_report(ctx: Ctx, component: str, dispatch_id: str) -> None:
    resolve_ref(
        ctx.root,
        f"tree#{component}/report#{dispatch_id}",
        expected={"report"},
    )


def _parse_anchor(spec: str) -> dict:
    # PATH:START:END — rsplit twice so paths may contain ':' in theory
    try:
        path, start, end = spec.rsplit(":", 2)
        return {"path": path, "start_line": int(start), "end_line": int(end)}
    except ValueError:
        raise HtError(f"malformed anchor '{spec}' (expected PATH:START:END) (B4 §9)")


def grant(
    ctx: Ctx,
    dispatch_id: str,
    text: str,
    proposed_tier: int,
    granted_tier: int,
    standing_class: str,
    anchor_specs: list[str],
    reason: str | None,
    instrument: str | None,
    epoch: int | None,
    tree_opt: str | None,
) -> Plan:
    component, node_id = _common.resolve_dispatch(ctx, dispatch_id, tree_opt)
    dispatch_path = ctx.root.dispatch_json(component, node_id, dispatch_id)
    dispatch = _common.jsonio.load(dispatch_path)
    resolved_node = _resolve_containing_node(ctx, component, node_id)
    node = resolved_node.document
    global_epoch = _common.current_global_epoch(ctx)
    claim_epoch = global_epoch if epoch is None else epoch
    anchor_docs = [_parse_anchor(s) for s in anchor_specs]

    existing_claims = node.get("claims", [])
    claim_id = f"c-{node_id}-{len(existing_claims) + 1}"
    claim_doc = {
        "id": claim_id,
        "node": node_id,
        "source_dispatch": dispatch_id,
        "text": text,
        "proposed_tier": proposed_tier,
        "granted_tier": granted_tier,
        "standing_class": standing_class,
        "revalidation": None,
        "anchors": anchor_docs,
        "epoch": claim_epoch,
        "instruments": [instrument] if instrument else [],
        "status": "granted",
    }

    new_node = dict(node)
    new_node["claims"] = list(existing_claims) + [claim_doc]

    existing_adj = list(dispatch.get("adjudications", []))
    ordinal = len(existing_adj) + 1
    adjudication_id = f"{dispatch_id}-a{ordinal}"
    adjudication_ref = f"tree#{component}/adjudication#{adjudication_id}"
    record_path = ctx.root.adjudication_path(component, node_id, adjudication_id)
    _assert_new_adjudication_path(ctx, record_path, adjudication_ref)
    verdict = "granted" if granted_tier == proposed_tier else "demoted"
    header = _adjudication_header(
        component=component,
        node_id=node_id,
        dispatch=dispatch,
        adjudication_id=adjudication_id,
        claim_id=claim_id,
        verdict=verdict,
        epoch=claim_epoch,
        proposed_tier=proposed_tier,
        granted_tier=granted_tier,
        reason=reason,
    )

    new_dispatch = dict(dispatch)
    new_dispatch["adjudications"] = existing_adj + [adjudication_ref]
    # On the FIRST granted claim for this dispatch, move the node. "First granted"
    # is detected by prior granted claims from THIS dispatch (not adjudication
    # count) so a preceding rejection does not suppress the flip.
    prior_grants = [
        c for c in existing_claims
        if c.get("source_dispatch") == dispatch_id and c.get("status") == "granted"
    ]
    if not prior_grants:
        if node["status"] == "unexplored":
            # harness-class derived write: the adjudication act (verifier) triggers
            # this lifecycle flip; the machinery executes it as its mechanical
            # consequence (A1 §2.1 "a verified report legally moves unexplored ->
            # worked"; A1 §5 harness authorship column; A1 §1 trigger/write split).
            new_node["status"] = "worked"

    def semantic() -> None:
        current_node = _resolve_containing_node(ctx, component, node_id)
        if current_node.path != resolved_node.path or current_node.document != node:
            raise HtError(f"node {resolved_node.canonical} changed before claim commit")
        if not dispatch.get("report_hash") or not dispatch.get("report_ref"):
            raise HtError(
                f"cannot grant claim: no report submitted for {dispatch_id} "
                f"(B4 §2 structural gate)"
            )
        _verify_dispatch_report(ctx, component, dispatch_id)
        _assert_new_adjudication_path(ctx, record_path, adjudication_ref)
        if granted_tier > proposed_tier:
            raise HtError(
                f"granted_tier {granted_tier} > proposed_tier {proposed_tier} "
                f"(B4 §9 — never grant above proposed tier)"
            )
        if granted_tier < proposed_tier and not reason:
            raise HtError(
                f"demotion granted_tier {granted_tier} < proposed_tier {proposed_tier} "
                f"requires --reason (B4 §2 demote-with-reason)"
            )
        anchors.resolve_all(ctx.root, anchor_docs)
        instruments.check_epoch(claim_epoch, global_epoch)
        if instrument:
            instruments.check_instrument(ctx.root, instrument, claim_epoch)

    return Plan(
        role=ctx.role,
        message=f"ht claim grant: {claim_id} (tier {granted_tier})",
        writes=[
            DocWrite(ctx.root.node_json(component, node_id), "node", node, new_node),
            DocWrite(dispatch_path, "dispatch", dispatch, new_dispatch),
        ],
        raw_files=[RawFile(
            dest=record_path,
            content=_adjudication_record(header, text, reason).encode("utf-8"),
            gitignored=False,
        )],
        regen_component=component,
        semantic=semantic,
    )


def _node_from_claim_id(claim_id: str) -> str:
    match = _CLAIM_ID.fullmatch(claim_id)
    if match is None:
        raise HtUsageError(
            f"malformed claim id '{claim_id}' (expected c-<node>-<N>)"
        )
    return match.group("node")


def revalidate(
    ctx: Ctx,
    claim_id: str,
    ref: str,
    epoch: int | None,
    tree_opt: str | None,
) -> Plan:
    node_id = _node_from_claim_id(claim_id)
    component = _common.resolve_tree_for_node(ctx, node_id, tree_opt)
    node = _common.load_node(ctx, component, node_id)
    claims = node.get("claims", [])
    claim_index = next(
        (index for index, claim_doc in enumerate(claims) if claim_doc.get("id") == claim_id),
        None,
    )
    if claim_index is None:
        raise HtUsageError(f"no such claim '{claim_id}'")

    global_epoch = _common.current_global_epoch(ctx)
    revalidation_epoch = global_epoch if epoch is None else epoch
    old_claim = claims[claim_index]
    new_claim = dict(old_claim)
    new_claim["revalidation"] = {
        "date": _common.today(),
        "epoch": revalidation_epoch,
        "ref": ref,
    }
    new_claims = list(claims)
    new_claims[claim_index] = new_claim
    new_node = dict(node)
    new_node["claims"] = new_claims

    def semantic() -> None:
        if old_claim.get("status") != "granted":
            raise HtError(
                f"claim {claim_id} status '{old_claim.get('status')}' cannot be "
                "revalidated: revalidation attaches to granted claims only [R-i7-3]"
            )
        if old_claim.get("revalidation") is not None:
            raise HtError(
                f"claim {claim_id} revalidation is already set and is fill-once "
                "(coherence amendments §10)"
            )
        instruments.check_epoch(revalidation_epoch, global_epoch)
        claim_epoch = old_claim.get("epoch")
        if revalidation_epoch < claim_epoch:
            raise HtError(
                f"claim {claim_id} revalidation epoch {revalidation_epoch} predates "
                f"claim grant epoch {claim_epoch}; revalidation must not predate the "
                "claim's grant epoch (§10: re-checked against CURRENT trunk) [R-i7-3]"
            )

    return Plan(
        role=ctx.role,
        message=f"ht claim revalidate: {claim_id} at epoch {revalidation_epoch}",
        writes=[DocWrite(ctx.root.node_json(component, node_id), "node", node, new_node)],
        regen_component=component,
        semantic=semantic,
    )


def _adjudication_header(
    *,
    component: str,
    node_id: str,
    dispatch: dict,
    adjudication_id: str,
    claim_id: str | None,
    verdict: str,
    epoch: int,
    proposed_tier: int | None,
    granted_tier: int | None,
    reason: str | None,
) -> dict:
    dispatch_id = dispatch["id"]
    return {
        "schema_version": "ht-adjudication/1.0.0",
        "adjudication_ref": f"tree#{component}/adjudication#{adjudication_id}",
        "dispatch_ref": f"tree#{component}/dispatch#{dispatch_id}",
        "node_ref": f"tree#{component}/node#{node_id}",
        "claim_ref": (
            None if claim_id is None else f"tree#{component}/claim#{claim_id}"
        ),
        "issue_ref": canonical_issue_ref(dispatch.get("issue_ref")),
        "report_ref": f"tree#{component}/report#{dispatch_id}",
        "report_sha256": dispatch.get("report_hash"),
        "verdict": verdict,
        "epoch": epoch,
        "date": _common.today(),
        "proposed_tier": proposed_tier,
        "granted_tier": granted_tier,
        "reason": reason,
    }


def _adjudication_record(header: dict, text: str, reason: str | None) -> str:
    verdict = header["verdict"]
    claim_heading = (
        "Rejected claim (proposed, NOT granted)"
        if verdict == "rejected"
        else "Adjudicated claim"
    )
    reason_body = reason if reason is not None else "None."
    return (
        ADJUDICATION_HEADER_PREFIX
        + json.dumps(
            header,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n\n"
        + f"# Adjudication record — {header['adjudication_ref']}\n\n"
        + f"- verdict: {verdict}\n"
        + "- authorship: verifier (narrow per-command Wave-A record)\n\n"
        + f"## {claim_heading}\n\n{text}\n\n"
        + f"## Reason\n\n{reason_body}\n"
    )


def reject(ctx: Ctx, dispatch_id: str, text: str, reason: str, tree_opt: str | None) -> Plan:
    """Outright rejection (B4 §2 third verdict). Creates NO claim; records a
    verifier-authored adjudication record with the mandatory reason and refs it
    from dispatch.adjudications. Legal verifier act -> exit 0."""
    if not reason:
        raise HtError("claim rejection requires --reason (B4 §2 reject-with-reason)")
    component, node_id = _common.resolve_dispatch(ctx, dispatch_id, tree_opt)
    dispatch_path = ctx.root.dispatch_json(component, node_id, dispatch_id)
    dispatch = _common.jsonio.load(dispatch_path)
    existing_adj = list(dispatch.get("adjudications", []))
    ordinal = len(existing_adj) + 1
    adjudication_id = f"{dispatch_id}-a{ordinal}"
    record_path = ctx.root.adjudication_path(component, node_id, adjudication_id)
    record_ref = f"tree#{component}/adjudication#{adjudication_id}"
    _assert_new_adjudication_path(ctx, record_path, record_ref)
    header = _adjudication_header(
        component=component,
        node_id=node_id,
        dispatch=dispatch,
        adjudication_id=adjudication_id,
        claim_id=None,
        verdict="rejected",
        epoch=_common.current_global_epoch(ctx),
        proposed_tier=None,
        granted_tier=None,
        reason=reason,
    )

    new_dispatch = dict(dispatch)
    # adjudications is harness-owned/tool-managed; the reason lives in the
    # verifier-authored record file the ref points at. NO claim is created and the
    # node status is unchanged (a rejection grants nothing, so nothing moves).
    new_dispatch["adjudications"] = existing_adj + [record_ref]

    def semantic() -> None:
        if not dispatch.get("report_hash") or not dispatch.get("report_ref"):
            raise HtError(
                f"cannot reject a claim: no report submitted for {dispatch_id} "
                f"(B4 §2 structural gate)"
            )
        _verify_dispatch_report(ctx, component, dispatch_id)
        _assert_new_adjudication_path(ctx, record_path, record_ref)

    return Plan(
        role=ctx.role,
        message=f"ht claim reject: {dispatch_id} adjudication {ordinal} (no claim)",
        writes=[DocWrite(dispatch_path, "dispatch", dispatch, new_dispatch)],
        raw_files=[RawFile(
            dest=record_path,
            content=_adjudication_record(header, text, reason).encode("utf-8"),
            gitignored=False,
        )],
        semantic=semantic,
    )
