"""Headless `claude -p` invocation for L2/L3 — trace-only sandbox (binding).

Mirrors the trace reader's digest pipeline invocation pattern exactly (director
rider): real binary by absolute path, `cwd=work_dir`, `--add-dir work_dir` only,
`--allowedTools Read Glob Grep`. The work dir holds the EXTRACTED trace + digest +
screens + spine; the raw session bundle is never added, so it is physically out of
reach. Deliberately self-contained (not importing the digest module's privates)
because that module is under concurrent edit.

`run_claude_p` is the default generator; the pipeline injects a fake in tests so no
tokens are ever spent (same discipline as the digest unit tests).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

_CLAUDE_CANDIDATES = (
    "/opt/homebrew/bin/claude",
    os.path.expanduser("~/.local/bin/claude"),
)

_DEFAULT_TIMEOUT_S = 600
_DEFAULT_ALLOWED = ("Read", "Glob", "Grep")


class LLMCallError(RuntimeError):
    """A *technical* failure of the claude -p call (spawn/timeout/non-success)."""


def find_claude():
    which = shutil.which("claude")
    for cand in ((which,) if which else ()) + _CLAUDE_CANDIDATES:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def run_claude_p(prompt_text, work_dir, model=None, allowed_tools=_DEFAULT_ALLOWED,
                 timeout_s=_DEFAULT_TIMEOUT_S):
    """Run a headless claude -p scoped to `work_dir`. Return (body, info).

    Raises LLMCallError on a technical failure; the caller decides how to degrade.
    """
    binary = find_claude()
    if binary is None:
        raise LLMCallError("no claude binary found for headless generation")
    cmd = [binary, "-p", prompt_text, "--output-format", "json", "--allowedTools",
           *allowed_tools, "--add-dir", str(work_dir)]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, cwd=str(work_dir), capture_output=True,
                              text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LLMCallError(f"claude -p did not run: {e}") from e
    if proc.returncode != 0:
        raise LLMCallError(f"claude -p exit {proc.returncode}: {proc.stderr[:400]}")
    try:
        data = json.loads(proc.stdout)
    except Exception as e:
        raise LLMCallError(f"claude -p output not JSON: {e}") from e
    if data.get("is_error") or data.get("subtype") != "success":
        raise LLMCallError(f"claude -p reported failure: {data.get('subtype')}")
    body = (data.get("result") or "").strip()
    if not body:
        raise LLMCallError("claude -p returned an empty result")
    model_id = next(iter(data.get("modelUsage", {})), None) or model or "unknown"
    info = {
        "model_id": model_id,
        "session_id": data.get("session_id"),
        "cost_usd": data.get("total_cost_usd"),
        "permission_denials": data.get("permission_denials") or [],
    }
    return body, info


def parse_json_body(body):
    """Parse an LLM response that should be a single JSON object.

    Tolerant of a ```json fence or leading/trailing prose: extracts the outermost
    balanced {...}. Raises ValueError if no JSON object is recoverable.
    """
    s = body.strip()
    if s.startswith("```"):
        # strip a fenced block
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # fall back to the outermost brace span
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(s[start:end + 1])
    raise ValueError("no JSON object found in LLM response")
