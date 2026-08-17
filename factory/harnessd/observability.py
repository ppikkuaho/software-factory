"""Generated-on-read whole-build observability.

The binding ledger and node-local artifacts remain the only truth.  This module joins them without
daemon IPC or runtime mutation, then renders terminal, JSON-ready, and self-contained HTML views.
Generated files are allowed only outside the agent/gate ``nodes/`` tree.
"""

from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import addressing, clock, contracts, ledger, messages, states, store, turn_state


SCHEMA_VERSION = 1
VIEWS_DIR = Path(".harnessd") / "views"
JOURNEY_FILENAME = "journey.html"


def _read_object(path: Path, *, required: bool = False) -> tuple[dict, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise
        return {}, f"{path}: absent"
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        if required:
            raise ValueError(f"{path} is unreadable or malformed: {exc}") from exc
        return {}, f"{path}: {type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        message = f"{path} did not contain a JSON object (got {type(payload).__name__})"
        if required:
            raise ValueError(message)
        return {}, message
    return payload, None


def _bindings(root: Path) -> dict[str, dict]:
    path = root / ledger.BINDING_FILENAME
    bindings = ledger.all_nodes(binding_path=path)
    if not isinstance(bindings, dict):
        raise ValueError(f"binding ledger at {path} did not contain a JSON object")
    for address, binding in bindings.items():
        if not isinstance(binding, dict):
            raise ValueError(
                f"binding ledger row {address!r} is {type(binding).__name__}, not a JSON object"
            )
    return bindings


def _depth(address: str, bindings: dict[str, dict]) -> tuple[int, bool]:
    depth = 0
    current = address
    seen: set[str] = set()
    while True:
        if current in seen:
            return depth, True
        seen.add(current)
        binding = bindings.get(current) or {}
        parent = binding.get("parent_address")
        if not parent:
            return depth, False
        if parent not in bindings:
            return depth, True
        depth += 1
        current = str(parent)


def _open_question_records(bindings: dict[str, dict], generated_at: str) -> list[dict]:
    rows: list[dict] = []
    for binding in bindings.values():
        records = binding.get("messages") or {}
        if not isinstance(records, dict):
            continue
        for record in records.values():
            if not isinstance(record, dict):
                continue
            if not record.get("needs_answer") or record.get("question_state") != messages.QUESTION_OPEN:
                continue
            row = {
                "message_id": record.get("message_id"),
                "source": record.get("source"),
                "target": record.get("target"),
                "edge": f"{record.get('source')} -> {record.get('target')}",
                "submitted_at": record.get("submitted_at"),
                "artifact": record.get("artifact"),
                "summary": record.get("summary"),
                "tags": list(record.get("tags") or []),
            }
            submitted_at = row["submitted_at"]
            try:
                age = max(0.0, float(clock.age_seconds(str(submitted_at), now=generated_at)))
            except (TypeError, ValueError):
                age = None
            row["age_seconds"] = age
            row["age_bucket"] = _age_bucket(age)
            row["age"] = _human_age(age)
            rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("source") or ""),
            str(row.get("target") or ""),
            str(row.get("message_id") or ""),
        ),
    )


def _age_bucket(age: float | None) -> str:
    if age is None:
        return "unknown"
    if age < 60:
        return "<1m"
    if age < 3600:
        return "1m-1h"
    if age < 86400:
        return "1h-24h"
    if age < 604800:
        return "1d-7d"
    return ">=7d"


def _human_age(age: float | None) -> str:
    if age is None:
        return "unknown"
    seconds = int(age)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def _turn_row(address: str, binding: dict, root: Path) -> tuple[dict, str | None]:
    observation = turn_state.read_current(address, binding, runtime_root=root)
    payload = observation.payload or {}
    raw_tools = list(payload.get("in_flight_tools") or [])
    waiting_tool_id = payload.get("waiting_on_human_tool_id")
    waiting_tool_name = payload.get("waiting_on_human_tool_name")
    tool_names: list[str] = []
    for tool in raw_tools:
        if isinstance(tool, dict):
            value = tool.get("tool_name") or tool.get("name") or tool.get("tool_use_id")
        else:
            value = (
                waiting_tool_name
                if waiting_tool_name and str(tool) == str(waiting_tool_id)
                else tool
            )
        if value:
            tool_names.append(str(value))
    row = {
        "status": observation.status,
        "reason": observation.reason,
        "state": payload.get("state"),
        "hook_profile": (
            payload.get("hook_profile")
            or binding.get("turn_hook_profile")
            or turn_state.HOOKLESS_FALLBACK
        ),
        "hook_health": binding.get("turn_hook_health") or "unknown",
        "hook_error": binding.get("turn_hook_error"),
        "last_hook_event": payload.get("last_hook_event"),
        "updated_at": payload.get("updated_at"),
        "in_flight_tools": raw_tools,
        "waiting_on_human_tool_id": waiting_tool_id,
        "waiting_on_human_tool_name": waiting_tool_name,
        "tool_names": sorted(dict.fromkeys(tool_names)),
        "tool_count": len(raw_tools),
    }
    warning = None
    if observation.status in {"malformed", "stale"}:
        warning = f"{address}: turn-state {observation.status}: {observation.reason or 'unknown'}"
    return row, warning


