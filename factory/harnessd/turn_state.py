"""Durable runtime-hook turn state, live owed checklist, and turn-end contract.

Hook subprocesses write only seat-qualified node-local files. The live run daemon adopts those
observations into binding/WAL state through the executor; hooks never write the ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harnessd import (
    addressing,
    clock,
    detector_signals,
    ledger,
    messages,
    return_contract,
    states,
    store,
)

SCHEMA_VERSION = 1

CLAUDE_FULL_EDGES = "claude_full_edges"
CODEX_TURN_END_ONLY = "codex_turn_end_only"
HOOKLESS_FALLBACK = "hookless_fallback"

TURN_NOT_STARTED = "not_started"
TURN_RUNNING = "turn_running"
TOOL_IN_FLIGHT = "tool_in_flight"
WAITING_ON_HUMAN = "waiting_on_human"
TURN_ENDED = "turn_ended"

PRODUCT_PRESENT = "product_present"
LEDGER_WAIT = "ledger_wait"
PROD_REQUIRED = "prod_required"

RAW_HOOK_EVENT = "raw_hook_event"
ADOPTED_HOOK_EVENT = "adopted_hook_event"
HOOK_RESPONSE = "hook_response"
_SYNC_RESPONSE_EVENTS = frozenset({"PostToolUse", "PostToolUseFailure", "Stop"})


class HookResponseTimeout(RuntimeError):
    """The daemon did not publish the exact callback response inside the hook budget."""


@dataclass(frozen=True)
class CurrentObservation:
    status: str
    payload: dict | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ExitDecision:
    decision: str
    reasons: tuple[str, ...]
    checklist: dict
    message: str | None = None


def profile_for_runtime(runtime: str | None) -> str:
    if runtime == "claude-code":
        return CLAUDE_FULL_EDGES
    if runtime == "codex":
        return CODEX_TURN_END_ONLY
    return HOOKLESS_FALLBACK


def hook_entry_path() -> Path:
    return Path(__file__).with_name("turn_state_hook.py").resolve()


def hook_owner_token(*, runtime_root: str | Path, node_address: str) -> str:
    """Read the incarnation token at adapter-install time, never at hook callback time."""
    path = addressing.signoff_path(node_address, runtime_root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    token = str(payload.get("owner_token") or "")
    if not token:
        raise ValueError(f"sign-off handshake {path} carries no owner_token")
    return token


def hook_argv(
    *,
    python_executable: str,
    runtime_root: str | Path,
    node_address: str,
    owner_token: str,
    runtime: str,
) -> list[str]:
    return [
        str(python_executable),
        str(hook_entry_path()),
        "--runtime-root",
        str(runtime_root),
        "--node-address",
        node_address,
        "--owner-token",
        owner_token,
        "--runtime",
        runtime,
    ]


def _binding(runtime_root: Path, node_address: str) -> dict | None:
    return ledger.read_binding(
        node_address,
        binding_path=runtime_root / ledger.BINDING_FILENAME,
    )


def _bindings(runtime_root: Path) -> dict[str, dict]:
    return ledger.all_nodes(binding_path=runtime_root / ledger.BINDING_FILENAME)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _checklist_version(items: list[dict]) -> str:
    surface = [
        {
            "item_id": item.get("item_id"),
            "ok": bool(item.get("ok")),
            "defects": list(item.get("defects") or []),
        }
        for item in items
    ]
    return hashlib.sha256(_json_bytes(surface)).hexdigest()


def _terminal_item(node_address: str, binding: dict, *, runtime_root: str | Path) -> dict:
    node = {"node_address": node_address}
    observation = detector_signals.observe_terminal_signal(
        node,
        binding,
        runtime_root=runtime_root,
    )
    signal = (
        str((observation.payload or {}).get("signal") or "").upper()
        if observation.status == "valid"
        else ""
    )
    ok = signal in {"DONE", "FAILED", "ESCALATED"}
    defects: list[str] = []
    if not ok:
        if observation.status == "malformed":
            defects.append(f"MALFORMED-TERMINAL-SIGNAL: {observation.reason}")
        elif observation.status == "stale":
            defects.append("STALE-TERMINAL-SIGNAL: the signal owner_token is not current")
        else:
            defects.append(
                "MISSING-TERMINAL-SIGNAL: when ending, write DONE or FAILED; when blocked, "
                "submit a needs_answer question and park"
            )
    return {
        "item_id": "terminal_signoff",
        "label": (
            "When ending, write the fenced terminal sign-off: DONE or FAILED. When blocked, "
            "submit a needs_answer question and park."
        ),
        "ok": ok,
        "defects": defects,
        "signal": signal or None,
    }


def build_checklist(
    node_address: str,
    binding: dict,
    *,
    runtime_root: str | Path,
    profile: str | None = None,
    bindings: dict[str, dict] | None = None,
) -> dict:
    """Project the live checklist from the ONE return-contract walk plus terminal sign-off."""
    root = Path(runtime_root)
    live_bindings = bindings if bindings is not None else _bindings(root)
    walk = return_contract.walk_done_contract(
        node_address,
        binding,
        runtime_root=root,
        bindings=live_bindings,
    )
    items = [
        {
            "item_id": item.item_id,
            "label": item.label,
            "ok": item.ok,
            "defects": list(item.defects),
        }
        for item in walk.items
    ]
    for question in messages.open_questions_for(
        node_address,
        bindings=live_bindings,
    ):
        source = str(question.get("source") or "")
        message_id = str(question.get("message_id") or "")
        items.append(
            {
                "item_id": f"question:{source}:{message_id}",
                "label": (
                    f"Answer open question {message_id!r} from {source}; send an ordinary "
                    "message with answers_question referencing it."
                ),
                "ok": False,
                "defects": [
                    f"OPEN-QUESTION: read {question.get('artifact') or 'the message artifact'}"
                ],
                "question_ref": {
                    "asker_address": source,
                    "message_id": message_id,
                },
            }
        )
    items.append(_terminal_item(node_address, binding, runtime_root=root))
    return {
        "schema_version": SCHEMA_VERSION,
        "node_address": node_address,
        "owner_token": binding.get("owner_token"),
        "hook_profile": profile or binding.get("turn_hook_profile") or HOOKLESS_FALLBACK,
        "updated_at": clock.now_utc(),
        "version": _checklist_version(items),
        "items": items,
        "open_item_ids": [item["item_id"] for item in items if not item["ok"]],
    }


def _write_json_atomic(path: Path, payload: dict) -> None:
    store.atomic_replace(
        path,
        lambda handle: (
            handle.write(json.dumps(payload, indent=2, sort_keys=True)),
            handle.write("\n"),
        ),
    )


def _write_empty_durable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _append_event(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def seed(
    node_address: str,
    binding: dict,
    *,
    runtime_root: str | Path,
    profile: str,
) -> dict:
    """Materialize the incarnation-local hook/checklist surface before actor open."""
    root = Path(runtime_root)
    lock_path = addressing.turn_state_lock_path(node_address, root)
    with store.file_lock(lock_path, shared=False):
        checklist = build_checklist(
            node_address,
            binding,
            runtime_root=root,
            profile=profile,
        )
        state = {
            "schema_version": SCHEMA_VERSION,
            "node_address": node_address,
            "owner_token": binding.get("owner_token"),
            "hook_profile": profile,
            "state": TURN_NOT_STARTED,
            "in_flight_tools": [],
            "waiting_on_human_tool_id": None,
            "waiting_on_human_tool_name": None,
            "revision": 0,
            "updated_at": clock.now_utc(),
            "last_hook_event": None,
            "checklist_version": checklist["version"],
            "open_item_ids": checklist["open_item_ids"],
            "exit_decision": None,
            "exit_delivery": None,
            "prod_dispatched_event_id": None,
        }
        _write_empty_durable(addressing.turn_events_path(node_address, root))
        _write_json_atomic(addressing.owed_checklist_path(node_address, root), checklist)
        _write_json_atomic(addressing.turn_state_path(node_address, root), state)
    return {
        "turn_hook_profile": profile,
        "turn_runtime_root": str(root),
        "turn_state_path": str(addressing.turn_state_path(node_address, root)),
        "turn_events_path": str(addressing.turn_events_path(node_address, root)),
        "owed_checklist_path": str(addressing.owed_checklist_path(node_address, root)),
        "turn_state_lock_path": str(lock_path),
    }


def read_current(
    node_address: str,
    binding: dict,
    *,
    runtime_root: str | Path,
) -> CurrentObservation:
    path = addressing.turn_state_path(node_address, runtime_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CurrentObservation(status="missing", reason=f"{path} is absent")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return CurrentObservation(
            status="malformed",
            reason=f"{type(exc).__name__}: {exc}",
        )
    if not isinstance(payload, dict):
        return CurrentObservation(
            status="malformed",
            reason=f"payload is {type(payload).__name__}, not an object",
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        return CurrentObservation(
            status="malformed",
            reason=f"unsupported schema_version={payload.get('schema_version')!r}",
        )
    if payload.get("node_address") != node_address:
        return CurrentObservation(
            status="malformed",
            reason=f"node_address={payload.get('node_address')!r} does not match {node_address!r}",
        )
    if payload.get("owner_token") != binding.get("owner_token"):
        return CurrentObservation(status="stale", payload=payload, reason="owner_token mismatch")
    if payload.get("hook_fault"):
        return CurrentObservation(
            status="malformed",
            payload=payload,
            reason=f"hook callback fault: {payload.get('hook_fault')}",
        )
    if payload.get("state") not in {
        TURN_NOT_STARTED,
        TURN_RUNNING,
        TOOL_IN_FLIGHT,
        WAITING_ON_HUMAN,
        TURN_ENDED,
    }:
        return CurrentObservation(
            status="malformed",
            payload=payload,
            reason=f"unknown state={payload.get('state')!r}",
        )
    return CurrentObservation(status="valid", payload=payload)


def checklist_changes(before: dict | None, after: dict) -> list[dict]:
    old = {
        item.get("item_id"): bool(item.get("ok"))
        for item in (before or {}).get("items", [])
        if isinstance(item, dict)
    }
    changes: list[dict] = []
    for item in after.get("items", []):
        item_id = item.get("item_id")
        now_ok = bool(item.get("ok"))
        if item_id not in old:
            continue
        if old[item_id] == now_ok:
            continue
        changes.append(
            {
                "item_id": item_id,
                "change": "landed" if now_ok else "reopened",
                "label": item.get("label"),
            }
        )
    return changes


def _read_checklist(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def refresh_checklist(
    node_address: str,
    binding: dict,
    *,
    runtime_root: str | Path,
) -> dict:
    """Immediately re-project the shared checklist after canonical question state changes."""
    root = Path(runtime_root)
    path = addressing.owed_checklist_path(node_address, root)
    state_path = addressing.turn_state_path(node_address, root)
    with store.file_lock(addressing.turn_state_lock_path(node_address, root), shared=False):
        projected = build_checklist(
            node_address,
            binding,
            runtime_root=root,
            profile=binding.get("turn_hook_profile"),
        )
        prior = _read_checklist(path)
        checklist = (
            prior
            if prior is not None and prior.get("version") == projected.get("version")
            else projected
        )
        if checklist is projected:
            _write_json_atomic(path, checklist)
        try:
            current = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            current = None
        if isinstance(current, dict) and current.get("owner_token") == binding.get("owner_token"):
            changed = (
                current.get("checklist_version") != checklist["version"]
                or list(current.get("open_item_ids") or [])
                != list(checklist["open_item_ids"])
            )
            if changed:
                current["checklist_version"] = checklist["version"]
                current["open_item_ids"] = checklist["open_item_ids"]
                # ``updated_at`` is the hook-edge clock used by the hung-tool fallback.
                # Checklist refresh has its own projection timestamp and must never advance it.
                _write_json_atomic(state_path, current)
        return checklist


def ledger_wait_reasons(
    node_address: str,
    binding: dict,
    *,
    runtime_root: str | Path,
    signal_observation: detector_signals.TerminalSignalObservation | None = None,
) -> tuple[str, ...]:
    """Return only waits already derivable from today's binding ledger."""
    reasons: list[str] = []
    this_path = node_address.split("#", 1)[0]
    for other_address, other in _bindings(Path(runtime_root)).items():
        if other_address == node_address:
            continue
        is_descendant = (
            other.get("parent_address") == node_address
            or other_address.split("#", 1)[0].startswith(this_path + "/")
        )
        if is_descendant and not states.is_terminal(other.get("state")):
            reasons.append(f"live_descendant:{other_address}")

    gate_state = binding.get("gate_state")
    if gate_state == "candidate_submitted":
        reasons.append("pending_gate_verdict")
    elif gate_state == "gate_escalated":
        reasons.append("held_gate_escalation")

    observation = signal_observation or detector_signals.observe_terminal_signal(
        {"node_address": node_address},
        binding,
    )
    if (
        observation.status == "valid"
        and str((observation.payload or {}).get("signal") or "").upper() == "ESCALATED"
        and not detector_signals.escalation_answered(
            binding,
            (observation.payload or {}).get("_signal_artifact_seen_at"),
        )
    ):
        reasons.append("held_escalation")

    if binding.get("plan_alignment_state") == "semantic_cell_pending":
        reasons.append("pending_plan_alignment_semantic_cell")
    elif binding.get("plan_alignment_state") == "ready":
        reasons.append("pending_plan_alignment_decision")

    records = binding.get("coordination_handoffs") or {}
    if isinstance(records, dict):
        for handoff_id, record in sorted(records.items()):
            if not isinstance(record, dict):
                continue
            if record.get("response_required") and record.get("state") == "submitted":
                reasons.append(f"pending_coordination_response:{handoff_id}")
    for question in messages.open_questions_asked_by(binding):
        reasons.append(f"pending_open_question:{question.get('message_id')}")
    return tuple(dict.fromkeys(reasons))


