"""Role x field write-authority — the A1 §10 table encoded as data (D10 gate).

This is the second pipeline step and the whole of the pre-commit hook's authority
check, so CLI and hook share ONE implementation (A2 §4). Both call `check_write`
with (doc_type, old, new, role) and it either passes or raises HtError.

Model (faithful reading of A1 §1/§10):

- Every top-level field of every state doc belongs to a write DOMAIN. node/tree/
  dispatch/ledger fields are all top-level, so top-level-key granularity suffices
  (the one nested exception, node.conflicts[].demoted_to, is handled explicitly
  for the settlement trigger/write split).
- 'harness' is the mechanical/tool-managed domain (git binding, epoch, dispatch
  metering/outcome/ids, index files). Harness fields are tool-written on behalf of
  every command, so a changed harness field is allowed under any role's tool commit.
  This encodes DP-A2-4's "honor-system at launch, hard per-field after": the hard
  gate is on role-OWNED fields; mechanical fields are trusted to the tool.
- CREATION (old is None) has two gates. (i) WHO may create: the doc's creator role
  (fixed per type; section-based for the ledger). (ii) WHAT the birth may carry:
  for node/tree/dispatch the SAME per-field check runs against a synthetic default
  birth doc (`_default_doc`), so any field with non-default content is authority-
  checked against the creating role — a director's mint defaults pass untouched,
  but a hand-crafted node bearing verifier-owned standing='supported' or non-empty
  claims is rejected (C2 fix; closes the "rich node + director commit -> merge with
  no verifier" D10 bypass). The ledger is the exception: its section IS the birth-
  authorship partition (the creator owns all content), so only the director-owned
  status is guarded against a smuggled non-open triage state.
- user overrides director-owned fields (A1 §10 'override' column).
- One DERIVED MECHANICAL WRITE in the harness authorship class touches an
  otherwise director-owned field: node.status unexplored->worked. It is the same
  class as index/live-view regen, git_binding creation at first dispatch, and
  epoch advance at merge — A1 §5 v2 gives that class its own write column, and
  A1 §1's trigger/write split is the general shape (the acting role's act queues
  the write; the owning machinery executes it). The verifier's adjudication act
  (`ht claim grant`) is the trigger; the machinery executes the lifecycle flip as
  its mechanical consequence, per A1 §2.1 "a verified report legally moves
  unexplored -> worked". The legality is single-sourced and TIGHT: the flip is
  admitted ONLY when the SAME document diff shows current==unexplored -> worked AND
  a newly granted claim (C1 fix) — so a verifier hand-editing status alone, with no
  adjudication, is rejected, and every other status change is director-owned.
- Settlement trigger/write split (A1 §1/§7): `ht ledger create --from-settlement`
  is verifier-run yet fills node.conflicts[].demoted_to (director domain). A
  verifier conflicts-change limited to demoted_to null->ref is allowed.
- Embedded claims remain verifier-owned wholesale. `ht claim revalidate` enforces
  claim.revalidation null-to-value fill-once, but the hook's top-level authority
  comparison does not freeze that nested field against a direct verifier rewrite
  (M1/M2 hook-boundary residual; coherence amendments §10).
"""

from __future__ import annotations

from typing import Any

from .errors import HtError

ROLES = {"director", "verifier", "unit", "user", "harness", "pc", "cgate"}

# R1 is intentionally an identity sentinel, not None or an empty string. Presence
# of HT_LANE and its value are separate facts at the CLI/commit boundary.
UNASSIGNED_LANE = object()


def verifier_lane_authorized(book: str, lane: object | str = UNASSIGNED_LANE) -> bool:
    """Return whether a verifier credential is assigned for a book-scoped write.

    absent HT_LANE is unassigned and most-restrictive; item 1 activates book routing.
    For entry creation, an assigned verifier may dock in its own lane book or the
    top book. Cross-book echo is a separate append-only exception enforced by
    ``check_write``.
    """
    if lane is UNASSIGNED_LANE:
        return False
    if isinstance(lane, str):
        normalized = lane.strip()
        return bool(normalized) and book in {normalized, "top"}
    return False