def _owed_row(
    address: str,
    binding: dict,
    root: Path,
    bindings: dict[str, dict],
) -> tuple[dict, str | None]:
    try:
        checklist = turn_state.build_checklist(
            address,
            binding,
            runtime_root=root,
            profile=binding.get("turn_hook_profile"),
            bindings=bindings,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return (
            {
                "status": "unavailable",
                "version": None,
                "total": 0,
                "present": 0,
                "open": 0,
                "open_item_ids": [],
                "items": [],
            },
            f"{address}: owed checklist unavailable: {type(exc).__name__}: {exc}",
        )
    items = list(checklist.get("items") or [])
    present = sum(bool(item.get("ok")) for item in items if isinstance(item, dict))
    return (
        {
            "status": "available",
            "version": checklist.get("version"),
            "total": len(items),
            "present": present,
            "open": len(items) - present,
            "open_item_ids": list(checklist.get("open_item_ids") or []),
            "items": items,
        },
        None,
    )


def _cohort_kind(address: str, binding: dict) -> str | None:
    if binding.get("review_check_for"):
        return "review_check"
    _path, seat = addressing.split_address(address)
    role_variant = str(binding.get("role_variant") or "")
    if seat in {"review", "review-check"} or role_variant.endswith(("#review", "#review-check")):
        return None
    return "product"


def _latest_barrier(
    address: str,
    cohort: str,
    root: Path,
) -> tuple[dict | None, list[str]]:
    path = addressing.inbox_path(address, root)
    warnings: list[str] = []
    latest = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None, warnings
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"{address}: barrier inbox unreadable: {type(exc).__name__}: {exc}"]
    for index, raw in enumerate(lines, start=1):
        try:
            row = json.loads(raw)
        except ValueError as exc:
            warnings.append(f"{address}: malformed inbox row {index}: {exc}")
            continue
        if (
            isinstance(row, dict)
            and row.get("type") == "barrier_complete"
            and row.get("cohort") == cohort
        ):
            latest = row
    return latest, warnings


def _barrier_row(
    owner: str,
    cohort: str,
    bindings: dict[str, dict],
    root: Path,
) -> tuple[dict, list[str]]:
    members: list[tuple[str, dict]] = []
    for address, binding in bindings.items():
        kind = _cohort_kind(address, binding)
        if cohort == "product":
            belongs = kind == "product" and binding.get("parent_address") == owner
        else:
            belongs = kind == "review_check" and binding.get("review_check_for") == owner
        if belongs:
            members.append((address, binding))
    members.sort(key=lambda item: item[0])
    terminal = sum(states.is_terminal(binding.get("state")) for _, binding in members)
    latest, warnings = _latest_barrier(owner, cohort, root)
    status = "none" if not members else ("complete" if terminal == len(members) else "open")
    return (
        {
            "cohort": cohort,
            "status": status,
            "terminal": terminal,
            "total": len(members),
            "members": [
                {
                    "node_address": address,
                    "state": binding.get("state"),
                    "generation": binding.get("generation"),
                    "lease_epoch": binding.get("lease_epoch"),
                }
                for address, binding in members
            ],
            "latest_event": latest,
        },
        warnings,
    )


def _posture(binding: dict) -> dict:
    containment = binding.get("containment_posture") or {}
    if not isinstance(containment, dict):
        containment = {}
    mode = containment.get("mode")
    permission = binding.get("permission_posture")
    if not mode:
        permission_text = str(permission or "")
        mode = "jailed" if permission_text.startswith("jailed") else "unjailed"
    return {
        "permission_posture": permission,
        "version": containment.get("version"),
        "jail_mode": mode,
        "degraded": bool(containment.get("degraded"))
        or str(permission or "").startswith("degraded-"),
        "degraded_reason": containment.get("degraded_reason"),
        "l1_god_view": bool(containment.get("l1_god_view", False)),
    }


