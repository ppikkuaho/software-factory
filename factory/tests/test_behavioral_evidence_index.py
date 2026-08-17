import importlib.util
import hashlib
import json
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "tools" / "build_behavioral_evidence_index.py"
    spec = importlib.util.spec_from_file_location("build_behavioral_evidence_index", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows, *, framed=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for row in rows:
        payload = json.dumps(row)
        if framed:
            lines.append(f"{len(payload)}\t{payload}")
        else:
            lines.append(payload)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_transcript(path: Path, *, session_id="sess-1", thought="I need to verify the packet before judging."):
    rows = [
        {
            "type": "session_meta",
            "sessionId": session_id,
            "cwd": str(path.parent),
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "cwd": str(path.parent),
            "uuid": f"assistant-{session_id}",
            "timestamp": "2026-06-16T00:00:00Z",
            "message": {
                "id": f"msg-{session_id}",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": thought,
                        "signature": f"sig-{session_id}",
                    },
                    {"type": "text", "text": "Reading packet."},
                ],
            },
        },
    ]
    _write_jsonl(path, rows)


def test_builds_index_with_wal_nodes_inboxes_and_reasoning_join(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    transcript = tmp_path / "transcripts" / "node.jsonl"
    old_transcript = tmp_path / "transcripts" / "node-old.jsonl"
    _write_transcript(transcript, session_id="sess-1", thought="I need to verify the packet before judging.")
    _write_transcript(old_transcript, session_id="sess-old", thought="The earlier incarnation hit a gate issue.")

    _write_json(runtime / "runtime.json", {"build_id": "build-test", "pid": 123})
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/widget#exec": {
                "node_address": "proj/widget#exec",
                "state": "running",
                "level": "L4",
                "role_variant": "L4",
                "parent_address": "proj#exec",
                "gate_required": True,
                "gate_state": "candidate_submitted",
                "gate_id": "gate-1",
                "terminal_signal": "FAILED",
                "terminal_note": "auth_expired",
                "failure_class": "auth_expired",
                "runtime_failure": {
                    "failure_class": "auth_expired",
                    "runtime": "codex",
                    "error_code": "unauthorized",
                    "summary": "Codex OAuth refresh failed in transcript",
                    "line": 2,
                },
                "nonterminal_marker_errors": {
                    "marker-err-1": {
                        "marker_error_key": "marker-err-1",
                        "marker_kind": "coordination_handoff",
                        "marker_artifact": str(runtime / "nodes" / "proj" / "widget" / "handoffs" / "broken.json"),
                        "marker_sha256": "marker-sha",
                        "errors": ["referenced artifact is absent"],
                        "observed_at": "2026-06-16T00:00:06Z",
                    }
                },
                "nonterminal_marker_error_last_key": "marker-err-1",
                "model_used": "opus-4.8 / claude-code",
                "session_uuid": "sess-1",
                "transcript_path": str(transcript),
                "tmux_target": "harness-proj-widget-exec:0.0",
                "owner_token": "secret-token",
                "lease_epoch": 2,
                "generation": 5,
            }
        },
    )
    _write_jsonl(
        runtime / "run-ledger.jsonl",
        [
            {
                "seq": 1,
                "ts": "2026-06-16T00:00:01Z",
                "node_address": "proj/widget#exec",
                "event": "gate_candidate_submitted",
                "actor": "harnessd",
                "from_state": "running",
                "to_state": "running",
                "generation": 5,
                "lease_epoch": 2,
                "summary": "candidate submitted",
                "binding_delta": {"gate_state": "candidate_submitted", "gate_id": "gate-1"},
            },
            {
                "seq": 2,
                "ts": "2026-06-16T00:00:03Z",
                "node_address": "proj/widget#exec",
                "event": "spawn_open",
                "actor": "harnessd",
                "from_state": "claimed",
                "to_state": "spawning",
                "generation": 6,
                "lease_epoch": 3,
                "summary": "old incarnation spawn",
                "binding_delta": {
                    "session_uuid": "sess-old",
                    "transcript_path": str(old_transcript),
                    "model_used": "opus-4.8 / claude-code",
                },
            },
            {
                "seq": 3,
                "ts": "2026-06-16T00:00:04Z",
                "node_address": "proj/widget#exec",
                "event": "return_contract_failed",
                "actor": "harnessd",
                "from_state": "running",
                "to_state": "running",
                "generation": 6,
                "lease_epoch": 3,
                "summary": "return contract refused collapse",
                "binding_delta": {
                    "defects": ["MISSING-REQUIREMENT-CITATION: report has no IDs"],
                    "signal_artifact_seen_at": "artifact-1",
                    "agent_signal_ts": "agent-ts-1",
                },
            },
            {
                "seq": 4,
                "ts": "2026-06-16T00:00:05Z",
                "node_address": "proj/widget#exec",
                "event": "watchdog_runtime_failure",
                "actor": "harnessd",
                "from_state": "running",
                "to_state": "failed",
                "generation": 6,
                "lease_epoch": 3,
                "summary": "watchdog-imposed FAILED: auth runtime failure",
                "binding_delta": {
                    "terminal_signal": "FAILED",
                    "terminal_note": "auth_expired",
                    "failure_class": "auth_expired",
                    "runtime_failure": {
                        "failure_class": "auth_expired",
                        "runtime": "codex",
                        "error_code": "unauthorized",
                        "summary": "Codex OAuth refresh failed in transcript",
                        "line": 2,
                    },
                },
            }
        ],
        framed=True,
    )
    _write_jsonl(
        runtime / "nodes" / "proj" / "widget" / ".inbox.exec.jsonl",
        [
            {
                "from": "harnessd",
                "type": "gate_bounced",
                "gate_id": "gate-1",
                "message": "review bounced",
                "ts": "2026-06-16T00:00:02Z",
            },
            {
                "from": "harnessd",
                "type": "child_collapsed",
                "child": "proj/widget#exec",
                "terminal_signal": "FAILED",
                "terminal_note": "auth_expired",
                "failure_class": "auth_expired",
                "collapse_lease_epoch": 3,
                "message": "child proj/widget#exec reached FAILED",
                "ts": "2026-06-16T00:00:05Z",
            },
            {
                "from": "harnessd",
                "type": "nonterminal_marker_invalid",
                "marker_error_key": "marker-err-1",
                "marker_kind": "coordination_handoff",
                "marker_artifact": str(runtime / "nodes" / "proj" / "widget" / "handoffs" / "broken.json"),
                "marker_sha256": "marker-sha",
                "errors": ["referenced artifact is absent"],
                "message": "repair the marker",
                "ts": "2026-06-16T00:00:06Z",
            }
        ],
    )
    (runtime / "nodes" / "proj" / "widget" / "plan.md").write_text(
        "# Plan\n\n<!-- trace: { id: R-001.1, serves: [R-001], kind: requirement, level: L4, node: proj/widget } -->\n- [ ] inspect R-001.1\n",
        encoding="utf-8",
    )
    (runtime / "nodes" / "proj" / "widget" / "decisions").mkdir()
    (runtime / "nodes" / "proj" / "widget" / "decisions" / "DD-001.md").write_text(
        "# Decision\n", encoding="utf-8"
    )

    index = mod.build_index(runtime)

    assert index["observer_effect"].startswith("read-only")
    assert index["counts"]["nodes"] == 1
    assert index["counts"]["artifacts"] == 3
    assert index["counts"]["transcripts"] == 2
    assert index["counts"]["reasoning_summaries"] == 2
    assert index["counts"]["trace_stanzas"] == 1
    assert index["counts"]["requirement_reference_files"] == 1
    assert index["counts"]["return_contract_defects"] == 1
    assert index["counts"]["runtime_failures"] == 1
    assert index["nodes"][0]["owner_token_present"] is True
    assert "owner_token" not in index["nodes"][0]
    assert index["nodes"][0]["failure_class"] == "auth_expired"
    assert index["nodes"][0]["runtime_failure"]["error_code"] == "unauthorized"
    assert index["nodes"][0]["nonterminal_marker_error_last_key"] == "marker-err-1"
    assert index["nodes"][0]["nonterminal_marker_errors"]["marker-err-1"]["marker_kind"] == "coordination_handoff"
    assert index["events"][0]["event"] == "gate_candidate_submitted"
    assert index["events"][0]["binding_delta"] == {
        "gate_state": "candidate_submitted",
        "gate_id": "gate-1",
    }
    assert index["events"][2]["binding_delta"]["defects"] == [
        "MISSING-REQUIREMENT-CITATION: report has no IDs"
    ]
    assert index["events"][3]["binding_delta"]["runtime_failure"]["failure_class"] == "auth_expired"
    assert index["inbox_rows"][0]["node_address"] == "proj/widget#exec"
    assert index["inbox_rows"][1]["terminal_note"] == "auth_expired"
    assert index["inbox_rows"][1]["failure_class"] == "auth_expired"
    assert index["inbox_rows"][2]["type"] == "nonterminal_marker_invalid"
    assert index["inbox_rows"][2]["marker_kind"] == "coordination_handoff"
    assert index["inbox_rows"][2]["marker_error_key"] == "marker-err-1"
    assert index["inbox_rows"][2]["errors"] == ["referenced artifact is absent"]
    assert index["runtime_failures"] == [
        {
            "seq": 4,
            "ts": "2026-06-16T00:00:05Z",
            "node_address": "proj/widget#exec",
            "event": "watchdog_runtime_failure",
            "failure_class": "auth_expired",
            "runtime": "codex",
            "error_code": "unauthorized",
            "summary": "Codex OAuth refresh failed in transcript",
            "transcript_line": 2,
        }
    ]
    assert {row["session_uuid"] for row in index["session_history"]} == {"sess-1", "sess-old"}
    assert {row["session_uuid"] for row in index["reasoning_summaries"]} == {"sess-1", "sess-old"}
    assert {row["text"] for row in index["reasoning_summaries"]} == {
        "I need to verify the packet before judging.",
        "The earlier incarnation hit a gate issue.",
    }
    assert sum(row["populated_summary_count"] for row in index["reasoning_summary_stats"]) == 2
    assert index["trace_stanzas"][0]["kind"] == "trace_stanza"
    assert index["trace_stanzas"][0]["trace_id"] == "R-001.1"
    assert index["trace_stanzas"][0]["trace_serves"] == ["R-001"]
    assert index["trace_stanzas"][0]["trace_node"] == "proj/widget"
    assert index["trace_stanzas"][0]["owner_node_addresses"] == ["proj/widget#exec"]
    assert index["requirement_references"][0]["kind"] == "requirement_reference"
    assert index["requirement_references"][0]["artifact_kind"] == "plan"
    assert index["requirement_references"][0]["requirement_ids"] == ["R-001", "R-001.1"]
    assert index["requirement_references"][0]["owner_node_addresses"] == ["proj/widget#exec"]
    assert index["return_contract_defects"][0]["defects"] == [
        "MISSING-REQUIREMENT-CITATION: report has no IDs"
    ]
    artifacts = {row["relpath"]: row for row in index["artifacts"]}
    assert artifacts["nodes/proj/widget/plan.md"]["kind"] == "plan"
    assert artifacts["nodes/proj/widget/decisions/DD-001.md"]["kind"] == "decision"
    assert artifacts["nodes/proj/widget/plan.md"]["owner_node_addresses"] == ["proj/widget#exec"]


