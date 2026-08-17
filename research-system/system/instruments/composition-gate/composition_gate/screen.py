"""Deterministic stage-1 composition screen over committed research state."""

from __future__ import annotations

from copy import deepcopy
import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Callable

from . import ENGINE_VERSION
from .config import DEFAULT_CONFIG_PATH, ScreenConfig, load_config
from .discovery import (
    Discovery,
    GitSnapshot,
    MERGE_RECORD_STORE,
    ScreenInputError,
    TREE_STORE,
    failed_snapshot_citations,
)
from .normalization import comparison_key, stable_path_key, stable_text_key


CHECK_NAMES = (
    "scope-overlap",
    "surface-budget",
    "settlement-completeness",
    "queue-adjacency",
    "watch-debt",
)
def _shared_scope_values(left: list[str], right: list[str]) -> list[str]:
    right_keys = {comparison_key(value) for value in right}
    matched: dict[str, str] = {}
    for value in left:
        key = comparison_key(value)
        if key in right_keys and key not in matched:
            matched[key] = value
    return sorted(
        matched.values(),
        key=stable_text_key,
    )


class _CommittedState:
    def __init__(self, discovery: Discovery):
        self.discovery = discovery

    def merge_records(self) -> list[tuple[str, dict]]:
        return [
            (document.path, document.value)
            for document in self.discovery.merge_records()
        ]

    def candidate(self, record_id: str) -> tuple[str, dict]:
        path = f"tier1/merge-records/{record_id}.json"
        for candidate_path, record in self.merge_records():
            if candidate_path == path:
                return candidate_path, record
        raise ScreenInputError(f"missing committed input {path}")

    def trees(self) -> list[tuple[str, dict]]:
        return [
            (document.path, document.value)
            for document in self.discovery.trees()
        ]


def _required_string(document: dict, field: str, path: str) -> str:
    if field not in document:
        raise ScreenInputError(f"missing field {field} in {path}")
    value = document[field]
    if not isinstance(value, str) or not value:
        raise ScreenInputError(f"field {field} in {path} must be a non-empty string")
    return value


def _record_id(record: dict, path: str) -> str:
    return _required_string(record, "id", path)


def _candidate_ref(record: dict, path: str) -> str:
    return _required_string(record, "candidate_ref", path)


def _consumed_epoch(record: dict, path: str) -> int | None:
    if "consumed_epoch" not in record:
        raise ScreenInputError(f"missing field consumed_epoch in {path}")
    value = record["consumed_epoch"]
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or value < 0
    ):
        raise ScreenInputError(f"field consumed_epoch in {path} must be null or non-negative")
    return value


def _record_scope(record: dict, path: str) -> dict:
    if "scope" not in record:
        raise ScreenInputError(f"missing field scope in {path}")
    scope = record["scope"]
    if not isinstance(scope, dict):
        raise ScreenInputError(f"field scope in {path} must be an object")
    if set(scope) != {"lane", "seats", "surfaces", "globs"}:
        raise ScreenInputError(
            f"scope in {path} requires exactly lane, seats, surfaces, and globs"
        )
    lane = scope["lane"]
    if not isinstance(lane, str) or not lane:
        raise ScreenInputError(f"scope.lane in {path} must be a non-empty string")
    normalized = {"lane": lane}
    for field in ("seats", "surfaces", "globs"):
        values = scope[field]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value for value in values
        ):
            raise ScreenInputError(
                f"scope.{field} in {path} must be an array of non-empty strings"
            )
        normalized[field] = list(values)
    return normalized


def _gate_verdict(record: dict, path: str) -> dict | None:
    if "gate_verdict" not in record:
        raise ScreenInputError(f"missing field gate_verdict in {path}")
    value = record["gate_verdict"]
    if value is not None and not isinstance(value, dict):
        raise ScreenInputError(f"field gate_verdict in {path} must be null or an object")
    return value


def _is_pending(record: dict, path: str) -> bool:
    gate = _gate_verdict(record, path)
    epoch = _consumed_epoch(record, path)
    if gate is None:
        return True
    verdict = gate.get("verdict")
    if not isinstance(verdict, str) or not verdict:
        raise ScreenInputError(f"gate_verdict.verdict in {path} must be non-empty")
    return verdict == "land" and epoch is None


def _record_input(path: str, record: dict) -> dict:
    return {
        "record_id": _record_id(record, path),
        "path": path,
        "candidate_ref": _candidate_ref(record, path),
        "consumed_epoch": _consumed_epoch(record, path),
        "gate_verdict": deepcopy(_gate_verdict(record, path)),
        "scope": _record_scope(record, path),
    }


