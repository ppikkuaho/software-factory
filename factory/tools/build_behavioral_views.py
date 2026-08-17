#!/usr/bin/env python3
"""Build read-only behavioral views from a harness evidence index.

The behavioral evidence index is the raw substrate. This tool projects that
substrate into stable UI/review views so a human can inspect tasks, gates,
decisions, runtime pressure, and visible reasoning-summary availability without
manual path hunting. It assigns no behavioral verdicts and mutates no runtime
state.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


_FAILURE_GATE_STATES = {"gate_failed"}
_ROUTING_GATE_STATES = {"gate_escalated"}
_ROUTING_PLAN_ALIGNMENT_STATES = {"ready", "decision_posted"}
_ROUTING_COORDINATION_HANDOFF_STATES = {"submitted", "decision_posted", "notice_posted"}
_ATTENTION_STATES = {"failed", "dead"}


def _load_evidence_indexer():
    path = Path(__file__).resolve().parent / "build_behavioral_evidence_index.py"
    spec = importlib.util.spec_from_file_location("build_behavioral_evidence_index", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load evidence indexer at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def _count(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(key)) for row in rows if row.get(key) is not None).items()))


def _count_combo(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        values = [row.get(key) for key in keys]
        if any(value is None for value in values):
            continue
        counts[" / ".join(str(value) for value in values)] += 1
    return dict(sorted(counts.items()))


def _node_path(address: str) -> str:
    return str(address).split("#", 1)[0]


def _events_by_node(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        address = event.get("node_address")
        if address:
            by_node[str(address)].append(event)
    return by_node


def _artifacts_by_owner(artifacts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for artifact in artifacts:
        for owner in artifact.get("owner_node_addresses") or []:
            by_owner[str(owner)].append(artifact)
    return by_owner


def _count_by_owner(rows: list[dict[str, Any]], *, owner_key: str = "owner_node_addresses") -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        owners = row.get(owner_key) or []
        if not isinstance(owners, list):
            continue
        for owner in owners:
            counts[str(owner)] += 1
    return dict(counts)


def _gate_ids_from_row(row: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    if row.get("gate_id"):
        ids.add(str(row["gate_id"]))
    delta = row.get("binding_delta") if isinstance(row.get("binding_delta"), dict) else {}
    if delta.get("gate_id"):
        ids.add(str(delta["gate_id"]))
    return ids


def _compact_event(row: dict[str, Any]) -> dict[str, Any]:
    delta = row.get("binding_delta") if isinstance(row.get("binding_delta"), dict) else {}
    return {
        "seq": row.get("seq"),
        "ts": row.get("ts"),
        "node_address": row.get("node_address"),
        "event": row.get("event"),
        "summary": row.get("summary"),
        "gate_state": delta.get("gate_state"),
        "gate_id": delta.get("gate_id"),
        "gate_bounce_count": delta.get("gate_bounce_count"),
        "failure_class": delta.get("failure_class"),
        "terminal_signal": delta.get("terminal_signal"),
        "plan_alignment_state": delta.get("plan_alignment_state"),
        "plan_alignment_decision": delta.get("plan_alignment_decision"),
        "coordination_handoff_last_id": delta.get("coordination_handoff_last_id"),
        "coordination_handoff_last_state": delta.get("coordination_handoff_last_state"),
        "nonterminal_marker_error_last_key": delta.get("nonterminal_marker_error_last_key"),
    }


def _compact_inbox(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": row.get("ts"),
        "node_address": row.get("node_address"),
        "type": row.get("type"),
        "from": row.get("from"),
        "gate_id": row.get("gate_id"),
        "candidate": row.get("candidate"),
        "review": row.get("review"),
        "child": row.get("child"),
        "phase": row.get("phase"),
        "package": row.get("package"),
        "ready_artifact": row.get("ready_artifact"),
        "ready_artifact_sha256": row.get("ready_artifact_sha256"),
        "decision": row.get("decision"),
        "decision_artifact": row.get("decision_artifact"),
        "handoff_id": row.get("handoff_id"),
        "handoff_kind": row.get("handoff_kind"),
        "artifact": row.get("artifact"),
        "marker_artifact": row.get("marker_artifact"),
        "marker_sha256": row.get("marker_sha256"),
        "marker_kind": row.get("marker_kind"),
        "marker_error_key": row.get("marker_error_key"),
        "errors": row.get("errors"),
        "response_required": row.get("response_required"),
        "failure_class": row.get("failure_class"),
        "message": row.get("message"),
    }


def _attention_taxonomy(
    *,
    state: str = "",
    gate_state: str = "",
    plan_alignment_state: str = "",
    coordination_handoff_last_state: str = "",
    gate_bounce_count: int = 0,
    marker_error_count: int = 0,
    runtime_failures=None,
    failure_class=None,
) -> dict[str, Any]:
    """Separate failure, routing, and mandatory gate-bounce audit signals."""
    runtime_failures = list(runtime_failures or [])
    failure_signals: list[str] = []
    routing_signals: list[str] = []
    audit_signals: list[str] = []
    if state in _ATTENTION_STATES:
        failure_signals.append(f"state:{state}")
    if gate_state in _FAILURE_GATE_STATES:
        failure_signals.append(f"gate_state:{gate_state}")
    if failure_class:
        failure_signals.append(f"failure_class:{failure_class}")
    if runtime_failures:
        failure_signals.append("runtime_failure")
    if marker_error_count:
        failure_signals.append("nonterminal_marker_error")
    if gate_state in _ROUTING_GATE_STATES:
        routing_signals.append(f"gate_state:{gate_state}")
    if plan_alignment_state in _ROUTING_PLAN_ALIGNMENT_STATES:
        routing_signals.append(f"plan_alignment_state:{plan_alignment_state}")
    if coordination_handoff_last_state in _ROUTING_COORDINATION_HANDOFF_STATES:
        routing_signals.append(f"coordination_handoff:{coordination_handoff_last_state}")
    try:
        bounce_count = int(gate_bounce_count or 0)
    except (TypeError, ValueError):
        bounce_count = 0
    if gate_state == "gate_bounced":
        bounce_count = max(bounce_count, 1)
    if bounce_count:
        audit_signals.append(f"gate_bounce:count={bounce_count}")
    return {
        "attention_signals": [*failure_signals, *routing_signals, *audit_signals],
        "failure_signals": failure_signals,
        "routing_signals": routing_signals,
        "audit_signals": audit_signals,
        "needs_attention": bool(failure_signals),
        "needs_audit": bool(audit_signals),
    }


def _marker_errors(node: dict[str, Any]) -> dict[str, Any]:
    errors = node.get("nonterminal_marker_errors") or {}
    return errors if isinstance(errors, dict) else {}


def _marker_error_resolved(node: dict[str, Any], record: dict[str, Any]) -> bool:
    marker_kind = str(record.get("marker_kind") or "")
    marker_artifact = str(record.get("marker_artifact") or "")
    if not marker_artifact:
        return False
    if marker_kind == "plan_alignment":
        if str(node.get("plan_alignment_ready_artifact") or "") != marker_artifact:
            return False
        return str(node.get("plan_alignment_state") or "") in _ROUTING_PLAN_ALIGNMENT_STATES
    if marker_kind == "coordination_handoff":
        errors = "\n".join(str(error) for error in record.get("errors") or [])
        if "already exists with different content" in errors:
            return False
        handoffs = node.get("coordination_handoffs") or {}
        if not isinstance(handoffs, dict):
            return False
        marker_sha = record.get("marker_sha256")
        for handoff in handoffs.values():
            if not isinstance(handoff, dict):
                continue
            if str(handoff.get("marker_artifact") or "") != marker_artifact:
                continue
            if marker_sha and handoff.get("marker_sha256") == marker_sha:
                continue
            if str(handoff.get("state") or "") in _ROUTING_COORDINATION_HANDOFF_STATES:
                return True
    return False


def _unresolved_marker_error_count(node: dict[str, Any]) -> int:
    count = 0
    for record in _marker_errors(node).values():
        if not isinstance(record, dict):
            continue
        if not _marker_error_resolved(node, record):
            count += 1
    return count


def _task_view(index: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = list(index.get("nodes") or [])
    artifacts_by_owner = _artifacts_by_owner(list(index.get("artifacts") or []))
    events_by_node = _events_by_node(list(index.get("events") or []))
    trace_counts = _count_by_owner(list(index.get("trace_stanzas") or []))
    reference_counts = _count_by_owner(list(index.get("requirement_references") or []))
    runtime_failures_by_node = _events_by_node(list(index.get("runtime_failures") or []))
    reasoning_counts = Counter(
        str(row.get("node_address"))
        for row in index.get("reasoning_summaries") or []
        if row.get("node_address")
    )
    gate_packets_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for packet in index.get("gate_packets") or []:
        if packet.get("node_address"):
            gate_packets_by_path[str(packet["node_address"])].append(packet)

    tasks: list[dict[str, Any]] = []
    for node in sorted(nodes, key=lambda row: str(row.get("node_address") or "")):
        address = str(node.get("node_address") or "")
        artifacts = artifacts_by_owner.get(address, [])
        artifact_counts = _count(artifacts, "kind")
        schedule = {
            key: node.get(key)
            for key in (
                "schedule_policy",
                "schedule_group",
                "schedule_index",
                "admission_state",
                "waiting_on_sibling",
                "queue_reason",
                "queued_since",
                "admission_ready_at",
                "admission_released_by",
                "admission_blocked_at",
                "admission_blocked_by",
                "admission_block_reason",
                "admission_blocked_predecessor_state",
                "admission_blocked_predecessor_gate_state",
            )
            if node.get(key) is not None
        }
        current_gate_state = str(node.get("gate_state") or "")
        current_state = str(node.get("state") or "")
        failures = runtime_failures_by_node.get(address, [])
        plan_alignment = {
            key: node.get(key)
            for key in (
                "plan_alignment_state",
                "plan_alignment_package",
                "plan_alignment_ready_artifact",
                "plan_alignment_ready_sha256",
                "plan_alignment_ready_at",
                "plan_alignment_decision",
                "plan_alignment_decision_artifact",
                "plan_alignment_decision_at",
            )
            if node.get(key) is not None
        }
        coordination = {
            key: node.get(key)
            for key in (
                "coordination_handoffs",
                "coordination_handoff_last_id",
                "coordination_handoff_last_state",
            )
            if node.get(key) is not None
        }
        marker_errors = _marker_errors(node)
        marker_error_count = _unresolved_marker_error_count(node)
        attention = _attention_taxonomy(
            state=current_state,
            gate_state=current_gate_state,
            runtime_failures=failures,
            failure_class=node.get("failure_class"),
            plan_alignment_state=str(node.get("plan_alignment_state") or ""),
            coordination_handoff_last_state=str(node.get("coordination_handoff_last_state") or ""),
            gate_bounce_count=node.get("gate_bounce_count") or 0,
            marker_error_count=marker_error_count,
        )
        tasks.append(
            {
                "node_address": address,
                "level": node.get("level"),
                "seat": node.get("seat"),
                "role_variant": node.get("role_variant"),
                "parent_address": node.get("parent_address"),
                "state": node.get("state"),
                "gate_required": node.get("gate_required"),
                "gate_state": node.get("gate_state"),
                "gate_id": node.get("gate_id"),
                "gate_review_address": node.get("gate_review_address"),
                "terminal_signal": node.get("terminal_signal"),
                "failure_class": node.get("failure_class"),
                "schedule": schedule,
                "plan_alignment": plan_alignment,
                "coordination": coordination,
                "nonterminal_marker_error_count": marker_error_count,
                "nonterminal_marker_error_total_count": len(marker_errors),
                "artifact_counts": artifact_counts,
                "key_artifacts": sorted(
                    row.get("relpath")
                    for row in artifacts
                    if row.get("kind") in {"brief", "acceptance", "plan", "report", "review_artifact"}
                    and row.get("relpath")
                ),
                "gate_packet_count": len(gate_packets_by_path.get(_node_path(address), [])),
                "trace_stanza_count": trace_counts.get(address, 0),
                "requirement_reference_file_count": reference_counts.get(address, 0),
                "decision_count": artifact_counts.get("decision", 0),
                "reasoning_summary_count": reasoning_counts.get(address, 0),
                "runtime_failure_count": len(failures),
                "event_count": len(events_by_node.get(address, [])),
                **attention,
            }
        )
    return tasks


def _gate_view(index: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = list(index.get("nodes") or [])
    events = list(index.get("events") or [])
    inbox_rows = list(index.get("inbox_rows") or [])
    packets = list(index.get("gate_packets") or [])

    gate_ids: set[str] = set()
    for node in nodes:
        if node.get("gate_id"):
            gate_ids.add(str(node["gate_id"]))
    for row in events + inbox_rows + packets:
        gate_ids.update(_gate_ids_from_row(row))

    packet_by_gate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for packet in packets:
        if packet.get("gate_id"):
            packet_by_gate[str(packet["gate_id"])].append(packet)

    gates: list[dict[str, Any]] = []
    for gate_id in sorted(gate_ids):
        producer_nodes = [node for node in nodes if node.get("gate_id") == gate_id]
        event_rows = [row for row in events if gate_id in _gate_ids_from_row(row)]
        inbox_gate_rows = [row for row in inbox_rows if gate_id in _gate_ids_from_row(row)]
        producer = producer_nodes[0] if producer_nodes else {}
        review_addresses = sorted(
            {
                str(value)
                for value in [
                    producer.get("gate_review_address"),
                    *[row.get("review") for row in inbox_gate_rows],
                ]
                if value
            }
        )
        packets_for_gate = packet_by_gate.get(gate_id, [])
        event_counts = _count(event_rows, "event")
        try:
            recorded_bounce_count = int(producer.get("gate_bounce_count") or 0)
        except (TypeError, ValueError):
            recorded_bounce_count = 0
        bounce_count = max(recorded_bounce_count, int(event_counts.get("gate_bounced") or 0))
        bounce_events = [row for row in event_rows if row.get("event") == "gate_bounced"]
        last_bounce = bounce_events[-1] if bounce_events else None
        attention = _attention_taxonomy(
            gate_state=str(producer.get("gate_state") or ""),
            failure_class=producer.get("failure_class"),
            gate_bounce_count=bounce_count,
        )
        gates.append(
            {
                "gate_id": gate_id,
                "producer_address": producer.get("node_address") or (event_rows[0].get("node_address") if event_rows else None),
                "review_addresses": review_addresses,
                "current_gate_state": producer.get("gate_state"),
                "gate_bounce_count": bounce_count,
                "audit_label": (
                    "LOOK HERE — something probably went wrong; inspect every gate bounce"
                    if bounce_count
                    else None
                ),
                "last_bounce": _compact_event(last_bounce) if last_bounce else None,
                "failure_class": producer.get("failure_class"),
                "packet_count": len(packets_for_gate),
                "packet_paths": [packet.get("packet_path") for packet in packets_for_gate if packet.get("packet_path")],
                "has_review_plan": any(packet.get("has_review_plan") for packet in packets_for_gate),
                "event_counts": event_counts,
                "inbox_type_counts": _count(inbox_gate_rows, "type"),
                "event_timeline": [_compact_event(row) for row in event_rows],
                "inbox_timeline": [_compact_inbox(row) for row in inbox_gate_rows],
                **attention,
            }
        )
    return gates


def _decision_view(index: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = []
    for artifact in sorted(index.get("artifacts") or [], key=lambda row: str(row.get("relpath") or "")):
        if artifact.get("kind") != "decision":
            continue
        decisions.append(
            {
                "relpath": artifact.get("relpath"),
                "path": artifact.get("path"),
                "node_path": artifact.get("node_path"),
                "owner_node_addresses": artifact.get("owner_node_addresses") or [],
                "bytes": artifact.get("bytes"),
                "sha256": artifact.get("sha256"),
                "mtime_ns": artifact.get("mtime_ns"),
            }
        )
    return decisions


def _runtime_pressure_view(index: dict[str, Any]) -> dict[str, Any]:
    pressure = index.get("infrastructure_pressure") if isinstance(index.get("infrastructure_pressure"), dict) else {}
    codex = pressure.get("codex") if isinstance(pressure.get("codex"), dict) else {}
    runtime_failures = list(index.get("runtime_failures") or [])
    return {
        "codex": {
            "current_active_count": codex.get("current_active_count", 0),
            "current_active_nodes": codex.get("current_active_nodes") or [],
            "max_active_count": codex.get("max_active_count", 0),
            "known_seat_ids": codex.get("known_seat_ids") or [],
            "auth_versions": codex.get("auth_versions") or [],
            "runtime_failure_counts": codex.get("runtime_failure_counts") or {},
            "timeline": codex.get("timeline") or [],
        },
        "runtime_failures": runtime_failures,
        "runtime_failure_counts": _count(runtime_failures, "failure_class"),
    }


def _reasoning_view(index: dict[str, Any]) -> dict[str, Any]:
    stats = list(index.get("reasoning_summary_stats") or [])
    summaries = list(index.get("reasoning_summaries") or [])
    by_node: dict[str, dict[str, Any]] = {}
    for row in stats:
        address = str(row.get("node_address") or "")
        if not address:
            continue
        current = by_node.setdefault(
            address,
            {
                "node_address": address,
                "transcript_count": 0,
                "populated_summary_count": 0,
                "empty_thinking_blocks": 0,
                "missing_transcript_count": 0,
                "levels": set(),
                "role_variants": set(),
            },
        )
        current["transcript_count"] += 1
        current["populated_summary_count"] += int(row.get("populated_summary_count") or 0)
        current["empty_thinking_blocks"] += int(row.get("empty_thinking_blocks") or 0)
        if row.get("exists") is False:
            current["missing_transcript_count"] += 1
        if row.get("level"):
            current["levels"].add(str(row["level"]))
        if row.get("role_variant"):
            current["role_variants"].add(str(row["role_variant"]))

    rows = []
    for address, row in sorted(by_node.items()):
        rows.append(
            {
                "node_address": address,
                "transcript_count": row["transcript_count"],
                "populated_summary_count": row["populated_summary_count"],
                "empty_thinking_blocks": row["empty_thinking_blocks"],
                "missing_transcript_count": row["missing_transcript_count"],
                "levels": sorted(row["levels"]),
                "role_variants": sorted(row["role_variants"]),
            }
        )
    return {
        "summary_count": len(summaries),
        "nodes_with_populated_summaries": [
            row["node_address"]
            for row in rows
            if row["populated_summary_count"] > 0
        ],
        "nodes_without_populated_summaries": [
            row["node_address"]
            for row in rows
            if row["populated_summary_count"] == 0
        ],
        "by_node": rows,
    }


def _transcript_behavior_view(index: dict[str, Any], *, limit: int = 100) -> dict[str, Any]:
    digest = list(index.get("transcript_digest_events") or [])
    probes = list(index.get("transcript_probe_events") or [])
    def is_upper_review_probe(row: dict[str, Any]) -> bool:
        return (
            str(row.get("role_variant") or "").endswith("#review-check")
            or bool(str(row.get("review_axis") or ""))
        )

    upper_review_probes = [row for row in probes if is_upper_review_probe(row)]
    probes_sorted = sorted(
        probes,
        key=lambda row: (
            str(row.get("node_address") or ""),
            int(row.get("jsonl_line") or 0),
            int(row.get("command_index") or 0),
        ),
    )
    upper_review_probes_sorted = [row for row in probes_sorted if is_upper_review_probe(row)]
    return {
        "digest_event_count": len(digest),
        "probe_event_count": len(probes),
        "probe_counts_by_kind": _count(probes, "command_kind"),
        "probe_counts_by_level": _count(probes, "level"),
        "probe_counts_by_role_variant": _count(probes, "role_variant"),
        "probe_counts_by_review_axis": _count(probes, "review_axis"),
        "probe_counts_by_level_axis": _count_combo(probes, ("level", "review_axis")),
        "probe_counts_by_role_axis_kind": _count_combo(
            probes,
            ("role_variant", "review_axis", "command_kind"),
        ),
        "upper_review_probe_count": len(upper_review_probes),
        "upper_review_probe_counts_by_axis_kind": _count_combo(
            upper_review_probes,
            ("review_axis", "command_kind"),
        ),
        "probe_events": [
            {
                "node_address": row.get("node_address"),
                "level": row.get("level"),
                "role_variant": row.get("role_variant"),
                "review_axis": row.get("review_axis"),
                "command_kind": row.get("command_kind"),
                "timing_bucket": row.get("timing_bucket"),
                "jsonl_line": row.get("jsonl_line"),
                "command": row.get("command"),
                "nearest_reasoning_summary": row.get("nearest_reasoning_summary"),
                "nearest_assistant_text": row.get("nearest_assistant_text"),
                "result_excerpt": row.get("result_excerpt"),
                "transcript_path": row.get("transcript_path"),
            }
            for row in probes_sorted[:limit]
        ],
        "upper_review_probe_events": [
            {
                "node_address": row.get("node_address"),
                "level": row.get("level"),
                "role_variant": row.get("role_variant"),
                "review_axis": row.get("review_axis"),
                "command_kind": row.get("command_kind"),
                "timing_bucket": row.get("timing_bucket"),
                "jsonl_line": row.get("jsonl_line"),
                "nearest_reasoning_summary": row.get("nearest_reasoning_summary"),
                "nearest_assistant_text": row.get("nearest_assistant_text"),
                "command": row.get("command"),
                "result_excerpt": row.get("result_excerpt"),
                "transcript_path": row.get("transcript_path"),
            }
            for row in upper_review_probes_sorted[:limit]
        ],
    }


def build_views_from_index(index: dict[str, Any]) -> dict[str, Any]:
    tasks = _task_view(index)
    gates = _gate_view(index)
    decisions = _decision_view(index)
    runtime_pressure = _runtime_pressure_view(index)
    reasoning = _reasoning_view(index)
    transcript_behavior = _transcript_behavior_view(index)
    return {
        "schema_version": 1,
        "source_index_schema_version": index.get("schema_version"),
        "observer_effect": "read-only evidence-index projection; no runtime mutation and no behavioral verdicts",
        "runtime_root": index.get("runtime_root"),
        "runtime": index.get("runtime") or {},
        "counts": {
            "tasks": len(tasks),
            "gates": len(gates),
            "decisions": len(decisions),
            "runtime_failures": len(runtime_pressure["runtime_failures"]),
            "nodes_with_reasoning_summaries": len(reasoning["nodes_with_populated_summaries"]),
            "transcript_digest_events": transcript_behavior["digest_event_count"],
            "transcript_probe_events": transcript_behavior["probe_event_count"],
        },
        "views": {
            "tasks": tasks,
            "gates": gates,
            "decisions": decisions,
            "runtime_pressure": runtime_pressure,
            "reasoning": reasoning,
            "transcript_behavior": transcript_behavior,
        },
    }


def build_views(runtime_root: Path) -> dict[str, Any]:
    indexer = _load_evidence_indexer()
    return build_views_from_index(indexer.build_index(Path(runtime_root)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="runtime root, or evidence-index JSON when --from-index is set")
    parser.add_argument("--from-index", action="store_true", help="read an existing evidence-index JSON file")
    parser.add_argument("--output", "-o", help="write JSON to this path instead of stdout")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    args = parser.parse_args(argv)

    payload = (
        build_views_from_index(_read_json(Path(args.source)))
        if args.from_index
        else build_views(Path(args.source))
    )
    text = json.dumps(payload, ensure_ascii=False, indent=None if args.compact else 2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
