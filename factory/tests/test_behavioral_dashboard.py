import importlib.util
import json
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "tools" / "build_behavioral_dashboard.py"
    spec = importlib.util.spec_from_file_location("build_behavioral_dashboard", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _views():
    return {
        "schema_version": 1,
        "source_index_schema_version": 7,
        "runtime_root": "/tmp/run",
        "runtime": {"build_id": "dash-smoke"},
        "counts": {
            "tasks": 2,
            "gates": 1,
            "decisions": 1,
            "runtime_failures": 1,
            "nodes_with_reasoning_summaries": 1,
            "transcript_digest_events": 2,
            "transcript_probe_events": 1,
        },
        "views": {
            "tasks": [
                {
                    "node_address": "proj/parser#exec",
                    "level": "L4",
                    "state": "running",
                    "gate_state": "gate_passed",
                    "failure_class": None,
                    "artifact_counts": {"brief": 1, "report": 1},
                    "attention_signals": ["gate_bounce:count=1"],
                    "failure_signals": [],
                    "routing_signals": [],
                    "audit_signals": ["gate_bounce:count=1"],
                    "needs_audit": True,
                    "needs_attention": False,
                },
                {
                    "node_address": "proj/render#exec",
                    "level": "L3",
                    "state": "failed",
                    "gate_state": "",
                    "failure_class": "auth_expired",
                    "artifact_counts": {"report": 1},
                    "attention_signals": ["state:failed", "failure_class:auth_expired"],
                    "failure_signals": ["state:failed", "failure_class:auth_expired"],
                    "routing_signals": [],
                    "needs_attention": True,
                },
            ],
            "gates": [
                {
                    "gate_id": "gate-1",
                    "producer_address": "proj/parser#exec",
                    "current_gate_state": "gate_passed",
                    "gate_bounce_count": 1,
                    "failure_class": None,
                    "event_counts": {"gate_bounced": 1, "gate_passed": 1},
                    "attention_signals": ["gate_bounce:count=1"],
                    "failure_signals": [],
                    "routing_signals": [],
                    "audit_signals": ["gate_bounce:count=1"],
                    "audit_label": "LOOK HERE — something probably went wrong",
                    "needs_audit": True,
                    "needs_attention": False,
                }
            ],
            "decisions": [{"relpath": "nodes/proj/parser/decisions/DD-001.md"}],
            "runtime_pressure": {
                "codex": {
                    "current_active_count": 1,
                    "max_active_count": 3,
                    "runtime_failure_counts": {"auth_expired": 1},
                },
                "runtime_failure_counts": {"auth_expired": 1},
            },
            "reasoning": {
                "summary_count": 2,
                "nodes_with_populated_summaries": ["proj/parser#exec"],
                "nodes_without_populated_summaries": ["proj/render#exec"],
            },
            "transcript_behavior": {
                "digest_event_count": 2,
                "probe_event_count": 1,
                "probe_counts_by_kind": {"pytest": 1},
                "probe_counts_by_level": {"L2+": 1},
                "probe_counts_by_review_axis": {"evidence-credibility": 1},
                "probe_counts_by_level_axis": {"L2+ / evidence-credibility": 1},
                "probe_counts_by_role_axis_kind": {
                    "L2+#review-check / evidence-credibility / pytest": 1
                },
                "upper_review_probe_count": 1,
                "upper_review_probe_counts_by_axis_kind": {"evidence-credibility / pytest": 1},
                "probe_events": [
                    {
                        "node_address": "proj/reviews/gate-1/reviewers/evidence-credibility#exec",
                        "level": "L2+",
                        "review_axis": "evidence-credibility",
                        "command_kind": "pytest",
                        "timing_bucket": "middle",
                        "jsonl_line": 42,
                        "command": "python3 -m pytest -q",
                        "nearest_reasoning_summary": "I need to confirm the evidence is current.",
                        "result_excerpt": "20 passed",
                    }
                ],
            },
        },
    }


def _score():
    return {
        "schema_version": 1,
        "scoreability": {
            "runtime_contaminated": True,
            "runtime_stopped": False,
            "runtime_failure_count": 1,
            "run_lifecycle": {"state": "running"},
        },
        "movement_evidence": {"event_counts": {"git_merged": 1}},
    }


def test_dashboard_renders_attention_and_scoreability_without_verdicts():
    mod = _module()

    text = mod.build_dashboard(
        behavioral_views=_views(),
        score_packet=_score(),
        run_id="dash-smoke",
    )

    assert text.startswith("# Behavioral Run Dashboard: dash-smoke")
    assert "no behavioral verdict assigned" in text
    assert "runtime_contaminated: true" in text
    assert "## Failure Attention Nodes" in text
    assert "| proj/render#exec | L3 | failed |  | state:failed, failure_class:auth_expired | report=1 |" in text
    assert "## Gate Bounce Audit — Look Here" in text
    assert "| gate-1 | proj/parser#exec | gate_passed | 1 | gate_bounce:count=1 | gate_bounced=1, gate_passed=1 |" in text
    assert "## Review Routing Gates" in text
    assert "| gate-1 | proj/parser#exec | gate_passed | 1 |" not in text.split(
        "## Review Routing Gates", 1
    )[1].split("## Runtime Pressure", 1)[0]
    assert "codex_runtime_failures: auth_expired=1" in text
    assert "transcript_probe_events: 1" in text
    assert "probe_counts_by_review_axis: evidence-credibility=1" in text
    assert "probe_counts_by_level_axis: L2+ / evidence-credibility=1" in text
    assert "probe_counts_by_role_axis_kind: L2+#review-check / evidence-credibility / pytest=1" in text
    assert "upper_review_probe_count: 1" in text
    assert "upper_review_probe_counts_by_axis_kind: evidence-credibility / pytest=1" in text
    assert "| proj/reviews/gate-1/reviewers/evidence-credibility#exec | L2+ | evidence-credibility | pytest | middle | 42 | python3 -m pytest -q | I need to confirm the evidence is current. | 20 passed |" in text
    assert "movement_event_counts: git_merged=1" in text


def test_dashboard_cli_writes_markdown(tmp_path):
    mod = _module()
    views_path = tmp_path / "views.json"
    score_path = tmp_path / "score.json"
    output = tmp_path / "dashboard.md"
    views_path.write_text(json.dumps(_views()) + "\n", encoding="utf-8")
    score_path.write_text(json.dumps(_score()) + "\n", encoding="utf-8")

    rc = mod.main([
        "--views",
        str(views_path),
        "--score-packet",
        str(score_path),
        "--run-id",
        "dash-cli",
        "--output",
        str(output),
    ])

    assert rc == 0
    assert output.read_text(encoding="utf-8").startswith("# Behavioral Run Dashboard: dash-cli")


def test_dashboard_never_truncates_gate_bounce_audit_rows():
    mod = _module()
    views = _views()
    views["views"]["gates"] = [
        {
            "gate_id": f"gate-{index:02d}",
            "producer_address": f"proj/task-{index:02d}#exec",
            "current_gate_state": "gate_passed",
            "gate_bounce_count": 1,
            "event_counts": {"gate_bounced": 1, "gate_passed": 1},
            "attention_signals": ["gate_bounce:count=1"],
            "failure_signals": [],
            "routing_signals": [],
            "audit_signals": ["gate_bounce:count=1"],
            "needs_audit": True,
            "needs_attention": False,
        }
        for index in range(13)
    ]

    text = mod.build_dashboard(
        behavioral_views=views,
        score_packet=_score(),
        run_id="dash-all-bounces",
    )

    audit_section = text.split("## Gate Bounce Audit — Look Here", 1)[1].split(
        "## Failure Attention Nodes", 1
    )[0]
    for index in range(13):
        assert f"| gate-{index:02d} | proj/task-{index:02d}#exec |" in audit_section


def test_dashboard_reports_every_blocked_input_incident_and_pending_is_unknown():
    mod = _module()
    score = _score()
    score["blocked_input_evidence"] = {
        "incidents": [
            {
                "incident_id": "stall-1",
                "node_address": "proj/parser#exec",
                "started_at": "2026-07-28T10:00:00+00:00",
                "classification": "blocked_on_input",
                "silent_seconds": 600.0,
                "pane_excerpt": "Do you want to proceed? | Esc to cancel",
                "prompt_signature": "claude-choice-cancel",
                "cancel_status": "sent",
                "retriggered": False,
                "escalated": False,
            },
            {
                "incident_id": "stall-2",
                "node_address": "proj/render#exec",
                "started_at": "2026-07-28T11:00:00+00:00",
                "classification": "blocked_on_input",
                "silent_seconds": 900.0,
                "pane_excerpt": "Choose an answer | Enter to select",
                "prompt_signature": "claude-choice-select-cancel",
                "cancel_status": "pending",
                "retriggered": True,
                "escalated": True,
            },
        ]
    }

    text = mod.build_dashboard(
        behavioral_views=_views(),
        score_packet=score,
        run_id="dash-stalls",
    )

    section = text.split("## Blocked-on-input incidents", 1)[1].split(
        "## Movement Evidence", 1
    )[0]
    assert "stall-1" in section
    assert "proj/parser#exec" in section
    assert "claude-choice-cancel" in section
    assert "sent" in section
    assert "stall-2" in section
    assert "proj/render#exec" in section
    assert "unknown-delivery" in section
    assert "true" in section


def test_dashboard_plainly_states_when_no_blocked_input_incident_occurred():
    mod = _module()

    text = mod.build_dashboard(
        behavioral_views=_views(),
        score_packet=_score(),
        run_id="dash-clean",
    )

    section = text.split("## Blocked-on-input incidents", 1)[1].split(
        "## Movement Evidence", 1
    )[0]
    assert "None occurred." in section
