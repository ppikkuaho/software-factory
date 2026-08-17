#!/usr/bin/env python3
"""Build a read-only markdown dashboard from behavioral run views.

The JSON evidence index and views remain the machine surface. This tool renders a
compact human inspection surface from those passive artifacts. It assigns no
behavioral verdicts and mutates no runtime state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _cell(value: Any) -> str:
    return _text(value).replace("|", "\\|").replace("\n", " ").strip()


def _count_text(counts: dict[str, Any] | None) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["_None._"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return lines


def _attention_tasks(tasks: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    rows = [row for row in tasks if row.get("needs_attention")]
    return sorted(rows, key=lambda row: str(row.get("node_address") or ""))[:limit]


def _attention_gates(gates: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    rows = [row for row in gates if row.get("needs_attention")]
    return sorted(rows, key=lambda row: str(row.get("gate_id") or ""))[:limit]


def _audit_gates(gates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [row for row in gates if row.get("needs_audit")]
    return sorted(rows, key=lambda row: str(row.get("gate_id") or ""))


def _routing_gates(gates: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    rows = [
        row for row in gates
        if row.get("routing_signals") and not row.get("needs_attention")
    ]
    return sorted(rows, key=lambda row: str(row.get("gate_id") or ""))[:limit]


def build_dashboard(
    *,
    behavioral_views: dict[str, Any],
    score_packet: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> str:
    views = behavioral_views.get("views") if isinstance(behavioral_views.get("views"), dict) else {}
    counts = behavioral_views.get("counts") if isinstance(behavioral_views.get("counts"), dict) else {}
    scoreability = (
        score_packet.get("scoreability")
        if isinstance(score_packet, dict) and isinstance(score_packet.get("scoreability"), dict)
        else {}
    )
    runtime = behavioral_views.get("runtime") if isinstance(behavioral_views.get("runtime"), dict) else {}
    tasks = list(views.get("tasks") or [])
    gates = list(views.get("gates") or [])
    runtime_pressure = views.get("runtime_pressure") if isinstance(views.get("runtime_pressure"), dict) else {}
    reasoning = views.get("reasoning") if isinstance(views.get("reasoning"), dict) else {}
    transcript_behavior = (
        views.get("transcript_behavior")
        if isinstance(views.get("transcript_behavior"), dict)
        else {}
    )
    movement = (
        score_packet.get("movement_evidence")
        if isinstance(score_packet, dict) and isinstance(score_packet.get("movement_evidence"), dict)
        else {}
    )
    blocked_input = (
        score_packet.get("blocked_input_evidence")
        if isinstance(score_packet, dict)
        and isinstance(score_packet.get("blocked_input_evidence"), dict)
        else {}
    )

    title_id = run_id or runtime.get("build_id") or behavioral_views.get("runtime_root") or "run"
    lines: list[str] = [
        f"# Behavioral Run Dashboard: {_text(title_id)}",
        "",
        "Observer effect: read-only projection from evidence artifacts; no runtime mutation and no behavioral verdict assigned.",
        "",
        "## Scoreability",
        "",
        f"- runtime_contaminated: {_text(scoreability.get('runtime_contaminated'))}",
        f"- runtime_stopped: {_text(scoreability.get('runtime_stopped'))}",
        # Incidents first (what a reader should reason from); the raw row count stays as detail.
        f"- runtime_failure_incidents: {_text(scoreability.get('runtime_failure_incident_count', scoreability.get('runtime_failure_count', 0)))}",
        f"- runtime_failure_rows: {_text(scoreability.get('runtime_failure_count', 0))}",
        f"- run_lifecycle: {_text((scoreability.get('run_lifecycle') or {}).get('state'))}",
        "",
        "## Inventory",
        "",
        f"- tasks: {_text(counts.get('tasks', len(tasks)))}",
        f"- gates: {_text(counts.get('gates', len(gates)))}",
        f"- decisions: {_text(counts.get('decisions', len(views.get('decisions') or [])))}",
        f"- runtime_failures: {_text(counts.get('runtime_failures', 0))}",
        f"- nodes_with_reasoning_summaries: {_text(counts.get('nodes_with_reasoning_summaries', 0))}",
        f"- transcript_digest_events: {_text(counts.get('transcript_digest_events', 0))}",
        f"- transcript_probe_events: {_text(counts.get('transcript_probe_events', 0))}",
        "",
        "## Gate Bounce Audit — Look Here",
        "",
        "Any bounce means something probably went wrong. Inspect the frozen candidate, "
        "the cited contract clause, and the review finding even if the gate later passed.",
        "",
    ]
    lines.extend(
        _table(
            ["gate_id", "producer", "state", "bounces", "audit_signals", "events"],
            [
                [
                    row.get("gate_id"),
                    row.get("producer_address"),
                    row.get("current_gate_state"),
                    row.get("gate_bounce_count"),
                    ", ".join(row.get("audit_signals") or []),
                    _count_text(
                        row.get("event_counts")
                        if isinstance(row.get("event_counts"), dict)
                        else {}
                    ),
                ]
                for row in _audit_gates(gates)
            ],
        )
    )
    lines.extend([
        "",
        "## Failure Attention Nodes",
        "",
    ])
    lines.extend(
        _table(
            ["node", "level", "state", "gate_state", "failure_signals", "artifacts"],
            [
                [
                    row.get("node_address"),
                    row.get("level"),
                    row.get("state"),
                    row.get("gate_state"),
                    ", ".join(row.get("failure_signals") or []),
                    _count_text(row.get("artifact_counts") if isinstance(row.get("artifact_counts"), dict) else {}),
                ]
                for row in _attention_tasks(tasks)
            ],
        )
    )
    lines.extend(["", "## Failure Attention Gates", ""])
    lines.extend(
        _table(
            ["gate_id", "producer", "state", "bounces", "failure_signals", "events"],
            [
                [
                    row.get("gate_id"),
                    row.get("producer_address"),
                    row.get("current_gate_state"),
                    row.get("gate_bounce_count"),
                    ", ".join(row.get("failure_signals") or []),
                    _count_text(row.get("event_counts") if isinstance(row.get("event_counts"), dict) else {}),
                ]
                for row in _attention_gates(gates)
            ],
        )
    )
    lines.extend(["", "## Review Routing Gates", ""])
    lines.extend(
        _table(
            ["gate_id", "producer", "state", "bounces", "routing_signals", "events"],
            [
                [
                    row.get("gate_id"),
                    row.get("producer_address"),
                    row.get("current_gate_state"),
                    row.get("gate_bounce_count"),
                    ", ".join(row.get("routing_signals") or []),
                    _count_text(row.get("event_counts") if isinstance(row.get("event_counts"), dict) else {}),
                ]
                for row in _routing_gates(gates)
            ],
        )
    )
    codex = runtime_pressure.get("codex") if isinstance(runtime_pressure.get("codex"), dict) else {}
    lines.extend(
        [
            "",
            "## Runtime Pressure",
            "",
            f"- codex_current_active: {_text(codex.get('current_active_count', 0))}",
            f"- codex_max_active: {_text(codex.get('max_active_count', 0))}",
            f"- codex_runtime_failures: {_count_text(codex.get('runtime_failure_counts') if isinstance(codex.get('runtime_failure_counts'), dict) else {})}",
            f"- runtime_failure_counts: {_count_text(runtime_pressure.get('runtime_failure_counts') if isinstance(runtime_pressure.get('runtime_failure_counts'), dict) else {})}",
            "",
            "## Reasoning Evidence",
            "",
            f"- summary_count: {_text(reasoning.get('summary_count', 0))}",
            f"- nodes_with_populated_summaries: {_text(len(reasoning.get('nodes_with_populated_summaries') or []))}",
            f"- nodes_without_populated_summaries: {_text(len(reasoning.get('nodes_without_populated_summaries') or []))}",
            "",
            "## Transcript Probe Events",
            "",
            f"- probe_counts_by_kind: {_count_text(transcript_behavior.get('probe_counts_by_kind') if isinstance(transcript_behavior.get('probe_counts_by_kind'), dict) else {})}",
            f"- probe_counts_by_level: {_count_text(transcript_behavior.get('probe_counts_by_level') if isinstance(transcript_behavior.get('probe_counts_by_level'), dict) else {})}",
            f"- probe_counts_by_review_axis: {_count_text(transcript_behavior.get('probe_counts_by_review_axis') if isinstance(transcript_behavior.get('probe_counts_by_review_axis'), dict) else {})}",
            f"- probe_counts_by_level_axis: {_count_text(transcript_behavior.get('probe_counts_by_level_axis') if isinstance(transcript_behavior.get('probe_counts_by_level_axis'), dict) else {})}",
            f"- probe_counts_by_role_axis_kind: {_count_text(transcript_behavior.get('probe_counts_by_role_axis_kind') if isinstance(transcript_behavior.get('probe_counts_by_role_axis_kind'), dict) else {})}",
            f"- upper_review_probe_count: {_text(transcript_behavior.get('upper_review_probe_count', 0))}",
            f"- upper_review_probe_counts_by_axis_kind: {_count_text(transcript_behavior.get('upper_review_probe_counts_by_axis_kind') if isinstance(transcript_behavior.get('upper_review_probe_counts_by_axis_kind'), dict) else {})}",
            "",
        ]
    )
    lines.extend(
        _table(
            ["node", "level", "axis", "kind", "timing", "line", "command", "reason", "result"],
            [
                [
                    row.get("node_address"),
                    row.get("level"),
                    row.get("review_axis"),
                    row.get("command_kind"),
                    row.get("timing_bucket"),
                    row.get("jsonl_line"),
                    row.get("command"),
                    row.get("nearest_reasoning_summary") or row.get("nearest_assistant_text"),
                    row.get("result_excerpt"),
                ]
                for row in list(transcript_behavior.get("probe_events") or [])[:12]
            ],
        )
    )
    incidents = list(blocked_input.get("incidents") or [])
    lines.extend(["", "## Blocked-on-input incidents", ""])
    if not incidents:
        lines.append("None occurred.")
    else:
        lines.extend(
            _table(
                [
                    "incident",
                    "seat",
                    "timestamp",
                    "class",
                    "silent_s",
                    "pane",
                    "signature",
                    "cancel",
                    "retriggered",
                    "escalated",
                ],
                [
                    [
                        row.get("incident_id"),
                        row.get("node_address"),
                        row.get("started_at"),
                        row.get("classification"),
                        row.get("silent_seconds"),
                        row.get("pane_excerpt"),
                        row.get("prompt_signature"),
                        (
                            "unknown-delivery"
                            if row.get("cancel_status") == "pending"
                            else row.get("cancel_status")
                        ),
                        row.get("retriggered"),
                        row.get("escalated"),
                    ]
                    for row in incidents
                ],
            )
        )
    lines.extend(
        [
            "",
            "## Movement Evidence",
            "",
            f"- movement_event_counts: {_count_text(movement.get('event_counts') if isinstance(movement.get('event_counts'), dict) else {})}",
            "",
            "## Artifact Pointers",
            "",
            f"- runtime_root: {_text(behavioral_views.get('runtime_root'))}",
            f"- source_index_schema_version: {_text(behavioral_views.get('source_index_schema_version'))}",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--views", required=True, help="behavioral-views JSON path")
    parser.add_argument("--score-packet", default=None, help="optional run-score-packet JSON path")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output", "-o", help="write markdown to this path instead of stdout")
    args = parser.parse_args(argv)

    score = _read_json(Path(args.score_packet)) if args.score_packet else None
    text = build_dashboard(
        behavioral_views=_read_json(Path(args.views)),
        score_packet=score,
        run_id=args.run_id,
    )
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