def test_evidence_index_extracts_trace_rows_from_python_tests(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    node_dir = runtime / "nodes" / "proj" / "widget"
    node_dir.mkdir(parents=True)
    (runtime / "binding-ledger.json").write_text(
        json.dumps({"proj/widget#exec": {"state": "done", "level": "L5"}}),
        encoding="utf-8",
    )
    (node_dir / "test_widget.py").write_text(
        "# <!-- trace: { id: TST-WIDGET-001, serves: [R-001], kind: test, level: L5, node: proj/widget#exec } -->\n"
        "def test_widget():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    index = mod.build_index(runtime)

    assert index["counts"]["trace_stanzas"] == 1
    assert index["counts"]["requirement_reference_files"] == 1
    assert index["trace_stanzas"][0]["trace_id"] == "TST-WIDGET-001"
    assert index["trace_stanzas"][0]["trace_kind"] == "test"
    assert index["trace_stanzas"][0]["trace_serves"] == ["R-001"]
    assert index["trace_stanzas"][0]["relpath"] == "nodes/proj/widget/test_widget.py"
    assert index["trace_stanzas"][0]["owner_node_addresses"] == ["proj/widget#exec"]


def test_discovers_gate_packet_files(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    gate_dir = runtime / "nodes" / "proj" / "widget" / "reviews" / "gate-abc"
    gate_dir.mkdir(parents=True)
    (gate_dir / "review-packet.md").write_text("# Packet\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text("# Plan\n", encoding="utf-8")
    (gate_dir / "interface-check.md").write_text("# Check\n", encoding="utf-8")
    snapshot_dir = gate_dir / "candidate-snapshot"
    snapshot_dir.mkdir()
    (snapshot_dir / "report.md").write_text("# Report\n", encoding="utf-8")
    manifest_payload = {
        "schema_version": 1,
        "candidate": "proj/widget#exec",
        "gate_id": "gate-abc",
        "root": str(runtime / "nodes" / "proj" / "widget"),
        "artifacts": [{"path": "report.md", "bytes": 9, "sha256": "abc"}],
    }
    manifest_text = json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n"
    (gate_dir / "candidate-artifacts.json").write_text(manifest_text, encoding="utf-8")
    _write_json(runtime / "binding-ledger.json", {})

    index = mod.build_index(runtime)

    assert index["counts"]["gate_packets"] == 1
    assert index["counts"]["artifacts"] == 5
    packet = index["gate_packets"][0]
    assert packet["node_address"] == "proj/widget"
    assert packet["gate_id"] == "gate-abc"
    assert packet["has_review_plan"] is True
    assert packet["candidate_artifact_manifest"] == str(gate_dir / "candidate-artifacts.json")
    assert packet["candidate_artifact_manifest_sha256"] == hashlib.sha256(
        manifest_text.encode("utf-8")
    ).hexdigest()
    assert packet["candidate_artifact_count"] == 1
    assert packet["candidate_artifact_snapshot_dir"] == str(snapshot_dir)
    assert packet["candidate_artifact_snapshot_file_count"] == 1
    assert {Path(row["path"]).name for row in packet["files"]} == {
        "candidate-artifacts.json",
        "report.md",
        "review-packet.md",
        "review-plan.md",
        "interface-check.md",
    }
    assert any(
        artifact["kind"] == "candidate_snapshot"
        and artifact["path"] == str(snapshot_dir / "report.md")
        for artifact in index["artifacts"]
    )


def test_indexes_spawn_failed_rows_as_runtime_contamination(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    _write_json(runtime / "binding-ledger.json", {})
    _write_jsonl(
        runtime / "run-ledger.jsonl",
        [
            {
                "seq": 11,
                "ts": "2026-06-18T16:40:00Z",
                "node_address": "proj/linecheck/metrics/build/test-author#exec",
                "event": "spawn_failed",
                "actor": "harnessd",
                "from_state": "claimed",
                "to_state": "planned",
                "generation": 1,
                "lease_epoch": 3,
                "summary": (
                    "spawn-failure escalation -> L1: node "
                    "proj/linecheck/metrics/build/test-author#exec failed to spawn "
                    "(class=runtime_down, model_used=, reason=codex process exited before pane open); "
                    "claim released (§6.3)"
                ),
                "binding_delta": {
                    "failure_class": "runtime_down",
                    "failure_reason": "codex process exited before pane open",
                    "model_used": "",
                    "claim_released": True,
                },
            }
        ],
        framed=True,
    )

    index = mod.build_index(runtime)

    assert index["counts"]["runtime_failures"] == 1
    assert index["runtime_failures"] == [
        {
            "seq": 11,
            "ts": "2026-06-18T16:40:00Z",
            "node_address": "proj/linecheck/metrics/build/test-author#exec",
            "event": "spawn_failed",
            "failure_class": "runtime_down",
            "runtime": None,
            "error_code": None,
            "summary": (
                "spawn-failure escalation -> L1: node "
                "proj/linecheck/metrics/build/test-author#exec failed to spawn "
                "(class=runtime_down, model_used=, reason=codex process exited before pane open); "
                "claim released (§6.3)"
            ),
            "transcript_line": None,
            "failure_reason": "codex process exited before pane open",
            "model_used": "",
            "claim_released": True,
        }
    ]


def test_classifies_stopped_runtime_from_dead_pid_empty_tmux_and_nonterminal_bindings(
    tmp_path,
    monkeypatch,
):
    mod = _module()
    runtime = tmp_path / "runtime"
    _write_json(runtime / "runtime.json", {"build_id": "build-stopped", "pid": 999999})
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj#exec": {
                "node_address": "proj#exec",
                "state": "running",
                "level": "L1",
            }
        },
    )

    def fake_kill(pid, sig):
        raise ProcessLookupError

    class Result:
        returncode = 1
        stdout = ""
        stderr = "no server running on /tmp/tmux-501/harnessd-build-stopped"

    def fake_run(*args, **kwargs):
        assert args[0][:3] == ["tmux", "-L", "harnessd-build-stopped"]
        return Result()

    monkeypatch.setattr(mod.os, "kill", fake_kill)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    index = mod.build_index(runtime)

    assert index["schema_version"] == 9
    assert index["run_lifecycle"] == {
        "state": "stopped_runtime",
        "reason": "runtime pid absent, no harness tmux sessions, and nonterminal bindings remain",
        "pid": {"pid": 999999, "state": "absent"},
        "tmux": {
            "state": "none",
            "socket": "harnessd-build-stopped",
            "sessions": [],
            "error": "no server running on /tmp/tmux-501/harnessd-build-stopped",
        },
        "nonterminal_node_addresses": ["proj#exec"],
        "nonterminal_node_count": 1,
    }


def test_classifies_stopped_runtime_from_dead_pid_stale_tmux_and_nonterminal_bindings(
    tmp_path,
    monkeypatch,
):
    mod = _module()
    runtime = tmp_path / "runtime"
    _write_json(runtime / "runtime.json", {"build_id": "build-stale-panes", "pid": 999999})
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj#exec": {
                "node_address": "proj#exec",
                "state": "running",
                "level": "L1",
            },
            "proj/task#exec": {
                "node_address": "proj/task#exec",
                "state": "done",
                "level": "L5",
            },
        },
    )

    def fake_kill(pid, sig):
        raise ProcessLookupError

    class Result:
        returncode = 0
        stdout = "harness-L1-exec: 1 windows (created Thu Jun 18 21:56:59 2026)\n"
        stderr = ""

    def fake_run(*args, **kwargs):
        assert args[0][:3] == ["tmux", "-L", "harnessd-build-stale-panes"]
        return Result()

    monkeypatch.setattr(mod.os, "kill", fake_kill)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    index = mod.build_index(runtime)

    assert index["run_lifecycle"] == {
        "state": "stopped_runtime",
        "reason": "runtime pid absent, harness tmux sessions still present, and nonterminal bindings remain",
        "pid": {"pid": 999999, "state": "absent"},
        "tmux": {
            "state": "present",
            "socket": "harnessd-build-stale-panes",
            "sessions": ["harness-L1-exec: 1 windows (created Thu Jun 18 21:56:59 2026)"],
        },
        "nonterminal_node_addresses": ["proj#exec"],
        "nonterminal_node_count": 1,
    }


