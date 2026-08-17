#!/usr/bin/env python3
"""Build a read-only run-adherence scoring packet from a harness runtime root.

This is not an automated behavioral score. It packages the evidence a reviewer
needs for the RUN-ADHERENCE-AUDIT rows while preserving the judgment boundary:
PASS/PARTIAL/FAIL remains a human/evaluator decision against the rubric.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


_MOVEMENT_EVENTS = {"git_merged", "delivered", "delivery_failed", "delivery_failed_escalation"}
_GATE_ROUTING_EVENTS = {"gate_escalated"}
_GATE_AUDIT_EVENTS = {"gate_bounced"}
_GATE_FAILURE_EVENTS = {"gate_failed"}
_TRACE_EXPECTED_ARTIFACT_KINDS = {
    "acceptance",
    "brief",
    "plan",
    "report",
    "review_artifact",
    "review_packet",
    "review_plan",
}


def _load_evidence_indexer():
    path = Path(__file__).resolve().parent / "build_behavioral_evidence_index.py"
    spec = importlib.util.spec_from_file_location("build_behavioral_evidence_index", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load evidence indexer at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _count(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(key)) for row in rows if row.get(key) is not None).items()))


def _defect_code(defect: str) -> str:
    head = str(defect).split(":", 1)[0].strip()
    return head or "UNKNOWN"


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
        "source_branch": delta.get("source_branch"),
        "target_branch": delta.get("target_branch"),
        "deliverable_state": delta.get("deliverable_state"),
        "delivery_destination": delta.get("delivery_destination"),
    }


def _contract_evidence(index: dict[str, Any]) -> dict[str, Any]:
    defects = list(index.get("return_contract_defects") or [])
    code_counts: Counter[str] = Counter()
    by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in defects:
        node = str(event.get("node_address") or "")
        event_codes = []
        for defect in event.get("defects") or []:
            code = _defect_code(str(defect))
            code_counts[code] += 1
            event_codes.append(code)
        by_node[node].append(
            {
                "seq": event.get("seq"),
                "signal_artifact_seen_at": event.get("signal_artifact_seen_at"),
                "defect_codes": event_codes,
                "defects": list(event.get("defects") or []),
            }
        )
    return {
        "return_contract_defect_events": len(defects),
        "defect_code_counts": dict(sorted(code_counts.items())),
        "defects_by_node": dict(sorted(by_node.items())),
    }


def _trace_evidence(index: dict[str, Any]) -> dict[str, Any]:
    trace_rows = list(index.get("trace_stanzas") or [])
    reference_rows = list(index.get("requirement_references") or [])
    parse_errors = [
        {
            "path": row.get("path"),
            "relpath": row.get("relpath"),
            "line": row.get("line"),
            "parse_error": row.get("parse_error"),
        }
        for row in trace_rows
        if row.get("parse_error")
    ]
    references_without_trace = [
        {
            "path": row.get("path"),
            "relpath": row.get("relpath"),
            "artifact_kind": row.get("artifact_kind"),
            "owner_node_addresses": row.get("owner_node_addresses") or [],
            "requirement_ids": row.get("requirement_ids") or [],
        }
        for row in reference_rows
        if row.get("artifact_kind") in _TRACE_EXPECTED_ARTIFACT_KINDS
        and not row.get("has_trace_stanza")
    ]
    return {
        "trace_stanzas": len(trace_rows),
        "trace_parse_errors": parse_errors,
        "requirement_reference_files": len(reference_rows),
        "requirement_reference_files_without_trace": references_without_trace,
    }


def _transcript_activity(index: dict[str, Any]) -> dict[str, Any]:
    transcripts = list(index.get("transcripts") or [])
    fields = [
        "assistant_rows",
        "user_rows",
        "codex_response_items",
        "codex_message_items",
        "codex_function_call_items",
        "codex_function_call_output_items",
        "codex_reasoning_items",
        "codex_reasoning_summary_items",
        "codex_reasoning_empty_summary_items",
        "codex_reasoning_encrypted_items",
        "codex_event_msg_rows",
        "codex_token_count_events",
        "codex_tool_result_events",
        "transcript_digest_events",
        "transcript_probe_events",
    ]
    totals = {
        field: sum(int(row.get(field) or 0) for row in transcripts)
        for field in fields
    }
    codex_transcripts = [
        {
            "node_address": row.get("node_address"),
            "transcript_path": row.get("transcript_path"),
            "current_binding": row.get("current_binding"),
            "codex_message_items": row.get("codex_message_items") or 0,
            "codex_function_call_items": row.get("codex_function_call_items") or 0,
            "codex_function_call_output_items": row.get("codex_function_call_output_items") or 0,
            "codex_reasoning_items": row.get("codex_reasoning_items") or 0,
            "codex_reasoning_empty_summary_items": row.get("codex_reasoning_empty_summary_items") or 0,
            "codex_reasoning_encrypted_items": row.get("codex_reasoning_encrypted_items") or 0,
            "codex_token_count_events": row.get("codex_token_count_events") or 0,
        }
        for row in transcripts
        if int(row.get("codex_response_items") or 0) > 0
        or int(row.get("codex_event_msg_rows") or 0) > 0
    ]
    return {
        "totals": totals,
        "codex_transcripts": codex_transcripts,
        "probe_counts_by_kind": _count(list(index.get("transcript_probe_events") or []), "command_kind"),
        "probe_counts_by_level": _count(list(index.get("transcript_probe_events") or []), "level"),
        "probe_counts_by_review_axis": _count(list(index.get("transcript_probe_events") or []), "review_axis"),
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
            for row in list(index.get("transcript_probe_events") or [])[:100]
        ],
    }


def build_score_packet_from_index(index: dict[str, Any]) -> dict[str, Any]:
    nodes = list(index.get("nodes") or [])
    events = list(index.get("events") or [])
    inbox_rows = list(index.get("inbox_rows") or [])
    runtime_failures = list(index.get("runtime_failures") or [])
    contaminating_runtime_failures = [
        row for row in runtime_failures
        if row.get("contaminates") is not False
    ]
    run_lifecycle = index.get("run_lifecycle") if isinstance(index.get("run_lifecycle"), dict) else {}
    runtime_stopped = run_lifecycle.get("state") == "stopped_runtime"

    excluded_nodes = sorted(
        {
            str(row.get("node_address"))
            for row in contaminating_runtime_failures
            if row.get("node_address")
        }
    )
    all_nodes = sorted(str(row.get("node_address")) for row in nodes if row.get("node_address"))
    scoreable_nodes = [node for node in all_nodes if node not in set(excluded_nodes)]
    gate_events = [
        row for row in events
        if str(row.get("event") or "").startswith("gate_")
    ]
    movement_events = [
        row for row in events
        if row.get("event") in _MOVEMENT_EVENTS
    ]
    reasoning_stats = list(index.get("reasoning_summary_stats") or [])
    nodes_with_reasoning = sorted(
        {
            str(row.get("node_address"))
            for row in reasoning_stats
            if row.get("node_address") and int(row.get("populated_summary_count") or 0) > 0
        }
    )

    return {
        "schema_version": 1,
        "source_index_schema_version": index.get("schema_version"),
        "observer_effect": "read-only evidence-index projection; no runtime mutation and no verdict assignment",
        "runtime_root": index.get("runtime_root"),
        "runtime": index.get("runtime") or {},
        "scoring_mode": {
            "kind": "evidence_packet_only",
            "verdict_policy": (
                "This packet prepares evidence for RUN-ADHERENCE-AUDIT rows; it does not assign "
                "PASS/PARTIAL/FAIL."
            ),
        },
        "scoreability": {
            "runtime_contaminated": bool(contaminating_runtime_failures) or runtime_stopped,
            "runtime_stopped": runtime_stopped,
            "run_lifecycle": run_lifecycle,
            "runtime_failure_count": len(contaminating_runtime_failures),
            # INCIDENTS, not rows: the per-row count materially misleads (the 2026-06-17
            # delivered run reported 28 rows that were TWO transient 529 storms on two seats).
            # Readers should reason from incidents; rows remain the detail underneath.
            "runtime_failure_incidents": list(index.get("runtime_failure_incidents") or []),
            "runtime_failure_incident_count": len(
                list(index.get("runtime_failure_incidents") or [])
            ),
            "non_contaminating_runtime_failure_count": (
                len(runtime_failures) - len(contaminating_runtime_failures)
            ),
            "excluded_node_addresses": excluded_nodes,
            "scoreable_node_addresses": scoreable_nodes,
            "rule": (
                "auth/runtime-capacity failures and stopped-runtime conditions are infrastructure evidence, "
                "not agent-behavior failures"
            ),
        },
        "node_inventory": {
            "total_nodes": len(nodes),
            "state_counts": _count(nodes, "state"),
            "level_counts": _count(nodes, "level"),
            "gate_state_counts": _count(nodes, "gate_state"),
            "terminal_signal_counts": _count(nodes, "terminal_signal"),
        },
        "gate_evidence": {
            "gate_event_counts": _count(gate_events, "event"),
            "routing_event_counts": _count(
                [row for row in gate_events if row.get("event") in _GATE_ROUTING_EVENTS],
                "event",
            ),
            "audit_event_counts": _count(
                [row for row in gate_events if row.get("event") in _GATE_AUDIT_EVENTS],
                "event",
            ),
            "failure_event_counts": _count(
                [row for row in gate_events if row.get("event") in _GATE_FAILURE_EVENTS],
                "event",
            ),
            "routing_policy": (
                "gate_escalated is review-routing evidence unless paired with gate_failed, "
                "lifecycle failure, or runtime contamination"
            ),
            "audit_policy": (
                "Every gate_bounced event is a LOOK CLOSER audit signal: something probably went "
                "wrong and the frozen candidate, cited contract, and finding must be inspected. "
                "A bounce is not automatically scored as product failure and remains visible even "
                "after a later gate PASS."
            ),
            "gate_timeline": [_compact_event(row) for row in gate_events],
            "inbox_type_counts": _count(inbox_rows, "type"),
            "gate_packet_count": len(index.get("gate_packets") or []),
        },
        "movement_evidence": {
            "event_counts": _count(movement_events, "event"),
            "timeline": [_compact_event(row) for row in movement_events],
        },
        "contract_evidence": _contract_evidence(index),
        "blocked_input_evidence": {
            "incident_count": len(list(index.get("blocked_input_incidents") or [])),
            "incidents": list(index.get("blocked_input_incidents") or []),
        },
        "trace_evidence": _trace_evidence(index),
        "reasoning_evidence": {
            "transcript_count": len(index.get("transcripts") or []),
            "reasoning_summary_count": len(index.get("reasoning_summaries") or []),
            "nodes_with_reasoning_summaries": nodes_with_reasoning,
            "summary_stats": reasoning_stats,
            "transcript_activity": _transcript_activity(index),
        },
    }


def build_score_packet(runtime_root: Path) -> dict[str, Any]:
    indexer = _load_evidence_indexer()
    return build_score_packet_from_index(indexer.build_index(Path(runtime_root)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_root", help="harness runtime root to packetize")
    parser.add_argument("--output", "-o", help="write JSON to this path instead of stdout")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    args = parser.parse_args(argv)

    payload = build_score_packet(Path(args.runtime_root))
    text = json.dumps(payload, ensure_ascii=False, indent=None if args.compact else 2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
