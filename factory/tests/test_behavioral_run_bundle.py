import importlib.util
import json
import os
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "tools" / "build_behavioral_run_bundle.py"
    spec = importlib.util.spec_from_file_location("build_behavioral_run_bundle", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_prepare_bundle_writes_manifest_intake_and_helper_scripts(tmp_path):
    mod = _module()
    intake = tmp_path / "intake.md"
    intake.write_text("Build a tiny log viewer.\n", encoding="utf-8")
    bundle_dir = tmp_path / "bundle"
    runtime_root = tmp_path / "runtime"

    manifest = mod.prepare_bundle(
        scenario_id="logview smoke",
        initial_intake_path=intake,
        bundle_dir=bundle_dir,
        build_id="build-logview-test",
        runtime_root=runtime_root,
        workspaces_root=tmp_path,
        created_at="2026-06-16T00:00:00Z",
    )

    manifest_path = bundle_dir / "run-manifest.json"
    launch_script = bundle_dir / "launch.sh"
    capture_script = bundle_dir / "capture.sh"
    copied_intake = bundle_dir / "scenario" / "initial-intake.md"

    assert manifest["schema_version"] == 1
    assert manifest["kind"] == "behavioral_run_manifest"
    assert manifest["run_id"] == "build-logview-test"
    assert manifest["runtime"]["runtime_root"] == str(runtime_root)
    assert manifest["runtime"]["launch_env"]["HARNESS_UNJAILED_SKIP_PERMISSIONS"] == "1"
    assert manifest["scenario"]["initial_intake_sha256"]
    assert manifest_path.is_file()
    assert copied_intake.read_text(encoding="utf-8") == "Build a tiny log viewer.\n"
    assert launch_script.is_file() and os.access(launch_script, os.X_OK)
    assert capture_script.is_file() and os.access(capture_script, os.X_OK)
    launch_text = launch_script.read_text(encoding="utf-8")
    assert "HARNESS_UNJAILED_SKIP_PERMISSIONS=1" in launch_text
    assert "HARNESS_BUILD_ID=build-logview-test" in launch_text
    assert f"HARNESS_RUNTIME_ROOT={runtime_root}" in launch_text
    assert "python3 -m harnessd.daemon" in launch_text
    assert "build_behavioral_run_bundle.py capture" in capture_script.read_text(encoding="utf-8")
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert loaded["bundle"]["capture_dir"] == str(bundle_dir / "capture")
    assert loaded["bundle"]["dashboard_path"] == str(bundle_dir / "capture" / "dashboard.md")


def test_capture_bundle_writes_index_views_score_and_capture_manifest(tmp_path):
    mod = _module()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "binding-ledger.json").write_text(
        json.dumps(
            {
                "proj/task#exec": {
                    "node_address": "proj/task#exec",
                    "state": "done",
                    "level": "L5",
                    "gate_state": "gate_passed",
                    "gate_id": "gate-1",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (runtime / "run-ledger.jsonl").write_text(
        json.dumps(
            {
                "seq": 1,
                "ts": "2026-06-16T00:00:00Z",
                "node_address": "proj/task#exec",
                "event": "gate_passed",
                "binding_delta": {"gate_id": "gate-1", "gate_state": "gate_passed"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    intake = tmp_path / "intake.md"
    intake.write_text("Capture smoke.\n", encoding="utf-8")
    bundle = tmp_path / "bundle"
    manifest = mod.prepare_bundle(
        scenario_id="capture smoke",
        initial_intake_path=intake,
        bundle_dir=bundle,
        build_id="capture-smoke",
        runtime_root=runtime,
        created_at="2026-06-16T00:00:00Z",
    )
    intake_path = bundle / "scenario" / "initial-intake.md"
    manifest_path = bundle / "run-manifest.json"
    manifest["scenario"]["initial_intake_path"] = str(intake_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    capture_manifest = mod.capture_bundle(manifest_path=manifest_path)

    capture_dir = bundle / "capture"
    index = json.loads((capture_dir / "evidence-index.json").read_text(encoding="utf-8"))
    views = json.loads((capture_dir / "behavioral-views.json").read_text(encoding="utf-8"))
    score = json.loads((capture_dir / "run-score-packet.json").read_text(encoding="utf-8"))
    dashboard = (capture_dir / "dashboard.md").read_text(encoding="utf-8")

    assert capture_manifest["kind"] == "behavioral_run_capture_manifest"
    assert capture_manifest["run_id"] == "capture-smoke"
    assert capture_manifest["artifacts"]["evidence_index"]["counts"]["nodes"] == 1
    assert capture_manifest["artifacts"]["dashboard"]["format"] == "markdown"
    assert index["counts"]["nodes"] == 1
    assert views["views"]["gates"][0]["gate_id"] == "gate-1"
    assert score["scoring_mode"]["kind"] == "evidence_packet_only"
    assert dashboard.startswith("# Behavioral Run Dashboard: capture-smoke")


def test_cli_prepare_and_capture(tmp_path):
    mod = _module()
    intake = tmp_path / "intake.md"
    intake.write_text("Build a tiny CLI.\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "binding-ledger.json").write_text("{}\n", encoding="utf-8")
    bundle = tmp_path / "bundle"

    rc = mod.main(
        [
            "prepare",
            "--scenario-id",
            "cli smoke",
            "--initial-intake-file",
            str(intake),
            "--bundle-dir",
            str(bundle),
            "--build-id",
            "cli-smoke",
            "--runtime-root",
            str(runtime),
            "--compact",
        ]
    )
    assert rc == 0

    rc = mod.main(["capture", "--manifest", str(bundle / "run-manifest.json"), "--compact"])
    assert rc == 0
    assert (bundle / "capture" / "capture-manifest.json").is_file()
    assert (bundle / "capture" / "dashboard.md").is_file()


def test_dirty_files_preserves_first_path_character(monkeypatch):
    mod = _module()

    class Result:
        stdout = " M design/working-notes/example.md\n"

    def fake_run(*args, **kwargs):
        return Result()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod._dirty_files() == ["design/working-notes/example.md"]
