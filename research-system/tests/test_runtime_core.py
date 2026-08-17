"""Direct Slice-A proofs for the non-process B1 runtime substrate."""

from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
from pathlib import Path
import stat
from uuid import uuid4

import pytest

from conftest import Sandbox
from ht.errors import HtError
from ht.paths import Root
from ht.runtime.atomic import (
    publish_immutable,
    read_exact_file,
    recover_immutable_publication,
    replace_file,
)
import ht.runtime.atomic as runtime_atomic
import ht.runtime.replay as runtime_replay
import ht.runtime.repository as runtime_repository
from ht.runtime.replay import (
    build_record,
    projection_eligibility,
    publish_projections,
    recover_tolerated_tail,
    replay,
    require_current_projections,
)
from ht.runtime.repository import snapshot_work
from ht.runtime.schema import canonical_json_bytes, strict_loads, validate
from ht.runtime.state import derive_dedup_key, genesis_bindings
from ht.runtime.wal import ParsedWal, frame_record, parse_bytes
from ht.runtime.wal import crc32_for


def _mint_issue(sandbox: Sandbox) -> None:
    result = sandbox.run(
        "issue", "mint",
        "--title", "SYNTHETIC-CORE-0",
        "--question", "SYNTHETIC-CORE-1",
        "--done-definition", "SYNTHETIC-CORE-2",
        "--provenance", "user-seed#synthetic-core-0",
        "--lanes", "L4",
        role="pc",
    )
    assert result.returncode == 0, result.stderr


def _daemon_started(runtime_id: str) -> bytes:
    kernel = deepcopy(genesis_bindings(runtime_id)["runtime#kernel"])
    daemon_incarnation_id = str(uuid4())
    kernel["daemon_incarnation_id"] = daemon_incarnation_id
    return frame_record(
        {
            "seq": 1,
            "ts": "2026-07-15T00:00:00Z",
            "event": "daemon_started",
            "node_address": "runtime#kernel",
            "binding_delta": {"post_images": {"runtime#kernel": kernel}},
            "daemon_incarnation_id": daemon_incarnation_id,
        }
    )


def _append_runtime_event(
    wal: bytes, state: runtime_replay.ReplayState, event: str, **fields: object
) -> tuple[bytes, runtime_replay.ReplayState, dict[str, object]]:
    record = build_record(state, event, "2026-07-15T00:00:00Z", **fields)
    wal += frame_record(record)
    return wal, replay(parse_bytes(wal), state.runtime_id), record


def _raw_frame(record_without_crc: dict[str, object]) -> bytes:
    record = deepcopy(record_without_crc)
    record["crc32"] = crc32_for(record_without_crc)
    payload = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode()
    return str(len(payload)).encode() + b"\t" + payload + b"\n"


def _request_object(
    request_id: str,
    work: dict[str, object],
    *,
    attempt: int = 1,
    retry_lineage: list[str] | None = None,
    created_at: str = "2026-07-15T00:00:00Z",
) -> dict[str, object]:
    return {
        "schema_version": "hypothesis-tree-runtime-request/1.0.0",
        "request_id": request_id,
        "request_created_at": created_at,
        "role": "synthetic-kernel-v1",
        "attempt": attempt,
        "retry_lineage": retry_lineage or [],
        "work": deepcopy(work),
    }


def _plan_and_claim(
    wal: bytes,
    state: runtime_replay.ReplayState,
    request: dict[str, object],
) -> tuple[bytes, runtime_replay.ReplayState, dict[str, object]]:
    request_id = str(request["request_id"])
    binding_id = str(uuid4())
    session_id = str(uuid4())
    wrapper_id = str(uuid4())
    helper_id = str(uuid4())
    address = f"runtime/{binding_id}#synthetic"
    request_sha = hashlib.sha256(canonical_json_bytes(request)).hexdigest()
    wal, state, _ = _append_runtime_event(
        wal, state, "work_planned", request_id=request_id,
        request_sha256=request_sha, binding_id=binding_id,
        dedup_key=derive_dedup_key(request), request=request,
    )
    packet = {
        "schema_version": "hypothesis-tree-runtime-session-packet/1.0.0",
        "runtime_id": state.runtime_id, "request_id": request_id,
        "binding_id": binding_id, "node_address": address, "lease_epoch": 1,
        "session_id": session_id,
        "fence": {"binding_id": binding_id, "lease_epoch": 1, "session_id": session_id},
        "role": "synthetic-kernel-v1", "attempt": request["attempt"],
        "retry_lineage": request["retry_lineage"], "work": request["work"],
        "admission_repository_commit": "6" * 40,
        "wrapper_instance_id": wrapper_id, "helper_instance_id": helper_id,
        "packet_created_at": "2026-07-15T00:00:00Z",
    }
    packet_json = canonical_json_bytes(packet).decode()
    packet_sha = hashlib.sha256(packet_json.encode()).hexdigest()
    launch = {
        "schema_version": "hypothesis-tree-runtime-launch/1.0.0",
        "runtime_id": state.runtime_id, "request_id": request_id,
        "binding_id": binding_id, "node_address": address, "lease_epoch": 1,
        "session_id": session_id, "role": "synthetic-kernel-v1",
        "wrapper_instance_id": wrapper_id, "helper_instance_id": helper_id,
        "packet_relative_path": f"sessions/{session_id}/packet.json",
        "packet_sha256": packet_sha, "entrypoint_token": "ht-runtime-wrapper/1.0.0",
        "helper_entrypoint_token": "ht-runtime-synthetic-helper/1.0.0",
        "custody_protocol": "inherited-flock-open-description/1.0.0",
        "barrier_protocol": "private-pipe-start-token/1.0.0",
    }
    launch_json = canonical_json_bytes(launch).decode()
    fields = {
        "request_id": request_id, "binding_id": binding_id, "lease_epoch": 1,
        "session_id": session_id, "admission_repository_commit": "6" * 40,
        "packet": packet, "packet_canonical_json": packet_json,
        "packet_sha256": packet_sha, "launch": launch,
        "launch_canonical_json": launch_json,
        "launch_sha256": hashlib.sha256(launch_json.encode()).hexdigest(),
    }
    wal, state, _ = _append_runtime_event(wal, state, "work_claimed", **fields)
    return wal, state, fields


def test_strict_json_and_schema_reject_ambiguity() -> None:
    with pytest.raises(HtError, match="duplicate JSON key"):
        strict_loads(b'{"a":1,"a":2}')
    with pytest.raises(HtError, match="non-finite"):
        strict_loads(b'{"a":NaN}')
    descriptor = {
        "schema_version": "hypothesis-tree-runtime/1.0.0",
        "runtime_kind": "hypothesis-tree",
        "build_id": "ht-runtime-kernel/1.0.0",
        "runtime_id": str(uuid4()),
        "runtime_root": "/tmp/runtime",
        "repository_root": "/tmp/root",
        "created_at": "2026-07-15T00:00:00Z",
        "unknown": True,
    }
    with pytest.raises(HtError, match="Additional properties"):
        validate("descriptor.schema.json", descriptor)


