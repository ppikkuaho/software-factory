"""Root-distribution integration and execution surface for composition-gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from composition_gate.decision import DecisionError, prepare_decision, render_decision
from composition_gate.packet import (
    DecisionSnapshot,
    PacketError,
    SnapshotReader,
    allocate_attempt,
    prepare_failure_packet,
    prepare_packet,
)
from composition_gate.stage2 import (
    ClaudeGenerator,
    Generator,
    Stage2Error,
    prepare_technical_failure,
    run_stage2,
)

FINALIZATION_FORMAT = "composition-gate-finalization.v1"


def _enforce_and_commit(root_obj, plan) -> None:
    """Lazy pipeline seam retained for source-only dry-run installations/tests."""

    from .pipeline import enforce_and_commit

    enforce_and_commit(root_obj, plan)


def _execution_error(exc: Exception) -> DecisionError:
    if isinstance(exc, DecisionError):
        return exc
    message = getattr(exc, "message", str(exc))
    return DecisionError("finalization-failed", message)


def finalize_prepared(root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Run the one root-owned mutexed finalizer over already prepared evidence."""

    from .commands._common import Ctx
    from .commands.cgate import build_finalization_plan
    from .mutex import global_mutex
    from .paths import Root

    root_obj = Root(Path(root).expanduser().resolve())
    try:
        with global_mutex(root_obj):
            plan, result = build_finalization_plan(Ctx(root_obj, "cgate"), payload)
            _enforce_and_commit(root_obj, plan)
    except Exception as exc:
        raise _execution_error(exc) from exc
    return result


def _execute_decision(
    root: str | Path,
    record_id: str,
    *,
    generator: Generator | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Prepare outside the mutex, then invoke exactly one compound finalizer."""

    if os.environ.get("HT_ROLE") != "cgate":
        raise DecisionError(
            "role-required",
            "ht-cgate decide --execute requires HT_ROLE=cgate",
        )
    resolved = Path(root).expanduser().resolve()
    decision = prepare_decision(resolved, record_id)
    stage2_prepared: dict[str, Any] | None = None
    if decision["route"] == "stage2":
        fingerprints = decision["fingerprints"]
        snapshot = DecisionSnapshot(
            resolved,
            fingerprints["decision_head"],
            fingerprints["decision_tree"],
        )
        allocated_attempt = allocate_attempt(
            resolved, record_id, attempt_id=attempt_id
        )
        packet_failure: PacketError | None = None
        try:
            prepared_packet = prepare_packet(
                resolved,
                record_id,
                snapshot=snapshot,
                allocated_attempt=allocated_attempt,
            )
        except PacketError as exc:
            packet_failure = exc
            prepared_packet = prepare_failure_packet(
                resolved,
                record_id,
                snapshot=snapshot,
                allocated_attempt=allocated_attempt,
                error=exc,
            )
        source = SnapshotReader(snapshot).read(
            f"tier1/merge-records/{record_id}.json"
        )
        try:
            record = json.loads(source.content.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DecisionError(
                "merge-record-invalid",
                "committed merge record became unreadable before stage 2",
            ) from exc
        if packet_failure is not None:
            stage2_prepared = prepare_technical_failure(
                prepared_packet,
                resolved,
                Stage2Error(packet_failure.kind, packet_failure.message),
            ).as_dict()
        else:
            chosen_generator = generator if generator is not None else ClaudeGenerator()
            stage2_prepared = run_stage2(
                prepared_packet,
                resolved,
                record,
                decision["rules_fired"],
                chosen_generator,
            ).as_dict()
    payload = {
        "format": FINALIZATION_FORMAT,
        "decision": decision,
        "stage2": stage2_prepared,
    }
    return finalize_prepared(resolved, payload)


def execute_decision(
    root: str | Path,
    record_id: str,
    *,
    generator: Generator | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Operator-facing wrapper with stable fail-closed preparation errors."""

    try:
        return _execute_decision(
            root,
            record_id,
            generator=generator,
            attempt_id=attempt_id,
        )
    except (PacketError, Stage2Error) as exc:
        raise DecisionError(exc.kind, exc.message) from exc


def render_execution(result: dict[str, Any]) -> str:
    return json.dumps(
        result,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


__all__ = [
    "DecisionError",
    "execute_decision",
    "finalize_prepared",
    "prepare_decision",
    "render_decision",
    "render_execution",
]
