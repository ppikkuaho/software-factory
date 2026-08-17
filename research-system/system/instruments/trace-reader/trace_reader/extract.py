"""Extraction orchestration: bundle -> trace.jsonl / branches.jsonl / actors.json / meta.json.

Deterministic end to end: same bundle bytes -> byte-identical output dirs. No
extraction wall-clock timestamp appears anywhere (all timestamps come from the
data). partial is always true in v1 — there is no definitive end signal, so the
watermark states exactly what prefix was seen.
"""

from __future__ import annotations

import json
import os

from . import EXTRACTOR_VERSION
from . import bundle as bundle_mod
from . import emit, graph, parse

MESSAGE_TYPES = emit.MESSAGE_TYPES


def _branch_num(branch_id: str) -> int:
    return int(branch_id.rsplit("/b", 1)[1])


def _actor_stream(actor, actor_path, rows, joins):
    """Parse-independent: given parsed rows, produce ordered PendingEvents + graph."""
    ag = graph.analyze(actor, rows)
    fold = emit.build_fold_map(rows, ag.active)
    pending = emit.emit_rows(actor, actor_path, rows, ag, fold, joins)
    pending += emit.fork_markers(actor, actor_path, ag)
    emit.order_and_number(pending)
    return ag, pending


def _actor_model(rows):
    models = []
    for _line, obj in rows:
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        if not isinstance(model, str) or model == "<synthetic>":
            continue
        if model not in models:
            models.append(model)
    if not models:
        return "unknown"
    if len(models) == 1:
        return models[0]
    return models


