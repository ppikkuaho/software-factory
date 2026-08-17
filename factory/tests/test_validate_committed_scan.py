"""Run-5 regression: whole-ledger validation reads committed writer shapes.

The pre-commit validator consumes an about-to-commit lifecycle row.  The
``harnessctl validate`` handler instead reads an already-committed checkpoint:
its applied effect may be the primary or related side of a multi-binding commit,
may advance generation without changing lifecycle state, and its raw WAL tail
may end in a later journal-only row.  These tests preserve the run-5
false-positive fixtures and keep malformed shape lookalikes outside admission.
"""

from __future__ import annotations

import json

import pytest

import harnessd.ipc as ipc
import harnessd.ledger as ledger
import harnessd.reconcile as reconcile


L1 = "L1/forge-queue#exec"
REVIEW = "L1/forge-queue#review"

@pytest.fixture
def runtime(tmp_path):
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = tmp_path
    try:
        yield tmp_path
    finally:
        ledger.RUNTIME_ROOT = previous


def _binding(
    address: str,
    *,
    state: str,
    generation: int,
    last_applied_seq: int,
    **extra,
) -> dict:
    binding = {
        "node_address": address,
        "state": state,
        "generation": generation,
        "owner_token": f"{address}:owner:1",
        "lease_epoch": 1,
        "last_applied_seq": last_applied_seq,
    }
    binding.update(extra)
    return binding


def _row(
    *,
    seq: int,
    address: str,
    event: str,
    from_state,
    to_state,
    expected_generation,
    generation,
    delta: dict,
    owner_token: str | None = None,
    lease_epoch: int | None = 1,
    related_binding_deltas: dict | None = None,
) -> dict:
    return ledger.build_wal_record(
        node_address=address,
        event=event,
        from_state=from_state,
        to_state=to_state,
        expected_generation=expected_generation,
        generation=generation,
        lease_epoch=lease_epoch,
        owner_token=owner_token or f"{address}:owner:1",
        binding_delta=delta,
        summary=event,
        artifacts=[],
        seq=seq,
        related_binding_deltas=related_binding_deltas,
    )


def _seed(bindings: dict[str, dict], rows: list[dict]) -> dict:
    for row in rows:
        ledger.append_wal(row)
    ledger.write_binding(bindings, _lock_held=True)
    return ipc.handle_request({"command": "validate"})


def test_run5_post_wake_accounting_and_rowless_planned_slot_validate_cleanly(runtime):
    """Preserve the corrected 13:38–13:45Z run-5 false-positive shapes."""
    running = _binding(
        L1,
        state="running",
        generation=4,
        last_applied_seq=3,
        last_inbox_acked_offset=913,
        wake_pending_ack_offset=None,
        wake_sent_transcript_size=None,
        wake_attempt_count=0,
        wake_cap_emitted_at=None,
    )
    planned = _binding(
        REVIEW,
        state="planned",
        generation=0,
        # Re-registration seeds the global WAL watermark even when this fresh
        # incarnation has no node-owned row.
        last_applied_seq=3,
    )
    rows = [
        _row(
            seq=1,
            address=L1,
            event="spawn_running",
            from_state="spawning",
            to_state="running",
            expected_generation=3,
            generation=4,
            delta={},
        ),
        _row(
            seq=2,
            address=L1,
            event="wake_verify_pending",
            from_state="running",
            to_state="running",
            expected_generation=4,
            generation=4,
            delta={
                "wake_pending_ack_offset": 913,
                "wake_sent_transcript_size": 221,
                "wake_attempt_count": 1,
            },
        ),
        _row(
            seq=3,
            address=L1,
            event="inbox_acked",
            from_state="running",
            to_state="running",
            expected_generation=4,
            generation=4,
            delta={
                "last_inbox_acked_offset": 913,
                "wake_pending_ack_offset": None,
                "wake_sent_transcript_size": None,
                "wake_attempt_count": 0,
                "wake_cap_emitted_at": None,
            },
        ),
    ]

    response = _seed({L1: running, REVIEW: planned}, rows)

    assert response == {
        "ok": True,
        "command": "validate",
        "errors": [],
        "warnings": [],
    }


