"""L1 mechanical screens (C1 §3.1) — no LLM, runs on 100% of every run.

Consumes the extracted trace (via the trace reader, invoked as a library) plus the
orientation view, and emits `screens.json`: token spend, tool-call counts,
repeated-identical calls (SP-2), orientation signals (SP-1), errored calls,
retry-shaped sequences, branch/compact/subagent structure, and — when a real
L1-L5 audit event log is found in the subject project dir — gate events. Every
number here is a *symptom*, not a verdict (readout/INTERPRETATION.md); diagnosis is
the triage + deep-dive layers' job.

Deterministic given the same bundle bytes: no wall-clock, no LLM.
"""

from __future__ import annotations

import collections
import glob as _glob
import hashlib
import json
import os
import re

from trace_reader.extract import extract
from trace_reader.orient import orient

from . import OBSERVATORY_VERSION

_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# HOME-/tmp-rooted absolute paths are the reliable cwd form in L1-L5 briefs;
# anchoring on a real root avoids matching slashed prose like "PASS/BOUNCE".
_ABS_PATH_RE = re.compile(
    r"/(?:Users|home)/[^/\s]+(?:/[\w.+\-]+)+"
    r"|/(?:private/tmp|tmp|opt|var|srv|mnt)/[\w.+\-]+(?:/[\w.+\-]+)*"
)
# generic path segments that never name a project root
_GENERIC_SEGS = {
    "Users", "home", "Documents", "Desktop", "tmp", "var", "opt", "private",
    "mnt", "srv", "code", "src", "repos", "projects", "dev", "work",
}

# read-only candidate names for an L1-L5 audit event log in the subject project dir
_AUDIT_GLOBS = (
    "audit*.jsonl", "audit*.log", "*audit-log*", "*audit_log*",
    ".harness/audit*", ".harness/**/audit*", "l1-l5*audit*",
    "harnessd/**/audit*.jsonl", "**/gate-events*.jsonl",
)


def _sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _load_jsonl(path):
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_json(path, default=None):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def session_id_from_bundle(bundle_path):
    return os.path.splitext(os.path.basename(bundle_path))[0]


# --- relevant-set derivation from the first user_msg task statement -----------

def _abs_paths(text):
    out = []
    for m in _ABS_PATH_RE.finditer(text or ""):
        p = m.group(0).rstrip("/.,);:\"'")
        if p.count("/") >= 2:
            out.append(p)
    return out


def _project_root_segment(path):
    segs = [s for s in path.split("/") if s]
    skip = set(_GENERIC_SEGS)
    for j, s in enumerate(segs):
        if s in ("Users", "home") and j + 1 < len(segs):
            skip.add(segs[j + 1])  # the username segment
    for s in segs:
        if s not in skip:
            return s
    return None


def derive_relevant_globs(task_text):
    """`*<project-root>*` globs from the absolute paths named in the task text."""
    roots = {_project_root_segment(p) for p in _abs_paths(task_text)}
    roots = {r for r in roots if r}
    return sorted(f"*{r}*" for r in roots) or ["*"]


def derive_subject_cwd(task_text):
    """Deepest common project root among the task text's absolute paths (or None)."""
    roots = []
    for p in _abs_paths(task_text):
        segs = [s for s in p.split("/") if s]
        skip = set(_GENERIC_SEGS)
        for j, s in enumerate(segs):
            if s in ("Users", "home") and j + 1 < len(segs):
                skip.add(segs[j + 1])
        idx = next((i for i, s in enumerate(segs) if s not in skip), None)
        if idx is not None:
            roots.append("/" + "/".join(segs[: idx + 1]))
    roots = sorted(set(roots))
    if not roots:
        return None
    if len(roots) == 1:
        return roots[0]
    try:
        return os.path.commonpath(roots)
    except ValueError:
        return None


