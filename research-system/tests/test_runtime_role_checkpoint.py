"""Direct proofs for the marker-selected B2 checkpoint/replay overlay."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from conftest import Sandbox
from ht.errors import HtError
from ht.runtime.atomic import read_exact_file
from ht.runtime.capability import (
    B1_TOP_NAMES,
    CanonicalDocument,
    inspect_capability_state,
)
from ht.runtime.inventory import validate_runtime_inventory
from ht.runtime.replay import (
    build_record,
    projection_eligibility,
    publish_projections,
    recover_tolerated_tail,
    replay,
    require_current_projections,
    upgrade_context_from_capability,
)
from ht.runtime.schema import canonical_json_bytes, validate
from ht.runtime.state import UpgradeContext, role_checkpoint
from ht.runtime.wal import ParsedWal, frame_record, parse_bytes


NOW = "2026-07-15T00:00:00Z"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _base(runtime_id: str | None = None):
    return replay(parse_bytes(b""), runtime_id or str(uuid4()))


def _capability_document(base) -> CanonicalDocument:
    value = {
        "schema_version": "hypothesis-tree-runtime-role-capability/1.0.0",
        "capability": "owned-role-runtime-v1",
        "runtime_id": base.runtime_id,
        "runtime_schema_version": "hypothesis-tree-runtime/1.0.0",
        "role_request_schema_version": "hypothesis-tree-runtime-role-request/2.0.0",
        "upgrade_base_seq": base.last_seq,
        "upgrade_base_clean_wal_sha256": base.clean_wal_sha256,
        "upgrade_base_checkpoint_sha256": _sha(base.checkpoint_bytes()),
        "upgrade_base_binding_ledger_sha256": _sha(base.binding_bytes()),
        "created_at": NOW,
    }
    data = canonical_json_bytes(value)
    return CanonicalDocument(value, data, _sha(data))


def _context(base) -> UpgradeContext:
    return upgrade_context_from_capability(_capability_document(base))


def _daemon_suffix(base) -> tuple[ParsedWal, object]:
    record = build_record(
        base,
        "daemon_started",
        NOW,
        daemon_incarnation_id=str(uuid4()),
    )
    parsed = parse_bytes(frame_record(record))
    return parsed, replay(parsed, base.runtime_id)


def _append(wal: bytes, state, event: str, **fields):
    wal += frame_record(build_record(state, event, NOW, **fields))
    return wal, replay(parse_bytes(wal), state.runtime_id)


def _write_runtime_file(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _file_identity(path: Path) -> tuple[int, ...]:
    value = path.lstat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
    )


def test_unupgraded_checkpoint_matches_b1_golden_and_context_is_immutable() -> None:
    base = _base("00000000-0000-4000-8000-000000000001")
    checkpoint_bytes = base.checkpoint_bytes()
    assert len(checkpoint_bytes) == 1022
    assert _sha(checkpoint_bytes) == (
        "165b2cfdbe66d4dc46ec79bdf87bc497b68384356e7050c5503653eee973d584"
    )
    assert base.checkpoint_object()["schema_version"] == (
        "hypothesis-tree-runtime-checkpoint/1.0.0"
    )
    assert not {
        "role_capability_sha256",
        "upgrade_base_seq",
        "upgrade_base_clean_wal_sha256",
        "upgrade_base_checkpoint_sha256",
        "upgrade_base_binding_ledger_sha256",
    } & base.checkpoint_object().keys()

    known_bad = checkpoint_bytes.replace(b'"last_seq":0', b'"last_seq":1')
    assert len(known_bad) == len(checkpoint_bytes)
    assert _sha(known_bad) != _sha(checkpoint_bytes)

    context = _context(base)
    with pytest.raises(FrozenInstanceError):
        context.upgrade_base_seq = 1  # type: ignore[misc]


def test_role_checkpoint_is_a_strict_additive_copy() -> None:
    base = _base()
    context = _context(base)
    v1 = base.checkpoint_object()
    frozen_v1 = deepcopy(v1)

    upgraded = role_checkpoint(v1, context)

    assert v1 == frozen_v1
    assert upgraded["schema_version"] == "hypothesis-tree-runtime-checkpoint/2.0.0"
    for name, value in v1.items():
        if name != "schema_version":
            assert upgraded[name] == value
    assert upgraded["role_capability_sha256"] == context.role_capability_sha256
    assert upgraded["upgrade_base_seq"] == context.upgrade_base_seq
    assert upgraded["upgrade_base_clean_wal_sha256"] == (
        context.upgrade_base_clean_wal_sha256
    )
    assert upgraded["upgrade_base_checkpoint_sha256"] == (
        context.upgrade_base_checkpoint_sha256
    )
    assert upgraded["upgrade_base_binding_ledger_sha256"] == (
        context.upgrade_base_binding_ledger_sha256
    )
    validate("checkpoint-role.schema.json", upgraded)


def test_capability_context_reproves_canonical_bytes_and_hash() -> None:
    document = _capability_document(_base())
    assert upgrade_context_from_capability(document).role_capability_sha256 == document.sha256

    with pytest.raises(HtError, match="canonical"):
        upgrade_context_from_capability(
            replace(document, canonical_bytes=document.canonical_bytes + b" ")
        )
    with pytest.raises(HtError, match="hash"):
        upgrade_context_from_capability(replace(document, sha256="f" * 64))
    changed = deepcopy(document.value)
    changed["capability"] = "different"
    with pytest.raises(HtError):
        upgrade_context_from_capability(replace(document, value=changed))


def test_upgraded_replay_allows_only_b1_daemon_control_tail_suffix() -> None:
    base = _base()
    context = _context(base)
    parsed, ordinary = _daemon_suffix(base)

    upgraded = replay(parsed, base.runtime_id, upgrade=context)

    assert upgraded.checkpoint_object()["schema_version"] == (
        "hypothesis-tree-runtime-checkpoint/2.0.0"
    )
    assert upgraded.bindings == ordinary.bindings
    assert upgraded.request_index == ordinary.request_index
    assert upgraded.dedup_index == ordinary.dedup_index
    assert upgraded.session_index == ordinary.session_index
    assert upgraded.control_index == ordinary.control_index
    assert upgraded.upgrade == context


def test_nonempty_b1_baseline_and_later_daemon_suffix_preserve_all_b1_values() -> None:
    runtime_id = str(uuid4())
    wal = b""
    state = _base(runtime_id)
    daemon_id = str(uuid4())
    wal, state = _append(
        wal,
        state,
        "daemon_started",
        daemon_incarnation_id=daemon_id,
    )
    control_id = str(uuid4())
    wal, state = _append(
        wal,
        state,
        "control_stop_accepted",
        control_id=control_id,
        target_daemon_incarnation_id=daemon_id,
    )
    wal, state = _append(
        wal,
        state,
        "daemon_stopped",
        daemon_incarnation_id=daemon_id,
        control_id=control_id,
    )
    context = _context(state)

    activation = replay(parse_bytes(wal), runtime_id, upgrade=context)
    v1 = state.checkpoint_object()
    v2 = activation.checkpoint_object()
    for name, value in v1.items():
        if name != "schema_version":
            assert v2[name] == value

    wal, ordinary = _append(
        wal,
        state,
        "daemon_started",
        daemon_incarnation_id=str(uuid4()),
    )
    upgraded = replay(parse_bytes(wal), runtime_id, upgrade=context)
    assert upgraded.bindings == ordinary.bindings
    assert upgraded.request_index == ordinary.request_index
    assert upgraded.dedup_index == ordinary.dedup_index
    assert upgraded.session_index == ordinary.session_index
    assert upgraded.control_index == ordinary.control_index


@pytest.mark.parametrize(
    "field",
    [
        "upgrade_base_clean_wal_sha256",
        "upgrade_base_checkpoint_sha256",
        "upgrade_base_binding_ledger_sha256",
    ],
)
def test_upgrade_baseline_drift_rejects(field: str) -> None:
    base = _base()
    context = replace(_context(base), **{field: "f" * 64})
    with pytest.raises(HtError, match="baseline"):
        replay(parse_bytes(b""), base.runtime_id, upgrade=context)


def test_upgrade_baseline_ahead_rejects() -> None:
    base = _base()
    context = replace(_context(base), upgrade_base_seq=1)
    with pytest.raises(HtError, match="baseline"):
        replay(parse_bytes(b""), base.runtime_id, upgrade=context)


def _decoded_record(event: str, *, seq: int = 1) -> ParsedWal:
    return ParsedWal(
        ({"seq": seq, "event": event},),
        b"x",
        None,
        (1,),
    )


def test_role_event_at_or_before_baseline_rejects_before_role_reducer() -> None:
    base = _base()
    context = replace(_context(base), upgrade_base_seq=1)
    with pytest.raises(HtError, match="role event.*baseline"):
        replay(_decoded_record("role_work_planned"), base.runtime_id, upgrade=context)


def test_role_suffix_rejects_until_role_reducer_is_installed() -> None:
    base = _base()
    with pytest.raises(HtError, match="role reducer"):
        replay(
            _decoded_record("role_work_planned"),
            base.runtime_id,
            upgrade=_context(base),
        )


def test_synthetic_lifecycle_suffix_rejects_after_upgrade() -> None:
    base = _base()
    with pytest.raises(HtError, match="synthetic lifecycle"):
        replay(
            _decoded_record("work_planned"),
            base.runtime_id,
            upgrade=_context(base),
        )


def test_v2_projection_accepts_only_c_or_w_and_repairs_ledger_first(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    base = _base()
    context = _context(base)
    checkpoint = replay(parse_bytes(b""), base.runtime_id, upgrade=context)
    parsed, _ordinary = _daemon_suffix(base)
    target = replay(parsed, base.runtime_id, upgrade=context)
    _write_runtime_file(root / "checkpoint.json", checkpoint.checkpoint_bytes())
    _write_runtime_file(root / "binding-ledger.json", checkpoint.binding_bytes())

    eligibility = projection_eligibility(
        parsed,
        base.runtime_id,
        checkpoint.checkpoint_bytes(),
        checkpoint.binding_bytes(),
        upgrade=context,
    )
    assert eligibility.checkpoint_state == checkpoint
    assert eligibility.full_state == target
    assert not eligibility.ledger_at_full_target

    _write_runtime_file(root / "binding-ledger.json", target.binding_bytes())
    eligibility = projection_eligibility(
        parsed,
        base.runtime_id,
        checkpoint.checkpoint_bytes(),
        target.binding_bytes(),
        upgrade=context,
    )
    assert eligibility.ledger_at_full_target

    publish_projections(root, target, allowed_prior=(checkpoint, target))
    assert require_current_projections(root, parsed, base.runtime_id, upgrade=context) == target


def test_projection_publication_rejects_disk_context_mismatch_before_any_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    base = _base()
    context_a = _context(base)
    context_b = replace(context_a, role_capability_sha256="f" * 64)
    checkpoint_a = replay(parse_bytes(b""), base.runtime_id, upgrade=context_a)
    checkpoint_b = replay(parse_bytes(b""), base.runtime_id, upgrade=context_b)
    parsed, _ordinary = _daemon_suffix(base)
    target_b = replay(parsed, base.runtime_id, upgrade=context_b)
    checkpoint_path = root / "checkpoint.json"
    ledger_path = root / "binding-ledger.json"
    _write_runtime_file(checkpoint_path, checkpoint_a.checkpoint_bytes())
    _write_runtime_file(ledger_path, checkpoint_a.binding_bytes())
    before_checkpoint = checkpoint_path.read_bytes()
    before_ledger = ledger_path.read_bytes()
    before_entries = tuple(sorted(child.name for child in root.iterdir()))
    before_stats = tuple(
        _file_identity(path)
        for path in (checkpoint_path, ledger_path)
    )

    with pytest.raises(HtError, match="capability marker fields.*context"):
        publish_projections(
            root,
            target_b,
            allowed_prior=(checkpoint_b, target_b),
        )

    assert checkpoint_path.read_bytes() == before_checkpoint
    assert ledger_path.read_bytes() == before_ledger
    assert tuple(sorted(child.name for child in root.iterdir())) == before_entries
    assert tuple(
        _file_identity(path)
        for path in (checkpoint_path, ledger_path)
    ) == before_stats


def test_v2_projection_rejects_checkpoint_ahead_marker_drift_and_bad_ledger() -> None:
    base = _base()
    context = _context(base)
    stored = replay(parse_bytes(b""), base.runtime_id, upgrade=context).checkpoint_object()

    ahead = deepcopy(stored)
    ahead["last_seq"] = 1
    with pytest.raises(HtError):
        projection_eligibility(
            parse_bytes(b""),
            base.runtime_id,
            canonical_json_bytes(ahead),
            base.binding_bytes(),
            upgrade=context,
        )

    drift = deepcopy(stored)
    drift["role_capability_sha256"] = "f" * 64
    with pytest.raises(HtError, match="capability|marker"):
        projection_eligibility(
            parse_bytes(b""),
            base.runtime_id,
            canonical_json_bytes(drift),
            base.binding_bytes(),
            upgrade=context,
        )

    with pytest.raises(HtError, match="ledger"):
        projection_eligibility(
            parse_bytes(b""),
            base.runtime_id,
            canonical_json_bytes(stored),
            canonical_json_bytes({}),
            upgrade=context,
        )


def test_v2_projection_rejects_role_identity_in_b1_index() -> None:
    base = _base()
    context = _context(base)
    stored = replay(parse_bytes(b""), base.runtime_id, upgrade=context).checkpoint_object()
    stored["request_index"][str(uuid4())] = {
        "node_address": f"runtime/{uuid4()}#role"
    }
    with pytest.raises(HtError):
        projection_eligibility(
            parse_bytes(b""),
            base.runtime_id,
            canonical_json_bytes(stored),
            base.binding_bytes(),
            upgrade=context,
        )


def test_v2_tail_recovery_preserves_upgrade_context(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    base = _base()
    context = _context(base)
    checkpoint = replay(parse_bytes(b""), base.runtime_id, upgrade=context)
    damaged = b"broken-final-frame"
    parsed = parse_bytes(damaged)
    assert parsed.tail is not None
    _write_runtime_file(root / "run-ledger.jsonl", damaged)
    _write_runtime_file(root / "checkpoint.json", checkpoint.checkpoint_bytes())
    _write_runtime_file(root / "binding-ledger.json", checkpoint.binding_bytes())

    recovered = recover_tolerated_tail(
        root,
        parsed,
        base.runtime_id,
        timestamp=NOW,
        upgrade=context,
    )

    assert recovered.upgrade == context
    assert recovered.checkpoint_object()["schema_version"] == (
        "hypothesis-tree-runtime-checkpoint/2.0.0"
    )
    assert recovered.final_tail is not None


def test_typed_capability_overlay_preserves_closed_b1_inventory_validation(
    sandbox: Sandbox,
) -> None:
    assert sandbox.run("runtime", "init", "--json").returncode == 0
    activated = sandbox.run("role", "init", "--json")
    assert activated.returncode == 0, activated.stderr
    estate = sandbox.root / "var/runtime"
    descriptor = json.loads(read_exact_file(estate / "runtime.json"))
    capability = inspect_capability_state(
        estate,
        runtime_id=descriptor["runtime_id"],
        base_top_names=B1_TOP_NAMES,
    )
    context = upgrade_context_from_capability(capability.capability)
    state = require_current_projections(
        estate,
        parse_bytes(read_exact_file(estate / "run-ledger.jsonl")),
        descriptor["runtime_id"],
        upgrade=context,
    )

    with pytest.raises(HtError, match="top-level entries differ"):
        validate_runtime_inventory(estate, state)
    validate_runtime_inventory(estate, state, capability_state=capability)
