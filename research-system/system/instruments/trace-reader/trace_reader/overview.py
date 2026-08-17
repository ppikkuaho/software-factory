"""Progress Overview — live-read control signal (C6 §5 + §1). Mechanical, no LLM.

A convenience over the extractor: extract the bundle as it stands now (a valid
prefix) to a throwaway work dir, then render a human-readable, as-of-step-N
snapshot of a possibly-running session. This is a CONTROL signal, NOT evidence
(C6 §1): uncitable by rule, expires on use, and it makes NO claim about how far
the run has progressed or what it has examined. Session-end detection is a
staleness heuristic only in v1 (no L1-L5 sign-off parsing yet).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from .extract import extract

OVERVIEW_VERSION = "trace-reader-overview/1.0.0"

# vocabulary the framing must never use — an overview may not assert progress
# completeness or examination coverage (C6 §1). Quoted trace snippets are content,
# not framing, and are exempt; these constants are the framing and are tested.
FORBIDDEN_VOCAB = (
    "complete", "completed", "completion", "covered", "coverage",
    "finished", "exhaustive", "comprehensive", "done",
)

CONTROL_BANNER = (
    "CONTROL SIGNAL — as of step {step} ({actor}); {run_state}. Uncitable by rule "
    "(C6 §1): this is a generated view with the same standing as any generated "
    "summary — not admissible as evidence, and it expires on use. It makes no "
    "claim about how far the run has progressed or what it has examined."
)

_RUNNING = "still running"
_STALE = "probably ended (stale {mins} min)"
_UNKNOWN_STATE = "run-state unknown (no timestamps)"

_SECTION_TITLES = (
    "Session status", "Current position", "Actors", "Counts (as of watermark)",
    "Recent activity",
)


def _parse_ts(ts):
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def staleness(last_ts, stale_minutes, now=None):
    """(label, age_minutes) from the last event ts. Honest labels only — a stale
    gap is reported as *probably* ended, never asserted from content."""
    now = now or datetime.now(timezone.utc)
    t = _parse_ts(last_ts)
    if t is None:
        return _UNKNOWN_STATE, None
    age = (now - t).total_seconds() / 60.0
    if age > stale_minutes:
        return _STALE.format(mins=int(age)), age
    return _RUNNING, age


def _load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _event_line(e):
    ref = f"[{e['actor']} {e['step']}]"
    kind = e.get("kind")
    if kind == "tool_call":
        hint = (e.get("input_digest") or {}).get("hint", "")
        return f"{ref} tool_call {e.get('name')}: {hint[:70]}"
    if kind in ("user_msg", "assistant_text"):
        return f"{ref} {kind}: {' '.join((e.get('text') or '').split())[:70]}"
    if kind == "thinking":
        return f"{ref} thinking"
    if kind == "lifecycle":
        return f"{ref} lifecycle/{e.get('subtype')}"
    return f"{ref} {kind}"


def _sum_tokens(events):
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens")
    totals = dict.fromkeys(keys, 0)
    for e in events:
        tok = e.get("tokens")
        if isinstance(tok, dict):
            for k in keys:
                totals[k] += int(tok.get(k, 0) or 0)
    return totals


def _render(bundle_path, meta, trace, actors, stale_minutes, now):
    with_ts = [e for e in trace if e.get("ts")]
    last_ts = max((e["ts"] for e in with_ts), default=None)
    run_state, age = staleness(last_ts, stale_minutes, now=now)

    # last active step / actor overall (by ts, then step)
    if with_ts:
        anchor = max(with_ts, key=lambda e: (e["ts"], e["step"]))
        head_step, head_actor = anchor["step"], anchor["actor"]
    else:
        head_step, head_actor = (trace[-1]["step"], trace[-1]["actor"]) if trace else (0, "main")

    counts = meta.get("counts", {})
    watermark = meta.get("watermark", {})

    lines = []
    lines.append(f"# Progress Overview — {os.path.basename(bundle_path)}\n")
    lines.append("> " + CONTROL_BANNER.format(step=head_step, actor=head_actor, run_state=run_state))
    lines.append(">")
    lines.append(f"> Partial read (meta.partial={meta.get('partial')}). Watermark:")
    for actor, wm in watermark.items():
        lines.append(f">   {actor}: {wm.get('lines')} lines, last ts {wm.get('last_timestamp')}")
    lines.append("")

    # Session status
    lines.append("## Session status")
    age_s = f"~{int(age)} min ago" if age is not None else "unknown age"
    lines.append(f"- run state (staleness heuristic, threshold {stale_minutes} min): {run_state}")
    lines.append(f"- last activity ts: {last_ts} ({age_s})")
    lines.append("- end-detection: staleness only in v1; no sign-off parsing (v2 candidate)")
    lines.append("")

    # Current position
    lines.append("## Current position")
    per_actor_last = {}
    for e in trace:
        cur = per_actor_last.get(e["actor"])
        if cur is None or e["step"] > cur["step"]:
            per_actor_last[e["actor"]] = e
    for actor, e in per_actor_last.items():
        lines.append(f"- {actor}: last step {e['step']} at {e.get('ts')}")

    def _last_of(kind):
        cands = [e for e in with_ts if e.get("kind") == kind]
        return max(cands, key=lambda e: e["ts"]) if cands else None

    lu = _last_of("user_msg")
    la = _last_of("assistant_text")
    if lu:
        lines.append(f"- last user message [{lu['actor']} {lu['step']}]: "
                     f"\"{' '.join((lu.get('text') or '').split())[:160]}\"")
    if la:
        lines.append(f"- last assistant text [{la['actor']} {la['step']}]: "
                     f"\"{' '.join((la.get('text') or '').split())[:160]}\"")
    lines.append("")

    # Actors
    lines.append("## Actors")
    ev_per_actor = {}
    for e in trace:
        ev_per_actor[e["actor"]] = ev_per_actor.get(e["actor"], 0) + 1
    for a in actors:
        actor = a["actor"]
        n = ev_per_actor.get(actor, 0)
        if actor == "main":
            lines.append(f"- main: {n} active events")
        elif a.get("orphan"):
            lines.append(f"- {actor}: ORPHAN (unattributed) — {n} active events")
        else:
            step = (a.get("join") or {}).get("spawn_event_step")
            lines.append(f"- {actor}: joined@step {step} — {n} active events")
    lines.append("")

    # Counts
    tok = _sum_tokens(trace)
    lines.append("## Counts (as of watermark)")
    lines.append(f"- active-path messages: {counts.get('active_path_messages')}")
    lines.append(f"- abandoned messages: {counts.get('abandoned_messages')} in "
                 f"{counts.get('branch_groups')} branch groups")
    lines.append(f"- compact boundaries: {counts.get('compact_boundaries')}")
    lines.append(f"- subagent joins: {counts.get('subagent_joins')}; orphan actors: "
                 f"{counts.get('orphan_actors', 0)} ({counts.get('orphan_events', 0)} orphan events)")
    lines.append(f"- token totals (active-path usage): input {tok['input_tokens']}, "
                 f"output {tok['output_tokens']}, cache-read {tok['cache_read_input_tokens']}, "
                 f"cache-creation {tok['cache_creation_input_tokens']}")
    lines.append("")

    # Recent activity
    lines.append("## Recent activity (last 10 events by time)")
    recent = sorted(with_ts, key=lambda e: (e["ts"], e["step"]))[-10:]
    for e in recent:
        lines.append(f"- {_event_line(e)}")
    lines.append("")

    return "\n".join(lines) + "\n"


def build_overview(bundle_path, stale_minutes=30, now=None):
    with tempfile.TemporaryDirectory(prefix="ht-overview-") as work:
        meta = extract(bundle_path, work)
        trace = _load_jsonl(os.path.join(work, "trace.jsonl"))
        with open(os.path.join(work, "actors.json"), encoding="utf-8") as fh:
            actors = json.load(fh)
    return _render(bundle_path, meta, trace, actors, stale_minutes, now)