def _glob_matches(left: list[str], right: list[str]) -> list[dict]:
    matches: list[dict] = []
    for candidate_glob in sorted(left, key=stable_text_key):
        for other_glob in sorted(right, key=stable_text_key):
            candidate_key = comparison_key(candidate_glob)
            other_key = comparison_key(other_glob)
            if candidate_key == other_key:
                mode = "literal"
            elif fnmatch.fnmatchcase(candidate_key, other_key):
                mode = "candidate-matched-by-other"
            elif fnmatch.fnmatchcase(other_key, candidate_key):
                mode = "other-matched-by-candidate"
            else:
                continue
            matches.append(
                {
                    "candidate_glob": candidate_glob,
                    "other_glob": other_glob,
                    "mode": mode,
                }
            )
    return matches


def _scope_collision(candidate: dict, other: dict) -> dict:
    axes: list[str] = []
    values: dict[str, object] = {}
    if comparison_key(candidate["lane"]) == comparison_key(other["lane"]):
        axes.append("lane")
        values["lane"] = candidate["lane"]
    for field in ("seats", "surfaces"):
        shared = _shared_scope_values(candidate[field], other[field])
        if shared:
            axes.append(field)
            values[field] = shared
    glob_matches = _glob_matches(candidate["globs"], other["globs"])
    if glob_matches:
        axes.append("globs")
        values["globs"] = glob_matches
    return {"axes": axes, "values": values}