def _gate(binding: dict) -> dict:
    bounce_count = int(binding.get("gate_bounce_count") or 0)
    needs_audit = bounce_count > 0
    return {
        "required": bool(binding.get("gate_required")),
        "state": binding.get("gate_state"),
        "gate_id": binding.get("gate_id"),
        "review_address": binding.get("gate_review_address"),
        "gate_for": binding.get("gate_for"),
        "review_check_for": binding.get("review_check_for"),
        "review_check_candidate": binding.get("review_check_candidate"),
        "candidate_manifest_sha256": binding.get("gate_candidate_manifest_sha256"),
        "bounce_count": bounce_count,
        "needs_audit": needs_audit,
        "audit_signals": [f"gate_bounce:count={bounce_count}"] if needs_audit else [],
        "audit_label": (
            "LOOK HERE — something probably went wrong; inspect every gate bounce"
            if needs_audit
            else None
        ),
        "last_bounce_at": binding.get("gate_bounced_at"),
        "last_bounce_review": binding.get("gate_last_bounce_review"),
        "last_bounce_notes": binding.get("gate_last_bounce_notes"),
        "failure_count": int(binding.get("gate_failure_count") or 0),
        "terminal_signal": binding.get("terminal_signal"),
    }


def _binding_summary(binding: dict) -> dict:
    keys = (
        "state",
        "liveness_state",
        "admission_state",
        "admission_blocked_by",
        "admission_block_reason",
        "waiting_on_sibling",
        "schedule_policy",
        "schedule_group",
        "schedule_index",
        "generation",
        "lease_epoch",
        "failure_class",
        "terminal_signal",
    )
    return {key: binding.get(key) for key in keys}


def _blocked_input_row(binding: dict, generated_at: str) -> dict:
    since = binding.get("seat_stall_since")
    duration = None
    if binding.get("seat_stall_active") and since:
        try:
            duration = max(0.0, float(clock.age_seconds(str(since), now=generated_at)))
        except (TypeError, ValueError):
            duration = None
    return {
        "active": bool(binding.get("seat_stall_active")),
        "incident_id": binding.get("seat_stall_incident_id"),
        "since": since,
        "duration_seconds": duration,
        "duration": _human_age(duration) if duration is not None else None,
        "classification": binding.get("seat_stall_classification"),
        "incident_count": int(binding.get("seat_stall_positive_incident_count") or 0),
        "pane_excerpt": binding.get("seat_stall_pane_excerpt"),
        "prompt_signature": binding.get("seat_stall_prompt_signature"),
        "cancel_status": binding.get("seat_stall_cancel_status"),
        "retriggered": bool(binding.get("seat_stall_retriggered")),
        "escalated": bool(binding.get("seat_stall_escalated")),
        "root_limit": bool(binding.get("seat_stall_root_limit")),
    }


def _plan_alignment_row(binding: dict) -> dict:
    questions = [
        row
        for _question_id, row in sorted(
            (binding.get("plan_alignment_owner_questions") or {}).items()
        )
        if isinstance(row, dict)
    ]
    return {
        "state": binding.get("plan_alignment_state"),
        "q3_bundle_sha256": binding.get("plan_alignment_bundle_sha256"),
        "semantic_bundle_sha256": binding.get(
            "plan_alignment_semantic_bundle_sha256"
        ),
        "semantic_evidence": binding.get("plan_alignment_semantic_evidence"),
        "semantic_evidence_sha256": binding.get(
            "plan_alignment_semantic_evidence_sha256"
        ),
        "semantic_failure": binding.get("plan_alignment_semantic_failure"),
        "required_elevations": binding.get(
            "plan_alignment_required_elevations"
        )
        or [],
        "owner_questions": questions,
        "open_owner_questions": [
            row for row in questions if row.get("status") == "open"
        ],
    }


def _fidelity_playback_row(binding: dict) -> dict:
    questions = [
        row
        for _question_id, row in sorted(
            (binding.get("fidelity_playback_owner_questions") or {}).items()
        )
        if isinstance(row, dict)
    ]
    return {
        "configured_authority": (
            binding.get("fidelity_playback_authority") or "owner"
        ),
        "configured_delegate": binding.get("fidelity_playback_delegate"),
        "delegation_reason": binding.get(
            "fidelity_playback_delegation_reason"
        ),
        "current_question_id": binding.get(
            "fidelity_playback_current_question_id"
        ),
        "questions": questions,
        "open_questions": [
            row for row in questions if row.get("status") == "open"
        ],
        "last_answer_authority": binding.get(
            "fidelity_playback_last_answer_authority"
        ),
        "last_answer_actor": binding.get(
            "fidelity_playback_last_answer_actor"
        ),
    }


