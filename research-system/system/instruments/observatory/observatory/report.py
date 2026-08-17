"""Report card assembly (C1 §8) — the bounded per-run output.

Header (session, bundle + trace + template hashes, model, generation path,
timestamps, permission_denials) + screens summary + SP-keyed spine results +
anchored impact-tiered findings + deep-dive results/deferrals + compact ledger
proposals + scope notes. The unanchored-paragraph detector runs over the card and
warns (never blocks), exactly as the digest pipeline does — report-card findings
must anchor like digest statements (director rider).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from . import OBSERVATORY_VERSION

REPORT_CARD_VERSION = "observatory-report-card/1.1.0"

# The detector is the SAME check the digest pipeline stamps; import it so the two
# products share one definition. If the concurrently-edited digest module cannot be
# imported, fall back to a minimal local replica (kept behaviorally identical).
try:
    from trace_reader.digests import anchor_warnings as _anchor_warnings
    _ANCHOR_DETECTOR_SRC = "trace_reader.digests.anchor_warnings"
except Exception:  # pragma: no cover - exercised only if the import surface changes
    import re as _re

    _ANCHOR_RE = _re.compile(r"\[[A-Za-z0-9][\w/-]*\s+\d+(?:\s*-\s*\d+)?\]")
    _ANCHOR_DETECTOR_SRC = "local replica (trace_reader.digests unavailable)"

    def _anchor_warnings(body):
        # minimal replica of the digest detector: headings + short blocks exempt,
        # a substantive prose block with no [actor step] cite is a likely bug.
        warnings = []
        checked = 0
        paras = [b.strip() for b in _re.split(r"\n\s*\n", body) if b.strip()]
        for i, para in enumerate(paras):
            if para.splitlines()[0].lstrip().startswith("#"):
                continue
            prose = _re.sub(r"^[\s>*\-\d.)]+", "", para)
            if len(prose.replace(" ", "")) < 40:
                continue
            checked += 1
            if not _ANCHOR_RE.search(para):
                warnings.append({"paragraph": i, "snippet": " ".join(para.split())[:100]})
        return {"paragraphs_checked": checked, "unanchored": warnings}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _yaml(header):
    lines = ["---"]

    def emit(key, val, indent=0):
        pad = "  " * indent
        if isinstance(val, dict):
            if not val:
                lines.append(f"{pad}{key}: {{}}")
                return
            lines.append(f"{pad}{key}:")
            for k in val:
                emit(k, val[k], indent + 1)
        elif isinstance(val, list):
            if not val:
                lines.append(f"{pad}{key}: []")
                return
            lines.append(f"{pad}{key}:")
            for item in val:
                lines.append(f"{pad}  - {item}")
        else:
            lines.append(f"{pad}{key}: {val}")

    for k in header:
        emit(k, header[k])
    lines.append("---")
    return "\n".join(lines)


def _fmt_tokens(tok):
    if not tok:
        return "none recorded"
    return ", ".join(f"{k}={v}" for k, v in tok.items())


def _screens_section(screens):
    L = ["## Screens (L1 — mechanical, symptoms not verdicts)", ""]
    ts = screens.get("token_spend", {})
    L.append(f"- **Token spend (active path):** {_fmt_tokens(ts.get('total'))}.")
    per = ts.get("per_actor") or {}
    if len(per) > 1:
        for actor, tk in per.items():
            L.append(f"  - `{actor}`: {_fmt_tokens(tk)}")
    if ts.get("branch_tokens"):
        L.append(f"  - rewound (branch) tokens, not counted in run spend: "
                 f"{_fmt_tokens(ts.get('branch_tokens'))}")
    tc = screens.get("tool_calls", {})
    L.append(f"- **Tool calls (active path):** {tc.get('total')} total — "
             f"{', '.join(f'{k}={v}' for k, v in (tc.get('by_tool') or {}).items()) or 'none'}.")
    rep = screens.get("repeated_identical_calls", {})
    L.append(f"- **Repeated-identical calls (SP-2, per-actor same tool+input):** "
             f"{rep.get('count', 0)} redundant call(s) across {rep.get('group_count', 0)} group(s).")
    for g in rep.get("groups", []):
        L.append(f"  - `{g['actor']}` {g['tool']} x{g['count']} at steps "
                 f"{g['steps']} — {(g.get('hint') or '')[:60]}")
    L.append("- **Orientation (SP-1):**")
    for actor, a in (screens.get("orientation") or {}).items():
        coverage = a.get("coverage") or {}
        L.append(f"  - `{actor}`: reads-before-first-relevant="
                 f"{a.get('reads_before_first_relevant')}, "
                 f"time-to-first-relevant-read={a.get('time_to_first_relevant_read_ms')} ms, "
                 f"off-task-reads={a.get('off_task_read_count')}")
        if coverage.get("source_format") == "codex":
            surfaces = ", ".join(coverage.get("unclassified_read_surfaces") or [])
            L.append(f"    - **Coverage warning:** read metrics are a lower bound "
                     "(incomplete); only explicitly recognized direct read tools "
                     f"are classified, while shell reads via "
                     f"{surfaces or 'exec/exec_command'} are unclassified. "
                     "First-k actions remain supported.")
    err = screens.get("errored_calls", {})
    L.append(f"- **Errored calls:** {err.get('count', 0)}.")
    for c in err.get("calls", []):
        loc = c.get("branch_id") or "active"
        L.append(f"  - `{c['actor']}` step {c['step']} {c['tool']} ({loc}) — "
                 f"{(c.get('hint') or '')[:60]}")
    L.append(f"- **Retry-shaped sequences:** {screens.get('retry_shaped', {}).get('count', 0)} "
             f"(identical call within 3 steps of its error).")
    br = screens.get("branches", {})
    L.append(f"- **Branches / rewinds:** {br.get('branch_groups')} branch group(s), "
             f"{br.get('distinct_branch_ids')} distinct branch id(s).")
    L.append(f"- **Compact boundaries:** {screens.get('compact_boundaries')}.")
    sub = screens.get("subagents", {})
    L.append(f"- **Subagents:** {sub.get('joins')} joined, {sub.get('orphan_actors')} orphan "
             f"actor(s), {sub.get('orphan_events')} orphan event(s).")
    L.append("")
    return "\n".join(L)


def _runtime_audit_section(screens):
    L = ["## Runtime audit (read-only caught-at provenance)", ""]
    provenance = screens.get("runtime_audit")
    if provenance is None:
        L.append("_Not requested. Gate events retain the legacy honest-null semantics._")
        L.append("")
        return "\n".join(L)

    wal = provenance["wal"]
    join = provenance["bundle_join"]
    events = screens.get("gate_events") or []
    counts = {level: 0 for level in ("L1", "L2", "L3", "L4", "L5", "production")}
    for event in events:
        counts[event["caught_at"]] += 1
    L.append(f"- **Schema:** `{provenance['schema_version']}`.")
    L.append(f"- **Runtime:** `{provenance['runtime_build_id']}` at "
             f"`{provenance['runtime_root']}` (lock mode: `{provenance['lock_mode']}`).")
    L.append("- **Captured source SHA-256:**")
    for name, digest in provenance["source_hashes"].items():
        L.append(f"  - `{name}`: `{digest}`")
    L.append(f"- **Bundle join:** `{join['node_address']}` / session "
             f"`{join['session_uuid']}` / immutable `{join['level']}` "
             f"by `{join['match_basis']}`.")
    L.append(f"- **Clean WAL:** {wal['clean_record_count']} record(s); last sequence "
             f"{wal['last_sequence'] if wal['last_sequence'] is not None else 'none'}.")
    if wal["torn_tail"] is None:
        L.append("- **Torn tail:** none.")
    else:
        tail = wal["torn_tail"]
        L.append(f"- **Torn tail:** ignored at byte offset {tail['byte_offset']} "
                 f"({tail['reason']}).")
    L.append(f"- **Normalized negative events:** {len(events)} total; "
             + ", ".join(f"{level}={counts[level]}" for level in counts) + ".")
    L.append("- **Introduced-at:** unclassified; classification remains future work.")
    L.append("")
    return "\n".join(L)


def _spine_section(triage_result, spine_entries, skip_llm):
    L = ["## Spine results (SP-keyed)", ""]
    if skip_llm:
        L.append("_L2 triage skipped (`--skip-llm`): spine entries are not "
                 "adjudicated in this pass. Screens above are the only signal._")
        L.append("")
        for e in spine_entries:
            L.append(f"- **{e['id']} {e['title']}** — not adjudicated (mechanical-only pass).")
        L.append("")
        return "\n".join(L)
    sr = (triage_result or {}).get("spine_results", {})
    title_by_id = {e["id"]: e["title"] for e in spine_entries}
    for e in spine_entries:
        r = sr.get(e["id"], {})
        verdict = r.get("result", "no-signal")
        note = r.get("note", "")
        anchors = r.get("anchors") or []
        anchor_s = f" [{'; '.join(anchors)}]" if anchors else ""
        L.append(f"- **{e['id']} {title_by_id.get(e['id'], '')}** — **{verdict}**. {note}{anchor_s}")
    L.append("")
    return "\n".join(L)


def _findings_section(triage_result, skip_llm):
    L = ["## Findings (anchored, impact-tiered)", ""]
    if skip_llm:
        L.append("_No findings — L2 triage skipped (`--skip-llm`)._")
        L.append("")
        return "\n".join(L)
    findings = (triage_result or {}).get("findings", [])
    if not findings:
        L.append("_No findings ranked by triage (an honest thin-run outcome)._")
        L.append("")
        return "\n".join(L)
    order = {"severe": 0, "notable": 1, "minor": 2, "trivial": 3}
    for f in sorted(findings, key=lambda f: order.get(f.get("impact"), 9)):
        anchors = f.get("anchors") or []
        anchor_s = f" [{'; '.join(anchors)}]" if anchors else ""
        ref = f.get("spine_ref")
        ref_s = f" ({ref})" if ref else ""
        L.append(f"### {f.get('id', '?')} — {f.get('title', 'untitled')} "
                 f"[{f.get('impact', 'unrated')}]{ref_s}")
        L.append(f"{f.get('rationale', '')}{anchor_s}")
        if f.get("cost_anchors"):
            L.append(f"- Cost anchors: {f['cost_anchors']}")
        L.append("")
    return "\n".join(L)


def _deepdive_section(deep_dive_records, targets, skip_llm):
    L = ["## Deep dives (L3 — empathetic replay of above-minor flags)", ""]
    if skip_llm:
        L.append("_No deep dives — L2/L3 skipped (`--skip-llm`)._")
        L.append("")
        return "\n".join(L)
    if not targets:
        L.append("_No above-minor descent targets — no deep dive required "
                 "(done-definition met: nothing above threshold to diagnose)._")
        L.append("")
        return "\n".join(L)
    for r in deep_dive_records:
        rec = r.get("record", {})
        disp = rec.get("disposition")
        L.append(f"### {r.get('finding_id', '?')} — {disp}")
        if disp == "deferred":
            L.append(f"- **Deferred:** {rec.get('deferral_reason')}")
        else:
            L.append(f"{rec.get('diagnosis', '')}")
            if rec.get("local_surface_note"):
                L.append(f"- Local surface: {rec['local_surface_note']}")
            if rec.get("impact_read"):
                L.append(f"- Impact read: {rec['impact_read']}")
        L.append("")
    return "\n".join(L)


def _ledger_section(triage_result, skip_llm):
    L = ["## Proposed ledger entries (verifier-gated — NOT written here)", ""]
    if skip_llm:
        L.append("_None — L2 triage skipped (`--skip-llm`)._")
        L.append("")
        return "\n".join(L)
    props = (triage_result or {}).get("ledger_proposals", [])
    if not props:
        L.append("_No ledger proposals from this pass._")
        L.append("")
        return "\n".join(L)
    L.append("The coordinator batches these to the director for per-entry approval. "
             "The observatory proposes; it never writes the ledger.")
    L.append("")
    for i, p in enumerate(props, 1):
        anchors = p.get("anchors") or []
        anchor_s = f" [{'; '.join(anchors)}]" if anchors else ""
        L.append(f"{i}. **[{p.get('admission_class', '?')}]** "
                 f"(impact: {p.get('impact', '?')}, spine: {p.get('spine_ref') or '—'}) "
                 f"{p.get('text', '')}{anchor_s}")
    L.append("")
    return "\n".join(L)


def _scope_section(screens, spine_source, deep_dive_records, skip_llm, extra_notes):
    L = ["## Scope notes", ""]
    L.append(f"- **Gate events:** {screens.get('gate_events_note')}")
    L.append(f"- **Partial:** the trace is a prefix (`partial={screens.get('partial')}`); "
             "no completeness claim is made.")
    sub = screens.get("subagents", {})
    if (sub.get("orphan_events") or 0) > 0:
        L.append(f"- **Orphan coverage:** {sub.get('orphan_events')} unattributed "
                 "orphan event(s) exist in this window; they inform no attributed claim.")
    codex_coverage = {
        actor: (a.get("coverage") or {})
        for actor, a in (screens.get("orientation") or {}).items()
        if (a.get("coverage") or {}).get("source_format") == "codex"
    }
    if codex_coverage:
        actors = ", ".join(f"`{actor}`" for actor in codex_coverage)
        fast_follow = next(
            (c.get("fast_follow") for c in codex_coverage.values() if c.get("fast_follow")),
            "classify shell-command file reads",
        )
        L.append(f"- **Codex orientation coverage ({actors}):** read counts, "
                 "time-to-first-relevant-read, reads-before-first-relevant, and "
                 "off-task-read counts are lower bounds, not complete metrics. "
                 "Only explicitly recognized direct read tools are classified; "
                 "file reads through `exec` and `exec_command` shell commands are "
                 "currently unclassified. "
                 f"First-k actions are supported. **Fast-follow:** {fast_follow}.")
    L.append(f"- **Spine source:** {spine_source} "
             "(provisional seed — director-provisional, not user-ratified canon).")
    if skip_llm:
        L.append("- **Generation:** `--skip-llm` — L2/L3 not run; this card is "
                 "mechanical-only.")
    else:
        deferrals = [r for r in (deep_dive_records or [])
                     if r.get("record", {}).get("disposition") == "deferred"]
        if deferrals:
            L.append(f"- **Deferrals:** {len(deferrals)} above-minor flag(s) deferred "
                     "with reason (thin single-run substrate; legal per P-1 "
                     "done-definition).")
    for n in extra_notes or []:
        L.append(f"- {n}")
    L.append("")
    return "\n".join(L)


def _with_anchors(prose, anchors):
    """Mirror how the card renders a claim: prose + its appended `[actor step]`
    suffix. A claim is anchored if it carries an inline cite OR a populated anchors
    list — both render as visible anchors to the reader, so the detector must see
    both to avoid false positives."""
    prose = prose or ""
    if anchors:
        prose = f"{prose} [{'; '.join(anchors)}]"
    return prose


def _claims_text(triage_result, deep_dive_records):
    """The LLM-authored behavioral prose on the card — findings, spine notes, ledger
    proposals, and deep-dive diagnoses. This, not the tool-authored mechanical
    screens / scope notes, is what the anchor discipline binds (director rider:
    report-card findings anchor exactly like digest statements). Ledger proposals
    are exactly what flows to the director's triage, so an unanchored proposal must
    warn like any other claim. Each claim is its own paragraph, rendered the way the
    card renders it, so the detector sees them individually."""
    paras = []
    if triage_result:
        for f in triage_result.get("findings", []):
            paras.append(_with_anchors(f"{f.get('title', '')}. {f.get('rationale', '')}",
                                       f.get("anchors")))
        for sid, r in (triage_result.get("spine_results") or {}).items():
            if isinstance(r, dict) and r.get("note"):
                paras.append(_with_anchors(f"{sid}: {r['note']}", r.get("anchors")))
        for p in triage_result.get("ledger_proposals", []):
            if isinstance(p, dict) and p.get("text"):
                paras.append(_with_anchors(p["text"], p.get("anchors")))
    for r in deep_dive_records or []:
        rec = r.get("record", {})
        # the deep-dive section renders these with inline anchors only (no suffix)
        for k in ("diagnosis", "impact_read", "local_surface_note"):
            if rec.get(k):
                paras.append(rec[k])
    return "\n\n".join(paras)


def build_report_card(screens, spine_entries, spine_source, triage_record,
                      deep_dive_records, targets, meta):
    skip_llm = meta.get("skip_llm", False)
    triage_result = (triage_record or {}).get("result") if triage_record else None

    if triage_record and triage_record.get("generation_error"):
        meta = dict(meta)
        meta["extra_notes"] = list(meta.get("extra_notes") or []) + [
            "**L2 triage unavailable (technical claude -p failure):** "
            f"{triage_record['generation_error']} — spine entries were NOT "
            "adjudicated this pass; L1 screens stand.",
        ]

    body_sections = [
        f"# Observatory report card — `{screens.get('session_id')}`",
        "",
        _screens_section(screens),
        _runtime_audit_section(screens),
        _spine_section(triage_result, spine_entries, skip_llm),
        _findings_section(triage_result, skip_llm),
        _deepdive_section(deep_dive_records, targets, skip_llm),
        _ledger_section(triage_result, skip_llm),
        _scope_section(screens, spine_source, deep_dive_records, skip_llm,
                       meta.get("extra_notes")),
    ]
    body = "\n".join(body_sections)

    # unanchored-paragraph detector — warn, never block (same as the digest). Run
    # over the LLM-authored behavioral claims only; mechanical screens and scope
    # notes are tool-authored numeric/structural prose and are exempt by design.
    anchors = _anchor_warnings(_claims_text(triage_result, deep_dive_records))

    permission_denials = []
    if triage_record:
        permission_denials += triage_record.get("permission_denials", [])
    for r in deep_dive_records or []:
        permission_denials += r.get("permission_denials", [])

    templates = {"triage": (triage_record or {}).get("template_sha256")}
    dd_template = next((r.get("template_sha256") for r in (deep_dive_records or [])), None)
    if dd_template:
        templates["deep_dive"] = dd_template
    templates["digest"] = meta.get("digest", {}).get("template_sha256")

    header = {
        "report_card_version": REPORT_CARD_VERSION,
        "observatory_version": OBSERVATORY_VERSION,
        "session_id": screens.get("session_id"),
        "generated_at": _now(),
        "generation_path": "mechanical-only (--skip-llm)" if skip_llm else "three-layer",
        "skip_llm": skip_llm,
        "bundle": {"path": screens.get("bundle_path"), "sha256": screens.get("bundle_sha256")},
        "runtime_audit": screens.get("runtime_audit"),
        "trace_files": screens.get("trace_files", {}),
        "digest": meta.get("digest", {}),
        "template_sha256": {k: v for k, v in templates.items() if v},
        "model": (triage_record or {}).get("model") if triage_record else "n/a (mechanical-only)",
        "permission_denials": sorted(set(permission_denials)),
        "spine": {"source": spine_source, "version": "spine.v0"},
        "relevant_globs": screens.get("relevant_globs", []),
        "subject_cwd": screens.get("subject_cwd"),
        "anchor_check": {
            "detector": _ANCHOR_DETECTOR_SRC,
            "scope": "LLM-authored behavioral claims (findings, spine notes, deep-dive diagnoses)",
            "paragraphs_checked": anchors["paragraphs_checked"],
            "unanchored": len(anchors["unanchored"]),
            "note": "presence-only — cannot detect off-by-N step drift; warn-not-block",
        },
    }

    warn_lines = []
    if anchors["unanchored"]:
        warn_lines.append(f"<!-- WARNING: {len(anchors['unanchored'])} report-card "
                          "paragraph(s) with no step anchor — findings must cite steps: -->")
        for w in anchors["unanchored"]:
            warn_lines.append(f"<!--   [para {w['paragraph']}] {w['snippet']} -->")

    parts = [_yaml(header)]
    if warn_lines:
        parts.append("\n".join(warn_lines))
    parts.append(body.strip())
    card = "\n\n".join(parts) + "\n"
    return card, {"header": header, "anchor_warnings": anchors}


def write_report_card(out_dir, card_text):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "report-card.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(card_text)
    return path
