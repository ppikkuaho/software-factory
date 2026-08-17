"""L3 deep dives (C1 §3.3) — one `claude -p` per above-minor descent target.

Empathetic replay of a flagged span from the reasoning + experience streams, under
local-surface discipline (judge from what the agent could see at that point). No
descent cap (P-1): the done-definition is that every above-threshold flag is either
diagnosed or explicitly deferred with a written reason. The generator is injectable
so tests spend zero tokens.
"""

from __future__ import annotations

import hashlib
import json
import os

from .llm import LLMCallError, parse_json_body, run_claude_p

DEEPDIVE_TEMPLATE = "deep-dive.v1.md"
_PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts"
)


def _load_template(name):
    with open(os.path.join(_PROMPTS_DIR, name), encoding="utf-8") as fh:
        text = fh.read()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _target_block(target):
    return json.dumps({
        "finding_id": target.get("finding_id"),
        "title": target.get("title"),
        "impact": target.get("impact"),
        "span": target.get("span"),
        "anchors": target.get("anchors", []),
        "reason": target.get("reason"),
        "triage_rationale": target.get("rationale"),
    }, ensure_ascii=False, indent=2)


def _normalize(record, target):
    if not isinstance(record, dict):
        record = {}
    record.setdefault("finding_id", target.get("finding_id"))
    disp = record.get("disposition")
    if disp not in ("diagnosed", "deferred"):
        # a malformed body is an honest deferral, not a fabricated diagnosis
        record["disposition"] = "deferred"
        record.setdefault("deferral_reason",
                          "deep-dive returned no usable disposition; deferred")
    if record["disposition"] == "deferred" and not record.get("deferral_reason"):
        record["deferral_reason"] = "deferred without a stated reason"
    if record["disposition"] == "diagnosed":
        record["deferral_reason"] = None
    for k in ("diagnosis", "local_surface_note", "impact_read"):
        record.setdefault(k, None)
    record.setdefault("anchors", target.get("anchors", []))
    return record


def run_deep_dive(work_dir, target, generator=None, model=None):
    """Run one deep dive for `target`. Returns a record dict."""
    base_text, template_sha = _load_template(DEEPDIVE_TEMPLATE)
    prompt = base_text.replace("{{TARGET_BLOCK}}", _target_block(target))
    gen = generator or run_claude_p
    try:
        body, info = gen(prompt, work_dir, model)
    except LLMCallError as e:
        # technical failure on this target: an honest deferral, not a lost flag and
        # not a fabricated diagnosis (P-1 done-definition allows deferral-with-reason)
        return {
            "template": DEEPDIVE_TEMPLATE,
            "template_sha256": template_sha,
            "finding_id": target.get("finding_id"),
            "target": target,
            "model": "unavailable",
            "session_id": None,
            "cost_usd": None,
            "permission_denials": [],
            "record": _normalize(
                {"disposition": "deferred",
                 "deferral_reason": f"claude -p failed technically: {e}"}, target),
            "raw_body": None,
            "parse_error": None,
        }
    try:
        record = parse_json_body(body)
        parse_error = None
    except Exception as e:
        record = {}
        parse_error = str(e)
    record = _normalize(record, target)
    return {
        "template": DEEPDIVE_TEMPLATE,
        "template_sha256": template_sha,
        "finding_id": target.get("finding_id"),
        "target": target,
        "model": info.get("model_id", model or "unknown"),
        "session_id": info.get("session_id"),
        "cost_usd": info.get("cost_usd"),
        "permission_denials": info.get("permission_denials", []),
        "record": record,
        "raw_body": body,
        "parse_error": parse_error,
    }


def run_deep_dives(work_dir, targets, generator=None, model=None):
    return [run_deep_dive(work_dir, t, generator=generator, model=model) for t in targets]