def render_blocked_message(checklist: dict) -> str:
    open_items = [item for item in checklist.get("items", []) if not item.get("ok")]
    lines = [
        "Turn end blocked by the live owed checklist.",
        "Produce the open items below, OR write an explanation and sign FAILED, OR submit a "
        "`needs_answer` question to your parent and park.",
        "Open owed items:",
    ]
    if not open_items:
        lines.append("- Re-run the terminal sign-off/checklist step; no open item was readable.")
    for item in open_items:
        lines.append(f"- [{item.get('item_id')}] {item.get('label')}")
        for defect in list(item.get("defects") or [])[:3]:
            lines.append(f"  - {defect}")
    return "\n".join(lines)


def evaluate_exit(
    node_address: str,
    binding: dict,
    *,
    runtime_root: str | Path,
    checklist: dict | None = None,
) -> ExitDecision:
    checklist = checklist or build_checklist(
        node_address,
        binding,
        runtime_root=runtime_root,
        profile=binding.get("turn_hook_profile"),
    )
    observation = detector_signals.observe_terminal_signal(
        {"node_address": node_address},
        binding,
        runtime_root=runtime_root,
    )
    signal = (
        str((observation.payload or {}).get("signal") or "").upper()
        if observation.status == "valid"
        else ""
    )
    if signal == "FAILED":
        return ExitDecision(
            decision=PRODUCT_PRESENT,
            reasons=("failed_explanation_signed",),
            checklist=checklist,
        )
    waits = ledger_wait_reasons(
        node_address,
        binding,
        runtime_root=runtime_root,
        signal_observation=observation,
    )
    if waits:
        return ExitDecision(decision=LEDGER_WAIT, reasons=waits, checklist=checklist)
    if signal == "DONE":
        product_items = [
            item
            for item in checklist.get("items", [])
            if item.get("item_id") != "terminal_signoff"
        ]
        if all(bool(item.get("ok")) for item in product_items):
            return ExitDecision(
                decision=PRODUCT_PRESENT,
                reasons=("done_product_present",),
                checklist=checklist,
            )
    return ExitDecision(
        decision=PROD_REQUIRED,
        reasons=("neither_product_nor_ledger_wait",),
        checklist=checklist,
        message=render_blocked_message(checklist),
    )


