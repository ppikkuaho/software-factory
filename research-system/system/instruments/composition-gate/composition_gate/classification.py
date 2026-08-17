"""Fail-closed validation for committed W2 composition-gate screens."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from . import ENGINE_VERSION
from .config import DEFAULT_CONFIG_PATH, ScreenConfig, load_config
from .discovery import MERGE_RECORD_STORE, TREE_STORE
from .normalization import comparison_key, stable_path_key, stable_text_key
from .screen import CHECK_NAMES, run_screen_at_snapshot


INVALID = "invalid"
ALLGREEN = "allgreen"
SEMANTIC_FAILURE = "semantic-failure"

PACKAGED_CONFIG_NAME = "screen-config.v1.json"
PACKAGED_CONFIG_SHA256 = (
    "2ceb7d124076018e9983d7974aa5b9aa7b39e251f3e5b2c079f6d08785f7d03f"
)

_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RECORD_ID = re.compile(r"MR-[0-9]+")
_RESULT_TOKENS = frozenset({"pass", "fail", "n/a"})
_SCOPE_AXES = ("lane", "seats", "surfaces", "globs")
_OVERLAP_MODES = frozenset(
    {"literal", "candidate-matched-by-other", "other-matched-by-candidate"}
)

_SCREEN_FIELDS = frozenset(
    {
        "results",
        "output_ref",
        "log_ref",
        "log_sha256",
        "output_sha256",
        "computed",
        "head_commit",
        "head_tree",
        "config_hash",
        "engine_version",
    }
)
_ENGINE_FIELDS = frozenset(
    {
        "record_id",
        "computed",
        "config_hash",
        "engine_version",
        "head_commit",
        "head_tree",
        "results",
    }
)
_RESULT_FIELDS = frozenset({"check", "result", "detail", "inputs"})
_CHECK_STORES = {
    "scope-overlap": (MERGE_RECORD_STORE,),
    "surface-budget": (MERGE_RECORD_STORE,),
    "settlement-completeness": (MERGE_RECORD_STORE, TREE_STORE),
    "queue-adjacency": (MERGE_RECORD_STORE,),
    "watch-debt": (MERGE_RECORD_STORE, TREE_STORE),
}
_CHECK_INPUT_FIELDS = {
    "scope-overlap": frozenset(
        {
            "config",
            "discovery",
            "candidate",
            "thresholds",
            "consumed_inventory",
            "pending_record_ids",
            "comparisons",
            "collisions",
        }
    ),
    "surface-budget": frozenset(
        {
            "config",
            "discovery",
            "candidate",
            "thresholds",
            "not_computable",
            "surface_counts",
        }
    ),
    "settlement-completeness": frozenset(
        {
            "config",
            "discovery",
            "candidate",
            "lane_frontier",
            "lane_consumed_records",
            "queued_rows",
            "overdue_rows",
        }
    ),
    "queue-adjacency": frozenset(
        {
            "config",
            "discovery",
            "candidate",
            "thresholds",
            "pending_records",
            "pending_count",
            "shared_surfaces",
        }
    ),
    "watch-debt": frozenset(
        {
            "config",
            "discovery",
            "candidate",
            "queued_watch_rows",
            "overlapping_watch_rows",
        }
    ),
}


class ScreenValidationError(ValueError):
    """The screen is not a trustworthy W2 evidence object."""


def _fail(message: str) -> None:
    raise ScreenValidationError(message)


def _object(value: Any, label: str, fields: frozenset[str] | None = None) -> dict:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    if fields is not None and set(value) != set(fields):
        missing = sorted(set(fields) - set(value))
        unknown = sorted(set(value) - set(fields))
        _fail(f"{label} fields mismatch; missing={missing}, unknown={unknown}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be a non-empty string")
    return value


def _oid(value: Any, label: str) -> str:
    if not isinstance(value, str) or _OID.fullmatch(value) is None:
        _fail(f"{label} must be a Git object id")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(f"{label} must be a lowercase SHA-256")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _fail(f"{label} must be an integer >= {minimum}")
    return value


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        _fail(f"{label} must be an array of non-empty strings")
    return value


def _record_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _RECORD_ID.fullmatch(value) is None:
        _fail(f"{label} must be MR-<n>")
    return value


def _git(root: Path, *args: str, binary: bool = False) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=not binary,
        )
    except OSError as exc:
        _fail(f"cannot execute Git: {exc}")
    if result.returncode != 0:
        stderr = result.stderr
        stdout = result.stdout
        if binary:
            stderr = stderr.decode("utf-8", errors="replace")
            stdout = stdout.decode("utf-8", errors="replace")
        _fail(stderr.strip() or stdout.strip() or "unknown Git error")
    return result.stdout if binary else result.stdout.strip()


def _tree_entry(root: Path, tree: str, path: str) -> tuple[str, str, str] | None:
    raw = _git(root, "ls-tree", "-z", "--full-tree", tree, "--", path, binary=True)
    assert isinstance(raw, bytes)
    rows = [row for row in raw.split(b"\0") if row]
    if len(rows) != 1:
        return None
    metadata, separator, raw_path = rows[0].partition(b"\t")
    fields = metadata.split(b" ")
    if separator != b"\t" or len(fields) != 3:
        _fail(f"malformed Git tree entry for {path}")
    try:
        mode, kind, oid = (field.decode("ascii") for field in fields)
        observed_path = raw_path.decode("utf-8")
    except UnicodeDecodeError:
        _fail(f"malformed Git tree entry for {path}")
    if observed_path != path:
        _fail(f"Git tree entry path mismatch for {path}")
    _oid(oid, f"Git tree entry {path}")
    return mode, kind, oid


def _scope(value: Any, label: str) -> dict:
    scope = _object(value, label, frozenset(_SCOPE_AXES))
    _string(scope["lane"], f"{label}.lane")
    for field in _SCOPE_AXES[1:]:
        _strings(scope[field], f"{label}.{field}")
    return scope


def _record(value: Any, label: str, expected_id: str | None = None) -> dict:
    record = _object(
        value,
        label,
        frozenset(
            {"record_id", "path", "candidate_ref", "consumed_epoch", "gate_verdict", "scope"}
        ),
    )
    rid = _record_id(record["record_id"], f"{label}.record_id")
    if expected_id is not None and rid != expected_id:
        _fail(f"{label}.record_id does not cite {expected_id}")
    if record["path"] != f"tier1/merge-records/{rid}.json":
        _fail(f"{label}.path does not match its record_id")
    _string(record["candidate_ref"], f"{label}.candidate_ref")
    if record["consumed_epoch"] is not None:
        _integer(record["consumed_epoch"], f"{label}.consumed_epoch")
    if record["gate_verdict"] is not None and not isinstance(record["gate_verdict"], dict):
        _fail(f"{label}.gate_verdict must be null or an object")
    _scope(record["scope"], f"{label}.scope")
    return record


def _pending(record: dict, label: str) -> bool:
    gate = record["gate_verdict"]
    if gate is None:
        return True
    verdict = _string(gate.get("verdict"), f"{label}.gate_verdict.verdict")
    return verdict == "land" and record["consumed_epoch"] is None


def _shared(left: list[str], right: list[str]) -> list[str]:
    right_keys = {comparison_key(item) for item in right}
    found: dict[str, str] = {}
    for item in left:
        key = comparison_key(item)
        if key in right_keys and key not in found:
            found[key] = item
    return sorted(found.values(), key=stable_text_key)


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


def _collision(candidate: dict, other: dict) -> dict:
    axes: list[str] = []
    values: dict[str, Any] = {}
    if comparison_key(candidate["lane"]) == comparison_key(other["lane"]):
        axes.append("lane")
        values["lane"] = candidate["lane"]
    for field in ("seats", "surfaces"):
        shared = _shared(candidate[field], other[field])
        if shared:
            axes.append(field)
            values[field] = shared
    globs = _glob_matches(candidate["globs"], other["globs"])
    if globs:
        axes.append("globs")
        values["globs"] = globs
    return {"axes": axes, "values": values}


def _validate_collision(value: Any, label: str) -> dict:
    collision = _object(value, label, frozenset({"axes", "values"}))
    axes = _strings(collision["axes"], f"{label}.axes")
    if len(set(axes)) != len(axes) or any(axis not in _SCOPE_AXES for axis in axes):
        _fail(f"{label}.axes contains an unknown or repeated axis")
    if axes != [axis for axis in _SCOPE_AXES if axis in axes]:
        _fail(f"{label}.axes is not in W2 order")
    values = _object(collision["values"], f"{label}.values")
    if set(values) != set(axes):
        _fail(f"{label}.values must cite exactly its axes")
    if "lane" in values:
        _string(values["lane"], f"{label}.values.lane")
    for field in ("seats", "surfaces"):
        if field in values:
            _strings(values[field], f"{label}.values.{field}")
    if "globs" in values:
        if not isinstance(values["globs"], list):
            _fail(f"{label}.values.globs must be an array")
        for index, row in enumerate(values["globs"]):
            item = _object(
                row,
                f"{label}.values.globs[{index}]",
                frozenset({"candidate_glob", "other_glob", "mode"}),
            )
            _string(item["candidate_glob"], "candidate_glob")
            _string(item["other_glob"], "other_glob")
            if item["mode"] not in _OVERLAP_MODES:
                _fail(f"{label}.values.globs[{index}].mode is invalid")
    return collision


def _config_citation(value: Any, config: ScreenConfig) -> None:
    citation = _object(value, "inputs.config", frozenset({"name", "sha256"}))
    if citation != {"name": PACKAGED_CONFIG_NAME, "sha256": config.sha256}:
        _fail("inputs.config is not the packaged W2 config")


def _discovery_citation(
    root: Path, value: Any, check: str, head_commit: str, head_tree: str
) -> None:
    discovery = _object(value, f"{check}.inputs.discovery", frozenset({"snapshot", "stores"}))
    snapshot = _object(
        discovery["snapshot"],
        f"{check}.inputs.discovery.snapshot",
        frozenset({"status", "head_commit", "head_tree", "error"}),
    )
    if snapshot != {
        "status": "ok",
        "head_commit": head_commit,
        "head_tree": head_tree,
        "error": None,
    }:
        _fail(f"{check} discovery snapshot does not cite the successful screen snapshot")
    stores = _object(discovery["stores"], f"{check}.inputs.discovery.stores")
    expected = set(_CHECK_STORES[check])
    if set(stores) != expected:
        _fail(f"{check} must cite exactly {sorted(expected)}")
    for name in _CHECK_STORES[check]:
        citation = _object(
            stores[name],
            f"{check} store {name}",
            frozenset({"status", "tree_oid", "observed", "error"}),
        )
        tree_oid = _oid(citation["tree_oid"], f"{check} store {name}.tree_oid")
        expected_citation = {
            "status": "ok",
            "tree_oid": tree_oid,
            "observed": {"mode": "040000", "type": "tree", "oid": tree_oid},
            "error": None,
        }
        if citation != expected_citation:
            _fail(f"{check} store {name} is not an exact successful W2 citation")
        if _tree_entry(root, head_tree, name) != ("040000", "tree", tree_oid):
            _fail(f"{check} store {name} citation does not match recorded Git")


def _scope_inputs(inputs: dict, record_id: str, config: ScreenConfig) -> None:
    candidate = _record(inputs["candidate"], "scope-overlap.candidate", record_id)
    thresholds = _object(inputs["thresholds"], "scope-overlap.thresholds", frozenset({"k_last_merged"}))
    k = _integer(thresholds["k_last_merged"], "k_last_merged", 1)
    if k != config.threshold("k_last_merged"):
        _fail("scope-overlap threshold differs from packaged config")

    inventory = inputs["consumed_inventory"]
    if not isinstance(inventory, list):
        _fail("scope-overlap.consumed_inventory must be an array")
    inventory_ids: set[str] = set()
    previous_key: tuple[int, str] | None = None
    selected_ids: set[str] = set()
    for index, row in enumerate(inventory):
        item = _object(
            row,
            f"consumed_inventory[{index}]",
            frozenset({"record_id", "path", "consumed_epoch", "selected"}),
        )
        rid = _record_id(item["record_id"], "consumed_inventory.record_id")
        epoch = _integer(item["consumed_epoch"], "consumed_inventory.consumed_epoch")
        if rid == record_id or rid in inventory_ids:
            _fail("scope-overlap.consumed_inventory has an invalid record")
        inventory_ids.add(rid)
        if item["path"] != f"tier1/merge-records/{rid}.json":
            _fail("scope-overlap.consumed_inventory path mismatch")
        key = (-epoch, rid)
        if previous_key is not None and key < previous_key:
            _fail("scope-overlap.consumed_inventory is not in W2 order")
        previous_key = key
        if type(item["selected"]) is not bool or item["selected"] != (index < k):
            _fail("scope-overlap consumed window is contradictory")
        if item["selected"]:
            selected_ids.add(rid)

    pending_ids = _strings(inputs["pending_record_ids"], "scope-overlap.pending_record_ids")
    if any(_RECORD_ID.fullmatch(rid) is None for rid in pending_ids):
        _fail("scope-overlap.pending_record_ids contains an invalid MR id")
    if pending_ids != sorted(set(pending_ids)) or record_id in pending_ids:
        _fail("scope-overlap.pending_record_ids is not the canonical pending set")

    comparisons = inputs["comparisons"]
    if not isinstance(comparisons, list):
        _fail("scope-overlap.comparisons must be an array")
    expected_ids = sorted(selected_ids | set(pending_ids))
    if len(comparisons) != len(expected_ids):
        _fail("scope-overlap.comparisons does not cover its cited windows")
    expected_collisions: list[dict] = []
    for index, (row, expected_id) in enumerate(zip(comparisons, expected_ids)):
        item = _object(
            row,
            f"scope-overlap.comparisons[{index}]",
            frozenset({"record", "comparison_sets", "collision"}),
        )
        other = _record(item["record"], f"comparisons[{index}].record", expected_id)
        sets = _strings(item["comparison_sets"], f"comparisons[{index}].comparison_sets")
        expected_sets = sorted(
            (["last-consumed"] if expected_id in selected_ids else [])
            + (["pending"] if expected_id in pending_ids else [])
        )
        if sets != expected_sets:
            _fail("scope-overlap comparison set membership is contradictory")
        if expected_id in pending_ids and not _pending(other, f"comparisons[{index}].record"):
            _fail("scope-overlap pending comparison is not pending")
        collision = _validate_collision(item["collision"], f"comparisons[{index}].collision")
        expected_collision = _collision(candidate["scope"], other["scope"])
        if collision != expected_collision:
            _fail("scope-overlap collision disagrees with cited scopes")
        if collision["axes"]:
            expected_collisions.append({"record_id": expected_id, **collision})
    if inputs["collisions"] != expected_collisions:
        _fail("scope-overlap.collisions disagrees with comparison rows")


def _surface_inputs(inputs: dict, record_id: str, config: ScreenConfig) -> None:
    candidate = _record(inputs["candidate"], "surface-budget.candidate", record_id)
    thresholds = _object(
        inputs["thresholds"],
        "surface-budget.thresholds",
        frozenset(
            {"surface_budget_max_cumulative_directives", "surface_budget_max_diff_lines"}
        ),
    )
    for name in thresholds:
        if _integer(thresholds[name], name, 1) != config.threshold(name):
            _fail(f"surface-budget {name} differs from packaged config")
    if inputs["not_computable"] != {
        "diff_lines": "merge-record v1 does not carry candidate diff size"
    }:
        _fail("surface-budget.not_computable is not the W2 citation")
    counts = _object(inputs["surface_counts"], "surface-budget.surface_counts")
    surfaces = candidate["scope"]["surfaces"]
    if set(counts) != set(surfaces):
        _fail("surface-budget.surface_counts must cite exactly candidate surfaces")
    for surface, value in counts.items():
        item = _object(value, f"surface_counts[{surface}]", frozenset({"count", "records"}))
        records = item["records"]
        if not isinstance(records, list):
            _fail(f"surface_counts[{surface}].records must be an array")
        if _integer(item["count"], f"surface_counts[{surface}].count") != len(records):
            _fail(f"surface_counts[{surface}].count disagrees with its rows")
        seen: set[str] = set()
        order: list[tuple[int, str]] = []
        for index, value in enumerate(records):
            record = _record(value, f"surface_counts[{surface}].records[{index}]")
            rid = record["record_id"]
            epoch = record["consumed_epoch"]
            if rid == record_id or rid in seen or epoch is None:
                _fail(f"surface_counts[{surface}] contains an invalid record")
            seen.add(rid)
            if comparison_key(surface) not in {
                comparison_key(item) for item in record["scope"]["surfaces"]
            }:
                _fail(f"surface_counts[{surface}] cites a record without that surface")
            order.append((-epoch, rid))
        if order != sorted(order):
            _fail(f"surface_counts[{surface}] records are not in W2 order")


def _queue_row(value: Any, label: str) -> dict:
    row = _object(value, label, frozenset({"tree_path", "row_id", "epoch", "merged_node"}))
    _string(row["tree_path"], f"{label}.tree_path")
    _string(row["row_id"], f"{label}.row_id")
    _integer(row["epoch"], f"{label}.epoch")
    _string(row["merged_node"], f"{label}.merged_node")
    return row


def _settlement_inputs(inputs: dict, record_id: str) -> None:
    candidate = _record(inputs["candidate"], "settlement-completeness.candidate", record_id)
    lane_records = inputs["lane_consumed_records"]
    if not isinstance(lane_records, list):
        _fail("settlement lane_consumed_records must be an array")
    seen: set[str] = set()
    order: list[tuple[int, str]] = []
    for index, value in enumerate(lane_records):
        record = _record(value, f"lane_consumed_records[{index}]")
        rid = record["record_id"]
        epoch = record["consumed_epoch"]
        if rid in seen or epoch is None:
            _fail("settlement lane_consumed_records has an invalid record")
        seen.add(rid)
        if comparison_key(record["scope"]["lane"]) != comparison_key(candidate["scope"]["lane"]):
            _fail("settlement lane_consumed_records crosses lanes")
        order.append((epoch, rid))
    if order != sorted(order):
        _fail("settlement lane_consumed_records is not in W2 order")
    frontier = max((epoch for epoch, _ in order), default=None)
    if inputs["lane_frontier"] != frontier:
        _fail("settlement lane_frontier disagrees with lane records")

    queued = inputs["queued_rows"]
    if not isinstance(queued, list):
        _fail("settlement queued_rows must be an array")
    keys: set[tuple[str, str]] = set()
    queue_order: list[tuple[Any, Any]] = []
    for index, value in enumerate(queued):
        row = _queue_row(value, f"queued_rows[{index}]")
        key = (row["tree_path"], row["row_id"])
        if key in keys:
            _fail("settlement queued_rows repeats a watch row")
        keys.add(key)
        queue_order.append((stable_path_key(key[0]), stable_text_key(key[1])))
    if queue_order != sorted(queue_order):
        _fail("settlement queued_rows is not in W2 order")
    expected_overdue = [row for row in queued if frontier is not None and row["epoch"] < frontier]
    if inputs["overdue_rows"] != expected_overdue:
        _fail("settlement overdue_rows disagrees with queued rows and frontier")


def _queue_inputs(inputs: dict, record_id: str, config: ScreenConfig) -> None:
    candidate = _record(inputs["candidate"], "queue-adjacency.candidate", record_id)
    thresholds = _object(
        inputs["thresholds"], "queue-adjacency.thresholds", frozenset({"queue_adjacency_min_pending"})
    )
    minimum = _integer(thresholds["queue_adjacency_min_pending"], "queue adjacency minimum", 1)
    if minimum != config.threshold("queue_adjacency_min_pending"):
        _fail("queue-adjacency threshold differs from packaged config")
    pending = inputs["pending_records"]
    if not isinstance(pending, list):
        _fail("queue-adjacency.pending_records must be an array")
    records: list[dict] = []
    ids: list[str] = []
    for index, value in enumerate(pending):
        record = _record(value, f"pending_records[{index}]")
        rid = record["record_id"]
        if rid in ids or (rid != record_id and not _pending(record, f"pending_records[{index}]")):
            _fail("queue-adjacency.pending_records contains an invalid record")
        ids.append(rid)
        records.append(record)
    if ids != sorted(ids) or record_id not in ids:
        _fail("queue-adjacency.pending_records is not the canonical W2 set")
    if inputs["pending_count"] != len(records):
        _fail("queue-adjacency.pending_count disagrees with pending_records")
    expected_shared = []
    for record in records:
        if record["record_id"] == record_id:
            continue
        shared = _shared(candidate["scope"]["surfaces"], record["scope"]["surfaces"])
        if shared:
            expected_shared.append({"record_id": record["record_id"], "surfaces": shared})
    if inputs["shared_surfaces"] != expected_shared:
        _fail("queue-adjacency.shared_surfaces disagrees with pending scopes")


def _watch_inputs(inputs: dict, record_id: str) -> None:
    candidate = _record(inputs["candidate"], "watch-debt.candidate", record_id)
    queued = inputs["queued_watch_rows"]
    if not isinstance(queued, list):
        _fail("watch-debt.queued_watch_rows must be an array")
    keys: set[tuple[str, str]] = set()
    order: list[tuple[Any, Any]] = []
    for index, value in enumerate(queued):
        item = _object(
            value,
            f"queued_watch_rows[{index}]",
            frozenset({"tree_path", "row_id", "epoch", "merged_node", "source_record", "overlap"}),
        )
        row = _queue_row(
            {key: item[key] for key in ("tree_path", "row_id", "epoch", "merged_node")},
            f"queued_watch_rows[{index}]",
        )
        key = (row["tree_path"], row["row_id"])
        if key in keys:
            _fail("watch-debt.queued_watch_rows repeats a watch row")
        keys.add(key)
        order.append((stable_path_key(key[0]), stable_text_key(key[1])))
        source = _record(item["source_record"], f"queued_watch_rows[{index}].source_record")
        if source["consumed_epoch"] != row["epoch"] or source["candidate_ref"] != row["merged_node"]:
            _fail("watch-debt source record does not resolve its queued row")
        expected_overlap = {
            "axes": [],
            "surfaces": _shared(candidate["scope"]["surfaces"], source["scope"]["surfaces"]),
            "globs": _glob_matches(candidate["scope"]["globs"], source["scope"]["globs"]),
        }
        if expected_overlap["surfaces"]:
            expected_overlap["axes"].append("surfaces")
        if expected_overlap["globs"]:
            expected_overlap["axes"].append("globs")
        if item["overlap"] != expected_overlap:
            _fail("watch-debt overlap disagrees with cited scopes")
    if order != sorted(order):
        _fail("watch-debt.queued_watch_rows is not in W2 order")
    expected = [row for row in queued if row["overlap"]["axes"]]
    if inputs["overlapping_watch_rows"] != expected:
        _fail("watch-debt.overlapping_watch_rows disagrees with queued rows")


def _check_inputs(
    root: Path,
    check: str,
    inputs: dict,
    record_id: str,
    head_commit: str,
    head_tree: str,
    config: ScreenConfig,
) -> None:
    if set(inputs) != set(_CHECK_INPUT_FIELDS[check]):
        _fail(f"{check}.inputs has unknown or missing top-level keys")
    _config_citation(inputs["config"], config)
    _discovery_citation(root, inputs["discovery"], check, head_commit, head_tree)
    if check == "scope-overlap":
        _scope_inputs(inputs, record_id, config)
    elif check == "surface-budget":
        _surface_inputs(inputs, record_id, config)
    elif check == "settlement-completeness":
        _settlement_inputs(inputs, record_id)
    elif check == "queue-adjacency":
        _queue_inputs(inputs, record_id, config)
    else:
        _watch_inputs(inputs, record_id)


def _engine_output(output: Any, record_id: str) -> list[dict]:
    document = _object(output, "engine output", _ENGINE_FIELDS)
    if document["record_id"] != record_id:
        _fail("engine output record_id does not match the target MR")
    _string(document["computed"], "engine output computed")
    _string(document["config_hash"], "engine output config_hash")
    _string(document["engine_version"], "engine output engine_version")
    if (document["head_commit"] is None) != (document["head_tree"] is None):
        _fail("engine output head_commit/head_tree must be null together")
    if document["head_commit"] is not None:
        _oid(document["head_commit"], "engine output head_commit")
        _oid(document["head_tree"], "engine output head_tree")
    results = document["results"]
    if not isinstance(results, list) or len(results) != len(CHECK_NAMES):
        _fail("engine output must contain exactly five results")
    for index, (value, check) in enumerate(zip(results, CHECK_NAMES)):
        row = _object(value, f"engine output results[{index}]", _RESULT_FIELDS)
        if row["check"] != check:
            _fail("engine output checks are not in canonical W2 order")
        if row["result"] not in _RESULT_TOKENS:
            _fail(f"engine output {check} has an illegal result")
        _string(row["detail"], f"engine output {check}.detail")
        inputs = _object(row["inputs"], f"engine output {check}.inputs")
        if not {"config", "discovery"} <= set(inputs):
            _fail(f"engine output {check} lacks W2 citations")
        _object(inputs["config"], f"engine output {check}.config")
        _object(inputs["discovery"], f"engine output {check}.discovery")
    return results


def validate_engine_output(output: Any, record_id: str) -> list[dict]:
    """Validate the W2 envelope before transcription, including its error shape."""

    return _engine_output(output, record_id)


def _var_ref(root: Path, value: Any, label: str) -> tuple[str, Path]:
    raw = _string(value, label)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    root_resolved = root.resolve()
    var_resolved = (root / "var").resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(var_resolved):
        _fail(f"{label} must resolve below the research root's var/")
    relative = resolved.relative_to(root_resolved).as_posix()
    if not relative.startswith("var/"):
        _fail(f"{label} must resolve below the research root's var/")
    return relative, resolved


def _outcome(check: str, result: str, inputs: dict) -> None:
    if check == "scope-overlap":
        expected = "fail" if inputs["collisions"] else "pass"
    elif check == "surface-budget":
        counts = inputs["surface_counts"]
        if not inputs["candidate"]["scope"]["surfaces"] and not counts:
            expected = "n/a"
        else:
            maximum = inputs["thresholds"]["surface_budget_max_cumulative_directives"]
            expected = "fail" if any(row["count"] > maximum for row in counts.values()) else "pass"
    elif check == "settlement-completeness":
        expected = "fail" if inputs["overdue_rows"] else "pass"
    elif check == "queue-adjacency":
        expected = (
            "fail"
            if inputs["pending_count"] >= inputs["thresholds"]["queue_adjacency_min_pending"]
            else "pass"
        )
    else:
        expected = "fail" if inputs["overlapping_watch_rows"] else "n/a"
    if result != expected:
        _fail(f"{check} result contradicts its cited W2 inputs")


def _record_and_screen(root: Path, record: dict | str, screen: dict | None) -> tuple[dict, dict]:
    if isinstance(record, str):
        rid = _record_id(record, "record_id")
        try:
            record = json.loads(
                (root / "tier1" / "merge-records" / f"{rid}.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            _fail(f"cannot read committed merge record: {exc}")
    if not isinstance(record, dict):
        _fail("merge record must be an object or MR id")
    if screen is None:
        screen = record.get("screen")
    if not isinstance(screen, dict):
        _fail("merge record screen is missing")
    return record, screen


def validate_screen(
    root: str | Path,
    record: dict | str,
    screen: dict | None = None,
    *,
    evidence_bytes: Mapping[str, bytes] | None = None,
) -> str:
    """Return ``allgreen`` or ``semantic-failure``; raise when evidence is invalid."""

    root_path = Path(root).expanduser().resolve()
    record, screen = _record_and_screen(root_path, record, screen)
    rid = _record_id(record.get("id"), "merge record id")
    _object(screen, "merge record screen", _SCREEN_FIELDS)

    refs = {
        "output": _var_ref(root_path, screen["output_ref"], "screen.output_ref"),
        "log": _var_ref(root_path, screen["log_ref"], "screen.log_ref"),
    }
    if screen["output_ref"] != refs["output"][0] or screen["log_ref"] != refs["log"][0]:
        _fail("screen refs must be normalized repository-relative var/ paths")
    bytes_by_path: dict[Path, bytes] = {}
    for normalized_ref, path in refs.values():
        if path not in bytes_by_path:
            if evidence_bytes is None:
                try:
                    bytes_by_path[path] = path.read_bytes()
                except OSError as exc:
                    _fail(f"screen evidence cannot be read: {exc}")
            else:
                captured = evidence_bytes.get(normalized_ref)
                if not isinstance(captured, bytes):
                    _fail(f"screen evidence was not captured for {normalized_ref}")
                bytes_by_path[path] = captured
    output_bytes = bytes_by_path[refs["output"][1]]
    log_bytes = bytes_by_path[refs["log"][1]]
    if hashlib.sha256(output_bytes).hexdigest() != _sha256(
        screen["output_sha256"], "screen.output_sha256"
    ):
        _fail("screen.output_sha256 does not match exact output bytes")
    if hashlib.sha256(log_bytes).hexdigest() != _sha256(screen["log_sha256"], "screen.log_sha256"):
        _fail("screen.log_sha256 does not match exact log bytes")
    try:
        output = json.loads(output_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"screen output is not valid UTF-8 JSON: {exc}")
    results = _engine_output(output, rid)
    if results != screen["results"]:
        _fail("committed screen results differ from output_ref bytes")
    for field in ("computed", "head_commit", "head_tree", "config_hash", "engine_version"):
        if screen[field] != output[field]:
            _fail(f"screen.{field} differs from output_ref bytes")

    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception as exc:
        _fail(f"cannot rehash packaged W2 config: {exc}")
    if config.sha256 != PACKAGED_CONFIG_SHA256:
        _fail("packaged W2 config bytes do not match the frozen hash")
    if screen["engine_version"] != ENGINE_VERSION or screen["config_hash"] != config.sha256:
        _fail("screen engine/config identity is not the frozen W2 identity")
    head_commit = _oid(screen["head_commit"], "screen.head_commit")
    head_tree = _oid(screen["head_tree"], "screen.head_tree")
    if _git(root_path, "rev-parse", "--verify", f"{head_commit}^{{tree}}") != head_tree:
        _fail("screen.head_tree does not match screen.head_commit")
    if _git(root_path, "show", "-s", "--format=%cI", head_commit) != screen["computed"]:
        _fail("screen.computed does not match the cited commit timestamp")

    authoritative = run_screen_at_snapshot(
        root_path,
        rid,
        head_commit=head_commit,
        head_tree=head_tree,
        computed=screen["computed"],
    )
    if output != authoritative:
        _fail("screen output is not the exact committed W2 snapshot result")

    for row in results:
        check = row["check"]
        detail = _string(row["detail"], f"{check}.detail")
        if detail.startswith("screen-error:"):
            _fail(f"{check} contains a screen-error detail")
        inputs = _object(row["inputs"], f"{check}.inputs")
        if "error" in inputs:
            _fail(f"{check} contains inputs.error")
        _check_inputs(root_path, check, inputs, rid, head_commit, head_tree, config)
        _outcome(check, row["result"], inputs)
    return SEMANTIC_FAILURE if any(row["result"] == "fail" for row in results) else ALLGREEN


def classify_screen(
    root: str | Path,
    record: dict | str,
    screen: dict | None = None,
    *,
    evidence_bytes: Mapping[str, bytes] | None = None,
) -> str:
    """Return exactly ``invalid``, ``allgreen``, or ``semantic-failure``."""

    try:
        return validate_screen(root, record, screen, evidence_bytes=evidence_bytes)
    except Exception:
        return INVALID


def is_allgreen(
    root: str | Path,
    record: dict | str,
    screen: dict | None = None,
    *,
    evidence_bytes: Mapping[str, bytes] | None = None,
) -> bool:
    return (
        classify_screen(root, record, screen, evidence_bytes=evidence_bytes)
        == ALLGREEN
    )


__all__ = [
    "ALLGREEN",
    "INVALID",
    "PACKAGED_CONFIG_NAME",
    "PACKAGED_CONFIG_SHA256",
    "SEMANTIC_FAILURE",
    "ScreenValidationError",
    "classify_screen",
    "is_allgreen",
    "validate_engine_output",
    "validate_screen",
]