def test_indexes_transcript_only_codex_runtime_failures(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    transcript = tmp_path / "transcripts" / "codex.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "event_msg",
                "timestamp": "2026-06-16T00:00:00Z",
                "payload": {
                    "type": "error",
                    "message": "Access token cannot be refreshed because the refresh token was already used.",
                    "codex_error_info": "unauthorized",
                },
            }
        ],
    )
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/codex#exec": {
                "node_address": "proj/codex#exec",
                "state": "failed",
                "level": "L5",
                "session_uuid": "sess-codex",
                "transcript_path": str(transcript),
                "model_used": "gpt-5.5 / codex",
                "codex_seat_id": "harness-proj-codex-exec-1234",
                "auth_version": "authv-99-123",
                "codex_access_seconds_remaining": 86400,
            }
        },
    )
    _write_jsonl(
        runtime / "run-ledger.jsonl",
        [
            {
                "seq": 1,
                "ts": "2026-06-16T00:00:01Z",
                "node_address": "proj/codex#exec",
                "event": "spawn_open",
                "summary": "spawn opened",
                "binding_delta": {
                    "session_uuid": "sess-codex",
                    "transcript_path": str(transcript),
                    "model_used": "gpt-5.5 / codex",
                    "codex_seat_id": "harness-proj-codex-exec-1234",
                    "auth_version": "authv-99-123",
                    "codex_access_seconds_remaining": 86400,
                },
            }
        ],
        framed=True,
    )

    index = mod.build_index(runtime)

    assert index["nodes"][0]["codex_seat_id"] == "harness-proj-codex-exec-1234"
    assert index["nodes"][0]["auth_version"] == "authv-99-123"
    assert index["nodes"][0]["codex_access_seconds_remaining"] == 86400
    assert index["events"][0]["binding_delta"]["codex_seat_id"] == "harness-proj-codex-exec-1234"
    assert index["events"][0]["binding_delta"]["auth_version"] == "authv-99-123"
    assert index["counts"]["runtime_failures"] == 1
    assert index["runtime_failures"] == [
        {
            "seq": 1,
            "ts": "2026-06-16T00:00:00Z",
            "node_address": "proj/codex#exec",
            "event": "transcript_runtime_failure",
            "failure_class": "auth_expired",
            "runtime": "codex",
            "error_code": "unauthorized",
            "summary": "Codex OAuth refresh failed in transcript",
            "transcript_path": str(transcript),
            "transcript_line": 1,
            "session_uuid": "sess-codex",
            "session_source_seq": 1,
            "current_binding": True,
        }
    ]