def _event_row(
    *,
    node_address: str,
    owner_token: str,
    profile: str,
    hook_event: str,
    state: str | None,
    adopted: bool,
    detail: dict | None = None,
    row_kind: str = ADOPTED_HOOK_EVENT,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "row_kind": row_kind,
        "event_id": str(uuid.uuid4()),
        "ts": clock.now_utc(),
        "node_address": node_address,
        "owner_token": owner_token,
        "hook_profile": profile,
        "hook_event": hook_event,
        "state": state,
        "adopted": adopted,
        "detail": dict(detail or {}),
    }


def _minimal_hook_payload(payload: dict) -> dict:
    """Persist only state-machine facts, never prompt/tool bodies or model content."""
    keys = ("hook_event_name", "type", "tool_use_id", "tool_name", "fault_reason")
    return {key: payload[key] for key in keys if payload.get(key) is not None}


def append_raw_hook_event(
    *,
    runtime_root: str | Path,
    node_address: str,
    owner_token: str,
    runtime: str,
    payload: dict,
) -> str:
    """Durably append one ledger-blind callback ingress row to the node-owned event log."""
    root = Path(runtime_root)
    hook_event = str(payload.get("hook_event_name") or payload.get("type") or "")
    row = _event_row(
        node_address=node_address,
        owner_token=owner_token,
        profile=profile_for_runtime(runtime),
        hook_event=hook_event or "malformed_hook_event",
        state=None,
        adopted=False,
        row_kind=RAW_HOOK_EVENT,
    )
    row["runtime"] = runtime
    row["payload"] = _minimal_hook_payload(payload)
    with store.file_lock(
        addressing.turn_state_lock_path(node_address, root),
        shared=False,
    ):
        _append_event(addressing.turn_events_path(node_address, root), row)
    return str(row["event_id"])


