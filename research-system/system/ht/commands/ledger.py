"""Federated ledger commands: create, status, and atomic support echo.

Section partition (A1 §7): user-section creation requires HT_ROLE=user;
research/observatory require HT_ROLE=verifier — enforced by the authority creation
check (a director attempting research-section creation is rejected). Status
transitions are director-owned. Echo support and its optional dedup event land in
one document write and one commit.

--from-settlement executes the settlement trigger/write split: it creates a
research-section entry proposed_by settlement#node-<ID>, seeds support from the
demoted branch's granted claims, and fills that node's conflict demoted_to.
"""

from __future__ import annotations

import re

from ..errors import HtError, HtUsageError
from ..pipeline import DocWrite, Plan
from . import _common
from ._common import Ctx


SECTIONS = ("user", "research", "observatory")

# D8 Phase-B provisional matcher.  Lowercase alphanumeric tokens are compared
# after removing deliberately small, language-neutral connective stopwords.  A
# candidate needs two shared tokens, except that a one-token shorter text needs
# that one token.  Empty token sets carry no match signal.
D8_SHARED_TOKEN_CAP = 2
D8_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)

D8_FRAMING = (
    "D8 is RATIFIED as inform-don't-block. The union screen is its mechanical "
    "realization, not a supersession: the create SURFACES candidates and requires "
    "an explicit disposition from the same authorized caller (--dedup-distinct with "
    "dedup_log events, or abandon-and-echo) — nothing is refused, nothing ever "
    "auto-merges, the decision is merely made explicit and traceable by the "
    "authority who was going to make it anyway. If the screen ever blocks "
    "legitimate flow in live use, that is a revisit-D8-realization signal to "
    "surface, never a rule to route around."
)


def _normalized_tokens(text: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in D8_STOPWORDS
    )


def _is_d8_candidate(proposal: str, existing: str) -> bool:
    proposal_tokens = _normalized_tokens(proposal)
    existing_tokens = _normalized_tokens(existing)
    if not proposal_tokens or not existing_tokens:
        return False
    shorter_size = min(len(proposal_tokens), len(existing_tokens))
    threshold = min(D8_SHARED_TOKEN_CAP, shorter_size)
    return len(proposal_tokens & existing_tokens) >= threshold


def union_dedup_candidates(ctx: Ctx, text: str) -> list[tuple[str, str, dict]]:
    """Return matching ``(book, section, entry)`` rows across every ledger book."""
    return [
        (book, section, entry)
        for book, section, _path, entry in _common.iter_ledger_entries(ctx)
        if _is_d8_candidate(text, entry.get("text", ""))
    ]


def _entry_lane(ctx: Ctx) -> str | None:
    # Users intentionally have no lane credential in v1 (Q3); lane seats are an
    # item-2+ growth seam.  Verifier-class writes preserve their adjudicating lane.
    if ctx.role != "verifier":
        return None
    if isinstance(ctx.lane, str) and ctx.lane.strip():
        return ctx.lane.strip()
    return None


def _default_book(ctx: Ctx) -> str:
    lane = _entry_lane(ctx)
    return lane if lane is not None else "top"


def _validate_d8_disposition(
    candidates: list[tuple[str, str, dict]],
    dedup_distinct: list[str] | None,
    abandon_and_echo: str | None,
) -> list[str]:
    if dedup_distinct is not None and abandon_and_echo is not None:
        raise HtUsageError(
            "--dedup-distinct and --abandon-and-echo are mutually exclusive (D8)"
        )

    candidate_ids = [entry["id"] for _book, _section, entry in candidates]
    if not candidate_ids:
        if dedup_distinct or abandon_and_echo is not None:
            raise HtUsageError(
                "D8 disposition supplied but the union screen surfaced no candidates"
            )
        return candidate_ids

    surfaced = ", ".join(candidate_ids)
    if dedup_distinct is None and abandon_and_echo is None:
        raise HtError(
            f"D8 union screen surfaced candidates: {surfaced}. "
            "Choose explicit --dedup-distinct for every surfaced L-id or "
            "--abandon-and-echo one surfaced L-id; nothing is refused and nothing "
            "ever auto-merges."
        )

    if dedup_distinct is not None:
        malformed = [value for value in dedup_distinct if re.fullmatch(r"L-\d+", value) is None]
        if malformed:
            raise HtUsageError(
                f"--dedup-distinct values must be ledger ids (L-<n>): {malformed} (D8)"
            )
        if len(dedup_distinct) != len(set(dedup_distinct)):
            raise HtUsageError("--dedup-distinct may list each surfaced candidate once (D8)")
        if set(dedup_distinct) != set(candidate_ids):
            raise HtError(
                f"--dedup-distinct must disposition exactly the surfaced candidates: "
                f"{surfaced} (D8)"
            )
        return candidate_ids

    assert abandon_and_echo is not None
    if abandon_and_echo not in candidate_ids:
        raise HtError(
            f"--abandon-and-echo must name one surfaced candidate ({surfaced}) (D8)"
        )
    return candidate_ids


