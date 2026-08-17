"""Stage-2 Behavioral-Record digest (C6 §1/§3/§4, director ruling D2).

Distinct from `digest.py` (which makes the mechanical {hint,sha256,bytes} field
digests inside the trace). This module drives an LLM over the *trace only* to
produce a named, anchored Behavioral Record, then wraps/validates/stamps it.

Generation is `claude -p` PRIMARY: the versioned prompt template instructs a
headless agent to read the trace files and emit the digest body; the tool captures
that body, mechanically checks anchoring, and stamps a provenance header. On a
*technical* generation failure the tool falls back to an agent-seat scaffold (same
template) rather than fabricating content. The `generator` argument is injectable
so unit tests never spend tokens.

The digest is a GENERATED view (uncitable by rule, C6 §1) — so, unlike the
extractor, it deliberately carries a wall-clock generation timestamp and is not
expected to be byte-deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

DIGEST_VERSION = "trace-reader-digest/2.0.0"

# flavor -> versioned template filename (under ../prompts/)
_TEMPLATES = {"record": "behavioral-record.v2.md"}

_GENERATION = 3
_PREDECESSOR = "digest.v2.md"

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")

# trace files the digest is allowed to rest on (orient.json optional)
_TRACE_FILES = ("trace.jsonl", "branches.jsonl", "actors.json", "meta.json", "orient.json")

# inline step anchor, including comma lists, e.g. [main 210], [main 1, 70],
# [main 25, 33, 36-37], [agent-a069... 5-9]
_ANCHOR_RE = re.compile(
    r"\[[A-Za-z0-9][\w/-]*\s+\d+(?:\s*-\s*\d+)?"
    r"(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*\]"
)

_FOOTER_HEADING = "## Record footer"
_FOOTER_EXEMPT_PREFIXES = ("**Provenance:**", "**Epistemic:**")
_FOOTER_EXEMPTION_REASON = (
    "non-behavioral Record footer metadata lines (Provenance and Epistemic)"
)
_ANCHOR_CHECK_NOTE = (
    "presence-only — this check cannot detect off-by-N step-anchor drift; "
    "reviewer semantic spot-check of >=10 anchors is acceptance-blocking"
)
_ANCHOR_WARNING_HEADER_RE = re.compile(
    rb"^<!-- WARNING: \d+ paragraph\(s\) with no step anchor "
    rb"\xe2\x80\x94 behavioral claims must cite steps: -->$"
)
_ANCHOR_WARNING_DETAIL_RE = re.compile(rb"^<!--   \[para \d+\] .* -->$")
_ORPHAN_WARNING_RE = re.compile(rb"^<!-- WARNING: orphan_events=\d+ .+ -->$")

_CLAUDE_CANDIDATES = ("/opt/homebrew/bin/claude",
                      os.path.expanduser("~/.local/bin/claude"))

_GENERATION_TIMEOUT_S = 600


class DigestGenerationError(RuntimeError):
    """Raised on a *technical* claude -p failure (spawn/timeout/non-success)."""


def _sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _render_generation_prompt(template_text, template_hash, trace_hashes):
    """Inject mechanically known footer hashes without changing the frozen file."""
    trace_values = "; ".join(f"{name}={value}" for name, value in trace_hashes.items())
    return (template_text
            .replace("{{PROMPT_TEMPLATE_SHA256}}", template_hash)
            .replace("{{TRACE_FILES_SHA256}}", trace_values or "none"))


def _find_claude():
    which = shutil.which("claude")
    for cand in ((which,) if which else ()) + _CLAUDE_CANDIDATES:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def claude_p_generator(template_text, trace_dir, model=None):
    """PRIMARY generator: run headless `claude -p`, capture the digest body.

    Returns (body, info). Raises DigestGenerationError on technical failure.
    """
    binary = _find_claude()
    if binary is None:
        raise DigestGenerationError("no claude binary found for headless generation")
    cmd = [binary, "-p", template_text, "--output-format", "json",
           "--allowedTools", "Read", "Glob", "Grep", "--add-dir", str(trace_dir)]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, cwd=str(trace_dir), capture_output=True,
                              text=True, timeout=_GENERATION_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DigestGenerationError(f"claude -p did not run: {e}") from e
    if proc.returncode != 0:
        raise DigestGenerationError(f"claude -p exit {proc.returncode}: {proc.stderr[:400]}")
    try:
        data = json.loads(proc.stdout)
    except Exception as e:
        raise DigestGenerationError(f"claude -p output not JSON: {e}") from e
    if data.get("is_error") or data.get("subtype") != "success":
        raise DigestGenerationError(f"claude -p reported failure: {data.get('subtype')}")
    body = (data.get("result") or "").strip()
    if not body:
        raise DigestGenerationError("claude -p returned an empty result")
    model_id = next(iter(data.get("modelUsage", {})), None) or model or "unknown"
    info = {
        "generation_path": "claude-p",
        "model_id": model_id,
        "session_id": data.get("session_id"),
        "cost_usd": data.get("total_cost_usd"),
        "permission_denials": data.get("permission_denials") or [],
    }
    return body, info


def _paragraphs(body):
    return [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]


def _footer_exemption(para, in_footer):
    if not in_footer:
        return None
    lines = [line.lstrip() for line in para.splitlines() if line.strip()]
    if lines and all(line.startswith(_FOOTER_EXEMPT_PREFIXES) for line in lines):
        return _FOOTER_EXEMPTION_REASON
    return None


def anchor_warnings(body):
    """Classify substantive body paragraphs and return true anchor misses.

    Heuristic + advisory (warn, never block): headings and short/structural
    blocks are not checked. Record-footer Provenance/Epistemic metadata is a
    named exemption; every other substantive paragraph is anchored or a true
    miss. The accounting is an explicit partition of ``paragraphs_checked``.

    LIMITATION (RQ-2 anchor audit, freeze condition 5): this check is
    PRESENCE-only. It cannot see OFF-BY-N drift — a paragraph that cites a wrong
    but plausible step (e.g. the announcing status line's step instead of the
    tool_call's, or a number miscounted across branch-step gaps) passes clean. A
    The separate reviewer semantic spot-check resolves cites back to events and
    is acceptance-blocking; it is deliberately not conflated with this checker.
    """
    warnings = []
    exemptions = []
    checked = 0
    anchored = 0
    in_footer = False
    for i, para in enumerate(_paragraphs(body)):
        first = para.splitlines()[0].lstrip()
        if first.startswith("#"):
            if first.startswith("## "):
                in_footer = first == _FOOTER_HEADING
            continue  # heading
        exemption_reason = _footer_exemption(para, in_footer)
        if exemption_reason:
            checked += 1
            exemptions.append({"paragraph": i, "reason": exemption_reason})
            continue
        # strip leading list/quote markers when measuring substance
        prose = re.sub(r"^[\s>*\-\d.)]+", "", para)
        if len(prose.replace(" ", "")) < 40:
            continue  # too short to be a behavioral claim
        checked += 1
        if _ANCHOR_RE.search(para):
            anchored += 1
        else:
            warnings.append({"paragraph": i, "snippet": " ".join(para.split())[:100]})
    return {
        "paragraphs_checked": checked,
        "anchored": anchored,
        "exempted": len(exemptions),
        "exemptions": exemptions,
        "unanchored": warnings,
    }


def _anchor_check_header(anchors):
    return {
        "paragraphs_checked": anchors["paragraphs_checked"],
        "anchored": anchors["anchored"],
        "exempted": anchors["exempted"],
        "exemption_reason": _FOOTER_EXEMPTION_REASON,
        "unanchored": len(anchors["unanchored"]),
        "note": _ANCHOR_CHECK_NOTE,
    }


def _anchor_warning_lines(anchors):
    warnings = anchors["unanchored"]
    if not warnings:
        return []
    lines = [
        f"<!-- WARNING: {len(warnings)} paragraph(s) with no step "
        "anchor — behavioral claims must cite steps: -->"
    ]
    for warning in warnings:
        lines.append(
            f"<!--   [para {warning['paragraph']}] {warning['snippet']} -->"
        )
    return lines


def _orphan_coverage_warning(body, orphan_events):
    if orphan_events and "orphan" not in body.lower():
        return ("orphan_events=%d but the digest body never mentions orphan "
                "coverage (binding note missing)" % orphan_events)
    return None


def _agent_seat_scaffold(template_text, error):
    return (
        "> GENERATION FALLBACK — `claude -p` failed technically "
        f"({error}). This digest requires **agent-seat authoring** following the "
        "template below. Until authored, it makes NO behavioral claims.\n\n"
        "## Template to author against\n\n" + template_text.strip() + "\n"
    )


def _yaml_block(header):
    """Deterministic, dependency-free YAML frontmatter for the provenance header."""
    lines = ["---"]

    def emit(key, val, indent=0):
        pad = "  " * indent
        if isinstance(val, dict):
            lines.append(f"{pad}{key}:")
            for k in val:
                emit(k, val[k], indent + 1)
        elif isinstance(val, list):
            lines.append(f"{pad}{key}:")
            for item in val:
                lines.append(f"{pad}  - {item}")
        else:
            lines.append(f"{pad}{key}: {val}")

    for key in header:
        emit(key, header[key])
    lines.append("---")
    return "\n".join(lines)


def _frontmatter_parts(data):
    """Return raw frontmatter, tail, and newline bytes without normalizing."""
    if data.startswith(b"---\r\n"):
        newline = b"\r\n"
    elif data.startswith(b"---\n"):
        newline = b"\n"
    else:
        raise ValueError("digest must begin with YAML frontmatter")
    closing = newline + b"---" + newline
    close_at = data.find(closing, 3 + len(newline))
    if close_at < 0:
        raise ValueError("digest frontmatter has no closing delimiter")
    frontmatter_end = close_at + len(newline) + 3
    return data[:frontmatter_end], data[frontmatter_end:], newline


def _replace_anchor_check(frontmatter, anchors, newline):
    lines = frontmatter.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == b"anchor_check:"]
    if len(starts) != 1:
        raise ValueError(
            f"digest frontmatter must contain exactly one anchor_check block; found {len(starts)}"
        )
    start = starts[0]
    end = start + 1
    while end < len(lines):
        bare = lines[end].rstrip(b"\r\n")
        if bare and not bare.startswith((b" ", b"\t")):
            break
        end += 1

    values = _anchor_check_header(anchors)
    replacement = [b"anchor_check:" + newline]
    for key, value in values.items():
        replacement.append(f"  {key}: {value}".encode("utf-8") + newline)
    return b"".join(lines[:start] + replacement + lines[end:])


def _split_generated_warning_prelude(tail, newline):
    """Separate generated warning comments from the exact digest body bytes."""
    separator = newline + newline
    if not tail.startswith(separator):
        raise ValueError("digest frontmatter must be followed by one blank line")
    content = tail[len(separator):]
    if not content.startswith(b"<!-- WARNING:"):
        return [], content
    split_at = content.find(separator)
    if split_at < 0:
        raise ValueError("generated warning prelude has no body separator")
    warning_blob = content[:split_at]
    warning_lines = warning_blob.split(newline)
    if not all(
        _ANCHOR_WARNING_HEADER_RE.fullmatch(line)
        or _ANCHOR_WARNING_DETAIL_RE.fullmatch(line)
        or _ORPHAN_WARNING_RE.fullmatch(line)
        for line in warning_lines
    ):
        # A body is allowed to start with an unrelated warning-like comment.
        return [], content
    return warning_lines, content[split_at + len(separator):]


def _without_anchor_warnings(warning_lines):
    kept = []
    skipping_details = False
    for line in warning_lines:
        if _ANCHOR_WARNING_HEADER_RE.fullmatch(line):
            skipping_details = True
            continue
        if skipping_details and _ANCHOR_WARNING_DETAIL_RE.fullmatch(line):
            continue
        skipping_details = False
        kept.append(line)
    return kept


def _atomic_write(path, data, mode):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".digest-restamp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restamp_digest(digest_path):
    """Recompute anchor accounting over an existing digest without generation.

    Only the raw ``anchor_check`` frontmatter block and generated anchor-warning
    comments may change. All other frontmatter bytes and all body bytes are
    copied verbatim.
    """
    digest_path = str(digest_path)
    with open(digest_path, "rb") as fh:
        original = fh.read()
    frontmatter, tail, newline = _frontmatter_parts(original)
    warning_lines, body = _split_generated_warning_prelude(tail, newline)
    try:
        body_text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"digest body is not UTF-8: {e}") from e

    anchors = anchor_warnings(body_text)
    new_frontmatter = _replace_anchor_check(frontmatter, anchors, newline)
    kept_warnings = _without_anchor_warnings(warning_lines)
    new_anchor_warnings = [line.encode("utf-8") for line in _anchor_warning_lines(anchors)]
    new_warnings = new_anchor_warnings + kept_warnings

    separator = newline + newline
    new_tail = separator
    if new_warnings:
        new_tail += newline.join(new_warnings) + separator
    new_tail += body
    updated = new_frontmatter + new_tail
    if updated != original:
        _atomic_write(digest_path, updated, os.stat(digest_path).st_mode & 0o7777)
    return {
        "path": digest_path,
        "anchor_check": _anchor_check_header(anchors),
        "anchor_warnings": anchors["unanchored"],
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "changed": updated != original,
    }


def build_digest(trace_dir, flavor="record", model=None, generator=None):
    trace_dir = str(trace_dir)
    if flavor not in _TEMPLATES:
        raise ValueError(f"unknown digest flavor {flavor!r}; known: {sorted(_TEMPLATES)}")
    template_path = os.path.join(_PROMPTS_DIR, _TEMPLATES[flavor])
    with open(template_path, encoding="utf-8") as fh:
        template_text = fh.read()
    template_hash = hashlib.sha256(template_text.encode("utf-8")).hexdigest()

    trace_hashes = {}
    for name in _TRACE_FILES:
        p = os.path.join(trace_dir, name)
        if os.path.isfile(p):
            trace_hashes[name] = _sha256_file(p)

    meta = {}
    meta_path = os.path.join(trace_dir, "meta.json")
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
    orphan_events = (meta.get("counts") or {}).get("orphan_events", 0)

    generation_prompt = _render_generation_prompt(template_text, template_hash, trace_hashes)
    gen = generator or claude_p_generator
    try:
        body, info = gen(generation_prompt, trace_dir, model)
        generation_path = info.get("generation_path", "claude-p")
    except DigestGenerationError as e:
        body = _agent_seat_scaffold(generation_prompt, e)
        info = {"model_id": "agent-seat", "error": str(e)}
        generation_path = "agent-seat"

    anchors = anchor_warnings(body)
    orphan_warn = _orphan_coverage_warning(body, orphan_events)

    header = {
        "digest_version": DIGEST_VERSION,
        "flavor": flavor,
        "generation_path": generation_path,
        "model": info.get("model_id", "unknown"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generation": _GENERATION,
        "predecessor": _PREDECESSOR,
        "prompt_template": {
            "name": _TEMPLATES[flavor],
            "version": DIGEST_VERSION,
            "sha256": template_hash,
        },
        "trace_files": trace_hashes,
        "source_partial": bool(meta.get("partial", True)),
        "orphan_events": orphan_events,
        "permission_denials": info.get("permission_denials", []),
        "anchor_check": _anchor_check_header(anchors),
        "standing": "generated view — cite the anchored trace spans, not this digest",
    }
    if info.get("session_id"):
        header["generation_session_id"] = info["session_id"]
    if info.get("error"):
        header["generation_error"] = info["error"]

    warn_lines = _anchor_warning_lines(anchors)
    if orphan_warn:
        warn_lines.append(f"<!-- WARNING: {orphan_warn} -->")

    parts = [_yaml_block(header)]
    if warn_lines:
        parts.append("\n".join(warn_lines))
    parts.append(body.strip())
    digest_md = "\n\n".join(parts) + "\n"

    out_path = os.path.join(trace_dir, "digest.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(digest_md)

    return {
        "path": out_path,
        "header": header,
        "generation_path": generation_path,
        "anchor_warnings": anchors["unanchored"],
        "orphan_warning": orphan_warn,
    }
