"""Codex rollout adapter -> the canonical trace-reader four-file seam.

Codex rollouts are chronological event logs, not Claude parentUuid trees.  Fork
and child files can begin with a filtered replay of ancestor history, so each
actor is restricted to the turn segment owned by its first session_meta row.
The physical-order assumption is guarded by a fail-closed structural tripwire;
branches are never reconstructed from insufficient evidence.
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from . import EXTRACTOR_VERSION
from . import parse
from .digest import digest


class CodexOrderInvariantError(RuntimeError):
    """The empty-branches/lived-order precondition cannot safely be applied."""


class CodexFormatError(RuntimeError):
    """A structurally Codex input is ambiguous or internally inconsistent."""


_CALL_TYPES = {"function_call", "custom_tool_call", "tool_search_call", "web_search_call"}
_OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output", "tool_search_output"}
_END_TYPES = {"mcp_tool_call_end", "patch_apply_end", "web_search_end"}
_DUPLICATE_MESSAGE_TYPES = {"user_message", "agent_message"}
_BRANCH_KEYS = {
    "parentUuid", "parent_uuid", "parent_id", "branch_id", "branchId",
    "active_path", "is_active", "is_abandoned", "abandoned",
}


@dataclass
class PreparedActor:
    actor: str
    path: str
    parsed: parse.ParsedFile
    session_id: str | None
    session_line: int | None
    session_obj: dict | None
    owned_rows: list
    turn_for_line: dict
    order_info: dict
    parent_actor: str | None = None
    join_line: int | None = None
    join_path: str | None = None
    orphan: bool = False
    suspected_reason: str | None = None


def _ptr(path, line):
    return {"file": path, "line": line}


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso_epoch(value, milliseconds=False):
    if not isinstance(value, (int, float)):
        return None
    seconds = value / 1000.0 if milliseconds else value
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _decode_jsonish(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _fail(actor, line, reason):
    loc = f"{actor.path}:{line}" if line is not None else actor.path
    raise CodexOrderInvariantError(
        f"codex_order_invariant_violated at {loc}: {reason}; "
        "empty branches and zero branch counts are unsafe"
    )


def _validate_envelope_order(actor):
    previous = None
    previous_inner = None
    for line, obj in actor.parsed.rows:
        if not isinstance(obj, dict):
            _fail(actor, line, "row is not a JSON object")
        if set(obj) - {"timestamp", "type", "payload"}:
            # New envelope fields are allowed, but ancestry/disposition fields are not.
            if (set(obj) - {"timestamp", "type", "payload"}) & _BRANCH_KEYS:
                _fail(actor, line, "new top-level ancestry/branch/disposition signal")
        ts = _parse_iso(obj.get("timestamp"))
        if obj.get("timestamp") is not None and ts is None:
            _fail(actor, line, "unparseable envelope timestamp")
        if ts is not None and previous is not None and ts < previous:
            _fail(actor, line, "envelope timestamp regressed in physical row order")
        if ts is not None:
            previous = ts
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        inner = _inner_time(payload)
        if inner is not None and previous_inner is not None and inner < previous_inner:
            _fail(actor, line, "explicit event-origin time regressed in physical row order")
        if inner is not None:
            previous_inner = inner


def _session_prefix(actor):
    metas = []
    saw_nonmeta = False
    for line, obj in actor.parsed.rows:
        is_meta = isinstance(obj, dict) and obj.get("type") == "session_meta"
        if is_meta:
            if saw_nonmeta:
                _fail(actor, line, "session_meta appeared after non-session rows")
            metas.append((line, obj))
        else:
            saw_nonmeta = True
    if not metas:
        if actor.parsed.rows:
            raise CodexFormatError(f"{actor.path}: no session_meta prefix")
        return None, None, None, []

    line, obj = metas[0]
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        raise CodexFormatError(f"{actor.path}:{line}: session_meta payload is not an object")
    session_id = payload.get("id") or payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise CodexFormatError(f"{actor.path}:{line}: session_meta has no id/session_id")
    return line, obj, session_id, metas


def _owned_rows(actor, metas):
    """Exclude a fork/child file's replayed ancestor prefix from actor behavior."""
    if not metas:
        return [], {"owned_start_line": None, "inherited_rows": 0, "history_mode": "empty"}
    current_payload = metas[0][1].get("payload") or {}
    current_time = _parse_iso(current_payload.get("timestamp"))
    starts = []
    for idx, (line, obj) in enumerate(actor.parsed.rows):
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if obj.get("type") == "event_msg" and isinstance(payload, dict) \
                and payload.get("type") == "task_started":
            starts.append((idx, line, obj, payload.get("started_at")))

    start = None
    if len(metas) == 1:
        start = starts[0] if starts else None
        if start is None and any(obj.get("type") != "session_meta" for _line, obj in actor.parsed.rows):
            _fail(actor, metas[-1][0], "non-session rows exist before any task_started boundary")
    else:
        if current_time is None:
            _fail(actor, metas[0][0], "cannot locate owned segment without session timestamp")
        cutoff = current_time.timestamp()
        for candidate in starts:
            started = candidate[3]
            if isinstance(started, (int, float)) and started >= cutoff:
                start = candidate
                break

    if start is None:
        return [], {
            "owned_start_line": None,
            "inherited_rows": max(0, len(actor.parsed.rows) - len(metas)),
            "history_mode": "prefix_before_owned_turn",
        }

    idx, line, _obj, _started = start
    return actor.parsed.rows[idx:], {
        "owned_start_line": line,
        "inherited_rows": max(0, idx - len(metas)),
        "history_mode": "filtered_replay_then_owned" if len(metas) > 1 else "owned_only",
    }