def _contract_row(
    address: str,
    binding: dict,
    stale_by_holder: dict[str, list[dict]],
) -> dict:
    versions = binding.get("contract_versions") or {}
    receipts = binding.get("contract_receipts") or {}
    return {
        "owned_versions": [
            value
            for _key, value in sorted((versions.items() if isinstance(versions, dict) else ()))
            if isinstance(value, dict)
        ],
        "held_receipts": [
            value
            for _key, value in sorted((receipts.items() if isinstance(receipts, dict) else ()))
            if isinstance(value, dict)
        ],
        "stale_receipts": list(stale_by_holder.get(address) or []),
    }


def _edges(bindings: dict[str, dict]) -> list[dict]:
    edges: set[tuple[str, str, str, str]] = set()
    for address, binding in bindings.items():
        parent = binding.get("parent_address")
        if parent:
            edges.add(("supervision", str(parent), address, "parent"))
        for predecessor in {
            binding.get("admission_blocked_by"),
            binding.get("waiting_on_sibling"),
        }:
            if predecessor:
                edges.add(("dependency", str(predecessor), address, "admission"))
        review = binding.get("gate_review_address")
        if review:
            edges.add(("review", address, str(review), "gate"))
        gate_for = binding.get("gate_for")
        if gate_for:
            edges.add(("review", str(gate_for), address, "gate"))
    return [
        {"type": kind, "source": source, "target": target, "reason": reason}
        for kind, source, target, reason in sorted(edges)
    ]


def snapshot(runtime_root: str | Path) -> dict:
    """Join one runtime's binding checkpoint and current artifacts without writing anything."""
    root = Path(runtime_root).resolve()
    generated_at = clock.now_utc()
    bindings = _bindings(root)
    runtime, runtime_warning = _read_object(root / "runtime.json")
    warnings: list[str] = [runtime_warning] if runtime_warning else []
    open_questions = _open_question_records(bindings, generated_at)
    stale = contracts.stale_receipt_holders(bindings, include_terminal=True)
    stale_by_holder: dict[str, list[dict]] = defaultdict(list)
    for row in stale:
        stale_by_holder[str(row.get("holder_address"))].append(row)

    rows: list[dict] = []
    for address in sorted(bindings):
        binding = bindings[address]
        depth, orphan = _depth(address, bindings)
        dependencies = sorted(
            {
                str(value)
                for value in (
                    binding.get("admission_blocked_by"),
                    binding.get("waiting_on_sibling"),
                )
                if value
            }
        )
        turn, turn_warning = _turn_row(address, binding, root)
        owed, owed_warning = _owed_row(address, binding, root, bindings)
        if turn_warning:
            warnings.append(turn_warning)
        if owed_warning:
            warnings.append(owed_warning)
        product, product_warnings = _barrier_row(address, "product", bindings, root)
        review_check, review_warnings = _barrier_row(address, "review_check", bindings, root)
        warnings.extend(product_warnings)
        warnings.extend(review_warnings)
        incoming_questions = [
            row for row in open_questions if row.get("target") == address
        ]
        outgoing_questions = [
            row for row in open_questions if row.get("source") == address
        ]
        plan_alignment = _plan_alignment_row(binding)
        fidelity_playback = _fidelity_playback_row(binding)
        owner_questions = [
            {
                **question,
                "owner_address": address,
                "question_kind": "plan_alignment",
            }
            for question in plan_alignment["owner_questions"]
        ] + [
            {
                **question,
                "owner_address": address,
                "question_kind": "fidelity_playback",
            }
            for question in fidelity_playback["questions"]
        ] + [
            {
                **question,
                "owner_address": address,
                "question_kind": "owner_facing_message",
            }
            for question in incoming_questions
            if "owner-facing" in set(question.get("tags") or [])
        ]
        open_owner_questions = [
            question
            for question in owner_questions
            if (
                question.get("status") == "open"
                or question.get("question_state") == messages.QUESTION_OPEN
                or question.get("question_kind") == "owner_facing_message"
            )
        ]
        rows.append(
            {
                "node_address": address,
                "node_path": addressing.node_path(address),
                "seat": addressing.split_address(address)[1],
                "level": binding.get("level"),
                "role_variant": binding.get("role_variant"),
                "child_purpose": binding.get("child_purpose"),
                "parent_address": binding.get("parent_address"),
                "current": not states.is_terminal(binding.get("state")),
                "dag": {
                    "depth": depth,
                    "orphan_or_cycle": orphan,
                    "dependencies": dependencies,
                },
                "binding": _binding_summary(binding),
                "blocked_on_input": _blocked_input_row(binding, generated_at),
                "turn": turn,
                "owed": owed,
                "questions": {
                    "incoming": incoming_questions,
                    "outgoing": outgoing_questions,
                },
                "owner_questions": {
                    "questions": owner_questions,
                    "open_questions": open_owner_questions,
                },
                "contracts": _contract_row(address, binding, stale_by_holder),
                "plan_alignment": plan_alignment,
                "fidelity_playback": fidelity_playback,
                "gate": _gate(binding),
                "barriers": {
                    "product": product,
                    "review_check": review_check,
                },
                "posture": _posture(binding),
            }
        )

    age_counts = Counter(str(row["age_bucket"]) for row in open_questions)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "runtime_root": str(root),
        "runtime": runtime,
        "nodes": rows,
        "edges": _edges(bindings),
        "current_positions": [
            row["node_address"] for row in rows if row.get("current")
        ],
        "open_questions": open_questions,
        "owner_questions": [
            question
            for row in rows
            for question in row["owner_questions"]["questions"]
        ],
        "question_age_buckets": dict(sorted(age_counts.items())),
        "summaries": {
            "binding_states": dict(
                sorted(Counter(str(row["binding"].get("state")) for row in rows).items())
            ),
            "turn_states": dict(
                sorted(Counter(str(row["turn"].get("state")) for row in rows).items())
            ),
            "gate_states": dict(
                sorted(
                    Counter(
                        str(row["gate"].get("state"))
                        for row in rows
                        if row["gate"].get("state") is not None
                    ).items()
                )
            ),
            "hook_health": dict(
                sorted(Counter(str(row["turn"].get("hook_health")) for row in rows).items())
            ),
        },
        "warnings": sorted(dict.fromkeys(warnings)),
    }