def test_run5_live_primary_and_related_binding_effects_validate_cleanly(runtime):
    """Exact run-5 writer shapes: seq 10 related, seq 92 related, seq 504 message."""
    root = "L1#exec"
    l2 = "L1/forge-queue#exec"
    producer = "L1/forge-queue/persistence#exec"
    review = "L1/forge-queue/client-cli#review"
    reviewer = (
        "L1/forge-queue/client-cli/reviews/gate-d6b4570b80895b4e/"
        "reviewers/risk-readiness#exec"
    )
    root_token = "L1#exec:subagent-l1-root:genesis-l1-root:2"
    l2_token = (
        "L1/forge-queue#exec:subagent-L1__forge-queue__exec:"
        "registered-L1__forge-queue__exec:2"
    )
    producer_token = (
        "L1/forge-queue/persistence#exec:"
        "subagent-L1__forge-queue__persistence__exec:"
        "registered-L1__forge-queue__persistence__exec:2"
    )
    review_token = (
        "L1/forge-queue/client-cli#review:"
        "subagent-L1__forge-queue__client-cli__review:"
        "registered-L1__forge-queue__client-cli__review:2"
    )
    reviewer_token = f"{reviewer}:owner:1"
    contract = {"brief.md": {"fingerprint": "b16e5d4f"}}
    messages_delta = {"ruling": {"message_id": "ruling"}}
    bindings = {
        root: _binding(
            root,
            state="running",
            generation=4,
            last_applied_seq=10,
            owner_token=root_token,
            lease_epoch=2,
            contract_versions=contract,
        ),
        l2: _binding(
            l2,
            state="running",
            generation=31,
            last_applied_seq=504,
            owner_token=l2_token,
            lease_epoch=2,
            contract_receipts=contract,
            messages=messages_delta,
            message_last_id="ruling",
        ),
        producer: _binding(
            producer,
            state="running",
            generation=7,
            last_applied_seq=505,
            owner_token=producer_token,
            lease_epoch=2,
            gate_state="candidate_submitted",
            messages=messages_delta,
            message_last_id="ruling",
        ),
        review: _binding(
            review,
            state="running",
            generation=8,
            last_applied_seq=92,
            owner_token=review_token,
            lease_epoch=2,
            contract_versions=contract,
        ),
        reviewer: _binding(
            reviewer,
            state="claimed",
            generation=2,
            last_applied_seq=92,
            owner_token=reviewer_token,
            lease_epoch=1,
            contract_receipts=contract,
        ),
    }
    rows = [
        _row(
            seq=10,
            address=l2,
            event="spawn_contracts_receipted",
            from_state="claimed",
            to_state="claimed",
            expected_generation=1,
            generation=2,
            owner_token=l2_token,
            lease_epoch=2,
            delta={"contract_receipts": contract},
            related_binding_deltas={
                root: {
                    "from_state": "running",
                    "to_state": "running",
                    "expected_generation": 3,
                    "generation": 4,
                    "lease_epoch": 2,
                    "owner_token": root_token,
                    "binding_delta": {"contract_versions": contract},
                }
            },
        ),
        _row(
            seq=92,
            address=reviewer,
            event="spawn_contracts_receipted",
            from_state="claimed",
            to_state="claimed",
            expected_generation=1,
            generation=2,
            owner_token=reviewer_token,
            delta={"contract_receipts": contract},
            related_binding_deltas={
                review: {
                    "from_state": "running",
                    "to_state": "running",
                    "expected_generation": 7,
                    "generation": 8,
                    "lease_epoch": 2,
                    "owner_token": review_token,
                    "binding_delta": {"contract_versions": contract},
                }
            },
        ),
        _row(
            seq=504,
            address=l2,
            event="message_recorded",
            from_state="running",
            to_state="running",
            expected_generation=30,
            generation=31,
            owner_token=l2_token,
            lease_epoch=2,
            delta={"messages": messages_delta, "message_last_id": "ruling"},
        ),
        _row(
            seq=505,
            address=producer,
            event="message_recorded",
            from_state="running",
            to_state="running",
            expected_generation=6,
            generation=7,
            owner_token=producer_token,
            lease_epoch=2,
            delta={"messages": messages_delta, "message_last_id": "ruling"},
        ),
    ]

    response = _seed(bindings, rows)

    assert response == {
        "ok": True,
        "command": "validate",
        "errors": [],
        "warnings": [],
    }


