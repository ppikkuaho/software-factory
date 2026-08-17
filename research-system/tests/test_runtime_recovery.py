"""Deterministic physical proofs for the B1 §14 storage-recovery boundary."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import stat
from typing import Iterator

import pytest

from conftest import Sandbox
from ht.errors import HtError
from ht.paths import Root
from ht.runtime.atomic import make_directory, publish_immutable, read_exact_file, replace_file
from ht.runtime.custody import custody_is_free, ensure_custody_file
import ht.runtime.daemon as runtime_daemon
from ht.runtime.launcher import construct_claim
from ht.runtime.replay import ReplayState, build_record, publish_projections, replay
from ht.runtime.repository import snapshot_work
from ht.runtime.schema import canonical_json_bytes, strict_loads
from ht.runtime.state import derive_dedup_key
from ht.runtime.wal import frame_record, parse_bytes
from ht.runtime.views import packet as packet_view


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _physical_snapshot(root: Path) -> tuple[tuple[str, int, int, int, int, bytes | None], ...]:
    rows = []
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        payload = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        rows.append(
            (
                relative,
                stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode),
                info.st_nlink,
                info.st_mtime_ns,
                payload,
            )
        )
    return tuple(rows)


@contextmanager
def _separate_shared_holder(path: Path) -> Iterator[None]:
    """Hold LOCK_SH in a pipe-synchronized separate process."""

    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions are made by the parent
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


class ForbiddenPopen:
    def __call__(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("recovery must not call Popen")


class _CapturedWrapper:
    def poll(self) -> None:
        return None


class CapturingPopen:
    def __init__(self, estate: Path, request_id: str) -> None:
        self.estate = estate
        self.request_id = request_id
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **options: object) -> _CapturedWrapper:
        response = strict_loads(
            read_exact_file(self.estate / "responses" / f"{self.request_id}.json"),
            label="accepted response at Popen",
        )
        assert response["status"] == "accepted"
        assert response["recovery_created"] is False
        self.calls.append((list(argv), dict(options)))
        return _CapturedWrapper()


def _mint_issue(sandbox: Sandbox) -> None:
    result = sandbox.run(
        "issue",
        "mint",
        "--title",
        "SYNTHETIC-RECOVERY-0",
        "--question",
        "SYNTHETIC-RECOVERY-1",
        "--done-definition",
        "SYNTHETIC-RECOVERY-2",
        "--provenance",
        "user-seed#synthetic-recovery-0",
        "--lanes",
        "L4",
        role="pc",
    )
    assert result.returncode == 0, result.stderr


def _append(
    wal: bytes,
    state: ReplayState,
    event: str,
    **fields: object,
) -> tuple[bytes, ReplayState]:
    wal += frame_record(build_record(state, event, "2026-07-15T00:00:00Z", **fields))
    return wal, replay(parse_bytes(wal), state.runtime_id)


@dataclass(frozen=True)
class Scenario:
    estate: Path
    runtime_id: str
    request: dict[str, object]
    state: ReplayState
    stale_state: ReplayState
    packet_bytes: bytes
    launch_bytes: bytes
    session_id: str

    @property
    def session_path(self) -> Path:
        return self.estate / "sessions" / self.session_id


@dataclass(frozen=True)
class PlannedScenario:
    estate: Path
    runtime_id: str
    request: dict[str, object]
    request_bytes: bytes
    binding_id: str
    state: ReplayState


def _planned_scenario(sandbox: Sandbox) -> PlannedScenario:
    _mint_issue(sandbox)
    assert sandbox.run("runtime", "init", "--json").returncode == 0
    estate = sandbox.root / "var" / "runtime"
    descriptor = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime")
    runtime_id = descriptor["runtime_id"]
    work = snapshot_work(Root(sandbox.root), "issue#I-1").as_dict()
    request: dict[str, object] = {
        "schema_version": "hypothesis-tree-runtime-request/1.0.0",
        "request_id": _uuid(501),
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
    state = replay(parse_bytes(b""), runtime_id)
    genesis = state
    wal, state = _append(
        b"", state, "daemon_started", daemon_incarnation_id=_uuid(1)
    )
    binding_id = _uuid(502)
    wal, state = _append(
        wal,
        state,
        "work_planned",
        request_id=request["request_id"],
        request_sha256=hashlib.sha256(request_bytes).hexdigest(),
        binding_id=binding_id,
        dedup_key=derive_dedup_key(request),
        request=request,
    )
    replace_file(estate / "run-ledger.jsonl", wal, expected_old=b"")
    publish_projections(estate, state, allowed_prior=(genesis, state))
    return PlannedScenario(
        estate=estate,
        runtime_id=runtime_id,
        request=request,
        request_bytes=request_bytes,
        binding_id=binding_id,
        state=state,
    )


def _scenario(
    sandbox: Sandbox,
    *,
    boundary: str,
    recovery_created: bool = False,
) -> Scenario:
    _mint_issue(sandbox)
    assert sandbox.run("runtime", "init", "--json").returncode == 0
    estate = sandbox.root / "var" / "runtime"
    descriptor = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime")
    runtime_id = descriptor["runtime_id"]
    work = snapshot_work(Root(sandbox.root), "issue#I-1").as_dict()
    request: dict[str, object] = {
        "schema_version": "hypothesis-tree-runtime-request/1.0.0",
        "request_id": _uuid(101),
        "request_created_at": "2026-07-15T00:00:00Z",
        "role": "synthetic-kernel-v1",
        "attempt": 1,
        "retry_lineage": [],
        "work": work,
    }
    request_bytes = canonical_json_bytes(request)
    publish_immutable(estate / "requests" / f"{request['request_id']}.json", request_bytes)

    wal = b""
    state = replay(parse_bytes(wal), runtime_id)
    genesis = state
    wal, state = _append(wal, state, "daemon_started", daemon_incarnation_id=_uuid(1))
    stale_state = state
    binding_id = _uuid(102)
    session_id = _uuid(103)
    wal, state = _append(
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
        wrapper_instance_id=_uuid(104),
        helper_instance_id=_uuid(105),
        packet_created_at="2026-07-15T00:00:00Z",
    )
    wal, state = _append(
        wal,
        state,
        "work_claimed",
        request_id=request["request_id"],
        binding_id=binding_id,
        lease_epoch=1,
        session_id=session_id,
        admission_repository_commit=work["submission_repository_commit"],
        packet=packet,
        packet_canonical_json=packet_bytes.decode("utf-8"),
        packet_sha256=packet_sha,
        launch=launch,
        launch_canonical_json=launch_bytes.decode("utf-8"),
        launch_sha256=launch_sha,
    )
    if boundary in {"starting", "accepted"}:
        wal, state = _append(
            wal,
            state,
            "session_starting",
            request_id=request["request_id"],
            binding_id=binding_id,
            lease_epoch=1,
            session_id=session_id,
        )
    if boundary == "accepted":
        wal, state = _append(
            wal,
            state,
            "request_accepted",
            request_id=request["request_id"],
            binding_id=binding_id,
            lease_epoch=1,
            session_id=session_id,
            packet_sha256=packet_sha,
            recovery_created=recovery_created,
        )

    replace_file(estate / "run-ledger.jsonl", wal, expected_old=b"")
    publish_projections(estate, state, allowed_prior=(genesis, state))
    return Scenario(
        estate=estate,
        runtime_id=runtime_id,
        request=request,
        state=state,
        stale_state=stale_state,
        packet_bytes=packet_bytes,
        launch_bytes=launch_bytes,
        session_id=session_id,
    )


def _publish_preparation(scenario: Scenario, names: tuple[str, ...]) -> None:
    make_directory(scenario.session_path)
    if "packet" in names:
        publish_immutable(scenario.session_path / "packet.json", scenario.packet_bytes)
    if "launch" in names:
        publish_immutable(scenario.session_path / "launch.json", scenario.launch_bytes)
    if "custody" in names:
        ensure_custody_file(scenario.session_path / "custody.lock")


def _publish_claim_prefix(scenario: Scenario, prefix: str) -> None:
    if prefix == "absent-directory":
        return
    make_directory(scenario.session_path)
    if prefix in {"packet", "packet-linked", "launch-temp", "launch-linked", "complete"}:
        publish_immutable(scenario.session_path / "packet.json", scenario.packet_bytes)
    if prefix in {"launch-linked", "complete"}:
        publish_immutable(scenario.session_path / "launch.json", scenario.launch_bytes)
        if prefix == "complete":
            ensure_custody_file(scenario.session_path / "custody.lock")
    elif prefix == "packet-temp":
        temporary = scenario.session_path / f".ht-publish-packet.json-{_uuid(301)}"
        temporary.write_bytes(b"interrupted-packet")
        os.chmod(temporary, 0o600)
    elif prefix == "packet-linked":
        temporary = scenario.session_path / f".ht-publish-packet.json-{_uuid(303)}"
        os.link(scenario.session_path / "packet.json", temporary)
    elif prefix == "launch-temp":
        temporary = scenario.session_path / f".ht-publish-launch.json-{_uuid(302)}"
        temporary.write_bytes(b"interrupted-launch")
        os.chmod(temporary, 0o600)
    elif prefix == "launch-linked":
        temporary = scenario.session_path / f".ht-publish-launch.json-{_uuid(304)}"
        os.link(scenario.session_path / "launch.json", temporary)


def _publish_accepted_response(scenario: Scenario) -> None:
    request_id = str(scenario.request["request_id"])
    entry = scenario.state.request_index[request_id]
    publish_immutable(
        scenario.estate / "responses" / f"{request_id}.json",
        canonical_json_bytes(runtime_daemon._response_object(entry)),
    )


def _receipt_objects(scenario: Scenario) -> dict[str, dict[str, object]]:
    work = next(
        value
        for key, value in scenario.state.bindings.items()
        if key != "runtime#kernel"
    )
    session = work["sessions"][scenario.session_id]
    common = {
        "runtime_id": scenario.runtime_id,
        "request_id": work["request_id"],
        "binding_id": work["binding_id"],
        "lease_epoch": session["lease_epoch"],
        "session_id": scenario.session_id,
        "packet_sha256": session["packet_sha256"],
    }
    started = {
        "schema_version": "hypothesis-tree-runtime-started/1.0.0",
        **common,
        "wrapper_instance_id": session["wrapper_instance_id"],
        "helper_instance_id": session["helper_instance_id"],
        "wrapper_pid": 201,
        "helper_pid": 202,
        "started_at": "2026-07-15T00:00:01Z",
    }
    ready = {
        "schema_version": "hypothesis-tree-runtime-ready/1.0.0",
        **common,
        "helper_instance_id": session["helper_instance_id"],
        "helper_pid": 202,
        "ready_at": "2026-07-15T00:00:02Z",
    }
    result = {
        "schema_version": "hypothesis-tree-runtime-result/1.0.0",
        **common,
        "helper_instance_id": session["helper_instance_id"],
        "outcome": "SUCCEEDED",
    }
    result_sha = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    terminal = {
        "schema_version": "hypothesis-tree-runtime-terminal/1.0.0",
        **common,
        "helper_instance_id": session["helper_instance_id"],
        "outcome": "SUCCEEDED",
        "result_sha256": result_sha,
        "terminal_at": "2026-07-15T00:00:03Z",
    }
    process_exit = {
        "schema_version": "hypothesis-tree-runtime-process-exit/1.0.0",
        **common,
        "wrapper_instance_id": session["wrapper_instance_id"],
        "helper_instance_id": session["helper_instance_id"],
        "wrapper_pid": 201,
        "helper_pid": 202,
        "wait_status": 0,
        "result_sha256": result_sha,
        "terminal_sha256": hashlib.sha256(canonical_json_bytes(terminal)).hexdigest(),
        "process_exited_at": "2026-07-15T00:00:04Z",
    }
    return {
        "started": started,
        "ready": ready,
        "result": result,
        "terminal": terminal,
        "process-exit": process_exit,
    }


def _publish_receipts(
    scenario: Scenario,
    receipts: dict[str, dict[str, object]],
    names: tuple[str, ...],
) -> None:
    for name in names:
        publish_immutable(
            scenario.session_path / f"{name}.json",
            canonical_json_bytes(receipts[name]),
        )


def _publication_temp(
    target: Path,
    number: int,
    *,
    payload: bytes = b"opaque-interrupted-publication",
) -> Path:
    temporary = target.parent / f".ht-publish-{target.name}-{_uuid(number)}"
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o600)
    return temporary


def _file_snapshot(path: Path) -> tuple[int, int, int, int, bytes]:
    info = path.lstat()
    return (
        stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode),
        info.st_nlink,
        info.st_mtime_ns,
        path.read_bytes(),
    )


def _set_receipt_outcome(
    receipts: dict[str, dict[str, object]],
    outcome: str,
) -> None:
    receipts["result"]["outcome"] = outcome
    result_sha = hashlib.sha256(canonical_json_bytes(receipts["result"])).hexdigest()
    receipts["terminal"]["outcome"] = outcome
    receipts["terminal"]["result_sha256"] = result_sha
    receipts["process-exit"]["result_sha256"] = result_sha
    receipts["process-exit"]["terminal_sha256"] = hashlib.sha256(
        canonical_json_bytes(receipts["terminal"])
    ).hexdigest()


def _advance_running(scenario: Scenario) -> ReplayState:
    started_sha = hashlib.sha256(
        read_exact_file(scenario.session_path / "started.json")
    ).hexdigest()
    ready_sha = hashlib.sha256(
        read_exact_file(scenario.session_path / "ready.json")
    ).hexdigest()
    work = next(
        value
        for key, value in scenario.state.bindings.items()
        if key != "runtime#kernel"
    )
    wal, state = _append(
        scenario.state.clean_prefix,
        scenario.state,
        "session_running",
        request_id=work["request_id"],
        binding_id=work["binding_id"],
        lease_epoch=1,
        session_id=scenario.session_id,
        started_sha256=started_sha,
        ready_sha256=ready_sha,
    )
    replace_file(
        scenario.estate / "run-ledger.jsonl",
        wal,
        expected_old=scenario.state.clean_prefix,
    )
    publish_projections(
        scenario.estate,
        state,
        allowed_prior=(scenario.state, state),
    )
    return state


def _advance_terminal(
    scenario: Scenario,
    state: ReplayState,
    *,
    outcome: str = "SUCCEEDED",
    reason_code: str | None = None,
) -> ReplayState:
    work = next(
        value
        for key, value in state.bindings.items()
        if key != "runtime#kernel"
    )
    hashes = {
        f"{name.replace('-', '_')}_sha256": hashlib.sha256(
            read_exact_file(scenario.session_path / f"{name}.json")
        ).hexdigest()
        if (scenario.session_path / f"{name}.json").exists()
        else None
        for name in ("started", "ready", "result", "terminal", "process-exit")
    }
    wal, terminal_state = _append(
        state.clean_prefix,
        state,
        "session_terminal",
        request_id=work["request_id"],
        binding_id=work["binding_id"],
        lease_epoch=1,
        session_id=scenario.session_id,
        outcome=outcome,
        reason_code=reason_code,
        **hashes,
    )
    replace_file(
        scenario.estate / "run-ledger.jsonl",
        wal,
        expected_old=state.clean_prefix,
    )
    publish_projections(
        scenario.estate,
        terminal_state,
        allowed_prior=(state, terminal_state),
    )
    return terminal_state


def _make_stale_projections(scenario: Scenario) -> None:
    replace_file(
        scenario.estate / "binding-ledger.json",
        scenario.stale_state.binding_bytes(),
        expected_old=scenario.state.binding_bytes(),
    )
    replace_file(
        scenario.estate / "checkpoint.json",
        scenario.stale_state.checkpoint_bytes(),
        expected_old=scenario.state.checkpoint_bytes(),
    )


def test_ensure_custody_file_exact_shapes_and_free_held_probe(tmp_path: Path) -> None:
    parent = tmp_path / "session"
    parent.mkdir(mode=0o700)
    path = parent / "custody.lock"
    ensure_custody_file(path)
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_nlink == 1 and path.read_bytes() == b""
    before = _physical_snapshot(parent)
    ensure_custody_file(path)
    assert _physical_snapshot(parent) == before
    assert custody_is_free(path)
    with _separate_shared_holder(path):
        assert not custody_is_free(path)
    assert custody_is_free(path)

    wrong_content = parent / "content.lock"
    wrong_content.write_bytes(b"x")
    os.chmod(wrong_content, 0o600)
    with pytest.raises(HtError):
        ensure_custody_file(wrong_content)
    wrong_mode = parent / "mode.lock"
    wrong_mode.write_bytes(b"")
    os.chmod(wrong_mode, 0o640)
    with pytest.raises(HtError):
        ensure_custody_file(wrong_mode)
    symlink = parent / "symlink.lock"
    symlink.symlink_to(path.name)
    with pytest.raises((HtError, OSError)):
        ensure_custody_file(symlink)
    alias_source = parent / "alias-source.lock"
    alias_source.write_bytes(b"")
    os.chmod(alias_source, 0o600)
    alias = parent / "alias.lock"
    os.link(alias_source, alias)
    with pytest.raises(HtError):
        ensure_custody_file(alias)
    directory = parent / "directory.lock"
    directory.mkdir(mode=0o700)
    with pytest.raises((HtError, OSError)):
        ensure_custody_file(directory)


@pytest.mark.parametrize("torn_tail", (False, True))
def test_held_claimed_custody_blocks_projection_or_tail_repair_unchanged(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    torn_tail: bool,
) -> None:
    scenario = _scenario(sandbox, boundary="claimed")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _make_stale_projections(scenario)
    if torn_tail:
        replace_file(
            scenario.estate / "run-ledger.jsonl",
            scenario.state.clean_prefix + b"torn-final-segment",
            expected_old=scenario.state.clean_prefix,
        )
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())
    with _separate_shared_holder(scenario.session_path / "custody.lock"):
        before = _physical_snapshot(scenario.estate)
        with pytest.raises(HtError, match="custody is held"):
            runtime_daemon._boot_state(scenario.estate, scenario.runtime_id)
        assert _physical_snapshot(scenario.estate) == before


@pytest.mark.parametrize("kind", ("without-event", "conflicting"))
def test_response_without_event_or_conflict_is_exact_no_mutation(
    sandbox: Sandbox,
    kind: str,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    if kind == "without-event":
        request_id = _uuid(999)
        response = {
            "schema_version": "hypothesis-tree-runtime-admission-response/1.0.0",
            "status": "rejected",
            "request_id": request_id,
            "reason_code": "work-drift",
        }
        publish_immutable(
            scenario.estate / "responses" / f"{request_id}.json",
            canonical_json_bytes(response),
        )
    else:
        publish_immutable(
            scenario.estate / "responses" / f"{scenario.request['request_id']}.json",
            b"conflicting-response\n",
        )
    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


@pytest.mark.parametrize("contradiction", ("held-custody", "child-artifact"))
def test_accepted_missing_response_preflight_rejects_process_evidence_unchanged(
    sandbox: Sandbox,
    contradiction: str,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    holder = None
    if contradiction == "child-artifact":
        work = next(
            value
            for key, value in scenario.state.bindings.items()
            if key != "runtime#kernel"
        )
        session = work["sessions"][scenario.session_id]
        started = {
            "schema_version": "hypothesis-tree-runtime-started/1.0.0",
            "runtime_id": scenario.runtime_id,
            "request_id": work["request_id"],
            "binding_id": work["binding_id"],
            "lease_epoch": 1,
            "session_id": scenario.session_id,
            "packet_sha256": session["packet_sha256"],
            "wrapper_instance_id": session["wrapper_instance_id"],
            "helper_instance_id": session["helper_instance_id"],
            "wrapper_pid": 101,
            "helper_pid": 102,
            "started_at": "2026-07-15T00:00:00Z",
        }
        publish_immutable(
            scenario.session_path / "started.json",
            canonical_json_bytes(started),
        )
    else:
        holder = _separate_shared_holder(scenario.session_path / "custody.lock")
        holder.__enter__()
    try:
        before = _physical_snapshot(scenario.estate)
        with pytest.raises(HtError):
            runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
        assert _physical_snapshot(scenario.estate) == before
        assert not (
            scenario.estate / "responses" / f"{scenario.request['request_id']}.json"
        ).exists()
    finally:
        if holder is not None:
            holder.__exit__(None, None, None)


def test_starting_missing_preparation_rejects_unchanged(sandbox: Sandbox) -> None:
    scenario = _scenario(sandbox, boundary="starting")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    (scenario.session_path / "launch.json").unlink()
    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError, match="lacks frozen preparation"):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


def test_starting_unaccepted_held_custody_rejects_before_mutation(
    sandbox: Sandbox,
) -> None:
    scenario = _scenario(sandbox, boundary="starting")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    with _separate_shared_holder(scenario.session_path / "custody.lock"):
        before = _physical_snapshot(scenario.estate)
        with pytest.raises(HtError, match="no-process boundary"):
            runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
        assert _physical_snapshot(scenario.estate) == before


@pytest.mark.parametrize(
    "prefix",
    (
        "absent-directory",
        "empty",
        "packet",
        "packet-temp",
        "packet-linked",
        "launch-temp",
        "launch-linked",
        "complete",
    ),
)
def test_claimed_preparation_reconstructs_only_exact_wal_bytes_after_scan(
    sandbox: Sandbox,
    prefix: str,
) -> None:
    scenario = _scenario(sandbox, boundary="claimed")
    if prefix != "absent-directory":
        make_directory(scenario.session_path)
    if prefix in {"packet", "packet-linked", "launch-temp", "launch-linked", "complete"}:
        publish_immutable(scenario.session_path / "packet.json", scenario.packet_bytes)
    if prefix in {"launch-linked", "complete"}:
        publish_immutable(scenario.session_path / "launch.json", scenario.launch_bytes)
        if prefix == "complete":
            ensure_custody_file(scenario.session_path / "custody.lock")
    elif prefix == "packet-temp":
        temporary = scenario.session_path / f".ht-publish-packet.json-{_uuid(301)}"
        temporary.write_bytes(b"interrupted-packet")
        os.chmod(temporary, 0o600)
    elif prefix == "packet-linked":
        temporary = scenario.session_path / f".ht-publish-packet.json-{_uuid(303)}"
        os.link(scenario.session_path / "packet.json", temporary)
    elif prefix == "launch-temp":
        temporary = scenario.session_path / f".ht-publish-launch.json-{_uuid(302)}"
        temporary.write_bytes(b"interrupted-launch")
        os.chmod(temporary, 0o600)
    elif prefix == "launch-linked":
        temporary = scenario.session_path / f".ht-publish-launch.json-{_uuid(304)}"
        os.link(scenario.session_path / "launch.json", temporary)

    before_scan = _physical_snapshot(scenario.estate)
    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before_scan
    recovered = runtime_daemon._recover_boot_storage(scenario.estate, facts)
    assert recovered.last_seq == scenario.state.last_seq
    assert read_exact_file(scenario.session_path / "packet.json") == scenario.packet_bytes
    assert read_exact_file(scenario.session_path / "launch.json") == scenario.launch_bytes
    assert read_exact_file(scenario.session_path / "custody.lock") == b""
    assert custody_is_free(scenario.session_path / "custody.lock")
    assert not [path for path in scenario.session_path.iterdir() if path.name.startswith(".ht-")]


def test_conflicting_claim_preparation_is_exact_no_mutation(sandbox: Sandbox) -> None:
    scenario = _scenario(sandbox, boundary="claimed")
    make_directory(scenario.session_path)
    publish_immutable(scenario.session_path / "packet.json", b"conflict\n")
    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError, match="conflicting atomic final"):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


def test_accepted_response_repair_preserves_boot_absence_fact(sandbox: Sandbox) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    request_id = str(scenario.request["request_id"])
    assert request_id not in facts.accepted_responses_present_at_entry
    runtime_daemon._recover_boot_storage(
        scenario.estate,
        facts,
        repair_accepted_lifecycles=frozenset({"starting"}),
    )
    response = strict_loads(
        read_exact_file(scenario.estate / "responses" / f"{request_id}.json"),
        label="repaired response",
    )
    assert response["status"] == "accepted"
    assert response["recovery_created"] is False
    assert request_id not in facts.accepted_responses_present_at_entry


def test_storage_recovery_reconciles_all_event_derived_response_kinds(
    sandbox: Sandbox,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    wal = scenario.state.clean_prefix
    state = scenario.state

    duplicate = dict(scenario.request)
    duplicate["request_id"] = _uuid(106)
    duplicate["request_created_at"] = "2026-07-15T00:00:01Z"
    publish_immutable(
        scenario.estate / "requests" / f"{duplicate['request_id']}.json",
        canonical_json_bytes(duplicate),
    )
    accepted = state.request_index[str(scenario.request["request_id"])]
    wal, state = _append(
        wal,
        state,
        "request_duplicate",
        request_id=duplicate["request_id"],
        original_request_id=scenario.request["request_id"],
        binding_id=accepted["binding_id"],
        lease_epoch=accepted["lease_epoch"],
        session_id=accepted["session_id"],
        packet_sha256=accepted["packet_sha256"],
    )

    rejected = dict(scenario.request)
    rejected["request_id"] = _uuid(107)
    rejected["request_created_at"] = "2026-07-15T00:00:02Z"
    publish_immutable(
        scenario.estate / "requests" / f"{rejected['request_id']}.json",
        canonical_json_bytes(rejected),
    )
    wal, state = _append(
        wal,
        state,
        "request_rejected",
        request_id=rejected["request_id"],
        binding_id=None,
        reason_code="work-drift",
    )

    control_id = _uuid(108)
    control = {
        "schema_version": "hypothesis-tree-runtime-control-request/1.0.0",
        "control_id": control_id,
        "control_created_at": "2026-07-15T00:00:03Z",
        "target_daemon_incarnation_id": _uuid(1),
        "operation": "stop",
    }
    publish_immutable(
        scenario.estate / "control" / "requests" / f"{control_id}.json",
        canonical_json_bytes(control),
    )
    wal, state = _append(
        wal,
        state,
        "control_stop_accepted",
        control_id=control_id,
        target_daemon_incarnation_id=_uuid(1),
    )
    replace_file(
        scenario.estate / "run-ledger.jsonl",
        wal,
        expected_old=scenario.state.clean_prefix,
    )
    publish_projections(
        scenario.estate,
        state,
        allowed_prior=(scenario.state, state),
    )

    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    runtime_daemon._recover_boot_storage(
        scenario.estate,
        facts,
        repair_accepted_lifecycles=frozenset({"starting"}),
    )
    statuses = {
        request_id: strict_loads(
            read_exact_file(scenario.estate / "responses" / f"{request_id}.json"),
            label="response",
        )["status"]
        for request_id in (
            str(scenario.request["request_id"]),
            str(duplicate["request_id"]),
            str(rejected["request_id"]),
        )
    }
    assert statuses == {
        str(scenario.request["request_id"]): "accepted",
        str(duplicate["request_id"]): "duplicate",
        str(rejected["request_id"]): "rejected",
    }
    control_response = strict_loads(
        read_exact_file(
            scenario.estate / "control" / "responses" / f"{control_id}.json"
        ),
        label="control response",
    )
    assert control_response["status"] == "accepted"


def test_normal_clean_boot_scan_and_storage_recovery_are_noop(sandbox: Sandbox) -> None:
    assert sandbox.run("runtime", "init", "--json").returncode == 0
    estate = sandbox.root / "var" / "runtime"
    descriptor = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime")
    before = _physical_snapshot(estate)
    facts = runtime_daemon._scan_boot(estate, descriptor["runtime_id"])
    state = runtime_daemon._recover_boot_storage(estate, facts)
    assert state.last_seq == 0
    assert _physical_snapshot(estate) == before


@pytest.mark.parametrize("queue", ("request", "control"))
def test_boot_scan_rejects_malformed_unseen_queue_object_unchanged(
    sandbox: Sandbox,
    queue: str,
) -> None:
    assert sandbox.run("runtime", "init", "--json").returncode == 0
    estate = sandbox.root / "var" / "runtime"
    descriptor = strict_loads(read_exact_file(estate / "runtime.json"), label="runtime")
    queue_root = (
        estate / "requests"
        if queue == "request"
        else estate / "control" / "requests"
    )
    publish_immutable(queue_root / f"{_uuid(401)}.json", b"{}\n")
    before = _physical_snapshot(estate)
    with pytest.raises(HtError):
        runtime_daemon._scan_boot(estate, descriptor["runtime_id"])
    assert _physical_snapshot(estate) == before


def test_bound_request_file_must_equal_work_planned_bytes_unchanged(
    sandbox: Sandbox,
) -> None:
    scenario = _scenario(sandbox, boundary="claimed")
    request_id = str(scenario.request["request_id"])
    path = scenario.estate / "requests" / f"{request_id}.json"
    old = read_exact_file(path)
    changed = dict(scenario.request)
    changed["request_created_at"] = "2026-07-15T00:00:09Z"
    replace_file(path, canonical_json_bytes(changed), expected_old=old)
    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError, match="work_planned truth"):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


def test_duplicate_request_keeps_own_canonical_submission_identity(
    sandbox: Sandbox,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    duplicate = dict(scenario.request)
    duplicate_id = _uuid(402)
    duplicate["request_id"] = duplicate_id
    duplicate["request_created_at"] = "2026-07-15T00:00:05Z"
    path = scenario.estate / "requests" / f"{duplicate_id}.json"
    publish_immutable(path, canonical_json_bytes(duplicate))
    accepted = scenario.state.request_index[str(scenario.request["request_id"])]
    wal, state = _append(
        scenario.state.clean_prefix,
        scenario.state,
        "request_duplicate",
        request_id=duplicate_id,
        original_request_id=scenario.request["request_id"],
        binding_id=accepted["binding_id"],
        lease_epoch=accepted["lease_epoch"],
        session_id=accepted["session_id"],
        packet_sha256=accepted["packet_sha256"],
    )
    replace_file(
        scenario.estate / "run-ledger.jsonl",
        wal,
        expected_old=scenario.state.clean_prefix,
    )
    publish_projections(
        scenario.estate,
        state,
        allowed_prior=(scenario.state, state),
    )
    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    duplicate_fact = next(
        fact for fact in facts.request_facts if fact.request_id == duplicate_id
    )
    assert duplicate_fact.durable_status == "duplicate"
    assert duplicate_fact.canonical_bytes == canonical_json_bytes(duplicate)

    changed_identity = dict(duplicate)
    changed_identity["work"] = dict(duplicate["work"])
    changed_identity["work"]["raw_file_sha256"] = "0" * 64
    changed_identity_bytes = canonical_json_bytes(changed_identity)
    replace_file(
        path,
        changed_identity_bytes,
        expected_old=duplicate_fact.canonical_bytes,
    )
    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError, match="durable dedup identity"):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before
    replace_file(
        path,
        duplicate_fact.canonical_bytes,
        expected_old=changed_identity_bytes,
    )

    changed = dict(duplicate)
    changed["request_id"] = _uuid(403)
    replace_file(
        path,
        canonical_json_bytes(changed),
        expected_old=duplicate_fact.canonical_bytes,
    )
    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError, match="filename or bytes are not canonical"):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


def test_indexed_control_request_must_equal_durable_target_unchanged(
    sandbox: Sandbox,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    control_id = _uuid(404)
    control = {
        "schema_version": "hypothesis-tree-runtime-control-request/1.0.0",
        "control_id": control_id,
        "control_created_at": "2026-07-15T00:00:05Z",
        "target_daemon_incarnation_id": _uuid(1),
        "operation": "stop",
    }
    path = scenario.estate / "control" / "requests" / f"{control_id}.json"
    original = canonical_json_bytes(control)
    publish_immutable(path, original)
    wal, state = _append(
        scenario.state.clean_prefix,
        scenario.state,
        "control_stop_accepted",
        control_id=control_id,
        target_daemon_incarnation_id=_uuid(1),
    )
    replace_file(
        scenario.estate / "run-ledger.jsonl",
        wal,
        expected_old=scenario.state.clean_prefix,
    )
    publish_projections(
        scenario.estate,
        state,
        allowed_prior=(scenario.state, state),
    )
    changed = dict(control)
    changed["target_daemon_incarnation_id"] = _uuid(405)
    replace_file(path, canonical_json_bytes(changed), expected_old=original)
    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError, match="durable control truth"):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


def test_running_artifact_hash_is_retained_and_replay_fenced(
    sandbox: Sandbox,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    receipts = _receipt_objects(scenario)
    _publish_receipts(scenario, receipts, ("started", "ready"))
    _advance_running(scenario)

    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    session_fact = next(
        fact for fact in facts.session_facts if fact.session_id == scenario.session_id
    )
    hashes = {fact.name: fact.sha256 for fact in session_fact.artifacts}
    assert hashes == {
        "started": hashlib.sha256(
            read_exact_file(scenario.session_path / "started.json")
        ).hexdigest(),
        "ready": hashlib.sha256(
            read_exact_file(scenario.session_path / "ready.json")
        ).hexdigest(),
    }

    ready_path = scenario.session_path / "ready.json"
    old = read_exact_file(ready_path)
    changed = dict(receipts["ready"])
    changed["ready_at"] = "2026-07-15T00:00:08Z"
    replace_file(ready_path, canonical_json_bytes(changed), expected_old=old)
    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError, match="replay-frozen truth"):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


def test_degraded_durable_started_hash_requires_physical_receipt_without_mutation(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    receipts = _receipt_objects(scenario)
    _publish_receipts(scenario, receipts, ("started",))
    started_path = scenario.session_path / "started.json"
    started_sha = hashlib.sha256(read_exact_file(started_path)).hexdigest()
    work = next(
        value
        for key, value in scenario.state.bindings.items()
        if key != "runtime#kernel"
    )
    wal, degraded = _append(
        scenario.state.clean_prefix,
        scenario.state,
        "session_degraded",
        request_id=work["request_id"],
        binding_id=work["binding_id"],
        lease_epoch=1,
        session_id=scenario.session_id,
        started_sha256=started_sha,
        reason_code="wrapper-exit-observed-custody-held",
    )
    replace_file(
        scenario.estate / "run-ledger.jsonl",
        wal,
        expected_old=scenario.state.clean_prefix,
    )
    publish_projections(
        scenario.estate,
        degraded,
        allowed_prior=(scenario.state, degraded),
    )
    started_path.unlink()

    popen_calls = 0

    def forbidden_popen(*_args: object, **_kwargs: object) -> object:
        nonlocal popen_calls
        popen_calls += 1
        raise AssertionError("corrupt boot must not call Popen")

    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", forbidden_popen)
    with _separate_shared_holder(scenario.session_path / "custody.lock"):
        before = _physical_snapshot(scenario.estate)
        wal_path = scenario.estate / "run-ledger.jsonl"
        before_events = [
            record["event"]
            for record in parse_bytes(read_exact_file(wal_path)).records
        ]
        with pytest.raises(HtError, match="durable hash lacks physical artifact"):
            runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
        after_events = [
            record["event"]
            for record in parse_bytes(read_exact_file(wal_path)).records
        ]
        assert _physical_snapshot(scenario.estate) == before

    assert before_events == after_events
    assert before_events.count("session_degraded") == 1
    assert "daemon_adopted_session" not in after_events
    assert before_events.count("daemon_started") == after_events.count(
        "daemon_started"
    )
    assert popen_calls == 0


@pytest.mark.parametrize(
    "contradiction",
    (
        "ready-pid",
        "terminal-result-hash",
        "terminal-outcome",
        "process-exit-pids",
        "process-exit-hashes",
    ),
)
def test_process_artifact_cross_coherence_rejects_before_mutation(
    sandbox: Sandbox,
    contradiction: str,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    receipts = _receipt_objects(scenario)
    if contradiction == "ready-pid":
        receipts["ready"]["helper_pid"] = 203
    elif contradiction == "terminal-result-hash":
        receipts["terminal"]["result_sha256"] = "0" * 64
    elif contradiction == "terminal-outcome":
        receipts["terminal"]["outcome"] = "FAILED"
    elif contradiction == "process-exit-pids":
        receipts["process-exit"]["wrapper_pid"] = 204
    elif contradiction == "process-exit-hashes":
        receipts["process-exit"]["result_sha256"] = "0" * 64
    _publish_receipts(
        scenario,
        receipts,
        ("started", "ready", "result", "terminal", "process-exit"),
    )
    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


def test_boot_full_closure_with_nonzero_wait_crashes_and_retains_all_hashes(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    receipts = _receipt_objects(scenario)
    receipts["process-exit"]["wait_status"] = -9
    names = ("started", "ready", "result", "terminal", "process-exit")
    _publish_receipts(scenario, receipts, names)
    observed_hashes = {
        f"{name.replace('-', '_')}_sha256": hashlib.sha256(
            read_exact_file(scenario.session_path / f"{name}.json")
        ).hexdigest()
        for name in names
    }
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())

    state, facts = runtime_daemon._prepare_boot(
        scenario.estate, scenario.runtime_id
    )
    incarnation = _uuid(590)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=incarnation,
    )
    supervisors: dict[str, runtime_daemon.SessionSupervisor] = {}
    state = runtime_daemon.classify_boot_sessions(
        scenario.estate,
        state,
        facts,
        incarnation,
        supervisors,
    )

    session = state.session_index[scenario.session_id]
    assert session["lifecycle"] == "terminal"
    assert session["outcome"] == "crashed"
    assert session["terminal_reason_code"] == "process-exit-incoherent"
    assert {
        field: session[field]
        for field in (
            "started_sha256",
            "ready_sha256",
            "result_sha256",
            "terminal_sha256",
            "process_exit_sha256",
        )
    } == observed_hashes
    assert supervisors == {}
    events = [record["event"] for record in parse_bytes(state.clean_prefix).records]
    assert events.count("session_terminal") == 1


def test_planned_boot_resumes_same_binding_and_response_precedes_popen(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _planned_scenario(sandbox)
    capture = CapturingPopen(scenario.estate, str(scenario.request["request_id"]))
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", capture)
    state, facts = runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=_uuid(601),
    )
    wrappers: dict[str, object] = {}
    state = runtime_daemon.reconcile_boot_once(
        sandbox.root, scenario.estate, state, facts, wrappers
    )

    records = parse_bytes(state.clean_prefix).records
    assert [record["event"] for record in records].count("work_planned") == 1
    assert [record["event"] for record in records].count("work_claimed") == 1
    accepted = state.request_index[str(scenario.request["request_id"])]
    assert accepted["status"] == "accepted"
    assert accepted["binding_id"] == scenario.binding_id
    assert accepted["lease_epoch"] == 1
    assert set(wrappers) == {accepted["session_id"]}
    assert len(capture.calls) == 1


def test_planned_boot_drift_rejects_same_binding_without_identity_or_child(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _planned_scenario(sandbox)
    monkeypatch.setattr(
        runtime_daemon,
        "revalidate_work",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HtError("synthetic drift")),
    )
    monkeypatch.setattr(
        runtime_daemon,
        "uuid4",
        lambda: (_ for _ in ()).throw(AssertionError("drift allocated identity")),
    )
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())
    state, facts = runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=_uuid(602),
    )
    state = runtime_daemon.reconcile_boot_once(
        sandbox.root, scenario.estate, state, facts, {}
    )

    entry = state.request_index[str(scenario.request["request_id"])]
    binding = state.bindings[f"runtime/{scenario.binding_id}#synthetic"]
    assert entry["status"] == "rejected" and entry["binding_id"] == scenario.binding_id
    assert binding["admission_status"] == "rejected"
    assert binding["last_lease_epoch"] == 0
    assert binding["sessions"] == {}
    events = [record["event"] for record in parse_bytes(state.clean_prefix).records]
    assert events.count("work_planned") == 1
    assert "work_claimed" not in events and "session_starting" not in events


def test_identical_unseen_request_waits_for_planned_owner_then_duplicates(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _planned_scenario(sandbox)
    duplicate = dict(scenario.request)
    duplicate["request_id"] = _uuid(503)
    duplicate["request_created_at"] = "2026-07-15T00:00:01Z"
    duplicate_path = scenario.estate / "requests" / f"{duplicate['request_id']}.json"
    publish_immutable(duplicate_path, canonical_json_bytes(duplicate))
    capture = CapturingPopen(scenario.estate, str(scenario.request["request_id"]))
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", capture)

    state, facts = runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=_uuid(603),
    )
    wrappers: dict[str, object] = {}
    state = runtime_daemon.reconcile_boot_once(
        sandbox.root, scenario.estate, state, facts, wrappers
    )
    assert str(duplicate["request_id"]) not in state.request_index
    assert len(capture.calls) == 1

    wrappers.clear()
    state = runtime_daemon._process_requests(
        sandbox.root, scenario.estate, state, wrappers
    )
    duplicate_entry = state.request_index[str(duplicate["request_id"])]
    assert duplicate_entry["status"] == "duplicate"
    assert duplicate_entry["original_request_id"] == scenario.request["request_id"]
    events = [record["event"] for record in parse_bytes(state.clean_prefix).records]
    assert events.count("work_planned") == 1
    assert events.count("request_duplicate") == 1
    assert len(capture.calls) == 1


def test_multiple_boot_plans_are_filename_ordered_and_capacity_one(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _planned_scenario(sandbox)
    _mint_issue(sandbox)
    second = dict(scenario.request)
    second["request_id"] = _uuid(500)
    second["request_created_at"] = "2026-07-15T00:00:01Z"
    second["work"] = snapshot_work(Root(sandbox.root), "issue#I-2").as_dict()
    second_bytes = canonical_json_bytes(second)
    publish_immutable(
        scenario.estate / "requests" / f"{second['request_id']}.json",
        second_bytes,
    )
    second_binding = _uuid(504)
    wal, state = _append(
        scenario.state.clean_prefix,
        scenario.state,
        "work_planned",
        request_id=second["request_id"],
        request_sha256=hashlib.sha256(second_bytes).hexdigest(),
        binding_id=second_binding,
        dedup_key=derive_dedup_key(second),
        request=second,
    )
    replace_file(
        scenario.estate / "run-ledger.jsonl",
        wal,
        expected_old=scenario.state.clean_prefix,
    )
    publish_projections(
        scenario.estate,
        state,
        allowed_prior=(scenario.state, state),
    )

    calls: list[str] = []

    def capture(argv: list[str], **_options: object) -> _CapturedWrapper:
        session_id = argv[argv.index("--session") + 1]
        packet = strict_loads(
            read_exact_file(scenario.estate / "sessions" / session_id / "packet.json"),
            label="captured packet",
        )
        response = strict_loads(
            read_exact_file(
                scenario.estate / "responses" / f"{packet['request_id']}.json"
            ),
            label="captured response",
        )
        assert response["session_id"] == session_id
        calls.append(packet["request_id"])
        return _CapturedWrapper()

    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", capture)
    state, facts = runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=_uuid(604),
    )
    wrappers: dict[str, object] = {}
    state = runtime_daemon.reconcile_boot_once(
        sandbox.root, scenario.estate, state, facts, wrappers
    )
    assert calls == [second["request_id"]]
    assert state.request_index[str(scenario.request["request_id"])]["status"] == "planned"

    accepted = state.request_index[str(second["request_id"])]
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "session_terminal",
        request_id=second["request_id"],
        binding_id=second_binding,
        lease_epoch=accepted["lease_epoch"],
        session_id=accepted["session_id"],
        outcome="crashed",
        reason_code="popen-failed",
        started_sha256=None,
        ready_sha256=None,
        result_sha256=None,
        terminal_sha256=None,
        process_exit_sha256=None,
    )
    wrappers.clear()
    state = runtime_daemon.reconcile_boot_once(
        sandbox.root, scenario.estate, state, facts, wrappers
    )
    assert calls == [second["request_id"], scenario.request["request_id"]]
    assert state.request_index[str(scenario.request["request_id"])]["status"] == "accepted"
    claim_order = [
        record["request_id"]
        for record in parse_bytes(state.clean_prefix).records
        if record["event"] == "work_claimed"
    ]
    assert claim_order == [second["request_id"], scenario.request["request_id"]]


@pytest.mark.parametrize(
    "prefix",
    (
        "absent-directory",
        "empty",
        "packet",
        "packet-temp",
        "packet-linked",
        "launch-temp",
        "launch-linked",
        "complete",
    ),
)
def test_claimed_prefix_rolls_back_then_reclaims_epoch_two(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
) -> None:
    scenario = _scenario(sandbox, boundary="claimed")
    _publish_claim_prefix(scenario, prefix)
    capture = CapturingPopen(scenario.estate, str(scenario.request["request_id"]))
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", capture)
    state, facts = runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=_uuid(610),
    )
    state = runtime_daemon.reconcile_boot_once(
        sandbox.root, scenario.estate, state, facts, {}
    )

    work = next(
        binding
        for address, binding in state.bindings.items()
        if address != "runtime#kernel"
    )
    old = work["sessions"][scenario.session_id]
    new = work["sessions"][work["current_session_id"]]
    assert old["lifecycle"] == "abandoned"
    assert old["abandonment_reason_code"] == "boot-recovery-pre-start"
    assert new["lease_epoch"] == 2
    assert new["session_id"] != scenario.session_id
    assert state.request_index[str(scenario.request["request_id"])]["session_id"] == new["session_id"]
    assert read_exact_file(scenario.session_path / "packet.json") == scenario.packet_bytes
    assert read_exact_file(scenario.session_path / "launch.json") == scenario.launch_bytes
    events = [record["event"] for record in parse_bytes(state.clean_prefix).records]
    assert events.count("work_planned") == 1
    assert events.count("claim_rolled_back") == 1
    assert events.count("work_claimed") == 2
    assert len(capture.calls) == 1

    if prefix == "complete":
        audit = packet_view(sandbox.root, scenario.session_id)
        assert audit["lifecycle"] == "abandoned"
        assert audit["packet_sha256"] == old["packet_sha256"]


def test_claimed_second_probe_contention_is_no_write_and_no_child(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(sandbox, boundary="claimed")
    state, facts = runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=_uuid(620),
    )
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())
    with _separate_shared_holder(scenario.session_path / "custody.lock"):
        before = _physical_snapshot(scenario.estate)
        with pytest.raises(HtError, match="became held before rollback"):
            runtime_daemon.reconcile_boot_once(
                sandbox.root, scenario.estate, state, facts, {}
            )
        assert _physical_snapshot(scenario.estate) == before


class _SyntheticCrash(RuntimeError):
    pass


@pytest.mark.parametrize(
    "boundary",
    (
        "reconstruction",
        "rollback",
        "replacement-claim",
        "packet",
        "launch",
        "custody",
    ),
)
def test_claimed_recovery_crash_windows_never_reuse_lease_or_replan(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    scenario = _scenario(sandbox, boundary="claimed")
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())
    state, facts = runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=_uuid(701),
    )

    if boundary != "reconstruction":
        with monkeypatch.context() as crash_patch:
            crash_patch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())
            if boundary in {"rollback", "replacement-claim"}:
                original_append = runtime_daemon._append
                target_event = (
                    "claim_rolled_back" if boundary == "rollback" else "work_claimed"
                )

                def crash_after_append(
                    estate: Path,
                    current: ReplayState,
                    event: str,
                    **fields: object,
                ) -> ReplayState:
                    result = original_append(estate, current, event, **fields)
                    if event == target_event:
                        raise _SyntheticCrash(boundary)
                    return result

                crash_patch.setattr(runtime_daemon, "_append", crash_after_append)
            elif boundary in {"packet", "launch"}:
                original_publish = runtime_daemon.publish_immutable
                target_name = f"{boundary}.json"

                def crash_after_publish(path: Path, data: bytes) -> None:
                    original_publish(path, data)
                    if path.name == target_name:
                        raise _SyntheticCrash(boundary)

                crash_patch.setattr(runtime_daemon, "publish_immutable", crash_after_publish)
            else:
                original_custody = runtime_daemon.create_custody

                def crash_after_custody(path: Path) -> int:
                    fd = original_custody(path)
                    os.close(fd)
                    raise _SyntheticCrash(boundary)

                crash_patch.setattr(runtime_daemon, "create_custody", crash_after_custody)
            with pytest.raises(_SyntheticCrash, match=boundary):
                runtime_daemon.reconcile_boot_once(
                    sandbox.root, scenario.estate, state, facts, {}
                )

    capture = CapturingPopen(scenario.estate, str(scenario.request["request_id"]))
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", capture)
    restarted, restarted_facts = runtime_daemon._prepare_boot(
        scenario.estate, scenario.runtime_id
    )
    restarted = runtime_daemon._append(
        scenario.estate,
        restarted,
        "daemon_started",
        daemon_incarnation_id=_uuid(702),
    )
    restarted = runtime_daemon.reconcile_boot_once(
        sandbox.root, scenario.estate, restarted, restarted_facts, {}
    )

    work = next(
        binding
        for address, binding in restarted.bindings.items()
        if address != "runtime#kernel"
    )
    sessions = sorted(work["sessions"].values(), key=lambda item: item["lease_epoch"])
    leases = [session["lease_epoch"] for session in sessions]
    expected = [1, 2] if boundary in {"reconstruction", "rollback"} else [1, 2, 3]
    assert leases == expected
    assert len({session["session_id"] for session in sessions}) == len(sessions)
    assert all(session["lifecycle"] == "abandoned" for session in sessions[:-1])
    assert sessions[-1]["lifecycle"] == "starting"
    events = [record["event"] for record in parse_bytes(restarted.clean_prefix).records]
    assert events.count("work_planned") == 1
    assert events.count("work_claimed") == len(expected)
    assert events.count("claim_rolled_back") == len(expected) - 1
    assert len(capture.calls) == 1


@pytest.mark.parametrize(
    ("boundary", "expected_reason", "expected_recovery_created"),
    (
        ("starting", "starting-recovery-no-process", True),
        ("accepted", "custody-free-incomplete-closure", False),
    ),
)
def test_r3_classifies_free_starting_boundaries_without_respawn(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    expected_reason: str,
    expected_recovery_created: bool,
) -> None:
    scenario = _scenario(sandbox, boundary=boundary)
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    if boundary == "accepted":
        _publish_accepted_response(scenario)
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())
    state, facts = runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
    incarnation = _uuid(801)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=incarnation,
    )
    supervisors: dict[str, runtime_daemon.SessionSupervisor] = {}
    state = runtime_daemon.classify_boot_sessions(
        scenario.estate,
        state,
        facts,
        incarnation,
        supervisors,
    )

    request_id = str(scenario.request["request_id"])
    response = strict_loads(
        read_exact_file(scenario.estate / "responses" / f"{request_id}.json"),
        label="classified response",
    )
    session = state.session_index[scenario.session_id]
    assert response["status"] == "accepted"
    assert response["recovery_created"] is expected_recovery_created
    assert session["lifecycle"] == "terminal"
    assert session["outcome"] == "crashed"
    assert session["terminal_reason_code"] == expected_reason
    assert supervisors == {}
    assert all(
        record["event"] != "daemon_adopted_session"
        for record in parse_bytes(state.clean_prefix).records
    )


@pytest.mark.parametrize("interrupted_publication", (False, True))
def test_r3_repairs_missing_accepted_response_then_crashes_without_respawn(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_publication: bool,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    request_id = str(scenario.request["request_id"])
    response_path = scenario.estate / "responses" / f"{request_id}.json"
    assert not response_path.exists()
    if interrupted_publication:
        temporary = response_path.parent / (
            f".ht-publish-{response_path.name}-{_uuid(701)}"
        )
        temporary.write_bytes(b"interrupted-response")
        os.chmod(temporary, 0o600)

    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert request_id not in facts.accepted_responses_present_at_entry
    before = _physical_snapshot(scenario.estate)
    state, prepared_facts = runtime_daemon._prepare_boot(
        scenario.estate, scenario.runtime_id
    )
    assert _physical_snapshot(scenario.estate) == before
    assert not response_path.exists()
    assert request_id not in prepared_facts.accepted_responses_present_at_entry

    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())
    incarnation = _uuid(802)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=incarnation,
    )
    supervisors: dict[str, runtime_daemon.SessionSupervisor] = {}
    state = runtime_daemon.classify_boot_sessions(
        scenario.estate,
        state,
        prepared_facts,
        incarnation,
        supervisors,
    )
    response = strict_loads(read_exact_file(response_path), label="repaired response")
    assert response["status"] == "accepted" and response["recovery_created"] is False
    assert request_id not in prepared_facts.accepted_responses_present_at_entry
    session = state.session_index[scenario.session_id]
    assert session["lifecycle"] == "terminal"
    assert session["terminal_reason_code"] == "accepted-response-missing-at-boot"
    assert supervisors == {}
    assert not [
        path
        for path in response_path.parent.iterdir()
        if path.name.startswith(f".ht-publish-{response_path.name}-")
    ]


def test_recovery_created_acceptance_is_permanently_nonadoptable(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted", recovery_created=True)
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())

    state, facts = runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
    incarnation = _uuid(803)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=incarnation,
    )
    supervisors: dict[str, runtime_daemon.SessionSupervisor] = {}
    state = runtime_daemon.classify_boot_sessions(
        scenario.estate, state, facts, incarnation, supervisors
    )

    session = state.session_index[scenario.session_id]
    assert session["lifecycle"] == "terminal"
    assert session["terminal_reason_code"] == "starting-recovery-no-process"
    assert supervisors == {}
    assert "daemon_adopted_session" not in {
        record["event"] for record in parse_bytes(state.clean_prefix).records
    }


@pytest.mark.parametrize("outcome", ("SUCCEEDED", "FAILED"))
def test_boot_free_coherent_closure_advances_and_settles_role_outcome(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    receipts = _receipt_objects(scenario)
    _set_receipt_outcome(receipts, outcome)
    _publish_receipts(
        scenario,
        receipts,
        ("started", "ready", "result", "terminal", "process-exit"),
    )
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())

    state, facts = runtime_daemon._prepare_boot(scenario.estate, scenario.runtime_id)
    incarnation = _uuid(804)
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=incarnation,
    )
    supervisors: dict[str, runtime_daemon.SessionSupervisor] = {}
    state = runtime_daemon.classify_boot_sessions(
        scenario.estate, state, facts, incarnation, supervisors
    )

    session = state.session_index[scenario.session_id]
    assert session["lifecycle"] == "terminal"
    assert session["outcome"] == outcome
    assert session["terminal_reason_code"] is None
    assert supervisors == {}
    events = [record["event"] for record in parse_bytes(state.clean_prefix).records]
    assert events[-2:] == ["session_running", "session_terminal"]
    assert "daemon_adopted_session" not in events


def test_held_live_session_is_adopted_then_settled_from_same_fence(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    pending = dict(scenario.request)
    pending["request_id"] = _uuid(810)
    pending["request_created_at"] = "2026-07-15T00:00:10Z"
    publish_immutable(
        scenario.estate / "requests" / f"{pending['request_id']}.json",
        canonical_json_bytes(pending),
    )
    receipts = _receipt_objects(scenario)
    _publish_receipts(
        scenario,
        receipts,
        ("started", "ready", "result", "terminal", "process-exit"),
    )
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())

    with _separate_shared_holder(scenario.session_path / "custody.lock"):
        state, facts = runtime_daemon._prepare_boot(
            scenario.estate, scenario.runtime_id
        )
        incarnation = _uuid(805)
        state = runtime_daemon._append(
            scenario.estate,
            state,
            "daemon_started",
            daemon_incarnation_id=incarnation,
        )
        supervisors: dict[str, runtime_daemon.SessionSupervisor] = {}
        state = runtime_daemon.classify_boot_sessions(
            scenario.estate, state, facts, incarnation, supervisors
        )
        adopted = state.session_index[scenario.session_id]
        assert adopted["lifecycle"] == "running"
        assert adopted["supervising_daemon_incarnation_id"] == incarnation
        assert supervisors == {
            scenario.session_id: runtime_daemon.SessionSupervisor(
                process=None, adopted=True
            )
        }
        state = runtime_daemon._process_requests(
            sandbox.root,
            scenario.estate,
            state,
            supervisors,
        )
        assert pending["request_id"] not in state.request_index
        before = state.clean_prefix
        state = runtime_daemon._supervise(scenario.estate, state, supervisors)
        assert state.clean_prefix == before
        assert not custody_is_free(scenario.session_path / "custody.lock")

    state = runtime_daemon._supervise(scenario.estate, state, supervisors)
    settled = state.session_index[scenario.session_id]
    assert settled["lifecycle"] == "terminal"
    assert settled["outcome"] == "SUCCEEDED"
    assert supervisors == {}
    events = [record["event"] for record in parse_bytes(state.clean_prefix).records]
    assert events.count("daemon_adopted_session") == 1


def test_adopted_wrapper_pid_probe_records_one_degradation_only(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    receipts = _receipt_objects(scenario)
    _publish_receipts(scenario, receipts, ("started",))
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())
    monkeypatch.setattr(runtime_daemon, "_pid_is_absent", lambda _pid: True)

    with _separate_shared_holder(scenario.session_path / "custody.lock"):
        state, facts = runtime_daemon._prepare_boot(
            scenario.estate, scenario.runtime_id
        )
        incarnation = _uuid(806)
        state = runtime_daemon._append(
            scenario.estate,
            state,
            "daemon_started",
            daemon_incarnation_id=incarnation,
        )
        supervisors: dict[str, runtime_daemon.SessionSupervisor] = {}
        state = runtime_daemon.classify_boot_sessions(
            scenario.estate, state, facts, incarnation, supervisors
        )
        state = runtime_daemon._supervise(scenario.estate, state, supervisors)
        first = state.clean_prefix
        state = runtime_daemon._supervise(scenario.estate, state, supervisors)
        assert state.clean_prefix == first
        assert state.session_index[scenario.session_id]["degraded_recorded"] is True
        events = [record["event"] for record in parse_bytes(state.clean_prefix).records]
        assert events.count("session_degraded") == 1

    state = runtime_daemon._supervise(scenario.estate, state, supervisors)
    session = state.session_index[scenario.session_id]
    assert session["lifecycle"] == "terminal"
    assert session["terminal_reason_code"] == "wrapper-exit-missing"


def test_graceful_stop_preserves_custody_for_successor_adoption(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    monkeypatch.setattr(runtime_daemon, "_popen_wrapper", ForbiddenPopen())

    with _separate_shared_holder(scenario.session_path / "custody.lock"):
        state, facts = runtime_daemon._prepare_boot(
            scenario.estate, scenario.runtime_id
        )
        first_incarnation = _uuid(807)
        state = runtime_daemon._append(
            scenario.estate,
            state,
            "daemon_started",
            daemon_incarnation_id=first_incarnation,
        )
        first_supervisors: dict[str, runtime_daemon.SessionSupervisor] = {}
        state = runtime_daemon.classify_boot_sessions(
            scenario.estate,
            state,
            facts,
            first_incarnation,
            first_supervisors,
        )

        control_id = _uuid(808)
        control = {
            "schema_version": "hypothesis-tree-runtime-control-request/1.0.0",
            "control_id": control_id,
            "control_created_at": "2026-07-15T00:00:09Z",
            "target_daemon_incarnation_id": first_incarnation,
            "operation": "stop",
        }
        publish_immutable(
            scenario.estate / "control" / "requests" / f"{control_id}.json",
            canonical_json_bytes(control),
        )
        state, accepted_control = runtime_daemon._process_controls(
            scenario.estate, state
        )
        assert accepted_control == control_id
        state = runtime_daemon._append(
            scenario.estate,
            state,
            "daemon_stopped",
            daemon_incarnation_id=first_incarnation,
            control_id=control_id,
        )
        assert set(first_supervisors) == {scenario.session_id}
        assert not custody_is_free(scenario.session_path / "custody.lock")

        successor, successor_facts = runtime_daemon._prepare_boot(
            scenario.estate, scenario.runtime_id
        )
        second_incarnation = _uuid(809)
        successor = runtime_daemon._append(
            scenario.estate,
            successor,
            "daemon_started",
            daemon_incarnation_id=second_incarnation,
        )
        second_supervisors: dict[str, runtime_daemon.SessionSupervisor] = {}
        successor = runtime_daemon.classify_boot_sessions(
            scenario.estate,
            successor,
            successor_facts,
            second_incarnation,
            second_supervisors,
        )
        assert set(second_supervisors) == {scenario.session_id}
        assert (
            successor.session_index[scenario.session_id][
                "supervising_daemon_incarnation_id"
            ]
            == second_incarnation
        )

    successor = runtime_daemon._supervise(
        scenario.estate, successor, second_supervisors
    )
    assert successor.session_index[scenario.session_id]["lifecycle"] == "terminal"


@pytest.mark.parametrize(
    ("target_name", "prerequisites"),
    (
        ("started", ()),
        ("ready", ("started",)),
        ("result", ("started", "ready")),
        ("terminal", ("started", "ready", "result")),
        ("process-exit", ("started",)),
    ),
)
def test_accepted_starting_child_publication_temp_is_opaque_and_unchanged(
    sandbox: Sandbox,
    target_name: str,
    prerequisites: tuple[str, ...],
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    receipts = _receipt_objects(scenario)
    _publish_receipts(scenario, receipts, prerequisites)
    target = scenario.session_path / f"{target_name}.json"
    temporary = _publication_temp(target, 900 + len(prerequisites))

    before = _physical_snapshot(scenario.estate)
    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)

    assert _physical_snapshot(scenario.estate) == before
    assert not target.exists()
    inspection = next(
        item for item in facts.operation_inspections if item.path == target
    )
    assert inspection.final_exists is False
    assert inspection.temporary_names == (temporary.name,)

    temporary_before = _file_snapshot(temporary)
    state, prepared_facts = runtime_daemon._prepare_boot(
        scenario.estate,
        scenario.runtime_id,
    )
    incarnation = _uuid(970 + len(prerequisites))
    state = runtime_daemon._append(
        scenario.estate,
        state,
        "daemon_started",
        daemon_incarnation_id=incarnation,
    )
    supervisors: dict[str, runtime_daemon.SessionSupervisor] = {}
    state = runtime_daemon.classify_boot_sessions(
        scenario.estate,
        state,
        prepared_facts,
        incarnation,
        supervisors,
    )
    assert state.session_index[scenario.session_id]["lifecycle"] == "terminal"
    assert supervisors == {}
    assert _file_snapshot(temporary) == temporary_before
    assert not target.exists()


def test_running_linked_ready_publication_temp_is_ignored_unchanged(
    sandbox: Sandbox,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    receipts = _receipt_objects(scenario)
    _publish_receipts(scenario, receipts, ("started", "ready"))
    _advance_running(scenario)
    target = scenario.session_path / "ready.json"
    temporary = target.parent / f".ht-publish-{target.name}-{_uuid(920)}"
    os.link(target, temporary)

    before = _physical_snapshot(scenario.estate)
    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)

    assert _physical_snapshot(scenario.estate) == before
    inspection = next(
        item for item in facts.operation_inspections if item.path == target
    )
    assert inspection.final_exists is True
    assert inspection.linked_temporary_name == temporary.name


def test_terminal_linked_process_exit_publication_temp_is_ignored_unchanged(
    sandbox: Sandbox,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    receipts = _receipt_objects(scenario)
    names = ("started", "ready", "result", "terminal", "process-exit")
    _publish_receipts(scenario, receipts, names)
    running = _advance_running(scenario)
    _advance_terminal(scenario, running)
    target = scenario.session_path / "process-exit.json"
    temporary = target.parent / f".ht-publish-{target.name}-{_uuid(921)}"
    os.link(target, temporary)

    before = _physical_snapshot(scenario.estate)
    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)

    assert _physical_snapshot(scenario.estate) == before
    inspection = next(
        item for item in facts.operation_inspections if item.path == target
    )
    assert inspection.final_exists is True
    assert inspection.linked_temporary_name == temporary.name


@pytest.mark.parametrize(
    "boundary",
    ("claimed", "starting"),
)
def test_child_publication_temp_is_rogue_before_live_accepted_boundary(
    sandbox: Sandbox,
    boundary: str,
) -> None:
    scenario = _scenario(sandbox, boundary=boundary)
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publication_temp(scenario.session_path / "started.json", 930)

    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError, match="unexpected entries"):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


def test_child_publication_temp_is_rogue_at_terminal_no_process_boundary(
    sandbox: Sandbox,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    _advance_terminal(
        scenario,
        scenario.state,
        outcome="crashed",
        reason_code="popen-failed",
    )
    _publication_temp(scenario.session_path / "started.json", 931)

    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError, match="unexpected entries"):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


@pytest.mark.parametrize("target_name", ("packet", "launch"))
def test_daemon_preparation_temp_is_rogue_after_claimed_boundary(
    sandbox: Sandbox,
    target_name: str,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    _publication_temp(scenario.session_path / f"{target_name}.json", 935)

    # session_starting can be durable only after both daemon publications
    # returned through unlink+directory-fsync, so a later temp is not owned.
    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError, match="unexpected entries"):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


@pytest.mark.parametrize(
    "near_miss",
    ("unknown-target", "bad-uuid", "wrong-mode", "symlink", "hardlink", "directory"),
)
def test_child_publication_temp_near_miss_rejects_unchanged(
    sandbox: Sandbox,
    near_miss: str,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    target = scenario.session_path / "started.json"
    if near_miss == "unknown-target":
        temporary = scenario.session_path / (
            f".ht-publish-unknown.json-{_uuid(940)}"
        )
        temporary.write_bytes(b"opaque")
        os.chmod(temporary, 0o600)
    elif near_miss == "bad-uuid":
        temporary = scenario.session_path / ".ht-publish-started.json-not-a-uuid"
        temporary.write_bytes(b"opaque")
        os.chmod(temporary, 0o600)
    elif near_miss == "wrong-mode":
        temporary = _publication_temp(target, 941)
        os.chmod(temporary, 0o640)
    elif near_miss == "symlink":
        temporary = scenario.session_path / (
            f".ht-publish-started.json-{_uuid(942)}"
        )
        temporary.symlink_to("custody.lock")
    elif near_miss == "hardlink":
        source = sandbox.root / "publication-alias-source"
        source.write_bytes(b"opaque")
        os.chmod(source, 0o600)
        temporary = scenario.session_path / (
            f".ht-publish-started.json-{_uuid(943)}"
        )
        os.link(source, temporary)
    else:
        temporary = scenario.session_path / (
            f".ht-publish-started.json-{_uuid(944)}"
        )
        temporary.mkdir(mode=0o700)

    before = _physical_snapshot(scenario.estate)
    with pytest.raises(HtError):
        runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)
    assert _physical_snapshot(scenario.estate) == before


@pytest.mark.parametrize("queue_kind", ("request", "control-request"))
def test_public_submission_temp_without_durable_event_is_opaque_and_unchanged(
    sandbox: Sandbox,
    queue_kind: str,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    directory = (
        scenario.estate / "requests"
        if queue_kind == "request"
        else scenario.estate / "control" / "requests"
    )
    target = directory / f"{_uuid(950)}.json"
    temporary = _publication_temp(target, 951)

    before = _physical_snapshot(scenario.estate)
    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)

    assert _physical_snapshot(scenario.estate) == before
    assert not target.exists()
    inspection = next(
        item for item in facts.operation_inspections if item.path == target
    )
    assert inspection.final_exists is False
    assert inspection.temporary_names == (temporary.name,)


def test_public_request_linked_publication_temp_is_loaded_without_cleanup(
    sandbox: Sandbox,
) -> None:
    scenario = _scenario(sandbox, boundary="accepted")
    _publish_preparation(scenario, ("packet", "launch", "custody"))
    _publish_accepted_response(scenario)
    request = dict(scenario.request)
    request["request_id"] = _uuid(960)
    request["request_created_at"] = "2026-07-15T00:00:10Z"
    target = scenario.estate / "requests" / f"{request['request_id']}.json"
    publish_immutable(target, canonical_json_bytes(request))
    temporary = target.parent / f".ht-publish-{target.name}-{_uuid(961)}"
    os.link(target, temporary)

    before = _physical_snapshot(scenario.estate)
    facts = runtime_daemon._scan_boot(scenario.estate, scenario.runtime_id)

    assert _physical_snapshot(scenario.estate) == before
    fact = next(item for item in facts.request_facts if item.request_id == request["request_id"])
    assert fact.value == request
    inspection = next(
        item for item in facts.operation_inspections if item.path == target
    )
    assert inspection.linked_temporary_name == temporary.name
