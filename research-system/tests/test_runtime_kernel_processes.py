"""Deterministic direct proofs for the normal B1 fenced process slice."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from threading import Event
import time
from uuid import uuid4

import pytest

from conftest import Sandbox
from ht.errors import HtError
from ht.paths import Root
from ht.runtime.custody import (
    audit_lock,
    create_custody,
    custody_is_free,
    try_instance_lock,
    validate_inherited,
)
import ht.runtime.daemon as runtime_daemon
import ht.commands.runtime as runtime_commands
import ht.runtime.synthetic_helper as runtime_helper
import ht.runtime.wrapper as runtime_wrapper
from ht.runtime.launcher import (
    BARRIER_BYTES,
    child_options,
    daemon_argv,
    helper_argv,
    load_session_context,
    wrapper_argv,
)
from ht.runtime.schema import canonical_json_bytes, strict_loads, validate
from ht.runtime.views import read_state_unlocked, status as runtime_status


def _mint_issue(sandbox: Sandbox) -> None:
    result = sandbox.run(
        "issue",
        "mint",
        "--title",
        "SYNTHETIC-PROCESS-0",
        "--question",
        "SYNTHETIC-PROCESS-1",
        "--done-definition",
        "SYNTHETIC-PROCESS-2",
        "--provenance",
        "user-seed#synthetic-process-0",
        "--lanes",
        "L4",
        role="pc",
    )
    assert result.returncode == 0, result.stderr


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, int, bytes | None], ...]:
    rows = []
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        payload = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        rows.append((relative, info.st_mode, info.st_mtime_ns, payload))
    return tuple(rows)


class _UnobservedWrapper:
    pass


def _accepted_context(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict, dict, str, int]:
    """Create a durable starting/accepted claim without creating a wrapper."""

    _mint_issue(sandbox)
    assert sandbox.run("runtime", "init", "--json").returncode == 0
    submitted = sandbox.run("runtime", "request", "--work-ref", "issue#I-1", "--json")
    assert submitted.returncode == 0, submitted.stderr
    request_id = json.loads(submitted.stdout)["request_id"]
    estate = sandbox.root / "var" / "runtime"
    wrappers: dict[str, object] = {}
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", lambda *_args, **_kwargs: _UnobservedWrapper())
    with audit_lock(estate / ".harnessd.lock", exclusive=True):
        descriptor = json.loads((estate / "runtime.json").read_text())
        state = runtime_daemon._boot_state(estate, descriptor["runtime_id"])
        state = runtime_daemon._append(
            estate,
            state,
            "daemon_started",
            daemon_incarnation_id=str(uuid4()),
        )
        request, request_bytes = runtime_daemon._load_request(
            estate / "requests" / f"{request_id}.json"
        )
        state = runtime_daemon._claim_request(
            sandbox.root,
            estate,
            state,
            request,
            request_bytes,
            wrappers,
        )
    binding = next(
        value
        for key, value in state.bindings.items()
        if key != "runtime#kernel" and value["request_id"] == request_id
    )
    session_id = binding["current_session_id"]
    session = estate / "sessions" / session_id
    packet, launch, packet_sha = load_session_context(sandbox.root, session_id)[1:]
    custody_fd = create_custody(session / "custody.lock")
    return session, packet, launch, packet_sha, custody_fd


def _terminalize_crashed(sandbox: Sandbox, packet: dict, custody_fd: int) -> str:
    estate = sandbox.root / "var" / "runtime"
    os.close(custody_fd)
    with audit_lock(estate / ".harnessd.lock", exclusive=True):
        _estate, _descriptor, state = read_state_unlocked(sandbox.root)
        work = next(
            value
            for key, value in state.bindings.items()
            if key != "runtime#kernel" and value["request_id"] == packet["request_id"]
        )
        state = runtime_daemon._append(
            estate,
            state,
            "session_terminal",
            request_id=work["request_id"],
            binding_id=work["binding_id"],
            lease_epoch=packet["lease_epoch"],
            session_id=packet["session_id"],
            outcome="crashed",
            reason_code="custody-free-incomplete-closure",
            started_sha256=None,
            ready_sha256=None,
            result_sha256=None,
            terminal_sha256=None,
            process_exit_sha256=None,
        )
    assert state.bindings[work["node_address"]]["terminal_outcome"] == "crashed"
    return packet["request_id"]


def _exclusive_trace(monkeypatch: pytest.MonkeyPatch, module: object):
    original = getattr(module, "audit_lock")
    entered = Event()
    acquired = Event()

    @contextmanager
    def traced(path: Path, *, exclusive: bool):
        assert exclusive is True
        entered.set()
        with original(path, exclusive=exclusive):
            acquired.set()
            yield

    monkeypatch.setattr(module, "audit_lock", traced)
    return original, entered, acquired


def _assert_failed_boot_unchanged(root: Path, estate: Path) -> None:
    before = _tree_snapshot(estate)
    read_fd, write_fd = os.pipe()
    assert runtime_daemon.run(root, write_fd) == 2
    readiness = strict_loads(os.read(read_fd, 4096), label="failed readiness")
    os.close(read_fd)
    assert readiness == {
        "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
        "status": "failed",
        "reason_code": "recovery-failed",
    }
    assert _tree_snapshot(estate) == before


def test_fixed_process_vectors_and_empty_environment(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    session_id = str(uuid4())
    vectors = (
        daemon_argv(root, 9),
        wrapper_argv(root, session_id, 10),
        helper_argv(root, session_id, 10, 11),
    )
    for argv in vectors:
        assert Path(argv[0]).is_absolute()
        assert argv[1:5] == ["-I", "-X", "utf8", "-B"]
        assert Path(argv[5]).is_absolute()
        assert all("SENTINEL" not in argument for argument in argv)
    options = child_options(root, (9,))
    assert options == {
        "env": {},
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "pass_fds": (9,),
        "close_fds": True,
    }


def test_same_open_description_custody_and_helper_start_barrier(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = sandbox.root
    session, packet, launch, packet_sha, custody_fd = _accepted_context(
        sandbox, monkeypatch
    )
    duplicate_fd = os.dup(custody_fd)
    validate_inherited(duplicate_fd, session / "custody.lock")
    assert not custody_is_free(session / "custody.lock")

    barrier_read, barrier_write = os.pipe()
    helper = subprocess.Popen(
        helper_argv(root, packet["session_id"], custody_fd, barrier_read),
        **child_options(root, (custody_fd, barrier_read)),
    )
    os.close(barrier_read)
    time.sleep(0.1)
    assert not (session / "ready.json").exists()
    os.write(barrier_write, BARRIER_BYTES)
    os.close(barrier_write)
    assert helper.wait(timeout=10) == 0
    ready = strict_loads((session / "ready.json").read_bytes(), label="ready")
    result = strict_loads((session / "result.json").read_bytes(), label="result")
    terminal = strict_loads((session / "terminal.json").read_bytes(), label="terminal")
    for name, value in (("ready", ready), ("result", result), ("terminal", terminal)):
        validate(
            {
                "ready": "ready-receipt.schema.json",
                "result": "result.schema.json",
                "terminal": "terminal-receipt.schema.json",
            }[name],
            value,
        )
        assert value["session_id"] == packet["session_id"]
        assert value["packet_sha256"] == packet_sha == launch["packet_sha256"]
    assert terminal["result_sha256"] == hashlib.sha256(
        (session / "result.json").read_bytes()
    ).hexdigest()
    os.close(custody_fd)
    assert not custody_is_free(session / "custody.lock")
    os.close(duplicate_fd)
    assert custody_is_free(session / "custody.lock")


def test_wrapper_publishes_started_before_release_and_exact_exit_receipt(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = sandbox.root
    session, packet, _launch, packet_sha, custody_fd = _accepted_context(
        sandbox, monkeypatch
    )
    wrapper = subprocess.Popen(
        wrapper_argv(root, packet["session_id"], custody_fd),
        **child_options(root, (custody_fd,)),
    )
    os.close(custody_fd)
    assert wrapper.wait(timeout=10) == 0
    started_bytes = (session / "started.json").read_bytes()
    started = strict_loads(started_bytes, label="started")
    process_exit = strict_loads((session / "process-exit.json").read_bytes(), label="exit")
    validate("started-receipt.schema.json", started)
    validate("process-exit-receipt.schema.json", process_exit)
    assert started["wrapper_pid"] == process_exit["wrapper_pid"] == wrapper.pid
    assert started["helper_pid"] == process_exit["helper_pid"]
    assert process_exit["wait_status"] == 0
    assert process_exit["packet_sha256"] == packet_sha
    assert process_exit["result_sha256"] == hashlib.sha256(
        (session / "result.json").read_bytes()
    ).hexdigest()
    assert process_exit["terminal_sha256"] == hashlib.sha256(
        (session / "terminal.json").read_bytes()
    ).hexdigest()
    assert custody_is_free(session / "custody.lock")


def test_helper_waits_for_exact_barrier_eof_before_missing_context_failure(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, packet, _launch, _packet_sha, custody_fd = _accepted_context(
        sandbox, monkeypatch
    )
    (session / "packet.json").unlink()
    barrier_read, barrier_write = os.pipe()
    helper = subprocess.Popen(
        helper_argv(sandbox.root, packet["session_id"], custody_fd, barrier_read),
        **child_options(sandbox.root, (custody_fd, barrier_read)),
    )
    os.close(barrier_read)
    with pytest.raises(subprocess.TimeoutExpired):
        helper.wait(timeout=0.2)
    assert not (session / "ready.json").exists()
    os.write(barrier_write, BARRIER_BYTES)
    os.close(barrier_write)
    assert helper.wait(timeout=10) == 2
    assert not (session / "ready.json").exists()
    os.close(custody_fd)


def test_external_shared_lock_blocks_request_publication(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mint_issue(sandbox)
    assert sandbox.run("runtime", "init", "--json").returncode == 0
    estate = sandbox.root / "var" / "runtime"
    original, entered, acquired = _exclusive_trace(monkeypatch, runtime_commands)
    monkeypatch.setattr(runtime_commands, "_emit", lambda *_args, **_kwargs: 0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original(estate / ".harnessd.lock", exclusive=False):
            future = pool.submit(
                runtime_commands.request,
                Root(sandbox.root),
                "issue#I-1",
                as_json=True,
            )
            assert entered.wait(timeout=10)
            assert not acquired.is_set()
            assert list((estate / "requests").iterdir()) == []
        assert future.result(timeout=10) == 0
    assert acquired.is_set()
    assert len(list((estate / "requests").iterdir())) == 1


def test_external_shared_lock_blocks_retry_publication(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session, packet, _launch, _packet_sha, custody_fd = _accepted_context(
        sandbox, monkeypatch
    )
    request_id = _terminalize_crashed(sandbox, packet, custody_fd)
    estate = sandbox.root / "var" / "runtime"
    before = {path.name for path in (estate / "requests").iterdir()}
    original, entered, acquired = _exclusive_trace(monkeypatch, runtime_commands)
    monkeypatch.setattr(runtime_commands, "_emit", lambda *_args, **_kwargs: 0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original(estate / ".harnessd.lock", exclusive=False):
            future = pool.submit(
                runtime_commands.retry,
                Root(sandbox.root),
                request_id,
                as_json=True,
            )
            assert entered.wait(timeout=10)
            assert not acquired.is_set()
            assert {path.name for path in (estate / "requests").iterdir()} == before
        assert future.result(timeout=10) == 0
    assert acquired.is_set()
    assert len(list((estate / "requests").iterdir())) == len(before) + 1


def test_external_shared_lock_blocks_stop_publication(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session, _packet, _launch, _packet_sha, custody_fd = _accepted_context(
        sandbox, monkeypatch
    )
    os.close(custody_fd)
    estate = sandbox.root / "var" / "runtime"
    original, entered, acquired = _exclusive_trace(monkeypatch, runtime_commands)
    monkeypatch.setattr(runtime_commands, "_emit", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        runtime_commands,
        "control_response_view",
        lambda _root, control_id: {
            "schema_version": "hypothesis-tree-runtime-control-response/1.0.0",
            "status": "accepted",
            "control_id": control_id,
            "target_daemon_incarnation_id": str(uuid4()),
        },
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original(estate / ".harnessd.lock", exclusive=False):
            future = pool.submit(runtime_commands.stop, Root(sandbox.root), as_json=True)
            assert entered.wait(timeout=10)
            assert not acquired.is_set()
            assert list((estate / "control" / "requests").iterdir()) == []
        assert future.result(timeout=10) == 0
    assert acquired.is_set()
    assert len(list((estate / "control" / "requests").iterdir())) == 1


def test_external_shared_lock_blocks_wrapper_started_publication(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, packet, _launch, _packet_sha, custody_fd = _accepted_context(
        sandbox, monkeypatch
    )
    estate = sandbox.root / "var" / "runtime"
    original, entered, acquired = _exclusive_trace(monkeypatch, runtime_wrapper)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original(estate / ".harnessd.lock", exclusive=False):
            future = pool.submit(
                runtime_wrapper.run,
                sandbox.root,
                packet["session_id"],
                custody_fd,
            )
            assert entered.wait(timeout=10)
            assert not acquired.is_set()
            assert not (session / "started.json").exists()
        assert future.result(timeout=20) == 0
    assert acquired.is_set()
    assert (session / "started.json").is_file()
    os.close(custody_fd)


def test_external_shared_lock_blocks_helper_receipt_publication(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, packet, _launch, _packet_sha, custody_fd = _accepted_context(
        sandbox, monkeypatch
    )
    estate = sandbox.root / "var" / "runtime"
    barrier_read, barrier_write = os.pipe()
    os.write(barrier_write, BARRIER_BYTES)
    os.close(barrier_write)
    original, entered, acquired = _exclusive_trace(monkeypatch, runtime_helper)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original(estate / ".harnessd.lock", exclusive=False):
            future = pool.submit(
                runtime_helper.run,
                sandbox.root,
                packet["session_id"],
                custody_fd,
                barrier_read,
            )
            assert entered.wait(timeout=10)
            assert not acquired.is_set()
            assert not (session / "ready.json").exists()
        assert future.result(timeout=10) == 0
    assert acquired.is_set()
    assert (session / "ready.json").is_file()
    os.close(custody_fd)


def test_request_final_under_lock_revalidation_rejects_drift_without_publication(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mint_issue(sandbox)
    assert sandbox.run("runtime", "init", "--json").returncode == 0
    estate = sandbox.root / "var" / "runtime"
    issue = sandbox.root / "tier1" / "issues" / "I-1.json"
    original = runtime_commands.revalidate_work

    def drift_then_revalidate(root: Root, expected):
        issue.write_bytes(issue.read_bytes() + b" ")
        return original(root, expected)

    monkeypatch.setattr(runtime_commands, "revalidate_work", drift_then_revalidate)
    with pytest.raises(HtError):
        runtime_commands.request(Root(sandbox.root), "issue#I-1", as_json=True)
    assert list((estate / "requests").iterdir()) == []


def test_retry_rereads_lineage_and_revalidates_under_lock_before_publication(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session, packet, _launch, _packet_sha, custody_fd = _accepted_context(
        sandbox, monkeypatch
    )
    request_id = _terminalize_crashed(sandbox, packet, custody_fd)
    estate = sandbox.root / "var" / "runtime"
    before = _tree_snapshot(estate / "requests")
    issue = sandbox.root / "tier1" / "issues" / "I-1.json"
    original = runtime_commands.revalidate_work

    def drift_then_revalidate(root: Root, expected):
        issue.write_bytes(issue.read_bytes() + b" ")
        return original(root, expected)

    monkeypatch.setattr(runtime_commands, "revalidate_work", drift_then_revalidate)
    with pytest.raises(HtError):
        runtime_commands.retry(Root(sandbox.root), request_id, as_json=True)
    assert _tree_snapshot(estate / "requests") == before


@pytest.mark.parametrize(
    "unexpected_name",
    (
        "rogue",
        ".ht-publish-runtime.json-00000000-0000-0000-0000-000000000001",
        ".ht-replace-binding-ledger.json-00000000-0000-0000-0000-000000000001",
    ),
)
def test_unexpected_top_entry_rejects_boot_read_and_mutation_unchanged(
    sandbox: Sandbox,
    unexpected_name: str,
) -> None:
    _mint_issue(sandbox)
    assert sandbox.run("runtime", "init", "--json").returncode == 0
    estate = sandbox.root / "var" / "runtime"
    (estate / unexpected_name).write_bytes(b"")
    os.chmod(estate / unexpected_name, 0o600)
    before = _tree_snapshot(estate)
    _assert_failed_boot_unchanged(sandbox.root, estate)
    with pytest.raises(HtError):
        runtime_status(sandbox.root)
    assert _tree_snapshot(estate) == before
    with pytest.raises(HtError):
        runtime_commands.request(Root(sandbox.root), "issue#I-1", as_json=True)
    assert _tree_snapshot(estate) == before


def test_unexpected_session_entry_rejects_boot_read_and_mutation_unchanged(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _packet, _launch, _packet_sha, custody_fd = _accepted_context(
        sandbox, monkeypatch
    )
    os.close(custody_fd)
    unexpected = session / "rogue.json"
    unexpected.write_bytes(b"{}\n")
    os.chmod(unexpected, 0o600)
    estate = sandbox.root / "var" / "runtime"
    before = _tree_snapshot(estate)
    _assert_failed_boot_unchanged(sandbox.root, estate)
    with pytest.raises(HtError):
        runtime_status(sandbox.root)
    assert _tree_snapshot(estate) == before
    with pytest.raises(HtError):
        runtime_commands.request(Root(sandbox.root), "issue#I-1", as_json=True)
    assert _tree_snapshot(estate) == before


def test_losing_daemon_contender_changes_no_runtime_bytes_or_mtimes(sandbox: Sandbox) -> None:
    init = sandbox.run("runtime", "init", "--json")
    assert init.returncode == 0, init.stderr
    estate = sandbox.root / "var" / "runtime"
    owner_fd = try_instance_lock(estate / ".ht-runtime.instance.lock")
    assert owner_fd is not None
    before = _tree_snapshot(estate)
    read_fd, write_fd = os.pipe()
    try:
        assert runtime_daemon.run(sandbox.root, write_fd) == 0
        readiness = strict_loads(os.read(read_fd, 4096), label="readiness")
        assert readiness == {
            "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
            "status": "already-running",
        }
    finally:
        os.close(read_fd)
        os.close(owner_fd)
    assert _tree_snapshot(estate) == before


@pytest.mark.parametrize(
    ("operation", "target"),
    (
        ("publish", "runtime.json"),
        ("publish", "binding-ledger.json"),
        ("publish", "run-ledger.jsonl"),
        ("publish", "checkpoint.json"),
        ("publish", ".harnessd.lock"),
        ("publish", ".ht-runtime.instance.lock"),
        ("replace", "binding-ledger.json"),
        ("replace", "run-ledger.jsonl"),
        ("replace", "checkpoint.json"),
    ),
)
def test_losing_contender_ignores_each_exact_live_top_atomic_temp(
    sandbox: Sandbox,
    operation: str,
    target: str,
) -> None:
    init = sandbox.run("runtime", "init", "--json")
    assert init.returncode == 0, init.stderr
    estate = sandbox.root / "var" / "runtime"
    owner_fd = try_instance_lock(estate / ".ht-runtime.instance.lock")
    assert owner_fd is not None
    temporary = estate / f".ht-{operation}-{target}-{uuid4()}"
    temporary.write_bytes(b"in-flight-owner-bytes")
    os.chmod(temporary, 0o600)
    before = _tree_snapshot(estate)
    read_fd, write_fd = os.pipe()
    try:
        assert runtime_daemon.run(sandbox.root, write_fd) == 0
        readiness = strict_loads(os.read(read_fd, 4096), label="readiness")
        assert readiness == {
            "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
            "status": "already-running",
        }
    finally:
        os.close(read_fd)
        os.close(owner_fd)
    assert _tree_snapshot(estate) == before


@pytest.mark.parametrize(
    ("operation", "target"),
    (
        ("publish", "runtime.json"),
        ("publish", "binding-ledger.json"),
        ("publish", "run-ledger.jsonl"),
        ("publish", "checkpoint.json"),
        ("publish", ".harnessd.lock"),
        ("publish", ".ht-runtime.instance.lock"),
        ("replace", "binding-ledger.json"),
        ("replace", "run-ledger.jsonl"),
        ("replace", "checkpoint.json"),
    ),
)
def test_losing_contender_rejects_non_uuid4_top_atomic_temp_unchanged(
    sandbox: Sandbox,
    operation: str,
    target: str,
) -> None:
    init = sandbox.run("runtime", "init", "--json")
    assert init.returncode == 0, init.stderr
    estate = sandbox.root / "var" / "runtime"
    owner_fd = try_instance_lock(estate / ".ht-runtime.instance.lock")
    assert owner_fd is not None
    invalid_suffixes = (
        "00000000-0000-0000-0000-000000000000",  # nil UUID
        "f47ac10b-58cc-1372-a447-001122334455",  # version 1
        "f47ac10b-58cc-4372-0447-001122334455",  # non-RFC-4122 variant
        "F47AC10B-58CC-4372-A447-001122334455",  # noncanonical uppercase
        "f47ac10b58cc4372a447001122334455",  # noncanonical compact form
    )
    try:
        for suffix in invalid_suffixes:
            unexpected = estate / f".ht-{operation}-{target}-{suffix}"
            unexpected.write_bytes(b"rogue")
            os.chmod(unexpected, 0o600)
            before = _tree_snapshot(estate)
            read_fd, write_fd = os.pipe()
            try:
                assert runtime_daemon.run(sandbox.root, write_fd) == 2
                readiness = strict_loads(os.read(read_fd, 4096), label="readiness")
                assert readiness == {
                    "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
                    "status": "failed",
                    "reason_code": "recovery-failed",
                }
            finally:
                os.close(read_fd)
            assert _tree_snapshot(estate) == before
            unexpected.unlink()
    finally:
        os.close(owner_fd)


@pytest.mark.parametrize(
    "unexpected_name",
    (
        ".ht-replace-binding-ledger.json-not-a-uuid",
        ".ht-replace-runtime.json-00000000-0000-0000-0000-000000000001",
        ".ht-publish-unknown.json-00000000-0000-0000-0000-000000000001",
    ),
)
def test_losing_contender_rejects_near_miss_or_rogue_hidden_temp_unchanged(
    sandbox: Sandbox,
    unexpected_name: str,
) -> None:
    init = sandbox.run("runtime", "init", "--json")
    assert init.returncode == 0, init.stderr
    estate = sandbox.root / "var" / "runtime"
    owner_fd = try_instance_lock(estate / ".ht-runtime.instance.lock")
    assert owner_fd is not None
    unexpected = estate / unexpected_name
    unexpected.write_bytes(b"rogue")
    os.chmod(unexpected, 0o600)
    before = _tree_snapshot(estate)
    read_fd, write_fd = os.pipe()
    try:
        assert runtime_daemon.run(sandbox.root, write_fd) == 2
        readiness = strict_loads(os.read(read_fd, 4096), label="readiness")
        assert readiness == {
            "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
            "status": "failed",
            "reason_code": "recovery-failed",
        }
    finally:
        os.close(read_fd)
        os.close(owner_fd)
    assert _tree_snapshot(estate) == before


def test_losing_contender_still_rejects_wrong_static_mode_unchanged(
    sandbox: Sandbox,
) -> None:
    init = sandbox.run("runtime", "init", "--json")
    assert init.returncode == 0, init.stderr
    estate = sandbox.root / "var" / "runtime"
    owner_fd = try_instance_lock(estate / ".ht-runtime.instance.lock")
    assert owner_fd is not None
    os.chmod(estate / "runtime.json", 0o640)
    before = _tree_snapshot(estate)
    read_fd, write_fd = os.pipe()
    try:
        assert runtime_daemon.run(sandbox.root, write_fd) == 2
        readiness = strict_loads(os.read(read_fd, 4096), label="readiness")
        assert readiness == {
            "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
            "status": "failed",
            "reason_code": "recovery-failed",
        }
    finally:
        os.close(read_fd)
        os.close(owner_fd)
    assert _tree_snapshot(estate) == before


def test_response_is_durable_before_wrapper_popen(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mint_issue(sandbox)
    init = sandbox.run("runtime", "init", "--json")
    assert init.returncode == 0, init.stderr
    submitted = sandbox.run("runtime", "request", "--work-ref", "issue#I-1", "--json")
    assert submitted.returncode == 0, submitted.stderr
    request_id = json.loads(submitted.stdout)["request_id"]
    estate = sandbox.root / "var" / "runtime"
    observed: list[tuple[list[str], dict]] = []

    def fail_after_barrier(argv: list[str], **options: object) -> None:
        response_path = estate / "responses" / f"{request_id}.json"
        response = strict_loads(response_path.read_bytes(), label="accepted response")
        assert response["status"] == "accepted"
        assert response["recovery_created"] is False
        observed.append((argv, dict(options)))
        raise OSError("synthetic spawn failure after barrier")

    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", fail_after_barrier)
    with audit_lock(estate / ".harnessd.lock", exclusive=True):
        descriptor = json.loads((estate / "runtime.json").read_text())
        state = runtime_daemon._boot_state(estate, descriptor["runtime_id"])
        state = runtime_daemon._append(
            estate, state, "daemon_started", daemon_incarnation_id=str(uuid4())
        )
        request, request_bytes = runtime_daemon._load_request(
            estate / "requests" / f"{request_id}.json"
        )
        state = runtime_daemon._claim_request(
            sandbox.root, estate, state, request, request_bytes, {}
        )
    assert len(observed) == 1
    argv, options = observed[0]
    assert argv[1:5] == ["-I", "-X", "utf8", "-B"]
    assert options["env"] == {}
    binding = next(
        value
        for key, value in state.bindings.items()
        if key != "runtime#kernel" and value["request_id"] == request_id
    )
    assert binding["admission_status"] == "accepted"
    assert binding["terminal_outcome"] == "crashed"
    assert binding["sessions"][binding["current_session_id"]]["terminal_reason_code"] == "popen-failed"
