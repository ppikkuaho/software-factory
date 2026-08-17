"""L2 triage (C1 §3.2) — one `claude -p` call over the digest + screens + spine.

Stages the LLM sandbox (digest.md + screens.json + spine.md, all inside the
trace-only work dir), runs the versioned, sha256-stamped triage template, and
parses the structured verdict: per-SP results, ranked impact-tiered findings,
descent targets, and ledger proposals. The generator is injectable so tests spend
zero tokens.
"""

from __future__ import annotations

import hashlib
import os
import shutil

from .llm import LLMCallError, parse_json_body, run_claude_p
from .spine import spine_checklist_md

TRIAGE_TEMPLATE = "triage.v1.md"
_PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts"
)

_IMPACT_TIERS = ("trivial", "minor", "notable", "severe")
_ABOVE_MINOR = ("notable", "severe")


def _load_template(name):
    with open(os.path.join(_PROMPTS_DIR, name), encoding="utf-8") as fh:
        text = fh.read()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def stage_sandbox(work_dir, digest_path, spine_entries, spine_source):
    """Ensure the trace-only work dir carries digest.md, screens.json, spine.md.

    screens.json is already written by L1. digest.md is copied in (unless it is
    already the work-dir digest). spine.md is (re)written from the loaded entries.
    """
    dst_digest = os.path.join(work_dir, "digest.md")
    if os.path.abspath(digest_path) != os.path.abspath(dst_digest):
        shutil.copyfile(digest_path, dst_digest)
    with open(os.path.join(work_dir, "spine.md"), "w", encoding="utf-8") as fh:
        fh.write(spine_checklist_md(spine_entries, spine_source))


def _normalize(result, spine_entries):
    if not isinstance(result, dict):
        result = {}
    sr = result.get("spine_results")
    sr = sr if isinstance(sr, dict) else {}
    for e in spine_entries:
        if e["id"] not in sr:
            sr[e["id"]] = {"result": "no-signal",
                           "note": "triage returned no entry for this SP id",
                           "anchors": []}
    result["spine_results"] = sr
    for key in ("findings", "descent_targets", "ledger_proposals"):
        v = result.get(key)
        result[key] = v if isinstance(v, list) else []
    return result


def above_minor_targets(result):
    """Descent targets whose finding is impact-tiered above `minor`."""
    findings = {f.get("id"): f for f in result.get("findings", []) if isinstance(f, dict)}
    out = []
    for t in result.get("descent_targets", []):
        if not isinstance(t, dict):
            continue
        f = findings.get(t.get("finding_id"))
        impact = (f or {}).get("impact")
        if impact in _ABOVE_MINOR:
            out.append({**t, "impact": impact,
                        "title": (f or {}).get("title"),
                        "rationale": (f or {}).get("rationale"),
                        "anchors": (f or {}).get("anchors", [])})
    # also cover any above-minor finding that lacks an explicit descent target
    covered = {t.get("finding_id") for t in out}
    for fid, f in findings.items():
        if f.get("impact") in _ABOVE_MINOR and fid not in covered:
            out.append({"finding_id": fid,
                        "span": _span_from_anchors(f.get("anchors", [])),
                        "reason": "above-minor finding without an explicit descent "
                                  "target; replay its anchored span",
                        "impact": f.get("impact"),
                        "title": f.get("title"),
                        "rationale": f.get("rationale"),
                        "anchors": f.get("anchors", [])})
    return out


def _span_from_anchors(anchors):
    return anchors[0] if anchors else None


def run_triage(work_dir, digest_path, spine_entries, spine_source,
               generator=None, model=None):
    """Run L2. Returns a record dict (never raises on LLM content; raises only on a
    hard technical generator failure the caller chose not to inject around)."""
    stage_sandbox(work_dir, digest_path, spine_entries, spine_source)
    template_text, template_sha = _load_template(TRIAGE_TEMPLATE)
    gen = generator or run_claude_p
    try:
        body, info = gen(template_text, work_dir, model)
    except LLMCallError as e:
        # technical failure: degrade honestly (L1 stands, spine all no-signal),
        # rather than crash the pass or fabricate a verdict
        return {
            "template": TRIAGE_TEMPLATE,
            "template_sha256": template_sha,
            "model": "unavailable",
            "session_id": None,
            "cost_usd": None,
            "permission_denials": [],
            "result": _normalize({}, spine_entries),
            "raw_body": None,
            "parse_error": None,
            "generation_error": str(e),
        }
    try:
        result = parse_json_body(body)
        parse_error = None
    except Exception as e:  # tolerate a non-JSON body — record it, degrade honestly
        result = {}
        parse_error = str(e)
    result = _normalize(result, spine_entries)
    return {
        "template": TRIAGE_TEMPLATE,
        "template_sha256": template_sha,
        "model": info.get("model_id", model or "unknown"),
        "session_id": info.get("session_id"),
        "cost_usd": info.get("cost_usd"),
        "permission_denials": info.get("permission_denials", []),
        "result": result,
        "raw_body": body,
        "parse_error": parse_error,
        "generation_error": None,
    }
