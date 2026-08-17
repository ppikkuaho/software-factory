"""Pre-commit backstop (A2 §4.2) — invoked as `python -m ht _precommit`.

Second D7 layer. For every staged path it enforces, from the STAGED content only:

- any staged path under trees/|ledger/|readout/|tier1/ requires HT_COMMIT set
  (otherwise: out-of-band state write — use ht);
- no staged path under nodes/*/archive/ (archives are write-once + git-ignored, U2);
- no MODIFICATION of a submitted nodes/*/reports/*.md (report freeze, U2/B4 §9);
- every classified tier1 record rejects deletion, rename, and historical-path
  re-creation; PC decisions reject mutation, while ratification disposition and
  annotation updates retain their field-authority exceptions;
- staged state JSON must schema-validate and the HT_ROLE must own every changed
  field (reusing ht.schemas + ht.authority so nothing is duplicated);
- a staged merge-consumption cohort is recognized only when every granted backing
  claim is trunk-classed or carries re-validation evidence (coherence amendments §10).

Fails closed: any staged violation aborts the commit with a reasoned message and a
non-zero exit.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from . import authority, classify, gitutil, schemas
from .errors import HtError
from .paths import Root
from .references import (
    canonical_json_sha256,
    parse_adjudication_header_line,
)


_TIER1_RECORD_TYPES = {
    "phase",
    "issue",
    "pc_decision",
    "ratification_item",
    "merge_record",
    "gate_review",
    "issue_queue",
    "interrupt",
    "composed_tree",
}

_TIER1_RECORD_LABELS = {
    "phase": "phase document",
    "issue": "issue",
    "pc_decision": "PC decision",
    "ratification_item": "ratification queue item",
    "merge_record": "merge record",
    "gate_review": "gate review",
    "issue_queue": "issue queue",
    "interrupt": "interrupt",
    "composed_tree": "composed tree",
}

_EMPTY_SCAFFOLD_MARKERS = {
    "tier1/merge-records/.gitkeep",
    "tier1/gate-reviews/.gitkeep",
    "trees/.gitkeep",
}


def _reject(msg: str) -> "int":
    sys.stderr.write(f"REJECTED (pre-commit): {msg}\n")
    return 1


def _resolve_root() -> Root:
    if os.environ.get("HT_INTERNAL_GIT_DIR_FD") is not None:
        # Repository transactions start this validator in the already-held
        # worktree directory.  Avoid rediscovering a mutable lexical .git path;
        # gitutil.run separately pins every Git read to the held Git-directory
        # descriptor inherited by this process.
        return Root(Path.cwd().resolve())
    r = gitutil.run(Path.cwd(), ["rev-parse", "--show-toplevel"], check=False)
    top = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else str(Path.cwd())
    return Root(Path(top).resolve())


def _phase_mode(root: Root, staged: list[tuple[str, str]]) -> str:
    """Read phase from the staged index, then HEAD; absence is sign-off."""
    phase_code = next(
        (code for code, path in staged if path == "tier1/phase.json"), None
    )
    if phase_code == "D":
        return "sign-off"
    text = None
    if phase_code is not None:
        text = gitutil.show(root.path, ":tier1/phase.json")
    if text is None:
        text = gitutil.show(root.path, "HEAD:tier1/phase.json")
    if text is None:
        return "sign-off"
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HtError(f"tier1/phase.json is not valid JSON: {exc} (A1 §10)")
    if not isinstance(doc, dict):
        raise HtError("tier1/phase.json must be a JSON object (A1 §10)")
    mode = doc.get("mode")
    if mode not in ("sign-off", "autonomy"):
        raise HtError(
            f"tier1/phase.json has invalid mode '{mode}' (sign-off|autonomy) (A1 §10)"
        )
    return mode


def _path_exists_in_history(root: Root, path: str) -> bool:
    """Whether a path has already been recorded on any reachable ref."""
    result = gitutil.run(
        root.path,
        ["log", "--all", "--format=%H", "--", path],
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _staged_regular_blob(root: Root, path: str) -> bool:
    entry = gitutil.run(
        root.path,
        ["ls-files", "--stage", "-z", "--", path],
        check=False,
    )
    rows = [row for row in entry.stdout.split("\x00") if row]
    header = rows[0].partition("\t")[0] if len(rows) == 1 else ""
    fields = header.split()
    if (
        entry.returncode != 0
        or len(rows) != 1
        or len(fields) != 3
        or fields[0] != "100644"
        or fields[2] != "0"
    ):
        return False
    object_type = gitutil.run(
        root.path,
        ["cat-file", "-t", f":{path}"],
        check=False,
    )
    return object_type.returncode == 0 and object_type.stdout.strip() == "blob"


def _indexed_ledger_paths(root: Root) -> list[str]:
    """Ledger entry paths present in the candidate Git index."""
    result = gitutil.run(root.path, ["ls-files", "-z", "--", "ledger"])
    return sorted(
        path
        for path in result.stdout.split("\x00")
        if path and classify.doc_type(path) == "ledger_entry"
    )


def _indexed_gate_review_paths(root: Root) -> list[str]:
    """Gate-review paths present in the candidate Git index."""
    result = gitutil.run(root.path, ["ls-files", "-z", "--", "tier1/gate-reviews"])
    return sorted(
        path
        for path in result.stdout.split("\x00")
        if path and classify.doc_type(path) == "gate_review"
    )


def _verifier_lane(role: str | None) -> object | str:
    """Normalize the optional verifier lane, failing closed on blank values."""
    raw_lane = os.environ.get("HT_LANE")
    normalized_lane = raw_lane.strip() if raw_lane is not None else ""
    if role == "verifier" and normalized_lane:
        return normalized_lane
    return authority.UNASSIGNED_LANE


_CANDIDATE_REF = re.compile(
    r"tree#(?P<component>[^/#\s]+)/node#(?P<node>[^/#\s]+)"
)
_ADJUDICATION_REF = re.compile(
    r"tree#(?P<component>[^/#\s]+)/adjudication#"
    r"(?P<id>(?P<dispatch>d-[1-9][0-9]*(?:\.[1-9][0-9]*)*-[1-9][0-9]*)-a[1-9][0-9]*)"
)


def _json_at(root: Root, ref_path: str) -> dict | None:
    text = gitutil.show(root.path, ref_path)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _candidate_text(root: Root, path: str, staged_paths: set[str]) -> str | None:
    return gitutil.show(root.path, f":{path}" if path in staged_paths else f"HEAD:{path}")


def _adjudication_header_at(
    root: Root, ref: str, staged_paths: set[str]
) -> dict | None:
    match = _ADJUDICATION_REF.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        return None
    dispatch_id = match.group("dispatch")
    node_id = dispatch_id[2:].rsplit("-", 1)[0]
    path = (
        f"trees/{match.group('component')}/nodes/{node_id}/adjudications/"
        f"{match.group('id')}.md"
    )
    text = _candidate_text(root, path, staged_paths)
    if text is None:
        return None
    first = text.splitlines()[0] if text.splitlines() else ""
    try:
        value = parse_adjudication_header_line(first, path)
    except HtError:
        return None
    return value


def _valid_staged_revalidation(claim: dict, global_epoch: int) -> bool:
    value = claim.get("revalidation")
    claim_epoch = claim.get("epoch")
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("date"), str)
        and value.get("date")
        and isinstance(value.get("ref"), str)
        and value.get("ref")
        and isinstance(claim_epoch, int)
        and not isinstance(claim_epoch, bool)
        and isinstance(value.get("epoch"), int)
        and not isinstance(value.get("epoch"), bool)
        and 0 <= claim_epoch <= value["epoch"] <= global_epoch
    )


def _staged_adjudication_coherent(
    ref: str,
    header: dict,
    component: str,
    node_id: str,
    dispatch: dict,
    claim: dict,
) -> bool:
    dispatch_id = dispatch.get("id")
    if (
        not isinstance(dispatch_id, str)
        or dispatch.get("node") != node_id
        or claim.get("node") != node_id
        or claim.get("source_dispatch") != dispatch_id
        or ref not in dispatch.get("adjudications", [])
    ):
        return False
    expected_issue = dispatch.get("issue_ref")
    expected_issue = None if expected_issue is None else f"issue#{expected_issue}"
    expected = {
        "schema_version": "ht-adjudication/1.0.0",
        "adjudication_ref": ref,
        "dispatch_ref": f"tree#{component}/dispatch#{dispatch_id}",
        "node_ref": f"tree#{component}/node#{node_id}",
        "claim_ref": f"tree#{component}/claim#{claim.get('id')}",
        "issue_ref": expected_issue,
        "report_ref": f"tree#{component}/report#{dispatch_id}",
        "report_sha256": dispatch.get("report_hash"),
        "epoch": claim.get("epoch"),
        "proposed_tier": claim.get("proposed_tier"),
        "granted_tier": claim.get("granted_tier"),
    }
    if any(header.get(field) != value for field, value in expected.items()):
        return False
    verdict = header.get("verdict")
    proposed = claim.get("proposed_tier")
    granted = claim.get("granted_tier")
    if (
        not isinstance(proposed, int)
        or isinstance(proposed, bool)
        or not isinstance(granted, int)
        or isinstance(granted, bool)
        or granted > proposed
    ):
        return False
    if verdict != ("granted" if granted == proposed else "demoted"):
        return False
    if not isinstance(header.get("date"), str) or not header.get("date"):
        return False
    reason = header.get("reason")
    if reason is not None and not isinstance(reason, str):
        return False
    return verdict != "demoted" or bool(reason)


def _staged_backing_snapshot_matches(
    root: Root,
    record: dict,
    component: str,
    node_id: str,
    node: dict,
    staged_paths: set[str],
) -> bool:
    expected = record.get("backing_claims")
    lane_ref = record.get("lane_adjudication_ref")
    if not isinstance(expected, list) or not expected or not isinstance(lane_ref, str):
        return False
    global_epoch = _head_global_epoch(root)
    if global_epoch is None:
        return False
    current: list[dict] = []
    canonical_claim_refs: set[str] = set()
    coherent_adjudication_refs: set[str] = set()
    issue_refs: set[str] = set()
    minted_from = node.get("minted_from")
    if isinstance(minted_from, str) and re.fullmatch(r"issue#I-[0-9]+", minted_from):
        issue_refs.add(minted_from)
    for claim in node.get("claims", []):
        if not isinstance(claim, dict) or claim.get("status") != "granted":
            continue
        if (
            claim.get("standing_class") != "trunk"
            and not _valid_staged_revalidation(claim, global_epoch)
        ):
            return False
        claim_id = claim.get("id")
        source_dispatch = claim.get("source_dispatch")
        if not isinstance(claim_id, str) or not isinstance(source_dispatch, str):
            return False
        dispatch_path = (
            f"trees/{component}/nodes/{node_id}/dispatches/{source_dispatch}.json"
        )
        dispatch_text = _candidate_text(root, dispatch_path, staged_paths)
        if dispatch_text is None:
            return False
        try:
            dispatch = json.loads(dispatch_text)
        except json.JSONDecodeError:
            return False
        if not isinstance(dispatch, dict):
            return False
        issue_id = dispatch.get("issue_ref")
        if issue_id is not None:
            if not isinstance(issue_id, str) or re.fullmatch(r"I-[0-9]+", issue_id) is None:
                return False
            issue_refs.add(f"issue#{issue_id}")
        claim_ref = f"tree#{component}/claim#{claim_id}"
        matches: list[str] = []
        for ref in dispatch.get("adjudications", []):
            header = _adjudication_header_at(root, ref, staged_paths)
            if (
                header is not None
                and header.get("claim_ref") == claim_ref
                and header.get("verdict") in {"granted", "demoted"}
                and header.get("node_ref") == f"tree#{component}/node#{node_id}"
                and _staged_adjudication_coherent(
                    ref, header, component, node_id, dispatch, claim
                )
            ):
                matches.append(ref)
        if len(matches) != 1:
            return False
        canonical_claim_refs.add(claim_ref)
        coherent_adjudication_refs.add(matches[0])
        current.append(
            {
                "ref": claim_ref,
                "sha256": canonical_json_sha256(claim),
                "adjudication_ref": matches[0],
            }
        )
    current.sort(key=lambda item: item["ref"])
    if current != expected:
        return False
    if len(issue_refs) > 1:
        return False
    lane_header = _adjudication_header_at(root, lane_ref, staged_paths)
    return bool(
        lane_header is not None
        and lane_ref in coherent_adjudication_refs
        and lane_header.get("node_ref") == f"tree#{component}/node#{node_id}"
        and lane_header.get("verdict") in {"granted", "demoted"}
        and lane_header.get("claim_ref") in canonical_claim_refs
    )


def _head_global_epoch(root: Root) -> int | None:
    """Return max committed tree-history epoch, or None on malformed state."""
    listed = gitutil.run(
        root.path,
        ["ls-tree", "-r", "--name-only", "HEAD", "--", "trees"],
        check=False,
    )
    if listed.returncode != 0:
        return None
    epochs = [0]
    for tree_path in sorted(
        path
        for path in listed.stdout.splitlines()
        if re.fullmatch(r"trees/[^/]+/tree\.json", path)
    ):
        tree = _json_at(root, f"HEAD:{tree_path}")
        if tree is None:
            return None
        for row in tree.get("epoch_history", []):
            epoch = row.get("epoch") if isinstance(row, dict) else None
            if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
                return None
            epochs.append(epoch)
    return max(epochs)


def _is_staged_merge_consumption(
    root: Root,
    role: str | None,
    record_path: str,
    record: dict,
    staged_paths: set[str],
    consumption_paths: set[str],
) -> bool:
    """Prove consumed_epoch is derived by this staged node-merge cohort (Q4).

    The proof uses HEAD and index content, not a caller-supplied capability.  It
    binds the record stamp to the canonical candidate, exact-land verdict, node
    transition, merging-tree epoch/history/decision append, cursor removal, and
    the next union-global epoch in the same director-authored commit.
    """
    if role != "director":
        return False
    if consumption_paths != {record_path}:
        return False
    record_id = record.get("id")
    if (
        not isinstance(record_id, str)
        or re.fullmatch(r"MR-[0-9]+", record_id) is None
        or record_path != f"tier1/merge-records/{record_id}.json"
    ):
        return False
    epoch = record.get("consumed_epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        return False
    gate = record.get("gate_verdict")
    if not isinstance(gate, dict) or gate.get("verdict") != "land":
        return False
    candidate = record.get("candidate_ref")
    match = _CANDIDATE_REF.fullmatch(candidate) if isinstance(candidate, str) else None
    if match is None:
        return False
    component = match.group("component")
    node_id = match.group("node")
    node_path = f"trees/{component}/nodes/{node_id}/node.json"
    tree_path = f"trees/{component}/tree.json"
    if node_path not in staged_paths or tree_path not in staged_paths:
        return False

    # One mutexed merge plan consumes one record, transitions one source node,
    # and advances one source tree.  Reject a hand-composed multi-merge commit
    # even if each local fragment looks individually coherent (Q7).
    changed_node_paths: set[str] = set()
    merged_node_paths: set[str] = set()
    changed_tree_paths: set[str] = set()
    advanced_tree_paths: set[str] = set()
    for staged_path in staged_paths:
        staged_type = classify.doc_type(staged_path)
        if staged_type not in {"node", "tree"}:
            continue
        old_doc = _json_at(root, f"HEAD:{staged_path}")
        new_doc = _json_at(root, f":{staged_path}")
        if staged_type == "node" and old_doc != new_doc:
            # Additions and deletions count too.  The real merge plan touches
            # exactly its source node and never smuggles a new/deleted sibling.
            changed_node_paths.add(staged_path)
        if staged_type == "tree" and old_doc != new_doc:
            changed_tree_paths.add(staged_path)
        if old_doc is None or new_doc is None:
            continue
        if (
            staged_type == "node"
            and old_doc.get("status") == "worked"
            and new_doc.get("status") == "merged"
        ):
            merged_node_paths.add(staged_path)
        if (
            staged_type == "tree"
            and old_doc.get("epoch_history") != new_doc.get("epoch_history")
        ):
            advanced_tree_paths.add(staged_path)
    if (
        changed_node_paths != {node_path}
        or merged_node_paths != {node_path}
        or advanced_tree_paths != {tree_path}
    ):
        return False

    old_node = _json_at(root, f"HEAD:{node_path}")
    new_node = _json_at(root, f":{node_path}")
    old_tree = _json_at(root, f"HEAD:{tree_path}")
    new_tree = _json_at(root, f":{tree_path}")
    if None in (old_node, new_node, old_tree, new_tree):
        return False
    assert old_node is not None and new_node is not None
    assert old_tree is not None and new_tree is not None
    if (
        old_node.get("id") != node_id
        or new_node.get("id") != node_id
        or old_tree.get("component") != component
        or new_tree.get("component") != component
        or old_node.get("status") != "worked"
        or new_node.get("status") != "merged"
        or set(authority.changed_fields(old_node, new_node)) != {"status"}
    ):
        return False
    granted_claims = [
        claim
        for claim in new_node.get("claims", [])
        if isinstance(claim, dict) and claim.get("status") == "granted"
    ]
    if not granted_claims:
        return False
    if any(
        claim.get("standing_class") != "trunk"
        and claim.get("revalidation") is None
        for claim in granted_claims
    ):
        return False
    if not _staged_backing_snapshot_matches(
        root, record, component, node_id, new_node, staged_paths
    ):
        return False

    # Retain the B4 §9 sibling merge-block in the hook replay.  A forged
    # mechanical seam must not bypass semantic prerequisites enforced by CLI.
    listed_nodes = gitutil.run(
        root.path,
        [
            "ls-tree",
            "-r",
            "--name-only",
            "HEAD",
            "--",
            f"trees/{component}/nodes",
        ],
        check=False,
    )
    if listed_nodes.returncode != 0:
        return False
    for sibling_path in listed_nodes.stdout.splitlines():
        if sibling_path == node_path or classify.doc_type(sibling_path) != "node":
            continue
        sibling = _json_at(root, f"HEAD:{sibling_path}")
        if sibling is None or sibling.get("parent") != old_node.get("parent"):
            continue
        if any(
            isinstance(conflict, dict) and conflict.get("settlement") == "pending"
            for conflict in sibling.get("conflicts", [])
        ):
            return False

    allowed_tree_changes = {"epoch", "epoch_history", "cursor", "decision_log"}
    tree_changes = set(authority.changed_fields(old_tree, new_tree))
    if not {"epoch", "epoch_history", "decision_log"} <= tree_changes:
        return False
    if not tree_changes <= allowed_tree_changes or new_tree.get("epoch") != epoch:
        return False

    old_history = old_tree.get("epoch_history")
    new_history = new_tree.get("epoch_history")
    if not isinstance(old_history, list) or not isinstance(new_history, list):
        return False
    if len(new_history) != len(old_history) + 1 or new_history[:-1] != old_history:
        return False
    history_row = new_history[-1]
    if (
        not isinstance(history_row, dict)
        or history_row.get("epoch") != epoch
        or history_row.get("merged_node") != node_id
        or history_row.get("user_ratified") != "n/a"
        or not isinstance(history_row.get("date"), str)
    ):
        return False

    old_decisions = old_tree.get("decision_log")
    new_decisions = new_tree.get("decision_log")
    if not isinstance(old_decisions, list) or not isinstance(new_decisions, list):
        return False
    if len(new_decisions) != len(old_decisions) + 1 or new_decisions[:-1] != old_decisions:
        return False
    decision = new_decisions[-1]
    if (
        not isinstance(decision, dict)
        or decision.get("move") != "merge"
        or decision.get("target") != node_id
        or decision.get("epoch") != epoch
    ):
        return False

    old_cursor = old_tree.get("cursor")
    expected_cursor = [
        item
        for item in old_cursor
        if isinstance(item, dict) and item.get("node") != node_id
    ] if isinstance(old_cursor, list) else None
    if expected_cursor is None or new_tree.get("cursor") != expected_cursor:
        return False

    global_epoch = _head_global_epoch(root)
    if global_epoch is None or epoch != global_epoch + 1:
        return False

    # Q6/Q7 settlement seam: every other committed tree receives exactly one
    # queued staleness-assessment row against this merge epoch in the same plan.
    listed_trees = gitutil.run(
        root.path,
        ["ls-tree", "-r", "--name-only", "HEAD", "--", "trees"],
        check=False,
    )
    if listed_trees.returncode != 0:
        return False
    committed_tree_paths = {
        path
        for path in listed_trees.stdout.splitlines()
        if re.fullmatch(r"trees/[^/]+/tree\.json", path)
    }
    if changed_tree_paths != committed_tree_paths:
        return False
    for other_tree_path in sorted(committed_tree_paths - {tree_path}):
        if other_tree_path not in staged_paths:
            return False
        old_other = _json_at(root, f"HEAD:{other_tree_path}")
        new_other = _json_at(root, f":{other_tree_path}")
        if old_other is None or new_other is None:
            return False
        if set(authority.changed_fields(old_other, new_other)) != {"watch_queue"}:
            return False
        old_queue = old_other.get("watch_queue")
        new_queue = new_other.get("watch_queue")
        if not isinstance(old_queue, list) or not isinstance(new_queue, list):
            return False
        if len(new_queue) != len(old_queue) + 1 or new_queue[:-1] != old_queue:
            return False
        ordinals = []
        for row in old_queue:
            watch_match = (
                re.fullmatch(r"W-([0-9]+)", str(row.get("id", "")))
                if isinstance(row, dict)
                else None
            )
            if watch_match is not None:
                ordinals.append(int(watch_match.group(1)))
        expected_row = {
            "id": f"W-{max(ordinals, default=0) + 1}",
            "merged_node": candidate,
            "prediction_claim": None,
            "observed": None,
            "verdict": None,
            "severity": None,
            "status": "queued",
            "kind": "staleness-assessment",
            "epoch": epoch,
        }
        if new_queue[-1] != expected_row:
            return False
    return True


def _staged_consumption_paths(root: Root, staged_paths: set[str]) -> set[str]:
    """Find null→non-null consumed_epoch writes in the candidate commit."""
    paths: set[str] = set()
    for path in staged_paths:
        if classify.doc_type(path) != "merge_record":
            continue
        old = _json_at(root, f"HEAD:{path}")
        new = _json_at(root, f":{path}")
        if (
            old is not None
            and new is not None
            and old.get("consumed_epoch") is None
            and new.get("consumed_epoch") is not None
        ):
            paths.add(path)
    return paths


def run() -> int:
    root = _resolve_root()
    ht_commit = os.environ.get("HT_COMMIT")
    role = os.environ.get("HT_ROLE")

    try:
        staged = gitutil.staged_name_status(root.path)
        phase = _phase_mode(root, staged)
    except HtError as exc:  # pragma: no cover
        return _reject(str(exc))

    lane = _verifier_lane(role)
    staged_paths = {path for _code, path in staged}
    consumption_paths = _staged_consumption_paths(root, staged_paths)

    for code, path in staged:
        dt = classify.doc_type(path)

        if dt in {"ledger_entry", "ledger_union_index"} and code in ("D", "R"):
            label = "ledger entry" if dt == "ledger_entry" else "generated ledger union index"
            return _reject(
                f"'{path}' removes or renames a {label} — ledger records retire or "
                "merge by status and the generated union view is regenerated in "
                "place; neither path class may vanish (A1 §7 / item 1 W3-W5)"
            )
        if (
            dt == "ledger_entry"
            and code == "A"
            and _path_exists_in_history(root, path)
        ):
            return _reject(
                f"'{path}' re-creates a previously recorded ledger path — global "
                "ledger identity/provenance is immutable and may not be laundered "
                "through deletion and re-creation (macro §5 / D4)"
            )

        if dt in _TIER1_RECORD_TYPES and code in ("D", "R"):
            return _reject(
                f"'{path}' removes or renames a Tier-1 record "
                f"({_TIER1_RECORD_LABELS[dt]}) — phase, issue, "
                f"decision-log, ratification-queue, merge-record, and gate-review documents "
                f"are append-only records and may not vanish (A1 §10)"
            )
        if (
            dt in _TIER1_RECORD_TYPES
            and code == "A"
            and _path_exists_in_history(root, path)
        ):
            return _reject(
                f"'{path}' re-creates a previously recorded Tier-1 path — Tier-1 "
                f"identity and creation stamps are immutable; records may not be "
                f"laundered through deletion and re-creation (A1 §10 / D4)"
            )

        if dt == "pc_decision" and code != "A":
            return _reject(
                f"'{path}' modifies an existing PC decision — decision-log records "
                f"are append-only (A1 §10)"
            )
        if dt == "gate_review" and code != "A":
            return _reject(
                f"'{path}' modifies an existing gate review — gate-review records "
                f"are frozen after creation (A1 §10)"
            )
        if dt == "interrupt" and code != "A":
            return _reject(
                f"'{path}' modifies an existing interrupt — interrupt records "
                f"are frozen after creation (F11; A1 §10)"
            )
        if dt == "ratification_item" and code not in ("A", "M"):
            return _reject(
                f"'{path}' removes or renames a ratification queue item — records are "
                f"append-only (A1 §10)"
            )

        # write-once archive guard (U2)
        if classify.is_archive(path):
            return _reject(
                f"'{path}' is under nodes/*/archive/ — archives are write-once and "
                f"git-ignored; no staged archive paths (U2)"
            )

        # Submitted reports are write-once path identities: only a fresh regular
        # blob addition is legal. Deletion/rename/type replacement and historical
        # recreation are as mutating as an in-place edit.
        if classify.is_report(path):
            if code != "A":
                return _reject(
                    f"'{path}' modifies, removes, renames, or replaces a submitted "
                    "report — reports are frozen at submit (U2 / B4 §9)"
                )
            if _path_exists_in_history(root, path):
                return _reject(
                    f"'{path}' re-creates a historical submitted-report path — "
                    "report identity is write-once"
                )
            if not _staged_regular_blob(root, path):
                return _reject(
                    f"'{path}' new submitted report must be a 100644 regular blob"
                )

        # adjudication records = the verdict history the bounce loop escalates with
        # (B4 §5). Tamper-evident, same argument as report freeze: each record is its
        # own numbered file (-a1, -a2, ...), so additions are fine but MODIFICATIONS
        # of an existing record are rejected (write-once), and the whole path class
        # is verifier-authored — a commit touching it must carry HT_ROLE=verifier.
        if classify.is_adjudication(path):
            if code != "A":
                return _reject(
                    f"'{path}' modifies, removes, renames, or replaces an existing "
                    "adjudication record — records are "
                    f"write-once verdict history (U2 / B4 §5)"
                )
            if _path_exists_in_history(root, path):
                return _reject(
                    f"'{path}' re-creates a historical adjudication path — verdict "
                    "history identity is write-once"
                )
            if not _staged_regular_blob(root, path):
                return _reject(
                    f"'{path}' new adjudication must be a 100644 regular blob"
                )
            if role != "verifier":
                return _reject(
                    f"'{path}' adjudication record must be verifier-authored "
                    f"(HT_ROLE=verifier, got '{role or 'unset'}') (B4 §5)"
                )
            # verifier addition of a new numbered record: allowed (still HT_COMMIT-gated below)

        if classify.is_observatory_report(path):
            if code != "A":
                return _reject(
                    f"'{path}' modifies, removes, renames, or replaces a registered observatory "
                    "report card — canonical report cards are write-once (Wave A)"
                )
            if code == "A" and _path_exists_in_history(root, path):
                return _reject(
                    f"'{path}' re-creates a historical observatory report-card path — "
                    "artifact identity may not be laundered through deletion/recreation"
                )
            if not _staged_regular_blob(root, path):
                return _reject(
                    f"'{path}' new observatory report card must be a 100644 regular blob"
                )
            if role != "harness":
                return _reject(
                    f"'{path}' observatory report registration must be harness-authored "
                    f"(HT_ROLE=harness, got '{role or 'unset'}')"
                )

        if not classify.in_state_lane(path):
            continue  # code / methodology-lane changes are ordinary development

        # out-of-band guard
        if not ht_commit:
            return _reject(
                f"out-of-band state write to '{path}' — use ht (HT_COMMIT unset) (A2 §4.2)"
            )

        if code == "D":
            continue  # deletions are HT_COMMIT-gated; append-only types rejected above

        if path in _EMPTY_SCAFFOLD_MARKERS:
            staged_entry = gitutil.run(
                root.path,
                ["ls-files", "--stage", "-z", "--", path],
                check=False,
            )
            rows = [row for row in staged_entry.stdout.split("\x00") if row]
            header = rows[0].partition("\t")[0] if len(rows) == 1 else ""
            fields = header.split()
            if (
                staged_entry.returncode != 0
                or len(rows) != 1
                or len(fields) != 3
                or fields[2] != "0"
            ):
                return _reject(
                    f"could not inspect staged scaffold marker '{path}' mode/type "
                    "([R-i7-11] D4+D8)"
                )
            mode = fields[0]
            staged_type = gitutil.run(
                root.path,
                ["cat-file", "-t", f":{path}"],
                check=False,
            )
            object_type = staged_type.stdout.strip()
            if staged_type.returncode != 0 or mode != "100644" or object_type != "blob":
                return _reject(
                    f"'{path}' is a sanctioned scaffold marker and its staged "
                    f"mode/type must be 100644 blob, got {mode or 'unknown'} "
                    f"{object_type or 'unknown'} ([R-i7-11] D4+D8)"
                )
            blob_size = gitutil.run(
                root.path,
                ["cat-file", "-s", f":{path}"],
                check=False,
            )
            if blob_size.returncode != 0:
                if code != "R":
                    return _reject(
                        f"could not inspect staged scaffold marker '{path}' ([R-i7-7])"
                    )
            else:
                try:
                    size = int(blob_size.stdout.strip())
                except ValueError:
                    return _reject(
                        f"could not inspect staged scaffold marker '{path}' ([R-i7-7])"
                    )
                if size != 0:
                    return _reject(
                        f"'{path}' is a sanctioned scaffold marker and its staged "
                        f"blob must be empty bytes, got {size} bytes ([R-i7-7])"
                    )

        if dt is None:
            continue  # e.g. .gitkeep, report .md addition, readout placeholder

        # schema-validate STAGED content
        staged_text = gitutil.show(root.path, f":{path}")
        if staged_text is None:
            return _reject(f"could not read staged content for '{path}' (A2 §4.2)")
        try:
            staged_doc = json.loads(staged_text)
        except json.JSONDecodeError as exc:
            return _reject(f"'{path}' is not valid JSON: {exc} (A2 §4.2)")
        if dt == "ledger_entry":
            expected_id = Path(path).stem
            if staged_doc.get("id") != expected_id:
                return _reject(
                    f"'{path}' ledger id {staged_doc.get('id')!r} does not match "
                    f"filename {expected_id!r} (macro §5 global namespace)"
                )
            for other_path in _indexed_ledger_paths(root):
                if other_path == path:
                    continue
                other_text = gitutil.show(root.path, f":{other_path}")
                if other_text is None:
                    continue
                try:
                    other_doc = json.loads(other_text)
                except json.JSONDecodeError:
                    continue  # its own staged validation reports the malformed file
                if other_doc.get("id") == expected_id:
                    return _reject(
                        f"'{path}' duplicates union-global ledger id {expected_id!r} "
                        f"already present at '{other_path}' (macro §5)"
                    )
        if dt == "gate_review":
            expected_id = Path(path).stem
            if staged_doc.get("id") != expected_id:
                return _reject(
                    f"'{path}' union-global gate-review id {staged_doc.get('id')!r} "
                    f"does not match filename {expected_id!r} (item 7 W3/W4)"
                )
            for other_path in _indexed_gate_review_paths(root):
                if other_path == path:
                    continue
                other_text = gitutil.show(root.path, f":{other_path}")
                if other_text is None:
                    continue
                try:
                    other_doc = json.loads(other_text)
                except json.JSONDecodeError:
                    continue
                if other_doc.get("id") == expected_id:
                    return _reject(
                        f"'{path}' duplicates union-global gate-review id "
                        f"{expected_id!r} already present at '{other_path}' "
                        f"(item 7 W3/W4)"
                    )
        try:
            schemas.validate(root.schemas_dir, dt, staged_doc)
        except HtError as exc:
            return _reject(f"{path}: {exc.message}")

        # role x field authority against HEAD:path
        if not role:
            return _reject(f"HT_ROLE unset for staged state write '{path}' (A1 §10)")
        old_doc = None
        if code != "A":
            head_text = gitutil.show(root.path, f"HEAD:{path}")
            if head_text is not None:
                try:
                    old_doc = json.loads(head_text)
                except json.JSONDecodeError:
                    old_doc = None
        try:
            mechanical_fields = frozenset()
            if dt == "merge_record" and _is_staged_merge_consumption(
                root,
                role,
                path,
                staged_doc,
                staged_paths,
                consumption_paths,
            ):
                mechanical_fields = frozenset({"consumed_epoch"})
            authority.check_write(
                dt, old_doc, staged_doc, role,
                ledger_section=classify.ledger_section(path),
                ledger_book=classify.ledger_book(path),
                phase=phase,
                lane=lane,
                mechanical_fields=mechanical_fields,
            )
        except HtError as exc:
            return _reject(f"{path}: {exc.message}")

    return 0
