"""Tolerant JSONL parsing.

A transcript is ALWAYS a valid prefix (no-EOF assumption, C6 §3): a truncated or
unparseable *final* line is tolerated silently (live-append can catch us
mid-write); an unparseable *non-final* line is counted and reported, never fatal.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


@dataclass
class ParsedFile:
    path: str
    # rows in file order: list of (line_number, parsed_object); 1-based lines.
    rows: list = field(default_factory=list)
    lines: int = 0
    sha256: str = ""
    last_timestamp: str | None = None
    unparseable_lines: list = field(default_factory=list)
    truncated_final_line: bool = False


def _count_lines(data: bytes) -> int:
    if not data:
        return 0
    n = data.count(b"\n")
    if not data.endswith(b"\n"):
        n += 1
    return n


def parse_bytes(path: str, data: bytes) -> ParsedFile:
    pf = ParsedFile(path=path)
    pf.sha256 = hashlib.sha256(data).hexdigest()
    pf.lines = _count_lines(data)
    ends_nl = data.endswith(b"\n")

    segments = data.split(b"\n")
    n = len(segments)
    for idx, seg in enumerate(segments):
        line_no = idx + 1
        is_last_segment = idx == n - 1
        if seg.strip() == b"":
            # trailing empty segment after a final newline, or a genuine blank line
            continue
        try:
            obj = json.loads(seg)
        except Exception:
            if is_last_segment and not ends_nl:
                # tolerated: the file was caught mid-append on its final line
                pf.truncated_final_line = True
                continue
            pf.unparseable_lines.append(line_no)
            continue
        pf.rows.append((line_no, obj))
        ts = obj.get("timestamp") if isinstance(obj, dict) else None
        if ts:
            pf.last_timestamp = ts
    return pf


def parse_file(path: str) -> ParsedFile:
    with open(path, "rb") as fh:
        data = fh.read()
    return parse_bytes(path, data)
