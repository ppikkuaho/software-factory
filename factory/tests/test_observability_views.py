"""Pass 8 — generated-on-read ledger/artifact views and journey DAG."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessd import (
    addressing,
    clock,
    daemon,
    fencing,
    ledger,
    observability,
)


ROOT = "L1#exec"
ALPHA = "L1/alpha#exec"
BETA = "L1/beta#exec"
BETA_REVIEW = "L1/beta#review"
CHECK_ONE = "L1/beta/reviews/gate-7/reviewers/one#exec"
CHECK_TWO = "L1/beta/reviews/gate-7/reviewers/two#exec"
NOW = "2026-07-24T12:00:00+00:00"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    prior = ledger.RUNTIME_ROOT
    sentinel = tmp_path / "unrelated-global-root"
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", sentinel)
    monkeypatch.setattr(clock, "now_utc", lambda: NOW)
    try:
        yield tmp_path, sentinel
    finally:
        ledger.RUNTIME_ROOT = prior


def _binding(address, *, parent=None, level="L3", state="running", **extra):
    value = {
        "node_address": address,
        "parent_address": parent,
        "level": level,
        "role_variant": level,
        "state": state,
        "liveness_state": "working",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": fencing.mint_owner_token(address, f"sub-{address}", "session", 1),
        "last_applied_seq": 0,
        "last_inbox_acked_offset": 0,
    }
    value.update(extra)
    return value


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _seed_join(root: Path) -> dict[str, dict]:
    contract = root / "nodes" / "L1" / "contracts" / "intent.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text("intent v2\n", encoding="utf-8")
    contract_key = str(contract.resolve())

    question = {
        "schema_version": 1,
        "message_id": "q-design",
        "source": ALPHA,
        "target": ROOT,
        "direction": "up",
        "artifact": "messages/q-design.md",
        "summary": "which compatibility behavior is intended?",
        "tags": ["arbitration"],
        "needs_answer": True,
        "question_state": "open",
        "submitted_at": "2026-07-24T10:30:00+00:00",
    }
    bindings = {
        ROOT: _binding(
            ROOT,
            parent=None,
            level="L1",
            turn_hook_profile="claude_full_edges",
            turn_hook_health="healthy",
            permission_posture="jailed-skip-permissions",
            containment_posture={
                "version": "blinders-v1",
                "mode": "enforce",
                "degraded": False,
                "l1_god_view": True,
            },
            contract_versions={
                contract_key: {
                    "schema_version": 1,
                    "owner_address": ROOT,
                    "artifact": contract_key,
                    "fingerprint": "new-fingerprint",
                    "revision_record_ref": "contract-revisions/intent-r2.json",
                    "lineage": [{"new_fingerprint": "new-fingerprint"}],
                }
            },
        ),
        ALPHA: _binding(
            ALPHA,
            parent=ROOT,
            state="done",
            messages={"q-design": question},
            contract_receipts={
                contract_key: {
                    "holder": ALPHA,
                    "owner_address": ROOT,
                    "artifact": contract_key,
                    "fingerprint": "old-fingerprint",
                    "stamp": {"sha256": "old-fingerprint"},
                }
            },
        ),
        BETA: _binding(
            BETA,
            parent=ROOT,
            state="running",
            liveness_state="waiting",
            turn_hook_profile="claude_full_edges",
            turn_hook_health="degraded",
            turn_hook_error="malformed callback",
            waiting_on_sibling=ROOT,
            admission_state="blocked_on_sibling",
            admission_blocked_by=ALPHA,
            admission_block_reason="predecessor_terminal_not_passed",
            gate_required=True,
            gate_state="gate_bounced",
            gate_id="gate-7",
            gate_review_address=BETA_REVIEW,
            gate_bounce_count=2,
            gate_failure_count=1,
            permission_posture="degraded-unjailed-prompting",
            containment_posture={
                "version": "blinders-v1",
                "mode": "observe",
                "degraded": True,
                "degraded_reason": "profile probe failed",
                "l1_god_view": False,
            },
        ),
        BETA_REVIEW: _binding(
            BETA_REVIEW,
            parent=ROOT,
            level="L3+",
            gate_for=BETA,
            gate_id="gate-7",
        ),
        CHECK_ONE: _binding(
            CHECK_ONE,
            parent=BETA_REVIEW,
            level="L3+",
            state="done",
            review_check_for=BETA_REVIEW,
            review_check_candidate=BETA,
            gate_id="gate-7",
            role_variant="L3+#review-check",
        ),
        CHECK_TWO: _binding(
            CHECK_TWO,
            parent=BETA_REVIEW,
            level="L3+",
            state="running",
            review_check_for=BETA_REVIEW,
            review_check_candidate=BETA,
            gate_id="gate-7",
            role_variant="L3+#review-check",
        ),
    }
    ledger.write_binding(
        copy.deepcopy(bindings),
        _lock_held=True,
        binding_path=root / ledger.BINDING_FILENAME,
    )
    _write_json(
        root / "runtime.json",
        {"build_id": "observability-run", "runtime_root": str(root), "pid": 999},
    )

    for address, binding in bindings.items():
        node = addressing.node_dir(address, root)
        node.mkdir(parents=True, exist_ok=True)
        (node / "report.md").write_text("# Report\n\nObserved product.\n", encoding="utf-8")
        _write_json(
            addressing.signal_path(address, root),
            {
                "signal": "DONE" if state_is_terminal(binding["state"]) else "ESCALATED",
                "ts": "2026-07-24T11:45:00+00:00",
                "owner_token": binding["owner_token"],
                "evidence": {},
            },
        )

    _write_json(
        addressing.turn_state_path(ROOT, root),
        {
            "schema_version": 1,
            "node_address": ROOT,
            "owner_token": bindings[ROOT]["owner_token"],
            "hook_profile": "claude_full_edges",
            "state": "tool_in_flight",
            "in_flight_tools": [{"tool_use_id": "tool-1", "tool_name": "Bash"}],
            "updated_at": "2026-07-24T11:59:00+00:00",
            "last_hook_event": "PreToolUse",
        },
    )
    _write_json(
        addressing.turn_state_path(BETA, root),
        {
            "schema_version": 1,
            "node_address": BETA,
            "owner_token": bindings[BETA]["owner_token"],
            "hook_profile": "claude_full_edges",
            "state": "turn_ended",
            "in_flight_tools": [],
            "updated_at": "2026-07-24T11:58:00+00:00",
            "last_hook_event": "Stop",
            "hook_fault": "malformed callback",
        },
    )
    inbox = addressing.inbox_path(BETA_REVIEW, root)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        json.dumps(
            {
                "type": "barrier_complete",
                "cohort": "review_check",
                "barrier_id": "old-complete-barrier",
                "members": [CHECK_ONE],
                "ts": "2026-07-24T11:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return bindings


def state_is_terminal(state: str) -> bool:
    return state in {"done", "failed", "dead"}


def _rows(snapshot):
    return {row["node_address"]: row for row in snapshot["nodes"]}


def test_snapshot_joins_every_required_observability_fact_without_writes(runtime):
    root, sentinel = runtime
    _seed_join(root)
    before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))

    snapshot = observability.snapshot(root)

    after = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    assert after == before, "snapshot reads must create or refresh nothing"
    assert ledger.RUNTIME_ROOT == sentinel, "offline reads must not bind process-global ledger state"
    assert snapshot["schema_version"] == 1
    assert snapshot["runtime"]["build_id"] == "observability-run"
    assert set(snapshot["current_positions"]) == {ROOT, BETA, BETA_REVIEW, CHECK_TWO}

    rows = _rows(snapshot)
    root_row = rows[ROOT]
    assert root_row["dag"]["depth"] == 0
    assert root_row["turn"]["status"] == "valid"
    assert root_row["turn"]["state"] == "tool_in_flight"
    assert root_row["turn"]["tool_names"] == ["Bash"]
    assert root_row["turn"]["hook_profile"] == "claude_full_edges"
    assert root_row["turn"]["hook_health"] == "healthy"
    assert root_row["owed"]["total"] == len(root_row["owed"]["items"])
    assert root_row["owed"]["present"] + root_row["owed"]["open"] == root_row["owed"]["total"]
    assert any(item["item_id"] == f"question:{ALPHA}:q-design" for item in root_row["owed"]["items"])
    assert root_row["questions"]["incoming"][0]["edge"] == f"{ALPHA} -> {ROOT}"
    assert root_row["questions"]["incoming"][0]["age_seconds"] == 5400.0
    assert root_row["questions"]["incoming"][0]["age_bucket"] == "1h-24h"
    assert len(root_row["contracts"]["owned_versions"]) == 1
    assert root_row["barriers"]["product"]["terminal"] == 1
    assert root_row["barriers"]["product"]["total"] == 2
    assert root_row["barriers"]["product"]["status"] == "open"
    assert root_row["posture"]["jail_mode"] == "enforce"
    assert root_row["posture"]["l1_god_view"] is True

    alpha = rows[ALPHA]
    assert alpha["questions"]["outgoing"][0]["message_id"] == "q-design"
    assert alpha["contracts"]["stale_receipts"][0]["held_fingerprint"] == "old-fingerprint"
    assert alpha["contracts"]["stale_receipts"][0]["current_fingerprint"] == "new-fingerprint"

    beta = rows[BETA]
    assert beta["dag"]["dependencies"] == [ROOT, ALPHA]
    assert beta["binding"]["admission_state"] == "blocked_on_sibling"
    assert beta["turn"]["status"] == "malformed"
    assert beta["turn"]["hook_health"] == "degraded"
    assert beta["gate"]["state"] == "gate_bounced"
    assert beta["gate"]["bounce_count"] == 2
    assert beta["gate"]["needs_audit"] is True
    assert beta["gate"]["audit_signals"] == ["gate_bounce:count=2"]
    assert beta["gate"]["audit_label"].startswith("LOOK HERE")
    assert beta["gate"]["failure_count"] == 1
    assert beta["posture"]["jail_mode"] == "observe"
    assert beta["posture"]["degraded"] is True

    review = rows[BETA_REVIEW]
    assert review["barriers"]["review_check"]["terminal"] == 1
    assert review["barriers"]["review_check"]["total"] == 2
    assert review["barriers"]["review_check"]["status"] == "open"
    assert review["barriers"]["review_check"]["latest_event"]["barrier_id"] == "old-complete-barrier"

    edge_types = {(edge["type"], edge["source"], edge["target"]) for edge in snapshot["edges"]}
    assert ("supervision", ROOT, ALPHA) in edge_types
    assert ("dependency", ALPHA, BETA) in edge_types
    assert ("dependency", ROOT, BETA) in edge_types
    assert ("review", BETA, BETA_REVIEW) in edge_types
    assert snapshot["question_age_buckets"] == {"1h-24h": 1}


def test_waiting_on_human_is_a_first_class_view_label(runtime):
    root, _sentinel = runtime
    bindings = _seed_join(root)
    _write_json(
        addressing.turn_state_path(ROOT, root),
        {
            "schema_version": 1,
            "node_address": ROOT,
            "owner_token": bindings[ROOT]["owner_token"],
            "hook_profile": "claude_full_edges",
            "state": "waiting_on_human",
            "in_flight_tools": ["question-1"],
            "waiting_on_human_tool_id": "question-1",
            "waiting_on_human_tool_name": "AskUserQuestion",
            "updated_at": "2026-07-24T11:59:00+00:00",
            "last_hook_event": "PreToolUse",
        },
    )

    snapshot = observability.snapshot(root)
    row = _rows(snapshot)[ROOT]
    assert row["turn"]["state"] == "waiting_on_human"
    assert row["turn"]["waiting_on_human_tool_id"] == "question-1"
    assert row["turn"]["waiting_on_human_tool_name"] == "AskUserQuestion"
    assert row["turn"]["tool_names"] == ["AskUserQuestion"]
    assert "turn=waiting_on_human" in observability.render_terminal(snapshot)


def test_blocked_on_input_is_a_loud_per_seat_view_flag(runtime):
    root, _sentinel = runtime
    bindings = _seed_join(root)
    bindings[ROOT].update(
        {
            "seat_stall_active": True,
            "seat_stall_incident_id": "stall-root-1",
            "seat_stall_since": "2026-07-24T11:50:00+00:00",
            "seat_stall_classification": "blocked_on_input",
            "seat_stall_positive_incident_count": 2,
            "seat_stall_pane_excerpt": "Do you want to proceed? | Esc to cancel",
            "seat_stall_prompt_signature": "claude-choice-cancel",
            "seat_stall_cancel_status": "pending",
            "seat_stall_retriggered": True,
            "seat_stall_escalated": False,
        }
    )
    ledger.write_binding(
        bindings,
        _lock_held=True,
        binding_path=root / ledger.BINDING_FILENAME,
    )

    snapshot = observability.snapshot(root)
    row = _rows(snapshot)[ROOT]
    stall = row["blocked_on_input"]
    assert stall == {
        "active": True,
        "incident_id": "stall-root-1",
        "since": "2026-07-24T11:50:00+00:00",
        "duration_seconds": 600.0,
        "duration": "10m",
        "classification": "blocked_on_input",
        "incident_count": 2,
        "pane_excerpt": "Do you want to proceed? | Esc to cancel",
        "prompt_signature": "claude-choice-cancel",
        "cancel_status": "pending",
        "retriggered": True,
        "escalated": False,
        "root_limit": False,
    }
    terminal = observability.render_terminal(snapshot)
    assert "⛔ BLOCKED ON INPUT 10m" in terminal
    assert "cancel=pending" in terminal


def test_tagged_owner_facing_message_is_counted_in_ownerq_union(runtime):
    """T-14 gate-convergence questions are visible in the operator's ownerq column."""
    root, _sentinel = runtime
    bindings = _seed_join(root)
    question = {
        "schema_version": 1,
        "message_id": "q-gate-convergence",
        "source": BETA,
        "target": ROOT,
        "direction": "up",
        "artifact": "messages/q-gate-convergence.md",
        "summary": "parent judgment is not converging",
        "tags": ["gate-convergence", "owner-facing"],
        "needs_answer": True,
        "question_state": "open",
        "submitted_at": "2026-07-24T11:50:00+00:00",
    }
    bindings[BETA]["messages"] = {"q-gate-convergence": question}
    bindings[ROOT]["plan_alignment_owner_questions"] = {
        "q-plan": {
            "question_id": "q-plan",
            "status": "open",
            "question": "Does the plan preserve intent?",
        }
    }
    bindings[ROOT]["fidelity_playback_owner_questions"] = {
        "q-fidelity": {
            "question_id": "q-fidelity",
            "status": "open",
            "question": "Does playback match the request?",
        }
    }
    ledger.write_binding(
        copy.deepcopy(bindings),
        _lock_held=True,
        binding_path=root / ledger.BINDING_FILENAME,
    )

    snapshot = observability.snapshot(root)

    owner_questions = snapshot["owner_questions"]
    projected = next(
        row for row in owner_questions if row.get("message_id") == "q-gate-convergence"
    )
    assert projected["question_kind"] == "owner_facing_message"
    terminal = observability.render_terminal(snapshot)
    root_line = next(line for line in terminal.splitlines() if f" {ROOT} " in line)
    assert "ownerq=3/3" in root_line
    assert "fidelityq=1/1" in root_line