def test_immutable_publication_is_exact_restart_only(tmp_path: Path) -> None:
    directory = tmp_path / "owned"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    target = directory / "value.json"
    payload = canonical_json_bytes({"token": "SYNTHETIC-0"})
    publish_immutable(target, payload)
    publish_immutable(target, payload)
    assert read_exact_file(target) == payload
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(HtError, match="conflicting immutable"):
        publish_immutable(target, canonical_json_bytes({"token": "SYNTHETIC-1"}))
    alias = directory / "alias.json"
    os.link(target, alias)
    with pytest.raises(HtError, match="hard links"):
        read_exact_file(target)
    alias.unlink()

    # Recover both crash windows: an unlinked complete temp and a temp already
    # hard-linked to its final name.
    pending_target = directory / "pending.json"
    pending_temp = directory / f".ht-publish-{pending_target.name}-{uuid4()}"
    pending_temp.write_bytes(payload)
    os.chmod(pending_temp, 0o600)
    recover_immutable_publication(pending_target, payload)
    assert not pending_target.exists()
    assert not pending_temp.exists()
    publish_immutable(pending_target, payload)
    assert pending_target.read_bytes() == payload
    linked_temp = directory / f".ht-publish-{target.name}-{uuid4()}"
    os.link(target, linked_temp)
    recover_immutable_publication(target, payload)
    assert not linked_temp.exists()
    assert target.stat().st_nlink == 1

    projection = directory / "projection.json"
    publish_immutable(projection, canonical_json_bytes({"seq": 0}))
    replacement = canonical_json_bytes({"seq": 1})
    replace_temp = directory / f".ht-replace-{projection.name}-{uuid4()}"
    replace_temp.write_bytes(b"partial older replacement")
    os.chmod(replace_temp, 0o600)
    replace_file(
        projection,
        replacement,
        expected_old=canonical_json_bytes({"seq": 0}),
    )
    assert projection.read_bytes() == replacement
    assert not replace_temp.exists()

    symlink = directory / "symlink.json"
    symlink.symlink_to(target.name)
    with pytest.raises(HtError, match="non-symlink"):
        publish_immutable(symlink, payload)


def test_exact_existing_publish_fsyncs_directory_then_revalidates_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir(mode=0o700)
    target = directory / "value.json"
    payload = canonical_json_bytes({"token": "SYNTHETIC-RESTART"})
    publish_immutable(target, payload)
    before = target.stat()
    calls: list[tuple[str, Path]] = []
    real_read = runtime_atomic.read_exact_file

    def observed_fsync(path: Path) -> None:
        calls.append(("fsync", path))

    def observed_read(path: Path, **kwargs: object) -> bytes:
        calls.append(("read", path))
        return real_read(path, **kwargs)

    monkeypatch.setattr(runtime_atomic, "fsync_directory", observed_fsync)
    monkeypatch.setattr(runtime_atomic, "read_exact_file", observed_read)
    publish_immutable(target, payload)

    assert ("fsync", directory) in calls
    barrier = calls.index(("fsync", directory))
    assert calls[barrier + 1 :]
    assert all(call == ("read", target) for call in calls[barrier + 1 :])
    after = target.stat()
    assert target.read_bytes() == payload
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o600
    assert after.st_mtime_ns == before.st_mtime_ns


def test_exact_existing_replace_fsyncs_directory_then_revalidates_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir(mode=0o700)
    target = directory / "projection.json"
    payload = canonical_json_bytes({"seq": 1})
    target.write_bytes(payload)
    os.chmod(target, 0o600)
    before = target.stat()
    calls: list[tuple[str, Path]] = []
    real_read = runtime_atomic.read_exact_file

    def observed_fsync(path: Path) -> None:
        calls.append(("fsync", path))

    def observed_read(path: Path, **kwargs: object) -> bytes:
        calls.append(("read", path))
        return real_read(path, **kwargs)

    monkeypatch.setattr(runtime_atomic, "fsync_directory", observed_fsync)
    monkeypatch.setattr(runtime_atomic, "read_exact_file", observed_read)
    replace_file(target, payload, expected_old=canonical_json_bytes({"seq": 0}))

    assert calls == [("read", target), ("fsync", directory), ("read", target)]
    after = target.stat()
    assert target.read_bytes() == payload
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o600
    assert after.st_mtime_ns == before.st_mtime_ns


def test_runtime_init_discards_partial_uncommitted_marker(sandbox: Sandbox) -> None:
    runtime = sandbox.root / "var/runtime"
    runtime.parent.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    os.chmod(runtime, 0o700)
    partial = runtime / f".ht-publish-.ht-runtime.genesis.json-{uuid4()}"
    partial.write_bytes(b'{"partial":')
    os.chmod(partial, 0o600)
    result = sandbox.run("runtime", "init", "--json")
    assert result.returncode == 0, result.stderr
    assert not partial.exists()
    assert json.loads(result.stdout)["status"] == "ready"


