from __future__ import annotations

import copy
import hashlib
import json

import pytest

from harnessd import addressing, config, contracts, fencing, ipc, ledger, notary
from harnessd.spawn import chokepoint, outbox
from harnessd.spawn.adapters.base import SpawnResult


PARENT = "L1#exec"
CHILD = "L1/widget#exec"


@pytest.fixture
def runtime(tmp_path):
    previous_root = ledger.RUNTIME_ROOT
    previous_adapter = chokepoint.ADAPTER
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous_root
        chokepoint.set_adapter(previous_adapter)


class _FakeAdapter:
    def __init__(self):
        self.calls: list[str] = []

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append(tmux_target)
        return SpawnResult(
            ok=True,
            session_uuid="l2-session",
            model_used="fake",
            role_variant="L2",
            system_prompt_file="operational/shared/system-prompt.md",
            system_prompt_file_hash="hash",
            tmux_target=f"{addressing.session_name_for(tmux_target)}:0.0",
            transcript_path="/tmp/l2-session.jsonl",
            failure_class=None,
        )


def _seed_l1(runtime):
    workspace = addressing.node_dir(PARENT, runtime)
    workspace.mkdir(parents=True, exist_ok=True)
    token = fencing.mint_owner_token(PARENT, "l1-seat", "l1-session", 1)
    binding = {
        "node_address": PARENT,
        "parent_address": None,
        "level": "L1",
        "subagent_id": "l1-seat",
        "session_uuid": "l1-session",
        "state": "running",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "working",
        "tmux_target": "harness:l1",
        "workspace": str(workspace),
    }
    ledger.write_binding({PARENT: copy.deepcopy(binding)}, _lock_held=True)
    return workspace


def _prepare_project_node(runtime, *, intent: str | None):
    node = addressing.node_dir(CHILD, runtime)
    (node / "client-brief").mkdir(parents=True, exist_ok=True)
    (node / "brief.md").write_text("# project brief\n", encoding="utf-8")
    (node / "acceptance.md").write_text("# project acceptance\n", encoding="utf-8")
    (node / "client-brief" / "raw-request.md").write_text(
        "Build the widget.\n",
        encoding="utf-8",
    )
    if intent is not None:
        (node / "client-brief" / "intent-spec.md").write_text(intent, encoding="utf-8")
    return node


def _request_l2(workspace):
    outbox.request_child_spawn(
        workspace,
        child_name="widget",
        child_level="L2",
    )


_CONFIRMED = """# intent-spec

| ID | Requirement | Reflect-back status |
|---|---|---|
| R-001 | Build the widget | confirmed |

## Reflect-back script

The widget intent was played back to the owner and answered.

- confirmed by: owner, answered directly on the L1 seat
- date: 2026-07-28

status: confirmed
"""

_PENDING = """# intent-spec

| ID | Requirement | Reflect-back status |
|---|---|---|
| R-001 | Build the widget | pending |

## Reflect-back script

The widget intent has not been answered yet.

status: pending
"""

_REVISED = """# intent-spec

| ID | Requirement | Reflect-back status |
|---|---|---|
| R-001 | Build the amended widget | confirmed |

## Reflect-back script

The amended widget intent was played back to the owner and answered.

- confirmed by: owner, answered directly on the L1 seat
- date: 2026-07-28

status: confirmed
"""

_CONFIRMED_ROWS_WITHOUT_REFLECT_BACK = """# intent-spec

| ID | Requirement | Reflect-back status |
|---|---|---|
| R-001 | Build the widget | confirmed |
"""

_CONFIRMED_STATUS_WITHOUT_AUTHORITY = _CONFIRMED.replace(
    "- confirmed by: owner, answered directly on the L1 seat\n",
    "",
)

_CONFIRMED_STATUS_WITHOUT_DATE = _CONFIRMED.replace(
    "- date: 2026-07-28\n",
    "",
)

_CONFIRMED_STATUS_WITH_INVALID_DATE = _CONFIRMED.replace(
    "- date: 2026-07-28\n",
    "- date: 2026-99-99\n",
)

_CONFIRMED_ROWS_WITH_PENDING_SCRIPT = _CONFIRMED.replace(
    "status: confirmed",
    "status: pending",
)