# doc_type -> { field_name -> set(owner roles) }.  'harness' == mechanical/tool.
FIELD_OWNERS: dict[str, dict[str, set[str]]] = {
    "node": {
        # NAVIGATION DOMAIN (director; user override)
        "id": {"director"},
        "parent": {"director"},
        "premise": {"director"},
        "minted_from": {"director"},
        "supersedes": {"director"},
        "superseded_by_node": {"director"},
        "status": {"director"},
        "status_reason": {"director"},
        "conflicts": {"director"},
        # GIT BINDING (harness)
        "git_binding": {"harness"},
        # EPISTEMIC DOMAIN (verifier)
        "standing": {"verifier"},
        "standing_note": {"verifier"},
        "claims": {"verifier"},
        "measurements": {"verifier"},
        "learnings_out": {"verifier"},
    },
    "tree": {
        "component": {"director"},
        "root_question": {"director"},
        "epoch": {"harness"},
        "epoch_history": {"harness"},
        "cursor": {"director"},
        "decision_log": {"director"},
        "global_learnings": {"verifier"},
        # rows created mechanically at merge; observed/verdict verifier-authored
        "watch_queue": {"harness", "verifier"},
    },
    "dispatch": {
        "id": {"harness"},
        "node": {"harness"},
        "epoch": {"harness"},
        "issue_ref": {"director"},
        "question": {"director"},
        "done_definition": {"director"},
        "plan_ref": {"director"},
        "steers": {"director"},
        "interrupt": {"unit"},
        "outcome": {"harness"},
        "metering": {"harness"},
        "role_packet": {"harness"},
        "adjudications": {"harness"},
        "report_ref": {"harness"},
        "archive_ref": {"harness"},
        "report_hash": {"harness"},
    },
    "ledger_entry": {
        # creation ownership is section-based (see CREATOR); post-creation the only
        # v0 mutation is the director-owned status transition.
        "status": {"director"},
        "text": {"verifier"},
        "proposed_by": {"verifier"},
        "support_count": {"verifier"},
        "echoes": {"verifier"},
        "cross_refs": {"verifier"},
        "dedup_log": {"verifier"},
        "anchors": {"verifier"},
        "section": {"verifier", "user"},
        "id": {"verifier", "user"},
        "component": {"verifier", "user"},
        "lane": {"verifier", "user"},
        "intended_scope": {"verifier"},
    },
    "phase": {
        "mode": {"user"},
        "set_by": {"user"},
        "date": {"user"},
    },
    "issue": {
        "id": {"pc"},
        "title": {"pc"},
        "provenance": {"pc"},
        "scope": {"pc"},
        "question": {"pc"},
        "done_definition": {"pc"},
        "lanes": {"pc"},
        "subgoals": {"pc"},
        "observatory_attachments": {"pc"},
        "status": {"pc"},
        "closure": {"pc"},
    },
    "pc_decision": {
        "id": {"pc"},
        "date": {"pc"},
        "kind": {"pc"},
        "decision": {"pc"},
        "context_refs": {"pc"},
        "ref": {"pc"},
    },
    "issue_queue": {
        "entries": {"pc"},
    },
    "interrupt": {
        "id": {"director"},
        "raised_by": {"director"},
        "issue_ref": {"director"},
        "sub_goal_ref": {"director"},
        "kind": {"director"},
        "rationale": {"director"},
        "date": {"director"},
    },
    "ratification_item": {
        "id": {"cgate", "verifier", "pc", "harness"},
        "kind": {"cgate", "verifier", "pc", "harness"},
        "payload_ref": {"cgate", "verifier", "pc", "harness"},
        "text": {"cgate", "verifier", "pc", "harness"},
        "queued_by": {"cgate", "verifier", "pc", "harness"},
        "date": {"cgate", "verifier", "pc", "harness"},
        "disposition": {"user"},
        "annotations": {"pc"},
    },
    "merge_record": {
        "id": {"harness"},
        "candidate_ref": {"harness"},
        # Harness-authored means the harness TRANSCRIBES the lane gate's outcome
        # at screen time; the epistemic source remains the lane verifier's
        # adjudication -- never read this as harness-decided.
        "lane_verdict": {"harness"},
        "lane_adjudication_ref": {"harness"},
        "backing_claims": {"harness"},
        "scope": {"harness"},
        "screen": {"harness"},
        "gate_verdict": {"cgate"},
        "watch_link": {"harness"},
        "created": {"harness"},
        "consumed_epoch": {"harness"},
    },
    "gate_review": {
        "id": {"cgate"},
        "merge_record_ref": {"cgate"},
        "created": {"cgate"},
        "stage": {"cgate"},
        "attempt_id": {"cgate"},
        "screen_ref": {"cgate"},
        "packet": {"cgate"},
        "template": {"cgate"},
        "generator": {"cgate"},
        "rules_fired": {"cgate"},
        "verdict": {"cgate"},
        "note": {"cgate"},
        "observations": {"cgate"},
        "raw_output": {"cgate"},
        "escalation_ref": {"cgate"},
    },
    # index.json / index.live.json and ledger/union.index.json are generated
    # harness views owned wholesale (J7 / item 1 W3 regen).
    "index": {},
    "ledger_union_index": {},
    "composed_tree": {},
}