def test_concurrent_exact_publishers_converge_during_commit_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir(mode=0o700)
    target = directory / "value.json"
    payload = canonical_json_bytes({"token": "SYNTHETIC-CONCURRENT"})
    real_link = os.link
    linked = __import__("threading").Event()
    release = __import__("threading").Event()
    first = True

    def paused_link(source: Path, destination: Path, **kwargs: object) -> None:
        nonlocal first
        real_link(source, destination, **kwargs)
        if first:
            first = False
            linked.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr("ht.runtime.atomic.os.link", paused_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(publish_immutable, target, payload)
        assert linked.wait(timeout=5)
        loser = pool.submit(publish_immutable, target, payload)
        loser.result(timeout=5)
        release.set()
        winner.result(timeout=5)
    assert read_exact_file(target) == payload
    assert target.stat().st_nlink == 1


def test_replace_rejects_unauthorized_or_malformed_final_without_cleanup(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir(mode=0o700)
    target = directory / "projection.json"
    old = canonical_json_bytes({"seq": 0})
    new = canonical_json_bytes({"seq": 1})
    target.write_bytes(b"corrupt")
    os.chmod(target, 0o600)
    pending = directory / f".ht-replace-{target.name}-{uuid4()}"
    pending.write_bytes(new)
    os.chmod(pending, 0o600)
    with pytest.raises(HtError, match="authorized old state"):
        replace_file(target, new, expected_old=old)
    assert target.read_bytes() == b"corrupt"
    assert pending.exists()

    target.unlink()
    target.symlink_to(pending.name)
    with pytest.raises(HtError, match="non-symlink"):
        replace_file(target, new, expected_old=old)
    assert target.is_symlink()


def test_replace_rejects_wrong_mode_and_link_alias(tmp_path: Path) -> None:
    directory = tmp_path / "owned"
    directory.mkdir(mode=0o700)
    target = directory / "projection.json"
    old = canonical_json_bytes({"seq": 0})
    new = canonical_json_bytes({"seq": 1})
    target.write_bytes(old)
    os.chmod(target, 0o640)
    with pytest.raises(HtError, match="mode"):
        replace_file(target, new, expected_old=old)
    assert target.read_bytes() == old

    os.chmod(target, 0o600)
    alias = directory / "alias.json"
    os.link(target, alias)
    with pytest.raises(HtError, match="hard links"):
        replace_file(target, new, expected_old=old)
    assert target.read_bytes() == old
    alias.unlink()


def test_stable_read_rechecks_open_file_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "value.json"
    target.write_bytes(b"SYNTHETIC")
    os.chmod(target, 0o600)
    real_fstat = os.fstat
    calls = 0

    def changed_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            os.fchmod(fd, 0o640)
        return real_fstat(fd)

    monkeypatch.setattr(runtime_atomic.os, "fstat", changed_fstat)
    with pytest.raises(HtError, match="changed .*while being read"):
        read_exact_file(target)


def test_item8_frames_tail_and_nonfinal_corruption() -> None:
    runtime_id = str(uuid4())
    good = _daemon_started(runtime_id)
    parsed = parse_bytes(good)
    assert parsed.tail is None
    state = replay(parsed, runtime_id)
    assert state.last_seq == 1
    assert state.bindings["runtime#kernel"]["daemon_incarnation_id"]

    torn = parse_bytes(good + b"17\t{\"SYNTHETIC\":0}")
    assert torn.clean_prefix == good
    assert torn.tail is not None
    assert torn.tail.reason == "unterminated-final-segment"
    assert torn.tail.discarded_byte_count == len(b"17\t{\"SYNTHETIC\":0}")

    broken_final = parse_bytes(good + b"1\t{}\n")
    assert broken_final.clean_prefix == good
    assert broken_final.tail is not None
    with pytest.raises(HtError, match="corrupt non-final"):
        parse_bytes(b"1\t{}\n" + good)


def test_projection_recovery_is_ledger_first_checkpoint_last(sandbox: Sandbox) -> None:
    initialized = sandbox.run("runtime", "init", "--json")
    assert initialized.returncode == 0, initialized.stderr
    runtime = sandbox.root / "var/runtime"
    descriptor = json.loads((runtime / "runtime.json").read_text())
    frame = _daemon_started(descriptor["runtime_id"])
    wal = runtime / "run-ledger.jsonl"
    wal.write_bytes(frame)
    parsed = parse_bytes(frame)
    eligibility = projection_eligibility(
        parsed,
        descriptor["runtime_id"],
        (runtime / "checkpoint.json").read_bytes(),
        (runtime / "binding-ledger.json").read_bytes(),
    )
    assert eligibility.checkpoint_state.last_seq == 0
    assert eligibility.full_state.last_seq == 1
    assert not eligibility.ledger_at_full_target
    with pytest.raises(HtError, match="recovery"):
        require_current_projections(runtime, parsed, descriptor["runtime_id"])

    # Model the only accepted between-replaces state, then finish checkpoint-last.
    (runtime / "binding-ledger.json").write_bytes(eligibility.full_state.binding_bytes())
    ahead = projection_eligibility(
        parsed,
        descriptor["runtime_id"],
        (runtime / "checkpoint.json").read_bytes(),
        (runtime / "binding-ledger.json").read_bytes(),
    )
    assert ahead.ledger_at_full_target
    publish_projections(
        runtime,
        ahead.full_state,
        allowed_prior=(ahead.checkpoint_state, ahead.full_state),
    )
    current = require_current_projections(runtime, parsed, descriptor["runtime_id"])
    assert current.last_seq == 1


def test_tolerated_tail_recovery_is_atomic_disclosure(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialized = sandbox.run("runtime", "init", "--json")
    assert initialized.returncode == 0, initialized.stderr
    runtime = sandbox.root / "var/runtime"
    descriptor = json.loads((runtime / "runtime.json").read_text())
    discarded = b"9\t{BROKEN}\n"
    (runtime / "run-ledger.jsonl").write_bytes(discarded)
    parsed = parse_bytes(discarded)
    assert parsed.tail is not None
    # A complete but uncommitted replacement from an older timestamp has no
    # authority and cannot freeze the retry's disclosure bytes.
    old_disclosure = frame_record(
        {
            "seq": 1,
            "ts": "2026-07-14T23:59:59Z",
            "event": "wal_tail_truncated",
            "node_address": "runtime#kernel",
            "binding_delta": {"post_images": {}},
            "tail_reason": parsed.tail.reason,
            "tail_byte_offset": parsed.tail.byte_offset,
            "tail_discarded_byte_count": parsed.tail.discarded_byte_count,
            "tail_discarded_sha256": parsed.tail.discarded_sha256,
        }
    )
    interrupted = runtime / f".ht-replace-run-ledger.jsonl-{uuid4()}"
    interrupted.write_bytes(old_disclosure)
    os.chmod(interrupted, 0o600)
    replacements: list[str] = []
    real_replace = runtime_replay.replace_file

    def observed_replace(path: Path, data: bytes, **kwargs: object) -> None:
        replacements.append(path.name)
        real_replace(path, data, **kwargs)

    monkeypatch.setattr(runtime_replay, "replace_file", observed_replace)
    target = recover_tolerated_tail(
        runtime,
        parsed,
        descriptor["runtime_id"],
        timestamp="2026-07-15T00:00:00Z",
    )
    recovered = parse_bytes((runtime / "run-ledger.jsonl").read_bytes())
    assert recovered.tail is None
    assert [record["event"] for record in recovered.records] == ["wal_tail_truncated"]
    assert target.final_tail == parsed.tail.as_dict()
    assert json.loads((runtime / "checkpoint.json").read_text())["last_seq"] == 1
    assert replacements[0] == "run-ledger.jsonl"
    assert not interrupted.exists()


def test_tail_recovery_finishes_only_distinguishable_checkpoint_last_before_wal(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialized = sandbox.run("runtime", "init", "--json")
    assert initialized.returncode == 0, initialized.stderr
    runtime = sandbox.root / "var/runtime"
    descriptor = json.loads((runtime / "runtime.json").read_text())
    wal_bytes = _daemon_started(descriptor["runtime_id"]) + b"BROKEN"
    (runtime / "run-ledger.jsonl").write_bytes(wal_bytes)
    parsed = parse_bytes(wal_bytes)
    eligibility = projection_eligibility(
        parsed,
        descriptor["runtime_id"],
        (runtime / "checkpoint.json").read_bytes(),
        (runtime / "binding-ledger.json").read_bytes(),
    )
    # Model the exact ledger-first/checkpoint-behind state W/C.
    (runtime / "binding-ledger.json").write_bytes(
        eligibility.full_state.binding_bytes()
    )
    replacements: list[str] = []
    real_replace = runtime_replay.replace_file

    def observed_replace(path: Path, data: bytes, **kwargs: object) -> None:
        replacements.append(path.name)
        real_replace(path, data, **kwargs)

    monkeypatch.setattr(runtime_replay, "replace_file", observed_replace)
    recover_tolerated_tail(
        runtime,
        parsed,
        descriptor["runtime_id"],
        timestamp="2026-07-15T00:00:00Z",
    )
    assert replacements[:2] == ["checkpoint.json", "run-ledger.jsonl"]


def test_typed_work_snapshot_proves_current_git_triplet(sandbox: Sandbox) -> None:
    _mint_issue(sandbox)
    snapshot = snapshot_work(Root(sandbox.root), "issue#I-1")
    assert snapshot.type == "issue"
    assert snapshot.canonical_ref == "issue#I-1"
    assert snapshot.repository_relpath == "tier1/issues/I-1.json"
    assert len(snapshot.canonical_object_sha256) == 64
    assert snapshot.head_blob_oid

    staged_mode = sandbox.git("update-index", "--chmod=+x", "--", snapshot.repository_relpath)
    assert staged_mode.returncode == 0, staged_mode.stderr
    with pytest.raises(HtError, match="stage-0 100644"):
        snapshot_work(Root(sandbox.root), "issue#I-1")
    restored_mode = sandbox.git("update-index", "--chmod=-x", "--", snapshot.repository_relpath)
    assert restored_mode.returncode == 0, restored_mode.stderr

    issue_path = sandbox.root / snapshot.repository_relpath
    original = issue_path.read_bytes()
    issue_path.write_bytes(original + b" ")
    with pytest.raises(HtError, match="worktree bytes differ"):
        snapshot_work(Root(sandbox.root), "issue#I-1")


def test_wal_wire_is_closed_and_malformed_shapes_are_normalized() -> None:
    runtime_id = str(uuid4())
    daemon_id = str(uuid4())
    kernel = deepcopy(genesis_bindings(runtime_id)["runtime#kernel"])
    kernel["daemon_incarnation_id"] = daemon_id
    base = {
        "seq": 1,
        "ts": "2026-07-15T00:00:00Z",
        "event": "daemon_started",
        "node_address": "runtime#kernel",
        "binding_delta": {"post_images": {"runtime#kernel": kernel}},
        "daemon_incarnation_id": daemon_id,
    }
    with pytest.raises(HtError, match="fields must be exactly"):
        frame_record({**base, "arbitrary_payload": "SYNTHETIC"})
    with pytest.raises(HtError, match="canonical UTC"):
        frame_record({**base, "ts": "2026-07-15T03:00:00+03:00"})
    with pytest.raises(HtError, match="unknown runtime WAL event"):
        frame_record({**base, "event": ["daemon_started"]})  # type: ignore[list-item]

    malformed = _raw_frame({**base, "binding_delta": []})
    parsed_final = parse_bytes(malformed)
    assert parsed_final.tail is not None
    assert parsed_final.tail.reason == "invalid-binding-delta"
    with pytest.raises(HtError, match="corrupt non-final"):
        parse_bytes(malformed + _daemon_started(runtime_id))


def test_reducer_derives_exact_multistep_lifecycle_and_rejects_unrelated_rewrite() -> None:
    runtime_id = str(uuid4())
    state = replay(parse_bytes(b""), runtime_id)
    wal = b""
    daemon_id = str(uuid4())
    wal, state, _ = _append_runtime_event(
        wal, state, "daemon_started", daemon_incarnation_id=daemon_id
    )

    request_id = str(uuid4())
    binding_id = str(uuid4())
    session_id = str(uuid4())
    wrapper_id = str(uuid4())
    helper_id = str(uuid4())
    address = f"runtime/{binding_id}#synthetic"
    work_identity = {
        "type": "issue",
        "canonical_ref": "issue#I-1",
        "repository_relpath": "tier1/issues/I-1.json",
        "canonical_object_sha256": "1" * 64,
        "raw_file_sha256": "2" * 64,
        "head_blob_oid": "3" * 40,
        "submission_repository_commit": "4" * 40,
        "git_object_format": "sha1",
    }
    request = {
        "schema_version": "hypothesis-tree-runtime-request/1.0.0",
        "request_id": request_id,
        "request_created_at": "2026-07-15T00:00:00Z",
        "role": "synthetic-kernel-v1",
        "attempt": 1,
        "retry_lineage": [],
        "work": work_identity,
    }
    request_sha = hashlib.sha256(canonical_json_bytes(request)).hexdigest()
    wal, state, _ = _append_runtime_event(
        wal,
        state,
        "work_planned",
        request_id=request_id,
        request_sha256=request_sha,
        binding_id=binding_id,
        dedup_key=derive_dedup_key(request),
        request=request,
    )
    packet = {
        "schema_version": "hypothesis-tree-runtime-session-packet/1.0.0",
        "runtime_id": runtime_id,
        "request_id": request_id,
        "binding_id": binding_id,
        "node_address": address,
        "lease_epoch": 1,
        "session_id": session_id,
        "fence": {"binding_id": binding_id, "lease_epoch": 1, "session_id": session_id},
        "role": "synthetic-kernel-v1",
        "attempt": 1,
        "retry_lineage": [],
        "work": work_identity,
        "admission_repository_commit": "6" * 40,
        "wrapper_instance_id": wrapper_id,
        "helper_instance_id": helper_id,
        "packet_created_at": "2026-07-15T00:00:00Z",
    }
    packet_json = canonical_json_bytes(packet).decode()
    packet_sha = hashlib.sha256(packet_json.encode()).hexdigest()
    launch = {
        "schema_version": "hypothesis-tree-runtime-launch/1.0.0",
        "runtime_id": runtime_id,
        "request_id": request_id,
        "binding_id": binding_id,
        "node_address": address,
        "lease_epoch": 1,
        "session_id": session_id,
        "role": "synthetic-kernel-v1",
        "wrapper_instance_id": wrapper_id,
        "helper_instance_id": helper_id,
        "packet_relative_path": f"sessions/{session_id}/packet.json",
        "packet_sha256": packet_sha,
        "entrypoint_token": "ht-runtime-wrapper/1.0.0",
        "helper_entrypoint_token": "ht-runtime-synthetic-helper/1.0.0",
        "custody_protocol": "inherited-flock-open-description/1.0.0",
        "barrier_protocol": "private-pipe-start-token/1.0.0",
    }
    launch_json = canonical_json_bytes(launch).decode()
    launch_sha = hashlib.sha256(launch_json.encode()).hexdigest()
    wal, state, _ = _append_runtime_event(
        wal,
        state,
        "work_claimed",
        request_id=request_id,
        binding_id=binding_id,
        lease_epoch=1,
        session_id=session_id,
        admission_repository_commit="6" * 40,
        packet=packet,
        packet_canonical_json=packet_json,
        packet_sha256=packet_sha,
        launch=launch,
        launch_canonical_json=launch_json,
        launch_sha256=launch_sha,
    )
    wal, state, _ = _append_runtime_event(
        wal, state, "session_starting", request_id=request_id,
        binding_id=binding_id, lease_epoch=1, session_id=session_id,
    )
    wal, state, _ = _append_runtime_event(
        wal, state, "request_accepted", request_id=request_id,
        binding_id=binding_id, lease_epoch=1, session_id=session_id,
        packet_sha256=packet_sha, recovery_created=False,
    )
    wal, state, _ = _append_runtime_event(
        wal, state, "session_running", request_id=request_id,
        binding_id=binding_id, lease_epoch=1, session_id=session_id,
        started_sha256="7" * 64, ready_sha256="8" * 64,
    )
    wal, state, _ = _append_runtime_event(
        wal, state, "session_terminal", request_id=request_id,
        binding_id=binding_id, lease_epoch=1, session_id=session_id,
        outcome="SUCCEEDED", reason_code=None, started_sha256="7" * 64,
        ready_sha256="8" * 64, result_sha256="9" * 64,
        terminal_sha256="a" * 64, process_exit_sha256="b" * 64,
    )
    assert state.bindings[address]["phase"] == "terminal"
    assert state.session_index[session_id]["outcome"] == "SUCCEEDED"
    assert state.request_index[request_id]["recovery_created"] is False

    # A second planned record is legal, but rewriting the first request entry
    # in its complete request-index post-image is not.
    request2 = deepcopy(request)
    request2["request_id"] = str(uuid4())
    request2["request_created_at"] = "2026-07-15T00:00:01Z"
    request2["work"]["raw_file_sha256"] = "c" * 64
    record = build_record(
        state,
        "work_planned",
        "2026-07-15T00:00:01Z",
        request_id=request2["request_id"],
        request_sha256=hashlib.sha256(canonical_json_bytes(request2)).hexdigest(),
        binding_id=str(uuid4()),
        dedup_key=derive_dedup_key(request2),
        request=request2,
    )
    record["index_delta"]["post_images"]["request_index"][request_id]["status"] = "rejected"
    with pytest.raises(HtError, match="sole legal transition|index post-images are malformed"):
        replay(parse_bytes(wal + frame_record(record)), runtime_id)


def test_ratified_process_schema_changes_are_closed() -> None:
    accepted = {
        "schema_version": "hypothesis-tree-runtime-admission-response/1.0.0",
        "status": "accepted",
        "request_id": str(uuid4()),
        "binding_id": str(uuid4()),
        "node_address": "runtime/synthetic#synthetic",
        "lease_epoch": 1,
        "session_id": str(uuid4()),
        "packet_sha256": "d" * 64,
    }
    with pytest.raises(HtError, match="rejects"):
        validate("admission-response.schema.json", accepted)
    accepted["recovery_created"] = False
    validate("admission-response.schema.json", accepted)

    result = {
        "schema_version": "hypothesis-tree-runtime-result/1.0.0",
        "runtime_id": str(uuid4()), "request_id": str(uuid4()),
        "binding_id": str(uuid4()), "lease_epoch": 1, "session_id": str(uuid4()),
        "packet_sha256": "e" * 64, "helper_instance_id": str(uuid4()),
        "outcome": "SUCCEEDED",
    }
    validate("result.schema.json", result)
    with pytest.raises(HtError, match="completed_at"):
        validate("result.schema.json", {**result, "completed_at": "2026-07-15T00:00:00Z"})


def test_genesis_marker_canonical_order_recovers_but_descriptor_only_never_repairs(
    sandbox: Sandbox,
) -> None:
    from ht.commands import runtime as runtime_command

    estate = sandbox.root / "var/runtime"
    estate.mkdir(parents=True, mode=0o700)
    os.chmod(estate.parent, 0o700)
    os.chmod(estate, 0o700)
    descriptor = runtime_command._descriptor(
        Root(sandbox.root), str(uuid4()), "2026-07-15T00:00:00Z"
    )
    marker = estate / runtime_command._MARKER
    publish_immutable(
        marker, canonical_json_bytes(runtime_command._marker_object(descriptor))
    )
    # Canonical serialization sorts the base64 map; recovery must not depend
    # on insertion order from the producer's in-memory dict.
    encoded = json.loads(marker.read_text())["expected_files_base64"]
    assert tuple(encoded) == tuple(sorted(runtime_command._FILE_ORDER))
    recovered = sandbox.run("runtime", "init", "--json")
    assert recovered.returncode == 0, recovered.stderr
    assert not marker.exists()

    missing = estate / "checkpoint.json"
    missing.unlink()
    rejected = sandbox.run("runtime", "init", "--json")
    assert rejected.returncode == 2
    assert "inventory differs from exact genesis" in rejected.stderr
    assert not missing.exists()


def test_exact_publisher_rechecks_stale_nlink_after_commit_temp_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir(mode=0o700)
    target = directory / "value.json"
    payload = canonical_json_bytes({"token": "SYNTHETIC-STABLE"})
    publish_immutable(target, payload)
    linked_temp = directory / f".ht-publish-{target.name}-{uuid4()}"
    os.link(target, linked_temp)
    real_owned = runtime_atomic._owned_temporaries
    cleaned = False

    def cleanup_between_lstat_and_scan(path: Path, operation: str) -> list[Path]:
        nonlocal cleaned
        if not cleaned and path == target and operation == "publish":
            cleaned = True
            linked_temp.unlink()
            return []
        return real_owned(path, operation)

    monkeypatch.setattr(runtime_atomic, "_owned_temporaries", cleanup_between_lstat_and_scan)
    publish_immutable(target, payload)
    assert read_exact_file(target) == payload
    assert target.stat().st_nlink == 1


def test_successor_daemon_replaces_crashed_incarnation_with_fresh_uuid() -> None:
    runtime_id = str(uuid4())
    old_id = str(uuid4())
    new_id = str(uuid4())
    wal = b""
    state = replay(parse_bytes(wal), runtime_id)
    wal, state, _ = _append_runtime_event(
        wal, state, "daemon_started", daemon_incarnation_id=old_id
    )
    wal, state, _ = _append_runtime_event(
        wal, state, "daemon_started", daemon_incarnation_id=new_id
    )
    assert state.bindings["runtime#kernel"]["daemon_incarnation_id"] == new_id
    with pytest.raises(HtError, match="fresh daemon incarnation"):
        build_record(state, "daemon_started", "2026-07-15T00:00:01Z", daemon_incarnation_id=new_id)
    # Root-level replay probe: the complete clean WAL independently preserves
    # the successor rather than requiring a graceful predecessor stop.
    assert replay(parse_bytes(wal), runtime_id).checkpoint_object()[
        "daemon_incarnation_id"
    ] == new_id


def test_exact_dedup_derivation_pending_uniqueness_and_retry_chain() -> None:
    runtime_id = str(uuid4())
    daemon_id = str(uuid4())
    work: dict[str, object] = {
        "type": "issue", "canonical_ref": "issue#I-1",
        "repository_relpath": "tier1/issues/I-1.json",
        "canonical_object_sha256": "1" * 64, "raw_file_sha256": "2" * 64,
        "head_blob_oid": "3" * 40, "submission_repository_commit": "4" * 40,
        "git_object_format": "sha1",
    }
    state = replay(parse_bytes(b""), runtime_id)
    wal = b""
    wal, state, _ = _append_runtime_event(
        wal, state, "daemon_started", daemon_incarnation_id=daemon_id
    )
    request_id = str(uuid4())
    request = _request_object(request_id, work)
    key = derive_dedup_key(request)
    changed_evidence = deepcopy(request)
    changed_evidence["request_id"] = str(uuid4())
    changed_evidence["request_created_at"] = "2026-07-15T00:00:01Z"
    changed_evidence["work"]["submission_repository_commit"] = "5" * 40  # type: ignore[index]
    assert derive_dedup_key(changed_evidence) == key
    changed_identity = deepcopy(request)
    changed_identity["work"]["raw_file_sha256"] = "9" * 64  # type: ignore[index]
    assert derive_dedup_key(changed_identity) != key

    request_sha = hashlib.sha256(canonical_json_bytes(request)).hexdigest()
    wal, planned, _ = _append_runtime_event(
        wal, state, "work_planned", request_id=request_id,
        request_sha256=request_sha, binding_id=str(uuid4()), dedup_key=key,
        request=request,
    )
    with pytest.raises(HtError, match="duplicates pending or accepted"):
        build_record(
            planned, "work_planned", "2026-07-15T00:00:01Z",
            request_id=changed_evidence["request_id"],
            request_sha256=hashlib.sha256(canonical_json_bytes(changed_evidence)).hexdigest(),
            binding_id=str(uuid4()), dedup_key=derive_dedup_key(changed_evidence),
            request=changed_evidence,
        )
    with pytest.raises(HtError, match="dedup key differs"):
        build_record(
            state, "work_planned", "2026-07-15T00:00:01Z", request_id=request_id,
            request_sha256=request_sha, binding_id=str(uuid4()), dedup_key="f" * 64,
            request=request,
        )

    # Complete the first request as an accepted infrastructure crash, which is
    # a legal retry predecessor.
    wal = wal[: parse_bytes(wal).frame_end_offsets[-2]]
    state = replay(parse_bytes(wal), runtime_id)
    wal, state, claim = _plan_and_claim(wal, state, request)
    identities = {name: claim[name] for name in ("request_id", "binding_id", "lease_epoch", "session_id")}
    wal, state, _ = _append_runtime_event(wal, state, "session_starting", **identities)
    wal, state, _ = _append_runtime_event(
        wal, state, "request_accepted", **identities,
        packet_sha256=claim["packet_sha256"], recovery_created=False,
    )
    wal, state, _ = _append_runtime_event(
        wal, state, "session_terminal", **identities, outcome="crashed",
        reason_code="popen-failed", started_sha256=None, ready_sha256=None,
        result_sha256=None, terminal_sha256=None, process_exit_sha256=None,
    )
    bad_attempt = _request_object(str(uuid4()), work, attempt=3, retry_lineage=[request_id])
    with pytest.raises(HtError, match="lineage length"):
        build_record(
            state, "work_planned", "2026-07-15T00:00:02Z",
            request_id=bad_attempt["request_id"],
            request_sha256=hashlib.sha256(canonical_json_bytes(bad_attempt)).hexdigest(),
            binding_id=str(uuid4()), dedup_key=derive_dedup_key(bad_attempt), request=bad_attempt,
        )
    missing = _request_object(str(uuid4()), work, attempt=2, retry_lineage=[str(uuid4())])
    with pytest.raises(HtError, match="predecessor is absent"):
        build_record(
            state, "work_planned", "2026-07-15T00:00:02Z",
            request_id=missing["request_id"],
            request_sha256=hashlib.sha256(canonical_json_bytes(missing)).hexdigest(),
            binding_id=str(uuid4()), dedup_key=derive_dedup_key(missing), request=missing,
        )
    wrong_identity_work = deepcopy(work)
    wrong_identity_work["repository_relpath"] = "tier1/issues/I-2.json"
    wrong_identity = _request_object(
        str(uuid4()), wrong_identity_work, attempt=2, retry_lineage=[request_id]
    )
    with pytest.raises(HtError, match="complete failed/crashed identity chain"):
        build_record(
            state, "work_planned", "2026-07-15T00:00:02Z",
            request_id=wrong_identity["request_id"],
            request_sha256=hashlib.sha256(canonical_json_bytes(wrong_identity)).hexdigest(),
            binding_id=str(uuid4()), dedup_key=derive_dedup_key(wrong_identity),
            request=wrong_identity,
        )
    retry_work = deepcopy(work)
    retry_work["submission_repository_commit"] = "5" * 40
    retry = _request_object(str(uuid4()), retry_work, attempt=2, retry_lineage=[request_id])
    record = build_record(
        state, "work_planned", "2026-07-15T00:00:02Z",
        request_id=retry["request_id"],
        request_sha256=hashlib.sha256(canonical_json_bytes(retry)).hexdigest(),
        binding_id=str(uuid4()), dedup_key=derive_dedup_key(retry), request=retry,
    )
    assert record["event"] == "work_planned"


def test_claim_identity_paths_terminal_and_adoption_gates() -> None:
    runtime_id = str(uuid4())
    daemon_id = str(uuid4())
    work: dict[str, object] = {
        "type": "issue", "canonical_ref": "issue#I-1",
        "repository_relpath": "tier1/issues/I-1.json",
        "canonical_object_sha256": "1" * 64, "raw_file_sha256": "2" * 64,
        "head_blob_oid": "3" * 40, "submission_repository_commit": "4" * 40,
        "git_object_format": "sha1",
    }
    state = replay(parse_bytes(b""), runtime_id)
    wal = b""
    wal, state, _ = _append_runtime_event(
        wal, state, "daemon_started", daemon_incarnation_id=daemon_id
    )
    request = _request_object(str(uuid4()), work)
    wal, claimed, claim = _plan_and_claim(wal, state, request)
    parsed = parse_bytes(wal)
    pre_claim_wal = wal[: parsed.frame_end_offsets[-2]]
    pre_claim = replay(parse_bytes(pre_claim_wal), runtime_id)

    same_owner = deepcopy(claim)
    same_owner["packet"]["wrapper_instance_id"] = daemon_id  # type: ignore[index]
    same_owner["launch"]["wrapper_instance_id"] = daemon_id  # type: ignore[index]
    for name in ("packet", "launch"):
        encoded = canonical_json_bytes(same_owner[name]).decode()
        same_owner[f"{name}_canonical_json"] = encoded
        same_owner[f"{name}_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
    same_owner["launch"]["packet_sha256"] = same_owner["packet_sha256"]  # type: ignore[index]
    launch_encoded = canonical_json_bytes(same_owner["launch"]).decode()
    same_owner["launch_canonical_json"] = launch_encoded
    same_owner["launch_sha256"] = hashlib.sha256(launch_encoded.encode()).hexdigest()
    with pytest.raises(HtError, match="must be distinct"):
        build_record(pre_claim, "work_claimed", "2026-07-15T00:00:01Z", **same_owner)

    wrong_path = deepcopy(claim)
    wrong_path["launch"]["packet_relative_path"] = f"sessions/{uuid4()}/packet.json"  # type: ignore[index]
    launch_encoded = canonical_json_bytes(wrong_path["launch"]).decode()
    wrong_path["launch_canonical_json"] = launch_encoded
    wrong_path["launch_sha256"] = hashlib.sha256(launch_encoded.encode()).hexdigest()
    with pytest.raises(HtError, match="artifacts disagree"):
        build_record(pre_claim, "work_claimed", "2026-07-15T00:00:01Z", **wrong_path)

    identities = {name: claim[name] for name in ("request_id", "binding_id", "lease_epoch", "session_id")}
    wal, starting, _ = _append_runtime_event(wal, claimed, "session_starting", **identities)
    with pytest.raises(HtError, match="requires the current started session"):
        build_record(
            starting, "session_terminal", "2026-07-15T00:00:01Z", **identities,
            outcome="crashed", reason_code="popen-failed", started_sha256=None,
            ready_sha256=None, result_sha256=None, terminal_sha256=None,
            process_exit_sha256=None,
        )
    wal, accepted, _ = _append_runtime_event(
        wal, starting, "request_accepted", **identities,
        packet_sha256=claim["packet_sha256"], recovery_created=False,
    )
    second_work = deepcopy(work)
    second_work["raw_file_sha256"] = "d" * 64
    second_request = _request_object(str(uuid4()), second_work)
    second_wal, second_claimed, second_claim = _plan_and_claim(
        wal, accepted, second_request
    )
    second_identity = {
        name: second_claim[name]
        for name in ("request_id", "binding_id", "lease_epoch", "session_id")
    }
    second_wal, second_starting, _ = _append_runtime_event(
        second_wal, second_claimed, "session_starting", **second_identity
    )
    collision = deepcopy(second_starting)
    collision.bindings[
        f"runtime/{second_identity['binding_id']}#synthetic"
    ]["dedup_key"] = derive_dedup_key(request)
    with pytest.raises(HtError, match="may not overwrite a dedup owner"):
        build_record(
            collision, "request_accepted", "2026-07-15T00:00:01Z",
            **second_identity, packet_sha256=second_claim["packet_sha256"],
            recovery_created=False,
        )
    with pytest.raises(HtError, match="role terminal outcome requires"):
        build_record(
            accepted, "session_terminal", "2026-07-15T00:00:01Z", **identities,
            outcome="SUCCEEDED", reason_code=None, started_sha256="7" * 64,
            ready_sha256="8" * 64, result_sha256="9" * 64,
            terminal_sha256="a" * 64, process_exit_sha256="b" * 64,
        )
    wal, degraded, _ = _append_runtime_event(
        wal, accepted, "session_degraded", **identities,
        started_sha256="7" * 64,
        reason_code="wrapper-exit-observed-custody-held",
    )
    with pytest.raises(HtError, match="started hash conflicts"):
        build_record(
            degraded, "session_running", "2026-07-15T00:00:01Z", **identities,
            started_sha256="6" * 64, ready_sha256="8" * 64,
        )
    wal, running, _ = _append_runtime_event(
        wal, degraded, "session_running", **identities,
        started_sha256="7" * 64, ready_sha256="8" * 64,
    )
    successor_id = str(uuid4())
    wal, successor, _ = _append_runtime_event(
        wal, running, "daemon_started", daemon_incarnation_id=successor_id
    )
    adoption = build_record(
        successor, "daemon_adopted_session", "2026-07-15T00:00:02Z",
        daemon_incarnation_id=successor_id, binding_id=identities["binding_id"],
        lease_epoch=1, session_id=identities["session_id"],
    )
    assert adoption["node_address"] == "runtime#kernel"

    wal, adopted, _ = _append_runtime_event(
        wal, successor, "daemon_adopted_session",
        daemon_incarnation_id=successor_id, binding_id=identities["binding_id"],
        lease_epoch=1, session_id=identities["session_id"],
    )
    with pytest.raises(HtError, match="not-yet-adopted"):
        build_record(
            adopted, "daemon_adopted_session", "2026-07-15T00:00:03Z",
            daemon_incarnation_id=successor_id, binding_id=identities["binding_id"],
            lease_epoch=1, session_id=identities["session_id"],
        )

    recovered_indexes = deepcopy(successor.request_index)
    recovered_indexes[str(claim["request_id"])]["recovery_created"] = True
    recovered = runtime_replay.ReplayState(
        successor.runtime_id, successor.last_seq, successor.clean_prefix,
        deepcopy(successor.bindings), recovered_indexes,
        deepcopy(successor.dedup_index), deepcopy(successor.session_index),
        deepcopy(successor.control_index), successor.final_tail,
    )
    with pytest.raises(HtError, match="live-created accepted request index"):
        build_record(
            recovered, "daemon_adopted_session", "2026-07-15T00:00:02Z",
            daemon_incarnation_id=successor_id, binding_id=identities["binding_id"],
            lease_epoch=1, session_id=identities["session_id"],
        )


def test_wal_parser_classifies_event_structural_defects_at_frame_boundary() -> None:
    runtime_id = str(uuid4())
    good = _daemon_started(runtime_id)
    kernel = deepcopy(genesis_bindings(runtime_id)["runtime#kernel"])
    kernel["daemon_incarnation_id"] = "not-a-uuid"
    malformed = _raw_frame(
        {
            "seq": 1, "ts": "2026-07-15T00:00:00Z", "event": "daemon_started",
            "node_address": "runtime#kernel",
            "binding_delta": {"post_images": {"runtime#kernel": kernel}},
            "daemon_incarnation_id": "not-a-uuid",
        }
    )
    assert parse_bytes(malformed).tail is not None
    with pytest.raises(HtError, match="corrupt non-final"):
        parse_bytes(malformed + good)


def test_nested_post_image_mutation_matrix_is_tail_only_at_final() -> None:
    runtime_id = str(uuid4())
    daemon_id = str(uuid4())
    wal = b""
    state = replay(parse_bytes(wal), runtime_id)
    wal, state, _ = _append_runtime_event(
        wal, state, "daemon_started", daemon_incarnation_id=daemon_id
    )
    work: dict[str, object] = {
        "type": "issue", "canonical_ref": "issue#I-1",
        "repository_relpath": "tier1/issues/I-1.json",
        "canonical_object_sha256": "1" * 64, "raw_file_sha256": "2" * 64,
        "head_blob_oid": "3" * 40, "submission_repository_commit": "4" * 40,
        "git_object_format": "sha1",
    }
    request = _request_object(str(uuid4()), work)
    wal, claimed, claim = _plan_and_claim(wal, state, request)
    identity = {
        name: claim[name]
        for name in ("request_id", "binding_id", "lease_epoch", "session_id")
    }
    wal, starting, _ = _append_runtime_event(
        wal, claimed, "session_starting", **identity
    )
    accepted = build_record(
        starting, "request_accepted", "2026-07-15T00:00:01Z", **identity,
        packet_sha256=claim["packet_sha256"], recovery_created=False,
    )
    valid_accepted = frame_record(accepted)
    address = f"runtime/{identity['binding_id']}#synthetic"
    session_id = str(identity["session_id"])
    request_id = str(identity["request_id"])
    dedup_key = starting.bindings[address]["dedup_key"]

    paths_and_values: tuple[tuple[tuple[str, ...], object], ...] = (
        (("binding_delta", "post_images", "runtime#kernel", "runtime_id"), "not-a-uuid"),
        (("binding_delta", "post_images", "runtime#kernel", "request_count"), True),
        (("binding_delta", "post_images", address, "work", "type"), "unknown"),
        (("binding_delta", "post_images", address, "work", "canonical_ref"), 7),
        (("binding_delta", "post_images", address, "sessions", session_id, "fence", "lease_epoch"), 2),
        (("binding_delta", "post_images", address, "sessions", session_id, "packet", "role"), "unknown"),
        (("binding_delta", "post_images", address, "sessions", session_id, "packet_canonical_json"), "{}\n"),
        (("binding_delta", "post_images", address, "sessions", session_id, "launch_sha256"), None),
        (("binding_delta", "post_images", address, "terminal_outcome"), "SUCCEEDED"),
        (("index_delta", "post_images", "request_index", request_id, "status"), "unknown"),
        (("index_delta", "post_images", "request_index", request_id, "node_address"), 7),
        (("index_delta", "post_images", "dedup_index", dedup_key, "request_id"), 7),
        (("index_delta", "post_images", "dedup_index", dedup_key, "lease_epoch"), True),
        (("index_delta", "post_images", "session_index", session_id, "outcome"), "SUCCEEDED"),
        (("index_delta", "post_images", "session_index", session_id, "terminal_reason_code"), []),
    )
    for path, replacement in paths_and_values:
        malformed = deepcopy(accepted)
        cursor: dict[str, object] = malformed
        for part in path[:-1]:
            cursor = cursor[part]  # type: ignore[assignment,index]
        cursor[path[-1]] = replacement
        bad = _raw_frame(malformed)
        parsed = parse_bytes(wal + bad)
        assert parsed.tail is not None, path
        with pytest.raises(HtError, match="corrupt non-final"):
            parse_bytes(wal + bad + valid_accepted)

    control_id = str(uuid4())
    control = build_record(
        state, "control_stop_accepted", "2026-07-15T00:00:01Z",
        control_id=control_id, target_daemon_incarnation_id=daemon_id,
    )
    valid_control = frame_record(control)
    malformed_control = deepcopy(control)
    malformed_control["index_delta"]["post_images"]["control_index"][control_id][  # type: ignore[index]
        "reason_code"
    ] = []
    bad_control = _raw_frame(malformed_control)
    daemon_prefix = _daemon_started(runtime_id)
    assert parse_bytes(daemon_prefix + bad_control).tail is not None
    with pytest.raises(HtError, match="corrupt non-final"):
        parse_bytes(daemon_prefix + bad_control + valid_control)


@pytest.mark.parametrize(
    "fence_location",
    ("work-session", "session-index"),
)
def test_boolean_nested_fence_lease_is_never_equal_to_epoch_one(
    fence_location: str,
) -> None:
    runtime_id = str(uuid4())
    wal = b""
    state = replay(parse_bytes(wal), runtime_id)
    wal, state, _ = _append_runtime_event(
        wal, state, "daemon_started", daemon_incarnation_id=str(uuid4())
    )
    request = _request_object(
        str(uuid4()),
        {
            "type": "issue",
            "canonical_ref": "issue#I-1",
            "repository_relpath": "tier1/issues/I-1.json",
            "canonical_object_sha256": "1" * 64,
            "raw_file_sha256": "2" * 64,
            "head_blob_oid": "3" * 40,
            "submission_repository_commit": "4" * 40,
            "git_object_format": "sha1",
        },
    )
    wal, claimed, claim = _plan_and_claim(wal, state, request)
    identity = {
        name: claim[name]
        for name in ("request_id", "binding_id", "lease_epoch", "session_id")
    }
    wal, starting, _ = _append_runtime_event(
        wal, claimed, "session_starting", **identity
    )
    accepted = build_record(
        starting,
        "request_accepted",
        "2026-07-15T00:00:01Z",
        **identity,
        packet_sha256=claim["packet_sha256"],
        recovery_created=False,
    )
    valid_frame = frame_record(accepted)
    address = f"runtime/{identity['binding_id']}#synthetic"
    session_id = str(identity["session_id"])

    poisoned = deepcopy(accepted)
    if fence_location == "work-session":
        poisoned["binding_delta"]["post_images"][address]["sessions"][session_id][  # type: ignore[index]
            "fence"
        ]["lease_epoch"] = True
    else:
        poisoned["index_delta"]["post_images"]["session_index"][session_id][  # type: ignore[index]
            "fence"
        ]["lease_epoch"] = True

    # The frame has a valid CRC: its malformed final placement is tolerated,
    # while the identical frame in a non-final position is hard corruption.
    poisoned_frame = _raw_frame(poisoned)
    final = parse_bytes(wal + poisoned_frame)
    assert final.tail is not None
    with pytest.raises(HtError, match="corrupt non-final"):
        parse_bytes(wal + poisoned_frame + valid_frame)

    # Replay remains an independent trust boundary even if a caller constructs
    # a ParsedWal without going through the byte parser.
    parsed_valid = parse_bytes(wal + valid_frame)
    records = [deepcopy(record) for record in parsed_valid.records]
    direct = deepcopy(poisoned)
    direct["crc32"] = crc32_for(poisoned)
    records[-1] = direct
    bypassed_parser = ParsedWal(
        tuple(records),
        parsed_valid.clean_prefix,
        None,
        parsed_valid.frame_end_offsets,
    )
    with pytest.raises(HtError, match="malformed runtime WAL transition"):
        replay(bypassed_parser, runtime_id)


def test_request_and_packet_git_object_format_lengths_are_cross_validated() -> None:
    runtime_id = str(uuid4())
    daemon_id = str(uuid4())
    wal = b""
    state = replay(parse_bytes(wal), runtime_id)
    wal, state, _ = _append_runtime_event(
        wal, state, "daemon_started", daemon_incarnation_id=daemon_id
    )
    work: dict[str, object] = {
        "type": "issue", "canonical_ref": "issue#I-1",
        "repository_relpath": "tier1/issues/I-1.json",
        "canonical_object_sha256": "1" * 64, "raw_file_sha256": "2" * 64,
        "head_blob_oid": "3" * 40, "submission_repository_commit": "4" * 40,
        "git_object_format": "sha256",
    }
    malformed_request = _request_object(str(uuid4()), work)
    with pytest.raises(HtError, match="64-hex sha256 Git OID"):
        build_record(
            state, "work_planned", "2026-07-15T00:00:01Z",
            request_id=malformed_request["request_id"],
            request_sha256=hashlib.sha256(canonical_json_bytes(malformed_request)).hexdigest(),
            binding_id=str(uuid4()), dedup_key=derive_dedup_key(malformed_request),
            request=malformed_request,
        )

    valid_work = deepcopy(work)
    valid_work["git_object_format"] = "sha1"
    request = _request_object(str(uuid4()), valid_work)
    claimed_wal, _claimed, claim = _plan_and_claim(wal, state, request)
    parsed = parse_bytes(claimed_wal)
    planned_wal = claimed_wal[: parsed.frame_end_offsets[-2]]
    planned = replay(parse_bytes(planned_wal), runtime_id)
    malformed_claim = deepcopy(claim)
    malformed_claim["packet"]["admission_repository_commit"] = "6" * 64  # type: ignore[index]
    malformed_claim["admission_repository_commit"] = "6" * 64
    packet_bytes = canonical_json_bytes(malformed_claim["packet"])
    malformed_claim["packet_canonical_json"] = packet_bytes.decode()
    malformed_claim["packet_sha256"] = hashlib.sha256(packet_bytes).hexdigest()
    malformed_claim["launch"]["packet_sha256"] = malformed_claim["packet_sha256"]  # type: ignore[index]
    launch_bytes = canonical_json_bytes(malformed_claim["launch"])
    malformed_claim["launch_canonical_json"] = launch_bytes.decode()
    malformed_claim["launch_sha256"] = hashlib.sha256(launch_bytes).hexdigest()
    with pytest.raises(HtError, match="40-hex sha1 Git OID"):
        build_record(
            planned, "work_claimed", "2026-07-15T00:00:01Z", **malformed_claim
        )


def test_repository_stable_read_rejects_executable_mode_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "work.json"
    target.write_bytes(b'{"type":"issue"}\n')
    os.chmod(target, 0o644)
    real_read = runtime_repository.os.read
    changed = False

    def chmod_during_read(fd: int, count: int) -> bytes:
        nonlocal changed
        if not changed:
            changed = True
            os.chmod(target, 0o755)
        return real_read(fd, count)

    monkeypatch.setattr(runtime_repository.os, "read", chmod_during_read)
    with pytest.raises(HtError, match="executable|changed during proof"):
        runtime_repository._stable_repository_bytes(target)


def test_control_alternatives_timestamp_formats_and_runtime_usage_exit(sandbox: Sandbox) -> None:
    control_id = str(uuid4())
    daemon_id = str(uuid4())
    accepted = {
        "schema_version": "hypothesis-tree-runtime-control-response/1.0.0",
        "status": "accepted", "control_id": control_id,
        "target_daemon_incarnation_id": daemon_id,
    }
    rejected = {
        **accepted, "status": "rejected", "reason_code": "stale-daemon-incarnation"
    }
    validate("control-response.schema.json", accepted)
    validate("control-response.schema.json", rejected)
    with pytest.raises(HtError, match="rejects"):
        validate("control-response.schema.json", {**accepted, "reason_code": "stale-daemon-incarnation"})
    with pytest.raises(HtError, match="rejects"):
        validate("control-response.schema.json", {key: value for key, value in rejected.items() if key != "reason_code"})

    descriptor = {
        "schema_version": "hypothesis-tree-runtime/1.0.0",
        "runtime_kind": "hypothesis-tree", "build_id": "ht-runtime-kernel/1.0.0",
        "runtime_id": str(uuid4()), "runtime_root": "/tmp/runtime",
        "repository_root": "/tmp/root", "created_at": "2026-02-30T00:00:00Z",
    }
    with pytest.raises(HtError, match="rejects"):
        validate("descriptor.schema.json", descriptor)
    descriptor["created_at"] = "2026-07-15T03:00:00+03:00"
    with pytest.raises(HtError, match="rejects"):
        validate("descriptor.schema.json", descriptor)

    schema_root = Path(__file__).parents[1] / "system/ht/runtime/schemas"
    for schema_path in schema_root.glob("*.schema.json"):
        stack = [json.loads(schema_path.read_text())]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                if value.get("format") == "date-time":
                    assert value.get("pattern", "").endswith("Z$")
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)

    runtime_usage = sandbox.run("runtime", "request", "--json")
    assert runtime_usage.returncode == 3
    ordinary_usage = sandbox.run("issue", "mint")
    assert ordinary_usage.returncode == 2
