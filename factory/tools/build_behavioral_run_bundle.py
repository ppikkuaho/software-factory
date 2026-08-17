#!/usr/bin/env python3
"""Prepare and capture repeatable behavioral-run bundles.

This tool is an operator/audit wrapper. It does not spawn agents directly and it
does not mutate a runtime tree. `prepare` writes a run manifest plus launch and
capture helper scripts. `capture` reads the manifest and packages the passive
evidence index, behavioral views, and run-score packet into one bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_WORKSPACES_ROOT = Path.home() / "l1-l5-workspaces"  # mirrors commissioning.DEFAULT_WORKSPACES_ROOT (relocated 2026-07-07)
_SCHEMA_VERSION = 1
_UNJAILED_SKIP_PERMISSIONS_ENV = "HARNESS_UNJAILED_SKIP_PERMISSIONS"


def _load_tool(name: str):
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load tool at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip()).strip("-").lower()
    return slug or "run"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _repo_fact(args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return proc.stdout.strip()


def _dirty_files() -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=_REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    dirty = []
    for line in proc.stdout.splitlines():
        if len(line) > 3:
            dirty.append(line[3:])
    return dirty


def _repo_metadata() -> dict[str, Any]:
    return {
        "repo_root": str(_REPO_ROOT),
        "branch": _repo_fact(["branch", "--show-current"]),
        "head": _repo_fact(["rev-parse", "HEAD"]),
        "dirty_files": _dirty_files(),
    }


def _write_json(path: Path, payload: dict[str, Any], *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=None if compact else 2) + "\n"
    path.write_text(text, encoding="utf-8")


def _make_executable(path: Path) -> None:
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _render_launch_script(*, manifest_path: Path, runtime_root: Path, build_id: str, intake_path: Path) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"cd {shlex.quote(str(_REPO_ROOT))}",
            f"export {_UNJAILED_SKIP_PERMISSIONS_ENV}=1",
            f"export HARNESS_BUILD_ID={shlex.quote(build_id)}",
            f"export HARNESS_RUNTIME_ROOT={shlex.quote(str(runtime_root))}",
            f"export HARNESS_L1_INTAKE=\"$(cat {shlex.quote(str(intake_path))})\"",
            f"echo \"Launching behavioral run from manifest: {shlex.quote(str(manifest_path))}\" >&2",
            "exec python3 -m harnessd.daemon",
            "",
        ]
    )


def _render_capture_script(manifest_path: Path) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"cd {shlex.quote(str(_REPO_ROOT))}",
            f"exec python3 tools/build_behavioral_run_bundle.py capture --manifest {shlex.quote(str(manifest_path))}",
            "",
        ]
    )


def prepare_bundle(
    *,
    scenario_id: str,
    initial_intake_path: Path,
    bundle_dir: Path,
    build_id: str | None = None,
    runtime_root: Path | None = None,
    workspaces_root: Path | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    created_at = created_at or _now()
    scenario_slug = _slug(scenario_id)
    timestamp_slug = re.sub(r"[^0-9TZ]", "", created_at)
    if build_id is None:
        build_id = f"{scenario_slug}-{timestamp_slug}"
    if workspaces_root is None:
        workspaces_root = _DEFAULT_WORKSPACES_ROOT
    if runtime_root is None:
        runtime_root = Path(workspaces_root) / build_id

    bundle_dir = Path(bundle_dir)
    scenario_dir = bundle_dir / "scenario"
    capture_dir = bundle_dir / "capture"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    capture_dir.mkdir(parents=True, exist_ok=True)

    intake_bytes = Path(initial_intake_path).read_bytes()
    copied_intake = scenario_dir / "initial-intake.md"
    copied_intake.write_bytes(intake_bytes)

    manifest_path = bundle_dir / "run-manifest.json"
    launch_script = bundle_dir / "launch.sh"
    capture_script = bundle_dir / "capture.sh"
    run_id = build_id

    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "behavioral_run_manifest",
        "observer_effect": (
            "prepare/capture wrapper only; launch helper delegates to harnessd.daemon with recorded "
            "run env, including the user-approved unattended permission posture; capture reads "
            "passive runtime artifacts"
        ),
        "run_id": run_id,
        "scenario": {
            "id": scenario_id,
            "initial_intake_path": str(copied_intake),
            "initial_intake_sha256": _sha256_bytes(intake_bytes),
            "initial_intake_bytes": len(intake_bytes),
        },
        "runtime": {
            "build_id": build_id,
            "runtime_root": str(Path(runtime_root)),
            "workspaces_root": str(Path(workspaces_root)),
            "launch_command": ["python3", "-m", "harnessd.daemon"],
            "launch_env": {
                _UNJAILED_SKIP_PERMISSIONS_ENV: "1",
                "HARNESS_BUILD_ID": build_id,
                "HARNESS_RUNTIME_ROOT": str(Path(runtime_root)),
                "HARNESS_L1_INTAKE": "<contents of scenario/initial-intake.md>",
            },
        },
        "bundle": {
            "bundle_dir": str(bundle_dir),
            "manifest_path": str(manifest_path),
            "launch_script": str(launch_script),
            "capture_script": str(capture_script),
            "capture_dir": str(capture_dir),
            "evidence_index_path": str(capture_dir / "evidence-index.json"),
            "behavioral_views_path": str(capture_dir / "behavioral-views.json"),
            "run_score_packet_path": str(capture_dir / "run-score-packet.json"),
            "dashboard_path": str(capture_dir / "dashboard.md"),
            "capture_manifest_path": str(capture_dir / "capture-manifest.json"),
        },
        "repo": _repo_metadata(),
        "created_at": created_at,
    }

    _write_json(manifest_path, manifest)
    launch_script.write_text(
        _render_launch_script(
            manifest_path=manifest_path,
            runtime_root=Path(runtime_root),
            build_id=build_id,
            intake_path=copied_intake,
        ),
        encoding="utf-8",
    )
    capture_script.write_text(_render_capture_script(manifest_path), encoding="utf-8")
    _make_executable(launch_script)
    _make_executable(capture_script)
    return manifest


def _load_manifest(path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as fh:
        manifest = json.load(fh)
    if not isinstance(manifest, dict):
        raise ValueError("run manifest must be a JSON object")
    if manifest.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(f"unsupported run manifest schema_version={manifest.get('schema_version')!r}")
    return manifest


def capture_bundle(*, manifest_path: Path, compact: bool = False) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    runtime_root = Path(manifest["runtime"]["runtime_root"])
    bundle = manifest.get("bundle") if isinstance(manifest.get("bundle"), dict) else {}
    capture_dir = Path(bundle.get("capture_dir") or Path(manifest_path).parent / "capture")
    capture_dir.mkdir(parents=True, exist_ok=True)

    indexer = _load_tool("build_behavioral_evidence_index")
    views_tool = _load_tool("build_behavioral_views")
    score_tool = _load_tool("build_run_score_packet")
    dashboard_tool = _load_tool("build_behavioral_dashboard")

    evidence_index = indexer.build_index(runtime_root)
    behavioral_views = views_tool.build_views_from_index(evidence_index)
    score_packet = score_tool.build_score_packet_from_index(evidence_index)

    index_path = Path(bundle.get("evidence_index_path") or capture_dir / "evidence-index.json")
    views_path = Path(bundle.get("behavioral_views_path") or capture_dir / "behavioral-views.json")
    score_path = Path(bundle.get("run_score_packet_path") or capture_dir / "run-score-packet.json")
    dashboard_path = Path(bundle.get("dashboard_path") or capture_dir / "dashboard.md")
    capture_manifest_path = Path(bundle.get("capture_manifest_path") or capture_dir / "capture-manifest.json")

    _write_json(index_path, evidence_index, compact=compact)
    _write_json(views_path, behavioral_views, compact=compact)
    _write_json(score_path, score_packet, compact=compact)
    dashboard_path.write_text(
        dashboard_tool.build_dashboard(
            behavioral_views=behavioral_views,
            score_packet=score_packet,
            run_id=manifest.get("run_id"),
        ),
        encoding="utf-8",
    )

    capture_manifest = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "behavioral_run_capture_manifest",
        "observer_effect": "read-only runtime artifact parse; no runtime mutation and no behavioral verdicts",
        "run_id": manifest.get("run_id"),
        "source_manifest_path": str(Path(manifest_path)),
        "runtime_root": str(runtime_root),
        "generated_at": _now(),
        "artifacts": {
            "evidence_index": {
                "path": str(index_path),
                "schema_version": evidence_index.get("schema_version"),
                "counts": evidence_index.get("counts") or {},
            },
            "behavioral_views": {
                "path": str(views_path),
                "schema_version": behavioral_views.get("schema_version"),
                "counts": behavioral_views.get("counts") or {},
            },
            "run_score_packet": {
                "path": str(score_path),
                "schema_version": score_packet.get("schema_version"),
                "scoreability": score_packet.get("scoreability") or {},
            },
            "dashboard": {
                "path": str(dashboard_path),
                "format": "markdown",
            },
        },
        "repo_at_capture": _repo_metadata(),
    }
    _write_json(capture_manifest_path, capture_manifest, compact=compact)
    return capture_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="create a behavioral run manifest and helper scripts")
    prepare.add_argument("--scenario-id", required=True)
    prepare.add_argument("--initial-intake-file", required=True)
    prepare.add_argument("--bundle-dir", required=True)
    prepare.add_argument("--build-id", default=None)
    prepare.add_argument("--runtime-root", default=None)
    prepare.add_argument("--workspaces-root", default=None)
    prepare.add_argument("--output", "-o", default=None, help="optional path for a copy of the manifest JSON")
    prepare.add_argument("--compact", action="store_true")

    capture = subparsers.add_parser("capture", help="build index/views/score packet for a run manifest")
    capture.add_argument("--manifest", required=True)
    capture.add_argument("--output", "-o", default=None, help="optional path for a copy of the capture manifest")
    capture.add_argument("--compact", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "prepare":
        payload = prepare_bundle(
            scenario_id=args.scenario_id,
            initial_intake_path=Path(args.initial_intake_file),
            bundle_dir=Path(args.bundle_dir),
            build_id=args.build_id,
            runtime_root=Path(args.runtime_root) if args.runtime_root else None,
            workspaces_root=Path(args.workspaces_root) if args.workspaces_root else None,
        )
    else:
        payload = capture_bundle(manifest_path=Path(args.manifest), compact=args.compact)

    if args.output:
        _write_json(Path(args.output), payload, compact=args.compact)
    else:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=None if args.compact else 2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