def test_committed_scan_refuses_illegal_and_shape_mimicking_foreign_rows(runtime):
    """The read discriminator is not softer than the writer classes it recognizes."""
    illegal = "L1/illegal#exec"
    wrong_slice = "L1/wrong-slice#exec"
    wrong_generation = "L1/wrong-generation#exec"
    wrong_identity = "L1/wrong-identity#exec"
    empty_noop = "L1/empty-noop#exec"
    foreign = "L1/foreign-target#exec"
    foreign_writer = "L1/other#exec"
    bindings = {
        illegal: _binding(
            illegal,
            state="planned",
            generation=4,
            last_applied_seq=1,
        ),
        wrong_slice: _binding(
            wrong_slice,
            state="running",
            generation=4,
            last_applied_seq=2,
        ),
        wrong_generation: _binding(
            wrong_generation,
            state="running",
            generation=4,
            last_applied_seq=3,
            marker="present",
        ),
        wrong_identity: _binding(
            wrong_identity,
            state="running",
            generation=4,
            last_applied_seq=5,
            marker="present",
        ),
        empty_noop: _binding(
            empty_noop,
            state="running",
            generation=4,
            last_applied_seq=6,
        ),
        foreign: _binding(
            foreign,
            state="running",
            generation=4,
            last_applied_seq=4,
        ),
        foreign_writer: _binding(
            foreign_writer,
            state="running",
            generation=4,
            last_applied_seq=4,
            marker="foreign",
        ),
    }
    rows = [
        _row(
            seq=1,
            address=illegal,
            event="illegal_regression",
            from_state="running",
            to_state="planned",
            expected_generation=3,
            generation=4,
            delta={},
        ),
        _row(
            seq=2,
            address=wrong_slice,
            event="shape_mimic",
            from_state="running",
            to_state="running",
            expected_generation=4,
            generation=4,
            delta={"node_address": foreign_writer},
        ),
        _row(
            seq=3,
            address=wrong_generation,
            event="shape_mimic_wrong_generation",
            from_state="running",
            to_state="running",
            expected_generation=5,
            generation=5,
            delta={"marker": "present"},
        ),
        _row(
            seq=4,
            address=foreign_writer,
            event="foreign_binding_write",
            from_state="running",
            to_state="running",
            expected_generation=4,
            generation=4,
            delta={"marker": "foreign"},
        ),
        _row(
            seq=5,
            address=wrong_identity,
            event="shape_mimic_wrong_identity",
            from_state="running",
            to_state="running",
            expected_generation=3,
            generation=4,
            owner_token=f"{wrong_identity}:other-owner:2",
            lease_epoch=2,
            delta={"marker": "present"},
        ),
        _row(
            seq=6,
            address=empty_noop,
            event="shape_mimic_empty_noop",
            from_state="running",
            to_state="running",
            expected_generation=4,
            generation=4,
            delta={},
        ),
    ]

    response = _seed(bindings, rows)

    assert response["ok"] is False
    errors = "\n".join(response["errors"])
    assert illegal in errors and "illegal lifecycle transition" in errors
    assert wrong_slice in errors and "node_address" in errors
    assert wrong_generation in errors and "generation" in errors
    assert wrong_identity in errors and "owner_token" in errors and "lease_epoch" in errors
    assert empty_noop in errors and "illegal empty no-op" in errors
    assert foreign in errors and "no committed WAL row" in errors


