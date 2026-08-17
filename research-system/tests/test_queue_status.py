"""Item-4 ratification queue reminder CLI and deliver-only launchd assets."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import Sandbox
from ht import jsonio
from ht.commands import queue_status as queue_status_command
from ht.paths import Root as ResearchRoot


ROOT = Path(__file__).resolve().parents[1]
_SOURCE_PYTHONPATH = os.pathsep.join(
    (
        str(ROOT / "system/instruments/composition-gate"),
        str(ROOT / "system"),
    )
)


def _run_queue_status(sandbox: Sandbox, *args: str, env_extra: dict | None = None):
    """Run this new view against only its co-versioned source dependencies."""

    child_env = {"PYTHONPATH": _SOURCE_PYTHONPATH}
    if env_extra:
        child_env.update(env_extra)
    return sandbox.run("queue-status", *args, env_extra=child_env)


def _queue_item(kind: str, *, disposition=None) -> str:
    return json.dumps(
        {
            "id": "RQ-1",
            "kind": kind,
            "payload_ref": "readout/example.md",
            "text": "Review this item",
            "queued_by": "harness",
            "date": "2026-07-13",
            "disposition": disposition,
            "annotations": [],
        }
    ) + "\n"


def _gate(verdict: str, number: int) -> dict:
    return {
        "verdict": verdict,
        "date": "2026-07-14",
        "review_ref": f"GR-{number}",
        "review_sha256": f"{number:064x}",
        "note": f"Synthetic {verdict} fixture for queue-status.",
    }


def _merge_record(
    record_id: str,
    *,
    verdict: str | None = None,
    consumed_epoch: int | None = None,
) -> dict:
    number = int(record_id.removeprefix("MR-"))
    return {
        "id": record_id,
        "candidate_ref": f"tree#L4/node#{number}",
        "lane_verdict": "lane-pass",
        "scope": {
            "lane": "L4",
            "seats": [],
            "surfaces": [],
            "globs": [f"trees/L4/nodes/{number}/**"],
        },
        "screen": {
            "results": [],
            "output_ref": None,
            "log_ref": None,
            "log_sha256": None,
            "output_sha256": None,
            "computed": None,
            "head_commit": None,
            "head_tree": None,
            "config_hash": None,
            "engine_version": None,
        },
        "gate_verdict": None if verdict is None else _gate(verdict, number),
        "watch_link": None,
        "created": "2026-07-14",
        "consumed_epoch": consumed_epoch,
    }


def _commit_merge_records(sandbox: Sandbox, records: list[dict]) -> None:
    relative_paths = []
    for record in records:
        relative = f"tier1/merge-records/{record['id']}.json"
        sandbox.write_file(relative, jsonio.dumps(record))
        relative_paths.append(relative)
    added = sandbox.git("add", "--", *relative_paths)
    assert added.returncode == 0, added.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "synthetic queue records")
    assert committed.returncode == 0, committed.stderr


def _physical_state(sandbox: Sandbox) -> tuple:
    def git_output(*args: str) -> str:
        result = sandbox.git(*args)
        assert result.returncode == 0, result.stderr
        return result.stdout

    var_rows: list[tuple[str, bytes]] = []
    var_root = sandbox.root / "var"
    if var_root.exists():
        for directory, _directories, files in os.walk(var_root):
            for filename in files:
                path = Path(directory) / filename
                var_rows.append(
                    (path.relative_to(sandbox.root).as_posix(), path.read_bytes())
                )
    return (
        git_output("rev-parse", "HEAD"),
        git_output("diff", "--raw"),
        git_output("diff", "--cached", "--raw"),
        git_output("status", "--porcelain=v1", "--untracked-files=all"),
        tuple(sorted(var_rows)),
    )


def _fake_osascript(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "osascript.log"
    fake = bin_dir / "osascript"
    fake.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$OSASCRIPT_LOG"\n')
    fake.chmod(0o755)
    return bin_dir, log


def test_queue_status_without_notify_prints_pending_total_and_sorted_kinds(sandbox):
    sandbox.write_file(
        "tier1/ratification-queue/RQ-1.json", _queue_item("tier3-ratification")
    )
    sandbox.write_file(
        "tier1/ratification-queue/RQ-2.json", _queue_item("improvement-note")
    )
    sandbox.write_file(
        "tier1/ratification-queue/RQ-3.json",
        _queue_item(
            "improvement-note",
            disposition={"status": "accepted", "by": "user", "date": "2026-07-13"},
        ),
    )

    result = _run_queue_status(sandbox)

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "queue-status: pending=2; by-kind: improvement-note=1, "
        "tier3-ratification=1; pending-merges=0\n"
    )
    assert result.stderr == ""


def test_notify_is_silent_and_does_not_call_osascript_for_empty_queue(
    sandbox, tmp_path
):
    bin_dir, log = _fake_osascript(tmp_path)

    result = _run_queue_status(
        sandbox,
        "--notify",
        env_extra={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "OSASCRIPT_LOG": str(log)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert not log.exists()


def test_pending_improvement_note_invokes_osascript_once(sandbox, tmp_path):
    sandbox.write_file(
        "tier1/ratification-queue/RQ-1.json", _queue_item("improvement-note")
    )
    bin_dir, log = _fake_osascript(tmp_path)

    result = _run_queue_status(
        sandbox,
        "--notify",
        env_extra={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "OSASCRIPT_LOG": str(log)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    argv = log.read_text().splitlines()
    assert argv[0] == "-e"
    assert "display notification" in argv[1]
    assert (
        "1 pending ratification item: improvement-note=1; pending merges: 0"
        in argv[1]
    )
    assert 'with title "Hypothesis Tree"' in argv[1]


def test_dry_run_shows_notification_without_invoking_osascript(sandbox, tmp_path):
    sandbox.write_file(
        "tier1/ratification-queue/RQ-1.json", _queue_item("improvement-note")
    )
    bin_dir, log = _fake_osascript(tmp_path)

    result = _run_queue_status(
        sandbox,
        "--notify",
        "--dry-run",
        env_extra={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "OSASCRIPT_LOG": str(log)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "DRY-RUN notification: 1 pending ratification item: improvement-note=1; "
        "pending merges: 0\n"
    )
    assert result.stderr == ""
    assert not log.exists()


def test_dry_run_requires_notify_and_asserts_rejection_on_stderr(sandbox):
    result = _run_queue_status(sandbox, "--dry-run")

    assert result.returncode == 3
    assert result.stdout == ""
    assert result.stderr == "REJECTED: --dry-run requires --notify\n"


def test_malformed_queue_item_is_rejected_on_stderr(sandbox):
    sandbox.write_file("tier1/ratification-queue/RQ-1.json", "{not json}\n")

    result = _run_queue_status(sandbox)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "REJECTED: cannot read ratification queue item" in result.stderr
    assert "tier1/ratification-queue/RQ-1.json" in result.stderr
    assert "(RQ-1 notification path)" in result.stderr


def test_missing_disposition_is_not_treated_as_pending(sandbox):
    item = json.loads(_queue_item("improvement-note"))
    del item["disposition"]
    sandbox.write_file("tier1/ratification-queue/RQ-1.json", json.dumps(item))

    result = _run_queue_status(sandbox)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "REJECTED:" in result.stderr
    assert "has no disposition field (RQ-1 notification path)" in result.stderr


def test_invalid_utf8_queue_item_is_rejected_without_traceback(sandbox):
    path = sandbox.root / "tier1/ratification-queue/RQ-1.json"
    path.write_bytes(b"\xff\xfe\n")

    result = _run_queue_status(sandbox)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "REJECTED: cannot read ratification queue item" in result.stderr
    assert "UnicodeDecodeError" not in result.stderr
    assert "Traceback" not in result.stderr


def test_osascript_failure_is_rejected_with_stderr_detail(sandbox, tmp_path):
    sandbox.write_file(
        "tier1/ratification-queue/RQ-1.json", _queue_item("improvement-note")
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "osascript"
    fake.write_text("#!/bin/sh\nprintf '%s\\n' 'notification denied' >&2\nexit 7\n")
    fake.chmod(0o755)

    result = _run_queue_status(
        sandbox,
        "--notify",
        env_extra={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "REJECTED: macOS notification failed: notification denied "
        "(RQ-1 notification path)\n"
    )


def test_pending_merge_count_uses_every_committed_unconsumed_record_and_is_read_only(
    sandbox: Sandbox,
) -> None:
    records = [
        _merge_record("MR-1"),
        _merge_record("MR-2", verdict="land"),
        _merge_record("MR-3", verdict="hold"),
        _merge_record("MR-4", verdict="consolidate-first"),
        _merge_record("MR-5", verdict="land", consumed_epoch=8),
    ]
    _commit_merge_records(sandbox, records)

    # Changed and untracked MR-shaped worktree bytes are deliberately visible
    # in physical status but invisible to the committed selector.
    sandbox.write_file(
        "tier1/merge-records/MR-1.json",
        jsonio.dumps(_merge_record("MR-1", verdict="land", consumed_epoch=9)),
    )
    sandbox.write_file(
        "tier1/merge-records/MR-99.json",
        jsonio.dumps(_merge_record("MR-99")),
    )
    before = _physical_state(sandbox)

    result = _run_queue_status(sandbox)

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "queue-status: pending=0; by-kind: none; pending-merges=4\n"
    )
    assert result.stderr == ""
    assert _physical_state(sandbox) == before


def test_pending_merges_alone_are_silent_and_do_not_invoke_osascript(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    _commit_merge_records(sandbox, [_merge_record("MR-1", verdict="hold")])
    bin_dir, log = _fake_osascript(tmp_path)

    result = _run_queue_status(
        sandbox,
        "--notify",
        env_extra={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "OSASCRIPT_LOG": str(log),
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert not log.exists()


def test_rq_notification_and_dry_run_include_pending_merge_count(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    _commit_merge_records(
        sandbox,
        [_merge_record("MR-1"), _merge_record("MR-2", verdict="hold")],
    )
    sandbox.write_file(
        "tier1/ratification-queue/RQ-1.json", _queue_item("improvement-note")
    )
    bin_dir, log = _fake_osascript(tmp_path)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "OSASCRIPT_LOG": str(log),
    }

    notified = _run_queue_status(sandbox, "--notify", env_extra=env)

    assert notified.returncode == 0, notified.stderr
    assert notified.stdout == ""
    argv = log.read_text().splitlines()
    assert len(argv) == 2
    assert (
        "1 pending ratification item: improvement-note=1; pending merges: 2"
        in argv[1]
    )

    log.unlink()
    dry_run = _run_queue_status(
        sandbox, "--notify", "--dry-run", env_extra=env
    )

    assert dry_run.returncode == 0, dry_run.stderr
    assert dry_run.stdout == (
        "DRY-RUN notification: 1 pending ratification item: "
        "improvement-note=1; pending merges: 2\n"
    )
    assert dry_run.stderr == ""
    assert not log.exists()


def test_malformed_committed_selector_rejects_before_rq_output_or_osascript(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    relative = "tier1/merge-records/MR-1.json"
    sandbox.write_file(relative, "{not json}\n")
    sandbox.write_file(
        "tier1/ratification-queue/RQ-1.json", _queue_item("improvement-note")
    )
    added = sandbox.git(
        "add", "--", relative, "tier1/ratification-queue/RQ-1.json"
    )
    assert added.returncode == 0, added.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "malformed MR store")
    assert committed.returncode == 0, committed.stderr
    bin_dir, log = _fake_osascript(tmp_path)

    result = _run_queue_status(
        sandbox,
        "--notify",
        env_extra={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "OSASCRIPT_LOG": str(log),
        },
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "REJECTED: merge-record snapshot discovery failed" in result.stderr
    assert not log.exists()


def test_queue_status_loads_one_snapshot_and_one_unconsumed_view(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ht import mrec_views

    calls = {"load": 0, "unconsumed": 0}

    class Snapshot:
        def unconsumed(self) -> tuple[object, ...]:
            calls["unconsumed"] += 1
            return (object(), object())

    def load_once(root: Path) -> Snapshot:
        calls["load"] += 1
        assert root == sandbox.root
        return Snapshot()

    monkeypatch.setattr(mrec_views, "load_merge_record_snapshot", load_once)

    result = queue_status_command.run(
        ResearchRoot(sandbox.root), notify=False, dry_run=False
    )

    assert result == 0
    assert calls == {"load": 1, "unconsumed": 1}
    assert capsys.readouterr().out == (
        "queue-status: pending=0; by-kind: none; pending-merges=2\n"
    )


def test_queue_status_module_import_does_not_eagerly_import_selector() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "system")
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import ht.commands.queue_status; "
                "assert 'ht.mrec_views' not in sys.modules"
            ),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert imported.returncode == 0, imported.stderr