def _inner_time(payload):
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("started_at"), (int, float)):
        return float(payload["started_at"])
    if isinstance(payload.get("completed_at"), (int, float)):
        return float(payload["completed_at"])
    if isinstance(payload.get("occurred_at_ms"), (int, float)):
        return float(payload["occurred_at_ms"]) / 1000.0
    return None


def _validate_owned_order(actor):
    direct_calls = {}
    direct_outputs = {}
    turn_for_line = {}
    starts = OrderedDict()
    terminals = {}
    rolled_back = set()
    open_turn = None
    last_terminal = None
    last_inner = None
    implicit_boundaries = 0
    rollback_count = 0

    for line, obj in actor.owned_rows:
        payload = obj.get("payload") if isinstance(obj, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        if set(payload) & _BRANCH_KEYS:
            _fail(actor, line, "new payload ancestry/branch/disposition signal")

        inner = _inner_time(payload)
        if inner is not None and last_inner is not None and inner < last_inner:
            _fail(actor, line, "explicit event-origin time regressed")
        if inner is not None:
            last_inner = inner

        outer_type = obj.get("type")
        ptype = payload.get("type")
        if outer_type == "event_msg" and ptype == "task_started":
            tid = payload.get("turn_id")
            if not isinstance(tid, str) or not tid:
                _fail(actor, line, "task_started has no turn_id")
            if tid in starts or tid in rolled_back:
                _fail(actor, line, f"turn_id {tid} was started more than once or after rollback")
            if open_turn is not None:
                implicit_boundaries += 1
            starts[tid] = line
            open_turn = tid
            last_terminal = None
        turn_for_line[line] = open_turn

        if outer_type == "event_msg" and ptype in ("task_complete", "turn_aborted"):
            tid = payload.get("turn_id")
            if tid not in starts:
                _fail(actor, line, f"terminal for unknown turn_id {tid}")
            if tid in terminals:
                _fail(actor, line, f"duplicate terminal for turn_id {tid}")
            if open_turn is not None and tid != open_turn:
                _fail(actor, line, f"terminal for {tid} interleaved into open turn {open_turn}")
            terminals[tid] = (line, ptype)
            last_terminal = (tid, line, ptype)
            open_turn = None

        if outer_type == "event_msg" and ptype == "thread_rolled_back":
            n = payload.get("num_turns")
            if n != 1:
                _fail(actor, line, f"rollback num_turns={n!r}; only observed value 1 is safe")
            if last_terminal is None or last_terminal[2] != "turn_aborted":
                _fail(actor, line, "rollback is not preceded by the affected aborted turn")
            rolled_back.add(last_terminal[0])
            rollback_count += 1
            last_terminal = None

        tid = payload.get("turn_id")
        if isinstance(tid, str) and tid in rolled_back \
                and not (outer_type == "event_msg" and ptype in ("turn_aborted", "thread_rolled_back")):
            _fail(actor, line, f"rolled-back turn_id {tid} reappeared after rollback")

        if outer_type == "response_item" and ptype in _CALL_TYPES:
            cid = payload.get("call_id")
            if not isinstance(cid, str) or not cid:
                _fail(actor, line, f"{ptype} has no call_id")
            if cid in direct_calls:
                _fail(actor, line, f"duplicate call_id {cid}")
            direct_calls[cid] = line
        if outer_type == "response_item" and ptype in _OUTPUT_TYPES:
            cid = payload.get("call_id")
            if not isinstance(cid, str) or not cid:
                _fail(actor, line, f"{ptype} has no call_id")
            if cid in direct_outputs:
                _fail(actor, line, f"duplicate output call_id {cid}")
            direct_outputs[cid] = line

    for cid, out_line in direct_outputs.items():
        call_line = direct_calls.get(cid)
        if call_line is not None and out_line < call_line:
            _fail(actor, out_line, f"result {cid} precedes its call at line {call_line}")

    return turn_for_line, {
        "status": "verified",
        "tripwire": "codex-order/1",
        "visible_order_semantics": "chronological visible rows; fork history may be filtered",
        "turns_started": len(starts),
        "turns_terminal": len(terminals),
        "open_or_implicit_turns": len(starts) - len(terminals),
        "implicit_turn_boundaries": implicit_boundaries,
        "rollbacks": rollback_count,
        "rolled_back_turn_ids": sorted(rolled_back),
        "residual_limit": "untimed semantic interleaving without structural signals is not detectable",
    }


def _prepare(actor, path, parent_actor=None, join_line=None, join_path=None,
             orphan=False, suspected_reason=None):
    prepared = PreparedActor(
        actor=actor,
        path=os.path.abspath(path),
        parsed=parse.parse_file(os.path.abspath(path)),
        session_id=None,
        session_line=None,
        session_obj=None,
        owned_rows=[],
        turn_for_line={},
        order_info={},
        parent_actor=parent_actor,
        join_line=join_line,
        join_path=join_path,
        orphan=orphan,
        suspected_reason=suspected_reason,
    )
    if prepared.parsed.unparseable_lines:
        _fail(
            prepared, prepared.parsed.unparseable_lines[0],
            f"unparseable non-final JSON row(s) {prepared.parsed.unparseable_lines}",
        )
    if not prepared.parsed.rows:
        prepared.order_info = {"status": "empty_prefix", "tripwire": "codex-order/1"}
        return prepared
    _validate_envelope_order(prepared)
    line, obj, sid, metas = _session_prefix(prepared)
    prepared.session_line, prepared.session_obj, prepared.session_id = line, obj, sid
    prepared.owned_rows, boundary = _owned_rows(prepared, metas)
    prepared.turn_for_line, order = _validate_owned_order(prepared)
    prepared.order_info = {**boundary, **order}
    return prepared


def _peek_meta(path):
    try:
        with open(path, "rb") as fh:
            line = fh.readline()
        obj = json.loads(line)
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "session_meta" \
            or not isinstance(obj.get("payload"), dict):
        return None
    return obj


def _parent_id(payload):
    direct = payload.get("parent_thread_id")
    if isinstance(direct, str):
        return direct
    source = payload.get("source")
    if isinstance(source, dict):
        spawn = ((source.get("subagent") or {}).get("thread_spawn") or {})
        if isinstance(spawn.get("parent_thread_id"), str):
            return spawn["parent_thread_id"]
    return None


def _candidate_index(path, cache):
    directory = os.path.dirname(path)
    if directory in cache:
        return cache[directory]
    candidates = set(glob.glob(os.path.join(directory, "rollout-*.jsonl")))
    for child_dir in ("children", "subagents"):
        candidates.update(glob.glob(os.path.join(directory, child_dir, "rollout-*.jsonl")))
    index = {}
    for candidate in sorted(candidates):
        meta = _peek_meta(candidate)
        if meta is None:
            continue
        payload = meta["payload"]
        sid = payload.get("id") or payload.get("session_id")
        if isinstance(sid, str):
            index[sid] = (os.path.abspath(candidate), meta)
    cache[directory] = index
    return index


def _activities(actor):
    found = OrderedDict()
    for line, obj in actor.owned_rows:
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if obj.get("type") != "event_msg" or not isinstance(payload, dict) \
                or payload.get("type") != "sub_agent_activity":
            continue
        if payload.get("kind") != "started":
            continue
        tid = payload.get("agent_thread_id")
        if isinstance(tid, str) and tid not in found:
            found[tid] = {
                "thread_id": tid,
                "line": line,
                "path": actor.path,
                "kind": payload.get("kind"),
                "agent_path": payload.get("agent_path"),
            }
    return found


def _after_watermark(meta, parent):
    child_time = _parse_iso((meta.get("payload") or {}).get("timestamp"))
    parent_time = _parse_iso(parent.parsed.last_timestamp)
    return bool(child_time and parent_time and child_time > parent_time)


def _discover(bundle_path):
    main = _prepare("main", bundle_path)
    actors = [main]
    by_session = {main.session_id: main} if main.session_id else {}
    queue = [main]
    cache = {}
    linked = []
    missing = []
    suspected = []

    while queue:
        parent = queue.pop(0)
        if parent.session_id is None:
            continue
        index = _candidate_index(parent.path, cache)
        activity = _activities(parent)

        for tid, rec in activity.items():
            hit = index.get(tid)
            if hit is None:
                missing.append({
                    "parent_actor": parent.actor,
                    "thread_id": tid,
                    "activity_raw_ptr": _ptr(rec["path"], rec["line"]),
                })
                continue
            child_path, child_meta = hit
            actual_parent = _parent_id(child_meta.get("payload") or {})
            if actual_parent != parent.session_id:
                _fail(parent, rec["line"], f"child {tid} session_meta parent is {actual_parent!r}")
            if tid in by_session:
                continue
            child = _prepare(
                f"agent-{tid}", child_path, parent_actor=parent.actor,
                join_line=rec["line"], join_path=rec["path"], orphan=False,
            )
            by_session[tid] = child
            actors.append(child)
            queue.append(child)
            linked.append({
                "parent_actor": parent.actor,
                "actor": child.actor,
                "thread_id": tid,
                "file": child.path,
                "activity_raw_ptr": _ptr(rec["path"], rec["line"]),
            })

        for tid, (candidate, meta) in sorted(index.items()):
            if tid in by_session or tid in activity or candidate == parent.path:
                continue
            if _parent_id(meta.get("payload") or {}) != parent.session_id:
                continue
            out_of_window = _after_watermark(meta, parent)
            observed = parse.parse_file(candidate)
            rec = {
                "parent_actor": parent.actor,
                "thread_id": tid,
                "file": candidate,
                "reason": "session_meta parentage without in-prefix activity",
                "session_meta_raw_ptr": _ptr(candidate, 1),
                "out_of_window": out_of_window,
                "source": {
                    "file": observed.path, "sha256": observed.sha256,
                    "lines": observed.lines, "last_timestamp": observed.last_timestamp,
                },
                "watermark": {
                    "file": observed.path, "lines": observed.lines,
                    "last_timestamp": observed.last_timestamp,
                    "unparseable_lines": observed.unparseable_lines,
                    "truncated_final_line": observed.truncated_final_line,
                },
            }
            suspected.append(rec)
            if out_of_window:
                continue
            child = _prepare(
                f"agent-{tid}", candidate, parent_actor=parent.actor,
                orphan=True, suspected_reason=rec["reason"],
            )
            by_session[tid] = child
            actors.append(child)
            queue.append(child)

    return actors, {"linked": linked, "missing_linked": missing,
                    "suspected_unlinked": suspected}


def _text_blocks(payload):
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, list):
        return []
    out = []
    for idx, block in enumerate(content):
        if not isinstance(block, dict):
            out.append((idx, None, None, block))
            continue
        out.append((idx, block.get("type"), block.get("text"), block))
    return out