def test_s5_and_t14_rows_validate_by_applied_shape_not_later_journal(runtime):
    """Same-day S5/T-14 rows use lifecycle, own-slice, and journal shapes."""
    streak = "L1/streak#exec"
    parked = "L1/parked#exec"
    escalated = "L1/escalated#exec"
    paused = "L1/paused#exec"
    bindings = {
        streak: _binding(
            streak,
            state="running",
            generation=7,
            last_applied_seq=1,
            consecutive_failed_incarnations=0,
            failed_incarnation_causes=[],
            respawn_parked_at=None,
        ),
        parked: _binding(
            parked,
            state="failed",
            generation=8,
            last_applied_seq=2,
            consecutive_failed_incarnations=3,
            failed_incarnation_causes=["one", "two", "three"],
            respawn_parked_at="2026-07-28T10:00:00+00:00",
        ),
        escalated: _binding(
            escalated,
            state="running",
            generation=9,
            last_applied_seq=4,
            gate_state="gate_escalated",
            gate_state_before="gate_bounced",
            gate_escalation_count=5,
        ),
        paused: _binding(
            paused,
            state="running",
            generation=2,
            last_applied_seq=6,
            paused_at="2026-07-28T10:05:00+00:00",
        ),
    }
    rows = [
        _row(
            seq=1,
            address=streak,
            event="seat_respawn_streak_reset",
            from_state="running",
            to_state="running",
            expected_generation=7,
            generation=7,
            delta={
                "consecutive_failed_incarnations": 0,
                "failed_incarnation_causes": [],
                "respawn_parked_at": None,
            },
        ),
        _row(
            seq=2,
            address=parked,
            event="watchdog_nonresponse",
            from_state="running",
            to_state="failed",
            expected_generation=7,
            generation=8,
            delta={
                "consecutive_failed_incarnations": 3,
                "failed_incarnation_causes": ["one", "two", "three"],
                "respawn_parked_at": "2026-07-28T10:00:00+00:00",
            },
        ),
        _row(
            seq=3,
            address=parked,
            event="seat_respawn_parked",
            from_state="failed",
            to_state="failed",
            expected_generation=None,
            generation=None,
            delta={"causes": ["one", "two", "three"]},
        ),
        _row(
            seq=4,
            address=escalated,
            event="gate_escalated",
            from_state="running",
            to_state="running",
            expected_generation=8,
            generation=9,
            delta={
                "gate_state": "gate_escalated",
                "gate_state_before": "gate_bounced",
                "gate_escalation_count": 5,
            },
        ),
        _row(
            seq=5,
            address=escalated,
            event="gate_escalation_nonconverging",
            from_state="running",
            to_state="running",
            expected_generation=None,
            generation=None,
            delta={"gate_escalation_count": 5, "threshold": 5},
        ),
        _row(
            seq=6,
            address=paused,
            event="paused",
            from_state="running",
            to_state="running",
            expected_generation=2,
            generation=2,
            delta={"paused_at": "2026-07-28T10:05:00+00:00"},
        ),
        _row(
            seq=7,
            address=paused,
            event="gate_escalation_nonconverging",
            from_state="running",
            to_state="running",
            expected_generation=None,
            generation=None,
            delta={"gate_escalation_count": 10, "threshold": 10},
        ),
    ]

    response = _seed(bindings, rows)

    assert response["ok"] is True
    assert response["command"] == "validate"
    assert response["errors"] == []
    assert len(response["warnings"]) == 1
    assert escalated in response["warnings"][0]
    assert "gate escalation" in response["warnings"][0]
