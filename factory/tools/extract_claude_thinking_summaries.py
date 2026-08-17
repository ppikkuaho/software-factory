#!/usr/bin/env python3
"""Extract populated Claude Code visible thinking-summary blocks from JSONL.

Claude Code stores assistant turns under:

    assistant.message.content[{type:"thinking", thinking:"...", signature:"..."}]

When `showThinkingSummaries` is enabled and Claude Code receives API-side
thinking summaries, the `thinking` string is the user-visible summary surface
shown by Claude Code. This extractor emits one JSON object per populated summary
with session/turn/order identity. Empty signature-only blocks are counted but
not emitted as summaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SUMMARY_KEYS = ("thinking_summary", "thinkingSummary", "latestThinkingSummary")


@dataclass
class ThinkingSummary:
    transcript_path: str
    jsonl_line: int
    session_id: str | None
    cwd: str | None
    timestamp: str | None
    assistant_uuid: str | None
    message_id: str | None
    request_id: str | None
    assistant_turn_index: int
    content_index: int
    summary_index: int
    block_type: str | None
    text: str
    signature_present: bool
    signature_sha256: str | None


def iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc


def extract(path: Path) -> tuple[list[ThinkingSummary], dict[str, Any]]:
    summaries: list[ThinkingSummary] = []
    assistant_turn_index = 0
    thinking_blocks = 0
    empty_thinking_blocks = 0

    for line_no, row in iter_jsonl(path):
        if row.get("type") != "assistant":
            continue
        assistant_turn_index += 1
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for content_index, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            block_type = part.get("type")
            text = reasoning_text(part)
            if block_type not in ("thinking", "redacted_thinking") and text is None:
                continue
            thinking_blocks += 1
            signature = part.get("signature") if isinstance(part.get("signature"), str) else ""
            if not isinstance(text, str) or text == "":
                empty_thinking_blocks += 1
                continue
            summaries.append(
                ThinkingSummary(
                    transcript_path=str(path),
                    jsonl_line=line_no,
                    session_id=row.get("sessionId"),
                    cwd=row.get("cwd"),
                    timestamp=row.get("timestamp"),
                    assistant_uuid=row.get("uuid"),
                    message_id=message.get("id"),
                    request_id=row.get("requestId"),
                    assistant_turn_index=assistant_turn_index,
                    content_index=content_index,
                    summary_index=len(summaries) + 1,
                    block_type=block_type,
                    text=text,
                    signature_present=bool(signature),
                    signature_sha256=hashlib.sha256(signature.encode("utf-8")).hexdigest()
                    if signature
                    else None,
                )
            )

    stats = {
        "transcript_path": str(path),
        "assistant_turns": assistant_turn_index,
        "thinking_blocks": thinking_blocks,
        "empty_thinking_blocks": empty_thinking_blocks,
        "populated_summary_count": len(summaries),
    }
    return summaries, stats


def reasoning_text(part: dict[str, Any]) -> str | None:
    if isinstance(part.get("thinking"), str):
        return part["thinking"]
    if part.get("type") == "redacted_thinking" and isinstance(part.get("text"), str):
        return part["text"]
    for key in SUMMARY_KEYS:
        value = part.get(key)
        if isinstance(value, str):
            return value
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcripts", nargs="+", help="Claude Code transcript .jsonl file(s)")
    parser.add_argument("--jsonl", action="store_true", help="emit one summary object per line")
    args = parser.parse_args(argv)

    all_summaries: list[ThinkingSummary] = []
    stats = []
    for transcript in args.transcripts:
        path = Path(transcript)
        summaries, file_stats = extract(path)
        all_summaries.extend(summaries)
        stats.append(file_stats)

    if args.jsonl:
        for summary in all_summaries:
            print(json.dumps(asdict(summary), ensure_ascii=False))
        return 0

    payload = {
        "capture_method": "parse Claude Code JSONL assistant.message.content[type=thinking].thinking",
        "observer_effect": "read-only transcript parse; no TUI interaction; no prompt/runtime changes",
        "file_stats": stats,
        "summary_count": len(all_summaries),
        "summaries": [asdict(summary) for summary in all_summaries],
    }
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