def test_optional_artifact_faults_warn_but_malformed_binding_fails_loudly(runtime):
    root, _sentinel = runtime
    bindings = _seed_join(root)
    _write_json(
        addressing.turn_state_path(ROOT, root),
        {"schema_version": 1, "node_address": "wrong", "owner_token": "wrong"},
    )
    addressing.inbox_path(BETA_REVIEW, root).write_text("{not-json}\n", encoding="utf-8")

    snapshot = observability.snapshot(root)

    rows = _rows(snapshot)
    assert rows[ROOT]["turn"]["status"] in {"malformed", "stale"}
    assert snapshot["warnings"], "optional malformed files must be visible, not fatal"

    (root / ledger.BINDING_FILENAME).write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        observability.snapshot(root)

    ledger.write_binding(
        bindings,
        _lock_held=True,
        binding_path=root / ledger.BINDING_FILENAME,
    )


def test_terminal_json_and_self_contained_html_render_the_same_snapshot(runtime):
    root, _sentinel = runtime
    _seed_join(root)
    snapshot = observability.snapshot(root)

    terminal = observability.render_terminal(snapshot)
    for address in _rows(snapshot):
        assert address in terminal
    assert "turn=tool_in_flight" in terminal
    assert "owed=" in terminal
    assert "gate_bounced" in terminal
    assert "LOOK HERE" in terminal
    assert "gate_bounce:count=2" in terminal
    assert "dependency" in terminal
    assert "stale=1" in terminal

    encoded = json.dumps(snapshot, sort_keys=True)
    assert json.loads(encoded) == snapshot

    html = observability.render_html(snapshot)
    assert html.startswith("<!doctype html>")
    assert "<svg" in html
    assert "embedded-snapshot" in html
    assert "edge supervision" in html
    assert "edge dependency" in html
    assert "edge review" in html
    assert "data-current=\"true\"" in html
    for address in _rows(snapshot):
        assert address in html
    lowered = html.lower()
    assert "https://" not in lowered
    assert "http://" not in lowered
    assert "<script src=" not in lowered
    assert "<link " not in lowered