def append_hook_response(
    *,
    runtime_root: str | Path,
    node_address: str,
    owner_token: str,
    ingress_event_id: str,
    response: dict | None,
) -> dict:
    """Publish the daemon result for one exact synchronous raw callback."""
    row = _event_row(
        node_address=node_address,
        owner_token=owner_token,
        profile=HOOKLESS_FALLBACK,
        hook_event=HOOK_RESPONSE,
        state=None,
        adopted=True,
        row_kind=HOOK_RESPONSE,
    )
    row["responds_to_event_id"] = str(ingress_event_id)
    row["response"] = response
    _append_event(addressing.turn_events_path(node_address, runtime_root), row)
    return row


def wait_for_hook_response(
    *,
    runtime_root: str | Path,
    node_address: str,
    ingress_event_id: str,
    timeout_s: float,
) -> dict | None:
    """Wait for the response row keyed to ``ingress_event_id``; kind/position never substitute."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    path = addressing.turn_events_path(node_address, runtime_root)
    while True:
        rows, _offset, _errors = read_event_tail(path, 0)
        for row in rows:
            if (
                row.get("row_kind") == HOOK_RESPONSE
                and row.get("responds_to_event_id") == ingress_event_id
            ):
                return row.get("response")
        if time.monotonic() >= deadline:
            raise HookResponseTimeout(
                f"no daemon hook response for exact event_id={ingress_event_id}"
            )
        time.sleep(0.01)


def _trigger_daemon_adoption(
    *,
    runtime_root: str | Path,
    node_address: str,
    ingress_event_id: str,
    timeout_s: float,
) -> dict:
    request = {
        "command": "turn-hook-adopt",
        "addr": node_address,
        "event_id": ingress_event_id,
    }
    path = Path(runtime_root) / ".harnessd" / "harnessd.sock"
    payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(0.05, float(timeout_s)))
        client.connect(str(path))
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            data = client.recv(65536)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    if not raw.strip():
        raise ConnectionError("daemon returned an empty turn-hook adoption response")
    return json.loads(raw.decode("utf-8"))


def capture_hook_event(
    *,
    runtime_root: str | Path,
    node_address: str,
    owner_token: str,
    runtime: str,
    payload: dict,
    trigger=None,
    response_timeout_s: float = 25.0,
) -> dict | None:
    """Append first, trigger daemon adoption, then wait only for an exact synchronous response."""
    started = time.monotonic()
    event_id = append_raw_hook_event(
        runtime_root=runtime_root,
        node_address=node_address,
        owner_token=owner_token,
        runtime=runtime,
        payload=payload,
    )
    hook_event = str(payload.get("hook_event_name") or payload.get("type") or "")
    try:
        if trigger is not None:
            trigger(event_id)
        else:
            remaining = max(0.05, response_timeout_s - (time.monotonic() - started))
            _trigger_daemon_adoption(
                runtime_root=runtime_root,
                node_address=node_address,
                ingress_event_id=event_id,
                timeout_s=min(5.0, remaining),
            )
    except (ConnectionError, FileNotFoundError, OSError, TimeoutError, ValueError):
        # The append is already durable. Synchronous callbacks continue watching for the periodic
        # sweep; non-response edges return and are adopted by that same recovery path.
        pass
    if hook_event not in _SYNC_RESPONSE_EVENTS:
        return None
    remaining = response_timeout_s - (time.monotonic() - started)
    if remaining <= 0:
        raise HookResponseTimeout(
            f"daemon hook response budget expired for exact event_id={event_id}"
        )
    return wait_for_hook_response(
        runtime_root=runtime_root,
        node_address=node_address,
        ingress_event_id=event_id,
        timeout_s=remaining,
    )


def _ack_text(changes: list[dict]) -> str:
    return "Owed checklist acknowledged: " + "; ".join(
        f"{change['item_id']} {change['change']}" for change in changes
    )


def record_hook_fault(
    *,
    runtime_root: str | Path,
    node_address: str,
    owner_token: str,
    runtime: str,
    reason: str,
    binding: dict | None = None,
    ingress_event_id: str | None = None,
    adopted: bool = False,
) -> None:
    """Persist a malformed callback as degraded state until a later valid edge clears it."""
    root = Path(runtime_root)
    profile = profile_for_runtime(runtime)
    state_path = addressing.turn_state_path(node_address, root)
    events_path = addressing.turn_events_path(node_address, root)
    with store.file_lock(
        addressing.turn_state_lock_path(node_address, root),
        shared=False,
    ):
        binding = binding if binding is not None else _binding(root, node_address)
        if binding is None or binding.get("owner_token") != owner_token:
            _append_event(
                events_path,
                _event_row(
                    node_address=node_address,
                    owner_token=owner_token,
                    profile=profile,
                    hook_event="malformed_hook_payload",
                    state=None,
                    adopted=False,
                    detail={
                        "reason": "stale_owner_token_or_missing_binding",
                        "parse_error": reason,
                    },
                ),
            )
            return
        observation = read_current(
            node_address,
            binding,
            runtime_root=root,
        )
        current = dict(observation.payload or {})
        current_state = current.get("state")
        if current_state not in {
            TURN_NOT_STARTED,
            TURN_RUNNING,
            TOOL_IN_FLIGHT,
            WAITING_ON_HUMAN,
            TURN_ENDED,
        }:
            current_state = TURN_NOT_STARTED
        row = _event_row(
            node_address=node_address,
            owner_token=owner_token,
            profile=profile,
            hook_event="malformed_hook_payload",
            state=current_state,
            adopted=adopted,
            detail={"reason": reason, "ingress_event_id": ingress_event_id},
        )
        _append_event(events_path, row)
        current.update(
            {
                "schema_version": SCHEMA_VERSION,
                "node_address": node_address,
                "owner_token": owner_token,
                "hook_profile": profile,
                "state": current_state,
                "in_flight_tools": list(current.get("in_flight_tools") or []),
                "revision": int(current.get("revision") or 0) + 1,
                "updated_at": row["ts"],
                "last_event_id": row["event_id"],
                "last_hook_event": "malformed_hook_payload",
                "hook_fault": reason,
            }
        )
        _write_json_atomic(state_path, current)


def handle_hook_event(
    *,
    runtime_root: str | Path,
    node_address: str,
    owner_token: str,
    runtime: str,
    payload: dict,
    binding: dict | None = None,
    ingress_event_id: str | None = None,
) -> dict | None:
    """Handle one Claude command-hook or Codex legacy-notify callback."""
    root = Path(runtime_root)
    profile = profile_for_runtime(runtime)
    hook_event = str(payload.get("hook_event_name") or payload.get("type") or "")
    state_path = addressing.turn_state_path(node_address, root)
    events_path = addressing.turn_events_path(node_address, root)
    checklist_path = addressing.owed_checklist_path(node_address, root)
    lock_path = addressing.turn_state_lock_path(node_address, root)

    with store.file_lock(lock_path, shared=False):
        binding = binding if binding is not None else _binding(root, node_address)
        current_observation = (
            read_current(node_address, binding, runtime_root=root)
            if binding is not None
            else CurrentObservation(status="missing", reason="binding is absent")
        )
        if binding is None or binding.get("owner_token") != owner_token:
            row = _event_row(
                node_address=node_address,
                owner_token=owner_token,
                profile=profile,
                hook_event=hook_event or "malformed_hook_event",
                state=None,
                adopted=False,
                detail={"reason": "stale_owner_token_or_missing_binding"},
            )
            _append_event(events_path, row)
            return None

        current = dict(current_observation.payload or {})
        in_flight = set(str(v) for v in current.get("in_flight_tools", []) if v)
        state = current.get("state") if current_observation.status == "valid" else TURN_NOT_STARTED
        waiting_tool_id = str(current.get("waiting_on_human_tool_id") or "").strip() or None
        waiting_tool_name = (
            str(current.get("waiting_on_human_tool_name") or "").strip() or None
        )
        detail: dict[str, Any] = {}
        response: dict | None = None
        checklist_before = _read_checklist(checklist_path)
        checklist_after = checklist_before
        exit_decision: ExitDecision | None = None
        exit_delivery: str | None = None

        if runtime == "claude-code":
            if hook_event == "UserPromptSubmit":
                state = TURN_RUNNING
            elif hook_event == "PreToolUse":
                tool_id = str(payload.get("tool_use_id") or "").strip()
                tool_name = str(payload.get("tool_name") or "").strip()
                if tool_id:
                    in_flight.add(tool_id)
                if tool_id and tool_name == "AskUserQuestion":
                    waiting_tool_id = tool_id
                    waiting_tool_name = tool_name
                state = WAITING_ON_HUMAN if waiting_tool_id else TOOL_IN_FLIGHT
                detail["tool_use_id"] = tool_id or None
                detail["tool_name"] = tool_name or None
            elif hook_event in {"PostToolUse", "PostToolUseFailure"}:
                tool_id = str(payload.get("tool_use_id") or "").strip()
                if tool_id:
                    in_flight.discard(tool_id)
                if tool_id and tool_id == waiting_tool_id:
                    waiting_tool_id = None
                    waiting_tool_name = None
                state = (
                    WAITING_ON_HUMAN
                    if waiting_tool_id
                    else (TOOL_IN_FLIGHT if in_flight else TURN_RUNNING)
                )
                detail["tool_use_id"] = tool_id or None
                detail["tool_name"] = str(payload.get("tool_name") or "").strip() or None
                checklist_after = build_checklist(
                    node_address,
                    binding,
                    runtime_root=root,
                    profile=profile,
                )
            elif hook_event == "Stop":
                checklist_after = build_checklist(
                    node_address,
                    binding,
                    runtime_root=root,
                    profile=profile,
                )
                exit_decision = evaluate_exit(
                    node_address,
                    binding,
                    runtime_root=root,
                    checklist=checklist_after,
                )
                if exit_decision.decision == PROD_REQUIRED:
                    state = TURN_RUNNING
                    exit_delivery = "inline_stop_block"
                    response = {"decision": "block", "reason": exit_decision.message}
                else:
                    state = TURN_ENDED
                    in_flight.clear()
                    waiting_tool_id = None
                    waiting_tool_name = None
                    exit_delivery = "allowed"
            else:
                detail["reason"] = "unsupported_claude_hook_event"
        elif runtime == "codex":
            if hook_event == "agent-turn-complete":
                checklist_after = build_checklist(
                    node_address,
                    binding,
                    runtime_root=root,
                    profile=profile,
                )
                exit_decision = evaluate_exit(
                    node_address,
                    binding,
                    runtime_root=root,
                    checklist=checklist_after,
                )
                state = TURN_ENDED
                in_flight.clear()
                waiting_tool_id = None
                waiting_tool_name = None
                exit_delivery = (
                    "daemon_prod"
                    if exit_decision.decision == PROD_REQUIRED
                    else "allowed"
                )
            else:
                detail["reason"] = "unsupported_codex_notify_event"

        changes = (
            checklist_changes(checklist_before, checklist_after)
            if checklist_after is not None and checklist_after is not checklist_before
            else []
        )
        if checklist_after is not None and checklist_after is not checklist_before:
            _write_json_atomic(checklist_path, checklist_after)

        detail.update(
            {
                "ingress_event_id": ingress_event_id,
                "in_flight_tools": sorted(in_flight),
                "waiting_on_human_tool_id": waiting_tool_id,
                "waiting_on_human_tool_name": waiting_tool_name,
                "checklist_changes": changes,
                "checklist_version": (checklist_after or {}).get("version"),
                "open_item_ids": list((checklist_after or {}).get("open_item_ids") or []),
                "exit_decision": exit_decision.decision if exit_decision else None,
                "exit_reasons": list(exit_decision.reasons) if exit_decision else [],
                "exit_delivery": exit_delivery,
                "blocked_message": exit_decision.message if exit_decision else None,
            }
        )
        row = _event_row(
            node_address=node_address,
            owner_token=owner_token,
            profile=profile,
            hook_event=hook_event or "malformed_hook_event",
            state=state,
            adopted=True,
            detail=detail,
        )
        _append_event(events_path, row)
        if changes:
            ack = _event_row(
                node_address=node_address,
                owner_token=owner_token,
                profile=profile,
                hook_event="owed_checklist_ack",
                state=state,
                adopted=True,
                detail={"changes": changes, "checklist_version": checklist_after["version"]},
            )
            _append_event(events_path, ack)
            if runtime == "claude-code" and hook_event in {
                "PostToolUse",
                "PostToolUseFailure",
            }:
                response = {
                    "hookSpecificOutput": {
                        "hookEventName": hook_event,
                        "additionalContext": _ack_text(changes),
                    }
                }

        next_current = {
            "schema_version": SCHEMA_VERSION,
            "node_address": node_address,
            "owner_token": owner_token,
            "hook_profile": profile,
            "state": state,
            "in_flight_tools": sorted(in_flight),
            "waiting_on_human_tool_id": waiting_tool_id,
            "waiting_on_human_tool_name": waiting_tool_name,
            "revision": int(current.get("revision") or 0) + 1,
            "updated_at": row["ts"],
            "last_event_id": row["event_id"],
            "last_hook_event": hook_event,
            "checklist_version": (checklist_after or {}).get("version"),
            "open_item_ids": list((checklist_after or {}).get("open_item_ids") or []),
            "exit_decision": exit_decision.decision if exit_decision else None,
            "exit_reasons": list(exit_decision.reasons) if exit_decision else [],
            "exit_delivery": exit_delivery,
            "blocked_message": exit_decision.message if exit_decision else None,
            "prod_dispatched_event_id": current.get("prod_dispatched_event_id"),
        }
        _write_json_atomic(state_path, next_current)
        return response


def read_event_tail(path: str | Path, offset: int) -> tuple[list[dict], int, list[str]]:
    """Read complete JSONL rows after a durable byte offset.

    RECREATED-LOG GUARD (live Run-5, 2026-07-31): a stored offset BEYOND the file's current end is
    not a caught-up cursor — it indexes an incarnation that no longer exists on disk. Every spawn
    recreates this log (``seed`` durably truncates it to zero bytes), so a binding carrying a prior
    incarnation's cursor made the seek land past EOF: zero rows read, the same offset re-stamped,
    the seat permanently blind (turn_state frozen at ``not_started``, Stop rows unconsumed, the
    wake/owed machinery dead). The spawn path now resets the cursor, but this guard is what heals
    seats ALREADY damaged by an earlier respawn — without needing another one.

    Re-reading from 0 is safe: a recreated log's rows belong to the CURRENT incarnation by
    construction, and any prior-incarnation row that somehow remains carries the prior owner_token,
    which the consumer already fences ("stale runtime-hook event ignored"). The comparison is
    STRICTLY greater-than — ``offset == size`` is the ordinary fully-consumed case and must not
    re-read (that would re-adopt every row on every tick).
    """
    rows: list[dict] = []
    errors: list[str] = []
    p = Path(path)
    try:
        with p.open("rb") as handle:
            start_at = max(0, int(offset or 0))
            if start_at > os.fstat(handle.fileno()).st_size:
                start_at = 0
            handle.seek(start_at)
            while True:
                start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    # A hook write is fsync'd as one line; leave a partial tail for the next tick.
                    handle.seek(start)
                    break
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    errors.append(f"offset {start}: {type(exc).__name__}: {exc}")
                    continue
                if not isinstance(payload, dict):
                    errors.append(f"offset {start}: payload is not an object")
                    continue
                rows.append(payload)
            return rows, handle.tell(), errors
    except FileNotFoundError:
        return [], int(offset or 0), ["event log is absent"]
    except OSError as exc:
        return [], int(offset or 0), [f"{type(exc).__name__}: {exc}"]


def mark_prod_dispatched(
    node_address: str,
    binding: dict,
    *,
    runtime_root: str | Path,
    event_id: str,
) -> None:
    """Durably mark a Codex daemon-prod event handled in the node-local current snapshot."""
    root = Path(runtime_root)
    with store.file_lock(addressing.turn_state_lock_path(node_address, root), shared=False):
        observation = read_current(node_address, binding, runtime_root=root)
        if observation.status != "valid":
            return
        payload = dict(observation.payload or {})
        if payload.get("last_event_id") != event_id:
            return
        payload["prod_dispatched_event_id"] = event_id
        payload["prod_dispatched_at"] = clock.now_utc()
        _write_json_atomic(addressing.turn_state_path(node_address, root), payload)
