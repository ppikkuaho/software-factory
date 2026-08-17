"""Mechanical digests, token extraction, and duration — all deterministic.

A digest is {hint, sha256, bytes}: hint is a short key-field extraction (first
~120 chars, whitespace-collapsed); sha256/bytes are over the canonical JSON of the
full value. The raw is always recoverable via raw_ptr, so the digest never needs
to be reversible.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

HINT_MAX = 120

# priority order of "key fields" whose value best names a tool call
_KEY_FIELDS = (
    "command",
    "file_path",
    "path",
    "notebook_path",
    "pattern",
    "prompt",
    "description",
    "query",
    "url",
    "old_string",
    "new_string",
    "content",
    "subagent_type",
)


def collapse(text: str) -> str:
    return " ".join(text.split())


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _hint_for_value(value) -> str:
    if isinstance(value, str):
        return collapse(value)[:HINT_MAX]
    if isinstance(value, dict):
        for k in _KEY_FIELDS:
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return collapse(v)[:HINT_MAX]
        # fall back to the first non-empty string value, else canonical JSON
        for k in sorted(value):
            v = value[k]
            if isinstance(v, str) and v.strip():
                return collapse(v)[:HINT_MAX]
        return collapse(json.dumps(value, sort_keys=True, ensure_ascii=False))[:HINT_MAX]
    if isinstance(value, list):
        # tool_result content is often a list of blocks; hint from concatenated text
        parts = []
        for b in value:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        if parts:
            return collapse(" ".join(parts))[:HINT_MAX]
        return collapse(json.dumps(value, sort_keys=True, ensure_ascii=False))[:HINT_MAX]
    return collapse(str(value))[:HINT_MAX]


def digest(value) -> dict:
    canon = _canonical(value)
    return {
        "hint": _hint_for_value(value),
        "sha256": hashlib.sha256(canon).hexdigest(),
        "bytes": len(canon),
    }


def extract_tokens(usage) -> dict | None:
    """Per-message token usage incl. cache split, if present."""
    if not isinstance(usage, dict):
        return None
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    out = {}
    for k in keys:
        v = usage.get(k)
        if isinstance(v, int):
            out[k] = v
    if not out:
        return None
    return out


def _parse_ts(ts: str | None):
    if not ts or not isinstance(ts, str):
        return None
    s = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def duration_ms(start_ts: str | None, end_ts: str | None) -> int | None:
    a, b = _parse_ts(start_ts), _parse_ts(end_ts)
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds() * 1000.0
    return int(round(delta))
