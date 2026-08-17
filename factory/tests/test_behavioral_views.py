import importlib.util
import json
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "tools" / "build_behavioral_views.py"
    spec = importlib.util.spec_from_file_location("build_behavioral_views", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sample_index():
    return {
        "schema_version": 5,
        "runtime_root": "/tmp/run",
        "runtime": {"build_id": "run-views"},
        "nodes": [
            {
                "node_address": "proj/parser#exec",
                "seat": "exec",
                "level": "L4",
                "role_variant": "L4",
                "parent_address": "proj#exec",
                "state": "running",
                "gate_required": True,
                "gate_state": "gate_passed",
                "gate_id": "gate-1",
                "gate_review_address": "proj/parser#review",
                "gate_bounce_count": 1,
                "schedule_policy": "serial_l3_workstreams",
                "admission_state": "blocked_on_sibling",
                "waiting_on_sibling": "proj/alpha#exec",
                "admission_blocked_by": "proj/alpha#exec",
                "admission_block_reason": "predecessor_terminal_not_passed",
            },
            {
                "node_address": "proj/parser#review",
                "seat": "review",
                "level": "L4+",
                "role_variant": "L4+",
                "parent_address": "proj#exec",
                "state": "done",
                "gate_for": "proj/parser#exec",
            },
            {
                "node_address": "proj/render#exec",
                "seat": "exec",
                "level": "L3",
                "role_variant": "L3",
                "parent_address": "proj#exec",
                "state": "failed",
                "failure_class": "auth_expired",
            },
            {
                "node_address": "proj#exec",
                "seat": "exec",
                "level": "L2",
                "role_variant": "L2",
                "parent_address": "L1#exec",
                "state": "running",
                "plan_alignment_state": "ready",
                "plan_alignment_package": "/tmp/run/nodes/proj/validated-plan-package.md",
                "plan_alignment_ready_artifact": "/tmp/run/nodes/proj/plan-alignment-ready.json",
                "plan_alignment_ready_sha256": "ready-sha",
                "nonterminal_marker_errors": {
                    "marker-err-1": {
                        "marker_error_key": "marker-err-1",
                        "marker_kind": "plan_alignment",
                        "marker_artifact": "/tmp/run/nodes/proj/plan-alignment-ready.json",
                        "marker_sha256": "bad-ready-sha",
                        "errors": ["package is absent"],
                    }
                },
                "nonterminal_marker_error_last_key": "marker-err-1",
            },
            {
                "node_address": "proj/bad-marker#exec",
                "seat": "exec",
                "level": "L4",
                "role_variant": "L4",
                "parent_address": "proj#exec",
                "state": "running",
                "nonterminal_marker_errors": {
                    "marker-err-2": {
                        "marker_error_key": "marker-err-2",
                        "marker_kind": "coordination_handoff",
                        "marker_artifact": "/tmp/run/nodes/proj/bad-marker/handoffs/broken.json",
                        "marker_sha256": "bad-handoff-sha",
                        "errors": ["artifact is absent"],
                    }
                },
                "nonterminal_marker_error_last_key": "marker-err-2",
            },
        ],
        "events": [
            {
                "seq": 1,
                "ts": "2026-06-16T00:00:00Z",
                "node_address": "proj/parser#exec",
                "event": "gate_candidate_submitted",
                "summary": "candidate submitted",
                "binding_delta": {"gate_id": "gate-1", "gate_state": "candidate_submitted"},
            },
            {
                "seq": 2,
                "ts": "2026-06-16T00:01:00Z",
                "node_address": "proj/parser#exec",
                "event": "gate_bounced",
                "summary": "review bounced",
                "binding_delta": {
                    "gate_id": "gate-1",
                    "gate_state": "gate_bounced",
                    "gate_bounce_count": 1,
                },
            },
            {
                "seq": 3,
                "ts": "2026-06-16T00:01:30Z",
                "node_address": "proj/parser#exec",
                "event": "gate_passed",
                "summary": "candidate passed after repair",
                "binding_delta": {
                    "gate_id": "gate-1",
                    "gate_state": "gate_passed",
                    "gate_bounce_count": 1,
                },
            },
            {
                "seq": 4,
                "ts": "2026-06-16T00:02:00Z",
                "node_address": "proj/render#exec",
                "event": "watchdog_runtime_failure",
                "summary": "auth failed",
                "binding_delta": {"failure_class": "auth_expired"},
            },
        ],
        "inbox_rows": [
            {
                "node_address": "proj/parser#exec",
                "type": "gate_bounced",
                "from": "harnessd",
                "gate_id": "gate-1",
                "candidate": "proj/parser#exec",
                "review": "proj/parser#review",
                "message": "fix interface mismatch",
                "ts": "2026-06-16T00:01:00Z",
            },
            {
                "node_address": "L1#exec",
                "type": "design-submission",
                "from": "harnessd",
                "phase": "plan_alignment",
                "child": "proj#exec",
                "package": "/tmp/run/nodes/proj/validated-plan-package.md",
                "ready_artifact": "/tmp/run/nodes/proj/plan-alignment-ready.json",
                "ready_artifact_sha256": "ready-sha",
                "message": "Validated plan package is ready for L1 plan-alignment review.",
                "ts": "2026-06-16T00:03:00Z",
            },
            {
                "node_address": "proj#exec",
                "type": "nonterminal_marker_invalid",
                "from": "harnessd",
                "marker_kind": "plan_alignment",
                "marker_error_key": "marker-err-1",
                "marker_artifact": "/tmp/run/nodes/proj/plan-alignment-ready.json",
                "marker_sha256": "bad-ready-sha",
                "errors": ["package is absent"],
                "message": "repair the marker",
                "ts": "2026-06-16T00:04:00Z",
            }
        ],
        "artifacts": [
            {
                "relpath": "nodes/proj/parser/brief.md",
                "path": "/tmp/run/nodes/proj/parser/brief.md",
                "kind": "brief",
                "owner_node_addresses": ["proj/parser#exec"],
                "node_path": "proj/parser",
                "bytes": 10,
                "sha256": "a",
                "mtime_ns": 1,
            },
            {
                "relpath": "nodes/proj/parser/report.md",
                "path": "/tmp/run/nodes/proj/parser/report.md",
                "kind": "report",
                "owner_node_addresses": ["proj/parser#exec"],
                "node_path": "proj/parser",
                "bytes": 20,
                "sha256": "b",
                "mtime_ns": 2,
            },
            {
                "relpath": "nodes/proj/parser/decisions/DD-001.md",
                "path": "/tmp/run/nodes/proj/parser/decisions/DD-001.md",
                "kind": "decision",
                "owner_node_addresses": ["proj/parser#exec"],
                "node_path": "proj/parser",
                "bytes": 30,
                "sha256": "c",
                "mtime_ns": 3,
            },
        ],
        "gate_packets": [
            {
                "node_address": "proj/parser",
                "gate_id": "gate-1",
                "packet_path": "/tmp/run/nodes/proj/parser/reviews/gate-1/review-packet.md",
                "has_review_plan": True,
            }
        ],
        "trace_stanzas": [
            {"owner_node_addresses": ["proj/parser#exec"], "trace_id": "R-001.1"}
        ],
        "requirement_references": [
            {"owner_node_addresses": ["proj/parser#exec"], "requirement_ids": ["R-001"]}
        ],
        "reasoning_summary_stats": [
            {
                "node_address": "proj/parser#exec",
                "level": "L4",
                "role_variant": "L4",
                "exists": True,
                "populated_summary_count": 2,
                "empty_thinking_blocks": 1,
            },
            {
                "node_address": "proj/render#exec",
                "level": "L3",
                "role_variant": "L3",
                "exists": True,
                "populated_summary_count": 0,
                "empty_thinking_blocks": 4,
            },
        ],
        "reasoning_summaries": [
            {"node_address": "proj/parser#exec", "text": "I should inspect the packet."},
            {"node_address": "proj/parser#exec", "text": "The interface contract is mismatched."},
        ],
        "runtime_failures": [
            {
                "node_address": "proj/render#exec",
                "failure_class": "auth_expired",
                "event": "watchdog_runtime_failure",
            }
        ],
        "infrastructure_pressure": {
            "codex": {
                "current_active_count": 1,
                "current_active_nodes": ["proj/parser#exec"],
                "max_active_count": 3,
                "known_seat_ids": ["codex-seat-1"],
                "auth_versions": ["authv-1"],
                "runtime_failure_counts": {"auth_expired": 1},
                "timeline": [{"seq": 1, "active_count": 1}],
            }
        },
    }


def test_builds_task_gate_decision_runtime_and_reasoning_views():
    mod = _module()

    payload = mod.build_views_from_index(_sample_index())

    assert payload["schema_version"] == 1
    assert payload["source_index_schema_version"] == 5
    assert payload["counts"] == {
        "tasks": 5,
        "gates": 1,
        "decisions": 1,
        "runtime_failures": 1,
        "nodes_with_reasoning_summaries": 1,
        "transcript_digest_events": 0,
        "transcript_probe_events": 0,
    }
    tasks = {row["node_address"]: row for row in payload["views"]["tasks"]}
    parser = tasks["proj/parser#exec"]
    assert parser["needs_attention"] is False
    assert parser["needs_audit"] is True
    assert parser["attention_signals"] == ["gate_bounce:count=1"]
    assert parser["audit_signals"] == ["gate_bounce:count=1"]
    assert parser["failure_signals"] == []
    assert parser["routing_signals"] == []
    assert parser["artifact_counts"] == {"brief": 1, "decision": 1, "report": 1}
    assert parser["gate_packet_count"] == 1
    assert parser["trace_stanza_count"] == 1
    assert parser["requirement_reference_file_count"] == 1
    assert parser["reasoning_summary_count"] == 2
    assert parser["schedule"]["admission_state"] == "blocked_on_sibling"
    assert parser["schedule"]["admission_blocked_by"] == "proj/alpha#exec"
    assert parser["schedule"]["admission_block_reason"] == "predecessor_terminal_not_passed"
    render = tasks["proj/render#exec"]
    assert render["needs_attention"] is True
    assert render["failure_signals"] == [
        "state:failed",
        "failure_class:auth_expired",
        "runtime_failure",
    ]
    assert render["routing_signals"] == []
    assert render["runtime_failure_count"] == 1
    project = tasks["proj#exec"]
    assert project["needs_attention"] is False
    assert project["failure_signals"] == []
    assert project["routing_signals"] == ["plan_alignment_state:ready"]
    assert project["nonterminal_marker_error_count"] == 0
    assert project["nonterminal_marker_error_total_count"] == 1
    assert project["plan_alignment"]["plan_alignment_state"] == "ready"
    assert project["plan_alignment"]["plan_alignment_ready_sha256"] == "ready-sha"
    bad_marker = tasks["proj/bad-marker#exec"]
    assert bad_marker["needs_attention"] is True
    assert bad_marker["failure_signals"] == ["nonterminal_marker_error"]
    assert bad_marker["routing_signals"] == []
    assert bad_marker["nonterminal_marker_error_count"] == 1
    assert bad_marker["nonterminal_marker_error_total_count"] == 1

    gate = payload["views"]["gates"][0]
    assert gate["gate_id"] == "gate-1"
    assert gate["producer_address"] == "proj/parser#exec"
    assert gate["review_addresses"] == ["proj/parser#review"]
    assert gate["current_gate_state"] == "gate_passed"
    assert gate["needs_attention"] is False
    assert gate["needs_audit"] is True
    assert gate["audit_signals"] == ["gate_bounce:count=1"]
    assert gate["audit_label"].startswith("LOOK HERE")
    assert gate["attention_signals"] == ["gate_bounce:count=1"]
    assert gate["failure_signals"] == []
    assert gate["routing_signals"] == []
    assert gate["packet_count"] == 1
    assert gate["has_review_plan"] is True
    assert gate["event_counts"] == {
        "gate_bounced": 1,
        "gate_candidate_submitted": 1,
        "gate_passed": 1,
    }
    assert gate["inbox_type_counts"] == {"gate_bounced": 1}

    assert payload["views"]["decisions"][0]["relpath"] == "nodes/proj/parser/decisions/DD-001.md"
    assert payload["views"]["runtime_pressure"]["codex"]["max_active_count"] == 3
    assert payload["views"]["runtime_pressure"]["runtime_failure_counts"] == {"auth_expired": 1}
    assert payload["views"]["reasoning"]["nodes_with_populated_summaries"] == ["proj/parser#exec"]
    assert payload["views"]["reasoning"]["nodes_without_populated_summaries"] == ["proj/render#exec"]


def test_builds_transcript_behavior_probe_view():
    mod = _module()
    index = {
        "schema_version": 9,
        "runtime_root": "/tmp/run",
        "runtime": {"build_id": "probe-view"},
        "nodes": [],
        "events": [],
        "inbox_rows": [],
        "artifacts": [],
        "gate_packets": [],
        "reasoning_summaries": [],
        "reasoning_summary_stats": [],
        "runtime_failures": [],
        "transcript_digest_events": [
            {
                "node_address": "proj/reviews/gate-1/reviewers/evidence-credibility#exec",
                "event_type": "reasoning_summary",
                "summary": "The lower gate already validated this.",
            }
        ],
        "transcript_probe_events": [
            {
                "node_address": "proj/reviews/gate-1/reviewers/evidence-credibility#exec",
                "level": "L2+",
                "role_variant": "L2+#review-check",
                "review_axis": "evidence-credibility",
                "command_kind": "pytest",
                "timing_bucket": "middle",
                "jsonl_line": 12,
                "command": "python3 -m pytest -q",
                "nearest_reasoning_summary": "The lower gate already validated this.",
                "nearest_assistant_text": "I will run one bounded probe.",
                "result_excerpt": "20 passed",
                "transcript_path": "/tmp/transcript.jsonl",
            }
        ],
    }

    payload = mod.build_views_from_index(index)

    assert payload["counts"]["transcript_digest_events"] == 1
    assert payload["counts"]["transcript_probe_events"] == 1
    view = payload["views"]["transcript_behavior"]
    assert view["probe_counts_by_kind"] == {"pytest": 1}
    assert view["probe_counts_by_level"] == {"L2+": 1}
    assert view["probe_counts_by_review_axis"] == {"evidence-credibility": 1}
    assert view["probe_counts_by_level_axis"] == {"L2+ / evidence-credibility": 1}
    assert view["probe_counts_by_role_axis_kind"] == {
        "L2+#review-check / evidence-credibility / pytest": 1
    }
    assert view["upper_review_probe_count"] == 1
    assert view["upper_review_probe_counts_by_axis_kind"] == {
        "evidence-credibility / pytest": 1
    }
    assert view["probe_events"][0]["nearest_reasoning_summary"] == "The lower gate already validated this."
    assert view["upper_review_probe_events"][0]["nearest_reasoning_summary"] == (
        "The lower gate already validated this."
    )


def test_cli_reads_existing_index_and_writes_output_file(tmp_path):
    mod = _module()
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(_sample_index()) + "\n", encoding="utf-8")
    output = tmp_path / "views.json"

    rc = mod.main([str(index_path), "--from-index", "--output", str(output), "--compact"])

    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["runtime_root"] == "/tmp/run"
    assert payload["views"]["gates"][0]["gate_id"] == "gate-1"
