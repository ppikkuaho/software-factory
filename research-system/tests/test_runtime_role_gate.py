"""Recipient-scenario proofs for the minimum capability-aware runtime gate."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

import pytest

from conftest import Sandbox
from ht import mutex
from ht.commands import role as role_commands
from ht.commands import runtime as runtime_commands
from ht.errors import HtError
from ht.paths import Root
from ht.runtime.replay import build_record
from ht.runtime.replay import replay
from ht.runtime.repository import snapshot_work
from ht.runtime.custody import try_instance_lock
import ht.runtime.daemon as runtime_daemon
from ht.runtime.views import read_state
from ht.runtime.wal import frame_record, parse_bytes


_B1_CHECKPOINT_FIELDS = {
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
}


def _runtime_init(sandbox: Sandbox) -> Path:
    result = sandbox.run("runtime", "init", "--json")
    assert result.returncode == 0, result.stderr
    return sandbox.root / "var/runtime"


def _role_init(sandbox: Sandbox) -> dict[str, object]:
    result = sandbox.run("role", "init", "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _b1_truth(estate: Path) -> dict[str, object]:
    checkpoint = json.loads((estate / "checkpoint.json").read_bytes())
    return {
        "wal": (estate / "run-ledger.jsonl").read_bytes(),
        "binding_ledger": (estate / "binding-ledger.json").read_bytes(),
        "checkpoint": {
            name: deepcopy(checkpoint[name]) for name in _B1_CHECKPOINT_FIELDS
        },
        "requests": sorted(path.name for path in (estate / "requests").iterdir()),
        "responses": sorted(path.name for path in (estate / "responses").iterdir()),
        "sessions": sorted(path.name for path in (estate / "sessions").iterdir()),
        "control_requests": sorted(
            path.name for path in (estate / "control/requests").iterdir()
        ),
        "control_responses": sorted(
            path.name for path in (estate / "control/responses").iterdir()
        ),
    }


def _estate_identity(
    estate: Path,
) -> tuple[tuple[str, int, int, int, int, int, bytes | None], ...]:
    rows = []
    for path in (estate, *sorted(estate.rglob("*"))):
        info = path.lstat()
        rows.append(
            (
                "." if path == estate else path.relative_to(estate).as_posix(),
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_nlink,
                info.st_mtime_ns,
                path.read_bytes() if stat.S_ISREG(info.st_mode) else None,
            )
        )
    return tuple(rows)


def _mint_synthetic_issue(sandbox: Sandbox) -> None:
    result = sandbox.run(
        "issue",
        "mint",
        "--title",
        "SYNTHETIC-ROLE-GATE-0",
        "--question",
        "SYNTHETIC-ROLE-GATE-1",
        "--done-definition",
        "SYNTHETIC-ROLE-GATE-2",
        "--provenance",
        "user-seed#synthetic-role-gate-0",
        "--lanes",
        "L4",
        role="pc",
    )
    assert result.returncode == 0, result.stderr


def test_public_role_init_is_recognized_by_status_without_b1_drift(
    sandbox: Sandbox,
) -> None:
    estate = _runtime_init(sandbox)
    before = _b1_truth(estate)

    initialized = _role_init(sandbox)
    assert initialized["status"] == "initialized"
    status = sandbox.run("runtime", "status", "--json")
    assert status.returncode == 0, status.stderr
    observed = json.loads(status.stdout)
    assert observed["runtime_id"] == initialized["runtime_id"]
    assert observed["daemon"] == {"status": "stopped"}
    ready = sandbox.run("runtime", "init", "--json")
    assert ready.returncode == 0, ready.stderr
    assert json.loads(ready.stdout) == {
        "status": "ready",
        "runtime_id": initialized["runtime_id"],
        "runtime_root": str(estate),
        "created": False,
    }
    existing = _role_init(sandbox)
    assert existing == {**initialized, "status": "existing"}
    assert _b1_truth(estate) == before


def test_upgraded_runtime_starts_stops_and_preserves_capability_checkpoint(
    sandbox: Sandbox,
) -> None:
    estate = _runtime_init(sandbox)
    initialized = _role_init(sandbox)
    checkpoint = json.loads((estate / "checkpoint.json").read_bytes())
    marker_fields = {
        name: checkpoint[name]
        for name in (
            "role_capability_sha256",
            "upgrade_base_seq",
            "upgrade_base_clean_wal_sha256",
            "upgrade_base_checkpoint_sha256",
            "upgrade_base_binding_ledger_sha256",
        )
    }

    started = sandbox.run("runtime", "start", "--background", "--json")
    assert started.returncode == 0, started.stderr
    assert json.loads(started.stdout)["status"] == "ready"
    try:
        running = sandbox.run("runtime", "status", "--json")
        assert running.returncode == 0, running.stderr
        assert json.loads(running.stdout)["daemon"]["status"] == "running"
        stopped = sandbox.run("runtime", "stop", "--json")
        assert stopped.returncode == 0, stopped.stderr
        assert json.loads(stopped.stdout)["status"] == "accepted"
    finally:
        final = sandbox.run("runtime", "status", "--json")
        if final.returncode == 0 and json.loads(final.stdout)["daemon"]["status"] == "running":
            sandbox.run("runtime", "stop", "--json")

    final = sandbox.run("runtime", "status", "--json")
    assert final.returncode == 0, final.stderr
    assert json.loads(final.stdout)["daemon"] == {"status": "stopped"}
    upgraded = json.loads((estate / "checkpoint.json").read_bytes())
    assert upgraded["schema_version"] == "hypothesis-tree-runtime-checkpoint/2.0.0"
    assert {name: upgraded[name] for name in marker_fields} == marker_fields
    assert _role_init(sandbox) == {**initialized, "status": "existing"}


def test_upgraded_losing_contender_tolerates_live_b1_checkpoint_temp(
    sandbox: Sandbox,
) -> None:
    estate = _runtime_init(sandbox)
    _role_init(sandbox)
    owner_fd = try_instance_lock(estate / ".ht-runtime.instance.lock")
    assert owner_fd is not None
    temporary = estate / f".ht-replace-checkpoint.json-{uuid4()}"
    temporary.write_bytes(b"live-owner-bytes")
    temporary.chmod(0o600)
    before = _estate_identity(estate)
    read_fd, write_fd = os.pipe()
    try:
        assert runtime_daemon.run(sandbox.root, write_fd) == 0
        assert json.loads(os.read(read_fd, 4096)) == {
            "schema_version": "hypothesis-tree-runtime-readiness/1.0.0",
            "status": "already-running",
        }
    finally:
        os.close(read_fd)
        os.close(owner_fd)
    assert _estate_identity(estate) == before


def test_upgraded_runtime_rejects_b1_submission_and_retry_without_mutation(
    sandbox: Sandbox,
) -> None:
    _mint_synthetic_issue(sandbox)
    estate = _runtime_init(sandbox)
    _role_init(sandbox)
    before = _b1_truth(estate)

    submitted = sandbox.run(
        "runtime", "request", "--work-ref", "issue#I-1", "--json"
    )
    assert submitted.returncode == 2
    assert "role-runtime-upgraded" in submitted.stderr
    retried = sandbox.run("runtime", "retry", str(uuid4()), "--json")
    assert retried.returncode == 2
    assert "role-runtime-upgraded" in retried.stderr
    assert _b1_truth(estate) == before


def test_hidden_activation_prefix_blocks_runtime_surfaces_without_mutation(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mint_synthetic_issue(sandbox)
    estate = _runtime_init(sandbox)

    def fail_after_hidden_publish(stage: str) -> None:
        if stage == "hidden-published":
            raise RuntimeError("injected crash after hidden publish")

    monkeypatch.setattr(role_commands, "_fault_point", fail_after_hidden_publish)
    with mutex.global_mutex(Root(sandbox.root)):
        with pytest.raises(RuntimeError, match="injected crash"):
            role_commands.init(Root(sandbox.root), as_json=True)
    before = _estate_identity(estate)
    identifier = str(uuid4())
    surfaces = (
        ("init", sandbox.run("runtime", "init", "--json")),
        ("status", sandbox.run("runtime", "status", "--json")),
        (
            "request",
            sandbox.run(
                "runtime", "request", "--work-ref", "issue#I-1", "--json"
            ),
        ),
        ("retry", sandbox.run("runtime", "retry", identifier, "--json")),
        (
            "response",
            sandbox.run(
                "runtime", "response", "show", "--request", identifier, "--json"
            ),
        ),
        (
            "packet",
            sandbox.run(
                "runtime", "packet", "show", "--session", identifier, "--json"
            ),
        ),
        ("wait", sandbox.run("runtime", "wait", identifier, "--timeout", "0", "--json")),
        ("stop", sandbox.run("runtime", "stop", "--json")),
        ("start", sandbox.run("runtime", "start", "--background", "--json")),
    )
    for name, result in surfaces:
        assert result.returncode == 2, (name, result.stdout, result.stderr)
        if name == "start":
            assert json.loads(result.stdout)["reason_code"] == "role-init-required"
        else:
            assert "role-init-required" in result.stderr, name
        assert _estate_identity(estate) == before, name


def test_hidden_prefix_runtime_init_preserves_unrelated_genesis_temp(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    estate = _runtime_init(sandbox)

    def fail_after_hidden_publish(stage: str) -> None:
        if stage == "hidden-published":
            raise RuntimeError("injected crash after hidden publish")

    monkeypatch.setattr(role_commands, "_fault_point", fail_after_hidden_publish)
    with mutex.global_mutex(Root(sandbox.root)):
        with pytest.raises(RuntimeError, match="injected crash"):
            role_commands.init(Root(sandbox.root), as_json=True)
    temporary = estate / f".ht-publish-.ht-runtime.genesis.json-{uuid4()}"
    temporary.write_bytes(b"unowned-genesis-temp")
    temporary.chmod(0o600)
    before = _estate_identity(estate)
    result = sandbox.run("runtime", "init", "--json")
    assert result.returncode == 2
    assert temporary.exists()
    assert _estate_identity(estate) == before


def test_post_upgrade_synthetic_record_rejects_before_wal_construction(
    sandbox: Sandbox,
) -> None:
    estate = _runtime_init(sandbox)
    _role_init(sandbox)
    state = read_state(sandbox.root)
    before = _estate_identity(estate)
    with pytest.raises(HtError, match="role-runtime-upgraded"):
        build_record(
            state,
            "work_planned",
            "2026-07-16T00:00:00Z",
            request_id=str(uuid4()),
            request_sha256="0" * 64,
            binding_id=str(uuid4()),
            dedup_key="1" * 64,
            role="synthetic-kernel-v1",
            attempt=1,
            retry_lineage=[],
            work={},
        )
    assert _estate_identity(estate) == before


def test_unindexed_b1_request_after_upgrade_fails_daemon_boot_before_wal(
    sandbox: Sandbox,
) -> None:
    _mint_synthetic_issue(sandbox)
    estate = _runtime_init(sandbox)
    _role_init(sandbox)
    request, data = runtime_commands._new_request(
        snapshot_work(Root(sandbox.root), "issue#I-1")
    )
    path = estate / "requests" / f"{request['request_id']}.json"
    path.write_bytes(data)
    path.chmod(0o600)
    wal_before = (estate / "run-ledger.jsonl").read_bytes()
    checkpoint_before = (estate / "checkpoint.json").read_bytes()

    started = sandbox.run("runtime", "start", "--background", "--json")
    assert started.returncode == 2
    assert json.loads(started.stdout)["reason_code"] == "recovery-failed"
    assert (estate / "run-ledger.jsonl").read_bytes() == wal_before
    assert (estate / "checkpoint.json").read_bytes() == checkpoint_before
    assert path.read_bytes() == data


def test_upgraded_daemon_recovers_tolerated_tail_with_v2_context(
    sandbox: Sandbox,
) -> None:
    estate = _runtime_init(sandbox)
    _role_init(sandbox)
    marker = json.loads((estate / "role-capability.json").read_bytes())
    wal = estate / "run-ledger.jsonl"
    wal.write_bytes(wal.read_bytes() + b"broken-final-frame")

    started = sandbox.run("runtime", "start", "--background", "--json")
    assert started.returncode == 0, (started.stdout, started.stderr)
    try:
        stopped = sandbox.run("runtime", "stop", "--json")
        assert stopped.returncode == 0, stopped.stderr
    finally:
        current = sandbox.run("runtime", "status", "--json")
        if current.returncode == 0 and json.loads(current.stdout)["daemon"]["status"] == "running":
            sandbox.run("runtime", "stop", "--json")

    status = sandbox.run("runtime", "status", "--json")
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["daemon"] == {"status": "stopped"}
    assert payload["final_tail"] is not None
    checkpoint = json.loads((estate / "checkpoint.json").read_bytes())
    assert checkpoint["schema_version"] == "hypothesis-tree-runtime-checkpoint/2.0.0"
    assert checkpoint["role_capability_sha256"] == hashlib.sha256(
        (estate / "role-capability.json").read_bytes()
    ).hexdigest()
    assert checkpoint["upgrade_base_seq"] == marker["upgrade_base_seq"]


def test_upgraded_daemon_recovers_ledger_first_projection_with_fresh_gate(
    sandbox: Sandbox,
) -> None:
    estate = _runtime_init(sandbox)
    _role_init(sandbox)
    checkpoint_before = (estate / "checkpoint.json").read_bytes()
    state = read_state(sandbox.root)
    record = build_record(
        state,
        "daemon_started",
        "2026-07-16T00:00:00Z",
        daemon_incarnation_id=str(uuid4()),
    )
    wal = frame_record(record)
    target = replay(parse_bytes(wal), state.runtime_id, upgrade=state.upgrade)
    (estate / "run-ledger.jsonl").write_bytes(wal)
    (estate / "binding-ledger.json").write_bytes(target.binding_bytes())
    assert (estate / "checkpoint.json").read_bytes() == checkpoint_before

    started = sandbox.run("runtime", "start", "--background", "--json")
    assert started.returncode == 0, (started.stdout, started.stderr)
    try:
        stopped = sandbox.run("runtime", "stop", "--json")
        assert stopped.returncode == 0, stopped.stderr
    finally:
        current = sandbox.run("runtime", "status", "--json")
        if current.returncode == 0 and json.loads(current.stdout)["daemon"]["status"] == "running":
            sandbox.run("runtime", "stop", "--json")
    final = sandbox.run("runtime", "status", "--json")
    assert final.returncode == 0, final.stderr
    assert json.loads(final.stdout)["daemon"] == {"status": "stopped"}
    checkpoint = json.loads((estate / "checkpoint.json").read_bytes())
    assert checkpoint["schema_version"] == "hypothesis-tree-runtime-checkpoint/2.0.0"
    assert checkpoint["last_seq"] >= 4


def test_unupgraded_b1_status_remains_exactly_supported(sandbox: Sandbox) -> None:
    estate = _runtime_init(sandbox)
    before = _b1_truth(estate)
    status = sandbox.run("runtime", "status", "--json")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["daemon"] == {"status": "stopped"}
    assert _b1_truth(estate) == before
