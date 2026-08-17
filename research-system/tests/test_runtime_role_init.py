"""Direct Stage-A proofs for the B1-to-B2 role capability transition."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Iterator
from uuid import uuid4

import pytest

from conftest import Sandbox
from ht import mutex
from ht.commands import role as role_commands
from ht.paths import Root
from ht.runtime.atomic import (
    make_directory,
    publish_immutable,
    read_exact_file,
    replace_file,
)
from ht.runtime.capability import (
    CAPABILITY_FILE,
    EXPECTED_ROLE_DIRECTORIES,
    INITIALIZATION_FILE,
)
from ht.runtime.custody import audit_lock, ensure_custody_file, try_instance_lock
from ht.runtime.launcher import construct_claim
from ht.runtime.replay import build_record, publish_projections, replay
from ht.runtime.repository import snapshot_work
from ht.runtime.schema import canonical_json_bytes, strict_loads
from ht.runtime.state import derive_dedup_key
from ht.runtime.views import read_state_unlocked
from ht.runtime.wal import frame_record, parse_bytes


_V1_FIELDS = (
    "runtime_id",
    "last_seq",
    "clean_wal_sha256",
    "daemon_incarnation_id",
    "request_index",
    "dedup_index",
    "session_index",
    "control_index",
    "bindings",
    "binding_ledger_sha256",
    "final_tail",
)
_V2_ADDITIONS = {
    "role_capability_sha256",
    "upgrade_base_seq",
    "upgrade_base_clean_wal_sha256",
    "upgrade_base_checkpoint_sha256",
    "upgrade_base_binding_ledger_sha256",
}
_FAULT_STAGES = (
    "hidden-published",
    *(
        stage
        for relative in EXPECTED_ROLE_DIRECTORIES
        for stage in (
            f"directory:{relative}:created",
            f"directory:{relative}:fsynced",
        )
    ),
    "checkpoint-replaced",
    "public-published",
    "validated-under-hidden",
    "hidden-unlinked",
    "runtime-fsynced",
)


def _runtime(sandbox: Sandbox) -> Path:
    result = sandbox.run("runtime", "init", "--json")
    assert result.returncode == 0, result.stderr
    return sandbox.root / "var/runtime"


def _identity(path: Path) -> tuple[int, int, int, int, bytes]:
    info = path.lstat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        path.read_bytes(),
    )


def _tree_identity(path: Path) -> tuple[tuple[str, int, int, int, int, bytes | None], ...]:
    rows = []
    for candidate in (path, *sorted(path.rglob("*"))):
        info = candidate.lstat()
        relative = "." if candidate == path else candidate.relative_to(path).as_posix()
        rows.append(
            (
                relative,
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_nlink,
                candidate.read_bytes() if stat.S_ISREG(info.st_mode) else None,
            )
        )
    return tuple(rows)


def _estate_identity(path: Path) -> tuple[tuple[str, int, int, int, int, bytes | None], ...]:
    return _tree_identity(path)


def _documents(estate: Path, *, created_at: str = "2026-07-15T00:00:00.000000Z"):
    descriptor = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime")
    return role_commands._build_activation_documents(
        runtime_id=descriptor["runtime_id"],
        checkpoint_bytes=read_exact_file(estate / "checkpoint.json"),
        binding_ledger_bytes=read_exact_file(estate / "binding-ledger.json"),
        created_at=created_at,
    )


def _write_exact(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.write_bytes(data)
    os.chmod(path, mode)


def _mkdir_prefix(estate: Path, count: int) -> None:
    for relative in EXPECTED_ROLE_DIRECTORIES[:count]:
        path = estate / relative
        if not path.exists():
            path.mkdir()
            os.chmod(path, 0o700)


def _mint_issue(sandbox: Sandbox) -> None:
    result = sandbox.run(
        "issue",
        "mint",
        "--title",
        "ROLE-INIT-TERMINAL-HISTORY",
        "--question",
        "Can activation preserve this terminal B1 history?",
        "--done-definition",
        "The exact history remains byte-identical",
        "--provenance",
        "user-seed#role-init-terminal-history",
        "--lanes",
        "L4",
        role="pc",
    )
    assert result.returncode == 0, result.stderr


def _terminal_history(sandbox: Sandbox) -> Path:
    _mint_issue(sandbox)
    estate = _runtime(sandbox)
    started = sandbox.run("runtime", "start", "--background", "--json")
    assert started.returncode == 0, started.stderr
    stopped = False
    try:
        submitted = sandbox.run(
            "runtime",
            "request",
            "--work-ref",
            "issue#I-1",
            "--json",
        )
        assert submitted.returncode == 0, submitted.stderr
        request_id = json.loads(submitted.stdout)["request_id"]
        waited = sandbox.run(
            "runtime",
            "wait",
            request_id,
            "--timeout",
            "30",
            "--json",
        )
        assert waited.returncode == 0, waited.stderr
        assert json.loads(waited.stdout)["outcome"] == "SUCCEEDED"
        stopped_result = sandbox.run("runtime", "stop", "--json")
        assert stopped_result.returncode == 0, stopped_result.stderr
        stopped = True
    finally:
        if not stopped:
            sandbox.run("runtime", "stop", "--json")
    return estate


@contextmanager
def _separate_shared_holder(path: Path) -> Iterator[None]:
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - parent checks the process result
        try:
            os.close(ready_read)
            os.close(release_write)
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            fcntl.flock(fd, fcntl.LOCK_SH)
            os.write(ready_write, b"R")
            os.read(release_read, 1)
            os.close(fd)
            os._exit(0)
        except BaseException:
            os._exit(91)
    os.close(ready_write)
    os.close(release_read)
    try:
        assert os.read(ready_read, 1) == b"R"
        yield
    finally:
        os.close(ready_read)
        os.write(release_write, b"X")
        os.close(release_write)
        waited, status = os.waitpid(pid, 0)
        assert waited == pid and os.waitstatus_to_exitcode(status) == 0


def _append_event(wal: bytes, state, event: str, **fields: object):
    wal += frame_record(
        build_record(state, event, "2026-07-15T00:00:00Z", **fields)
    )
    return wal, replay(parse_bytes(wal), state.runtime_id)


def _stopped_session_state(sandbox: Sandbox, *, abandoned: bool) -> Path:
    _mint_issue(sandbox)
    estate = _runtime(sandbox)
    descriptor = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime")
    runtime_id = descriptor["runtime_id"]
    work = snapshot_work(Root(sandbox.root), "issue#I-1").as_dict()
    request = {
        "schema_version": "hypothesis-tree-runtime-request/1.0.0",
        "request_id": "00000000-0000-4000-8000-000000000101",
        "request_created_at": "2026-07-15T00:00:00Z",
        "role": "synthetic-kernel-v1",
        "attempt": 1,
        "retry_lineage": [],
        "work": work,
    }
    request_bytes = canonical_json_bytes(request)
    publish_immutable(
        estate / "requests" / f"{request['request_id']}.json",
        request_bytes,
    )
    base = replay(parse_bytes(b""), runtime_id)
    state = base
    wal = b""
    daemon_id = "00000000-0000-4000-8000-000000000102"
    binding_id = "00000000-0000-4000-8000-000000000103"
    session_id = "00000000-0000-4000-8000-000000000104"
    wal, state = _append_event(
        wal,
        state,
        "daemon_started",
        daemon_incarnation_id=daemon_id,
    )
    wal, state = _append_event(
        wal,
        state,
        "work_planned",
        request_id=request["request_id"],
        request_sha256=hashlib.sha256(request_bytes).hexdigest(),
        binding_id=binding_id,
        dedup_key=derive_dedup_key(request),
        request=request,
    )
    packet, packet_bytes, packet_sha, launch, launch_bytes, launch_sha = construct_claim(
        runtime_id=runtime_id,
        request=request,
        binding_id=binding_id,
        lease_epoch=1,
        session_id=session_id,
        admission_repository_commit=work["submission_repository_commit"],
        wrapper_instance_id="00000000-0000-4000-8000-000000000105",
        helper_instance_id="00000000-0000-4000-8000-000000000106",
        packet_created_at="2026-07-15T00:00:00Z",
    )
    wal, state = _append_event(
        wal,
        state,
        "work_claimed",
        request_id=request["request_id"],
        binding_id=binding_id,
        lease_epoch=1,
        session_id=session_id,
        admission_repository_commit=work["submission_repository_commit"],
        packet=packet,
        packet_canonical_json=packet_bytes.decode(),
        packet_sha256=packet_sha,
        launch=launch,
        launch_canonical_json=launch_bytes.decode(),
        launch_sha256=launch_sha,
    )
    if abandoned:
        wal, state = _append_event(
            wal,
            state,
            "claim_rolled_back",
            request_id=request["request_id"],
            binding_id=binding_id,
            lease_epoch=1,
            session_id=session_id,
            reason_code="boot-recovery-pre-start",
        )
    control_id = "00000000-0000-4000-8000-000000000107"
    control = {
        "schema_version": "hypothesis-tree-runtime-control-request/1.0.0",
        "control_id": control_id,
        "control_created_at": "2026-07-15T00:00:00Z",
        "target_daemon_incarnation_id": daemon_id,
        "operation": "stop",
    }
    publish_immutable(
        estate / "control/requests" / f"{control_id}.json",
        canonical_json_bytes(control),
    )
    wal, state = _append_event(
        wal,
        state,
        "control_stop_accepted",
        control_id=control_id,
        target_daemon_incarnation_id=daemon_id,
    )
    wal, state = _append_event(
        wal,
        state,
        "daemon_stopped",
        daemon_incarnation_id=daemon_id,
        control_id=control_id,
    )
    replace_file(estate / "run-ledger.jsonl", wal, expected_old=b"")
    publish_projections(estate, state, allowed_prior=(base, state))
    if abandoned:
        session_path = estate / "sessions" / session_id
        make_directory(session_path)
        publish_immutable(session_path / "packet.json", packet_bytes)
        publish_immutable(session_path / "launch.json", launch_bytes)
        ensure_custody_file(session_path / "custody.lock")
    return estate


def test_role_init_cli_initialized_then_existing(sandbox: Sandbox) -> None:
    _runtime(sandbox)

    initialized = sandbox.run("role", "init", "--json")
    assert initialized.returncode == 0, initialized.stderr
    first = json.loads(initialized.stdout)
    assert first["schema_version"] == "hypothesis-tree-role-init-result/1.0.0"
    assert first["status"] == "initialized"

    existing = sandbox.run("role", "init", "--json")
    assert existing.returncode == 0, existing.stderr
    second = json.loads(existing.stdout)
    assert second == {**first, "status": "existing"}


def test_role_init_usage_is_sealed_and_exits_three(sandbox: Sandbox) -> None:
    _runtime(sandbox)
    for argv in (("role", "init"), ("role", "unknown"), ("role", "init", "--extra")):
        result = sandbox.run(*argv)
        assert result.returncode == 3
        assert "usage:" in result.stderr


def test_activation_documents_are_exactly_derived_and_ordered(sandbox: Sandbox) -> None:
    estate = _runtime(sandbox)
    checkpoint_bytes = read_exact_file(estate / "checkpoint.json")
    checkpoint = strict_loads(checkpoint_bytes, label="checkpoint")
    ledger = read_exact_file(estate / "binding-ledger.json")
    documents = _documents(estate)

    assert list(documents.capability.value) == [
        "schema_version",
        "capability",
        "runtime_id",
        "runtime_schema_version",
        "role_request_schema_version",
        "upgrade_base_seq",
        "upgrade_base_clean_wal_sha256",
        "upgrade_base_checkpoint_sha256",
        "upgrade_base_binding_ledger_sha256",
        "created_at",
    ]
    assert documents.capability.value["upgrade_base_checkpoint_sha256"] == (
        hashlib.sha256(checkpoint_bytes).hexdigest()
    )
    assert documents.capability.value["upgrade_base_binding_ledger_sha256"] == (
        hashlib.sha256(ledger).hexdigest()
    )
    upgraded = documents.upgraded_checkpoint.value
    assert set(upgraded) == set(checkpoint) | _V2_ADDITIONS
    assert upgraded["schema_version"] == "hypothesis-tree-runtime-checkpoint/2.0.0"
    assert all(upgraded[name] == checkpoint[name] for name in _V1_FIELDS)
    assert documents.initialization.value["expected_directories"] == list(
        EXPECTED_ROLE_DIRECTORIES
    )
    assert documents.initialization.value["capability_canonical_json"] == (
        documents.capability.canonical_bytes.decode()
    )


def test_role_init_preserves_nonempty_terminal_b1_history_exactly(
    sandbox: Sandbox,
) -> None:
    estate = _terminal_history(sandbox)
    checkpoint_before_bytes = read_exact_file(estate / "checkpoint.json")
    checkpoint_before = strict_loads(checkpoint_before_bytes, label="checkpoint")
    file_paths = (
        "runtime.json",
        "run-ledger.jsonl",
        "binding-ledger.json",
        ".harnessd.lock",
        ".ht-runtime.instance.lock",
    )
    files_before = {name: _identity(estate / name) for name in file_paths}
    trees_before = {
        name: _tree_identity(estate / name)
        for name in ("requests", "responses", "control", "sessions")
    }

    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "initialized"

    assert {name: _identity(estate / name) for name in file_paths} == files_before
    assert {
        name: _tree_identity(estate / name)
        for name in ("requests", "responses", "control", "sessions")
    } == trees_before
    checkpoint_after = strict_loads(
        read_exact_file(estate / "checkpoint.json"),
        label="role checkpoint",
    )
    assert set(checkpoint_after) == set(checkpoint_before) | _V2_ADDITIONS
    assert checkpoint_after["schema_version"] == (
        "hypothesis-tree-runtime-checkpoint/2.0.0"
    )
    assert all(checkpoint_after[name] == checkpoint_before[name] for name in _V1_FIELDS)
    assert not (estate / INITIALIZATION_FILE).exists()
    marker = estate / CAPABILITY_FILE
    marker_info = marker.lstat()
    assert stat.S_ISREG(marker_info.st_mode)
    assert stat.S_IMODE(marker_info.st_mode) == 0o600
    assert marker_info.st_nlink == 1
    assert hashlib.sha256(marker.read_bytes()).hexdigest() == payload[
        "capability_sha256"
    ]
    for relative in EXPECTED_ROLE_DIRECTORIES:
        info = (estate / relative).lstat()
        assert stat.S_ISDIR(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o700


def test_held_instance_lock_rejects_unchanged_and_lock_is_reacquirable(
    sandbox: Sandbox,
) -> None:
    estate = _runtime(sandbox)
    lock_path = estate / ".ht-runtime.instance.lock"
    owner_fd = try_instance_lock(lock_path)
    assert owner_fd is not None
    before = _estate_identity(estate)
    try:
        result = sandbox.run("role", "init", "--json")
        assert result.returncode == 2
        assert "role-init-runtime-busy" in result.stderr
        assert _estate_identity(estate) == before
    finally:
        os.close(owner_fd)
    proof_fd = try_instance_lock(lock_path)
    assert proof_fd is not None
    os.close(proof_fd)


def test_held_terminal_custody_rejects_unchanged(sandbox: Sandbox) -> None:
    estate = _terminal_history(sandbox)
    session_dirs = tuple((estate / "sessions").iterdir())
    assert len(session_dirs) == 1
    before = _estate_identity(estate)
    with _separate_shared_holder(session_dirs[0] / "custody.lock"):
        result = sandbox.run("role", "init", "--json")
        assert result.returncode == 2
        assert "role-init-runtime-busy" in result.stderr
        assert _estate_identity(estate) == before


def test_unindexed_immutable_request_rejects_unchanged(sandbox: Sandbox) -> None:
    _mint_issue(sandbox)
    estate = _runtime(sandbox)
    submitted = sandbox.run(
        "runtime",
        "request",
        "--work-ref",
        "issue#I-1",
        "--json",
    )
    assert submitted.returncode == 0, submitted.stderr
    before = _estate_identity(estate)
    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 2
    assert "role-init-runtime-busy" in result.stderr
    assert _estate_identity(estate) == before


def test_interrupted_b1_request_publication_rejects_unchanged(
    sandbox: Sandbox,
) -> None:
    estate = _runtime(sandbox)
    request_id = str(uuid4())
    temporary = estate / "requests" / f".ht-publish-{request_id}.json-{uuid4()}"
    _write_exact(temporary, b'{"partial":')
    before = _estate_identity(estate)
    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 2
    assert "interrupted B1 operation" in result.stderr
    assert _estate_identity(estate) == before


def test_tolerated_wal_tail_rejects_unchanged(sandbox: Sandbox) -> None:
    estate = _runtime(sandbox)
    with (estate / "run-ledger.jsonl").open("ab") as stream:
        stream.write(b"partial-final-frame")
        stream.flush()
        os.fsync(stream.fileno())
    before = _estate_identity(estate)
    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 2
    assert "WAL tail" in result.stderr
    assert _estate_identity(estate) == before


def test_ledger_ahead_checkpoint_stale_rejects_unchanged(sandbox: Sandbox) -> None:
    estate = _runtime(sandbox)
    descriptor = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime")
    base = replay(parse_bytes(b""), descriptor["runtime_id"])
    record = build_record(
        base,
        "daemon_started",
        "2026-07-15T00:00:00Z",
        daemon_incarnation_id=str(uuid4()),
    )
    wal = frame_record(record)
    ahead = replay(parse_bytes(wal), descriptor["runtime_id"])
    replace_file(estate / "run-ledger.jsonl", wal, expected_old=b"")
    replace_file(
        estate / "binding-ledger.json",
        ahead.binding_bytes(),
        expected_old=base.binding_bytes(),
    )
    before = _estate_identity(estate)
    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 2
    assert "exclusive-lock recovery" in result.stderr
    assert _estate_identity(estate) == before


def test_planned_binding_rejects_unchanged(sandbox: Sandbox) -> None:
    _mint_issue(sandbox)
    estate = _runtime(sandbox)
    submitted = sandbox.run(
        "runtime",
        "request",
        "--work-ref",
        "issue#I-1",
        "--json",
    )
    assert submitted.returncode == 0, submitted.stderr
    request_id = json.loads(submitted.stdout)["request_id"]
    request_path = estate / "requests" / f"{request_id}.json"
    request_bytes = read_exact_file(request_path)
    request = strict_loads(request_bytes, label="request")
    with audit_lock(estate / ".harnessd.lock", exclusive=True):
        _estate, _descriptor, state = read_state_unlocked(sandbox.root)
        record = build_record(
            state,
            "work_planned",
            "2026-07-15T00:00:00Z",
            request_id=request_id,
            request_sha256=hashlib.sha256(request_bytes).hexdigest(),
            binding_id=str(uuid4()),
            dedup_key=derive_dedup_key(request),
            request=request,
        )
        wal = frame_record(record)
        target = replay(parse_bytes(wal), state.runtime_id)
        replace_file(estate / "run-ledger.jsonl", wal, expected_old=b"")
        publish_projections(estate, target, allowed_prior=(state, target))
    before = _estate_identity(estate)
    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 2
    assert "role-init-runtime-busy" in result.stderr
    assert _estate_identity(estate) == before


@pytest.mark.parametrize("abandoned", (False, True), ids=("claimed", "abandoned"))
def test_nonterminal_or_abandoned_session_rejects_unchanged(
    sandbox: Sandbox,
    abandoned: bool,
) -> None:
    estate = _stopped_session_state(sandbox, abandoned=abandoned)
    before = _estate_identity(estate)
    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 2
    assert "role-init-runtime-busy" in result.stderr
    assert _estate_identity(estate) == before


def test_orphan_hidden_prelink_temp_is_recovered_as_new_initialization(
    sandbox: Sandbox,
) -> None:
    estate = _runtime(sandbox)
    temporary = estate / f".ht-publish-{INITIALIZATION_FILE}-{uuid4()}"
    _write_exact(temporary, b'{"partial":')
    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "initialized"
    assert not temporary.exists()


@pytest.mark.parametrize("prefix_count", range(len(EXPECTED_ROLE_DIRECTORIES) + 1))
def test_every_directory_fsync_prefix_repairs_without_a_new_clock(
    sandbox: Sandbox,
    prefix_count: int,
) -> None:
    estate = _runtime(sandbox)
    documents = _documents(estate)
    _write_exact(estate / INITIALIZATION_FILE, documents.initialization.canonical_bytes)
    _mkdir_prefix(estate, prefix_count)
    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "repaired"
    assert payload["created_at"] == documents.capability.value["created_at"]
    assert payload["capability_sha256"] == documents.capability.sha256


@pytest.mark.parametrize(
    "stage,expected_status",
    (
        ("hidden-linked", "repaired"),
        ("checkpoint-temp", "repaired"),
        ("checkpoint-replaced", "repaired"),
        ("public-temp", "repaired"),
        ("public-linked", "repaired"),
        ("public-validated", "repaired"),
        ("hidden-unlinked", "existing"),
    ),
)
def test_activation_boundary_fault_states_resume_through_public_cli(
    sandbox: Sandbox,
    stage: str,
    expected_status: str,
) -> None:
    estate = _runtime(sandbox)
    documents = _documents(estate)
    hidden = estate / INITIALIZATION_FILE
    public = estate / CAPABILITY_FILE
    if stage != "hidden-unlinked":
        _write_exact(hidden, documents.initialization.canonical_bytes)
    if stage == "hidden-linked":
        os.link(hidden, estate / f".ht-publish-{INITIALIZATION_FILE}-{uuid4()}")
    else:
        _mkdir_prefix(estate, len(EXPECTED_ROLE_DIRECTORIES))
    if stage in {
        "checkpoint-replaced",
        "public-temp",
        "public-linked",
        "public-validated",
        "hidden-unlinked",
    }:
        replace_file(
            estate / "checkpoint.json",
            documents.upgraded_checkpoint.canonical_bytes,
            expected_old=documents.baseline_checkpoint_bytes,
        )
    if stage == "checkpoint-temp":
        _write_exact(
            estate / f".ht-replace-checkpoint.json-{uuid4()}",
            b'{"partial":',
        )
    if stage == "public-temp":
        _write_exact(estate / f".ht-publish-{CAPABILITY_FILE}-{uuid4()}", b"partial")
    if stage in {"public-linked", "public-validated", "hidden-unlinked"}:
        _write_exact(public, documents.capability.canonical_bytes)
    if stage == "public-linked":
        os.link(public, estate / f".ht-publish-{CAPABILITY_FILE}-{uuid4()}")

    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == expected_status
    assert payload["created_at"] == documents.capability.value["created_at"]
    assert payload["capability_sha256"] == documents.capability.sha256
    assert not hidden.exists()


@pytest.mark.parametrize("fault_stage", _FAULT_STAGES)
def test_injected_activation_faults_leave_only_publicly_repairable_states(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
) -> None:
    estate = _runtime(sandbox)
    frozen_time = "2026-07-15T00:00:00.000000Z"
    monkeypatch.setattr(role_commands, "_created_at", lambda: frozen_time)

    def fail_at(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"injected crash at {stage}")

    monkeypatch.setattr(role_commands, "_fault_point", fail_at)
    with mutex.global_mutex(Root(sandbox.root)):
        with pytest.raises(RuntimeError, match="injected crash"):
            role_commands.init(Root(sandbox.root), as_json=True)
    monkeypatch.setattr(role_commands, "_fault_point", lambda _stage: None)

    retry = sandbox.run("role", "init", "--json")
    assert retry.returncode == 0, retry.stderr
    payload = json.loads(retry.stdout)
    assert payload["status"] == (
        "existing" if fault_stage in {"hidden-unlinked", "runtime-fsynced"} else "repaired"
    )
    assert payload["created_at"] == frozen_time
    assert not (estate / INITIALIZATION_FILE).exists()


def test_retry_after_hidden_unlink_completes_runtime_directory_fsync(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    estate = _runtime(sandbox)

    def fail_after_unlink(stage: str) -> None:
        if stage == "hidden-unlinked":
            raise RuntimeError("injected crash after hidden unlink")

    monkeypatch.setattr(role_commands, "_fault_point", fail_after_unlink)
    with mutex.global_mutex(Root(sandbox.root)):
        with pytest.raises(RuntimeError, match="injected crash after hidden unlink"):
            role_commands.init(Root(sandbox.root), as_json=True)
    assert not (estate / INITIALIZATION_FILE).exists()
    assert (estate / CAPABILITY_FILE).is_file()

    monkeypatch.setattr(role_commands, "_fault_point", lambda _stage: None)
    real_fsync = role_commands.fsync_directory
    fsynced: list[Path] = []

    def observe_fsync(path: Path) -> None:
        fsynced.append(path)
        real_fsync(path)

    monkeypatch.setattr(role_commands, "fsync_directory", observe_fsync)
    with mutex.global_mutex(Root(sandbox.root)):
        assert role_commands.init(Root(sandbox.root), as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "existing"
    assert fsynced == [estate]
    assert not (estate / INITIALIZATION_FILE).exists()


@pytest.mark.parametrize(
    "corruption",
    (
        "hidden-bytes",
        "hidden-mode",
        "hidden-symlink",
        "hidden-hardlink",
        "prefix-hole",
        "prefix-nonempty",
        "prefix-mode",
        "public-mismatch",
    ),
)
def test_corrupt_marker_or_prefix_rejects_without_mutation(
    sandbox: Sandbox,
    corruption: str,
) -> None:
    estate = _runtime(sandbox)
    documents = _documents(estate)
    hidden = estate / INITIALIZATION_FILE
    if corruption == "hidden-bytes":
        _write_exact(hidden, b"{}\n")
    elif corruption == "hidden-mode":
        _write_exact(hidden, documents.initialization.canonical_bytes, mode=0o640)
    elif corruption == "hidden-symlink":
        hidden.symlink_to("checkpoint.json")
    elif corruption == "hidden-hardlink":
        _write_exact(hidden, documents.initialization.canonical_bytes)
        os.link(hidden, estate / "unowned-hidden-alias")
    elif corruption == "prefix-hole":
        _write_exact(hidden, documents.initialization.canonical_bytes)
        path = estate / EXPECTED_ROLE_DIRECTORIES[1]
        path.mkdir()
        os.chmod(path, 0o700)
    elif corruption == "prefix-nonempty":
        _write_exact(hidden, documents.initialization.canonical_bytes)
        _mkdir_prefix(estate, 1)
        _write_exact(estate / EXPECTED_ROLE_DIRECTORIES[0] / "rogue.json", b"{}\n")
    elif corruption == "prefix-mode":
        _write_exact(hidden, documents.initialization.canonical_bytes)
        _mkdir_prefix(estate, 1)
        os.chmod(estate / EXPECTED_ROLE_DIRECTORIES[0], 0o755)
    else:
        _mkdir_prefix(estate, len(EXPECTED_ROLE_DIRECTORIES))
        replace_file(
            estate / "checkpoint.json",
            documents.upgraded_checkpoint.canonical_bytes,
            expected_old=documents.baseline_checkpoint_bytes,
        )
        changed = dict(documents.capability.value)
        changed["created_at"] = "2026-07-15T00:00:01.000000Z"
        _write_exact(estate / CAPABILITY_FILE, canonical_json_bytes(changed))

    before = _estate_identity(estate)
    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 2
    assert _estate_identity(estate) == before