def _event_text(payload):
    for key in ("message", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def _event_ts(obj, payload):
    ptype = payload.get("type") if isinstance(payload, dict) else None
    if ptype == "task_started":
        return _iso_epoch(payload.get("started_at"))
    if ptype in ("task_complete", "turn_aborted"):
        return _iso_epoch(payload.get("completed_at"))
    if ptype == "sub_agent_activity":
        return _iso_epoch(payload.get("occurred_at_ms"), milliseconds=True)
    return obj.get("timestamp")


def _event_ts_source(payload):
    ptype = payload.get("type") if isinstance(payload, dict) else None
    if ptype == "task_started" and _iso_epoch(payload.get("started_at")) is not None:
        return "payload.started_at"
    if ptype in ("task_complete", "turn_aborted") \
            and _iso_epoch(payload.get("completed_at")) is not None:
        return "payload.completed_at"
    if ptype == "sub_agent_activity" \
            and _iso_epoch(payload.get("occurred_at_ms"), milliseconds=True) is not None:
        return "payload.occurred_at_ms"
    return "envelope.timestamp"


def _duration_ms(payload):
    value = payload.get("duration_ms")
    if isinstance(value, int):
        return value
    duration = payload.get("duration")
    if isinstance(duration, dict):
        secs, nanos = duration.get("secs"), duration.get("nanos")
        if isinstance(secs, int) and isinstance(nanos, int):
            return secs * 1000 + round(nanos / 1_000_000)
    return None


def _result_value(payload):
    ptype = payload.get("type")
    if ptype in _OUTPUT_TYPES:
        if ptype == "tool_search_output":
            return {k: payload.get(k) for k in ("tools", "execution", "status")}
        return _decode_jsonish(payload.get("output"))
    if ptype == "mcp_tool_call_end":
        return payload.get("result")
    if ptype == "patch_apply_end":
        return {k: payload.get(k) for k in ("success", "status", "changes", "stdout", "stderr")}
    if ptype == "web_search_end":
        return {k: payload.get(k) for k in ("action", "query")}
    return payload


def _is_error(value):
    value = _decode_jsonish(value)
    if not isinstance(value, dict):
        return False
    if isinstance(value.get("is_error"), bool):
        return value["is_error"]
    if value.get("success") is False:
        return True
    if isinstance(value.get("exit_code"), int) and value["exit_code"] != 0:
        return True
    return False


def _call_input(payload):
    ptype = payload.get("type")
    if ptype == "function_call":
        return _decode_jsonish(payload.get("arguments"))
    if ptype == "custom_tool_call":
        return _decode_jsonish(payload.get("input"))
    if ptype == "tool_search_call":
        return _decode_jsonish(payload.get("arguments"))
    if ptype == "web_search_call":
        return payload.get("action")
    return None


def _call_name(payload):
    ptype = payload.get("type")
    if ptype in ("function_call", "custom_tool_call"):
        return payload.get("name")
    if ptype == "tool_search_call":
        return "tool_search"
    if ptype == "web_search_call":
        return "web_search"
    return ptype


def _self_contained_tool(payload):
    ptype = payload.get("type")
    if ptype == "mcp_tool_call_end":
        invocation = payload.get("invocation") or {}
        name = ".".join(str(x) for x in (invocation.get("server"), invocation.get("tool")) if x)
        return name or "mcp_tool_call", invocation.get("arguments"), payload.get("result")
    if ptype == "patch_apply_end":
        return "apply_patch", None, _result_value(payload)
    if ptype == "web_search_end":
        return "web_search", {"query": payload.get("query"), "action": payload.get("action")}, _result_value(payload)
    return ptype, None, _result_value(payload)


def _add(events, line, intra, obj, actor, kind, fields, ts=None,
         ts_source="envelope.timestamp"):
    event = {"step": None, "ts": ts if ts is not None else obj.get("timestamp"),
             "ts_source": ts_source, "actor": actor.actor, "kind": kind}
    event.update(fields)
    event["raw_ptr"] = _ptr(actor.path, line)
    events.append((line, intra, event))


def _pair_maps(actor):
    calls, outputs, ends = {}, {}, {}
    for line, obj in actor.owned_rows:
        payload = obj.get("payload") if isinstance(obj, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        ptype = payload.get("type")
        cid = payload.get("call_id")
        if obj.get("type") == "response_item" and ptype in _CALL_TYPES:
            calls[cid] = (line, obj)
        elif obj.get("type") == "response_item" and ptype in _OUTPUT_TYPES:
            outputs[cid] = (line, obj)
        elif obj.get("type") == "event_msg" and ptype in _END_TYPES and isinstance(cid, str):
            ends.setdefault(cid, (line, obj))
    results = dict(outputs)
    for cid, value in ends.items():
        if cid in calls and cid not in results:
            results[cid] = value
    return calls, results, outputs, ends


def _message_source_texts(actor):
    by_turn_role = {}
    for line, obj in actor.owned_rows:
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if obj.get("type") != "response_item" or not isinstance(payload, dict) \
                or payload.get("type") != "message":
            continue
        role = payload.get("role")
        turn = actor.turn_for_line.get(line)
        for _idx, btype, text, _block in _text_blocks(payload):
            if isinstance(text, str) and text and btype in ("input_text", "output_text"):
                by_turn_role.setdefault((turn, role), set()).add(
                    digest(text)["sha256"]
                )
    return by_turn_role


def _reasoning_modes(actor):
    summary_hashes = {}
    for line, obj in actor.owned_rows:
        payload = obj.get("payload") if isinstance(obj, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        turn = actor.turn_for_line.get(line)
        if obj.get("type") == "response_item" and payload.get("type") == "reasoning":
            for item in payload.get("summary") or []:
                if isinstance(item, dict) and item.get("type") == "summary_text" \
                    and isinstance(item.get("text"), str) and item["text"]:
                    summary_hashes.setdefault(turn, set()).add(digest(item["text"])["sha256"])
    return summary_hashes


def _map_actor(actor):
    events = []
    calls, results, direct_outputs, ends = _pair_maps(actor)
    response_texts = _message_source_texts(actor)
    summary_hashes = _reasoning_modes(actor)

    if actor.session_obj is not None:
        payload = actor.session_obj.get("payload") or {}
        fields = {
            "subtype": "session_start",
            "session_id": actor.session_id,
            "cwd": payload.get("cwd"),
            "model_provider": payload.get("model_provider"),
            "cli_version": payload.get("cli_version"),
            "parent_thread_id": _parent_id(payload),
            "forked_from_id": payload.get("forked_from_id"),
        }
        _add(events, actor.session_line, 0, actor.session_obj, actor, "lifecycle", fields,
             ts=payload.get("timestamp") or actor.session_obj.get("timestamp"),
             ts_source="payload.timestamp" if payload.get("timestamp") else "envelope.timestamp")
        if payload.get("forked_from_id"):
            _add(events, actor.session_line, 1, actor.session_obj, actor, "lifecycle", {
                "subtype": "fork", "forked_from_id": payload.get("forked_from_id"),
                "parent_thread_id": _parent_id(payload),
            }, ts=payload.get("timestamp") or actor.session_obj.get("timestamp"),
                ts_source="payload.timestamp" if payload.get("timestamp") else "envelope.timestamp")

    for line, obj in actor.owned_rows:
        payload = obj.get("payload") if isinstance(obj, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        outer, ptype = obj.get("type"), payload.get("type")
        turn = actor.turn_for_line.get(line)

        if outer == "response_item" and ptype == "message":
            role = payload.get("role")
            for idx, btype, text, block in _text_blocks(payload):
                if role == "user" and btype == "input_text" and isinstance(text, str):
                    _add(events, line, idx, obj, actor, "user_msg", {"text": text})
                elif role == "assistant" and btype == "output_text" and isinstance(text, str):
                    _add(events, line, idx, obj, actor, "assistant_text", {"text": text})
                elif role in ("developer", "system"):
                    _add(events, line, idx, obj, actor, "lifecycle", {
                        "subtype": "context_message", "role": role,
                        "content_digest": digest(block),
                    })
                else:
                    _add(events, line, idx, obj, actor, "lifecycle", {
                        "subtype": "message_content", "role": role,
                        "content_type": btype, "content_digest": digest(block),
                    })
            continue

        if outer == "response_item" and ptype == "agent_message":
            _add(events, line, 0, obj, actor, "lifecycle", {
                "subtype": "agent_message", "author": payload.get("author"),
                "recipient": payload.get("recipient"),
                "content_digest": digest(payload.get("content")),
            })
            continue

        if outer == "response_item" and ptype == "reasoning":
            emitted = False
            for idx, item in enumerate(payload.get("summary") or []):
                if isinstance(item, dict) and item.get("type") == "summary_text" \
                        and isinstance(item.get("text"), str) and item["text"]:
                    _add(events, line, idx, obj, actor, "thinking", {
                        "text": item["text"],
                        "reasoning_source": "response_item.reasoning.summary_text",
                    })
                    emitted = True
            if not emitted and payload.get("encrypted_content"):
                _add(events, line, 0, obj, actor, "thinking", {
                    "redacted": True,
                    "reasoning_source": "response_item.reasoning.encrypted_content",
                })
                emitted = True
            if emitted:
                continue

        if outer == "response_item" and ptype in _CALL_TYPES:
            cid = payload.get("call_id")
            result = results.get(cid)
            result_payload = (result[1].get("payload") or {}) if result else None
            result_value = _result_value(result_payload) if result_payload else None
            fields = {
                "name": _call_name(payload),
                "tool_use_id": cid,
                "input_digest": digest(_call_input(payload)),
                "result_digest": digest(result_value) if result else None,
                "duration_ms": _duration_ms(result_payload) if result_payload else None,
                "tokens": None,
            }
            if result:
                fields["result_raw_ptr"] = _ptr(actor.path, result[0])
                if _is_error(result_value):
                    fields["result_is_error"] = True
            _add(events, line, 0, obj, actor, "tool_call", fields)
            continue

        if outer == "response_item" and ptype in _OUTPUT_TYPES:
            cid = payload.get("call_id")
            if cid in calls:
                continue
            _add(events, line, 0, obj, actor, "lifecycle", {
                "subtype": "unmatched_tool_result", "tool_use_id": cid,
                "result_digest": digest(_result_value(payload)),
            })
            continue

        if outer == "event_msg" and ptype in _DUPLICATE_MESSAGE_TYPES:
            text = _event_text(payload)
            role = "user" if ptype == "user_message" else "assistant"
            known = response_texts.get((turn, role), set())
            if isinstance(text, str) and digest(text)["sha256"] in known:
                continue
            _add(events, line, 0, obj, actor, "lifecycle", {
                "subtype": f"unmatched_{ptype}", "role": role,
                "payload_digest": digest(payload),
            })
            continue

        if outer == "event_msg" and ptype == "agent_reasoning":
            text = payload.get("text")
            text_hash = digest(text)["sha256"] if isinstance(text, str) and text else None
            if isinstance(text, str) and text \
                    and text_hash not in summary_hashes.get(turn, set()):
                _add(events, line, 0, obj, actor, "thinking", {
                    "text": text,
                    "reasoning_source": "event_msg.agent_reasoning.text",
                })
            continue

        if outer == "event_msg" and ptype == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else None
            _add(events, line, 0, obj, actor, "lifecycle", {
                "subtype": "usage_snapshot",
                "last_token_usage": (info or {}).get("last_token_usage"),
                "total_token_usage": (info or {}).get("total_token_usage"),
                "model_context_window": (info or {}).get("model_context_window"),
            })
            continue

        if outer == "event_msg" and ptype in _END_TYPES:
            cid = payload.get("call_id")
            if cid in calls:
                continue
            name, inp, result_value = _self_contained_tool(payload)
            _add(events, line, 0, obj, actor, "lifecycle", {
                "subtype": "tool_end", "tool_end_type": ptype,
                "name": name, "tool_use_id": cid,
                "input_digest": digest(inp) if inp is not None else None,
                "result_digest": digest(result_value),
                "duration_ms": _duration_ms(payload),
                "result_is_error": _is_error(result_value),
            }, ts=_event_ts(obj, payload), ts_source=_event_ts_source(payload))
            continue

        if outer == "event_msg":
            if ptype == "task_started":
                fields = {"subtype": "turn_start", "turn_id": payload.get("turn_id")}
            elif ptype == "task_complete":
                fields = {"subtype": "turn_complete", "turn_id": payload.get("turn_id"),
                          "duration_ms": payload.get("duration_ms")}
            elif ptype == "turn_aborted":
                fields = {"subtype": "turn_aborted", "turn_id": payload.get("turn_id"),
                          "duration_ms": payload.get("duration_ms"), "reason": payload.get("reason")}
            elif ptype == "thread_rolled_back":
                fields = {"subtype": "rewind", "num_turns": payload.get("num_turns")}
            elif ptype == "context_compacted":
                fields = {"subtype": "context_compacted"}
            elif ptype == "sub_agent_activity":
                fields = {
                    "subtype": "subagent_activity", "activity_kind": payload.get("kind"),
                    "agent_path": payload.get("agent_path"),
                    "child_thread_id": payload.get("agent_thread_id"),
                }
            elif ptype == "thread_settings_applied":
                fields = {"subtype": "settings_applied", "settings_digest": digest(payload)}
            else:
                fields = {"subtype": "codex_event", "codex_subtype": ptype,
                          "payload_digest": digest(payload)}
            _add(events, line, 0, obj, actor, "lifecycle", fields,
                 ts=_event_ts(obj, payload), ts_source=_event_ts_source(payload))
            continue

        if outer == "turn_context":
            fields = {
                "subtype": "turn_context", "turn_id": payload.get("turn_id"),
                "model": payload.get("model"), "effort": payload.get("effort"),
                "cwd": payload.get("cwd"),
            }
            if payload.get("summary") is not None:
                fields["summary_digest"] = digest(payload.get("summary"))
            _add(events, line, 0, obj, actor, "lifecycle", fields)
            continue

        if outer == "compacted":
            _add(events, line, 0, obj, actor, "lifecycle", {
                "subtype": "compact_boundary",
                "window_id": payload.get("window_id"),
                "previous_window_id": payload.get("previous_window_id"),
                "window_number": payload.get("window_number"),
                "replacement_history_digest": digest(payload.get("replacement_history")),
                "summary_digest": digest(payload.get("message")),
            })
            continue

        if outer == "world_state":
            _add(events, line, 0, obj, actor, "lifecycle", {
                "subtype": "world_state", "state_digest": digest(payload),
            })
            continue

        if outer == "inter_agent_communication_metadata":
            _add(events, line, 0, obj, actor, "lifecycle", {
                "subtype": "inter_agent_context", "metadata_digest": digest(payload),
            })
            continue

        _add(events, line, 0, obj, actor, "lifecycle", {
            "subtype": "codex_unknown", "source_type": outer,
            "codex_subtype": ptype, "payload_digest": digest(payload),
        })

    events.sort(key=lambda item: (item[0], item[1]))
    serialized = []
    for step, (_line, _intra, event) in enumerate(events, 1):
        event["step"] = step
        if actor.orphan:
            event["orphan"] = True
        serialized.append(event)
    return serialized


def _usage(actor):
    latest = None
    for line, obj in actor.owned_rows:
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if obj.get("type") == "event_msg" and isinstance(payload, dict) \
                and payload.get("type") == "token_count" \
                and isinstance(payload.get("info"), dict):
            latest = (line, payload["info"])
    if latest is None:
        return None
    line, info = latest
    return {
        "last_token_usage": info.get("last_token_usage"),
        "total_token_usage": info.get("total_token_usage"),
        "model_context_window": info.get("model_context_window"),
        "raw_ptr": _ptr(actor.path, line),
    }


def _watermark(actor):
    p = actor.parsed
    return {
        "file": p.path,
        "lines": p.lines,
        "last_timestamp": p.last_timestamp,
        "unparseable_lines": p.unparseable_lines,
        "truncated_final_line": p.truncated_final_line,
    }


def _write_jsonl(path, values):
    with open(path, "w", encoding="utf-8") as fh:
        for value in values:
            fh.write(json.dumps(value, ensure_ascii=False) + "\n")


def _write_json(path, value):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def extract_codex(bundle_path, out_dir, requested_format="auto", detection_basis=None):
    actors, child_discovery = _discover(bundle_path)
    all_events = []
    by_actor_events = {}
    for actor in actors:
        mapped = _map_actor(actor)
        by_actor_events[actor.actor] = mapped
        all_events.extend(mapped)

    actor_rows = []
    for actor in actors:
        if actor.parent_actor and actor.join_line is not None:
            parent_events = by_actor_events.get(actor.parent_actor, [])
            spawn_step = next(
                (event["step"] for event in parent_events
                 if (event.get("raw_ptr") or {}).get("line") == actor.join_line
                 and event.get("kind") == "lifecycle"
                 and event.get("subtype") == "subagent_activity"),
                None,
            )
            join = {
                "thread_id": actor.session_id,
                "parent_actor": actor.parent_actor,
                "activity_raw_ptr": _ptr(actor.join_path, actor.join_line),
                "spawn_event_step": spawn_step,
            }
        else:
            join = None
        actor_rows.append({
            "actor": actor.actor, "path": actor.path, "join": join,
            "orphan": actor.orphan,
            **({"suspected_reason": actor.suspected_reason} if actor.suspected_reason else {}),
        })

    usage_by_actor = {a.actor: value for a in actors if (value := _usage(a)) is not None}
    main_usage = usage_by_actor.get("main")
    raw_types = Counter()
    for actor in actors:
        for _line, obj in actor.owned_rows:
            payload = obj.get("payload") if isinstance(obj, dict) else None
            key = obj.get("type")
            if isinstance(payload, dict) and payload.get("type"):
                key = f"{key}/{payload['type']}"
            raw_types[key] += 1

    orphan_events = sum(len(by_actor_events[a.actor]) for a in actors if a.orphan)
    counts = {
        "active_path_messages": sum(
            1 for event in all_events if event.get("kind") in ("user_msg", "assistant_text")
        ),
        "abandoned_messages": 0,
        "branch_groups": 0,
        "compact_boundaries": sum(
            1 for event in all_events
            if event.get("kind") == "lifecycle" and event.get("subtype") == "compact_boundary"
        ),
        "subagent_joins": sum(1 for a in actors if a.parent_actor and not a.orphan),
        "orphan_actors": sum(1 for a in actors if a.orphan),
        "orphan_events": orphan_events,
        "suspected_child_rollouts": len(child_discovery["suspected_unlinked"]),
        "missing_linked_child_rollouts": len(child_discovery["missing_linked"]),
        "rollbacks": sum(a.order_info.get("rollbacks", 0) for a in actors),
        "events_active": len(all_events),
        "events_branches": 0,
    }
    source = [
        {"file": a.path, "sha256": a.parsed.sha256, "lines": a.parsed.lines,
         "last_timestamp": a.parsed.last_timestamp}
        for a in actors
    ]
    source_paths = {item["file"] for item in source}
    discovery_watermark = {}
    for item in child_discovery["suspected_unlinked"]:
        observed_source = item.get("source")
        if observed_source and observed_source["file"] not in source_paths:
            source.append({**observed_source, "role": "suspected_unlinked_child"})
            source_paths.add(observed_source["file"])
        if item.get("watermark"):
            discovery_watermark[item["thread_id"]] = item["watermark"]

    meta = {
        "extractor_version": EXTRACTOR_VERSION,
        "format": {
            "requested": requested_format,
            "detected": "codex",
            "basis": list(detection_basis or []),
        },
        "source": source,
        "partial": True,
        "watermark": {a.actor: _watermark(a) for a in actors},
        "discovery_watermark": discovery_watermark,
        "timeline": {
            "semantics": "visible lived order, including actions later followed by rollback",
            "branch_partition": "not reconstructed; branches.jsonl intentionally empty",
            "zero_branch_counts_condition": "codex-order/1 passed for every included actor",
            "actors": {a.actor: a.order_info for a in actors},
        },
        "usage": {
            "semantics": "source cumulative snapshots; not per-event spend; actors not summed",
            "run_cumulative": main_usage,
            "by_actor": usage_by_actor,
        },
        "child_discovery": child_discovery,
        "counts": counts,
        "format_counts": {"owned_rows_by_type": dict(sorted(raw_types.items()))},
        "suppressed_duplicates": [
            "event_msg.user_message when exact response_item.message duplicate",
            "event_msg.agent_message when exact response_item.message duplicate",
            "event_msg.agent_reasoning only when its exact text digest matches a response summary_text",
            "paired response_item outputs and paired event_msg tool-end summaries",
        ],
        "timestamp_policy": {
            "event_field": "ts_source",
            "precedence": ["event-specific inner occurrence/completion time", "envelope.timestamp"],
        },
    }

    os.makedirs(out_dir, exist_ok=True)
    _write_jsonl(os.path.join(out_dir, "trace.jsonl"), all_events)
    _write_jsonl(os.path.join(out_dir, "branches.jsonl"), [])
    _write_json(os.path.join(out_dir, "actors.json"), actor_rows)
    _write_json(os.path.join(out_dir, "meta.json"), meta)
    return meta
