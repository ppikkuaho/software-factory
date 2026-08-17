"""Statistics store (C1 §7/§8) — mechanical-only, merge-updated per run.

`readout/statistics.json`, keyed by session id: per-run screens aggregates +
running aggregates + the defect-flow (caught-at × introduced-at) matrix. LLM
outputs are deliberately EXCLUDED — the store is deterministic given the same
bundle bytes, so re-running a session is idempotent. The co-located legend lives
in `readout/INTERPRETATION.md` (the numbers travel with their interpretation).

Defect-flow v1.1: caught-at is populated only from a validated runtime audit.
Introduced-at remains explicitly unclassified; it is never inferred.
"""

from __future__ import annotations

import json
import os

from . import OBSERVATORY_VERSION

STATISTICS_VERSION = "observatory-statistics/1.1.0"
_LEGACY_STATISTICS_VERSION = "observatory-statistics/1.0.0"

# L1-L5 path levels + the censoring cell for escaped defects (C1 §7 caveat b)
_LEVELS = ["L1", "L2", "L3", "L4", "L5", "production"]
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def screens_to_entry(screens):
    """A deterministic, mechanical per-run record derived purely from screens."""
    ts = screens.get("token_spend", {})
    tc = screens.get("tool_calls", {})
    orient = {}
    for actor, a in (screens.get("orientation") or {}).items():
        orient[actor] = {
            "time_to_first_relevant_read_ms": a.get("time_to_first_relevant_read_ms"),
            "reads_before_first_relevant": a.get("reads_before_first_relevant"),
            "off_task_read_count": a.get("off_task_read_count"),
        }
    return {
        "bundle_sha256": screens.get("bundle_sha256"),
        "token_spend_total": ts.get("total", {}),
        "token_spend_per_actor": ts.get("per_actor", {}),
        "tool_calls_total": tc.get("total"),
        "tool_calls_by_tool": tc.get("by_tool", {}),
        "repeated_identical_calls": (screens.get("repeated_identical_calls") or {}).get("count"),
        "errored_calls": (screens.get("errored_calls") or {}).get("count"),
        "retry_shaped": (screens.get("retry_shaped") or {}).get("count"),
        "orientation": orient,
        "branch_groups": (screens.get("branches") or {}).get("branch_groups"),
        "compact_boundaries": screens.get("compact_boundaries"),
        "subagent_joins": (screens.get("subagents") or {}).get("joins"),
        "orphan_actors": (screens.get("subagents") or {}).get("orphan_actors"),
        "gate_events": screens.get("gate_events"),
        "gate_events_note": screens.get("gate_events_note"),
        "runtime_audit": screens.get("runtime_audit"),
    }


def _int(x):
    return x if isinstance(x, int) else 0


def _aggregate(runs):
    tok = {k: 0 for k in _TOKEN_KEYS}
    agg = {
        "run_count": len(runs),
        "total_tool_calls": 0,
        "total_errored_calls": 0,
        "total_repeated_identical_calls": 0,
        "total_retry_shaped": 0,
        "runs_with_errored_calls": 0,
        "runs_with_repeated_calls": 0,
        "runs_with_off_task_reads": 0,
    }
    for entry in runs.values():
        for k in _TOKEN_KEYS:
            tok[k] += _int((entry.get("token_spend_total") or {}).get(k))
        agg["total_tool_calls"] += _int(entry.get("tool_calls_total"))
        agg["total_errored_calls"] += _int(entry.get("errored_calls"))
        agg["total_repeated_identical_calls"] += _int(entry.get("repeated_identical_calls"))
        agg["total_retry_shaped"] += _int(entry.get("retry_shaped"))
        if _int(entry.get("errored_calls")) > 0:
            agg["runs_with_errored_calls"] += 1
        if _int(entry.get("repeated_identical_calls")) > 0:
            agg["runs_with_repeated_calls"] += 1
        if any(_int(a.get("off_task_read_count")) > 0
               for a in (entry.get("orientation") or {}).values()):
            agg["runs_with_off_task_reads"] += 1
    agg["total_token_spend"] = tok
    return agg