def _tree_order(snapshot_value: dict) -> list[tuple[dict, int]]:
    rows = {row["node_address"]: row for row in snapshot_value.get("nodes") or []}
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for address, row in rows.items():
        parent = row.get("parent_address")
        if parent and parent in rows:
            children[str(parent)].append(address)
        else:
            roots.append(address)
    output: list[tuple[dict, int]] = []
    visited: set[str] = set()

    def visit(address: str, depth: int) -> None:
        if address in visited:
            return
        visited.add(address)
        output.append((rows[address], depth))
        for child in sorted(children.get(address, [])):
            visit(child, depth + 1)

    for root in sorted(roots):
        visit(root, 0)
    for address in sorted(set(rows) - visited):
        visit(address, 0)
    return output


def render_terminal(snapshot_value: dict) -> str:
    """Render a compact stable supervision tree from a previously captured snapshot."""
    if not snapshot_value.get("nodes"):
        return "(no nodes)"
    lines = [
        f"build={snapshot_value.get('runtime', {}).get('build_id') or '?'} "
        f"generated={snapshot_value.get('generated_at')} "
        f"current={len(snapshot_value.get('current_positions') or [])}"
    ]
    for row, depth in _tree_order(snapshot_value):
        binding = row["binding"]
        turn = row["turn"]
        owed = row["owed"]
        questions = row["questions"]
        owner_questions = row["owner_questions"]
        plan_alignment = row["plan_alignment"]
        fidelity_playback = row["fidelity_playback"]
        gate = row["gate"]
        blocked = row["blocked_on_input"]
        product = row["barriers"]["product"]
        review = row["barriers"]["review_check"]
        contracts_row = row["contracts"]
        marker = "▶" if row.get("current") else "✓"
        lines.append(
            f"{'  ' * depth}{marker} {row['node_address']} [{row.get('level') or '?'}] "
            f"{binding.get('state')}/{binding.get('liveness_state')} "
            f"turn={turn.get('state') or turn.get('status')} "
            f"owed={owed.get('present')}/{owed.get('total')} "
            f"questions={len(questions.get('incoming') or [])}/{len(questions.get('outgoing') or [])} "
            f"ownerq={len(owner_questions.get('open_questions') or [])}/"
            f"{len(owner_questions.get('questions') or [])} "
            f"fidelityq={len(fidelity_playback.get('open_questions') or [])}/"
            f"{len(fidelity_playback.get('questions') or [])}:"
            f"{fidelity_playback.get('last_answer_authority') or fidelity_playback.get('configured_authority')} "
            f"plan={plan_alignment.get('state') or '-'} "
            f"gate={gate.get('state') or '-'}:bounces={gate.get('bounce_count')} "
            f"barrier=p:{product.get('terminal')}/{product.get('total')},"
            f"r:{review.get('terminal')}/{review.get('total')} "
            f"contracts={len(contracts_row.get('held_receipts') or [])}:"
            f"stale={len(contracts_row.get('stale_receipts') or [])} "
            f"jail={row['posture'].get('jail_mode') or '-'}"
        )
        if gate.get("needs_audit"):
            lines.append(
                f"{'  ' * (depth + 1)}⚠ {gate.get('audit_label')}: "
                f"{', '.join(gate.get('audit_signals') or [])}"
            )
        if blocked.get("active"):
            lines.append(
                f"{'  ' * (depth + 1)}⛔ BLOCKED ON INPUT "
                f"{blocked.get('duration') or 'unknown'} "
                f"class={blocked.get('classification') or '-'} "
                f"cancel={blocked.get('cancel_status') or '-'} "
                f"count={blocked.get('incident_count')}"
            )
        for dependency in row["dag"].get("dependencies") or []:
            lines.append(f"{'  ' * (depth + 1)}↝ dependency {dependency} -> {row['node_address']}")
        if row["dag"].get("orphan_or_cycle"):
            lines.append(f"{'  ' * (depth + 1)}⚠ orphan parent or supervision cycle")
        for warning in (
            turn.get("reason"),
            row["posture"].get("degraded_reason"),
        ):
            if warning:
                lines.append(f"{'  ' * (depth + 1)}⚠ {warning}")
    return "\n".join(lines)