_CMD_TAG_RE = re.compile(r"<command-[^>]*>.*?</command-[^>]*>", re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _is_substantive_task(text):
    """A task statement, not a bare slash-command wrapper. Strip command tags; if
    real prose remains (or an absolute path is present), it can carry a task."""
    cleaned = _ANY_TAG_RE.sub("", _CMD_TAG_RE.sub("", text)).strip()
    return len(cleaned) >= 20


def first_task_text(events):
    """First substantive user_msg — the in-trace task statement. Skips meta rows and
    pure slash-command wrappers (e.g. a session opened with `/message`), matching the
    orient/digest pipeline's in-trace discipline while degrading honestly."""
    first_nonempty = None
    for e in events:
        if e.get("kind") != "user_msg":
            continue
        text = (e.get("text") or "").strip()
        if not text:
            continue
        if first_nonempty is None:
            first_nonempty = e["text"]
        if e.get("meta"):
            continue
        if _is_substantive_task(e["text"]):
            return e["text"]
    return first_nonempty or ""


# --- gate-event audit log (read-only; absent is the expected v1 case) ---------

def find_gate_events(subject_cwd):
    """Best-effort READ-ONLY search for an L1-L5 audit event log.

    Returns (gate_events, note). We do NOT invent: if the schema is unknown or no
    log is found, gate_events is None and the note says exactly why (C1 §7).
    """
    if not subject_cwd or not os.path.isdir(subject_cwd):
        return None, ("no subject project dir resolved from the task text; the "
                      "caught-at axis is unpopulated (expected in v1 — no L1-L5 "
                      "audit log wired)")
    found = []
    for pat in _AUDIT_GLOBS:
        for hit in _glob.glob(os.path.join(subject_cwd, pat), recursive=True):
            if os.path.isfile(hit):
                found.append(hit)
    found = sorted(set(found))
    if not found:
        return None, ("no L1-L5 audit event log found under %s; caught-at axis "
                      "unpopulated (expected in v1)" % subject_cwd)
    return None, ("candidate audit log(s) found (%s) but the L1-L5 audit-event "
                  "schema is not known to observatory v1; NOT parsed, to avoid "
                  "fabricating gate events (C1 §7). Wire the schema to populate "
                  "the caught-at axis." % ", ".join(found))


# --- the screens computation --------------------------------------------------

def _sum_tokens(events):
    tot = collections.Counter()
    per_actor = collections.defaultdict(collections.Counter)
    for e in events:
        tk = e.get("tokens")
        if isinstance(tk, dict):
            for k in _TOKEN_KEYS:
                v = tk.get(k)
                if isinstance(v, int):
                    tot[k] += v
                    per_actor[e["actor"]][k] += v
    return dict(tot), {a: dict(c) for a, c in per_actor.items()}


def _tool_calls(active_calls):
    by_tool = collections.Counter(e.get("name") for e in active_calls)
    by_actor = collections.defaultdict(collections.Counter)
    for e in active_calls:
        by_actor[e["actor"]][e.get("name")] += 1
    return {
        "total": len(active_calls),
        "by_tool": dict(by_tool),
        "by_actor": {a: dict(c) for a, c in by_actor.items()},
    }


def _repeated_identical(active_calls):
    """Per-actor identical (tool, input sha) repeats — the SP-2 signal.

    Per-actor by design: the same file read by main AND a subagent is not
    redundancy, it is two agents each orienting.
    """
    groups = collections.OrderedDict()
    for e in active_calls:
        sha = (e.get("input_digest") or {}).get("sha256")
        if sha is None:
            continue
        key = (e["actor"], e.get("name"), sha)
        groups.setdefault(key, []).append(e)
    offenders = []
    extra = 0
    for (actor, tool, sha), evs in groups.items():
        if len(evs) > 1:
            offenders.append({
                "actor": actor,
                "tool": tool,
                "sha256": sha,
                "hint": (evs[0].get("input_digest") or {}).get("hint"),
                "steps": [e["step"] for e in evs],
                "count": len(evs),
            })
            extra += len(evs) - 1
    return {"count": extra, "group_count": len(offenders), "groups": offenders}


def _errored(all_calls):
    calls = []
    for e in all_calls:
        if e.get("result_is_error"):
            calls.append({
                "actor": e["actor"],
                "step": e["step"],
                "tool": e.get("name"),
                "branch_id": e.get("branch_id"),
                "hint": (e.get("input_digest") or {}).get("hint"),
            })
    return {"count": len(calls), "calls": calls}


def _retry_shaped(active_calls):
    """Identical (tool, input sha) call within 3 steps AFTER an errored call, same
    actor. Count only (C1 §3.1) — the sequence anchors ride along for descent."""
    by_actor = collections.defaultdict(list)
    for e in active_calls:
        by_actor[e["actor"]].append(e)
    seqs = []
    for actor, evs in by_actor.items():
        evs = sorted(evs, key=lambda e: e["step"])
        for i, e in enumerate(evs):
            if not e.get("result_is_error"):
                continue
            sha = (e.get("input_digest") or {}).get("sha256")
            for f in evs[i + 1:]:
                if f["step"] - e["step"] > 3:
                    break
                if f.get("name") == e.get("name") and \
                        (f.get("input_digest") or {}).get("sha256") == sha:
                    seqs.append({"actor": actor, "error_step": e["step"],
                                 "retry_step": f["step"], "tool": e.get("name")})
                    break
    return {"count": len(seqs), "sequences": seqs}


def _orientation(orient_result):
    out = {}
    for actor, a in (orient_result.get("actors") or {}).items():
        frr = a.get("first_relevant_read")
        out[actor] = {
            "coverage": a.get("coverage"),
            "read_count": a.get("read_count"),
            "time_to_first_relevant_read_ms": (frr or {}).get("latency_ms"),
            "first_relevant_read_step": (frr or {}).get("step"),
            "reads_before_first_relevant": (a.get("reads_before_first_relevant") or {}).get("count"),
            "off_task_read_count": a.get("off_task_read_count"),
            "off_task_paths": a.get("off_task_paths"),
        }
    return out


def run_screens(work_dir, bundle_path, relevant_globs, subject_cwd, orient_result,
                gate_events, gate_note, runtime_audit=None):
    active = _load_jsonl(os.path.join(work_dir, "trace.jsonl"))
    branch = _load_jsonl(os.path.join(work_dir, "branches.jsonl"))
    actors = _load_json(os.path.join(work_dir, "actors.json"), [])
    meta = _load_json(os.path.join(work_dir, "meta.json"), {})
    counts = meta.get("counts", {})

    active_calls = [e for e in active if e.get("kind") == "tool_call"]
    all_calls = active_calls + [e for e in branch if e.get("kind") == "tool_call"]

    tok_total, tok_actor = _sum_tokens(active)
    branch_tok, _ = _sum_tokens(branch)

    trace_hashes = {}
    for name in ("trace.jsonl", "branches.jsonl", "actors.json", "meta.json",
                 "orient.json"):
        p = os.path.join(work_dir, name)
        if os.path.isfile(p):
            trace_hashes[name] = _sha256_file(p)

    branch_ids = sorted({e.get("branch_id") for e in branch if e.get("branch_id")})

    return {
        "screens_version": OBSERVATORY_VERSION,
        "session_id": session_id_from_bundle(bundle_path),
        "bundle_path": bundle_path,
        "bundle_sha256": _sha256_file(bundle_path) if os.path.isfile(bundle_path) else None,
        "trace_files": trace_hashes,
        "relevant_globs": relevant_globs,
        "subject_cwd": subject_cwd,
        "format": orient_result.get("format"),
        "token_spend": {
            "total": tok_total,
            "per_actor": tok_actor,
            "branch_tokens": branch_tok,
        },
        "tool_calls": _tool_calls(active_calls),
        "repeated_identical_calls": _repeated_identical(active_calls),
        "orientation": _orientation(orient_result),
        "errored_calls": _errored(all_calls),
        "retry_shaped": _retry_shaped(active_calls),
        "branches": {
            "branch_groups": counts.get("branch_groups"),
            "distinct_branch_ids": len(branch_ids),
            "branch_ids": branch_ids,
        },
        "compact_boundaries": counts.get("compact_boundaries"),
        "subagents": {
            "joins": counts.get("subagent_joins"),
            "orphan_actors": counts.get("orphan_actors"),
            "orphan_events": counts.get("orphan_events"),
            "actors": [a.get("actor") for a in actors],
        },
        "gate_events": gate_events,
        "gate_events_note": gate_note,
        "runtime_audit": runtime_audit.provenance if runtime_audit is not None else None,
        "partial": bool(meta.get("partial", True)),
        "watermark": meta.get("watermark"),
    }


def screen_extracted(work_dir, bundle_path, k=15, runtime_audit=None):
    """L1 over an ALREADY-extracted trace dir: derive the relevant set -> orient ->
    find gate events -> screens. Writes orient.json + screens.json into work_dir.
    Returns (screens_dict, orient_result, relevant_globs, subject_cwd)."""
    active = _load_jsonl(os.path.join(work_dir, "trace.jsonl"))
    task = first_task_text(active)
    relevant_globs = derive_relevant_globs(task)
    subject_cwd = derive_subject_cwd(task)
    orient_result = orient(work_dir, relevant_globs, k=k)
    if runtime_audit is None:
        gate_events, gate_note = find_gate_events(subject_cwd)
    else:
        gate_events = runtime_audit.events
        gate_note = (
            "caught-at populated from validated harness runtime audit "
            f"{runtime_audit.provenance['schema_version']}; introduced-at remains unclassified"
        )
    screens = run_screens(work_dir, bundle_path, relevant_globs, subject_cwd,
                          orient_result, gate_events, gate_note, runtime_audit)
    with open(os.path.join(work_dir, "screens.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(screens, ensure_ascii=False, indent=2))
        fh.write("\n")
    return screens, orient_result, relevant_globs, subject_cwd


def extract_and_screen(bundle_path, work_dir, k=15, runtime_audit=None):
    """Full L1: extract -> orient -> screens. Returns (screens_dict, orient_result,
    relevant_globs, subject_cwd). Writes trace + orient.json into work_dir."""
    os.makedirs(work_dir, exist_ok=True)
    extract(bundle_path, work_dir)
    return screen_extracted(work_dir, bundle_path, k=k, runtime_audit=runtime_audit)