def test_confirmed_client_brief_freezes_and_receipts_before_l2_opens(runtime):
    workspace = _seed_l1(runtime)
    node = _prepare_project_node(runtime, intent=_CONFIRMED)
    spec = node / "client-brief" / "intent-spec.md"
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    _request_l2(workspace)

    outcomes = outbox.service_outbox(PARENT)

    assert len(outcomes) == 1 and outcomes[0].status == "spawned"
    assert fake.calls == [CHILD]
    binding = ledger.read_binding(CHILD)
    held = binding.get("intent_spec_receipt")
    raw_held = binding.get("raw_request_receipt")
    assert held["holder"] == CHILD
    assert held["artifact"] == str(spec)
    assert held["stamp"] == {
        "present": True,
        "sha256": hashlib.sha256(_CONFIRMED.encode("utf-8")).hexdigest(),
        "bytes": len(_CONFIRMED.encode("utf-8")),
    }
    assert spec.stat().st_mode & 0o777 == 0o444
    assert notary.check(held).ok is True
    assert raw_held["artifact"] == str(
        node / "client-brief" / "raw-request.md"
    )
    assert (
        node / "client-brief" / "raw-request.md"
    ).stat().st_mode & 0o777 == 0o444
    assert notary.check(raw_held).ok is True


def test_missing_raw_request_refuses_before_freezing_intent(runtime):
    workspace = _seed_l1(runtime)
    node = _prepare_project_node(runtime, intent=_CONFIRMED)
    raw_request = node / "client-brief" / "raw-request.md"
    raw_request.unlink()
    spec = node / "client-brief" / "intent-spec.md"
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    _request_l2(workspace)

    outcomes = outbox.service_outbox(PARENT)

    assert outcomes[0].status == "rejected"
    assert "client-brief/raw-request.md" in outcomes[0].reason
    assert fake.calls == []
    assert spec.stat().st_mode & 0o200, "the paired freeze must not partially freeze intent"


def test_missing_intent_refuses_loudly_without_registering_or_opening_l2(runtime):
    workspace = _seed_l1(runtime)
    _prepare_project_node(runtime, intent=None)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    request = outbox.request_child_spawn(
        workspace,
        child_name="widget",
        child_level="L2",
    )

    outcomes = outbox.service_outbox(PARENT)

    assert len(outcomes) == 1 and outcomes[0].status == "rejected"
    assert "required project-genesis piece missing or unreadable" in outcomes[0].reason
    assert "client-brief/intent-spec.md" in outcomes[0].reason
    assert fake.calls == []
    assert ledger.read_binding(CHILD) is None
    reason_file = request.with_name(request.name + ".rejected.reason")
    assert "L1 must write the confirmed" in reason_file.read_text(encoding="utf-8")


def test_pending_intent_refuses_loudly_and_is_not_frozen(runtime):
    workspace = _seed_l1(runtime)
    node = _prepare_project_node(runtime, intent=_PENDING)
    spec = node / "client-brief" / "intent-spec.md"
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    _request_l2(workspace)

    outcomes = outbox.service_outbox(PARENT)

    assert len(outcomes) == 1 and outcomes[0].status == "rejected"
    assert "required project-genesis piece is not confirmed" in outcomes[0].reason
    assert "pending reflect-back" in outcomes[0].reason
    assert fake.calls == []
    assert ledger.read_binding(CHILD) is None
    assert spec.stat().st_mode & 0o200, "an unconfirmed spec must remain editable, not freeze"


@pytest.mark.parametrize(
    "intent",
    [
        _CONFIRMED_ROWS_WITHOUT_REFLECT_BACK,
        _CONFIRMED_STATUS_WITHOUT_AUTHORITY,
        _CONFIRMED_STATUS_WITHOUT_DATE,
        _CONFIRMED_STATUS_WITH_INVALID_DATE,
        _CONFIRMED_ROWS_WITH_PENDING_SCRIPT,
    ],
)
def test_confirmed_rows_without_answered_reflect_back_record_refuse_cascade_spawn(
    runtime,
    intent,
):
    workspace = _seed_l1(runtime)
    node = _prepare_project_node(runtime, intent=intent)
    spec = node / "client-brief" / "intent-spec.md"
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    _request_l2(workspace)

    outcomes = outbox.service_outbox(PARENT)

    assert len(outcomes) == 1 and outcomes[0].status == "rejected"
    assert "intent spec has no answered reflect-back confirmation record" in outcomes[0].reason
    assert "ask the owner, receive the answer, and record that answer" in outcomes[0].reason
    assert "confirmed by:" in outcomes[0].reason
    assert "date: YYYY-MM-DD" in outcomes[0].reason
    assert fake.calls == []
    assert ledger.read_binding(CHILD) is None
    assert spec.stat().st_mode & 0o200, "an unconfirmed spec must remain editable, not freeze"


def test_direct_chokepoint_types_missing_answered_reflect_back_record(runtime):
    _seed_l1(runtime)
    _prepare_project_node(runtime, intent=_CONFIRMED_ROWS_WITHOUT_REFLECT_BACK)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)

    result = chokepoint.register_and_spawn_child(
        PARENT,
        CHILD,
        child_level_config=config.get_level_config("L2"),
    )

    assert result.ok is False
    assert result.failure_class == "intent_spec_confirmation_missing"
    assert fake.calls == []
    assert ledger.read_binding(CHILD) is None