def create(
    ctx: Ctx,
    section: str,
    text: str,
    proposed_by: str | None,
    from_settlement: str | None,
    component: str | None,
    book: str | None = None,
    dedup_distinct: list[str] | None = None,
    abandon_and_echo: str | None = None,
) -> Plan:
    if section not in SECTIONS:
        raise HtUsageError(f"unknown section '{section}' (user|research|observatory)")
    book = _default_book(ctx) if book is None else book
    if not book.strip() or "/" in book or book in {".", ".."}:
        raise HtUsageError(f"invalid ledger book '{book}'")

    writes: list[DocWrite] = []
    warnings: list[str] = []
    regen: str | None = None
    echoes: list[dict] = []
    support_count = 0
    settlement: tuple[str, str, dict, list[dict], int] | None = None
    cited_issue: dict | None = None

    if from_settlement is not None:
        # node#ID or bare ID
        node_id = from_settlement.split("#", 1)[1] if "#" in from_settlement else from_settlement
        if section != "research":
            raise HtError(
                "settlement-demoted entries land in the research section (A1 §7)"
            )
        tree_component = _common.resolve_tree_for_node(ctx, node_id, component)
        node = _common.load_node(ctx, tree_component, node_id)
        # Seed support: measurements die; verified support survives.  The abandon
        # path does not copy these claims because it appends exactly one proposal
        # echo to the existing entry under Q1.
        for claim in node.get("claims", []):
            if claim.get("status") == "granted":
                echoes.append(
                    {"source_ref": f"claim#{claim['id']}", "epoch": claim["epoch"]}
                )
        support_count = len(echoes)
        proposed_by = f"settlement#node-{node_id}"

        conflicts = node.get("conflicts", [])
        target_idx = None
        for i in range(len(conflicts) - 1, -1, -1):
            if (
                conflicts[i].get("settlement") == "demoted"
                and conflicts[i].get("demoted_to") is None
            ):
                target_idx = i
                break
        if target_idx is None:
            raise HtError(
                f"node {node_id} has no demoted conflict awaiting a ledger ref "
                f"(run `ht settle --resolution demoted` first) (A1 §1/§7)"
            )
        settlement = (tree_component, node_id, node, conflicts, target_idx)

    issue_match = re.fullmatch(r"issue#(I-[0-9]+)", proposed_by or "")
    if issue_match is not None:
        # Issue provenance has the same union-global referential integrity as an
        # issue_ref join, regardless of which federated book receives the row.
        # Keep the docking trigger itself on the established exact fullmatch.
        from . import issue as issue_command

        issue_ref = issue_match.group(0)
        try:
            cited_issue = issue_command.load_issue(ctx, issue_match.group(1))
        except HtUsageError as exc:
            raise HtUsageError(
                f"no such issue provenance ref '{issue_ref}'"
            ) from exc

    candidates = union_dedup_candidates(ctx, text)
    candidate_ids = _validate_d8_disposition(
        candidates, dedup_distinct, abandon_and_echo
    )
    if abandon_and_echo is not None:
        if not isinstance(proposed_by, str) or not proposed_by.strip():
            raise HtUsageError(
                "--abandon-and-echo requires a non-empty proposal source ref "
                "(--proposed-by) so the merged D8 event remains traceable"
            )
        echo_plan = echo(
            ctx,
            abandon_and_echo,
            proposed_by,
            None,
            dedup_matched=proposed_by,
            dedup_resolution="merged",
        )
        if settlement is None:
            return echo_plan
        tree_component, node_id, node, conflicts, target_idx = settlement
        new_node = dict(node)
        new_conflicts = [dict(conflict) for conflict in conflicts]
        new_conflicts[target_idx]["demoted_to"] = f"ledger#{abandon_and_echo}"
        new_node["conflicts"] = new_conflicts
        return Plan(
            role=ctx.role,
            message=f"ht ledger abandon-and-echo: {abandon_and_echo} ({proposed_by})",
            writes=echo_plan.writes
            + [DocWrite(ctx.root.node_json(tree_component, node_id), "node", node, new_node)],
            regen_component=tree_component,
        )

    intended_scope: str | None = None
    if book == "top" and ctx.role == "verifier" and issue_match is not None:
        # Item 2's top-book audit rule docks the verifier at the issue LCA.  Until
        # that lane has a live tree, preserve item 1's any-assigned-lane rule and
        # stamp the intended destination only when the author's lane differs.
        assert cited_issue is not None
        required_lane = cited_issue["scope"]
        author_lane = _entry_lane(ctx)
        if ctx.root.tree_json(required_lane).exists():
            if author_lane != required_lane:
                raise HtError(
                    f"top-book entry for {cited_issue['id']} requires verifier lane "
                    f"'{required_lane}', got '{author_lane}' (coherence amendments §8; "
                    "item 2 X4)"
                )
        elif author_lane != required_lane:
            intended_scope = required_lane
            warnings.append(
                f"LCA FALLBACK WARNING: scope lane '{required_lane}' for issue "
                f"{cited_issue['id']} has no live tree; accepting assigned verifier "
                f"lane '{author_lane}' under the item-1 rule and stamping "
                f"intended_scope='{required_lane}' for re-homing debt (item 2 X4)."
            )

    entry_id = _common.next_ledger_id(ctx)
    if settlement is not None:
        tree_component, node_id, node, conflicts, target_idx = settlement
        new_node = dict(node)
        new_conflicts = [dict(conflict) for conflict in conflicts]
        new_conflicts[target_idx]["demoted_to"] = f"ledger#{entry_id}"
        new_node["conflicts"] = new_conflicts
        writes.append(
            DocWrite(ctx.root.node_json(tree_component, node_id), "node", node, new_node)
        )
        regen = tree_component

    entry = {
        "id": entry_id,
        "section": section,
        "text": text,
        "proposed_by": proposed_by,
        "support_count": support_count,
        "echoes": echoes,
        "cross_refs": [],
        "dedup_log": [
            {"matched": candidate_id, "resolution": "distinct"}
            for candidate_id in candidate_ids
        ],
        "status": {"state": "open", "ref": None, "reason": None},
        "anchors": [],
        "component": component,
        "lane": _entry_lane(ctx),
        "intended_scope": intended_scope,
    }
    writes.insert(
        0,
        DocWrite(
            ctx.root.ledger_entry(book, section, entry_id),
            "ledger_entry", None, entry, ledger_section=section, ledger_book=book,
        ),
    )

    return Plan(
        role=ctx.role,
        message=f"ht ledger create: {entry_id} ({section})",
        writes=writes,
        regen_component=regen,
        warnings=warnings,
    )


