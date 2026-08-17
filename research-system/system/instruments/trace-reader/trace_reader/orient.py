"""Orientation view — Stage-3 computed features (C6 §3), deterministic, no LLM.

Over an *extracted* trace (a dir holding trace.jsonl + actors.json), for each
actor: the first-k actions, the first read of a task-relevant file, how many reads
happened before it, and how many off-task reads there were. This is the mechanical
feature layer the orientation study sits on ("first-k actions vs. task-relevant
file set, time-to-first-relevant-read, off-task read count"). Judgment/LLM digest
is a separate, later stage.

Targets are re-derived from each tool call's raw_ptr row (the exact input), falling
back to the digest hint when the source bundle is not reachable. Relevance is
fnmatch of the target path against the caller-supplied globs (`*` spans `/`).
"""

from __future__ import annotations

import fnmatch
import json
import os

from .digest import collapse, duration_ms

ORIENT_VERSION = "trace-reader-orient/1.0.0"

# tool_call names that read files from disk (C6 §3). Module constant by design so
# the read-set is explicit and auditable; extend here if new read tools appear.
READ_TOOLS = ("Read", "Grep", "Glob", "NotebookRead")

_CODEX_SHELL_READ_SURFACES = ("exec", "exec_command")
_CODEX_READ_FAST_FOLLOW = (
    "classify file reads issued through exec/exec_command shell commands"
)

# path-like input fields, in priority order, used to name a read's file target
_PATH_FIELDS = ("file_path", "notebook_path", "path")
# broader field set for naming any action's target in the first-k summary
_ACTION_FIELDS = ("file_path", "notebook_path", "path", "pattern", "command",
                  "url", "query", "prompt", "description")


class _RawCache:
    """Lazily re-reads raw_ptr rows to recover exact tool inputs."""

    def __init__(self):
        self._files = {}

    def input_for(self, ev):
        ptr = ev.get("raw_ptr") or {}
        path, line = ptr.get("file"), ptr.get("line")
        if not path or not line:
            return None
        segs = self._files.get(path)
        if segs is None:
            try:
                with open(path, "rb") as fh:
                    segs = fh.read().split(b"\n")
            except OSError:
                segs = []
            self._files[path] = segs
        if line < 1 or line > len(segs):
            return None
        try:
            obj = json.loads(segs[line - 1])
        except Exception:
            return None
        tid = ev.get("tool_use_id")
        for b in (obj.get("message", {}) or {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == tid:
                return b.get("input")
        return None


def _read_target(inp, hint):
    """The file a read touched: a path field from the raw input, else the hint."""
    if isinstance(inp, dict):
        for f in _PATH_FIELDS:
            v = inp.get(f)
            if isinstance(v, str) and v:
                return v
        # a pathless Grep/Glob searches the cwd — no single-file target
        return None
    return hint or None


def _action_target(name, inp, hint):
    if isinstance(inp, dict):
        for f in _ACTION_FIELDS:
            v = inp.get(f)
            if isinstance(v, str) and v:
                return collapse(v)[:120]
    return hint


def _is_relevant(path, globs):
    return path is not None and any(fnmatch.fnmatch(path, g) for g in globs)


def _load_trace(trace_dir):
    events = []
    with open(os.path.join(trace_dir, "trace.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _load_meta(trace_dir):
    try:
        with open(os.path.join(trace_dir, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return {}


def _detected_format(meta):
    """Read the extractor's additive format provenance, tolerating old traces."""
    fmt = meta.get("format")
    if isinstance(fmt, dict):
        detected = fmt.get("detected")
        return detected if isinstance(detected, str) else None
    # A string is accepted for hand-built/older transition fixtures, while new
    # extractors use the provenance object above.
    return fmt if isinstance(fmt, str) else None


def _coverage(source_format):
    """Mechanical orientation coverage, attached to every actor."""
    codex = source_format == "codex"
    return {
        "source_format": source_format,
        "read_metrics": "lower_bound" if codex else "unknown",
        "complete": False if codex else None,
        "shell_reads": "unclassified" if codex else "unknown",
        "unclassified_read_surfaces": list(_CODEX_SHELL_READ_SURFACES) if codex else [],
        "first_k_actions": "supported",
        "fast_follow": _CODEX_READ_FAST_FOLLOW if codex else None,
    }


def _actor_order(trace_dir, events):
    order_path = os.path.join(trace_dir, "actors.json")
    try:
        with open(order_path, encoding="utf-8") as fh:
            return [a["actor"] for a in json.load(fh)]
    except OSError:
        # fall back to first-seen order in the trace
        seen = []
        for e in events:
            if e["actor"] not in seen:
                seen.append(e["actor"])
        return seen


def _compute_actor(actor, events, globs, k, raw, source_format):
    evs = sorted((e for e in events if e["actor"] == actor), key=lambda e: e["step"])
    actor_start_ts = next((e.get("ts") for e in evs if e.get("ts")), None)

    reads = []
    for e in evs:
        if e.get("kind") == "tool_call" and e.get("name") in READ_TOOLS:
            hint = (e.get("input_digest") or {}).get("hint")
            target = _read_target(raw.input_for(e), hint)
            reads.append({"step": e["step"], "ts": e.get("ts"), "target": target})

    first_rel = next((r for r in reads if _is_relevant(r["target"], globs)), None)
    if first_rel is not None:
        first_relevant_read = {
            "step": first_rel["step"],
            "ts": first_rel["ts"],
            "path": first_rel["target"],
            "latency_ms": duration_ms(actor_start_ts, first_rel["ts"]),
        }
        before = [r for r in reads if r["step"] < first_rel["step"]]
    else:
        first_relevant_read = None
        before = list(reads)   # every read happened; none was relevant

    reads_before = {
        "count": len(before),
        "paths": [r["target"] for r in before if r["target"] is not None],
    }
    off = [r for r in reads if r["target"] is not None and not _is_relevant(r["target"], globs)]

    actions = [e for e in evs if e.get("kind") == "tool_call"][:k]
    first_k = []
    for e in actions:
        hint = (e.get("input_digest") or {}).get("hint")
        inp = raw.input_for(e)
        if e.get("name") in READ_TOOLS:
            target = _read_target(inp, hint)
        else:
            target = _action_target(e.get("name"), inp, hint)
        first_k.append({"step": e["step"], "tool": e.get("name"), "target": target})

    return {
        "coverage": _coverage(source_format),
        "read_count": len(reads),
        "first_k_actions": first_k,
        "first_relevant_read": first_relevant_read,
        "reads_before_first_relevant": reads_before,
        "off_task_read_count": len(off),
        "off_task_paths": sorted({r["target"] for r in off}),
    }


def orient(trace_dir, relevant_globs, k=15):
    events = _load_trace(trace_dir)
    source_format = _detected_format(_load_meta(trace_dir))
    raw = _RawCache()
    actors = {}
    for actor in _actor_order(trace_dir, events):
        actors[actor] = _compute_actor(
            actor, events, list(relevant_globs), k, raw, source_format
        )
    result = {
        "orient_version": ORIENT_VERSION,
        "trace_dir": os.path.abspath(trace_dir),
        "format": source_format,
        "params": {"relevant": list(relevant_globs), "k": k, "read_tools": list(READ_TOOLS)},
        "actors": actors,
    }
    with open(os.path.join(trace_dir, "orient.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False, indent=2))
        fh.write("\n")
    return result
