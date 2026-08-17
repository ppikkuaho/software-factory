"""Row -> event mapping (C6 §3, §4).

The parent-chain graph decides *which path* a row belongs to; this module decides
*how a row becomes events*. Type filtering happens only here. Tool results are
folded into their tool_call by tool_use_id; per-message token usage rides the
first event of the message; branch/subagent boundaries surface as synthetic
lifecycle events anchored to real rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .digest import collapse, digest, duration_ms, extract_tokens

MESSAGE_TYPES = {"user", "assistant", "system"}
# pure UI meta, no action-stream meaning (C6 §4 ruling): elided at emit
ELIDE_TYPES = {"ai-title", "last-prompt"}

# intra-line ordering: base blocks keep their block index; synthetics follow
_INTRA_SPAWN = 1000
_INTRA_RETURN = 1000
_INTRA_FORK = 2000


@dataclass
class PendingEvent:
    actor: str
    actor_path: str
    raw_line: int
    intra: int
    kind: str
    fields: dict
    ts: str | None
    is_active: bool
    branch_group: object | None = None   # graph.BranchGroup when abandoned
    tool_use_id: str | None = None        # set on tool_call events, for cross-refs


@dataclass
class Join:
    actor: str        # subagent actor, e.g. "agent-a069..."
    agent_id: str     # actor without the "agent-" prefix
    tool_use_id: str


def build_fold_map(rows, active) -> dict:
    """tool_use_id -> [candidate results] from every user tool_result row.

    A tool call can, after a rewind, have more than one result row (an active one
    and an abandoned one) or a single result that landed on a sibling branch (the
    parallel-tool-call + rewind case). Candidates are kept per id so the caller can
    fold the result on the tool_use's OWN partition first, deterministically.
    """
    fold = {}
    for line, obj in rows:
        if not isinstance(obj, dict) or obj.get("type") != "user":
            continue
        content = obj.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        is_active = obj.get("uuid") in active
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid is not None:
                    fold.setdefault(tid, []).append({
                        "content": b.get("content"),
                        "is_error": bool(b.get("is_error")),
                        "ts": obj.get("timestamp"),
                        "line": line,
                        "active": is_active,
                    })
    return fold


def resolve_result(fold, tid, want_active):
    """Pick the result for a tool_use: same-partition first, then earliest line."""
    cands = fold.get(tid)
    if not cands:
        return None
    same = [c for c in cands if c["active"] == want_active]
    pool = same if same else cands
    return min(pool, key=lambda c: c["line"])


def main_tool_use_ids(rows) -> set:
    ids = set()
    for _line, obj in rows:
        if isinstance(obj, dict) and obj.get("type") == "assistant":
            for b in obj.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    ids.add(b.get("id"))
    return ids


def _disposition(obj, ag):
    """Return (is_active, branch_group) for a row given the actor graph."""
    if not isinstance(obj, dict):
        return True, None
    u = obj.get("uuid")
    if u is None:
        # session-level marker (mode / checkpoint / queue-op): always on the stream
        return True, None
    if u in ag.active:
        return True, None
    return False, ag.uuid_to_branch.get(u)


def _assistant_events(actor, actor_path, line, obj, fold, joins, is_active, bg):
    msg = obj.get("message", {}) or {}
    content = msg.get("content")
    ts = obj.get("timestamp")
    tokens = extract_tokens(msg.get("usage"))
    events = []
    blocks = content if isinstance(content, list) else []
    for bi, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        tool_use_id = None
        if btype == "thinking":
            kind, fields = "thinking", {
                "text": block.get("thinking", ""),
                "reasoning_source": "assistant_block.thinking",
            }
        elif btype == "redacted_thinking":
            kind, fields = "thinking", {
                "redacted": True,
                "reasoning_source": "assistant_block.redacted_thinking",
            }
        elif btype == "text":
            kind, fields = "assistant_text", {"text": block.get("text", "")}
        elif btype == "tool_use":
            kind = "tool_call"
            tid = block.get("id")
            tool_use_id = tid
            res = resolve_result(fold, tid, is_active)
            fields = {
                "name": block.get("name"),
                "tool_use_id": tid,
                "input_digest": digest(block.get("input")),
                "result_digest": digest(res["content"]) if res else None,
                "duration_ms": duration_ms(ts, res["ts"]) if res else None,
                "tokens": None,
            }
            if res:
                fields["result_raw_ptr"] = {"file": actor_path, "line": res["line"]}
            if res and res["is_error"]:
                fields["result_is_error"] = True
        else:
            kind, fields = "assistant_text", {"text": "", "unknown_block_type": btype}
        events.append(
            PendingEvent(actor, actor_path, line, bi, kind, fields, ts, is_active, bg, tool_use_id)
        )
    # per-message tokens ride the first emitted event of the message
    if tokens is not None and events:
        events[0].fields["tokens"] = tokens

    # subagent spawn markers, anchored to the tool_use row
    extra = []
    for ev in events:
        if ev.kind == "tool_call" and ev.tool_use_id in joins:
            j = joins[ev.tool_use_id]
            extra.append(
                PendingEvent(
                    actor, actor_path, line, _INTRA_SPAWN, "lifecycle",
                    {"subtype": "subagent_spawn", "agent_id": j.agent_id,
                     "tool_use_id": j.tool_use_id, "subagent_actor": j.actor},
                    ts, is_active, bg,
                )
            )
    return events + extra


def _user_events(actor, actor_path, line, obj, joins, is_active, bg):
    msg = obj.get("message", {}) or {}
    content = msg.get("content")
    ts = obj.get("timestamp")
    events = []
    if isinstance(content, str):
        fields = {"text": content}
        if obj.get("isMeta"):
            fields["meta"] = True
        if obj.get("isCompactSummary"):
            fields["compact_summary"] = True
        events.append(PendingEvent(actor, actor_path, line, 0, "user_msg", fields, ts, is_active, bg))
        return events
    if isinstance(content, list):
        for bi, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                events.append(
                    PendingEvent(actor, actor_path, line, bi, "user_msg",
                                 {"text": block.get("text", "")}, ts, is_active, bg)
                )
            elif btype == "tool_result":
                # folded into the tool_call; only a subagent return surfaces here
                tid = block.get("tool_use_id")
                if tid in joins:
                    j = joins[tid]
                    events.append(
                        PendingEvent(actor, actor_path, line, _INTRA_RETURN, "lifecycle",
                                     {"subtype": "subagent_return", "agent_id": j.agent_id,
                                      "tool_use_id": j.tool_use_id}, ts, is_active, bg)
                    )
            else:
                events.append(
                    PendingEvent(actor, actor_path, line, bi, "user_msg",
                                 {"text": "", "unknown_block_type": btype}, ts, is_active, bg)
                )
    return events


def _system_events(actor, actor_path, line, obj, is_active, bg):
    subtype = obj.get("subtype")
    ts = obj.get("timestamp")
    if subtype == "compact_boundary":
        cm = obj.get("compactMetadata") or {}
        fields = {
            "subtype": "compact_boundary",
            "trigger": cm.get("trigger"),
            "pre_tokens": cm.get("preTokens"),
            "post_tokens": cm.get("postTokens"),
        }
    else:
        fields = {"subtype": "system_note", "system_subtype": subtype}
        dur = obj.get("durationMs")
        if isinstance(dur, int):
            fields["duration_ms"] = dur
        content = obj.get("content")
        if isinstance(content, str) and content.strip():
            fields["hint"] = collapse(content)[:120]
    return [PendingEvent(actor, actor_path, line, 0, "lifecycle", fields, ts, is_active, bg)]


def _lifecycle_nonuuid(actor, actor_path, line, obj):
    """mode / permission-mode / file-history-snapshot / queue-operation (no uuid)."""
    t = obj.get("type")
    ts = obj.get("timestamp")
    if t == "file-history-snapshot":
        fields = {"subtype": "checkpoint", "message_id": obj.get("messageId"),
                  "snapshot_update": bool(obj.get("isSnapshotUpdate"))}
    elif t == "mode":
        fields = {"subtype": "mode_change", "mode": obj.get("mode")}
    elif t == "permission-mode":
        fields = {"subtype": "mode_change", "permission_mode": obj.get("permissionMode")}
    elif t == "queue-operation":
        fields = {"subtype": "queue_operation", "operation": obj.get("operation")}
        content = obj.get("content")
        if isinstance(content, str) and content.strip():
            fields["hint"] = collapse(content)[:120]
    else:
        fields = {"subtype": "system_note", "system_subtype": f"unknown:{t}"}
    return [PendingEvent(actor, actor_path, line, 0, "lifecycle", fields, ts, True, None)]


def emit_rows(actor, actor_path, rows, ag, fold, joins):
    """Produce base + inline-synthetic PendingEvents for one actor (unordered)."""
    pending = []
    for line, obj in rows:
        if not isinstance(obj, dict):
            continue
        t = obj.get("type")
        if t in ELIDE_TYPES:
            continue
        is_active, bg = _disposition(obj, ag)
        if t == "assistant":
            pending += _assistant_events(actor, actor_path, line, obj, fold, joins, is_active, bg)
        elif t == "user":
            pending += _user_events(actor, actor_path, line, obj, joins, is_active, bg)
        elif t == "system":
            pending += _system_events(actor, actor_path, line, obj, is_active, bg)
        elif t == "attachment":
            fields = {"subtype": "attachment", "digest": digest(obj.get("attachment"))}
            pending.append(PendingEvent(actor, actor_path, line, 0, "lifecycle", fields,
                                        obj.get("timestamp"), is_active, bg))
        elif t in ("mode", "permission-mode", "file-history-snapshot", "queue-operation"):
            pending += _lifecycle_nonuuid(actor, actor_path, line, obj)
        else:
            # unknown uuid-bearing type still surfaces (exhaustiveness)
            pending.append(PendingEvent(actor, actor_path, line, 0, "lifecycle",
                                        {"subtype": "system_note", "system_subtype": f"unknown:{t}"},
                                        obj.get("timestamp"), is_active, bg))
    return pending


def fork_markers(actor, actor_path, ag):
    """One fork_marker per active-path fork point that has abandoned children."""
    by_fork = {}
    for g in ag.groups:
        if g.fork_parent_uuid is None:
            continue   # chain-break root: no active-path fork
        by_fork.setdefault(g.fork_parent_uuid, []).append(g)
    events = []
    for fork_uuid, groups in by_fork.items():
        line, obj = ag.node[fork_uuid]
        branch_ids = sorted(g.branch_id for g in groups)
        events.append(
            PendingEvent(actor, actor_path, line, _INTRA_FORK, "lifecycle",
                         {"subtype": "fork_marker", "branch_ids": branch_ids},
                         obj.get("timestamp"), True, None)
        )
    return events


def order_and_number(pending):
    """Assign a single per-actor step sequence over ALL events, in file order."""
    pending.sort(key=lambda pe: (pe.raw_line, pe.intra))
    for i, pe in enumerate(pending, start=1):
        pe.step = i
    return pending


def serialize(pe):
    ev = {"step": pe.step, "ts": pe.ts, "actor": pe.actor, "kind": pe.kind}
    ev.update(pe.fields)
    ev["raw_ptr"] = {"file": pe.actor_path, "line": pe.raw_line}
    if pe.branch_group is not None:
        ev["branch_id"] = pe.branch_group.branch_id
        fl = pe.branch_group.fork_line
        ev["fork_raw_ptr"] = {"file": pe.actor_path, "line": fl} if fl is not None else None
    return ev
