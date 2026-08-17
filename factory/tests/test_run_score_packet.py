import importlib.util
import json
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "tools" / "build_run_score_packet.py"
    spec = importlib.util.spec_from_file_location("build_run_score_packet", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_score_packet_packages_evidence_without_assigning_verdicts():
    mod = _module()
    index = {
        "schema_version": 4,
        "runtime_root": "/tmp/run",
        "runtime": {"build_id": "build-score"},
        "nodes": [
            {
                "node_address": "proj/task#exec",
                "state": "failed",
                "level": "L5",
                "gate_state": "candidate_submitted",
                "terminal_signal": "FAILED",
            },
            {
                "node_address": "proj#exec",
                "state": "done",
                "level": "L1",
                "gate_state": "gate_passed",
            },
        ],
        "events": [
            {
                "seq": 1,
                "ts": "2026-06-16T00:00:00Z",
                "node_address": "proj/task#exec",
                "event": "gate_candidate_submitted",
                "summary": "candidate submitted",
                "binding_delta": {"gate_state": "candidate_submitted", "gate_id": "gate-1"},
            },
            {
                "seq": 2,
                "ts": "2026-06-16T00:01:00Z",
                "node_address": "proj/task#exec",
                "event": "gate_bounced",
                "summary": "review bounced candidate",
                "binding_delta": {
                    "gate_state": "gate_bounced",
                    "gate_id": "gate-1",
                    "gate_bounce_count": 1,
                },
            },
            {
                "seq": 3,
                "ts": "2026-06-16T00:01:30Z",
                "node_address": "proj/task#exec",
                "event": "gate_failed",
                "summary": "candidate artifact drift",
                "binding_delta": {
                    "gate_state": "gate_failed",
                    "gate_id": "gate-2",
                    "failure_class": "candidate_artifact_drift",
                },
            },
            {
                "seq": 4,
                "ts": "2026-06-16T00:02:00Z",
                "node_address": "proj/task#exec",
                "event": "git_merged",
                "summary": "git merge after gate PASS",
                "binding_delta": {
                    "source_branch": "proj/task",
                    "target_branch": "proj",
                    "gate_id": "gate-1",
                },
            },
            {
                "seq": 5,
                "ts": "2026-06-16T00:03:00Z",
                "node_address": "proj#exec",
                "event": "delivered",
                "summary": "promote delivered",
                "binding_delta": {
                    "deliverable_state": "delivered",
                    "delivery_destination": "in-place / no external delivery",
                },
            },
        ],
        "inbox_rows": [
            {"node_address": "proj#exec", "type": "gate_passed"},
            {"node_address": "proj#exec", "type": "child_collapsed", "failure_class": "auth_expired"},
        ],
        "gate_packets": [{"node_address": "proj/task", "gate_id": "gate-1"}],
        "return_contract_defects": [
            {
                "seq": 6,
                "node_address": "proj/task#exec",
                "signal_artifact_seen_at": "artifact-1",
                "defects": [
                    "MISSING-REPORT: report.md is required",
                    "MISSING-REQUIREMENT-CITATION: no R-id found",
                ],
            }
        ],
        "trace_stanzas": [
            {"path": "/tmp/run/nodes/proj/task/plan.md", "relpath": "nodes/proj/task/plan.md"},
            {
                "path": "/tmp/run/nodes/proj/task/report.md",
                "relpath": "nodes/proj/task/report.md",
                "line": 3,
                "parse_error": "missing required field 'kind'",
            },
        ],
        "requirement_references": [
            {
                "path": "/tmp/run/nodes/proj/task/plan.md",
                "relpath": "nodes/proj/task/plan.md",
                "artifact_kind": "plan",
                "owner_node_addresses": ["proj/task#exec"],
                "requirement_ids": ["R-001"],
                "has_trace_stanza": True,
            },
            {
                "path": "/tmp/run/nodes/proj/task/report.md",
                "relpath": "nodes/proj/task/report.md",
                "artifact_kind": "report",
                "owner_node_addresses": ["proj/task#exec"],
                "requirement_ids": ["R-001"],
                "has_trace_stanza": False,
            },
        ],
        "transcripts": [{"node_address": "proj/task#exec"}],
        "reasoning_summaries": [{"node_address": "proj/task#exec", "text": "I should inspect evidence."}],
        "reasoning_summary_stats": [
            {"node_address": "proj/task#exec", "populated_summary_count": 1}
        ],
        "runtime_failures": [
            {
                "node_address": "proj/task#exec",
                "failure_class": "auth_expired",
                "event": "watchdog_runtime_failure",
            }
        ],
    }

    packet = mod.build_score_packet_from_index(index)

    assert packet["scoring_mode"]["kind"] == "evidence_packet_only"
    assert "PASS" in packet["scoring_mode"]["verdict_policy"]
    assert packet["scoreability"]["runtime_contaminated"] is True
    assert packet["scoreability"]["excluded_node_addresses"] == ["proj/task#exec"]
    assert packet["scoreability"]["scoreable_node_addresses"] == ["proj#exec"]
    assert packet["node_inventory"]["level_counts"] == {"L1": 1, "L5": 1}
    assert packet["gate_evidence"]["gate_event_counts"] == {
        "gate_bounced": 1,
        "gate_candidate_submitted": 1,
        "gate_failed": 1,
    }
    assert packet["gate_evidence"]["routing_event_counts"] == {}
    assert packet["gate_evidence"]["audit_event_counts"] == {"gate_bounced": 1}
    assert packet["gate_evidence"]["failure_event_counts"] == {"gate_failed": 1}
    assert "gate_escalated" in packet["gate_evidence"]["routing_policy"]
    assert "LOOK CLOSER" in packet["gate_evidence"]["audit_policy"]
    assert packet["gate_evidence"]["inbox_type_counts"] == {
        "child_collapsed": 1,
        "gate_passed": 1,
    }
    assert packet["movement_evidence"]["event_counts"] == {"delivered": 1, "git_merged": 1}
    assert packet["contract_evidence"]["defect_code_counts"] == {
        "MISSING-REPORT": 1,
        "MISSING-REQUIREMENT-CITATION": 1,
    }
    assert packet["trace_evidence"]["trace_stanzas"] == 2
    assert packet["trace_evidence"]["trace_parse_errors"][0]["relpath"] == "nodes/proj/task/report.md"
    assert packet["trace_evidence"]["requirement_reference_files_without_trace"][0]["artifact_kind"] == "report"
    assert packet["reasoning_evidence"]["nodes_with_reasoning_summaries"] == ["proj/task#exec"]


def test_score_packet_carries_all_blocked_input_incidents_to_run_end_surface():
    mod = _module()
    incidents = [
        {
            "incident_id": "stall-1",
            "node_address": "proj/area#exec",
            "cancel_status": "sent",
        },
        {
            "incident_id": "stall-2",
            "node_address": "proj/other#exec",
            "cancel_status": "pending",
        },
    ]
    index = {
        "schema_version": 9,
        "runtime_root": "/tmp/run",
        "runtime": {"build_id": "build-stall"},
        "nodes": [],
        "events": [],
        "inbox_rows": [],
        "runtime_failures": [],
        "runtime_failure_incidents": [],
        "reasoning_summary_stats": [],
        "blocked_input_incidents": incidents,
    }

    packet = mod.build_score_packet_from_index(index)

    assert packet["blocked_input_evidence"] == {
        "incident_count": 2,
        "incidents": incidents,
    }


def test_score_packet_surfaces_stopped_runtime_as_scoreability_condition():
    mod = _module()
    index = {
        "schema_version": 7,
        "runtime_root": "/tmp/run",
        "runtime": {"build_id": "build-stopped", "pid": 999999},
        "nodes": [
            {"node_address": "proj#exec", "state": "running", "level": "L1"},
            {"node_address": "proj/task#exec", "state": "done", "level": "L5"},
        ],
        "events": [],
        "inbox_rows": [],
        "runtime_failures": [],
        "run_lifecycle": {
            "state": "stopped_runtime",
            "reason": "runtime pid absent, no harness tmux sessions, and nonterminal bindings remain",
            "pid": {"pid": 999999, "state": "absent"},
            "tmux": {"state": "none", "sessions": []},
            "nonterminal_node_addresses": ["proj#exec"],
            "nonterminal_node_count": 1,
        },
    }

    packet = mod.build_score_packet_from_index(index)

    assert packet["scoreability"]["runtime_contaminated"] is True
    assert packet["scoreability"]["runtime_stopped"] is True
    assert packet["scoreability"]["runtime_failure_count"] == 0
    assert packet["scoreability"]["excluded_node_addresses"] == []
    assert packet["scoreability"]["scoreable_node_addresses"] == ["proj#exec", "proj/task#exec"]
    assert packet["scoreability"]["run_lifecycle"]["state"] == "stopped_runtime"


def test_score_packet_does_not_contaminate_on_post_terminal_runtime_noise():
    mod = _module()
    index = {
        "schema_version": 8,
        "runtime_root": "/tmp/run",
        "runtime": {"build_id": "build-post-terminal-noise"},
        "nodes": [{"node_address": "proj#exec", "state": "done", "level": "L1"}],
        "events": [],
        "inbox_rows": [],
        "runtime_failures": [
            {
                "node_address": "proj#exec",
                "failure_class": "tool_call_parse_failed",
                "event": "transcript_runtime_failure",
                "contaminates": False,
                "non_contaminating_reason": "post_terminal_transcript_noise",
            }
        ],
        "run_lifecycle": {
            "state": "active_or_complete",
            "nonterminal_node_addresses": [],
            "nonterminal_node_count": 0,
        },
    }

    packet = mod.build_score_packet_from_index(index)

    assert packet["scoreability"]["runtime_contaminated"] is False
    assert packet["scoreability"]["runtime_failure_count"] == 0
    assert packet["scoreability"]["non_contaminating_runtime_failure_count"] == 1
    assert packet["scoreability"]["excluded_node_addresses"] == []
    assert packet["scoreability"]["scoreable_node_addresses"] == ["proj#exec"]


def test_score_packet_summarizes_codex_transcript_activity():
    mod = _module()
    index = {
        "schema_version": 7,
        "runtime_root": "/tmp/run",
        "runtime": {"build_id": "build-codex-activity"},
        "nodes": [{"node_address": "proj/task#exec", "state": "done", "level": "L5"}],
        "events": [],
        "inbox_rows": [],
        "runtime_failures": [],
        "transcripts": [
            {
                "node_address": "proj/task#exec",
                "transcript_path": "/tmp/codex.jsonl",
                "current_binding": True,
                "assistant_rows": 3,
                "user_rows": 1,
                "codex_response_items": 12,
                "codex_message_items": 3,
                "codex_function_call_items": 4,
                "codex_function_call_output_items": 4,
                "codex_reasoning_items": 2,
                "codex_reasoning_summary_items": 0,
                "codex_reasoning_empty_summary_items": 2,
                "codex_reasoning_encrypted_items": 2,
                "codex_event_msg_rows": 6,
                "codex_token_count_events": 2,
                "codex_tool_result_events": 3,
            }
        ],
    }

    packet = mod.build_score_packet_from_index(index)
    activity = packet["reasoning_evidence"]["transcript_activity"]

    assert activity["totals"]["assistant_rows"] == 3
    assert activity["totals"]["codex_message_items"] == 3
    assert activity["totals"]["codex_function_call_items"] == 4
    assert activity["totals"]["codex_token_count_events"] == 2
    assert activity["totals"]["codex_reasoning_empty_summary_items"] == 2
    assert activity["totals"]["codex_reasoning_encrypted_items"] == 2
    assert activity["codex_transcripts"] == [
        {
            "node_address": "proj/task#exec",
            "transcript_path": "/tmp/codex.jsonl",
            "current_binding": True,
            "codex_message_items": 3,
            "codex_function_call_items": 4,
            "codex_function_call_output_items": 4,
            "codex_reasoning_items": 2,
            "codex_reasoning_empty_summary_items": 2,
            "codex_reasoning_encrypted_items": 2,
            "codex_token_count_events": 2,
        }
    ]


def test_score_packet_cli_writes_output_file(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "binding-ledger.json").write_text("{}\n", encoding="utf-8")
    output = tmp_path / "packet.json"

    rc = mod.main([str(runtime), "--output", str(output), "--compact"])

    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["runtime_root"] == str(runtime)
    assert payload["scoring_mode"]["kind"] == "evidence_packet_only"