def _state_class(state: Any) -> str:
    value = str(state or "unknown").lower()
    return value if value in {"planned", "claimed", "spawning", "running", "blocked", "done", "failed", "dead"} else "unknown"


def render_html(snapshot_value: dict) -> str:
    """Render a dependency-free self-contained HTML/SVG journey from one snapshot."""
    ordered = _tree_order(snapshot_value)
    by_address = {row["node_address"]: row for row, _depth_value in ordered}
    card_w, card_h, x_gap, y_gap = 260, 108, 330, 142
    positions: dict[str, tuple[int, int]] = {}
    max_depth = 0
    for index, (row, depth) in enumerate(ordered):
        positions[row["node_address"]] = (40 + depth * x_gap, 50 + index * y_gap)
        max_depth = max(max_depth, depth)
    width = max(1000, 100 + max_depth * x_gap + card_w)
    height = max(500, 120 + len(ordered) * y_gap)

    edge_parts: list[str] = []
    marker_by_type = {
        "supervision": "arrow-supervision",
        "dependency": "arrow-dependency",
        "review": "arrow-review",
    }
    for edge in snapshot_value.get("edges") or []:
        source = positions.get(str(edge.get("source")))
        target = positions.get(str(edge.get("target")))
        if not source or not target:
            continue
        sx, sy = source
        tx, ty = target
        x1, y1 = sx + card_w, sy + card_h / 2
        x2, y2 = tx, ty + card_h / 2
        bend = max(35, abs(x2 - x1) * 0.45)
        kind = str(edge.get("type") or "supervision")
        path = (
            f"M {x1:.1f} {y1:.1f} C {x1 + bend:.1f} {y1:.1f}, "
            f"{x2 - bend:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}"
        )
        edge_parts.append(
            f'<path class="edge {html.escape(kind)}" d="{path}" '
            f'marker-end="url(#{marker_by_type.get(kind, "arrow-supervision")})">'
            f"<title>{html.escape(str(edge.get('source')))} → "
            f"{html.escape(str(edge.get('target')))} ({html.escape(kind)})</title></path>"
        )

    node_parts: list[str] = []
    for row, _depth_value in ordered:
        address = row["node_address"]
        x, y = positions[address]
        binding_state = row["binding"].get("state")
        turn_value = row["turn"].get("state") or row["turn"].get("status")
        gate_value = row["gate"].get("state") or "—"
        gate_audit = " · ⚠ LOOK HERE" if row["gate"].get("needs_audit") else ""
        blocked = row.get("blocked_on_input") or {}
        blocked_label = (
            f" · ⛔ {blocked.get('duration') or 'blocked'}"
            if blocked.get("active")
            else ""
        )
        owed = row["owed"]
        state_class = _state_class(binding_state)
        current = "true" if row.get("current") else "false"
        node_parts.append(
            f'<g class="node state-{state_class}" tabindex="0" role="button" '
            f'data-address="{html.escape(address, quote=True)}" data-state="{state_class}" '
            f'data-current="{current}" transform="translate({x} {y})">'
            f'<rect class="node-card" width="{card_w}" height="{card_h}" rx="14"/>'
            f'<rect class="state-stripe" width="7" height="{card_h}" rx="4"/>'
            f'<text class="node-address" x="18" y="26">{html.escape(address)}</text>'
            f'<text class="node-meta" x="18" y="49">'
            f"{html.escape(str(row.get('level') or '?'))} · "
            f"{html.escape(str(row.get('seat') or '?'))} · "
            f"{html.escape(str(binding_state or '?'))}</text>"
            f'<text class="node-detail" x="18" y="72">turn {html.escape(str(turn_value))} · '
            f"owed {owed.get('present')}/{owed.get('total')}</text>"
            f'<text class="node-detail" x="18" y="94">gate {html.escape(str(gate_value))}'
            f"{html.escape(gate_audit + blocked_label)}</text>"
            f"<title>{html.escape(address)} — {html.escape(str(binding_state))}</title>"
            "</g>"
        )

    embedded = json.dumps(
        snapshot_value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    build_id = snapshot_value.get("runtime", {}).get("build_id") or "unknown build"
    summary = snapshot_value.get("summaries") or {}
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>L1-L5 journey — {html.escape(str(build_id))}</title>
<style>
:root {{ color-scheme: dark; --bg:#081018; --panel:#101b27; --line:#60758a; --text:#e8f0f7; --muted:#91a3b5; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }}
header {{ position:sticky; top:0; z-index:5; display:flex; gap:18px; align-items:center; padding:14px 18px; background:#0b1622ee; border-bottom:1px solid #263748; backdrop-filter:blur(10px); }}
h1 {{ margin:0; font:650 18px/1.2 ui-sans-serif,system-ui,sans-serif; }}
.meta {{ color:var(--muted); flex:1; }}
.controls {{ display:flex; gap:8px; align-items:center; }}
button,input,select {{ background:#142333; color:var(--text); border:1px solid #32475b; border-radius:7px; padding:7px 9px; font:inherit; }}
main {{ display:grid; grid-template-columns:minmax(0,1fr) 390px; min-height:calc(100vh - 66px); }}
#graph-wrap {{ overflow:auto; position:relative; }}
svg {{ display:block; min-width:100%; background-image:radial-gradient(#1d3042 1px,transparent 1px); background-size:22px 22px; }}
.edge {{ fill:none; stroke-width:2; opacity:.78; }}
.edge.supervision {{ stroke:#61778c; }}
.edge.dependency {{ stroke:#f3aa3c; stroke-dasharray:9 7; }}
.edge.review {{ stroke:#a887ff; stroke-dasharray:3 6; }}
.node {{ cursor:pointer; outline:none; }}
.node-card {{ fill:#111f2c; stroke:#385067; stroke-width:1.5; }}
.node[data-current="true"] .node-card {{ stroke:#55d6be; stroke-width:3; filter:drop-shadow(0 0 7px #55d6be66); }}
.node:hover .node-card,.node:focus .node-card,.node.selected .node-card {{ stroke:#fff; stroke-width:3; }}
.state-stripe {{ fill:#8da0b2; }}
.state-running .state-stripe,.state-spawning .state-stripe {{ fill:#38c6a2; }}
.state-blocked .state-stripe {{ fill:#f3aa3c; }}
.state-done .state-stripe {{ fill:#4fa8ff; }}
.state-failed .state-stripe,.state-dead .state-stripe {{ fill:#ff647c; }}
.state-planned .state-stripe,.state-claimed .state-stripe {{ fill:#a887ff; }}
.node-address {{ fill:var(--text); font-size:13px; font-weight:700; }}
.node-meta,.node-detail {{ fill:var(--muted); font-size:11px; }}
aside {{ border-left:1px solid #263748; padding:18px; background:var(--panel); overflow:auto; max-height:calc(100vh - 66px); }}
aside h2 {{ margin:0 0 12px; font:650 16px ui-sans-serif,system-ui,sans-serif; }}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; color:#c9d8e6; }}
.legend {{ display:flex; gap:13px; color:var(--muted); }}
.swatch {{ display:inline-block; width:24px; border-top:2px solid #61778c; vertical-align:middle; margin-right:5px; }}
.swatch.dep {{ border-color:#f3aa3c; border-top-style:dashed; }}
.swatch.review {{ border-color:#a887ff; border-top-style:dotted; }}
.hidden {{ display:none; }}
@media (max-width:900px) {{ main {{ grid-template-columns:1fr; }} aside {{ border-left:0; border-top:1px solid #263748; max-height:none; }} }}
</style>
</head>
<body>
<header>
  <div><h1>{html.escape(str(build_id))}</h1><div class="meta">generated {html.escape(str(snapshot_value.get("generated_at")))}</div></div>
  <div class="legend"><span><i class="swatch"></i>supervision</span><span><i class="swatch dep"></i>dependency</span><span><i class="swatch review"></i>review</span></div>
  <div class="controls">
    <input id="search" aria-label="Filter address" placeholder="filter address">
    <select id="state-filter" aria-label="Filter state"><option value="">all states</option>{''.join(f'<option value="{html.escape(state)}">{html.escape(state)}</option>' for state in sorted((summary.get("binding_states") or {}).keys()))}</select>
    <button id="zoom-out" type="button">−</button><button id="fit" type="button">fit</button><button id="zoom-in" type="button">+</button>
  </div>
</header>
<main>
<section id="graph-wrap" aria-label="Whole-build journey graph">
<svg id="journey" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="L1-L5 supervision and dependency DAG">
<defs>
  <marker id="arrow-supervision" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#61778c"/></marker>
  <marker id="arrow-dependency" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#f3aa3c"/></marker>
  <marker id="arrow-review" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#a887ff"/></marker>
</defs>
{''.join(edge_parts)}
{''.join(node_parts)}
</svg>
</section>
<aside><h2 id="detail-title">Whole-build snapshot</h2><pre id="detail">{html.escape(json.dumps({"summaries": summary, "current_positions": snapshot_value.get("current_positions"), "warnings": snapshot_value.get("warnings")}, indent=2, sort_keys=True))}</pre></aside>
</main>
<script id="embedded-snapshot" type="application/json">{embedded}</script>
<script>
(() => {{
  const data = JSON.parse(document.getElementById("embedded-snapshot").textContent);
  const byAddress = new Map(data.nodes.map(row => [row.node_address, row]));
  const svg = document.getElementById("journey");
  const nodes = [...svg.querySelectorAll(".node")];
  const detail = document.getElementById("detail");
  const title = document.getElementById("detail-title");
  const selectNode = node => {{
    nodes.forEach(item => item.classList.toggle("selected", item === node));
    const address = node.dataset.address;
    title.textContent = address;
    detail.textContent = JSON.stringify(byAddress.get(address), null, 2);
  }};
  nodes.forEach(node => {{
    node.addEventListener("click", () => selectNode(node));
    node.addEventListener("keydown", event => {{ if (event.key === "Enter" || event.key === " ") selectNode(node); }});
  }});
  const applyFilter = () => {{
    const query = document.getElementById("search").value.toLowerCase();
    const state = document.getElementById("state-filter").value;
    nodes.forEach(node => node.classList.toggle("hidden", !node.dataset.address.toLowerCase().includes(query) || (state && node.dataset.state !== state)));
  }};
  document.getElementById("search").addEventListener("input", applyFilter);
  document.getElementById("state-filter").addEventListener("change", applyFilter);
  const base = {{w:{width}, h:{height}}}; let scale = 1;
  const resize = () => svg.setAttribute("viewBox", `0 0 ${{base.w / scale}} ${{base.h / scale}}`);
  document.getElementById("zoom-in").addEventListener("click", () => {{ scale = Math.min(3, scale * 1.2); resize(); }});
  document.getElementById("zoom-out").addEventListener("click", () => {{ scale = Math.max(.35, scale / 1.2); resize(); }});
  document.getElementById("fit").addEventListener("click", () => {{ scale = 1; resize(); }});
}})();
</script>
</body>
</html>
"""


def default_output_path(runtime_root: str | Path) -> Path:
    return Path(runtime_root).resolve() / VIEWS_DIR / JOURNEY_FILENAME


def _assert_non_node_output(output_path: Path, runtime_root: Path) -> None:
    nodes_root = (runtime_root / addressing.NODES_DIRNAME).resolve()
    resolved = output_path.resolve()
    try:
        resolved.relative_to(nodes_root)
    except ValueError:
        return
    raise ValueError(
        f"observability output must stay outside the runtime node tree {nodes_root}: {resolved}"
    )


def write_html(
    snapshot_value: dict,
    output_path: str | Path,
    *,
    runtime_root: str | Path,
) -> Path:
    """Atomically write an explicit capture, refusing every agent/gate node-tree target."""
    root = Path(runtime_root).resolve()
    path = Path(output_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    _assert_non_node_output(path, root)
    rendered = render_html(snapshot_value)
    store.atomic_replace(path, lambda handle: handle.write(rendered))
    return path