def test_indexes_transcript_only_claude_api_runtime_failures(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    transcript = tmp_path / "transcripts" / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "system",
                "subtype": "api_error",
                "level": "error",
                "timestamp": "2026-06-17T07:16:34.196Z",
                "error": {
                    "message": (
                        '529 {"type":"error","error":{"type":"overloaded_error",'
                        '"message":"Overloaded"}}'
                    ),
                    "status": 529,
                    "requestId": "req_test",
                    "formatted": "529 Overloaded",
                },
            }
        ],
    )
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/claude#exec": {
                "node_address": "proj/claude#exec",
                "state": "running",
                "level": "L3",
                "session_uuid": "sess-claude",
                "transcript_path": str(transcript),
                "model_used": "claude-opus-4-8",
            }
        },
    )
    _write_jsonl(
        runtime / "run-ledger.jsonl",
        [
            {
                "seq": 1,
                "ts": "2026-06-17T07:16:33Z",
                "node_address": "proj/claude#exec",
                "event": "spawn_open",
                "summary": "spawn opened",
                "binding_delta": {
                    "session_uuid": "sess-claude",
                    "transcript_path": str(transcript),
                    "model_used": "claude-opus-4-8",
                },
            }
        ],
        framed=True,
    )

    index = mod.build_index(runtime)

    assert index["counts"]["runtime_failures"] == 1
    assert index["runtime_failures"] == [
        {
            "seq": 1,
            "ts": "2026-06-17T07:16:34.196Z",
            "node_address": "proj/claude#exec",
            "event": "transcript_runtime_failure",
            "failure_class": "runtime_provider_error",
            "runtime": "claude_code",
            "error_code": "529",
            "summary": "Claude Code provider/API error in transcript",
            "transcript_path": str(transcript),
            "transcript_line": 1,
            "session_uuid": "sess-claude",
            "session_source_seq": 1,
            "current_binding": True,
        }
    ]