# doc_type -> creator role. ledger_entry is section-based, resolved separately.
CREATOR = {
    "node": "director",
    "tree": "director",
    "dispatch": "director",
    "index": "harness",
    "phase": "user",
    "issue": "pc",
    "pc_decision": "pc",
    "issue_queue": "pc",
    "interrupt": "director",
    "merge_record": "harness",
    "gate_review": "cgate",
}

_RATIFICATION_CREATOR = {
    "cgate-escalation": "cgate",
    "tier3-ratification": "verifier",
    "improvement-note": "harness",
    "activation-request": "pc",
}

_TIER1_DOC_TYPES = {
    "phase", "issue", "pc_decision", "ratification_item", "merge_record",
    "gate_review", "issue_queue", "interrupt",
}

_EMPTY = object()


def _norm(v: Any) -> Any:
    """Treat null / [] / {} / '' as one 'unwritten' sentinel so creation
    scaffolding (empty cross-domain containers, default nulls) is not a write."""
    if v is None or v == [] or v == {} or v == "":
        return _EMPTY
    return v


def changed_fields(old: dict | None, new: dict) -> dict[str, tuple[Any, Any]]:
    old = old or {}
    changed: dict[str, tuple[Any, Any]] = {}
    for k in sorted(set(old) | set(new)):  # sorted => stable rejection messages
        ov, nv = old.get(k), new.get(k)
        if _norm(ov) != _norm(nv):
            changed[k] = (ov, nv)
    return changed


def ledger_creator_for_section(section: str) -> str:
    # A1 §7: user section = user; research/observatory = verifier only.
    return "user" if section == "user" else "verifier"


def _granted_claim_ids(doc: dict) -> set[str]:
    return {
        c.get("id")
        for c in (doc.get("claims") or [])
        if c.get("status") == "granted"
    }


def _is_mechanical_status_flip(doc_type: str, old: dict | None, new: dict) -> bool:
    """The one derived mechanical write in the harness authorship class that lands
    on a director-owned field: node.status unexplored -> worked, executed by the
    machinery as the mechanical consequence of a verified report (A1 §2.1), the
    same class as index regen / git_binding / epoch advance (A1 §5 harness column;
    A1 §1 trigger/write split).

    TIGHT — both must hold in the SAME document diff (C1 fix): the exact value
    transition unexplored -> worked, AND a newly granted claim (the adjudication
    that triggers the flip). CLI `ht claim grant` satisfies both by construction; a
    bare hand-edited status flip with no adjudication carries no new granted claim
    and is therefore rejected by the CLI gate and the pre-commit hook alike."""
    if doc_type != "node" or old is None:
        return False
    if old.get("status") != "unexplored" or new.get("status") != "worked":
        return False
    return bool(_granted_claim_ids(new) - _granted_claim_ids(old))


def _conflicts_demotion_fill_only(ov: Any, nv: Any) -> bool:
    """True iff a node.conflicts change is limited to filling demoted_to
    (null -> ref) — the settlement trigger/write-split execution (A1 §1/§7)."""
    ov = ov or []
    nv = nv or []
    if len(ov) != len(nv):
        return False
    for a, b in zip(ov, nv):
        rest_a = {k: v for k, v in a.items() if k != "demoted_to"}
        rest_b = {k: v for k, v in b.items() if k != "demoted_to"}
        if rest_a != rest_b:
            return False
        if a.get("demoted_to") == b.get("demoted_to"):
            continue
        if a.get("demoted_to") is None and b.get("demoted_to") is not None:
            continue
        return False
    return True