def test_html_output_is_control_plane_only_and_node_outputs_are_refused(runtime):
    root, _sentinel = runtime
    _seed_join(root)
    snapshot = observability.snapshot(root)
    node_files_before = sorted(
        str(path.relative_to(root / "nodes"))
        for path in (root / "nodes").rglob("*")
    )

    default = observability.default_output_path(root)
    assert default == root / ".harnessd" / "views" / "journey.html"
    written = observability.write_html(snapshot, default, runtime_root=root)

    assert written == default
    assert default.is_file()
    node_files_after = sorted(
        str(path.relative_to(root / "nodes"))
        for path in (root / "nodes").rglob("*")
    )
    assert node_files_after == node_files_before
    with pytest.raises(ValueError, match="node tree"):
        observability.write_html(
            snapshot,
            addressing.node_dir(BETA, root) / "journey.html",
            runtime_root=root,
        )


def test_dead_run_view_never_requires_daemon_or_socket(runtime, monkeypatch):
    root, _sentinel = runtime
    bindings = _seed_join(root)
    for binding in bindings.values():
        binding["state"] = "done"
    ledger.write_binding(
        bindings,
        _lock_held=True,
        binding_path=root / ledger.BINDING_FILENAME,
    )
    monkeypatch.setattr(
        "harnessd.harnessctl._round_trip",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("offline observability must not call daemon IPC")
        ),
    )

    snapshot = observability.snapshot(root)

    assert snapshot["current_positions"] == []
    assert len(snapshot["nodes"]) == len(bindings)


class _Detector:
    def liveness(self, _node_address):
        return SimpleNamespace(state="working", last_progress_at=None)


class _Tmux:
    def list_targets(self):
        return {}


def test_real_daemon_ticks_never_create_aggregate_or_view_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    address = "site#exec"
    binding = _binding(address, parent=None, level="L2", state="done")
    ledger.write_binding({address: binding}, _lock_held=True)
    node = addressing.node_dir(address, tmp_path)
    node.mkdir(parents=True)
    (node / "log.md").write_text("one\n", encoding="utf-8")
    (node / "status.md").write_text("ready\n", encoding="utf-8")

    daemon.poll_once(None, _Tmux(), _Detector())
    (node / "log.md").write_text("two\n", encoding="utf-8")
    daemon.poll_once(None, _Tmux(), _Detector())

    assert not (tmp_path / "observability").exists()
    assert not (tmp_path / ".harnessd" / "views").exists()
    assert not list(tmp_path.rglob("log.aggregate.md"))
    assert not list(tmp_path.rglob("status.aggregate.md"))