def _extract_claude(bundle_path: str, out_dir: str, requested_format="auto",
                    detection_basis=None) -> dict:
    b = bundle_mod.discover(bundle_path)
    os.makedirs(out_dir, exist_ok=True)

    # --- main actor ---
    main_parsed = parse.parse_file(b.main_path)
    main_ids = emit.main_tool_use_ids(main_parsed.rows)

    # resolve subagent joins against the main transcript's tool_use ids
    joins = {}                 # tool_use_id -> emit.Join
    joined_actors = set()
    for sf in b.subagents:
        tid = sf.tool_use_id
        if tid is not None and tid in main_ids:
            agent_id = sf.actor[len("agent-"):] if sf.actor.startswith("agent-") else sf.actor
            joins[tid] = emit.Join(actor=sf.actor, agent_id=agent_id, tool_use_id=tid)
            joined_actors.add(sf.actor)

    main_ag, main_pending = _actor_stream("main", b.main_path, main_parsed.rows, joins)

    # spawn_event_step: step of each subagent_spawn lifecycle event in the main stream
    spawn_step = {}
    for pe in main_pending:
        if pe.kind == "lifecycle" and pe.fields.get("subtype") == "subagent_spawn":
            spawn_step[pe.fields["tool_use_id"]] = pe.step

    # --- subagent actors ---
    sub_streams = []           # list of (SubagentFile, ParsedFile, ActorGraph, pending)
    for sf in b.subagents:
        parsed = parse.parse_file(sf.path)
        ag, pending = _actor_stream(sf.actor, sf.path, parsed.rows, joins={})
        sub_streams.append((sf, parsed, ag, pending))

    # spawn order for output: joined subagents by their spawn step, then orphans by actor
    def sub_sort_key(item):
        sf = item[0]
        step = spawn_step.get(sf.tool_use_id)
        if step is None:
            return (1, sf.actor, 0)
        return (0, step, sf.actor)

    sub_streams.sort(key=sub_sort_key)
    actor_order = ["main"] + [item[0].actor for item in sub_streams]
    actor_rank = {a: i for i, a in enumerate(actor_order)}

    # --- assemble trace + branches ---
    def split(pending):
        active = [emit.serialize(pe) for pe in pending if pe.is_active]
        branches = [(pe, emit.serialize(pe)) for pe in pending if not pe.is_active]
        return active, branches

    trace_events = []
    branch_events = []   # list of (actor_rank, branch_num, step, event)

    main_active, main_branch = split(main_pending)
    trace_events.extend(main_active)
    for pe, ev in main_branch:
        branch_events.append((actor_rank["main"], _branch_num(ev["branch_id"]), ev["step"], ev))

    orphan_event_count = 0
    for sf, parsed, ag, pending in sub_streams:
        sub_active, sub_branch = split(pending)
        is_orphan = sf.actor not in joined_actors
        if is_orphan:
            # D1 quarantine-lite: every event of an orphan actor is flagged in place
            # (no fifth file); absence of the flag means non-orphan.
            for ev in sub_active:
                ev["orphan"] = True
            for _pe, ev in sub_branch:
                ev["orphan"] = True
            orphan_event_count += len(sub_active) + len(sub_branch)
        trace_events.extend(sub_active)
        for pe, ev in sub_branch:
            branch_events.append((actor_rank[sf.actor], _branch_num(ev["branch_id"]), ev["step"], ev))

    branch_events.sort(key=lambda t: (t[0], t[1], t[2]))
    branch_events = [t[3] for t in branch_events]

    # --- actors.json ---
    actors = [{"actor": "main", "path": b.main_path, "join": None, "orphan": False,
               "model": _actor_model(main_parsed.rows)}]
    for sf, parsed, ag, pending in sub_streams:
        if sf.actor in joined_actors:
            join = {"tool_use_id": sf.tool_use_id, "spawn_event_step": spawn_step.get(sf.tool_use_id)}
            orphan = False
        else:
            join = None
            orphan = True
        actors.append({"actor": sf.actor, "path": sf.path, "join": join, "orphan": orphan,
                       "model": _actor_model(parsed.rows)})

    # --- counts (headline four are MAIN-scoped; ground truth is the main transcript) ---
    active_msgs = sum(
        1 for _l, o in main_parsed.rows
        if isinstance(o, dict) and o.get("type") in MESSAGE_TYPES and o.get("uuid") in main_ag.active
    )
    abandoned_msgs = sum(
        1 for _l, o in main_parsed.rows
        if isinstance(o, dict) and o.get("type") in MESSAGE_TYPES
        and o.get("uuid") is not None and o.get("uuid") not in main_ag.active
    )
    compact_boundaries = sum(
        1 for _l, o in main_parsed.rows
        if isinstance(o, dict) and o.get("type") == "system" and o.get("subtype") == "compact_boundary"
    )
    counts = {
        "active_path_messages": active_msgs,
        "abandoned_messages": abandoned_msgs,
        "branch_groups": len(main_ag.groups),
        "compact_boundaries": compact_boundaries,
        "subagent_joins": len(joined_actors),
        "orphan_actors": len(b.subagents) - len(joined_actors),
        "orphan_events": orphan_event_count,
        "events_active": len(trace_events),
        "events_branches": len(branch_events),
    }

    # --- meta.json ---
    def watermark_entry(actor, parsed):
        return {
            "file": parsed.path,
            "lines": parsed.lines,
            "last_timestamp": parsed.last_timestamp,
            "unparseable_lines": parsed.unparseable_lines,
            "truncated_final_line": parsed.truncated_final_line,
        }

    source = [{"file": main_parsed.path, "sha256": main_parsed.sha256,
               "lines": main_parsed.lines, "last_timestamp": main_parsed.last_timestamp}]
    watermark = {"main": watermark_entry("main", main_parsed)}
    for sf, parsed, ag, pending in sub_streams:
        source.append({"file": parsed.path, "sha256": parsed.sha256,
                       "lines": parsed.lines, "last_timestamp": parsed.last_timestamp})
        watermark[sf.actor] = watermark_entry(sf.actor, parsed)

    meta = {
        "extractor_version": EXTRACTOR_VERSION,
        "format": {
            "requested": requested_format,
            "detected": "claude",
            "basis": list(detection_basis or []),
        },
        "source": source,
        "partial": True,   # v1: no definitive end signal exists
        "watermark": watermark,
        "counts": counts,
    }

    _write_jsonl(os.path.join(out_dir, "trace.jsonl"), trace_events)
    _write_jsonl(os.path.join(out_dir, "branches.jsonl"), branch_events)
    _write_json(os.path.join(out_dir, "actors.json"), actors)
    _write_json(os.path.join(out_dir, "meta.json"), meta)
    return meta