def test_indexes_transcript_only_claude_tool_call_parse_failures(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    transcript = tmp_path / "transcripts" / "claude-tool-parse.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "timestamp": "2026-06-18T20:17:20.693Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "The model's tool call could not be parsed (retry also failed).",
                        }
                    ],
                },
            }
        ],
    )
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/claude#exec": {
                "node_address": "proj/claude#exec",
                "state": "running",
                "level": "L3",
                "session_uuid": "sess-claude-tool-parse",
                "transcript_path": str(transcript),
                "model_used": "opus-4.8 / claude-code",
            }
        },
    )
    _write_jsonl(
        runtime / "run-ledger.jsonl",
        [
            {
                "seq": 1,
                "ts": "2026-06-18T20:17:13Z",
                "node_address": "proj/claude#exec",
                "event": "spawn_open",
                "summary": "spawn opened",
                "binding_delta": {
                    "session_uuid": "sess-claude-tool-parse",
                    "transcript_path": str(transcript),
                    "model_used": "opus-4.8 / claude-code",
                },
            }
        ],
        framed=True,
    )

    index = mod.build_index(runtime)

    assert index["counts"]["runtime_failures"] == 1
    assert index["runtime_failures"] == [
        {
            "seq": 1,
            "ts": "2026-06-18T20:17:20.693Z",
            "node_address": "proj/claude#exec",
            "event": "transcript_runtime_failure",
            "failure_class": "tool_call_parse_failed",
            "runtime": "claude_code",
            "error_code": "tool_call_parse_failed",
            "summary": "Claude Code could not parse the model tool call after retry",
            "transcript_path": str(transcript),
            "transcript_line": 1,
            "session_uuid": "sess-claude-tool-parse",
            "session_source_seq": 1,
            "current_binding": True,
        }
    ]


def test_marks_post_terminal_transcript_failures_non_contaminating(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    transcript = tmp_path / "transcripts" / "claude-post-terminal.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "timestamp": "2026-06-18T20:17:20.693Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "The model's tool call could not be parsed (retry also failed).",
                        }
                    ],
                },
            }
        ],
    )
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/claude#exec": {
                "node_address": "proj/claude#exec",
                "state": "done",
                "level": "L1",
                "session_uuid": "sess-claude-post-terminal",
                "transcript_path": str(transcript),
                "model_used": "opus-4.8 / claude-code",
            }
        },
    )
    _write_jsonl(
        runtime / "run-ledger.jsonl",
        [
            {
                "seq": 1,
                "ts": "2026-06-18T20:17:13Z",
                "node_address": "proj/claude#exec",
                "event": "spawn_open",
                "summary": "spawn opened",
                "binding_delta": {
                    "session_uuid": "sess-claude-post-terminal",
                    "transcript_path": str(transcript),
                    "model_used": "opus-4.8 / claude-code",
                },
            },
            {
                "seq": 2,
                "ts": "2026-06-18T20:17:18Z",
                "node_address": "proj/claude#exec",
                "event": "signal_DONE",
                "summary": "terminal collapse: DONE -> done",
                "binding_delta": {"state": "done", "terminal_signal": "DONE"},
            },
        ],
        framed=True,
    )

    index = mod.build_index(runtime)

    assert index["counts"]["runtime_failures"] == 1
    assert index["runtime_failures"] == [
        {
            "seq": 1,
            "ts": "2026-06-18T20:17:20.693Z",
            "node_address": "proj/claude#exec",
            "event": "transcript_runtime_failure",
            "failure_class": "tool_call_parse_failed",
            "runtime": "claude_code",
            "error_code": "tool_call_parse_failed",
            "summary": "Claude Code could not parse the model tool call after retry",
            "transcript_path": str(transcript),
            "transcript_line": 1,
            "session_uuid": "sess-claude-post-terminal",
            "session_source_seq": 1,
            "current_binding": True,
            "contaminates": False,
            "non_contaminating_reason": "post_terminal_transcript_noise",
            "terminal_event": "signal_DONE",
            "terminal_event_seq": 2,
            "terminal_event_ts": "2026-06-18T20:17:18Z",
        }
    ]


