"""Closed physical-state contract for the B2 role-capability markers."""

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
from ht.errors import HtError
from ht.runtime.capability import (
    B1_TOP_NAMES,
    CAPABILITY_FILE,
    EXPECTED_ROLE_DIRECTORIES,
    INITIALIZATION_FILE,
    inspect_capability_state,
)
from ht.runtime.schema import canonical_json_bytes


NOW = "2026-07-15T00:00:00.000000Z"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_exact(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.write_bytes(data)
    os.chmod(path, mode)


def _mkdir_exact(path: Path) -> None:
    path.mkdir()
    os.chmod(path, 0o700)


def _initialized_runtime(sandbox: Sandbox) -> tuple[Path, str, bytes, dict]:
    result = sandbox.run("runtime", "init", "--json")
    assert result.returncode == 0, result.stderr
    estate = sandbox.root / "var/runtime"
    runtime_id = json.loads((estate / "runtime.json").read_text())["runtime_id"]
    checkpoint_bytes = (estate / "checkpoint.json").read_bytes()
    checkpoint = json.loads(checkpoint_bytes)
    return estate, runtime_id, checkpoint_bytes, checkpoint


def _documents(
    runtime_id: str,
    checkpoint_bytes: bytes,
    checkpoint: dict,
) -> tuple[dict, bytes, dict, bytes, dict, bytes]:
    capability = {
        "schema_version": "hypothesis-tree-runtime-role-capability/1.0.0",
        "capability": "owned-role-runtime-v1",
        "runtime_id": runtime_id,
        "runtime_schema_version": "hypothesis-tree-runtime/1.0.0",
        "role_request_schema_version": "hypothesis-tree-runtime-role-request/2.0.0",
        "upgrade_base_seq": checkpoint["last_seq"],
        "upgrade_base_clean_wal_sha256": checkpoint["clean_wal_sha256"],
        "upgrade_base_checkpoint_sha256": _sha(checkpoint_bytes),
        "upgrade_base_binding_ledger_sha256": checkpoint[
            "binding_ledger_sha256"
        ],
        "created_at": NOW,
    }
    capability_bytes = canonical_json_bytes(capability)
    upgraded = {
        **checkpoint,
        "schema_version": "hypothesis-tree-runtime-checkpoint/2.0.0",
        "role_capability_sha256": _sha(capability_bytes),
        "upgrade_base_seq": capability["upgrade_base_seq"],
        "upgrade_base_clean_wal_sha256": capability[
            "upgrade_base_clean_wal_sha256"
        ],
        "upgrade_base_checkpoint_sha256": capability[
            "upgrade_base_checkpoint_sha256"
        ],
        "upgrade_base_binding_ledger_sha256": capability[
            "upgrade_base_binding_ledger_sha256"
        ],
    }
    upgraded_bytes = canonical_json_bytes(upgraded)
    initialization = {
        "schema_version": "hypothesis-tree-runtime-role-initialization/1.0.0",
        "runtime_id": runtime_id,
        "capability_canonical_json": capability_bytes.decode("utf-8"),
        "capability_sha256": _sha(capability_bytes),
        "upgraded_checkpoint_canonical_json": upgraded_bytes.decode("utf-8"),
        "upgraded_checkpoint_sha256": _sha(upgraded_bytes),
        "expected_directories": list(EXPECTED_ROLE_DIRECTORIES),
        "created_at": NOW,
    }
    return (
        capability,
        capability_bytes,
        upgraded,
        upgraded_bytes,
        initialization,
        canonical_json_bytes(initialization),
    )


def _create_prefix(estate: Path, count: int) -> None:
    for relative in EXPECTED_ROLE_DIRECTORIES[:count]:
        path = estate / relative
        if not path.exists():
            _mkdir_exact(path)


def _inspect(estate: Path, runtime_id: str):
    return inspect_capability_state(
        estate,
        runtime_id=runtime_id,
        base_top_names=B1_TOP_NAMES,
    )


def test_unupgraded_is_exact_and_owner_temps_are_not_activation(
    sandbox: Sandbox,
) -> None:
    estate, runtime_id, _checkpoint_bytes, _checkpoint = _initialized_runtime(sandbox)
    state = _inspect(estate, runtime_id)
    assert state.branch == "unupgraded"
    assert state.capability is None
    assert state.initialization is None
    assert state.created_directories == ()

    temporary = estate / f".ht-publish-{INITIALIZATION_FILE}-{uuid4()}"
    _write_exact(temporary, b'{"partial":')
    assert _inspect(estate, runtime_id).branch == "unupgraded"

    temporary.rename(estate / f".ht-publish-{INITIALIZATION_FILE}-not-a-uuid")
    with pytest.raises(HtError, match="temporary|top-level"):
        _inspect(estate, runtime_id)


@pytest.mark.parametrize("laundered_name", ("producers", "arbitrary-extra"))
def test_caller_cannot_widen_the_closed_b1_top_inventory(
    sandbox: Sandbox,
    laundered_name: str,
) -> None:
    estate, runtime_id, _checkpoint_bytes, _checkpoint = _initialized_runtime(sandbox)
    path = estate / laundered_name
    if laundered_name == "producers":
        _mkdir_exact(path)
    else:
        _write_exact(path, b"rogue")

    with pytest.raises(ValueError, match="exact B1 top-level"):
        inspect_capability_state(
            estate,
            runtime_id=runtime_id,
            base_top_names=B1_TOP_NAMES | {laundered_name},
        )


def test_public_capability_temp_has_no_owner_before_hidden_marker(
    sandbox: Sandbox,
) -> None:
    estate, runtime_id, _checkpoint_bytes, _checkpoint = _initialized_runtime(sandbox)
    temporary = estate / f".ht-publish-{CAPABILITY_FILE}-{uuid4()}"
    _write_exact(temporary, b'{"partial":')

    with pytest.raises(HtError, match="capability.*temporary|hidden.*marker"):
        _inspect(estate, runtime_id)


@pytest.mark.parametrize("prefix_count", range(len(EXPECTED_ROLE_DIRECTORIES) + 1))
def test_hidden_marker_accepts_each_exact_baseline_checkpoint_prefix(
    sandbox: Sandbox,
    prefix_count: int,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    *_, initialization, initialization_bytes = _documents(
        runtime_id, checkpoint_bytes, checkpoint
    )
    _write_exact(estate / INITIALIZATION_FILE, initialization_bytes)
    _create_prefix(estate, prefix_count)

    state = _inspect(estate, runtime_id)
    assert state.branch == "repair-prefix"
    assert state.initialization is not None
    assert state.checkpoint_stage == "baseline"
    assert state.public_committed is False
    assert state.created_directories == EXPECTED_ROLE_DIRECTORIES[:prefix_count]
    assert state.initialization.value == initialization


def test_hidden_marker_accepts_only_ordered_complete_v2_and_public_target(
    sandbox: Sandbox,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    (
        _capability,
        capability_bytes,
        _upgraded,
        upgraded_bytes,
        _initialization,
        initialization_bytes,
    ) = _documents(runtime_id, checkpoint_bytes, checkpoint)
    _write_exact(estate / INITIALIZATION_FILE, initialization_bytes)
    _create_prefix(estate, len(EXPECTED_ROLE_DIRECTORIES))
    _write_exact(estate / "checkpoint.json", upgraded_bytes)
    _write_exact(estate / CAPABILITY_FILE, capability_bytes)

    state = _inspect(estate, runtime_id)
    assert state.branch == "repair-prefix"
    assert state.checkpoint_stage == "upgraded"
    assert state.public_committed is True
    assert state.capability is not None
    assert state.capability.canonical_bytes == capability_bytes


def test_public_marker_selects_only_complete_empty_upgraded_inventory(
    sandbox: Sandbox,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    (
        capability,
        capability_bytes,
        upgraded,
        upgraded_bytes,
        _initialization,
        _initialization_bytes,
    ) = _documents(runtime_id, checkpoint_bytes, checkpoint)
    _create_prefix(estate, len(EXPECTED_ROLE_DIRECTORIES))
    _write_exact(estate / "checkpoint.json", upgraded_bytes)
    _write_exact(estate / CAPABILITY_FILE, capability_bytes)

    state = _inspect(estate, runtime_id)
    assert state.branch == "upgraded-complete"
    assert state.capability is not None
    assert state.capability.value == capability
    assert state.checkpoint.value == upgraded
    assert state.initialization is None

    # Current values may advance after activation, but the five marker-selected
    # fields remain immutable and exact. Replay owns the current-value proof.
    advanced = deepcopy(upgraded)
    advanced["last_seq"] = 1
    advanced["clean_wal_sha256"] = "a" * 64
    advanced["daemon_incarnation_id"] = str(uuid4())
    _write_exact(estate / "checkpoint.json", canonical_json_bytes(advanced))
    assert _inspect(estate, runtime_id).branch == "upgraded-complete"


@pytest.mark.parametrize(
    "mutation",
    (
        "hidden-noncanonical",
        "capability-hash",
        "checkpoint-hash",
        "runtime-id",
        "timestamp",
        "directory-order",
        "checkpoint-marker-field",
    ),
)
def test_hidden_marker_cross_document_corruption_rejects(
    sandbox: Sandbox,
    mutation: str,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    (
        capability,
        capability_bytes,
        upgraded,
        upgraded_bytes,
        initialization,
        _initialization_bytes,
    ) = _documents(runtime_id, checkpoint_bytes, checkpoint)
    if mutation == "hidden-noncanonical":
        data = json.dumps(initialization, indent=2).encode() + b"\n"
    else:
        changed = deepcopy(initialization)
        if mutation == "capability-hash":
            changed["capability_sha256"] = "f" * 64
        elif mutation == "checkpoint-hash":
            changed["upgraded_checkpoint_sha256"] = "f" * 64
        elif mutation == "runtime-id":
            changed["runtime_id"] = str(uuid4())
        elif mutation == "timestamp":
            changed["created_at"] = "2026-07-15T00:00:01.000000Z"
        elif mutation == "directory-order":
            changed["expected_directories"][0:2] = reversed(
                changed["expected_directories"][0:2]
            )
        else:
            altered_checkpoint = deepcopy(upgraded)
            altered_checkpoint["upgrade_base_seq"] += 1
            altered_bytes = canonical_json_bytes(altered_checkpoint)
            changed["upgraded_checkpoint_canonical_json"] = altered_bytes.decode()
            changed["upgraded_checkpoint_sha256"] = _sha(altered_bytes)
        data = canonical_json_bytes(changed)
    _write_exact(estate / INITIALIZATION_FILE, data)

    with pytest.raises(HtError):
        _inspect(estate, runtime_id)

    # Keep fixtures themselves load-bearing: every unmutated embedded document
    # is exact canonical data selected by the accepted schemas.
    assert json.loads(capability_bytes) == capability
    assert json.loads(upgraded_bytes) == upgraded


@pytest.mark.parametrize("shape", ("mode", "symlink", "hardlink", "directory"))
def test_public_marker_requires_exact_regular_single_link_shape(
    sandbox: Sandbox,
    shape: str,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    _, capability_bytes, _, upgraded_bytes, _, _ = _documents(
        runtime_id, checkpoint_bytes, checkpoint
    )
    _create_prefix(estate, len(EXPECTED_ROLE_DIRECTORIES))
    _write_exact(estate / "checkpoint.json", upgraded_bytes)
    marker = estate / CAPABILITY_FILE
    if shape == "mode":
        _write_exact(marker, capability_bytes, mode=0o640)
    elif shape == "symlink":
        marker.symlink_to("checkpoint.json")
    elif shape == "hardlink":
        _write_exact(marker, capability_bytes)
        os.link(marker, estate / "unowned-alias")
    else:
        marker.mkdir(mode=0o700)

    with pytest.raises(HtError):
        _inspect(estate, runtime_id)


@pytest.mark.parametrize("shape", ("mode", "symlink", "hardlink", "directory"))
def test_hidden_marker_requires_exact_or_owned_commit_link_shape(
    sandbox: Sandbox,
    shape: str,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    *_, initialization_bytes = _documents(runtime_id, checkpoint_bytes, checkpoint)
    marker = estate / INITIALIZATION_FILE
    if shape == "mode":
        _write_exact(marker, initialization_bytes, mode=0o640)
    elif shape == "symlink":
        marker.symlink_to("checkpoint.json")
    elif shape == "hardlink":
        _write_exact(marker, initialization_bytes)
        os.link(marker, estate / "unowned-hidden-alias")
    else:
        marker.mkdir(mode=0o700)

    with pytest.raises(HtError):
        _inspect(estate, runtime_id)


def test_repair_rejects_prefix_holes_nonempty_directories_and_early_v2(
    sandbox: Sandbox,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    _, _, _, upgraded_bytes, _, initialization_bytes = _documents(
        runtime_id, checkpoint_bytes, checkpoint
    )
    _write_exact(estate / INITIALIZATION_FILE, initialization_bytes)

    # A later sibling without its earlier sibling is not a creation prefix.
    _mkdir_exact(estate / EXPECTED_ROLE_DIRECTORIES[1])
    with pytest.raises(HtError, match="prefix"):
        _inspect(estate, runtime_id)
    (estate / EXPECTED_ROLE_DIRECTORIES[1]).rmdir()

    _create_prefix(estate, 1)
    _write_exact(estate / EXPECTED_ROLE_DIRECTORIES[0] / "rogue.json", b"{}\n")
    with pytest.raises(HtError, match="unexpected|empty"):
        _inspect(estate, runtime_id)
    (estate / EXPECTED_ROLE_DIRECTORIES[0] / "rogue.json").unlink()

    _write_exact(estate / "checkpoint.json", upgraded_bytes)
    with pytest.raises(HtError, match="checkpoint|prefix"):
        _inspect(estate, runtime_id)


def test_upgraded_branch_rejects_missing_prefix_and_dynamic_children(
    sandbox: Sandbox,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    _, capability_bytes, _, upgraded_bytes, _, _ = _documents(
        runtime_id, checkpoint_bytes, checkpoint
    )
    _write_exact(estate / "checkpoint.json", upgraded_bytes)
    _write_exact(estate / CAPABILITY_FILE, capability_bytes)
    with pytest.raises(HtError, match="prefix|director"):
        _inspect(estate, runtime_id)

    _create_prefix(estate, len(EXPECTED_ROLE_DIRECTORIES))
    auth = estate / "producers/codex/bootstrap-home/auth.json"
    _write_exact(auth, b"{}\n")
    with pytest.raises(HtError, match="unexpected|empty"):
        _inspect(estate, runtime_id)


def test_public_marker_and_checkpoint_must_be_canonical_and_marker_bound(
    sandbox: Sandbox,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    capability, capability_bytes, upgraded, upgraded_bytes, _, _ = _documents(
        runtime_id, checkpoint_bytes, checkpoint
    )
    _create_prefix(estate, len(EXPECTED_ROLE_DIRECTORIES))
    _write_exact(estate / "checkpoint.json", upgraded_bytes)
    _write_exact(
        estate / CAPABILITY_FILE,
        json.dumps(capability, indent=2).encode() + b"\n",
    )
    with pytest.raises(HtError, match="canonical"):
        _inspect(estate, runtime_id)

    _write_exact(estate / CAPABILITY_FILE, capability_bytes)
    changed = deepcopy(upgraded)
    changed["role_capability_sha256"] = "f" * 64
    _write_exact(estate / "checkpoint.json", canonical_json_bytes(changed))
    with pytest.raises(HtError, match="capability|marker"):
        _inspect(estate, runtime_id)


def test_marker_linked_commit_temp_is_repair_only_but_complete_rejects_temps(
    sandbox: Sandbox,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    _, capability_bytes, _, upgraded_bytes, _, initialization_bytes = _documents(
        runtime_id, checkpoint_bytes, checkpoint
    )
    _write_exact(estate / INITIALIZATION_FILE, initialization_bytes)
    hidden_alias = estate / f".ht-publish-{INITIALIZATION_FILE}-{uuid4()}"
    os.link(estate / INITIALIZATION_FILE, hidden_alias)
    assert _inspect(estate, runtime_id).branch == "repair-prefix"
    hidden_alias.unlink()

    _create_prefix(estate, len(EXPECTED_ROLE_DIRECTORIES))
    _write_exact(estate / "checkpoint.json", upgraded_bytes)
    _write_exact(estate / CAPABILITY_FILE, capability_bytes)
    (estate / INITIALIZATION_FILE).unlink()
    uncommitted = estate / f".ht-publish-{CAPABILITY_FILE}-{uuid4()}"
    _write_exact(uncommitted, b"partial")
    with pytest.raises(HtError, match="temporary|complete"):
        _inspect(estate, runtime_id)


def test_no_role_marker_can_be_inferred_from_visible_directories(
    sandbox: Sandbox,
) -> None:
    estate, runtime_id, _checkpoint_bytes, _checkpoint = _initialized_runtime(sandbox)
    _mkdir_exact(estate / EXPECTED_ROLE_DIRECTORIES[0])
    before = stat.S_IMODE((estate / EXPECTED_ROLE_DIRECTORIES[0]).stat().st_mode)
    with pytest.raises(HtError, match="without.*marker|top-level"):
        _inspect(estate, runtime_id)
    assert stat.S_IMODE((estate / EXPECTED_ROLE_DIRECTORIES[0]).stat().st_mode) == before


def test_checkpoint_replace_temp_is_carried_only_at_exact_owned_stage(
    sandbox: Sandbox,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    *_, initialization_bytes = _documents(runtime_id, checkpoint_bytes, checkpoint)
    _write_exact(estate / INITIALIZATION_FILE, initialization_bytes)
    _create_prefix(estate, len(EXPECTED_ROLE_DIRECTORIES))
    temporary = estate / f".ht-replace-checkpoint.json-{uuid4()}"
    _write_exact(temporary, b'{"partial":')

    state = _inspect(estate, runtime_id)
    assert state.branch == "repair-prefix"
    assert state.checkpoint_stage == "baseline"
    assert state.checkpoint_replacement.temporary_names == (temporary.name,)
    assert state.checkpoint_replacement.final_bytes == checkpoint_bytes


@pytest.mark.parametrize(
    "stage",
    ("unupgraded", "early-prefix", "upgraded", "public", "complete"),
)
def test_checkpoint_replace_temp_rejects_outside_exact_owned_stage(
    sandbox: Sandbox,
    stage: str,
) -> None:
    estate, runtime_id, checkpoint_bytes, checkpoint = _initialized_runtime(sandbox)
    (
        _capability,
        capability_bytes,
        _upgraded,
        upgraded_bytes,
        _initialization,
        initialization_bytes,
    ) = _documents(runtime_id, checkpoint_bytes, checkpoint)
    if stage != "unupgraded":
        _write_exact(estate / INITIALIZATION_FILE, initialization_bytes)
        _create_prefix(
            estate,
            len(EXPECTED_ROLE_DIRECTORIES) - (1 if stage == "early-prefix" else 0),
        )
    if stage in {"upgraded", "public", "complete"}:
        _write_exact(estate / "checkpoint.json", upgraded_bytes)
    if stage in {"public", "complete"}:
        _write_exact(estate / CAPABILITY_FILE, capability_bytes)
    if stage == "complete":
        (estate / INITIALIZATION_FILE).unlink()
    _write_exact(estate / f".ht-replace-checkpoint.json-{uuid4()}", b"partial")

    with pytest.raises(HtError, match="checkpoint replacement|temporary"):
        _inspect(estate, runtime_id)