class TraceFormatError(RuntimeError):
    """Input format is ambiguous, unsupported, or contradicts an explicit mode."""


def _detect_format(bundle_path: str):
    """Return (format|None, evidence) from row structure, never path naming."""
    parsed = parse.parse_file(os.path.abspath(bundle_path))
    codex = []
    claude = []
    for line, obj in parsed.rows:
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        if obj.get("type") == "session_meta" and isinstance(payload, dict) \
                and (payload.get("id") or payload.get("session_id")):
            codex.append({"file": parsed.path, "line": line,
                          "signal": "session_meta envelope with session identity"})
        if obj.get("type") in ("event_msg", "response_item", "turn_context", "compacted") \
                and isinstance(payload, dict):
            codex.append({"file": parsed.path, "line": line,
                          "signal": f"Codex envelope family {obj.get('type')}"})
        if obj.get("type") in emit.MESSAGE_TYPES and (
            obj.get("uuid") is not None or obj.get("parentUuid") is not None
            or isinstance(obj.get("message"), dict)
        ):
            claude.append({"file": parsed.path, "line": line,
                           "signal": "Claude message/parent-chain row"})
    if codex and claude:
        raise TraceFormatError(
            f"ambiguous mixed trace formats in {parsed.path}: "
            f"Codex evidence at line {codex[0]['line']}, Claude evidence at line {claude[0]['line']}"
        )
    if codex:
        return "codex", codex[:4]
    if claude:
        return "claude", claude[:4]
    # A file caught inside its very first JSON object still has to honor the
    # no-EOF rule. Use only producer-specific leading keys; never use its path.
    try:
        with open(parsed.path, "rb") as fh:
            prefix = fh.read(128).lstrip()
    except OSError:
        prefix = b""
    if prefix.startswith((b'{"p', b'{"uuid"', b'{"parentUuid"')):
        return "claude", [{"file": parsed.path, "line": 1,
                           "signal": "truncated first-row Claude parent/uuid key prefix"}]
    if prefix.startswith(b'{"timestamp"'):
        return "codex", [{"file": parsed.path, "line": 1,
                          "signal": "truncated first-row Codex timestamp envelope prefix"}]
    return None, []


def extract(bundle_path: str, out_dir: str, format="auto") -> dict:
    """Dispatch a Claude bundle or Codex rollout into the one canonical seam."""
    if format not in ("auto", "claude", "codex"):
        raise TraceFormatError(f"unknown format {format!r}; expected auto, claude, or codex")
    detected, basis = _detect_format(bundle_path)
    if format == "auto":
        if detected is None:
            # A prefix shorter than its first producer-specific key is genuinely
            # unknowable. Preserve the pre-dispatch Claude extractor's no-EOF
            # behavior: it emits an empty, explicitly partial seam. No source row
            # is mapped under the assumption, and the judgment is recorded.
            parsed_prefix = parse.parse_file(os.path.abspath(bundle_path))
            if parsed_prefix.truncated_final_line and not parsed_prefix.rows:
                chosen = "claude"
                basis = [{"file": os.path.abspath(bundle_path), "line": 1,
                          "signal": "ambiguous truncated first row; legacy no-EOF empty-prefix fallback"}]
            else:
                raise TraceFormatError(
                    f"could not auto-detect trace format for {os.path.abspath(bundle_path)}; "
                    "use --format only when the intended producer is known"
                )
        else:
            chosen = detected
    else:
        if detected is not None and detected != format:
            raise TraceFormatError(
                f"explicit format {format!r} contradicts detected {detected!r} rows in "
                f"{os.path.abspath(bundle_path)}"
            )
        chosen = format

    if chosen == "codex":
        from .codex import extract_codex
        return extract_codex(bundle_path, out_dir, requested_format=format,
                             detection_basis=basis)
    return _extract_claude(bundle_path, out_dir, requested_format=format,
                           detection_basis=basis)


def _write_jsonl(path, events):
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False))
            fh.write("\n")


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, indent=2))
        fh.write("\n")