def test_counts_codex_response_item_transcript_activity(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    transcript = tmp_path / "transcripts" / "codex-activity.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "summary": [], "encrypted_content": "opaque"},
            },
            {
                "type": "response_item",
                "payload": {"type": "message", "content": [{"type": "output_text", "text": "Done"}]},
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call", "name": "exec_command", "call_id": "call-1"},
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
            },
            {
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "call-2"},
            },
            {
                "type": "response_item",
                "payload": {"type": "custom_tool_call_output", "call_id": "call-2", "output": "ok"},
            },
            {"type": "event_msg", "payload": {"type": "token_count", "info": {}}},
            {"type": "event_msg", "payload": {"type": "exec_command_end", "call_id": "call-1"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "wake"}},
        ],
    )
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/codex#exec": {
                "node_address": "proj/codex#exec",
                "state": "done",
                "level": "L5",
                "session_uuid": "sess-codex",
                "transcript_path": str(transcript),
                "model_used": "gpt-5.5 / codex",
            }
        },
    )

    index = mod.build_index(runtime)
    stats = index["transcripts"][0]

    assert stats["assistant_rows"] == 1
    assert stats["user_rows"] == 1
    assert stats["codex_response_items"] == 6
    assert stats["codex_message_items"] == 1
    assert stats["codex_function_call_items"] == 2
    assert stats["codex_function_call_output_items"] == 2
    assert stats["codex_reasoning_items"] == 1
    assert stats["codex_reasoning_summary_items"] == 0
    assert stats["codex_reasoning_empty_summary_items"] == 1
    assert stats["codex_reasoning_encrypted_items"] == 1
    assert stats["codex_event_msg_rows"] == 3
    assert stats["codex_token_count_events"] == 1
    assert stats["codex_tool_result_events"] == 1


def test_indexes_claude_transcript_digest_and_probe_reason(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    transcript = tmp_path / "transcripts" / "reviewer.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "sessionId": "sess-reviewer",
                "cwd": str(tmp_path),
                "message": {
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "The lower gate already validated this, but I need to check whether the evidence is current enough to rely on.",
                        },
                        {"type": "text", "text": "I will run one bounded probe for currency."},
                    ]
                },
            },
            {
                "type": "assistant",
                "sessionId": "sess-reviewer",
                "cwd": str(tmp_path),
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-1",
                            "name": "Bash",
                            "input": {"command": "python3 -m pytest -q"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "sessionId": "sess-reviewer",
                "cwd": str(tmp_path),
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-1",
                            "content": "20 passed in 0.44s",
                            "is_error": False,
                        }
                    ]
                },
            },
        ],
    )
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/reviews/gate-1/reviewers/evidence-credibility#exec": {
                "node_address": "proj/reviews/gate-1/reviewers/evidence-credibility#exec",
                "state": "done",
                "level": "L2+",
                "role_variant": "L2+#review-check",
                "session_uuid": "sess-reviewer",
                "transcript_path": str(transcript),
                "model_used": "opus-4.8 / claude-code",
            }
        },
    )

    index = mod.build_index(runtime)

    assert index["schema_version"] == 9
    assert index["counts"]["transcript_digest_events"] == 4
    assert index["counts"]["transcript_probe_events"] == 1
    stats = index["transcripts"][0]
    assert stats["transcript_digest_events"] == 4
    assert stats["transcript_probe_events"] == 1
    probe = index["transcript_probe_events"][0]
    assert probe["node_address"] == "proj/reviews/gate-1/reviewers/evidence-credibility#exec"
    assert probe["level"] == "L2+"
    assert probe["role_variant"] == "L2+#review-check"
    assert probe["review_axis"] == "evidence-credibility"
    assert probe["command_kind"] == "pytest"
    assert probe["command"] == "python3 -m pytest -q"
    assert probe["nearest_reasoning_summary"].startswith("The lower gate already validated this")
    assert probe["nearest_assistant_text"] == "I will run one bounded probe for currency."
    assert probe["result_excerpt"] == "20 passed in 0.44s"
    assert probe["result_line"] == 3
    assert probe["result_is_error"] is False


