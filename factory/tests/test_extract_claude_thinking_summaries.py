import importlib.util
import json
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "tools" / "extract_claude_thinking_summaries.py"
    spec = importlib.util.spec_from_file_location("extract_claude_thinking_summaries", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_transcript(path: Path):
    rows = [
        {"type": "user", "message": {"role": "user", "content": "go"}, "sessionId": "s1"},
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "timestamp": "2026-06-16T00:00:00Z",
            "sessionId": "s1",
            "message": {
                "role": "assistant",
                "id": "m1",
                "content": [
                    {"type": "thinking", "thinking": "visible summary one", "signature": "sig-1"},
                    {"type": "text", "text": "answer"},
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "sessionId": "s1",
            "message": {
                "role": "assistant",
                "id": "m2",
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "sig-2"},
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_extracts_one_record_per_nonempty_reasoning_block(tmp_path):
    mod = _module()
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript)

    rows, stats = mod.extract(transcript)

    assert len(rows) == 1
    assert rows[0].text == "visible summary one"
    assert rows[0].session_id == "s1"
    assert rows[0].message_id == "m1"
    assert rows[0].assistant_uuid == "a1"
    assert rows[0].content_index == 0
    assert rows[0].signature_present is True
    assert rows[0].signature_sha256 is not None
    assert stats["thinking_blocks"] == 2
    assert stats["empty_thinking_blocks"] == 1
    assert stats["populated_summary_count"] == 1


def test_empty_signed_reasoning_blocks_are_stats_not_evidence(tmp_path):
    mod = _module()
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript)

    rows, stats = mod.extract(transcript)

    assert len(rows) == 1
    assert stats["empty_thinking_blocks"] == 1


def test_extracts_redacted_and_summary_key_variants(tmp_path):
    mod = _module()
    transcript = tmp_path / "session.jsonl"
    rows = [
        {
            "type": "assistant",
            "sessionId": "s1",
            "message": {
                "role": "assistant",
                "id": "m1",
                "content": [
                    {"type": "redacted_thinking", "text": "redacted visible note"},
                    {"type": "metadata", "latestThinkingSummary": "summary key note"},
                ],
            },
        }
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    summaries, stats = mod.extract(transcript)

    assert [summary.text for summary in summaries] == ["redacted visible note", "summary key note"]
    assert stats["populated_summary_count"] == 2