# Explicit "default-unwritten" birth state per doc type (C2 fix). Only fields whose
# default is a NON-empty value that a NON-creator domain owns need listing here;
# empty arrays / nulls are already treated as unwritten by _norm. For a node the
# epistemic defaults (standing=untested) must appear so a legitimate director mint
# shows no epistemic write, while any richer epistemic content on a hand-crafted
# node registers as a change and is authority-checked against the creating role.
def _default_doc(doc_type: str) -> dict:
    if doc_type == "node":
        return {"status": "unexplored", "standing": "untested"}
    if doc_type == "issue":
        return {"status": "proposed", "closure": None}
    if doc_type == "ratification_item":
        # Null is the required undisposed creation state. The appender leaves the
        # user-owned disposition group untouched; annotations begin as an empty
        # PC-owned append-only list.
        return {"disposition": None, "annotations": []}
    if doc_type == "issue_queue":
        return {"entries": []}
    if doc_type == "merge_record":
        # Null is the canonical awaiting-verdict creation state. The harness leaves
        # the cgate-owned group untouched at birth; cgate later fills it atomically.
        return {"gate_verdict": None, "consumed_epoch": None}
    return {}


def _issue_status_is_phase_gated(old_status: Any, new_status: Any) -> bool:
    # Gate the destination, not an enumerated predecessor pair. Otherwise an issue
    # can be parked/withdrawn/closed and then re-enter active state around the phase
    # policy. This also covers non-default status supplied at creation time because
    # creation is checked against the synthetic proposed-state birth document.
    return old_status != new_status and new_status in {"ratified", "active"}


def _list_append_only(old: Any, new: Any) -> bool:
    old_list = old or []
    new_list = new or []
    return (
        isinstance(old_list, list)
        and isinstance(new_list, list)
        and len(new_list) >= len(old_list)
        and new_list[: len(old_list)] == old_list
    )


def _is_terminal_disposition(value: Any) -> bool:
    """Whether a ratification disposition is one complete terminal user write."""
    if not isinstance(value, dict):
        return False
    if value.get("status") not in {"accepted", "rejected", "deferred"}:
        return False
    return all(
        isinstance(value.get(field), str) and bool(value[field].strip())
        for field in ("by", "date")
    )


def _is_terminal_gate_verdict(value: Any) -> bool:
    """Whether a merge gate verdict is one complete terminal cgate write."""
    if not isinstance(value, dict):
        return False
    verdict = value.get("verdict")
    if not isinstance(verdict, str) or not verdict.strip():
        return False
    return all(
        isinstance(value.get(field), str) and bool(value[field].strip())
        for field in ("date", "review_ref", "review_sha256", "note")
    )


def _assigned_lane_name(lane: object | str) -> str | None:
    if lane is UNASSIGNED_LANE or not isinstance(lane, str):
        return None
    normalized = lane.strip()
    return normalized or None


def _cross_book_echo_is_atomic(old: dict, new: dict) -> bool:
    """Exact W5 exception: one echo + support increment + one dedup event."""
    changes = changed_fields(old, new)
    if set(changes) != {"dedup_log", "echoes", "support_count"}:
        return False
    old_echoes = old.get("echoes") or []
    new_echoes = new.get("echoes") or []
    old_dedup = old.get("dedup_log") or []
    new_dedup = new.get("dedup_log") or []
    return (
        isinstance(old_echoes, list)
        and isinstance(new_echoes, list)
        and new_echoes[: len(old_echoes)] == old_echoes
        and len(new_echoes) == len(old_echoes) + 1
        and isinstance(old_dedup, list)
        and isinstance(new_dedup, list)
        and new_dedup[: len(old_dedup)] == old_dedup
        and len(new_dedup) == len(old_dedup) + 1
        and isinstance(old.get("support_count"), int)
        and new.get("support_count") == old["support_count"] + 1
    )