def test_direct_chokepoint_names_missing_intent_failure_class(runtime):
    _seed_l1(runtime)
    _prepare_project_node(runtime, intent=None)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)

    result = chokepoint.register_and_spawn_child(
        PARENT,
        CHILD,
        child_level_config=config.get_level_config("L2"),
    )

    assert result.ok is False
    assert result.failure_class == "intent_spec_missing"
    assert fake.calls == []
    assert ledger.read_binding(CHILD) is None


def test_existing_intent_receipt_refuses_changed_bytes_instead_of_silent_restamp(runtime):
    workspace = _seed_l1(runtime)
    node = _prepare_project_node(runtime, intent=_CONFIRMED)
    spec = node / "client-brief" / "intent-spec.md"
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    _request_l2(workspace)
    assert outbox.service_outbox(PARENT)[0].status == "spawned"
    prior = ledger.read_binding(CHILD)["intent_spec_receipt"]

    spec.chmod(0o644)
    spec.write_text(_CONFIRMED.replace("widget", "different widget"), encoding="utf-8")
    held, block = chokepoint.prepare_intent_spec_for_spawn(
        PARENT,
        CHILD,
        child_level="L2",
    )

    assert held is None
    assert block[0] == "intent_spec_revision_required"
    assert "ordinary spawn cannot re-stamp changed intent" in block[1]
    assert ledger.read_binding(CHILD)["intent_spec_receipt"] == prior


def test_fenced_l1_revision_is_legitimate_exit_then_l2_rebinds_explicitly(runtime):
    workspace = _seed_l1(runtime)
    node = _prepare_project_node(runtime, intent=_CONFIRMED)
    spec = node / "client-brief" / "intent-spec.md"
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    _request_l2(workspace)
    assert outbox.service_outbox(PARENT)[0].status == "spawned"
    old_receipt = ledger.read_binding(CHILD)["intent_spec_receipt"]
    candidate = workspace / "intent-revision-candidate.md"
    candidate.write_text(_REVISED, encoding="utf-8")
    token = ledger.read_binding(PARENT)["owner_token"]

    response = ipc.handle_request(
        {
            "command": "intent-revise",
            "addr": PARENT,
            "target_address": CHILD,
            "candidate_ref": str(candidate),
            "reason": "The client confirmed the amended widget scope.",
            "expected_owner_token": token,
        }
    )

    assert response["ok"] is True
    assert spec.read_text(encoding="utf-8") == _REVISED
    assert spec.stat().st_mode & 0o777 == 0o444
    version = ledger.read_binding(PARENT)["contract_versions"][str(spec.resolve())]
    assert version["fingerprint"] != old_receipt["fingerprint"]
    assert len(version["lineage"]) == 1
    assert contracts.stale_receipt_holders()[0]["holder_address"] == CHILD

    rebind_dir = addressing.contract_rebind_dir(CHILD, runtime)
    rebind_dir.mkdir()
    marker = rebind_dir / "adopt-revised-intent.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rebind_id": "adopt-revised-intent",
                "holder": CHILD,
                "owner_token": ledger.read_binding(CHILD)["owner_token"],
                "contract_path": str(spec.resolve()),
                "revision_record_ref": version["revision_record_ref"],
            }
        ),
        encoding="utf-8",
    )
    assert contracts.service_rebind_marker(marker, runtime_root=runtime) is True
    rebound = ledger.read_binding(CHILD)
    assert rebound["intent_spec_receipt"]["fingerprint"] == version["fingerprint"]
    held, block = chokepoint.prepare_intent_spec_for_spawn(
        PARENT,
        CHILD,
        child_level="L2",
    )
    assert block is None
    assert held["fingerprint"] == version["fingerprint"]


def test_intent_revision_refuses_pending_candidate_and_stale_l1_fence(runtime):
    workspace = _seed_l1(runtime)
    _prepare_project_node(runtime, intent=_CONFIRMED)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    _request_l2(workspace)
    assert outbox.service_outbox(PARENT)[0].status == "spawned"
    candidate = workspace / "pending-intent.md"
    candidate.write_text(_PENDING, encoding="utf-8")

    pending = chokepoint.revise_intent_spec(
        PARENT,
        target_address=CHILD,
        candidate_ref=str(candidate),
        reason="Not actually confirmed.",
        expected_owner_token=ledger.read_binding(PARENT)["owner_token"],
    )
    stale = chokepoint.revise_intent_spec(
        PARENT,
        target_address=CHILD,
        candidate_ref=str(candidate),
        reason="Stale caller.",
        expected_owner_token="stale-token",
    )

    assert not pending.ok and "pending reflect-back" in pending.errors[0]
    assert not stale.ok and "fencing abort" in stale.errors[0]
