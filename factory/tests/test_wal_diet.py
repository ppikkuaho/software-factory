"""Owner-ratified WAL diet: executable event inventory and recovery equivalence.

The classification below is the machine-readable authority.  Every authored
mint discovered from the harness source must be classified; runtime docs point
here rather than maintaining a second living vocabulary.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import harnessd
from harnessd import executor, ledger, reconcile
from harnessd.wal_policy import (
    CONDITIONAL,
    EVIDENCE_CONSUMER,
    EVENT_CLASSIFICATION,
    NODE_LOCAL,
    RECOVERY_CONSUMER,
    REPLAY,
    epoch_fenced,
)


def test_blocked_input_control_edges_are_replay_classified():
    assert EVENT_CLASSIFICATION["seat_stalled"] == REPLAY
    assert EVENT_CLASSIFICATION["seat_stall_actioned"] == REPLAY
    assert EVENT_CLASSIFICATION["seat_stall_recovered"] == REPLAY
    assert reconcile.wal_policy.EVENT_CLASSIFICATION is EVENT_CLASSIFICATION


def test_epoch_fence_marks_only_rows_below_the_live_registration_epoch():
    """The disposition axis re-registration moves: which incarnation owns a committed row.

    A replay-classified event name says the row's CLASS is a binding effect; the epoch says
    whether it is THIS incarnation's.  Generation cannot answer it — re-registration resets
    generation to 0, so a retired row's pre-image can match the fresh registration by
    coincidence.  Absent epochs never fence: a journal-only row carries none, and an
    unepoched binding presents no incarnation to fence against.
    """
    live = {"lease_epoch": 7}

    assert epoch_fenced(live, {"event": "died_infrastructure", "lease_epoch": 6})
    assert not epoch_fenced(live, {"event": "died_infrastructure", "lease_epoch": 7})
    assert not epoch_fenced(live, {"event": "claim", "lease_epoch": 8})
    assert not epoch_fenced(live, {"event": "boot_confirm_pending", "lease_epoch": None})
    assert not epoch_fenced({}, {"event": "claim", "lease_epoch": 6})
    # bool is an int subclass; a True/False epoch is malformed, not an incarnation.
    assert not epoch_fenced(live, {"event": "claim", "lease_epoch": True})


def _literal_values(expr: ast.AST | None, constants: dict[str, str]) -> set[str]:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.Name) and expr.id in constants:
        return {constants[expr.id]}
    if isinstance(expr, ast.IfExp):
        return _literal_values(expr.body, constants) | _literal_values(
            expr.orelse, constants
        )
    if isinstance(expr, ast.BoolOp):
        return set().union(
            *(_literal_values(value, constants) for value in expr.values)
        )
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "get"
        and len(expr.args) > 1
        and isinstance(expr.args[0], ast.Constant)
        and expr.args[0].value == "event"
    ):
        return _literal_values(expr.args[1], constants)
    return set()


def _authored_event_types() -> set[str]:
    """Resolve the finite authored event vocabulary without executing the daemon."""
    found: set[str] = set()
    root = Path(harnessd.__file__).resolve().parent
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not (
                isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                positional = node.args.args
                defaults = [None] * (
                    len(positional) - len(node.args.defaults)
                ) + list(node.args.defaults)
                for argument, default in zip(positional, defaults):
                    if argument.arg == "event":
                        found.update(_literal_values(default, constants))
                for argument, default in zip(
                    node.args.kwonlyargs, node.args.kw_defaults
                ):
                    if argument.arg == "event":
                        found.update(_literal_values(default, constants))

            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == "event":
                        found.update(_literal_values(node.value, constants))
                    if (
                        isinstance(target, ast.Name)
                        and target.id == "_COLLAPSE_EVENTS"
                        and isinstance(node.value, ast.Dict)
                    ):
                        for value in node.value.values:
                            found.update(_literal_values(value, constants))

            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            else:
                call_name = ""
            for keyword in node.keywords:
                if keyword.arg == "event":
                    found.update(_literal_values(keyword.value, constants))
                if call_name == "_deliver_keystroke" and keyword.arg == "kind":
                    found.update(
                        value + "_send_failed"
                        for value in _literal_values(keyword.value, constants)
                    )
                if (
                    call_name == "_record_nonterminal_marker_error"
                    and keyword.arg == "marker_kind"
                ):
                    found.update(
                        value + "_marker_invalid"
                        for value in _literal_values(keyword.value, constants)
                    )
            if call_name == "_journal_kickoff_event" and len(node.args) >= 2:
                found.update(_literal_values(node.args[1], constants))
    return found


def test_every_authored_wal_event_is_classified():
    discovered = _authored_event_types()
    assert discovered == set(EVENT_CLASSIFICATION), (
        "WAL event classification drifted: "
        f"unclassified={sorted(discovered - set(EVENT_CLASSIFICATION))}; "
        f"no-longer-minted={sorted(set(EVENT_CLASSIFICATION) - discovered)}"
    )
    assert set(EVENT_CLASSIFICATION.values()) == {
        REPLAY,
        RECOVERY_CONSUMER,
        EVIDENCE_CONSUMER,
        CONDITIONAL,
        NODE_LOCAL,
    }


def _binding(address: str, *, last_applied_seq: int = 0, **extra) -> dict:
    binding = {
        "node_address": address,
        "state": "running",
        "generation": 0,
        "last_applied_seq": last_applied_seq,
        "lease_epoch": 1,
        "owner_token": f"{address}:sub:session:1",
    }
    binding.update(extra)
    return binding


def _wal(
    *,
    seq: int,
    address: str,
    event: str,
    expected_generation,
    generation,
    delta: dict,
    from_state: str = "running",
    to_state: str = "running",
) -> dict:
    return ledger.build_wal_record(
        node_address=address,
        event=event,
        from_state=from_state,
        to_state=to_state,
        expected_generation=expected_generation,
        generation=generation,
        lease_epoch=1,
        owner_token=f"{address}:sub:session:1",
        binding_delta=delta,
        summary="WAL diet recovery-equivalence fixture",
        artifacts=[],
        seq=seq,
    )


def _normalized_bytes(bindings: dict) -> bytes:
    return json.dumps(
        bindings,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def test_dieted_and_legacy_full_wal_rebuild_byte_identical_binding_maps():
    """Diet replay matches legacy with pending lifecycle and own-slice rows."""
    producer = "project/area#exec"
    sender = "project/area/task#exec"
    l1 = "project#exec"
    parked = "project/area/parked#exec"
    notified = "project/area/notified#exec"
    paused = "project/area/paused#exec"
    observed = {
        "turn_state": "turn_running",
        "turn_state_event_id": "turn-1",
        "turn_event_acked_offset": 123,
        "turn_hook_health": "healthy",
    }
    legacy_bindings = {
        producer: _binding(producer, last_applied_seq=1, **observed),
        sender: _binding(sender),
        l1: _binding(l1),
        parked: _binding(parked),
        notified: _binding(notified),
        paused: _binding(paused),
    }
    dieted_bindings = copy.deepcopy(legacy_bindings)
    dieted_bindings[producer]["last_applied_seq"] = 0

    legacy_observation = _wal(
        seq=1,
        address=producer,
        event="turn_state_observed",
        expected_generation=0,
        generation=0,
        delta=observed,
    )
    gate = _wal(
        seq=2,
        address=producer,
        event="gate_bounced",
        expected_generation=0,
        generation=1,
        delta={"gate_state": "gate_bounced", "gate_bounce_count": 1},
    )
    message = _wal(
        seq=3,
        address=sender,
        event="message_recorded",
        expected_generation=0,
        generation=1,
        delta={
            "messages": {
                "m-1": {
                    "message_id": "m-1",
                    "target": producer,
                    "content_sha256": "abc",
                }
            },
            "message_last_id": "m-1",
        },
    )
    delegation = _wal(
        seq=4,
        address=l1,
        event="fidelity_playback_authority_delegated",
        expected_generation=0,
        generation=0,
        delta={
            "fidelity_playback_authority": "operator-delegate",
            "fidelity_playback_delegate": "synthetic-owner",
        },
    )
    park_transition = _wal(
        seq=5,
        address=parked,
        event="died_infrastructure",
        expected_generation=0,
        generation=1,
        to_state="failed",
        delta={
            "terminal_signal": "DIED_INFRA",
            "respawn_parked_at": "2026-07-28T12:00:00+00:00",
            "failed_incarnation_count": 3,
            "failed_incarnation_causes": ["one", "two", "three"],
        },
    )
    park_audit = _wal(
        seq=6,
        address=parked,
        event="seat_respawn_parked",
        expected_generation=None,
        generation=None,
        delta={"failed_incarnation_causes": ["one", "two", "three"]},
        from_state="failed",
        to_state="failed",
    )
    wake_cap = _wal(
        seq=7,
        address=notified,
        event="wake_cap",
        expected_generation=0,
        generation=0,
        delta={
            "wake_attempt_count": 3,
            "wake_pending_ack_offset": 88,
            "wake_cap_emitted_at": "2026-07-28T12:00:00+00:00",
        },
    )
    pause = _wal(
        seq=8,
        address=paused,
        event="paused",
        expected_generation=0,
        generation=0,
        delta={"paused_at": "2026-07-28T12:00:01+00:00"},
    )
    streak_reset = _wal(
        seq=9,
        address=parked,
        event="seat_respawn_streak_reset",
        expected_generation=1,
        generation=1,
        delta={
            "failed_incarnation_count": 0,
            "failed_incarnation_causes": [],
            "respawn_parked_at": None,
        },
        from_state="failed",
        to_state="failed",
    )
    common = [
        gate,
        message,
        delegation,
        park_transition,
        park_audit,
        wake_cap,
        pause,
        streak_reset,
    ]

    rebuilt_from_full = reconcile.replay_wal(
        legacy_bindings, [legacy_observation, *common]
    )
    rebuilt_from_diet = reconcile.replay_wal(dieted_bindings, common)

    assert _normalized_bytes(rebuilt_from_diet) == _normalized_bytes(
        rebuilt_from_full
    )
    assert rebuilt_from_diet[l1]["fidelity_playback_authority"] == "operator-delegate"
    assert rebuilt_from_diet[notified]["wake_attempt_count"] == 3
    assert rebuilt_from_diet[paused]["paused_at"] == "2026-07-28T12:00:01+00:00"
    assert rebuilt_from_diet[parked]["failed_incarnation_count"] == 0


def test_ordinary_turn_state_checkpoint_updates_binding_without_wal(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    address = "project/work#exec"
    binding = _binding(address)
    ledger.write_binding({address: binding}, _lock_held=True)

    result = executor.turn_state_checkpoint(
        address,
        expected_owner_token=binding["owner_token"],
        delta={
            "turn_state": "turn_running",
            "turn_state_event_id": "turn-1",
            "turn_event_acked_offset": 99,
        },
    )

    assert result.ok
    assert ledger.read_binding(address)["turn_event_acked_offset"] == 99
    assert [
        row for row in ledger.load_wal() if row.get("event") == "turn_state_observed"
    ] == []


def test_watchdog_checkpoint_replays_every_field_the_writer_commits(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    address = "project/watchdog#exec"
    binding = _binding(
        address,
        stale_check_count=2,
        last_evidence="pane_idle",
        last_progress_at="2026-07-28T11:00:00+00:00",
    )
    initial = copy.deepcopy(binding)
    ledger.write_binding({address: binding}, _lock_held=True)

    result = executor.watchdog_checkpoint(
        address,
        condition="healthy",
        liveness_state="working",
        last_progress_at="2026-07-28T12:00:00+00:00",
        last_evidence="turn_state",
        expected_owner_token=binding["owner_token"],
        gate_crossed_at="2026-07-28T11:59:00+00:00",
    )

    assert result.ok and result.appended
    rebuilt = reconcile.replay_wal({address: initial}, ledger.load_wal())
    assert _normalized_bytes(rebuilt) == _normalized_bytes(ledger.all_nodes())
