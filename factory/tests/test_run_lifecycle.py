"""Run-scoped daemon lifecycle — on-demand launchd, one-shot genesis, definitive idle exit.

No test loads a real launchd job. Process/service seams are injected; plist and lifecycle artifacts
are real files under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import harnessd.daemon as daemon
import harnessd.lifecycle as lifecycle
import harnessd.commissioning as commissioning


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_paths_and_label_are_deterministic_and_runtime_scoped(tmp_path):
    root = tmp_path / "run one"
    paths = lifecycle.paths_for(root)
    assert paths.directory == root / ".harnessd" / "lifecycle"
    assert paths.request.parent == paths.directory
    assert paths.claimed_request.parent == paths.directory
    assert paths.plist.parent == paths.directory
    assert paths.stdout_log.parent == paths.directory
    assert paths.stderr_log.parent == paths.directory

    label = lifecycle.launchd_label(root, "Build One")
    assert label == lifecycle.launchd_label(root, "Build One")
    assert label.startswith("com.harness.daemon.")
    assert " " not in label and "/" not in label
    assert label != lifecycle.launchd_label(tmp_path / "other", "Build One")


def test_prepare_run_renders_crash_only_per_run_plist_and_private_request(tmp_path):
    root = tmp_path / "run"
    calls = []

    def launchctl(args):
        calls.append(tuple(args))
        return _completed(returncode=1, stderr="service not found")

    spec = lifecycle.prepare_run(
        runtime_root=root,
        build_id="build-17",
        initial_intake="Build the parser.",
        repo_root=Path(__file__).resolve().parents[1],
        python_executable="/usr/bin/python3",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=launchctl,
    )

    assert calls and calls[0][0] == "print", "loaded-job probe must precede artifact creation"
    assert spec.request_id
    payload = json.loads(spec.paths.request.read_text(encoding="utf-8"))
    assert payload["schema_version"] == lifecycle.REQUEST_SCHEMA_VERSION
    assert payload["request_id"] == spec.request_id
    assert payload["build_id"] == "build-17"
    assert payload["runtime_root"] == str(root.resolve())
    assert payload["initial_intake"] == "Build the parser."
    assert payload["fidelity_playback_authority"] == "owner"
    assert payload["fidelity_playback_delegate"] is None
    assert payload["fidelity_playback_delegation_reason"] is None
    assert "review_panel_arms" not in payload
    assert spec.paths.request.stat().st_mode & 0o777 == 0o600

    with spec.paths.plist.open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["Label"] == spec.label
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert "RunAtLoad" not in plist
    assert plist["ThrottleInterval"] >= 10
    assert plist["WorkingDirectory"] == str(Path(__file__).resolve().parents[1])
    assert plist["ProgramArguments"][0] == "/usr/bin/python3"
    assert plist["ProgramArguments"][1:3] == ["-m", "harnessd.daemon"]
    assert "--start-request" in plist["ProgramArguments"]
    assert str(spec.paths.request) in plist["ProgramArguments"]
    assert plist["StandardOutPath"] == str(spec.paths.stdout_log)
    assert plist["StandardErrorPath"] == str(spec.paths.stderr_log)


def test_prepare_run_captures_delegate_triple_in_the_one_shot_request(tmp_path):
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="delegated-run",
        fidelity_playback_authority="operator-delegate",
        fidelity_playback_delegate="synthetic-owner",
        fidelity_playback_delegation_reason="owner-ruled synthetic trace",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )

    payload = json.loads(spec.paths.request.read_text(encoding="utf-8"))
    assert payload["fidelity_playback_authority"] == "operator-delegate"
    assert payload["fidelity_playback_delegate"] == "synthetic-owner"
    assert (
        payload["fidelity_playback_delegation_reason"]
        == "owner-ruled synthetic trace"
    )


def test_prepare_run_captures_ordered_mixed_panel_arms_in_the_one_shot_request(
    tmp_path,
):
    arms = [
        {
            "pattern": "forge-queue/queue-core#exec",
            "axes": ["composition-interface", "risk-readiness"],
        },
        {
            "pattern": "forge-queue/persistence#exec",
            "axes": ["broad"],
        },
    ]
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="mixed-panel-run",
        review_panel_arms=arms,
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )

    payload = json.loads(spec.paths.request.read_text(encoding="utf-8"))
    assert payload["review_panel_arms"] == arms


def test_prepare_run_freezes_all_daemon_direct_tools_into_recorded_plist_environment(
    tmp_path, monkeypatch
):
    tool_bin = tmp_path / "tool-bin"
    tool_bin.mkdir()
    for name in ("python3", "tmux", "git", "sandbox-exec", "security"):
        path = tool_bin / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool_bin))

    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="tool-freeze",
        python_executable=str(tool_bin / "python3"),
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )
    with spec.paths.plist.open("rb") as handle:
        plist = plistlib.load(handle)

    environment = plist["EnvironmentVariables"]
    assert environment["PATH"].split(os.pathsep)[0] == str(tool_bin.resolve())
    assert environment["HARNESSD_TMUX"] == str((tool_bin / "tmux").resolve())
    assert environment["HARNESSD_GIT"] == str((tool_bin / "git").resolve())
    assert environment["HARNESSD_SANDBOX_EXEC"] == str(
        (tool_bin / "sandbox-exec").resolve()
    )
    assert environment["HARNESSD_SECURITY"] == str(
        (tool_bin / "security").resolve()
    )
    assert environment["HARNESSD_PYTHON"] == str((tool_bin / "python3").resolve())
    assert spec.daemon_tools == {
        "git": str((tool_bin / "git").resolve()),
        "python": str((tool_bin / "python3").resolve()),
        "sandbox-exec": str((tool_bin / "sandbox-exec").resolve()),
        "security": str((tool_bin / "security").resolve()),
        "tmux": str((tool_bin / "tmux").resolve()),
    }


def test_prepare_run_refuses_once_with_every_missing_daemon_direct_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.setattr(lifecycle, "DAEMON_SYSTEM_PATH_DIRS", ())
    root = tmp_path / "run"

    with pytest.raises(lifecycle.LifecycleError) as raised:
        lifecycle.prepare_run(
            runtime_root=root,
            build_id="missing-tools",
            python_executable=str(tmp_path / "missing-python"),
            ipc_probe=lambda _path: False,
            lock_probe=lambda _path: False,
            launchctl_runner=lambda _args: _completed(returncode=1),
        )

    message = str(raised.value)
    assert raised.value.error_code == "daemon_tools_missing"
    assert all(
        name in message
        for name in ("python", "tmux", "git", "sandbox-exec", "security")
    )
    assert not lifecycle.paths_for(root).request.exists()
    assert not lifecycle.paths_for(root).plist.exists()


@pytest.mark.parametrize("probe", ["ipc", "lock", "job"])
def test_prepare_run_refuses_each_duplicate_before_writing_artifacts(tmp_path, probe):
    root = tmp_path / "run"
    seen = []

    def ipc(_path):
        seen.append("ipc")
        return probe == "ipc"

    def lock(_path):
        seen.append("lock")
        return probe == "lock"

    def launchctl(args):
        seen.append("job")
        return _completed(returncode=0 if probe == "job" else 1)

    with pytest.raises(lifecycle.DaemonAlreadyActive):
        lifecycle.prepare_run(
            runtime_root=root,
            build_id="b",
            ipc_probe=ipc,
            lock_probe=lock,
            launchctl_runner=launchctl,
        )

    paths = lifecycle.paths_for(root)
    assert not paths.request.exists()
    assert not paths.plist.exists()
    assert seen == {
        "ipc": ["ipc"],
        "lock": ["ipc", "lock"],
        "job": ["ipc", "lock", "job"],
    }[probe]


def test_concurrent_start_guard_refuses_without_overwriting_first_request(tmp_path):
    root = tmp_path / "run"
    absent = lambda _path: False
    no_job = lambda _args: _completed(returncode=1)
    first = lifecycle.prepare_run(
        runtime_root=root,
        build_id="b",
        initial_intake="first",
        ipc_probe=absent,
        lock_probe=absent,
        launchctl_runner=no_job,
    )
    before = first.paths.request.read_bytes()
    with pytest.raises(lifecycle.DaemonAlreadyActive, match="start preparation"):
        lifecycle.prepare_run(
            runtime_root=root,
            build_id="b",
            initial_intake="second",
            ipc_probe=absent,
            lock_probe=absent,
            launchctl_runner=no_job,
        )
    assert first.paths.request.read_bytes() == before
    lifecycle.release_start_guard(first)


def test_start_request_is_claimed_once_by_atomic_rename(tmp_path):
    root = tmp_path / "run"
    spec = lifecycle.prepare_run(
        runtime_root=root,
        build_id="b",
        initial_intake="Do it.",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )

    claimed = lifecycle.claim_start_request(
        spec.paths.request,
        spec.paths.claimed_request,
        expected_request_id=spec.request_id,
    )
    assert claimed["initial_intake"] == "Do it."
    assert not spec.paths.request.exists()
    assert spec.paths.claimed_request.exists()
    assert lifecycle.claim_start_request(
        spec.paths.request,
        spec.paths.claimed_request,
        expected_request_id=spec.request_id,
    ) is None


def test_claim_refuses_wrong_request_id_without_consuming(tmp_path):
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="b",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )
    with pytest.raises(lifecycle.LifecycleError, match="request id"):
        lifecycle.claim_start_request(
            spec.paths.request,
            spec.paths.claimed_request,
            expected_request_id="different",
        )
    assert spec.paths.request.exists()
    assert not spec.paths.claimed_request.exists()


def test_bootstrap_uses_gui_domain_and_rolls_back_on_kickstart_failure(tmp_path):
    calls = []
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="b",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )

    def runner(args):
        calls.append(tuple(args))
        if args[0] == "bootstrap":
            return _completed()
        if args[0] == "kickstart":
            return _completed(returncode=5, stderr="nope")
        return _completed()

    with pytest.raises(lifecycle.LifecycleError, match="kickstart"):
        lifecycle.bootstrap_run(spec, launchctl_runner=runner, uid=501)

    assert calls[0] == ("bootstrap", "gui/501", str(spec.paths.plist))
    assert calls[1] == ("kickstart", f"gui/501/{spec.label}")
    assert calls[2] == ("bootout", f"gui/501/{spec.label}")
    assert not spec.paths.request.exists()
    assert not spec.paths.plist.exists()


def test_bootstrap_failure_retains_artifacts_if_loaded_job_cannot_be_removed(tmp_path):
    calls = []
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="b",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )

    def runner(args):
        calls.append(tuple(args))
        if args[0] == "bootstrap":
            return _completed()
        if args[0] == "kickstart":
            return _completed(returncode=5, stderr="nope")
        if args[0] == "bootout":
            return _completed(returncode=5, stderr="still loaded")
        return _completed()  # print proves the job remains loaded

    with pytest.raises(lifecycle.LifecycleError, match="kickstart"):
        lifecycle.bootstrap_run(spec, launchctl_runner=runner, uid=501)

    assert sum(call[0] == "bootout" for call in calls) == 3
    assert sum(call[0] == "print" for call in calls) == 3
    assert spec.paths.request.exists()
    assert spec.paths.plist.exists()


def test_cleanup_boots_out_then_removes_generated_artifacts(tmp_path):
    calls = []
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="b",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )
    lifecycle.claim_start_request(
        spec.paths.request,
        spec.paths.claimed_request,
        expected_request_id=spec.request_id,
    )

    assert lifecycle.cleanup_run(
        label=spec.label,
        paths=spec.paths,
        launchctl_runner=lambda args: calls.append(tuple(args)) or _completed(),
        uid=501,
    ) is True
    assert calls == [("bootout", f"gui/501/{spec.label}")]
    assert not spec.paths.plist.exists()
    assert not spec.paths.request.exists()
    assert not spec.paths.claimed_request.exists()


def test_cleanup_retains_repair_pointer_when_loaded_job_refuses_bounded_bootout(tmp_path):
    calls = []
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="b",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )
    lifecycle.claim_start_request(
        spec.paths.request,
        spec.paths.claimed_request,
        expected_request_id=spec.request_id,
    )

    def launchctl(args):
        calls.append(tuple(args))
        return _completed(returncode=0 if args[0] == "print" else 5)

    assert lifecycle.cleanup_run(
        label=spec.label,
        paths=spec.paths,
        launchctl_runner=launchctl,
        uid=501,
        sleep_fn=lambda _seconds: None,
    ) is False
    assert calls == [
        item
        for _attempt in range(3)
        for item in [
            ("bootout", f"gui/501/{spec.label}"),
            ("print", f"gui/501/{spec.label}"),
        ]
    ]
    assert spec.paths.plist.exists()
    assert spec.paths.claimed_request.exists()


def test_cleanup_removes_artifacts_when_failed_bootout_proves_job_already_absent(tmp_path):
    calls = []
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="b",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )

    def launchctl(args):
        calls.append(tuple(args))
        return _completed(returncode=1)

    assert lifecycle.cleanup_run(
        label=spec.label,
        paths=spec.paths,
        launchctl_runner=launchctl,
        uid=501,
    ) is True
    assert calls == [
        ("bootout", f"gui/501/{spec.label}"),
        ("print", f"gui/501/{spec.label}"),
    ]
    assert not spec.paths.plist.exists()
    assert not spec.paths.request.exists()


@pytest.mark.parametrize(
    ("launchctl_text", "socket_present", "expected_code", "expected_alive"),
    [
        ("state = running\nruns = 1\npid = 444\n", False, "daemon_hung", True),
        (
            "state = waiting\nruns = 4\nlast exit code = 1\n",
            False,
            "daemon_crashed",
            False,
        ),
        (
            "state = running\nruns = 4\npid = 445\nlast exit code = 1\n",
            False,
            "daemon_crashed",
            True,
        ),
        ("state = running\nruns = 1\npid = 444\n", True, "l1_not_ready", True),
    ],
)
def test_readiness_diagnostic_distinguishes_hung_crashed_and_l1_pending(
    tmp_path, launchctl_text, socket_present, expected_code, expected_alive
):
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="b",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )
    spec.paths.stderr_log.write_text(
        "\n".join(
            [f"old-{index}" for index in range(30)]
            + ["Traceback:", "FileNotFoundError: tmux"]
        )
        + "\n",
        encoding="utf-8",
    )

    error = lifecycle.readiness_error(
        spec,
        timeout_s=30,
        socket_present=socket_present,
        last_l1_error="L1 binding absent",
        launchctl_runner=lambda _args: _completed(stdout=launchctl_text),
        uid=501,
    )

    assert error.error_code == expected_code
    assert error.details["process_alive"] is expected_alive
    assert error.details["restart_count"] == (4 if expected_code == "daemon_crashed" else 1)
    assert error.details["socket_present"] is socket_present
    assert error.details["stderr_tail"][-2:] == [
        "Traceback:",
        "FileNotFoundError: tmux",
    ]
    assert len(error.details["stderr_tail"]) <= lifecycle.READINESS_STDERR_TAIL_LINES


def test_daemon_hung_refusal_names_exact_tcc_hazard_and_fix(tmp_path):
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="b",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )

    error = lifecycle.readiness_error(
        spec,
        timeout_s=30,
        socket_present=False,
        last_l1_error="[Errno 2] No such file or directory",
        launchctl_runner=lambda _args: _completed(
            stdout="state = running\nruns = 1\npid = 444\n"
        ),
        uid=501,
    )

    assert error.error_code == "daemon_hung"
    assert spec.python_executable in str(error)
    assert str(spec.repo_root) in str(error)
    assert "macOS TCC" in str(error)
    assert "Documents" in str(error)
    assert "Homebrew" in str(error) and "Cellar" in str(error)
    assert "System Settings" in str(error)
    assert "[Errno 2]" not in str(error)


@pytest.mark.parametrize(
    ("nodes", "expected"),
    [
        ({}, False),
        ({"a": {"state": "done"}, "b": {"state": "failed"}, "c": {"state": "dead"}}, False),
        ({"a": {"state": "planned"}}, True),
        ({"a": {"state": "claimed"}}, True),
        ({"a": {"state": "spawning"}}, True),
        ({"a": {"state": "running"}}, True),
        ({"a": {"state": "blocked"}}, True),
        ({"a": {}}, True),
        ({"a": {"state": "future-state"}}, True),
    ],
)
def test_live_build_read_is_exact_and_unknown_is_conservative(nodes, expected):
    assert daemon.has_live_build(read_nodes=lambda: nodes) is expected


def test_live_build_read_failure_is_conservative():
    def broken():
        raise OSError("corrupt")

    assert daemon.has_live_build(read_nodes=broken) is True


def test_schedule_cleanup_is_called_only_by_explicit_normal_shutdown_seam(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        lifecycle.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(pid=1234),
    )
    spec = SimpleNamespace(
        label="com.harness.daemon.test",
        paths=lifecycle.paths_for(tmp_path / "run"),
    )
    lifecycle.schedule_cleanup_after_exit(spec, parent_pid=os.getpid(), uid=501)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert "cleanup-after-pid" in argv
    assert str(os.getpid()) in argv
    assert kwargs["start_new_session"] is True


def _sparse_runtime(tmp_path):
    return SimpleNamespace(
        runtime_root=tmp_path,
        build_id="b",
        config=SimpleNamespace(env={}, runtime_root=tmp_path, build_id="b"),
        adapter=None,
        executor=object(),
        tmux=object(),
    )


def test_recovery_boot_reuses_restart_reconciler_and_never_calls_full_genesis(tmp_path, monkeypatch):
    runtime = _sparse_runtime(tmp_path)
    calls = []
    monkeypatch.setattr(daemon, "acquire_instance_lock", lambda _root: calls.append("lock"))
    monkeypatch.setattr(
        daemon._genesis_mod,
        "write_runtime_json",
        lambda *_args, **_kwargs: calls.append("runtime"),
    )
    monkeypatch.setattr(
        daemon._genesis_mod,
        "run_genesis",
        lambda *_args, **_kwargs: pytest.fail("recovery must never run full genesis"),
    )
    monkeypatch.setattr(
        daemon._reconcile_mod,
        "reconcile_on_restart",
        lambda executor, tmux: calls.append(("reconcile", executor, tmux)),
    )

    daemon.boot(runtime, recover_only=True)
    assert calls == [
        "lock",
        "runtime",
        ("reconcile", runtime.executor, runtime.tmux),
    ]


def test_full_boot_keeps_existing_genesis_path(tmp_path, monkeypatch):
    runtime = _sparse_runtime(tmp_path)
    calls = []
    monkeypatch.setattr(daemon, "acquire_instance_lock", lambda _root: calls.append("lock"))
    monkeypatch.setattr(
        daemon._genesis_mod,
        "write_runtime_json",
        lambda *_args, **_kwargs: calls.append("runtime"),
    )
    monkeypatch.setattr(
        daemon._reconcile_mod,
        "reconcile_on_restart",
        lambda *_args, **_kwargs: pytest.fail("boot must delegate full routing to run_genesis"),
    )
    monkeypatch.setattr(
        daemon._genesis_mod,
        "run_genesis",
        lambda executor, tmux, cfg: calls.append(("genesis", executor, tmux, cfg)),
    )
    daemon.boot(runtime)
    assert calls == [
        "lock",
        "runtime",
        ("genesis", runtime.executor, runtime.tmux, runtime.config),
    ]


def test_poll_loop_checks_idle_only_after_completed_tick_and_returns(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        daemon,
        "poll_once",
        lambda executor, tmux, detector: calls.append(("poll", executor, tmux, detector)),
    )
    monkeypatch.setattr(daemon, "write_status", lambda _root: calls.append("status"))
    monkeypatch.setattr(
        daemon,
        "has_live_build",
        lambda: calls.append("live-read") or False,
    )
    monkeypatch.setattr(
        daemon.time,
        "sleep",
        lambda _seconds: pytest.fail("idle loop must return before sleeping"),
    )
    daemon.poll_loop(5, executor="executor", tmux="tmux", detector="detector")
    assert calls == [
        ("poll", "executor", "tmux", "detector"),
        "status",
        "live-read",
    ]


def test_run_claims_request_before_full_genesis_and_schedules_cleanup_only_on_idle(
    tmp_path, monkeypatch
):
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path,
        build_id="b",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )
    runtime = _sparse_runtime(tmp_path)
    calls = []

    class Listener:
        def close(self):
            calls.append("listener-close")

    monkeypatch.setattr(daemon, "_apply_global_seams", lambda _runtime: calls.append("seams"))
    monkeypatch.setattr(
        daemon,
        "acquire_instance_lock",
        lambda _root: calls.append("instance-lock"),
    )
    monkeypatch.setattr(
        daemon,
        "boot",
        lambda _runtime, *, recover_only=False: calls.append(("boot", recover_only)),
    )
    monkeypatch.setattr(daemon, "has_live_build", lambda: True)
    monkeypatch.setattr(daemon, "make_ipc_listener", lambda _root: Listener())
    monkeypatch.setattr(daemon._ipc_mod, "serve_forever", lambda _listener: None)
    monkeypatch.setattr(daemon, "poll_loop", lambda *_args, **_kwargs: calls.append("idle-return"))
    monkeypatch.setattr(daemon, "release_instance_lock", lambda: calls.append("unlock"))
    monkeypatch.setattr(
        daemon.lifecycle,
        "schedule_cleanup_after_exit",
        lambda _spec, *, parent_pid: calls.append(("cleanup", parent_pid)),
    )

    daemon.run(runtime, lifecycle_spec=spec)
    assert ("boot", False) in calls
    assert not spec.paths.request.exists()
    assert spec.paths.claimed_request.exists()
    assert calls[-2] == "unlock"
    assert calls[-1][0] == "cleanup"


def test_run_without_pending_request_is_recovery_only_and_crash_never_schedules_cleanup(
    tmp_path, monkeypatch
):
    spec = lifecycle.RunSpec(
        runtime_root=tmp_path.resolve(),
        build_id="b",
        label=lifecycle.launchd_label(tmp_path, "b"),
        request_id="already-consumed",
        paths=lifecycle.paths_for(tmp_path),
        repo_root=Path(__file__).resolve().parents[1],
        python_executable="/usr/bin/python3",
    )
    runtime = _sparse_runtime(tmp_path)
    calls = []
    monkeypatch.setattr(daemon, "_apply_global_seams", lambda _runtime: None)
    monkeypatch.setattr(daemon, "acquire_instance_lock", lambda _root: None)
    monkeypatch.setattr(
        daemon,
        "boot",
        lambda _runtime, *, recover_only=False: calls.append(("boot", recover_only)),
    )
    monkeypatch.setattr(daemon, "has_live_build", lambda: True)
    monkeypatch.setattr(daemon, "make_ipc_listener", lambda _root: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(daemon._ipc_mod, "serve_forever", lambda _listener: None)
    monkeypatch.setattr(
        daemon,
        "poll_loop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    monkeypatch.setattr(daemon, "release_instance_lock", lambda: calls.append("unlock"))
    monkeypatch.setattr(
        daemon.lifecycle,
        "schedule_cleanup_after_exit",
        lambda *_args, **_kwargs: pytest.fail("crash path must not schedule cleanup"),
    )

    with pytest.raises(RuntimeError, match="crash"):
        daemon.run(runtime, lifecycle_spec=spec)
    assert ("boot", True) in calls
    assert calls[-1] == "unlock"


def test_daemon_entry_assembles_intake_then_passes_fixed_lifecycle_spec(tmp_path, monkeypatch):
    review_panel_arms = [
        {
            "pattern": "forge-queue/queue-core#exec",
            "axes": ["composition-interface", "risk-readiness"],
        },
        {
            "pattern": "forge-queue/persistence#exec",
            "axes": ["broad"],
        },
    ]
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="build-entry",
        initial_intake="Build from the one-shot request.",
        fidelity_playback_authority="operator-delegate",
        fidelity_playback_delegate="synthetic-owner",
        fidelity_playback_delegation_reason="supervised synthetic-owner run",
        review_panel_arms=review_panel_arms,
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )
    built = []
    ran = []
    runtime = _sparse_runtime(spec.runtime_root)

    def build_runtime(**kwargs):
        built.append(kwargs)
        return runtime

    monkeypatch.setattr(commissioning, "build_runtime", build_runtime)
    monkeypatch.setattr(
        daemon,
        "run",
        lambda passed_runtime, *, lifecycle_spec: ran.append((passed_runtime, lifecycle_spec)),
    )

    code = daemon.main(
        [
            "--runtime-root",
            str(spec.runtime_root),
            "--build-id",
            spec.build_id,
            "--start-request",
            str(spec.paths.request),
            "--request-id",
            spec.request_id,
            "--launchd-label",
            spec.label,
            "--launchd-plist",
            str(spec.paths.plist),
        ]
    )
    assert code == 0
    assert built == [
        {
            "runtime_root": spec.runtime_root,
            "build_id": spec.build_id,
            "initial_intake": "Build from the one-shot request.",
            "fidelity_playback_authority": "operator-delegate",
            "fidelity_playback_delegate": "synthetic-owner",
            "fidelity_playback_delegation_reason": (
                "supervised synthetic-owner run"
            ),
            "review_panel_arms": review_panel_arms,
        }
    ]
    assert ran[0][0] is runtime
    passed_spec = ran[0][1]
    assert passed_spec.label == spec.label
    assert passed_spec.request_id == spec.request_id
    assert passed_spec.paths.request == spec.paths.request


def test_daemon_entry_refuses_invalid_delegation_request_before_runtime_assembly(
    tmp_path, monkeypatch
):
    spec = lifecycle.prepare_run(
        runtime_root=tmp_path / "run",
        build_id="invalid-delegate",
        ipc_probe=lambda _path: False,
        lock_probe=lambda _path: False,
        launchctl_runner=lambda _args: _completed(returncode=1),
    )
    payload = json.loads(spec.paths.request.read_text(encoding="utf-8"))
    payload["fidelity_playback_authority"] = "operator-delegate"
    payload["fidelity_playback_delegate"] = None
    payload["fidelity_playback_delegation_reason"] = None
    spec.paths.request.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        commissioning,
        "build_runtime",
        lambda **_kwargs: pytest.fail(
            "invalid request must refuse before runtime assembly"
        ),
    )

    with pytest.raises(lifecycle.LifecycleError) as raised:
        daemon.main(
            [
                "--runtime-root",
                str(spec.runtime_root),
                "--build-id",
                spec.build_id,
                "--start-request",
                str(spec.paths.request),
                "--request-id",
                spec.request_id,
                "--launchd-label",
                spec.label,
                "--launchd-plist",
                str(spec.paths.plist),
            ]
        )

    assert raised.value.error_code == "fidelity_playback_declaration_invalid"
    assert "requires both" in str(raised.value)
