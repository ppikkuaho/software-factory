from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.ledger as ledger
from harnessd.spawn import launch_surface
from tools import audit_launch_surfaces


def _bind_runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    return previous


def _restore_runtime(previous):
    ledger.RUNTIME_ROOT = previous


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _write_signoff(address: str, runtime: Path, token: str = "owner-token-live") -> None:
    path = addressing.signoff_path(address, runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "owner_token": token,
                "signal_path": str(addressing.signal_path(address, runtime)),
            }
        ),
        encoding="utf-8",
    )


def _corpus_copy(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    shutil.copytree(launch_surface.harness_root() / "operational", root / "operational")
    return root


def _registered_block_source(block_id: str, *, variant: str | None = None) -> str:
    registry_path = (
        launch_surface.harness_root()
        / "operational"
        / "shared"
        / "blocks"
        / "registry.json"
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    spec = registry["blocks"][block_id]
    source = variant or spec["source"]
    return (
        launch_surface.harness_root()
        / registry["blocks_root"]
        / source
    ).read_text(encoding="utf-8").strip()


def test_every_generated_launch_packet_carries_one_canonical_naming_convention(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        for role in sorted(launch_surface.PILOT_ROLES):
            level = role if role != "REVIEW-CHECK" else "L4+"
            level_config = config.LevelConfig.for_level(level)
            if role == "REVIEW-CHECK":
                level_config = replace(level_config, role_variant="L4+#review-check")
            address = f"proj/{role.lower()}#exec"
            node_dir = tmp_path / "workspaces" / role
            _write_signoff(address, tmp_path)

            artifacts = launch_surface.materialize(
                address,
                level_config,
                {"workspace": str(node_dir)},
                runtime_root=tmp_path,
            )

            assert artifacts is not None
            launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")
            assert launch.count(launch_surface.NAMING_CONVENTIONS) == 1
    finally:
        _restore_runtime(previous)


def test_behavioral_laws_reach_each_relevant_generated_role_surface(tmp_path):
    signoff_law = _registered_block_source("decision-delivery-signoff")
    gate_lead_law = _registered_block_source("review-accountability")
    reviewer_law = _registered_block_source(
        "review-accountability",
        variant="review-accountability.reviewer.md",
    )
    previous = _bind_runtime(tmp_path)
    try:
        launches: dict[str, str] = {}
        for role in sorted(launch_surface.PILOT_ROLES):
            level = role if role != "REVIEW-CHECK" else "L4+"
            level_config = config.LevelConfig.for_level(level)
            if role == "REVIEW-CHECK":
                level_config = replace(level_config, role_variant="L4+#review-check")
            address = f"proj/{role.lower()}#exec"
            node_dir = tmp_path / "workspaces" / role
            _write_signoff(address, tmp_path)

            artifacts = launch_surface.materialize(
                address,
                level_config,
                {"workspace": str(node_dir)},
                runtime_root=tmp_path,
            )

            assert artifacts is not None
            launches[role] = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")

        for role, launch in launches.items():
            assert launch.count(signoff_law) == 1, role
            assert "read new inbox rows when the harness notification names their sender and unread count" in _one_line(launch), role
            assert "second-to-last act" in _one_line(launch), role
            assert (
                "Once you submit a candidate, its artifacts are frozen until the gate verdict "
                "returns."
            ) in _one_line(launch), role
            assert (
                "Do not edit reviewed bytes for any reason, including to fix a defect you just "
                "found."
            ) in _one_line(launch), role
            assert (
                "Report the defect and let the verdict or a fresh submission carry the repair."
            ) in _one_line(launch), role
            assert (
                "Put disposable scratch only in the harness-provisioned `.tmp` tree."
            ) in _one_line(launch), role
            assert (
                "the harness never requires a seat to delete anything"
            ) in _one_line(launch), role
            assert "never issue destructive filesystem commands" in _one_line(launch), role

        l1_launch = _one_line(launches["L1"])
        l2_launch = _one_line(launches["L2"])
        assert "asking the owner the full reflect-back is the completion of intake" in l1_launch
        assert "must not write the L2 spawn request" in l1_launch
        assert "author of a superseding ruling owns its ripple" in l2_launch
        assert "Delivery status is derived from the recipients' inbox rows" in l2_launch
        assert "Never edit a child's frozen inputs in place." in l2_launch
        assert "Rulings travel by canonical message only." in l2_launch
        assert (
            "give the child a fresh incarnation or use the explicit correction path"
        ) in l2_launch
        assert (
            "Deliver recurring records, including registers and decision sets, as deltas "
            "against the last delivered version, never as full regenerations."
        ) in l2_launch
        for role, launch in launches.items():
            if role != "L2":
                assert "recurring records, including registers and decision sets" not in launch
                assert "Never edit a child's frozen inputs in place." not in launch

        for role in ("L2+", "L3+", "L4+"):
            assert launches[role].count(gate_lead_law) == 1, role
            assert reviewer_law not in launches[role]
        for role in ("L5+", "REVIEW-CHECK"):
            assert launches[role].count(reviewer_law) == 1, role
            assert "Double-assign every decisive question" not in launches[role]
    finally:
        _restore_runtime(previous)


def test_l5_launch_surface_is_minimal_startup_packet_with_reference_map(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        address = "proj/widget#exec"
        node_dir = addressing.node_dir(address, tmp_path)
        node_dir.mkdir(parents=True)
        (node_dir / "brief.md").write_text(
            "# Task\n\nImplement CI-3 exactly; coordinate with L4 if the acceptance is ambiguous.\n",
            encoding="utf-8",
        )
        (node_dir / "acceptance.md").write_text(
            "# Acceptance\n\n- CI-3 parses both rendered outputs.\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path)

        artifacts = launch_surface.materialize(
            address,
            config.LevelConfig.for_level("L5"),
            {"workspace": str(node_dir)},
            runtime_root=tmp_path,
        )

        assert artifacts is not None
        launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")
        reference = Path(artifacts.reference_map_file).read_text(encoding="utf-8")
        one_line_launch = _one_line(launch)
        one_line_launch = _one_line(launch)
        one_line_launch = _one_line(launch)
        reference_json = json.loads(Path(artifacts.reference_map_json_file).read_text(encoding="utf-8"))

        assert "Task Executor" in launch
        assert "Do not append ancestor or project-root logs" in launch
        assert "## Startup Task List Seed" in launch
        assert "## Startup Sequence" in launch
        assert "first operational act is to create or refresh the native task list" in launch
        assert "discover only the task-list tool family" in launch
        assert "before any file reads or workspace inspection" in launch
        assert "bounded orientation" in launch
        assert "plan.md` as the durable copy" in launch
        assert "file-read tool" in launch
        assert "cannot spawn child agents" in launch
        assert "write the package under a top-level `tests/` directory" in launch
        assert "tests/test_*.py" in launch
        assert "expected collection/result shape" in launch
        assert "RED now, GREEN after implementation" in launch
        assert "all-pass before implementation" in launch
        assert "literal `## Verification Commands`" in launch
        assert "Implement CI-3 exactly" in launch
        assert "CI-3 parses both rendered outputs" in launch
        assert "owner-token-live" in launch
        assert "Read-only sign-off handshake file" in launch
        assert "Terminal signal file you write" in launch
        assert "Do not create, edit, or overwrite the sign-off handshake file" in launch
        assert "Terminal signal JSON uses `signal`, not `status`" in launch
        assert "`DONE` or `FAILED`" in launch
        assert "ESCALATED" not in launch
        assert "`needs_answer: true`" in launch
        assert "park without a terminal signal" in launch
        assert "exact current UTC instant immediately before writing the signal" in launch
        assert "date -u +%Y-%m-%dT%H:%M:%SZ" in launch
        assert "do not invent, round, or copy an example timestamp" in launch
        assert "operational/shared/agent-lifecycle.md" not in launch
        assert "The Model: Bus + Docs" not in launch

        assert "operational/L5/swe-handbook.md" in reference
        assert "design/PLAN-ALIGNMENT-GATE.md" in reference
        assert "operational/shared/agent-lifecycle.md" in reference
        assert str(launch_surface.harness_root() / "operational/shared/agent-lifecycle.md") in reference
        assert str(launch_surface.harness_root() / "design/PLAN-ALIGNMENT-GATE.md") in reference
        assert "sibling" not in reference.lower()
        assert reference_json["role"] == "L5"
        assert reference_json["harness_root"] == str(launch_surface.harness_root())
        assert any(item["id"] == "reference-map-v1" for item in reference_json["references"])
        assert any(
            path["path"] == "operational/shared/agent-lifecycle.md"
            and path["absolute_path"] == str(launch_surface.harness_root() / "operational/shared/agent-lifecycle.md")
            for item in reference_json["references"]
            for path in item["resolved_paths"]
        )
        assert any(item["id"] == "hidden-surface-v1" for item in reference_json["hidden"])

        first_source_hash = artifacts.launch_surface_source_hash
        first_packet_hash = artifacts.launch_packet_hash
        (node_dir / "brief.md").write_text(
            "# Task\n\nImplement a different bounded slice without changing canonical surfaces.\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path, token="owner-token-next")
        second = launch_surface.materialize(
            address,
            config.LevelConfig.for_level("L5"),
            {"workspace": str(node_dir)},
            runtime_root=tmp_path,
        )
        assert second is not None
        assert second.launch_surface_source_hash == first_source_hash
        assert second.launch_packet_hash != first_packet_hash
    finally:
        _restore_runtime(previous)


def test_l5plus_launch_surface_includes_review_packet(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        address = "proj/widget#review"
        node_dir = addressing.node_dir(address, tmp_path)
        packet = node_dir / "reviews" / "gate-smoke" / "review-packet.md"
        packet.parent.mkdir(parents=True)
        packet.write_text(
            "# Review Packet\n\n- gate_id: gate-smoke\n- producer report: app/report.md\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path, token="review-token-live")

        artifacts = launch_surface.materialize(
            address,
            config.LevelConfig.for_level("L5+"),
            {"workspace": str(node_dir)},
            runtime_root=tmp_path,
        )

        assert artifacts is not None
        launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")

        assert "independent reviewer" in launch
        assert "gate-smoke" in launch
        assert "producer report: app/report.md" in launch
        assert "review-token-live" in launch
        assert "collecting or exercising zero intended checks" in launch
        assert "plain literal verdict line" in launch
        assert "VERDICT: ACCEPT" in launch
        assert "terminal-signal\nevidence alone is not enough" in launch
        assert "Terminal signal JSON uses `signal`, not `status`" in launch
        assert "exact current UTC instant immediately before writing the signal" in launch
        assert "date -u +%Y-%m-%dT%H:%M:%SZ" in launch
        assert "do not invent, round, or copy an example timestamp" in launch
        assert "When you cite an exit code as evidence" in launch
        assert "Do not pipe the command through `tail`, `head`, `grep`, or similar" in launch
        assert "preserved that command's status directly" in launch
    finally:
        _restore_runtime(previous)


def test_l4_launch_surface_carries_workstream_rules_without_manuals(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        address = "proj/area/workstream#exec"
        node_dir = addressing.node_dir(address, tmp_path)
        node_dir.mkdir(parents=True)
        (node_dir / "brief.md").write_text(
            "# Workstream Brief\n\nBuild the log viewer workstream from R-042.\n",
            encoding="utf-8",
        )
        (node_dir / "acceptance.md").write_text(
            "# Workstream Acceptance\n\n- R-042 task outputs compose cleanly.\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path, token="l4-token-live")

        artifacts = launch_surface.materialize(
            address,
            config.LevelConfig.for_level("L4"),
            {"workspace": str(node_dir)},
            runtime_root=tmp_path,
        )

        assert artifacts is not None
        launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")
        reference = Path(artifacts.reference_map_file).read_text(encoding="utf-8")
        reference_json = json.loads(Path(artifacts.reference_map_json_file).read_text(encoding="utf-8"))

        assert "Workstream Coordinator" in launch
        assert "Build the log viewer workstream from R-042" in launch
        assert "normal M51 L5 `test_author` path" in launch
        assert "The normal `test_author` and implementation spawn JSON shapes are in this launch packet" in launch
        assert "treat those shapes as sufficient for ordinary L5 child spawning" in launch
        assert "accepted_test_package" in launch
        assert "bindable acceptance package lives under" in launch
        assert "top-level `tests/` directory" in launch
        assert "expected collection/result shape" in launch
        assert "intentionally RED now" in launch
        assert "becomes GREEN after the implementation satisfies it" in launch
        assert "all-pass before implementation" in launch
        assert "## Verification Commands" in launch
        assert "the harness\nbinds the accepted package" in launch
        assert "so the harness binds the package" in launch
        assert "Do not manually copy the accepted `tests/` package" in launch
        assert "not the review gate id" in launch
        assert "not a file path" in launch
        assert "not an instruction to inspect harness code" in launch
        assert "Do not inspect harness implementation internals to preflight this contract" in launch
        assert "If a DONE rejection lands, repair from the typed inbox defect" in launch
        assert "Open `design/PLAN-ALIGNMENT-GATE.md` when the task package lacks the needed trace shape" in launch
        assert "design/PLAN-ALIGNMENT-GATE.md" in launch
        assert "harnessd/return_contract.py" not in launch
        assert "copy or bind the accepted" not in launch
        assert "unresolved decomposition decision" in launch
        assert "wait for the decision before spawning any L5" in launch
        assert "The default authorization is the L3 workstream brief" in launch
        assert "wait before spawning that test-author child" in launch
        assert "A runtime-native `Agent` is not a harness child" in launch
        assert "Every pointer you write into an L5 child brief must resolve from that child's workspace" in launch
        assert "paths from the child node's perspective" in launch
        assert "let the harness wake you on the next inbox route" in launch
        assert "Do not hold the pane in long foreground `sleep`/polling loops" in launch
        assert "keep it to a quick sweep, not a minute-scale wait" in launch
        assert "L4#review" in launch
        assert "l4-token-live" in launch
        assert "The Model: Bus + Docs" not in launch
        assert "except appending to the project log" not in launch
        assert "operational/shared/agent-lifecycle.md" not in launch

        assert "operational/shared/agent-lifecycle.md" in reference
        assert "operational/L5/role.md" in reference
        assert reference_json["role"] == "L4"
        assert any(item["id"] == "hidden-surface-v1" for item in reference_json["hidden"])
    finally:
        _restore_runtime(previous)


def test_l4plus_launch_surface_includes_review_packet_and_composition_checks(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        address = "proj/area/workstream#review"
        node_dir = addressing.node_dir(address, tmp_path)
        packet = node_dir / "reviews" / "gate-workstream" / "review-packet.md"
        packet.parent.mkdir(parents=True)
        packet.write_text(
            "# Review Packet\n\n- gate_id: gate-workstream\n- producer report: report.md\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path, token="l4-review-token")

        artifacts = launch_surface.materialize(
            address,
            config.LevelConfig.for_level("L4+"),
            {"workspace": str(node_dir)},
            runtime_root=tmp_path,
        )

        assert artifacts is not None
        launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")
        reference = Path(artifacts.reference_map_file).read_text(encoding="utf-8")
        one_line_launch = _one_line(launch)

        assert "Workstream Composition Review" in launch
        assert "## Startup Task List Seed" in launch
        assert "first operational act is to create or refresh the native task list" in launch
        assert "high-level review-management items" in launch
        assert "Write the workstream-composition review plan" in launch
        assert "reviews/<gate-id>/review-plan.md" in launch
        assert "Record FULL mode so the harness" in launch
        assert "daemon opens four first-class" in launch
        assert "review-check seats" in launch
        assert "reviewers/fidelity-coverage/report.md" in launch
        assert "reviewers/composition-interface/report.md" in launch
        assert "reviewers/evidence-credibility/report.md" in launch
        assert "reviewers/risk-readiness/report.md" in launch
        assert "brief-coverage.md" not in launch
        assert "task-interface-fit.md" not in launch
        assert "Treat lower gates as competent by default" in launch
        assert "Their verdicts establish unit facts" in one_line_launch
        assert "they do not establish the workstream-composition verdict" in one_line_launch
        assert "Drive the assembled workstream directly at this altitude" in one_line_launch
        assert "required oracle, not an optional sanity probe" in one_line_launch
        assert "named evidence uncertainty remains" in one_line_launch
        assert "state that unresolved question in the check report" in one_line_launch
        assert "producer's candidate tree as\nevidence, not as your probe workspace" in launch
        assert "submitted candidate tree is evidence, not the review's writable workspace" in launch
        assert "orchestrate" in launch
        assert "Do not author their reports" in launch
        assert "yourself in FULL mode" in launch
        assert "`review_check` cohort barrier" in launch
        assert "Individual reviewer terminal rows append silently" in one_line_launch
        assert "report files alone are not terminal evidence" in launch
        assert "check report" in launch
        assert "plain literal verdict\nline" in launch
        assert "VERDICT: ACCEPT" in launch
        assert "terminal\nsignal evidence field does not replace" in launch
        assert "gate-workstream" in launch
        assert "producer report: report.md" in launch
        assert "l4-review-token" in launch
        assert "operational/shared/review-handbook.md" not in launch
        assert "operational/shared/review-handbook.md" in reference
    finally:
        _restore_runtime(previous)


def test_review_check_launch_surface_uses_assigned_report_not_gate_artifact(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        address = "proj/area/workstream/reviews/gate-workstream/reviewers/fidelity-coverage#exec"
        node_dir = addressing.node_dir(address, tmp_path)
        producer_address = "proj/area/workstream#exec"
        gate_dir = addressing.node_dir(producer_address, tmp_path) / "reviews" / "gate-workstream"
        report_path = gate_dir / "reviewers" / "fidelity-coverage" / "report.md"
        brief_path = report_path.parent / "brief.md"
        packet = gate_dir / "review-packet.md"
        brief_path.parent.mkdir(parents=True)
        packet.write_text(
            "# Review Packet\n\n- gate_id: gate-workstream\n- producer report: report.md\n",
            encoding="utf-8",
        )
        brief_path.write_text(
            "# Review Check Brief - Fidelity and Coverage\n\n"
            f"Assigned report path: `{report_path}`\n\n"
            "Write exactly one check report.\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path, token="check-token")
        ledger.write_binding(
            {
                address: {
                    "node_address": address,
                    "level": "L4+",
                    "role_variant": "L4+#review-check",
                    "gate_review_packet": str(packet),
                    "review_check_report": str(report_path),
                }
            },
            _lock_held=True,
        )

        artifacts = launch_surface.materialize(
            address,
            replace(config.LevelConfig.for_level("L4+"), role_variant="L4+#review-check"),
            {
                "workspace": str(node_dir),
                "spec_pointer": str(brief_path),
                "gate_review_packet": str(packet),
                "review_check_report": str(report_path),
            },
            runtime_root=tmp_path,
        )

        assert artifacts is not None
        assert artifacts.role == "REVIEW-CHECK"
        launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")

        assert "role: `REVIEW-CHECK`" in launch
        assert "## Startup Task List Seed" in launch
        assert "first operational act is to create or refresh the native task list" in launch
        assert "Review-check seats do not fill the final gate artifact" in launch
        assert "their durable output is the assigned check report" in launch
        assert "Write only the assigned check report" in launch
        assert "Do not write the final gate artifact" in launch
        assert "do not render ACCEPT/BOUNCE/ESCALATE for the candidate" in launch
        assert "## review-check-brief.md" in launch
        assert f"Assigned report path: `{report_path}`" in launch
        assert "## review-packet.md" in launch
        assert "gate-workstream" in launch
        assert "check-token" in launch
    finally:
        _restore_runtime(previous)


def test_l3plus_launch_surface_calibrates_lower_gate_evidence(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        address = "proj/area#review"
        node_dir = addressing.node_dir(address, tmp_path)
        packet = node_dir / "reviews" / "gate-area" / "review-packet.md"
        packet.parent.mkdir(parents=True)
        packet.write_text(
            "# Review Packet\n\n- gate_id: gate-area\n- producer report: report.md\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path, token="l3-review-token")

        artifacts = launch_surface.materialize(
            address,
            config.LevelConfig.for_level("L3+"),
            {"workspace": str(node_dir)},
            runtime_root=tmp_path,
        )

        assert artifacts is not None
        launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")
        one_line_launch = _one_line(launch)

        assert "gate lead for area-composition review" in launch
        assert "## Startup Task List Seed" in launch
        assert "Write the area-composition review plan" in launch
        assert "reviews/<gate-id>/review-plan.md" in launch
        assert "orchestrate area-level review checks" in launch
        assert "Do not author check reports yourself in FULL mode" in one_line_launch
        assert "`review_check` cohort barrier in FULL mode" in one_line_launch
        assert "Individual terminal rows append silently" in launch
        assert "report files alone are not terminal evidence" in launch
        assert "Treat lower gates" in launch
        assert "competent by default" in launch
        assert "establish lower-unit facts and locate the area-composition surfaces" in one_line_launch
        assert "Drive the assembled area directly at this altitude" in one_line_launch
        assert "required composition oracle, not an optional sanity probe" in one_line_launch
        assert "named area-level uncertainty" in one_line_launch
        assert "State that unresolved question in the check report" in one_line_launch
        assert "check report" in launch
        assert "plain literal verdict line" in one_line_launch
        assert "VERDICT: ACCEPT" in launch
        assert "terminal-signal evidence alone is not enough" in launch
        assert "gate-area" in launch
        assert "l3-review-token" in launch
    finally:
        _restore_runtime(previous)


def test_l2plus_launch_surface_calibrates_lower_gate_evidence(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        address = "proj#review"
        node_dir = addressing.node_dir(address, tmp_path)
        packet = node_dir / "reviews" / "gate-product" / "review-packet.md"
        packet.parent.mkdir(parents=True)
        packet.write_text(
            "# Review Packet\n\n- gate_id: gate-product\n- producer report: report.md\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path, token="l2-review-token")

        artifacts = launch_surface.materialize(
            address,
            config.LevelConfig.for_level("L2+"),
            {"workspace": str(node_dir)},
            runtime_root=tmp_path,
        )

        assert artifacts is not None
        launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")
        one_line_launch = _one_line(launch)

        assert "gate lead for product-composition review" in launch
        assert "## Startup Task List Seed" in launch
        assert "Write the product-composition review plan" in launch
        assert "reviews/<gate-id>/review-plan.md" in launch
        assert "orchestrate product-level review checks" in launch
        assert "Do not author check reports yourself in FULL mode" in one_line_launch
        assert "`review_check` cohort barrier in FULL mode" in one_line_launch
        assert "Individual terminal rows append silently" in launch
        assert "report files alone are not terminal evidence" in launch
        assert "Treat lower gates" in launch
        assert "competent by\ndefault" in launch
        assert (
            "establish lower-unit facts and locate the product-composition surfaces"
        ) in one_line_launch
        assert "Drive the assembled product directly at this altitude" in one_line_launch
        assert "required composition oracle, not an optional sanity probe" in one_line_launch
        assert "named product-level uncertainty" in one_line_launch
        assert "State that unresolved question in the check report" in one_line_launch
        assert "check report" in launch
        assert "plain literal verdict line" in one_line_launch
        assert "VERDICT: ACCEPT" in launch
        assert "terminal-signal evidence alone is not enough" in launch
        assert "gate-product" in launch
        assert "l2-review-token" in launch
    finally:
        _restore_runtime(previous)


def test_l3_launch_surface_names_planning_execution_modes_and_gate_route(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        address = "proj/area#exec"
        node_dir = addressing.node_dir(address, tmp_path)
        node_dir.mkdir(parents=True)
        (node_dir / "brief.md").write_text(
            "# Area Brief\n\nRealize the parser area from R-200.\n",
            encoding="utf-8",
        )
        (node_dir / "acceptance.md").write_text(
            "# Area Acceptance\n\n- R-200 area outputs compose.\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path, token="l3-token-live")

        artifacts = launch_surface.materialize(
            address,
            config.LevelConfig.for_level("L3"),
            {"workspace": str(node_dir)},
            runtime_root=tmp_path,
        )

        assert artifacts is not None
        launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")
        reference = Path(artifacts.reference_map_file).read_text(encoding="utf-8")
        one_line_launch = _one_line(launch)
        assert "planning-L3 produces area design and collapses" in launch
        assert "execution-L3 realizes a frozen design through L4" in launch
        assert "should say that the\ndesign is frozen for execution by the plan-alignment PASS" in launch
        assert "If `design.md` still opens as a planning\n`candidate`" in launch
        assert "not as permission to reopen the\ndesign" in launch
        assert "A runtime-native `Agent` is not a harness child" in launch
        assert '"child_name": "<workstream-slug>"' in launch
        assert '"child_level": "L4"' in launch
        assert "sufficient for ordinary execution workstream spawning" in launch
        assert "Do not spawn L5 children or `purpose: \"test_author\"` directly" in launch
        assert "translate that into an L4 workstream brief" in launch
        assert "submits a candidate\nto `L3#review`" in launch
        assert "## Runtime Paths For Control Verbs" in launch
        assert "python3 -m harnessd.harnessctl <verb> ..." in launch
        assert "HARNESSD_SOCKET=" in launch
        assert "Normal child spawning is not a `harnessctl` verb" in launch
        assert "l3-token-live" in launch
        assert "let the harness wake you on the next inbox route" in one_line_launch
        assert "Do not hold the pane in long foreground `sleep`/polling loops" in one_line_launch
        assert "operational/shared/agent-lifecycle.md" not in launch
        assert "operational/shared/agent-lifecycle.md" in reference
        assert "use when spawning L4s" not in reference
        assert "For normal L4 workstream spawning, use the concise outbox shape" in reference
    finally:
        _restore_runtime(previous)


def test_l2_launch_surface_names_plan_alignment_and_l3_spine(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        address = "proj#exec"
        node_dir = addressing.node_dir(address, tmp_path)
        node_dir.mkdir(parents=True)
        (node_dir / "client-brief").mkdir()
        (node_dir / "client-brief" / "raw-request.md").write_text(
            "Build a log viewer.\n",
            encoding="utf-8",
        )
        (node_dir / "client-brief" / "intent-spec.md").write_text(
            "# Intent Spec\n\nR-300: Build the log viewer.\n",
            encoding="utf-8",
        )
        (node_dir / "client-brief" / "vision.md").write_text(
            "# Vision\n\nMake logs inspectable.\n",
            encoding="utf-8",
        )
        (node_dir / "client-brief" / "priorities.md").write_text(
            "# Priorities\n\nCorrectness before speed.\n",
            encoding="utf-8",
        )
        (node_dir / "brief.md").write_text(
            "# Project Brief\n\nBuild the log viewer product from R-300.\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path, token="l2-token-live")

        artifacts = launch_surface.materialize(
            address,
            config.LevelConfig.for_level("L2"),
            {"workspace": str(node_dir)},
            runtime_root=tmp_path,
        )

        assert artifacts is not None
        launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")
        reference = Path(artifacts.reference_map_file).read_text(encoding="utf-8")
        one_line_launch = _one_line(launch)
        assert "## Startup Sequence" in launch
        assert "register planning-L3s" in launch
        assert "`purpose: \"planning\"`" in launch
        assert "harness admits L3 siblings serially" in launch
        assert "## Launch Surface — L3 Child Spawn" in launch
        assert '"child_level": "L3", "purpose": "planning"' in launch
        assert '"child_name": "<area-name>", "child_level": "L3"' in launch
        assert "replace that\nchild node's canonical `brief.md` and `acceptance.md`" in launch
        assert "Make the freeze visible in the child's ordinary files" in launch
        assert "Do not leave a planning-era `status: candidate` header" in launch
        assert "not a redesign" in launch
        assert "exec-brief.md" in launch
        assert "Do not instruct execution-L3 to spawn L5\nchildren or `purpose: \"test_author\"` directly" in launch
        assert "express that as an L4 workstream\nobligation" in launch
        assert "start by authoring the architecture package" in launch
        assert "Create `.harness-outbox/` when the first child request is ready" in launch
        assert "Do not preflight `harnessctl`, daemon status, runtime roots, or parent inboxes" in launch
        assert "normal authority for L3 spawning" in launch
        assert "runtime ledgers and harness implementation files are" in launch
        assert '"type": "plan_alignment_ready"' in launch
        assert "node root" in launch
        assert '"package": "plan/validated-plan-package.md"' in launch
        assert "not aliases such as `validated_plan_package`" in launch
        assert "`design-submission` is the parent-visible pointer type" in launch
        assert "Feedback" in launch and "before the wait begins" in launch
        assert "## client-brief/raw-request.md" in launch
        assert "Build a log viewer." in launch
        assert "## client-brief/intent-spec.md" in launch
        assert "R-300: Build the log viewer." in launch
        assert "## client-brief/vision.md" in launch
        assert "Make logs inspectable." in launch
        assert "## client-brief/priorities.md" in launch
        assert "Correctness before speed." in launch
        assert "Do not spawn execution-L3s (L3) until L1 returns PASS" in launch
        assert "Product execution always passes through L3" in launch
        assert "let the harness wake you on the next inbox route" in one_line_launch
        assert "Do not hold the pane in long foreground `sleep`/polling loops" in one_line_launch
        assert "That submits the candidate to `L2#review`" in launch
        assert "l2-token-live" in launch
        assert "design/PROJECT-PLANNING.md" not in launch
        assert "design/PROJECT-PLANNING.md" in reference
    finally:
        _restore_runtime(previous)


def test_l1_launch_surface_keeps_intake_exception_and_final_altitude_clear(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        address = "root#exec"
        node_dir = addressing.node_dir(address, tmp_path)
        node_dir.mkdir(parents=True)
        (node_dir / "brief.md").write_text(
            "# Root Brief\n\nWait for durable intake or gate events.\n",
            encoding="utf-8",
        )
        _write_signoff(address, tmp_path, token="l1-token-live")

        artifacts = launch_surface.materialize(
            address,
            config.LevelConfig.for_level("L1"),
            {"workspace": str(node_dir)},
            runtime_root=tmp_path,
        )

        assert artifacts is not None
        launch = Path(artifacts.launch_packet_file).read_text(encoding="utf-8")
        reference = Path(artifacts.reference_map_file).read_text(encoding="utf-8")
        assert "## Startup Sequence" in launch
        assert "Project execution normally starts" in launch
        assert "by preparing an L2 child" in launch
        assert "Mint root requirement IDs in the canonical dotted form" in launch
        assert "`R-001`, `R-002`, `R-002.1`" in launch
        assert "trace: { id: R-001, serves: [O-001], kind: requirement, level: L1" in launch
        assert "trace: { id: R-005.1, serves: [R-005], kind: requirement, level: L1" in launch
        assert "Acceptance criteria are bullets keyed to the requirement IDs" in launch
        assert "do not invent a second ID family for acceptance or process rows" in launch
        assert "Do not mint `A-`, `AC-`, or `P-`" in launch
        assert "## Launch Surface — Project Child Spawn" in launch
        assert ".harness-outbox/" in launch
        assert "Write the normal JSON request with only these fields" in launch
        assert '"child_level": "L2"' in launch
        assert "Treat this block as sufficient for the normal L2 project spawn" in launch
        assert "write the two-field request" in launch
        assert "does not need an inline" in launch
        assert "Use lifecycle or" in launch
        assert "only when the request is rejected" in launch
        assert "Runtime Paths For L1 Control Verbs" in launch
        assert "python3 -m harnessd.harnessctl <verb> ..." in launch
        assert "HARNESSD_SOCKET=" in launch
        assert "runtime-native intake" in launch
        assert "only for L1-owned" in launch
        assert "`answer-down`" not in launch
        assert "specialized control edge" in launch
        assert "`needs_answer`/answer messages" in launch
        assert "Not a Test Run" in launch
        assert "<project-name>/client-brief/fidelity-judgment.md" in launch
        assert "Record the exact" in launch
        assert "recipient-visible action you drove" in launch
        assert "cleaner\nrepresentative command" in launch
        assert "read the durable file `plan.md`" in launch
        assert "native runtime task list is useful working memory, but it is not" in launch
        assert "confirm every evidence path you cite" in launch
        assert "`.signal.exec.json` resolves relative to your node" in launch
        assert "l1-token-live" in launch
        assert "operational/L1/intake-session-template.md" not in launch
        assert "operational/L1/intake-session-template.md" in reference
        assert str(launch_surface.harness_root() / "operational/L2/spawn-template.md") in reference
    finally:
        _restore_runtime(previous)


def test_launch_surface_non_pilot_roles_keep_legacy_path(tmp_path):
    previous = _bind_runtime(tmp_path)
    try:
        non_pilot = config.LevelConfig(
            level="LX",
            model="test",
            runtime="test",
            role_variant="LX",
            tool_manifest=(),
        )
        assert (
            launch_surface.materialize(
                "proj/widget#exec",
                non_pilot,
                {"workspace": str(tmp_path / "node")},
                runtime_root=tmp_path,
            )
            is None
        )
    finally:
        _restore_runtime(previous)


def test_launch_surface_validates_expected_pilot_blocks_on_real_corpus():
    launch_surface.validate("L1")
    launch_surface.validate("L2")
    launch_surface.validate("L2+")
    launch_surface.validate("L3")
    launch_surface.validate("L3+")
    launch_surface.validate("L4")
    launch_surface.validate("L4+")
    launch_surface.validate("L5")
    launch_surface.validate("L5+")


def test_launch_surface_audit_reports_blocks_and_source_hashes():
    payload = audit_launch_surfaces.build_audit(roles=("L1", "L5"))

    assert payload["schema_version"] == 1
    assert payload["roles"]["L1"]["kinds"]["launch"]["block_count"] >= 1
    assert payload["roles"]["L5"]["kinds"]["reference"]["block_count"] == 1
    assert payload["roles"]["L5"]["source_files"][0]["sha256"]


def test_launch_surface_missing_expected_block_fails(tmp_path):
    root = _corpus_copy(tmp_path)
    doc = root / "operational/L5/config.md"
    text = doc.read_text(encoding="utf-8")
    text = text.replace("<!-- surface:L5 launch id=verification-floor v1 -->", "")
    text = text.replace("<!-- /surface:L5 launch id=verification-floor -->", "")
    doc.write_text(text, encoding="utf-8")

    with pytest.raises(launch_surface.LaunchSurfaceError, match="missing surface block launch id=verification-floor"):
        launch_surface.validate("L5", root=root)


def test_launch_surface_duplicate_expected_block_fails(tmp_path):
    root = _corpus_copy(tmp_path)
    doc = root / "operational/L5/spawn-template.md"
    doc.write_text(
        doc.read_text(encoding="utf-8")
        + "\n<!-- surface:L5 reference id=reference-map-v1 -->\nDuplicate reference.\n"
        + "<!-- /surface:L5 reference id=reference-map-v1 -->\n",
        encoding="utf-8",
    )

    with pytest.raises(launch_surface.LaunchSurfaceError, match="duplicate surface block reference id=reference-map-v1"):
        launch_surface.validate("L5", root=root)


def test_launch_surface_unclosed_marker_fails_loudly(tmp_path):
    root = _corpus_copy(tmp_path)
    doc = root / "operational/L5/role.md"
    doc.write_text(
        doc.read_text(encoding="utf-8").replace(
            "<!-- /surface:L5 launch id=execute-review-pair -->",
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(launch_surface.LaunchSurfaceError, match="unclosed surface marker launch id=execute-review-pair"):
        launch_surface.validate("L5", root=root)


def test_launch_surface_doc_system_overlap_allows_wrapper_but_rejects_partial_crossing(tmp_path):
    root = _corpus_copy(tmp_path)
    launch_surface.validate("L5+", root=root)

    doc = root / "operational/L5+/role.md"
    text = doc.read_text(encoding="utf-8")
    safe = (
        "<!-- surface:L5+ launch id=gate-output-contract v1 -->\n"
        "<!-- block:gate-output-contract v7 -->"
    )
    unsafe = (
        "<!-- block:gate-output-contract v7 -->\n"
        "<!-- surface:L5+ launch id=gate-output-contract v1 -->"
    )
    doc.write_text(text.replace(safe, unsafe, 1), encoding="utf-8")

    with pytest.raises(launch_surface.LaunchSurfaceError, match="unsafe overlap"):
        launch_surface.validate("L5+", root=root)