def _consumed_records(records: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    consumed = [
        (path, record)
        for path, record in records
        if _consumed_epoch(record, path) is not None
    ]
    return sorted(
        consumed,
        key=lambda item: (-_consumed_epoch(item[1], item[0]), _record_id(item[1], item[0])),
    )


def _pending_records(records: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    return sorted(
        [(path, record) for path, record in records if _is_pending(record, path)],
        key=lambda item: _record_id(item[1], item[0]),
    )


def _scope_overlap(
    state: _CommittedState, config: ScreenConfig, record_id: str
) -> tuple[str, str, dict]:
    candidate_path, candidate_record = state.candidate(record_id)
    candidate_scope = _record_scope(candidate_record, candidate_path)
    records = state.merge_records()
    k = config.threshold("k_last_merged")
    consumed_all = [
        item
        for item in _consumed_records(records)
        if _record_id(item[1], item[0]) != record_id
    ]
    consumed_window = consumed_all[:k]
    pending = [
        item
        for item in _pending_records(records)
        if _record_id(item[1], item[0]) != record_id
    ]
    compared: dict[str, tuple[str, dict, list[str]]] = {}
    for path, record in consumed_window:
        compared[_record_id(record, path)] = (path, record, ["last-consumed"])
    for path, record in pending:
        other_id = _record_id(record, path)
        if other_id in compared:
            compared[other_id][2].append("pending")
        else:
            compared[other_id] = (path, record, ["pending"])

    comparisons: list[dict] = []
    collisions: list[dict] = []
    for other_id in sorted(compared):
        path, record, sets = compared[other_id]
        collision = _scope_collision(candidate_scope, _record_scope(record, path))
        row = {
            "record": _record_input(path, record),
            "comparison_sets": sorted(sets),
            "collision": collision,
        }
        comparisons.append(row)
        if collision["axes"]:
            collisions.append(
                {"record_id": other_id, **collision}
            )
    if collisions:
        detail = "scope collisions: " + "; ".join(
            f"{row['record_id']} ({', '.join(row['axes'])})" for row in collisions
        )
        result = "fail"
    else:
        detail = "no scope overlap with the last consumed window or pending records"
        result = "pass"
    return result, detail, {
        "candidate": _record_input(candidate_path, candidate_record),
        "thresholds": {"k_last_merged": k},
        "consumed_inventory": [
            {
                "record_id": _record_id(record, path),
                "path": path,
                "consumed_epoch": _consumed_epoch(record, path),
                "selected": index < k,
            }
            for index, (path, record) in enumerate(consumed_all)
        ],
        "pending_record_ids": [_record_id(record, path) for path, record in pending],
        "comparisons": comparisons,
        "collisions": collisions,
    }


def _surface_budget(
    state: _CommittedState, config: ScreenConfig, record_id: str
) -> tuple[str, str, dict]:
    candidate_path, candidate_record = state.candidate(record_id)
    candidate_scope = _record_scope(candidate_record, candidate_path)
    maximum = config.threshold("surface_budget_max_cumulative_directives")
    diff_maximum = config.threshold("surface_budget_max_diff_lines")
    base_inputs = {
        "candidate": _record_input(candidate_path, candidate_record),
        "thresholds": {
            "surface_budget_max_cumulative_directives": maximum,
            "surface_budget_max_diff_lines": diff_maximum,
        },
        "not_computable": {
            "diff_lines": "merge-record v1 does not carry candidate diff size"
        },
    }
    surfaces = candidate_scope["surfaces"]
    if not surfaces:
        return "n/a", "no actor-visible surfaces declared", {
            **base_inputs,
            "surface_counts": {},
        }

    records = state.merge_records()
    consumed = [
        item
        for item in _consumed_records(records)
        if _record_id(item[1], item[0]) != record_id
    ]
    counts: dict[str, dict] = {}
    for surface in sorted(surfaces, key=stable_text_key):
        touching = [
            _record_input(path, record)
            for path, record in consumed
            if comparison_key(surface)
            in {
                comparison_key(value)
                for value in _record_scope(record, path)["surfaces"]
            }
        ]
        counts[surface] = {"count": len(touching), "records": touching}
    exceeded = [surface for surface, row in counts.items() if row["count"] > maximum]
    if exceeded:
        result = "fail"
        detail = (
            "surface cumulative-directive budget exceeded: "
            + ", ".join(f"{surface}={counts[surface]['count']}" for surface in exceeded)
        )
    else:
        result = "pass"
        detail = "surface cumulative-directive counts are within the provisional budget"
    return result, detail, {**base_inputs, "surface_counts": counts}


def _queued_rows(state: _CommittedState) -> list[dict]:
    rows: list[dict] = []
    for path, tree in state.trees():
        queue = tree.get("watch_queue")
        if not isinstance(queue, list):
            raise ScreenInputError(f"watch_queue in {path} must be an array")
        for row in queue:
            if not isinstance(row, dict):
                raise ScreenInputError(f"watch_queue row in {path} must be an object")
            if row.get("kind") != "staleness-assessment" or row.get("status") != "queued":
                continue
            row_id = row.get("id")
            epoch = row.get("epoch")
            merged_node = row.get("merged_node")
            if not isinstance(row_id, str) or not row_id:
                raise ScreenInputError(f"queued watch row in {path} needs a non-empty id")
            if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
                raise ScreenInputError(f"queued watch row {row_id} in {path} needs an epoch")
            if not isinstance(merged_node, str) or not merged_node:
                raise ScreenInputError(
                    f"queued watch row {row_id} in {path} needs merged_node"
                )
            rows.append(
                {
                    "tree_path": path,
                    "row_id": row_id,
                    "epoch": epoch,
                    "merged_node": merged_node,
                }
            )
    return sorted(
        rows,
        key=lambda row: (stable_path_key(row["tree_path"]), stable_text_key(row["row_id"])),
    )


def _settlement_completeness(
    state: _CommittedState, config: ScreenConfig, record_id: str
) -> tuple[str, str, dict]:
    del config
    candidate_path, candidate_record = state.candidate(record_id)
    candidate_scope = _record_scope(candidate_record, candidate_path)
    records = state.merge_records()
    lane_frontier_records = [
        (path, record)
        for path, record in records
        if comparison_key(_record_scope(record, path)["lane"])
        == comparison_key(candidate_scope["lane"])
        and _consumed_epoch(record, path) is not None
    ]
    frontier = max(
        (_consumed_epoch(record, path) for path, record in lane_frontier_records),
        default=None,
    )
    queued = _queued_rows(state)
    overdue = [row for row in queued if frontier is not None and row["epoch"] < frontier]
    if overdue:
        result = "fail"
        detail = "queued staleness assessments older than lane frontier: " + ", ".join(
            f"{row['tree_path']}#{row['row_id']}" for row in overdue
        )
    else:
        result = "pass"
        detail = "no queued staleness assessment predates the candidate lane frontier"
    return result, detail, {
        "candidate": _record_input(candidate_path, candidate_record),
        "lane_frontier": frontier,
        "lane_consumed_records": [
            _record_input(path, record)
            for path, record in sorted(
                lane_frontier_records,
                key=lambda item: (
                    _consumed_epoch(item[1], item[0]),
                    _record_id(item[1], item[0]),
                ),
            )
        ],
        "queued_rows": queued,
        "overdue_rows": overdue,
    }


def _queue_adjacency(
    state: _CommittedState, config: ScreenConfig, record_id: str
) -> tuple[str, str, dict]:
    candidate_path, candidate_record = state.candidate(record_id)
    candidate_scope = _record_scope(candidate_record, candidate_path)
    records = state.merge_records()
    pending = _pending_records(records)
    pending_ids = {_record_id(record, path) for path, record in pending}
    if record_id not in pending_ids:
        pending.append((candidate_path, candidate_record))
    pending = sorted(pending, key=lambda item: _record_id(item[1], item[0]))
    minimum = config.threshold("queue_adjacency_min_pending")
    shared_surfaces: list[dict] = []
    for path, record in pending:
        other_id = _record_id(record, path)
        if other_id == record_id:
            continue
        shared = _shared_scope_values(
            candidate_scope["surfaces"],
            _record_scope(record, path)["surfaces"],
        )
        if shared:
            shared_surfaces.append({"record_id": other_id, "surfaces": shared})
    count = len(pending)
    if count >= minimum:
        result = "fail"
        detail = f"pending merge adjacency {count} >= {minimum}; shared surfaces: " + (
            "; ".join(
                f"{row['record_id']}={','.join(row['surfaces'])}"
                for row in shared_surfaces
            )
            if shared_surfaces
            else "none"
        )
    else:
        result = "pass"
        detail = f"pending merge adjacency {count} < {minimum}"
    return result, detail, {
        "candidate": _record_input(candidate_path, candidate_record),
        "thresholds": {"queue_adjacency_min_pending": minimum},
        "pending_records": [_record_input(path, record) for path, record in pending],
        "pending_count": count,
        "shared_surfaces": shared_surfaces,
    }


def _watch_debt(
    state: _CommittedState, config: ScreenConfig, record_id: str
) -> tuple[str, str, dict]:
    del config
    candidate_path, candidate_record = state.candidate(record_id)
    candidate_scope = _record_scope(candidate_record, candidate_path)
    records = state.merge_records()
    queued = _queued_rows(state)
    checked: list[dict] = []
    overlaps: list[dict] = []
    for row in queued:
        matches = [
            (path, record)
            for path, record in records
            if _candidate_ref(record, path) == row["merged_node"]
            and _consumed_epoch(record, path) == row["epoch"]
        ]
        if len(matches) != 1:
            raise ScreenInputError(
                f"queued watch {row['tree_path']}#{row['row_id']} resolves to "
                f"{len(matches)} merge records for {row['merged_node']} at epoch {row['epoch']}"
            )
        source_path, source_record = matches[0]
        source_scope = _record_scope(source_record, source_path)
        shared_surfaces = _shared_scope_values(
            candidate_scope["surfaces"], source_scope["surfaces"]
        )
        glob_matches = _glob_matches(candidate_scope["globs"], source_scope["globs"])
        overlap_axes = []
        if shared_surfaces:
            overlap_axes.append("surfaces")
        if glob_matches:
            overlap_axes.append("globs")
        checked_row = {
            **row,
            "source_record": _record_input(source_path, source_record),
            "overlap": {
                "axes": overlap_axes,
                "surfaces": shared_surfaces,
                "globs": glob_matches,
            },
        }
        checked.append(checked_row)
        if overlap_axes:
            overlaps.append(checked_row)
    if overlaps:
        result = "fail"
        detail = "open disconfirmation watch debt overlaps candidate scope: " + "; ".join(
            f"{row['tree_path']}#{row['row_id']} ({', '.join(row['overlap']['axes'])})"
            for row in overlaps
        )
    else:
        result = "n/a"
        detail = "no watch outcomes exist in v1"
    return result, detail, {
        "candidate": _record_input(candidate_path, candidate_record),
        "queued_watch_rows": checked,
        "overlapping_watch_rows": overlaps,
    }


_CHECKS: tuple[
    tuple[str, Callable[[_CommittedState, ScreenConfig, str], tuple[str, str, dict]]], ...
] = (
    ("scope-overlap", _scope_overlap),
    ("surface-budget", _surface_budget),
    ("settlement-completeness", _settlement_completeness),
    ("queue-adjacency", _queue_adjacency),
    ("watch-debt", _watch_debt),
)

_CHECK_STORES: dict[str, tuple[str, ...]] = {
    "scope-overlap": (MERGE_RECORD_STORE,),
    "surface-budget": (MERGE_RECORD_STORE,),
    "settlement-completeness": (MERGE_RECORD_STORE, TREE_STORE),
    "queue-adjacency": (MERGE_RECORD_STORE,),
    "watch-debt": (MERGE_RECORD_STORE, TREE_STORE),
}


def _best_effort_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


def _sanitized_config_error(path: Path, exc: Exception) -> ScreenInputError:
    if isinstance(exc, OSError):
        reason = exc.strerror or type(exc).__name__
    else:
        reason = f"{type(exc).__name__}: {exc}"
    return ScreenInputError(f"cannot load config {path.name}: {reason}")


def _wrapped_check(
    name: str,
    check: Callable[[_CommittedState, ScreenConfig, str], tuple[str, str, dict]],
    state: _CommittedState | None,
    config: ScreenConfig | None,
    config_error: Exception | None,
    engine_error: Exception | None,
    config_path: Path,
    config_hash: str,
    record_id: str,
) -> dict:
    required_stores = _CHECK_STORES[name]
    preflight_error: Exception | None = None
    if state is not None:
        try:
            state.discovery.preflight(required_stores)
        except Exception as exc:
            preflight_error = exc
        discovery_inputs = state.discovery.citations(required_stores)
    else:
        assert engine_error is not None
        discovery_inputs = failed_snapshot_citations(engine_error, required_stores)
    base_inputs = {
        "config": {
            "name": config_path.name,
            "sha256": config_hash,
        },
        "discovery": discovery_inputs,
    }
    try:
        if config_error is not None:
            raise config_error
        if engine_error is not None:
            raise engine_error
        if preflight_error is not None:
            raise preflight_error
        assert config is not None
        assert state is not None
        result, detail, inputs = check(state, config, record_id)
        if result not in {"pass", "fail", "n/a"}:
            raise ScreenInputError(f"check {name} returned invalid result {result!r}")
        if not isinstance(detail, str) or not isinstance(inputs, dict):
            raise ScreenInputError(f"check {name} returned an invalid result shape")
        return {
            "check": name,
            "result": result,
            "detail": detail,
            "inputs": {**base_inputs, **inputs},
        }
    except Exception as exc:
        return {
            "check": name,
            "result": "fail",
            "detail": f"screen-error: {type(exc).__name__}: {exc}",
            "inputs": {
                **base_inputs,
                "record_id": record_id,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            },
        }


def run_screen(
    root: str | Path,
    record_id: str,
    *,
    config_path: str | Path | None = None,
) -> dict:
    resolved_config_path = (
        Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG_PATH
    )
    config: ScreenConfig | None = None
    config_error: Exception | None = None
    try:
        config = load_config(resolved_config_path)
    except Exception as exc:
        config_error = _sanitized_config_error(resolved_config_path, exc)
    config_hash = config.sha256 if config is not None else _best_effort_hash(resolved_config_path)
    state: _CommittedState | None = None
    engine_error: Exception | None = None
    try:
        discovery = Discovery.capture(root)
        state = _CommittedState(discovery)
        computed = discovery.snapshot.computed
        head_commit = discovery.snapshot.head_commit
        head_tree = discovery.snapshot.head_tree
    except Exception as exc:
        computed = "unavailable"
        head_commit = None
        head_tree = None
        engine_error = exc
    results = [
        _wrapped_check(
            name,
            check,
            state,
            config,
            config_error,
            engine_error,
            resolved_config_path,
            config_hash,
            record_id,
        )
        for name, check in _CHECKS
    ]
    return {
        "record_id": record_id,
        "computed": computed,
        "config_hash": config_hash,
        "engine_version": ENGINE_VERSION,
        "head_commit": head_commit,
        "head_tree": head_tree,
        "results": results,
    }


def run_screen_at_snapshot(
    root: str | Path,
    record_id: str,
    *,
    head_commit: str,
    head_tree: str,
    computed: str,
    config_path: str | Path | None = None,
) -> dict:
    """Recompute the exact W2 output at one already validated Git snapshot.

    This is the historical counterpart to :func:`run_screen`.  It deliberately
    accepts the captured physical identity instead of consulting current HEAD,
    so downstream evidence validation can prove that a transcript covers the
    complete committed frontier it cites.
    """

    resolved_root = Path(root).expanduser().resolve()
    resolved_config_path = (
        Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG_PATH
    )
    try:
        config = load_config(resolved_config_path)
    except Exception as exc:
        config_error: Exception | None = _sanitized_config_error(
            resolved_config_path, exc
        )
        config = None
    else:
        config_error = None
    config_hash = (
        config.sha256
        if config is not None
        else _best_effort_hash(resolved_config_path)
    )
    state = _CommittedState(
        Discovery(GitSnapshot(resolved_root, head_commit, head_tree, computed))
    )
    results = [
        _wrapped_check(
            name,
            check,
            state,
            config,
            config_error,
            None,
            resolved_config_path,
            config_hash,
            record_id,
        )
        for name, check in _CHECKS
    ]
    return {
        "record_id": record_id,
        "computed": computed,
        "config_hash": config_hash,
        "engine_version": ENGINE_VERSION,
        "head_commit": head_commit,
        "head_tree": head_tree,
        "results": results,
    }


def render_screen(result: dict) -> str:
    return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