def test_indexes_claude_textual_tool_invocation_rows(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    transcript = tmp_path / "transcripts" / "reviewer.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "sessionId": "sess-reviewer",
                "timestamp": "2026-06-20T00:19:53.121Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "court\n<invoke name=\"Bash\">\n"
                                "<parameter name=\"command\">tail -n +3 .inbox.review.jsonl</parameter>\n"
                                "<parameter name=\"description\">Read new inbox rows</parameter>\n"
                                "</invoke>"
                            ),
                        }
                    ],
                    "stop_reason": "end_turn",
                },
            }
        ],
    )
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/review#review": {
                "node_address": "proj/review#review",
                "state": "running",
                "level": "L4+",
                "role_variant": "L4+#review",
                "session_uuid": "sess-reviewer",
                "transcript_path": str(transcript),
                "model_used": "opus-4.8 / claude-code",
            }
        },
    )

    index = mod.build_index(runtime)

    stats = index["transcripts"][0]
    assert stats["claude_textual_tool_invocation_rows"] == 1
    malformed = [
        event for event in index["transcript_digest_events"]
        if event["event_type"] == "malformed_tool_invocation_text"
    ]
    assert len(malformed) == 1
    assert "tail -n +3 .inbox.review.jsonl" in malformed[0]["summary"]


def test_does_not_classify_signoff_prose_as_pytest_probe(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    transcript = tmp_path / "transcripts" / "signoff.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "sessionId": "sess-signoff",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-1",
                            "name": "Bash",
                            "input": {
                                "command": "cat > .signal.exec.json <<EOF\n{\"notes\":\"certified pytest re-run (20 passed)\"}\nEOF"
                            },
                        }
                    ]
                },
            }
        ],
    )
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/reviewer#exec": {
                "node_address": "proj/reviewer#exec",
                "state": "done",
                "level": "L3+",
                "role_variant": "L3+#review-check",
                "session_uuid": "sess-signoff",
                "transcript_path": str(transcript),
            }
        },
    )

    index = mod.build_index(runtime)

    assert index["counts"]["transcript_digest_events"] == 1
    assert index["counts"]["transcript_probe_events"] == 0
    assert index["transcript_probe_events"] == []


def test_indexes_codex_transcript_probe_command(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    transcript = tmp_path / "transcripts" / "codex-probe.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "I should verify the local acceptance suite before reporting."}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({"cmd": "cd app && pytest -q"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "3 passed",
                },
            },
        ],
    )
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/task#exec": {
                "node_address": "proj/task#exec",
                "state": "done",
                "level": "L5",
                "role_variant": "L5",
                "session_uuid": "sess-codex",
                "transcript_path": str(transcript),
                "model_used": "gpt-5.5 / codex",
            }
        },
    )

    index = mod.build_index(runtime)

    assert index["counts"]["transcript_probe_events"] == 1
    probe = index["transcript_probe_events"][0]
    assert probe["command_kind"] == "pytest"
    assert probe["command"] == "cd app && pytest -q"
    assert probe["nearest_reasoning_summary"] == "I should verify the local acceptance suite before reporting."
    assert probe["result_excerpt"] == "3 passed"