def status(ctx: Ctx, entry_id: str, to: str, ref: str | None, reason: str | None) -> Plan:
    if to not in ("minted", "retired", "merged-into"):
        raise HtUsageError(f"unknown target state '{to}' (minted|retired|merged-into)")
    book, section, old = _common.find_ledger_entry(ctx, entry_id)
    new = dict(old)
    new["status"] = {"state": to, "ref": ref, "reason": reason}
    return Plan(
        role=ctx.role,
        message=f"ht ledger status: {entry_id} -> {to}",
        writes=[
            DocWrite(
                ctx.root.ledger_entry(book, section, entry_id),
                "ledger_entry", old, new, ledger_section=section, ledger_book=book,
            )
        ],
    )


def echo(
    ctx: Ctx,
    entry_id: str,
    source_ref: str,
    epoch: int | None,
    dedup_matched: str | None = None,
    dedup_resolution: str | None = None,
) -> Plan:
    """Record a support echo (A1 §7). echoes + support_count are verifier-authored,
    so a non-verifier role is rejected by the field-authority gate. Each call
    appends exactly one {source_ref, epoch} and increments support_count by exactly
    1 — every increment is traceable, never a bare counter."""
    if not source_ref.strip():
        raise HtError(
            "ledger echo requires a non-empty --source-ref — every support "
            "increment must be traceable (A1 §7)"
        )
    if (dedup_matched is None) != (dedup_resolution is None):
        raise HtUsageError(
            "--dedup-matched and --dedup-resolution must be supplied together "
            "(A1 §7 atomic echo/dedup)"
        )
    if dedup_matched is not None and not dedup_matched.strip():
        raise HtUsageError("--dedup-matched must be non-empty (A1 §7)")
    if dedup_resolution not in (None, "distinct", "merged"):
        raise HtUsageError(
            "--dedup-resolution must be distinct or merged (A1 §7)"
        )
    if dedup_resolution == "distinct":
        if re.fullmatch(r"L-\d+", dedup_matched or "") is None:
            raise HtUsageError(
                "--dedup-matched must be a ledger entry id (L-<n>) for "
                "resolution=distinct (A1 §7)"
            )
        _common.find_ledger_entry(ctx, dedup_matched)
    elif dedup_resolution == "merged" and dedup_matched != source_ref:
        raise HtUsageError(
            "resolution=merged requires --dedup-matched to equal the abandoned "
            "proposal --source-ref (A1 §7 / D8)"
        )
    book, section, old = _common.find_ledger_entry(ctx, entry_id)
    # v0 default epoch 0: there are no tree epochs yet (observatory feeds the
    # ledger before any tree exists). Once trees exist this takes the current
    # global trunk epoch (A2 §5 one shared ledger, one epoch counter).
    e = _common.current_global_epoch(ctx) if epoch is None else epoch

    new = dict(old)
    new["echoes"] = list(old.get("echoes", [])) + [{"source_ref": source_ref, "epoch": e}]
    new["support_count"] = old.get("support_count", 0) + 1
    if dedup_matched is not None:
        new["dedup_log"] = list(old.get("dedup_log", [])) + [
            {"matched": dedup_matched, "resolution": dedup_resolution}
        ]

    return Plan(
        role=ctx.role,
        message=f"ht ledger echo: {entry_id} +1 ({source_ref})",
        writes=[
            DocWrite(
                ctx.root.ledger_entry(book, section, entry_id),
                "ledger_entry", old, new, ledger_section=section, ledger_book=book,
            )
        ],
    )