def _defect_flow(runs):
    """Caught-at × the explicit, sole introduced-at ``unclassified`` bucket."""
    audited_runs = [sid for sid, entry in runs.items() if entry.get("runtime_audit") is not None]
    counts = {level: 0 for level in _LEVELS}
    for sid in audited_runs:
        events = runs[sid].get("gate_events")
        if not isinstance(events, list):
            raise ValueError("audited statistics entries must carry a gate_events list")
        for event in events:
            caught_at = event.get("caught_at") if isinstance(event, dict) else None
            if caught_at not in counts:
                raise ValueError("audited statistics event has an unknown caught_at level")
            counts[caught_at] += 1
    populated = bool(audited_runs)
    matrix = (
        {level: {"unclassified": counts[level]} for level in _LEVELS}
        if populated else {}
    )
    return {
        "caught_at_axis": list(_LEVELS),
        "introduced_at_axis": ["unclassified"],
        "caught_at_counts": counts if populated else None,
        "matrix": matrix,
        "populated": populated,
        "gate_event_runs": len(audited_runs),
        "note": (
            "caught-at is populated only from validated runtime audit events. "
            "introduced-at classification remains future work, so every audited event "
            "is counted in the explicit unclassified bucket. Without an audit the axis "
            "remains unpopulated rather than fabricated. Caveats: (a) a gate that catches "
            "nothing looks clean — 'found at X' != 'caused by X'; (b) escaped "
            "defects arrive later as caught-at:production, not lost."
        ),
    }


def _skeleton():
    return {"statistics_version": STATISTICS_VERSION,
            "observatory_version": OBSERVATORY_VERSION, "runs": {}}


def load(stats_path):
    if os.path.isfile(stats_path):
        with open(stats_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("statistics store must be a JSON object")
        version = data.get("statistics_version")
        if version not in {STATISTICS_VERSION, _LEGACY_STATISTICS_VERSION}:
            if version is None:
                raise ValueError("statistics store is missing statistics_version")
            raise ValueError(f"unsupported statistics store version: {version}")
        runs = data.get("runs")
        if not isinstance(runs, dict):
            raise ValueError("statistics store runs must be an object")
        if version == _LEGACY_STATISTICS_VERSION:
            for entry in runs.values():
                if not isinstance(entry, dict):
                    raise ValueError("legacy statistics run entries must be objects")
                if entry.get("gate_events") is not None:
                    raise ValueError(
                        "legacy statistics gate_events must be absent or null"
                    )
                if entry.get("runtime_audit") is not None:
                    raise ValueError("legacy statistics cannot carry runtime audit data")
            # Preserve only the authoritative per-run inputs.  Derived sections
            # are recomputed by render() under the current schema.
            return {
                "statistics_version": STATISTICS_VERSION,
                "observatory_version": OBSERVATORY_VERSION,
                "runs": runs,
            }
        return data
    return _skeleton()


def render(store):
    """Recompute derived sections from `runs` and return the canonical JSON text."""
    runs = store.get("runs", {})
    out = {
        "statistics_version": STATISTICS_VERSION,
        "observatory_version": OBSERVATORY_VERSION,
        "runs": runs,
        "aggregates": _aggregate(runs),
        "defect_flow": _defect_flow(runs),
    }
    return json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def merge_update(stats_path, screens):
    """Insert/replace this run's entry and rewrite the store. Idempotent: the same
    screens produce byte-identical output. Returns the rendered store text."""
    store = load(stats_path)
    session_id = screens.get("session_id")
    store["runs"][session_id] = screens_to_entry(screens)
    text = render(store)
    os.makedirs(os.path.dirname(os.path.abspath(stats_path)), exist_ok=True)
    with open(stats_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text