def test_indexes_l3_admission_state_and_codex_pressure(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    _write_json(
        runtime / "binding-ledger.json",
        {
            "proj/alpha#exec": {
                "node_address": "proj/alpha#exec",
                "state": "running",
                "level": "L3",
                "model_used": "gpt-5.5 / codex",
                "codex_seat_id": "codex-seat-alpha",
                "auth_version": "authv-1",
                "schedule_policy": "serial_l3_workstreams",
                "schedule_group": "proj#exec:L3",
                "schedule_index": 1,
                "admission_state": "admitted",
            },
            "proj/beta#exec": {
                "node_address": "proj/beta#exec",
                "state": "planned",
                "level": "L3",
                "schedule_policy": "serial_l3_workstreams",
                "schedule_group": "proj#exec:L3",
                "schedule_index": 2,
                "admission_state": "blocked_on_sibling",
                "waiting_on_sibling": "proj/alpha#exec",
                "queue_reason": "predecessor_not_passed",
                "queued_since": "2026-06-16T00:00:02Z",
                "admission_blocked_at": "2026-06-16T00:00:05Z",
                "admission_blocked_by": "proj/alpha#exec",
                "admission_block_reason": "predecessor_terminal_not_passed",
                "admission_blocked_predecessor_state": "failed",
                "admission_blocked_predecessor_gate_state": None,
            },
        },
    )
    _write_jsonl(
        runtime / "run-ledger.jsonl",
        [
            {
                "seq": 1,
                "ts": "2026-06-16T00:00:01Z",
                "node_address": "proj/alpha#exec",
                "event": "spawn_open",
                "from_state": "claimed",
                "to_state": "spawning",
                "binding_delta": {
                    "model_used": "gpt-5.5 / codex",
                    "codex_seat_id": "codex-seat-alpha",
                    "auth_version": "authv-1",
                },
            },
            {
                "seq": 2,
                "ts": "2026-06-16T00:00:02Z",
                "node_address": "proj/beta#exec",
                "event": "admission_blocked",
                "from_state": "planned",
                "to_state": "planned",
                "binding_delta": {
                    "admission_state": "blocked_on_sibling",
                    "waiting_on_sibling": "proj/alpha#exec",
                    "queue_reason": "predecessor_not_passed",
                    "admission_blocked_by": "proj/alpha#exec",
                    "admission_block_reason": "predecessor_terminal_not_passed",
                    "schedule_policy": "serial_l3_workstreams",
                    "schedule_group": "proj#exec:L3",
                    "schedule_index": 2,
                },
            },
            {
                "seq": 3,
                "ts": "2026-06-16T00:00:03Z",
                "node_address": "proj/gamma#exec",
                "event": "spawn_open",
                "from_state": "claimed",
                "to_state": "spawning",
                "binding_delta": {
                    "model_used": "gpt-5.5 / codex",
                    "codex_seat_id": "codex-seat-gamma",
                    "auth_version": "authv-1",
                },
            },
            {
                "seq": 4,
                "ts": "2026-06-16T00:00:04Z",
                "node_address": "proj/gamma#exec",
                "event": "watchdog_runtime_failure",
                "from_state": "running",
                "to_state": "failed",
                "binding_delta": {
                    "failure_class": "auth_expired",
                    "runtime_failure": {
                        "failure_class": "auth_expired",
                        "runtime": "codex",
                        "summary": "Codex OAuth refresh failed in transcript",
                    },
                },
            },
        ],
        framed=True,
    )

    index = mod.build_index(runtime)

    assert index["schema_version"] == 9
    beta = {node["node_address"]: node for node in index["nodes"]}["proj/beta#exec"]
    assert beta["admission_state"] == "blocked_on_sibling"
    assert beta["waiting_on_sibling"] == "proj/alpha#exec"
    assert beta["admission_blocked_by"] == "proj/alpha#exec"
    assert beta["admission_block_reason"] == "predecessor_terminal_not_passed"
    assert beta["schedule_index"] == 2
    waiting_event = [row for row in index["events"] if row["event"] == "admission_blocked"][0]
    assert waiting_event["binding_delta"]["queue_reason"] == "predecessor_not_passed"
    assert waiting_event["binding_delta"]["admission_block_reason"] == "predecessor_terminal_not_passed"
    codex = index["infrastructure_pressure"]["codex"]
    assert codex["current_active_count"] == 1
    assert codex["current_active_nodes"] == ["proj/alpha#exec"]
    assert codex["max_active_count"] == 2
    assert codex["runtime_failure_counts"] == {"auth_expired": 1}


def test_cli_writes_output_file(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _write_json(runtime / "binding-ledger.json", {})
    output = tmp_path / "index.json"

    rc = mod.main([str(runtime), "--output", str(output), "--compact"])

    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["runtime_root"] == str(runtime)
    assert payload["counts"]["nodes"] == 0


def test_blocked_input_incidents_join_write_ahead_intent_action_and_recovery():
    mod = _module()
    rows = [
        {
            "seq": 1,
            "ts": "2026-07-28T10:00:00+00:00",
            "node_address": "proj/area#exec",
            "event": "seat_stalled",
            "binding_delta": {
                "seat_stall_incident_id": "stall-1",
                "seat_stall_classification": "blocked_on_input",
                "seat_stall_silent_seconds": 600.0,
                "seat_stall_pane_excerpt": "Do you want to proceed? | Esc to cancel",
                "seat_stall_prompt_signature": "claude-choice-cancel",
                "seat_stall_cancel_status": "pending",
                "seat_stall_retriggered": False,
                "seat_stall_escalated": False,
            },
        },
        {
            "seq": 2,
            "ts": "2026-07-28T10:00:01+00:00",
            "node_address": "proj/area#exec",
            "event": "seat_stall_actioned",
            "binding_delta": {
                "seat_stall_incident_id": "stall-1",
                "seat_stall_cancel_status": "sent",
            },
        },
        {
            "seq": 3,
            "ts": "2026-07-28T10:02:00+00:00",
            "node_address": "proj/area#exec",
            "event": "seat_stall_recovered",
            "binding_delta": {
                "seat_stall_incident_id": "stall-1",
                "seat_stall_active": False,
                "seat_stall_recovered_at": "2026-07-28T10:02:00+00:00",
            },
        },
        {
            "seq": 4,
            "ts": "2026-07-28T11:00:00+00:00",
            "node_address": "proj/other#exec",
            "event": "seat_stalled",
            "binding_delta": {
                "seat_stall_incident_id": "stall-2",
                "seat_stall_classification": "silent_in_flight_unconfirmed",
                "seat_stall_silent_seconds": 900.0,
                "seat_stall_pane_excerpt": "waiting on HTTPS",
                "seat_stall_prompt_signature": None,
                "seat_stall_cancel_status": "not_attempted",
                "seat_stall_retriggered": False,
                "seat_stall_escalated": False,
            },
        },
    ]

    incidents = mod._blocked_input_incidents(rows)

    assert incidents == [
        {
            "incident_id": "stall-1",
            "node_address": "proj/area#exec",
            "started_at": "2026-07-28T10:00:00+00:00",
            "classification": "blocked_on_input",
            "silent_seconds": 600.0,
            "pane_excerpt": "Do you want to proceed? | Esc to cancel",
            "prompt_signature": "claude-choice-cancel",
            "cancel_status": "sent",
            "retriggered": False,
            "escalated": False,
            "actioned_at": "2026-07-28T10:00:01+00:00",
            "recovered_at": "2026-07-28T10:02:00+00:00",
        },
        {
            "incident_id": "stall-2",
            "node_address": "proj/other#exec",
            "started_at": "2026-07-28T11:00:00+00:00",
            "classification": "silent_in_flight_unconfirmed",
            "silent_seconds": 900.0,
            "pane_excerpt": "waiting on HTTPS",
            "prompt_signature": None,
            "cancel_status": "not_attempted",
            "retriggered": False,
            "escalated": False,
            "actioned_at": None,
            "recovered_at": None,
        },
    ]
