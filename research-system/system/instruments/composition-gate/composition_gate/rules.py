"""Pure CG-P4 decisions over a committed, classifier-validated W2 screen.

This layer is deterministic and read-only.  The shared classifier alone decides
whether screen evidence is invalid; malformed or ambiguous preset-only fields
merely prevent that preset from firing and leave a valid semantic failure on the
stage-2 route.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
import re
from typing import Any, Mapping

from .classification import ALLGREEN, INVALID, SEMANTIC_FAILURE, classify_screen
from .normalization import comparison_key, stable_path_key, stable_text_key
from .screen import CHECK_NAMES


_RECORD_ID = re.compile(r"MR-([0-9]+)")


def _decision(
    route: str,
    verdict: str | None,
    note: str,
    rules_fired: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "route": route,
        "verdict": verdict,
        "note": note,
        "rules_fired": rules_fired,
    }


def _failure_rows(record: dict) -> tuple[str, dict] | None:
    screen = record.get("screen")
    results = screen.get("results") if isinstance(screen, dict) else None
    if not isinstance(results, list) or len(results) != len(CHECK_NAMES):
        return None
    if any(not isinstance(row, dict) for row in results):
        return None
    if [row.get("check") for row in results] != list(CHECK_NAMES):
        return None
    if any(row.get("result") not in {"pass", "fail", "n/a"} for row in results):
        return None
    failed = [row for row in results if row["result"] == "fail"]
    if len(failed) != 1 or not isinstance(failed[0].get("inputs"), dict):
        return None
    return failed[0]["check"], failed[0]["inputs"]


def _record_ordinal(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = _RECORD_ID.fullmatch(value)
    return int(match.group(1)) if match is not None else None


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        return None
    return value


def _pending_scope_is_disjoint(candidate: Any, pending: Any) -> bool:
    if not isinstance(candidate, dict) or not isinstance(pending, dict):
        return False
    candidate_scope = candidate.get("scope")
    pending_scope = pending.get("scope")
    if not isinstance(candidate_scope, dict) or not isinstance(pending_scope, dict):
        return False
    candidate_surfaces = _string_list(candidate_scope.get("surfaces"))
    pending_surfaces = _string_list(pending_scope.get("surfaces"))
    candidate_globs = _string_list(candidate_scope.get("globs"))
    pending_globs = _string_list(pending_scope.get("globs"))
    if None in (candidate_surfaces, pending_surfaces, candidate_globs, pending_globs):
        return False

    pending_surface_keys = {comparison_key(item) for item in pending_surfaces}
    if any(comparison_key(item) in pending_surface_keys for item in candidate_surfaces):
        return False
    for candidate_glob in candidate_globs:
        candidate_key = comparison_key(candidate_glob)
        for pending_glob in pending_globs:
            pending_key = comparison_key(pending_glob)
            if (
                candidate_key == pending_key
                or fnmatch.fnmatchcase(candidate_key, pending_key)
                or fnmatch.fnmatchcase(pending_key, candidate_key)
            ):
                return False
    return True


def _overlap_sequence(inputs: dict) -> tuple[str, str] | None:
    collisions = inputs.get("collisions")
    comparisons = inputs.get("comparisons")
    candidate = inputs.get("candidate")
    if (
        not isinstance(collisions, list)
        or not collisions
        or not isinstance(comparisons, list)
        or not isinstance(candidate, dict)
    ):
        return None

    comparison_rows: dict[str, list[dict]] = {}
    for row in comparisons:
        if not isinstance(row, dict):
            return None
        record = row.get("record")
        record_id = record.get("record_id") if isinstance(record, dict) else None
        if _record_ordinal(record_id) is None:
            return None
        comparison_rows.setdefault(record_id, []).append(row)

        sets = _string_list(row.get("comparison_sets"))
        if sets is None:
            return None
        if "pending" in sets and not _pending_scope_is_disjoint(candidate, record):
            return None

    joined: list[tuple[int, int, str]] = []
    seen_collisions: set[str] = set()
    for collision in collisions:
        if not isinstance(collision, dict):
            return None
        record_id = collision.get("record_id")
        ordinal = _record_ordinal(record_id)
        axes = _string_list(collision.get("axes"))
        if (
            ordinal is None
            or record_id in seen_collisions
            or axes is None
            or not axes
            or "surfaces" in axes
            or "globs" in axes
        ):
            return None
        seen_collisions.add(record_id)
        matches = comparison_rows.get(record_id, [])
        if len(matches) != 1:
            return None
        match = matches[0]
        if match.get("comparison_sets") != ["last-consumed"]:
            return None
        record = match.get("record")
        epoch = record.get("consumed_epoch") if isinstance(record, dict) else None
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
            return None
        joined.append((epoch, ordinal, record_id))

    oldest = min(joined)[2]
    outcome = f"land-after-{oldest}"
    note = (
        f"Stage-2 binding preset: {outcome}; {oldest} is the oldest colliding "
        "last-consumed merge record."
    )
    return outcome, note


def _settlement_hold(inputs: dict) -> tuple[str, str] | None:
    overdue = inputs.get("overdue_rows")
    if not isinstance(overdue, list) or not overdue:
        return None
    refs: list[tuple[tuple[str, str], str]] = []
    seen: set[str] = set()
    for row in overdue:
        if not isinstance(row, dict):
            return None
        tree_path = row.get("tree_path")
        row_id = row.get("row_id")
        if (
            not isinstance(tree_path, str)
            or not tree_path
            or not isinstance(row_id, str)
            or not row_id
        ):
            return None
        ref = f"{tree_path}#{row_id}"
        if ref in seen:
            return None
        seen.add(ref)
        refs.append(((stable_path_key(tree_path), stable_text_key(row_id)), ref))
    ordered = [ref for _, ref in sorted(refs)]
    note = (
        "Stage-2 binding preset: hold until queued staleness assessments are "
        f"recorded: {', '.join(ordered)}."
    )
    return "hold", note


def _consolidate(inputs: dict) -> tuple[str, str] | None:
    pending_count = inputs.get("pending_count")
    pending_records = inputs.get("pending_records")
    if (
        not isinstance(pending_count, int)
        or isinstance(pending_count, bool)
        or pending_count < 3
        or not isinstance(pending_records, list)
        or len(pending_records) != pending_count
    ):
        return None
    ids: list[tuple[int, str]] = []
    seen: set[str] = set()
    for record in pending_records:
        record_id = record.get("record_id") if isinstance(record, dict) else None
        ordinal = _record_ordinal(record_id)
        if ordinal is None or record_id in seen:
            return None
        seen.add(record_id)
        ids.append((ordinal, record_id))
    ordered = [record_id for _, record_id in sorted(ids)]
    note = (
        "Stage-2 binding preset: consolidate-first for pending merge records: "
        f"{', '.join(ordered)}."
    )
    return "consolidate-first", note


def evaluate_rules(
    root: str | Path,
    record: dict,
    *,
    evidence_bytes: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    """Return exactly route/verdict/note/rules_fired for one committed MR."""

    classification = (
        classify_screen(root, record)
        if evidence_bytes is None
        else classify_screen(root, record, evidence_bytes=evidence_bytes)
    )
    if classification == INVALID:
        return _decision(
            "stuck",
            "escalate-stuck",
            "Committed W2 screen evidence is invalid or cannot be revalidated.",
            [{"rule_id": "R-SCREEN-INVALID", "outcome": "escalate-stuck"}],
        )
    if classification == ALLGREEN:
        return _decision(
            "auto",
            "land",
            "All five checks passed or returned legitimate n/a.",
            [{"rule_id": "R-ALLGREEN", "outcome": "land"}],
        )
    if classification != SEMANTIC_FAILURE:
        return _decision(
            "stuck",
            "escalate-stuck",
            "Committed W2 screen evidence is invalid or cannot be revalidated.",
            [{"rule_id": "R-SCREEN-INVALID", "outcome": "escalate-stuck"}],
        )

    failed = _failure_rows(record)
    if failed is not None:
        check, inputs = failed
        preset: tuple[str, str] | None = None
        rule_id: str | None = None
        if check == "scope-overlap":
            preset = _overlap_sequence(inputs)
            rule_id = "R-OVERLAP-SEQ"
        elif check == "settlement-completeness":
            preset = _settlement_hold(inputs)
            rule_id = "R-SETTLE-HOLD"
        elif check == "queue-adjacency":
            preset = _consolidate(inputs)
            rule_id = "R-CONSOLIDATE"
        if preset is not None and rule_id is not None:
            outcome, note = preset
            return _decision(
                "stage2",
                None,
                note,
                [{"rule_id": rule_id, "outcome": outcome}],
            )

    return _decision(
        "stage2",
        None,
        "Valid semantic failure requires stage-2 review.",
        [],
    )


__all__ = ["evaluate_rules"]