def check_write(
    doc_type: str,
    old: dict | None,
    new: dict,
    role: str,
    *,
    ledger_section: str | None = None,
    ledger_book: str | None = None,
    phase: str = "sign-off",
    lane: object | str = UNASSIGNED_LANE,
    mechanical_fields: frozenset[str] = frozenset(),
) -> None:
    """Raise HtError if `role` is not authorised for this write (A1 §10 / D10).

    ``mechanical_fields`` is a narrow pipeline seam for fields derived by the
    current command plan.  It is not inferred from the acting role: in
    particular, ``merge_record.consumed_epoch`` is legal only in the same
    director-triggered merge plan that advances the epoch (item 1 Q4).
    """
    if role not in ROLES:
        raise HtError(f"unknown role '{role}' (valid: {sorted(ROLES)}) (A1 §10)")
    if phase not in ("sign-off", "autonomy"):
        raise HtError(f"unknown phase '{phase}' (sign-off|autonomy) (A1 §10)")
    # index docs are harness-owned wholesale — regenerated by the tool.
    if doc_type in {"index", "ledger_union_index", "composed_tree"}:
        return

    is_creation = old is None

    if is_creation:
        # (i) WHO may create this doc? creator role is fixed per doc type, or
        # section-based for the ledger (A1 §7 birth-authorship partition).
        if doc_type == "ledger_entry":
            section = ledger_section or (new.get("section") if new else None)
            creator = ledger_creator_for_section(section) if section else None
            if creator is None:
                raise HtError("ledger entry missing section (A1 §7)")
        elif doc_type == "ratification_item":
            kind = new.get("kind") if new else None
            creator = _RATIFICATION_CREATOR.get(kind)
            if creator is None:
                raise HtError(
                    f"ratification_item.kind '{kind}' has no creator mapping "
                    f"(A1 §10 write-authority)"
                )
            queued_by = new.get("queued_by")
            if queued_by != creator:
                raise HtError(
                    f"ratification_item.queued_by '{queued_by}' must match the "
                    f"kind-selected creator (owner: {creator}) "
                    f"(A1 §10 write-authority)"
                )
        else:
            creator = CREATOR.get(doc_type)
        if creator is None:
            raise HtError(f"no creator role for doc type '{doc_type}' (A1 §10)")
        empty_queue_bootstrap = (
            doc_type == "issue_queue"
            and role == "harness"
            and new == {"entries": []}
        )
        if not (
            role == creator
            or empty_queue_bootstrap
            or (
                role == "harness"
                and doc_type not in _TIER1_DOC_TYPES
                and doc_type != "ledger_entry"
            )
            or (
                role == "user"
                and creator in {"director", "pc"}
                and doc_type not in {"ratification_item", "pc_decision", "interrupt"}
            )
        ):
            raise HtError(
                f"role '{role}' may not create a {doc_type} "
                f"(creator: {creator}) (A1 §10 write-authority)"
            )
        # (ii) WHAT content may the birth carry? For the ledger, the section IS the
        # birth-authorship partition — the creator owns all content — so we only
        # guard the director-owned status against a smuggled triage state (A1 §7).
        if doc_type == "ledger_entry":
            state = (new.get("status") or {}).get("state")
            if state not in (None, "open"):
                raise HtError(
                    f"ledger entry created with non-open status '{state}' — status "
                    f"transitions are director-owned (A1 §7 / §10)"
                )
            if role == "verifier":
                lane_name = _assigned_lane_name(lane)
                if lane_name is None:
                    raise HtError(
                        "unassigned verifier lane may not create a book-scoped "
                        "ledger entry (R1; coherence amendments §8)"
                    )
                book = ledger_book
                if book is None or not verifier_lane_authorized(book, lane_name):
                    raise HtError(
                        f"verifier lane '{lane_name}' may not create in ledger book "
                        f"'{book or 'unknown'}' (allowed: own book or top; A1 §7 book authority)"
                    )
                # ITEM-2 STRICT-LCA SEAM: top-book creation records the acting
                # lane now; item 2 will validate that lane as the LCA of scope.
                if new.get("lane") != lane_name:
                    raise HtError(
                        f"verifier-created ledger entry must record lane '{lane_name}' "
                        "in provenance field ledger_entry.lane (A1 §7 book authority)"
                    )
            elif role == "user" and new.get("lane") is not None:
                raise HtError(
                    "user-created ledger entry must record null ledger_entry.lane; "
                    "HT_LANE provenance is verifier-class only (A1 §7 book authority)"
                )
            return
        if doc_type == "merge_record" and new.get("consumed_epoch") is not None:
            raise HtError(
                "merge_record.consumed_epoch must be null at creation; ht fills it "
                "only when an exact-land merge consumes the record "
                "(coherence amendments §3; D10)"
            )
        # For structured state docs, run the SAME per-field check against the synthetic
        # default birth doc (C2 fix): any field carrying non-default content must be
        # owned by the creating role, so a director cannot smuggle verifier-owned
        # epistemic content (standing / claims) into a hand-crafted node.
        old = _default_doc(doc_type)

    # Creator-stamped identity/date fields are immutable after birth. Phase.date is
    # a set-date, not an object-creation date, so it remains user-writable.
    immutable_fields = {
        "ledger_entry": {"id", "section", "lane"},
        "issue": {"id"},
        "merge_record": {
            "id",
            "candidate_ref",
            "lane_verdict",
            "lane_adjudication_ref",
            "backing_claims",
            "scope",
            "created",
        },
    }.get(doc_type, set())
    if not is_creation:
        immutable_changed = [
            field for field in immutable_fields if _norm(old.get(field)) != _norm(new.get(field))
        ]
        if immutable_changed:
            fields_str = ", ".join(f"{doc_type}.{field}" for field in sorted(immutable_changed))
            raise HtError(
                f"role '{role}' may not mutate {fields_str} "
                f"(owner: frozen-after-creation) (A1 §10 write-authority; D10)"
            )

    if doc_type == "ledger_entry" and role == "verifier" and not is_creation:
        lane_name = _assigned_lane_name(lane)
        if lane_name is None:
            raise HtError(
                "unassigned verifier lane may not mutate a book-scoped ledger "
                "entry (R1; coherence amendments §8)"
            )
        if ledger_book is None:
            raise HtError("ledger write missing book context (A1 §7 book authority)")
        if ledger_book != lane_name and not _cross_book_echo_is_atomic(old, new):
            raise HtError(
                f"verifier lane '{lane_name}' may write cross-book ledger/{ledger_book} "
                "only via atomic echo + support + dedup append "
                "(A1 §7 book authority; coherence amendments §8)"
            )

    # Queue rows are append-only outside the user-owned disposition and the PC's
    # reserved append-only annotations. Their creator owns birth content, never a
    # later rewrite of the payload.
    if doc_type == "ratification_item" and not is_creation:
        changes = changed_fields(old, new)
        frozen = [f for f in changes if f not in {"disposition", "annotations"}]
        if frozen:
            fields_str = ", ".join(f"ratification_item.{f}" for f in frozen)
            raise HtError(
                f"role '{role}' may not mutate {fields_str} "
                f"(owner: frozen-after-creation) "
                f"(A1 §10 write-authority; append-only; D10)"
            )
        if "annotations" in changes:
            ov, nv = changes["annotations"]
            if not _list_append_only(ov, nv):
                raise HtError(
                    "ratification_item.annotations is append-only "
                    "(owner: pc) (A1 §10 write-authority; D10)"
                )

        if "disposition" in changes:
            old_disposition = old.get("disposition")
            new_disposition = new.get("disposition")
            if old_disposition is not None:
                raise HtError(
                    "ratification_item.disposition is frozen after its first user "
                    "write (owner: user) (A1 §10 write-authority; D10)"
                )
            if not _is_terminal_disposition(new_disposition):
                raise HtError(
                    "ratification_item.disposition must be filled atomically from "
                    "null to a terminal accepted/rejected/deferred value with by "
                    "and date (owner: user) (A1 §10 write-authority; D10)"
                )

    if doc_type == "interrupt" and not is_creation and old != new:
        raise HtError(
            "interrupt is frozen after creation (owner: frozen-after-creation) "
            "(F11; A1 §10 write-authority; D10)"
        )

    if doc_type == "gate_review" and not is_creation and old != new:
        raise HtError(
            "gate_review is frozen after creation (owner: cgate) "
            "(A1 §10 write-authority; D10)"
        )

    if (
        doc_type == "merge_record"
        and old.get("screen") != new.get("screen")
        and old.get("gate_verdict") is not None
    ):
        raise HtError(
            "merge_record.screen is frozen after gate verdict under [R-i7-1] D2 "
            "(owner: harness; A1 §10 write-authority; D10)"
        )

    # The cgate-owned verdict group follows the same null-at-creation, atomic-fill,
    # frozen-after-first-write pattern as ratification disposition. Harness may not
    # birth a verdict, and even cgate may neither partially fill nor revise/remove it.
    if (
        doc_type == "merge_record"
        and old.get("gate_verdict") != new.get("gate_verdict")
    ):
        old_verdict = old.get("gate_verdict")
        new_verdict = new.get("gate_verdict")
        if old_verdict is not None:
            raise HtError(
                "merge_record.gate_verdict is frozen after its first cgate write "
                "(owner: cgate) (A1 §10 write-authority; D10)"
            )
        if not _is_terminal_gate_verdict(new_verdict):
            raise HtError(
                "merge_record.gate_verdict must be filled atomically from null to "
                "a non-empty verdict with date, review linkage/hash, and note "
                "(owner: cgate) "
                "(A1 §10 write-authority; D10)"
            )

    if (
        doc_type == "merge_record"
        and old.get("consumed_epoch") != new.get("consumed_epoch")
    ):
        old_epoch = old.get("consumed_epoch")
        new_epoch = new.get("consumed_epoch")
        if old_epoch is not None:
            raise HtError(
                "merge_record.consumed_epoch is frozen after merge consumption "
                "(owner: harness) (coherence amendments §3; D10)"
            )
        if not isinstance(new_epoch, int) or isinstance(new_epoch, bool) or new_epoch < 0:
            raise HtError(
                "merge_record.consumed_epoch must be filled atomically from null "
                "to the non-negative landed epoch (owner: harness) "
                "(coherence amendments §3; D10)"
            )
        if role != "director" or "consumed_epoch" not in mechanical_fields:
            raise HtError(
                "merge_record.consumed_epoch may be filled only through the "
                "derived mechanical-write seam of the same director-triggered "
                "node merge plan (owner: harness; item 1 Q4; coherence "
                "amendments §3; A1 §10; D10)"
            )

    # MUTATION (and creation-content) — per-field. Collect ALL violating fields and
    # raise once, listing them in sorted (deterministic) order: changed_fields is
    # already sorted, so the message never depends on iteration order.
    owners_table = FIELD_OWNERS.get(doc_type, {})
    violations: list[tuple[str, str]] = []
    for field, (ov, nv) in changed_fields(old, new).items():
        owners = owners_table.get(field, set())
        if "harness" in owners and doc_type not in _TIER1_DOC_TYPES:
            continue  # mechanical/tool-managed field
        if (
            doc_type == "issue"
            and field == "status"
            and _issue_status_is_phase_gated(ov, nv)
        ):
            if role == "user" or (phase == "autonomy" and role == "pc"):
                continue
            owner = "user" if phase == "sign-off" else "pc"
            violations.append(("issue.status", owner))
            continue
        if role in owners:
            continue
        if (
            role == "user"
            and owners.intersection({"director", "pc"})
            and not (doc_type == "ratification_item" and field == "annotations")
        ):
            continue  # A1 §10 user override of navigation fields
        if field == "status" and _is_mechanical_status_flip(doc_type, old, new):
            continue  # harness-class derived write (A1 §2.1 + §5): unexplored -> worked
            #          + a new granted claim in the SAME diff (C1)
        if (
            doc_type == "node"
            and field == "conflicts"
            and role == "verifier"
            and _conflicts_demotion_fill_only(ov, nv)
        ):
            continue  # settlement demotion trigger/write split
        if (
            doc_type == "merge_record"
            and field == "consumed_epoch"
            and role == "director"
            and "consumed_epoch" in mechanical_fields
            and ov is None
            and isinstance(nv, int)
            and not isinstance(nv, bool)
            and nv >= 0
        ):
            continue  # director-triggered merge; ht performs derived harness stamp (Q4)
        owner_str = "/".join(sorted(owners)) or "nobody"
        violations.append((f"{doc_type}.{field}", owner_str))

    if violations:
        owners_seen = {o for _, o in violations}
        if len(owners_seen) == 1:
            fields_str = ", ".join(f for f, _ in violations)
            raise HtError(
                f"role '{role}' does not own {fields_str} "
                f"(owner: {owners_seen.pop()}) (A1 §10 write-authority; D10)"
            )
        parts = ", ".join(f"{f} (owner: {o})" for f, o in violations)
        raise HtError(
            f"role '{role}' does not own {parts} (A1 §10 write-authority; D10)"
        )
