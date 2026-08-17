#!/usr/bin/env python3
"""Build a passive behavioral-evidence index for one harness runtime root.

The indexer is read-only: it parses the durable runtime artifacts that already
exist (binding ledger, WAL, inboxes, review packets, transcripts) and emits a
single JSON object for audit/scoring/visualization. It does not write into the
runtime tree, send wake messages, or interact with agent panes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from harnessd.runtime_failures import (  # noqa: E402
    RUNTIME_FAILURE_CLASSES,
    _claude_message_text,
    claude_textual_tool_invocation,
    runtime_failure_from_transcript_event,
)
from harnessd import commissioning as _commissioning  # noqa: E402


_TRACE_FIELDS = {"id", "serves", "kind", "level", "node"}
_TRACE_STANZA = re.compile(r"<!--\s*trace:\s*\{(.*?)\}\s*-->", re.DOTALL)
_SERVES_LIST = re.compile(r"serves:\s*\[([^\]]*)\]")
_REQ_ID = re.compile(r"\bR-\d+(?:\.\d+)*\b")
_SNIPPET_LIMIT = 600
_COMMAND_LIMIT = 1200
_PROBE_PATTERNS = (
    (
        "pytest",
        re.compile(
            r"(?:^|[;&|]\s*)(?:[A-Z_][A-Z0-9_]*=\S+\s+)*pytest(?:\s|$)"
            r"|\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+pytest\b",
            re.I,
        ),
    ),
    ("unit_test", re.compile(r"\b(go|cargo|swift)\s+test\b|\b(npm|pnpm|yarn)\s+(run\s+)?test\b|\bunittest\b", re.I)),
    ("lint", re.compile(r"\b(ruff|eslint|flake8|black\s+--check|mypy|tsc|npm\s+run\s+lint|pnpm\s+lint|yarn\s+lint)\b", re.I)),
    ("build", re.compile(r"\b(go|cargo)\s+build\b|\b(npm|pnpm|yarn)\s+(run\s+)?build\b", re.I)),
    ("hash", re.compile(r"\b(shasum|sha256sum|md5sum|openssl\s+dgst)\b", re.I)),
    ("browser_probe", re.compile(r"\b(playwright|selenium|lighthouse|chrome|chromium)\b", re.I)),
    ("http_probe", re.compile(r"\b(curl|wget)\b", re.I)),
    ("smoke", re.compile(r"\b(smoke|python(?:\d+(?:\.\d+)?)?\s+-c|node\s+-e)\b", re.I)),
)
_SHELL_TOOL_NAMES = {"Bash", "Shell", "exec_command", "shell", "bash"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_thinking_extractor():
    path = Path(__file__).resolve().parent / "extract_claude_thinking_summaries.py"
    spec = importlib.util.spec_from_file_location("extract_claude_thinking_summaries", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load thinking-summary extractor at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _parse_json_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text:
        return None
    if "\t" in text:
        prefix, payload = text.split("\t", 1)
        if prefix.isdigit():
            text = payload
    try:
        row = json.loads(text)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def _read_json_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            row = _parse_json_line(line)
            if row is None:
                continue
            row.setdefault("_line", line_no)
            rows.append(row)
    return rows


def _seat_from_address(address: str) -> str:
    if "#" not in address:
        return "exec"
    return address.rsplit("#", 1)[1]


def _node_path(address: str) -> Path:
    pathish = address.split("#", 1)[0]
    return Path(*[part for part in pathish.split("/") if part])


def _node_dir(runtime_root: Path, address: str) -> Path:
    return runtime_root / "nodes" / _node_path(address)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _transcript_key(path: str | None) -> str | None:
    if not path:
        return None
    return str(Path(path).expanduser().resolve(strict=False))


def _binding_summary(runtime_root: Path, address: str, binding: dict[str, Any]) -> dict[str, Any]:
    node_dir = _node_dir(runtime_root, address)
    transcript = binding.get("transcript_path")
    return {
        "node_address": address,
        "seat": _seat_from_address(address),
        "node_dir": str(node_dir),
        "state": binding.get("state"),
        "level": binding.get("level"),
        "role_variant": binding.get("role_variant"),
        "parent_address": binding.get("parent_address"),
        "gate_required": binding.get("gate_required"),
        "gate_state": binding.get("gate_state"),
        "gate_for": binding.get("gate_for"),
        "gate_id": binding.get("gate_id"),
        "gate_review_address": binding.get("gate_review_address"),
        "gate_candidate_artifact_manifest": binding.get("gate_candidate_artifact_manifest"),
        "gate_candidate_artifact_manifest_sha256": binding.get("gate_candidate_artifact_manifest_sha256"),
        "gate_candidate_artifact_snapshot_dir": binding.get("gate_candidate_artifact_snapshot_dir"),
        "gate_bounce_count": binding.get("gate_bounce_count"),
        "terminal_signal": binding.get("terminal_signal"),
        "terminal_note": binding.get("terminal_note"),
        "failure_class": binding.get("failure_class"),
        "runtime_failure": binding.get("runtime_failure"),
        "model_used": binding.get("model_used"),
        "codex_seat_id": binding.get("codex_seat_id"),
        "auth_version": binding.get("auth_version"),
        "codex_access_seconds_remaining": binding.get("codex_access_seconds_remaining"),
        "admission_state": binding.get("admission_state"),
        "queue_reason": binding.get("queue_reason"),
        "queued_since": binding.get("queued_since"),
        "admission_ready_at": binding.get("admission_ready_at"),
        "admission_released_by": binding.get("admission_released_by"),
        "admission_blocked_at": binding.get("admission_blocked_at"),
        "admission_blocked_by": binding.get("admission_blocked_by"),
        "admission_block_reason": binding.get("admission_block_reason"),
        "admission_blocked_predecessor_state": binding.get("admission_blocked_predecessor_state"),
        "admission_blocked_predecessor_gate_state": binding.get(
            "admission_blocked_predecessor_gate_state"
        ),
        "waiting_on_sibling": binding.get("waiting_on_sibling"),
        "schedule_policy": binding.get("schedule_policy"),
        "schedule_group": binding.get("schedule_group"),
        "schedule_index": binding.get("schedule_index"),
        "plan_alignment_state": binding.get("plan_alignment_state"),
        "plan_alignment_ready_artifact": binding.get("plan_alignment_ready_artifact"),
        "plan_alignment_ready_sha256": binding.get("plan_alignment_ready_sha256"),
        "plan_alignment_package": binding.get("plan_alignment_package"),
        "plan_alignment_ready_at": binding.get("plan_alignment_ready_at"),
        "plan_alignment_decision": binding.get("plan_alignment_decision"),
        "plan_alignment_decision_artifact": binding.get("plan_alignment_decision_artifact"),
        "plan_alignment_decision_at": binding.get("plan_alignment_decision_at"),
        "coordination_handoffs": binding.get("coordination_handoffs"),
        "coordination_handoff_last_id": binding.get("coordination_handoff_last_id"),
        "coordination_handoff_last_state": binding.get("coordination_handoff_last_state"),
        "nonterminal_marker_errors": binding.get("nonterminal_marker_errors"),
        "nonterminal_marker_error_last_key": binding.get("nonterminal_marker_error_last_key"),
        "session_uuid": binding.get("session_uuid"),
        "transcript_path": transcript,
        "transcript_exists": bool(transcript and Path(transcript).is_file()),
        "tmux_target": binding.get("tmux_target"),
        "owner_token_present": bool(binding.get("owner_token")),
        "lease_epoch": binding.get("lease_epoch"),
        "generation": binding.get("generation"),
    }


def _event_summary(row: dict[str, Any]) -> dict[str, Any]:
    delta = row.get("binding_delta") if isinstance(row.get("binding_delta"), dict) else {}
    keep_delta = {
        key: delta.get(key)
        for key in (
            "state",
            "gate_state",
            "gate_id",
            "gate_candidate_artifact_manifest",
            "gate_candidate_artifact_manifest_sha256",
            "gate_bounce_count",
            "gate_failure_count",
            "terminal_signal",
            "terminal_note",
            "signal_artifact_seen_at",
            "in_flight_release",
            "collapse_lease_epoch",
            "wake_pending_ack_offset",
            "last_inbox_acked_offset",
            "failure_class",
            "failure_reason",
            "runtime_failure",
            "claim_released",
            "session_uuid",
            "transcript_path",
            "model_used",
            "codex_seat_id",
            "auth_version",
            "codex_access_seconds_remaining",
            "admission_state",
            "queue_reason",
            "queued_since",
            "admission_ready_at",
            "admission_released_by",
            "admission_blocked_at",
            "admission_blocked_by",
            "admission_block_reason",
            "admission_blocked_predecessor_state",
            "admission_blocked_predecessor_gate_state",
            "waiting_on_sibling",
            "schedule_policy",
            "schedule_group",
            "schedule_index",
            "plan_alignment_state",
            "plan_alignment_ready_artifact",
            "plan_alignment_ready_sha256",
            "plan_alignment_package",
            "plan_alignment_ready_at",
            "plan_alignment_decision",
            "plan_alignment_decision_artifact",
            "plan_alignment_decision_at",
            "coordination_handoffs",
            "coordination_handoff_last_id",
            "coordination_handoff_last_state",
            "nonterminal_marker_errors",
            "nonterminal_marker_error_last_key",
            "defects",
            "agent_signal_ts",
        )
        if key in delta
    }
    return {
        "seq": row.get("seq"),
        "ts": row.get("ts"),
        "node_address": row.get("node_address"),
        "event": row.get("event"),
        "actor": row.get("actor"),
        "from_state": row.get("from_state"),
        "to_state": row.get("to_state"),
        "generation": row.get("generation"),
        "lease_epoch": row.get("lease_epoch"),
        "summary": row.get("summary"),
        "binding_delta": keep_delta,
        "line": row.get("_line"),
    }


def _inbox_rows(runtime_root: Path) -> list[dict[str, Any]]:
    root = runtime_root / "nodes"
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for inbox in sorted(root.rglob(".inbox.*.jsonl")):
        seat = inbox.name.removeprefix(".inbox.").removesuffix(".jsonl")
        node_address = str(inbox.parent.relative_to(root)).replace("/", "/")
        address = f"{node_address}#{seat}"
        for row in _read_json_rows(inbox):
            rows.append(
                {
                    "node_address": address,
                    "inbox_path": str(inbox),
                    "line": row.get("_line"),
                    "type": row.get("type"),
                    "from": row.get("from"),
                    "child": row.get("child"),
                    "terminal_signal": row.get("terminal_signal"),
                    "terminal_note": row.get("terminal_note"),
                    "failure_class": row.get("failure_class"),
                    "failure_reason": row.get("failure_reason"),
                    "model_used": row.get("model_used"),
                    "claim_released": row.get("claim_released"),
                    "collapse_generation": row.get("collapse_generation"),
                    "collapse_lease_epoch": row.get("collapse_lease_epoch"),
                    "gate_id": row.get("gate_id"),
                    "candidate": row.get("candidate"),
                    "review": row.get("review"),
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
                    "signal_artifact_seen_at": row.get("signal_artifact_seen_at"),
                    "message": row.get("message"),
                    "ts": row.get("ts"),
                }
            )
    return rows


def _gate_packets(runtime_root: Path) -> list[dict[str, Any]]:
    nodes_root = runtime_root / "nodes"
    packets: list[dict[str, Any]] = []
    if not nodes_root.is_dir():
        return packets
    for packet in sorted(nodes_root.rglob("reviews/*/review-packet.md")):
        gate_dir = packet.parent
        node_address = str(gate_dir.parent.parent.relative_to(nodes_root))
        gate_id = gate_dir.name
        manifest_path = gate_dir / "candidate-artifacts.json"
        manifest_sha = None
        manifest_artifact_count = None
        snapshot_dir = gate_dir / "candidate-snapshot"
        snapshot_file_count = (
            len([path for path in snapshot_dir.rglob("*") if path.is_file()])
            if snapshot_dir.is_dir()
            else 0
        )
        if manifest_path.is_file():
            raw = manifest_path.read_bytes()
            manifest_sha = hashlib.sha256(raw).hexdigest()
            try:
                manifest = json.loads(raw.decode("utf-8"))
                manifest_artifact_count = len(manifest.get("artifacts") or [])
            except (UnicodeDecodeError, json.JSONDecodeError):
                manifest_artifact_count = None
        files = []
        for path in sorted(gate_dir.rglob("*")):
            if path.is_file():
                files.append(
                    {
                        "path": str(path),
                        "relpath": _rel(path, runtime_root),
                        "bytes": path.stat().st_size,
                    }
                )
        packets.append(
            {
                "node_address": node_address,
                "gate_id": gate_id,
                "review_dir": str(gate_dir),
                "packet_path": str(packet),
                "candidate_artifact_manifest": str(manifest_path) if manifest_path.is_file() else None,
                "candidate_artifact_manifest_sha256": manifest_sha,
                "candidate_artifact_snapshot_dir": str(snapshot_dir),
                "candidate_artifact_snapshot_file_count": snapshot_file_count,
                "candidate_artifact_count": manifest_artifact_count,
                "has_review_plan": (gate_dir / "review-plan.md").is_file(),
                "files": files,
            }
        )
    return packets


def _artifact_kind(path: Path, runtime_root: Path) -> str:
    name = path.name
    parts = path.relative_to(runtime_root).parts
    if "candidate-snapshot" in parts:
        return "candidate_snapshot"
    if name.startswith(".inbox.") and name.endswith(".jsonl"):
        return "inbox"
    if name.startswith(".signal.") and name.endswith(".json"):
        return "terminal_signal"
    if name.startswith(".sign-off.") and name.endswith(".json"):
        return "sign_off"
    if name == "brief.md":
        return "brief"
    if name == "plan.md":
        return "plan"
    if name == "report.md":
        return "report"
    if name == "acceptance.md":
        return "acceptance"
    if name == "plan-alignment-ready.json":
        return "plan_alignment_ready"
    if "plan-alignment" in parts and name.endswith(".md"):
        return "plan_alignment_decision"
    if "handoffs" in parts and name.endswith(".json"):
        return "coordination_handoff"
    if "coordination" in parts and name.endswith(".md"):
        return "coordination_decision"
    if name in {"portfolio.md", "project.md", "conventions.md"}:
        return "coordination"
    if "decisions" in parts and name.endswith(".md"):
        return "decision"
    if "reviews" in parts:
        if name == "review-packet.md":
            return "review_packet"
        if name == "review-plan.md":
            return "review_plan"
        return "review_artifact"
    if name.endswith(".md"):
        return "markdown_artifact"
    return "work_file"


def _node_artifacts(runtime_root: Path, bindings: dict[str, Any]) -> list[dict[str, Any]]:
    nodes_root = runtime_root / "nodes"
    if not nodes_root.is_dir():
        return []

    node_dirs: dict[Path, list[str]] = {}
    for address in bindings:
        node_dirs.setdefault(_node_dir(runtime_root, address).resolve(strict=False), []).append(address)
    ordered_dirs = sorted(node_dirs, key=lambda path: len(path.parts), reverse=True)

    artifacts: list[dict[str, Any]] = []
    for path in sorted(nodes_root.rglob("*")):
        if not path.is_file():
            continue
        owner_dir = None
        for candidate in ordered_dirs:
            try:
                path.relative_to(candidate)
            except ValueError:
                continue
            owner_dir = candidate
            break
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        artifacts.append(
            {
                "path": str(path),
                "relpath": _rel(path, runtime_root),
                "node_path": str(owner_dir.relative_to(nodes_root.resolve(strict=False)))
                if owner_dir is not None
                else None,
                "owner_node_addresses": sorted(node_dirs.get(owner_dir, [])) if owner_dir else [],
                "kind": _artifact_kind(path, runtime_root),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest() if data else None,
                "mtime_ns": path.stat().st_mtime_ns,
            }
        )
    return artifacts


def _session_history(raw_events: list[dict[str, Any]], bindings: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every known transcript incarnation, not just the current binding one."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in raw_events:
        delta = event.get("binding_delta") if isinstance(event.get("binding_delta"), dict) else {}
        node_address = event.get("node_address")
        transcript = delta.get("transcript_path")
        session_uuid = delta.get("session_uuid")
        if not node_address or not (transcript or session_uuid):
            continue
        key = (str(node_address), _transcript_key(transcript) or f"session:{session_uuid}")
        if key in seen:
            continue
        seen.add(key)
        binding = bindings.get(str(node_address)) if isinstance(bindings.get(str(node_address)), dict) else {}
        rows.append(
            {
                "node_address": str(node_address),
                "session_uuid": session_uuid,
                "transcript_path": transcript,
                "model_used": delta.get("model_used") or binding.get("model_used"),
                "level": binding.get("level"),
                "role_variant": binding.get("role_variant"),
                "event": event.get("event"),
                "seq": event.get("seq"),
                "ts": event.get("ts"),
                "current_binding": (
                    bool(transcript)
                    and _transcript_key(transcript) == _transcript_key(binding.get("transcript_path"))
                ),
                "source": "wal",
            }
        )

    for address, binding in sorted(bindings.items()):
        if not isinstance(binding, dict):
            continue
        transcript = binding.get("transcript_path")
        session_uuid = binding.get("session_uuid")
        if not (transcript or session_uuid):
            continue
        key = (str(address), _transcript_key(transcript) or f"session:{session_uuid}")
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "node_address": str(address),
                "session_uuid": session_uuid,
                "transcript_path": transcript,
                "model_used": binding.get("model_used"),
                "level": binding.get("level"),
                "role_variant": binding.get("role_variant"),
                "event": None,
                "seq": None,
                "ts": None,
                "current_binding": True,
                "source": "binding",
            }
        )
    return sorted(rows, key=lambda row: (row.get("seq") is None, row.get("seq") or 0, row["node_address"]))


def _transcript_stats(path: Path) -> dict[str, Any]:
    stats = {
        "transcript_path": str(path),
        "exists": path.is_file(),
        "line_count": 0,
        "assistant_rows": 0,
        "user_rows": 0,
        "codex_response_items": 0,
        "codex_message_items": 0,
        "codex_function_call_items": 0,
        "codex_function_call_output_items": 0,
        "codex_reasoning_items": 0,
        "codex_reasoning_summary_items": 0,
        "codex_reasoning_empty_summary_items": 0,
        "codex_reasoning_encrypted_items": 0,
        "codex_event_msg_rows": 0,
        "codex_token_count_events": 0,
        "codex_tool_result_events": 0,
        "claude_textual_tool_invocation_rows": 0,
    }
    if not path.is_file():
        return stats
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            stats["line_count"] += 1
            row = _parse_json_line(line)
            if not row:
                continue
            if row.get("type") == "assistant":
                stats["assistant_rows"] += 1
                if claude_textual_tool_invocation(row):
                    stats["claude_textual_tool_invocation_rows"] += 1
            elif row.get("type") == "user":
                stats["user_rows"] += 1
            elif row.get("type") == "response_item":
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                payload_type = str(payload.get("type") or "")
                stats["codex_response_items"] += 1
                if payload_type == "message":
                    stats["assistant_rows"] += 1
                    stats["codex_message_items"] += 1
                elif payload_type in {"function_call", "custom_tool_call"}:
                    stats["codex_function_call_items"] += 1
                elif payload_type in {"function_call_output", "custom_tool_call_output"}:
                    stats["codex_function_call_output_items"] += 1
                elif payload_type == "reasoning":
                    stats["codex_reasoning_items"] += 1
                    summary = payload.get("summary")
                    if isinstance(summary, list) and summary:
                        stats["codex_reasoning_summary_items"] += 1
                    else:
                        stats["codex_reasoning_empty_summary_items"] += 1
                    if payload.get("encrypted_content"):
                        stats["codex_reasoning_encrypted_items"] += 1
            elif row.get("type") == "event_msg":
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                payload_type = str(payload.get("type") or "")
                stats["codex_event_msg_rows"] += 1
                if payload_type == "user_message":
                    stats["user_rows"] += 1
                elif payload_type == "token_count":
                    stats["codex_token_count_events"] += 1
                elif payload_type.endswith("_end"):
                    stats["codex_tool_result_events"] += 1
    return stats


def _snippet(value: Any, *, limit: int = _SNIPPET_LIMIT) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            value = str(value)
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _tool_call_id(part: dict[str, Any]) -> str | None:
    for key in ("id", "tool_use_id", "call_id"):
        value = part.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _tool_input(part: dict[str, Any]) -> Any:
    if "input" in part:
        return part.get("input")
    payload = part.get("payload") if isinstance(part.get("payload"), dict) else part
    if isinstance(payload, dict):
        for key in ("arguments", "input", "args"):
            if key in payload:
                value = payload.get(key)
                if isinstance(value, str):
                    try:
                        return json.loads(value)
                    except json.JSONDecodeError:
                        return value
                return value
    return None


def _command_from_tool(tool_name: str | None, tool_input: Any) -> str | None:
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "script", "shell_command"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return value
        if tool_name in {"exec_command", "shell", "bash"}:
            for key in ("cmd", "command"):
                value = tool_input.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    if tool_name in _SHELL_TOOL_NAMES and isinstance(tool_input, str) and tool_input.strip():
        return tool_input
    return None


def _command_kind(command: str | None) -> str | None:
    if not command:
        return None
    for kind, pattern in _PROBE_PATTERNS:
        if pattern.search(command):
            return kind
    return "shell"


def _review_axis_from_address(address: str) -> str | None:
    pathish = address.split("#", 1)[0]
    parts = [part for part in pathish.split("/") if part]
    if "reviewers" in parts:
        idx = parts.index("reviewers")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    if address.endswith("#review"):
        return "gate-lead"
    return None


def _timing_bucket(line_no: int | None, line_count: int | None) -> str | None:
    if not line_no or not line_count:
        return None
    ratio = line_no / max(line_count, 1)
    if ratio <= 0.25:
        return "early"
    if ratio <= 0.75:
        return "middle"
    return "late"


def _codex_reasoning_summary(payload: dict[str, Any]) -> str | None:
    summary = payload.get("summary")
    if not isinstance(summary, list) or not summary:
        return None
    chunks: list[str] = []
    for item in summary:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("summary")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks) if chunks else None


def _codex_message_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if not isinstance(text, str):
                text = item.get("content")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks) if chunks else None


def _transcript_behavior(
    path: Path,
    session: dict[str, Any],
    *,
    line_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract a passive chronological digest and command/probe events."""
    if not path.is_file():
        return [], []

    address = str(session.get("node_address") or "")
    base = {
        "node_address": address,
        "level": session.get("level"),
        "role_variant": session.get("role_variant"),
        "model_used": session.get("model_used"),
        "review_axis": _review_axis_from_address(address),
        "transcript_path": str(path),
        "session_uuid": session.get("session_uuid"),
        "session_source_seq": session.get("seq"),
        "current_binding": session.get("current_binding"),
    }
    digest: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    pending_probes: dict[str, dict[str, Any]] = {}
    last_reasoning: str | None = None
    last_text: str | None = None
    event_index = 0
    command_index = 0

    def add_digest(line_no: int, event_type: str, summary: Any, **extra: Any) -> dict[str, Any]:
        nonlocal event_index
        event_index += 1
        row = {
            **base,
            "event_index": event_index,
            "jsonl_line": line_no,
            "timing_bucket": _timing_bucket(line_no, line_count),
            "event_type": event_type,
            "summary": _snippet(summary),
        }
        row.update({key: value for key, value in extra.items() if value is not None})
        digest.append(row)
        return row

    def add_tool_call(
        line_no: int,
        *,
        tool_name: str | None,
        tool_input: Any,
        call_id: str | None,
    ) -> None:
        nonlocal command_index
        command = _command_from_tool(tool_name, tool_input)
        kind = _command_kind(command)
        add_digest(
            line_no,
            "tool_call",
            command or tool_input or tool_name,
            tool_name=tool_name,
            command_kind=kind,
        )
        if kind is None or kind == "shell":
            return
        command_index += 1
        probe = {
            **base,
            "command_index": command_index,
            "jsonl_line": line_no,
            "timing_bucket": _timing_bucket(line_no, line_count),
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "command_kind": kind,
            "command": _snippet(command, limit=_COMMAND_LIMIT),
            "nearest_reasoning_summary": last_reasoning,
            "nearest_assistant_text": last_text,
            "result_excerpt": None,
            "result_line": None,
            "result_is_error": None,
        }
        probes.append(probe)
        if call_id:
            pending_probes[call_id] = probe

    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            row = _parse_json_line(line)
            if not row:
                continue
            row_type = row.get("type")
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            content = message.get("content")
            if row_type == "assistant" and isinstance(content, list):
                if claude_textual_tool_invocation(row):
                    add_digest(line_no, "malformed_tool_invocation_text", _claude_message_text(row))
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if part_type in {"thinking", "redacted_thinking"}:
                        text = part.get("thinking")
                        if not isinstance(text, str) and isinstance(part.get("text"), str):
                            text = part.get("text")
                        if isinstance(text, str) and text.strip():
                            last_reasoning = _snippet(text)
                            add_digest(line_no, "reasoning_summary", text)
                    elif part_type == "text":
                        text = part.get("text")
                        if isinstance(text, str) and text.strip():
                            last_text = _snippet(text)
                            add_digest(line_no, "assistant_text", text)
                    elif part_type == "tool_use":
                        add_tool_call(
                            line_no,
                            tool_name=part.get("name"),
                            tool_input=_tool_input(part),
                            call_id=_tool_call_id(part),
                        )
            elif row_type == "user" and isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "tool_result":
                        continue
                    call_id = _tool_call_id(part)
                    result = _snippet(part.get("content"), limit=_COMMAND_LIMIT)
                    add_digest(
                        line_no,
                        "tool_result",
                        result,
                        tool_call_id=call_id,
                        result_is_error=part.get("is_error"),
                    )
                    if call_id and call_id in pending_probes:
                        pending_probes[call_id]["result_excerpt"] = result
                        pending_probes[call_id]["result_line"] = line_no
                        pending_probes[call_id]["result_is_error"] = bool(part.get("is_error"))
            elif row_type == "response_item":
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                payload_type = payload.get("type")
                if payload_type == "reasoning":
                    text = _codex_reasoning_summary(payload)
                    if text:
                        last_reasoning = _snippet(text)
                        add_digest(line_no, "reasoning_summary", text)
                elif payload_type == "message":
                    text = _codex_message_text(payload)
                    if text:
                        last_text = _snippet(text)
                        add_digest(line_no, "assistant_text", text)
                elif payload_type in {"function_call", "custom_tool_call"}:
                    add_tool_call(
                        line_no,
                        tool_name=payload.get("name"),
                        tool_input=_tool_input(payload),
                        call_id=payload.get("call_id"),
                    )
                elif payload_type in {"function_call_output", "custom_tool_call_output"}:
                    call_id = payload.get("call_id")
                    result = _snippet(payload.get("output"), limit=_COMMAND_LIMIT)
                    add_digest(line_no, "tool_result", result, tool_call_id=call_id)
                    if isinstance(call_id, str) and call_id in pending_probes:
                        pending_probes[call_id]["result_excerpt"] = result
                        pending_probes[call_id]["result_line"] = line_no
                        pending_probes[call_id]["result_is_error"] = False
    return digest, probes


def _reasoning_summaries(
    bindings: dict[str, Any],
    session_history: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    extractor = _load_thinking_extractor()
    sessions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for session in session_history:
        address = str(session.get("node_address") or "")
        key = _transcript_key(session.get("transcript_path"))
        if not address or not key:
            continue
        identity = (address, key)
        if identity in seen:
            continue
        seen.add(identity)
        sessions.append(session)

    summaries: list[dict[str, Any]] = []
    stats_rows: list[dict[str, Any]] = []
    for session in sorted(sessions, key=lambda row: (row["node_address"], row.get("seq") or 0, row.get("transcript_path") or "")):
        address = str(session["node_address"])
        binding = bindings.get(address) if isinstance(bindings.get(address), dict) else {}
        key = _transcript_key(session.get("transcript_path"))
        if key is None:
            continue
        path = Path(key)
        if not path.is_file():
            stats_rows.append(
                {
                    "node_address": address,
                    "transcript_path": str(path),
                    "session_uuid": session.get("session_uuid"),
                    "session_source_seq": session.get("seq"),
                    "current_binding": session.get("current_binding"),
                    "exists": False,
                    "populated_summary_count": 0,
                }
            )
            continue
        try:
            extracted, stats = extractor.extract(path)
        except SystemExit as exc:
            stats_rows.append(
                {
                    "node_address": address,
                    "transcript_path": str(path),
                    "session_uuid": session.get("session_uuid"),
                    "session_source_seq": session.get("seq"),
                    "current_binding": session.get("current_binding"),
                    "exists": True,
                    "error": str(exc),
                    "populated_summary_count": 0,
                }
            )
            continue
        stats = dict(stats)
        stats.update(
            {
                "node_address": address,
                "session_uuid": session.get("session_uuid"),
                "session_source_seq": session.get("seq"),
                "current_binding": session.get("current_binding"),
                "level": session.get("level") or binding.get("level"),
                "role_variant": session.get("role_variant") or binding.get("role_variant"),
                "model_used": session.get("model_used") or binding.get("model_used"),
            }
        )
        stats_rows.append(stats)
        for summary in extracted:
            row = asdict(summary)
            row.update(
                {
                    "node_address": address,
                    "session_uuid": session.get("session_uuid"),
                    "session_source_seq": session.get("seq"),
                    "current_binding": session.get("current_binding"),
                    "level": session.get("level") or binding.get("level"),
                    "role_variant": session.get("role_variant") or binding.get("role_variant"),
                    "model_used": session.get("model_used") or binding.get("model_used"),
                    "gate_state": binding.get("gate_state"),
                    "terminal_signal": binding.get("terminal_signal"),
                }
            )
            summaries.append(row)
    return summaries, stats_rows


def _parse_trace_stanza(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    body = raw.strip()
    serves: list[str] = []
    match = _SERVES_LIST.search(body)
    if match:
        serves = [token.strip() for token in match.group(1).split(",") if token.strip()]
        body = body[: match.start()] + "serves: __SERVES__" + body[match.end():]
    fields: dict[str, Any] = {}
    for pair in body.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            return None, f"unparseable field {pair!r}"
        key, value = pair.split(":", 1)
        fields[key.strip()] = value.strip()
    if "serves" in fields:
        fields["serves"] = serves
    extra = set(fields) - _TRACE_FIELDS
    if extra:
        return None, f"non-canonical field(s) {sorted(extra)}"
    if not fields.get("id"):
        return None, "missing required field 'id'"
    if not fields.get("kind"):
        return None, "missing required field 'kind'"
    return fields, None


def _line_no_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _node_dir_index(runtime_root: Path, bindings: dict[str, Any]) -> tuple[dict[Path, list[str]], list[Path]]:
    node_dirs: dict[Path, list[str]] = {}
    for address in bindings:
        node_dirs.setdefault(_node_dir(runtime_root, address).resolve(strict=False), []).append(address)
    ordered_dirs = sorted(node_dirs, key=lambda path: len(path.parts), reverse=True)
    return node_dirs, ordered_dirs


def _owner_for_path(path: Path, nodes_root: Path, node_dirs: dict[Path, list[str]], ordered_dirs: list[Path]) -> tuple[str | None, list[str]]:
    owner_dir = None
    for candidate in ordered_dirs:
        try:
            path.resolve(strict=False).relative_to(candidate)
        except ValueError:
            continue
        owner_dir = candidate
        break
    if owner_dir is None:
        return None, []
    try:
        node_path = str(owner_dir.relative_to(nodes_root.resolve(strict=False)))
    except ValueError:
        node_path = None
    return node_path, sorted(node_dirs.get(owner_dir, []))


def _requirement_evidence(runtime_root: Path, bindings: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes_root = runtime_root / "nodes"
    if not nodes_root.is_dir():
        return [], []
    node_dirs, ordered_dirs = _node_dir_index(runtime_root, bindings)
    trace_rows: list[dict[str, Any]] = []
    reference_rows: list[dict[str, Any]] = []
    evidence_paths = [
        path
        for pattern in ("*.md", "*.py")
        for path in nodes_root.rglob(pattern)
        if not any(part.startswith(".") for part in path.relative_to(nodes_root).parts)
    ]
    for path in sorted(evidence_paths):
        # Hidden node markdown is harness-authored metadata such as .identity-prompt.md,
        # not producer-authored fidelity evidence for trace/reference scoring.
        if path.name.startswith("."):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        node_path, addresses = _owner_for_path(path, nodes_root, node_dirs, ordered_dirs)
        relpath = _rel(path, runtime_root)
        for match in _TRACE_STANZA.finditer(text):
            fields, error = _parse_trace_stanza(match.group(1))
            row = {
                "kind": "trace_stanza",
                "path": str(path),
                "relpath": relpath,
                "node_path": node_path,
                "owner_node_addresses": addresses,
                "line": _line_no_for_offset(text, match.start()),
                "parse_error": error,
            }
            if fields:
                row.update({f"trace_{key}": value for key, value in fields.items()})
            trace_rows.append(row)
        ids = sorted(set(_REQ_ID.findall(text)))
        if ids:
            reference_rows.append(
                {
                    "kind": "requirement_reference",
                    "path": str(path),
                    "relpath": relpath,
                    "artifact_kind": _artifact_kind(path, runtime_root),
                    "node_path": node_path,
                    "owner_node_addresses": addresses,
                    "requirement_ids": ids,
                    "reference_count": len(_REQ_ID.findall(text)),
                    "has_trace_stanza": any(row["relpath"] == relpath for row in trace_rows),
                }
            )
    return trace_rows, reference_rows


def _return_contract_defects(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "return_contract_failed":
            continue
        delta = event.get("binding_delta") if isinstance(event.get("binding_delta"), dict) else {}
        rows.append(
            {
                "seq": event.get("seq"),
                "ts": event.get("ts"),
                "node_address": event.get("node_address"),
                "signal_artifact_seen_at": delta.get("signal_artifact_seen_at"),
                "agent_signal_ts": delta.get("agent_signal_ts"),
                "defects": list(delta.get("defects") or []),
                "summary": event.get("summary"),
            }
        )
    return rows


def _transcript_runtime_failures(session_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for session in session_history:
        node_address = str(session.get("node_address") or "")
        transcript = _transcript_key(session.get("transcript_path"))
        if not node_address or not transcript:
            continue
        identity = (node_address, transcript)
        if identity in seen:
            continue
        seen.add(identity)
        path = Path(transcript)
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    row = _parse_json_line(line)
                    if row is None:
                        continue
                    failure = runtime_failure_from_transcript_event(row)
                    if not failure:
                        continue
                    rows.append(
                        {
                            "seq": session.get("seq"),
                            "ts": failure.get("timestamp") or session.get("ts"),
                            "node_address": node_address,
                            "event": "transcript_runtime_failure",
                            "failure_class": failure.get("failure_class"),
                            "runtime": failure.get("runtime"),
                            "error_code": failure.get("error_code"),
                            "summary": failure.get("summary"),
                            "transcript_path": transcript,
                            "transcript_line": line_no,
                            "session_uuid": session.get("session_uuid"),
                            "session_source_seq": session.get("seq"),
                            "current_binding": session.get("current_binding"),
                        }
                    )
        except OSError:
            continue
    return rows


# A gap larger than this between consecutive failure rows on one seat starts a NEW incident.
# Chosen from the measured shape of the real storms (2026-06-17 run: 24 rows over ~9 min;
# 2026-06-19 r6: 10 rows over ~54 min with intra-storm gaps well under 15 min).
_INCIDENT_GAP_SECONDS = 15 * 60


def _runtime_failure_incidents(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cluster per-row runtime failures into INCIDENTS (seat + failure class + time window).

    The per-row count materially misleads: the delivered 2026-06-17 run reported
    "runtime_failure_count: 28" when the truth was TWO transient provider-overload storms on
    two seats (~9 min total cost). Readers reason about incidents; the rows stay available
    as the detail underneath."""
    import datetime as _dt

    def _parse_ts(value: Any):
        try:
            return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("node_address") or ""),
            str(row.get("failure_class") or ""),
            str(row.get("runtime") or ""),
        )
        groups.setdefault(key, []).append(row)

    incidents: list[dict[str, Any]] = []
    for (node_address, failure_class, runtime), group in sorted(groups.items()):
        dated = sorted(
            (r for r in group if _parse_ts(r.get("ts")) is not None),
            key=lambda r: _parse_ts(r.get("ts")),
        )
        undated = [r for r in group if _parse_ts(r.get("ts")) is None]
        clusters: list[list[dict[str, Any]]] = []
        for row in dated:
            ts = _parse_ts(row.get("ts"))
            if (
                clusters
                and (ts - _parse_ts(clusters[-1][-1].get("ts"))).total_seconds()
                <= _INCIDENT_GAP_SECONDS
            ):
                clusters[-1].append(row)
            else:
                clusters.append([row])
        if undated:
            clusters.append(undated)
        for cluster in clusters:
            incidents.append(
                {
                    "node_address": node_address,
                    "failure_class": failure_class,
                    "runtime": runtime or None,
                    "row_count": len(cluster),
                    "error_codes": sorted(
                        {str(r.get("error_code")) for r in cluster if r.get("error_code")}
                    ),
                    "first_ts": cluster[0].get("ts"),
                    "last_ts": cluster[-1].get("ts"),
                }
            )
    return incidents


def _blocked_input_incidents(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join each write-ahead stall intent with its observed action and recovery edges."""
    incidents: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in sorted(
        events,
        key=lambda row: (row.get("seq") is None, row.get("seq") or 0),
    ):
        kind = event.get("event")
        if kind not in {"seat_stalled", "seat_stall_actioned", "seat_stall_recovered"}:
            continue
        delta = (
            event.get("binding_delta")
            if isinstance(event.get("binding_delta"), dict)
            else {}
        )
        incident_id = str(delta.get("seat_stall_incident_id") or "")
        if not incident_id:
            continue
        if kind == "seat_stalled":
            if incident_id not in incidents:
                order.append(incident_id)
            incidents[incident_id] = {
                "incident_id": incident_id,
                "node_address": event.get("node_address"),
                "started_at": delta.get("seat_stall_since") or event.get("ts"),
                "classification": delta.get("seat_stall_classification"),
                "silent_seconds": delta.get("seat_stall_silent_seconds"),
                "pane_excerpt": delta.get("seat_stall_pane_excerpt"),
                "prompt_signature": delta.get("seat_stall_prompt_signature"),
                "cancel_status": delta.get("seat_stall_cancel_status"),
                "retriggered": bool(delta.get("seat_stall_retriggered")),
                "escalated": bool(delta.get("seat_stall_escalated")),
                "actioned_at": None,
                "recovered_at": None,
            }
            continue
        incident = incidents.get(incident_id)
        if incident is None:
            continue
        if kind == "seat_stall_actioned":
            incident["cancel_status"] = delta.get("seat_stall_cancel_status")
            incident["actioned_at"] = (
                delta.get("seat_stall_actioned_at") or event.get("ts")
            )
        elif kind == "seat_stall_recovered":
            incident["recovered_at"] = (
                delta.get("seat_stall_recovered_at") or event.get("ts")
            )
    return [incidents[incident_id] for incident_id in order]


def _runtime_failures(
    events: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    transcript_failures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, str, str | None]] = set()
    seen_node_failure: set[tuple[str, str | None]] = set()
    terminal_events = _terminal_event_by_node(events)
    for event in events:
        delta = event.get("binding_delta") if isinstance(event.get("binding_delta"), dict) else {}
        runtime_failure = delta.get("runtime_failure") if isinstance(delta.get("runtime_failure"), dict) else {}
        failure_class = delta.get("failure_class") or runtime_failure.get("failure_class")
        event_name = event.get("event")
        if (
            event_name not in {"watchdog_runtime_failure", "spawn_failed"}
            and failure_class not in RUNTIME_FAILURE_CLASSES
        ):
            continue
        key = (event.get("seq"), str(event.get("node_address") or ""), failure_class)
        if key in seen:
            continue
        seen.add(key)
        seen_node_failure.add((str(event.get("node_address") or ""), failure_class))
        row = {
            "seq": event.get("seq"),
            "ts": event.get("ts"),
            "node_address": event.get("node_address"),
            "event": event.get("event"),
            "failure_class": failure_class,
            "runtime": runtime_failure.get("runtime"),
            "error_code": runtime_failure.get("error_code"),
            "summary": runtime_failure.get("summary") or event.get("summary"),
            "transcript_line": runtime_failure.get("line"),
        }
        for key in ("failure_reason", "model_used", "claim_released"):
            if key in delta:
                row[key] = delta.get(key)
        rows.append(row)

    for failure in transcript_failures:
        failure_class = failure.get("failure_class")
        if failure_class not in RUNTIME_FAILURE_CLASSES:
            continue
        key = (
            failure.get("transcript_path"),
            failure.get("transcript_line"),
            str(failure.get("node_address") or ""),
            failure_class,
        )
        if key in seen:
            continue
        seen.add(key)
        row = dict(failure)
        post_terminal = _post_terminal_failure_metadata(row, terminal_events)
        if post_terminal:
            row.update(post_terminal)
        else:
            seen_node_failure.add((str(failure.get("node_address") or ""), failure_class))
        rows.append(row)

    for node in nodes:
        runtime_failure = (
            node.get("runtime_failure") if isinstance(node.get("runtime_failure"), dict) else {}
        )
        failure_class = node.get("failure_class") or runtime_failure.get("failure_class")
        if failure_class not in RUNTIME_FAILURE_CLASSES:
            continue
        if (str(node.get("node_address") or ""), failure_class) in seen_node_failure:
            continue
        key = (None, str(node.get("node_address") or ""), failure_class)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "seq": None,
                "ts": None,
                "node_address": node.get("node_address"),
                "event": "binding_runtime_failure",
                "failure_class": failure_class,
                "runtime": runtime_failure.get("runtime"),
                "error_code": runtime_failure.get("error_code"),
                "summary": runtime_failure.get("summary") or node.get("terminal_note"),
                "transcript_line": runtime_failure.get("line"),
            }
        )
    return rows


def _terminal_event_by_node(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    terminal: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event") not in {"signal_DONE", "signal_FAILED"}:
            continue
        node = str(event.get("node_address") or "")
        if not node:
            continue
        ts = _parse_iso_ts(event.get("ts"))
        if ts is None:
            continue
        prior = terminal.get(node)
        if prior is None or ts < prior["parsed_ts"]:
            terminal[node] = {
                "seq": event.get("seq"),
                "ts": event.get("ts"),
                "event": event.get("event"),
                "parsed_ts": ts,
            }
    return terminal


def _post_terminal_failure_metadata(
    failure: dict[str, Any],
    terminal_events: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    node = str(failure.get("node_address") or "")
    terminal = terminal_events.get(node)
    if terminal is None:
        return {}
    failure_ts = _parse_iso_ts(failure.get("ts"))
    if failure_ts is None or failure_ts < terminal["parsed_ts"]:
        return {}
    return {
        "contaminates": False,
        "non_contaminating_reason": "post_terminal_transcript_noise",
        "terminal_event": terminal["event"],
        "terminal_event_seq": terminal["seq"],
        "terminal_event_ts": terminal["ts"],
    }


def _parse_iso_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_TERMINAL_STATES = {"done", "failed", "dead", "collapsed", "cancelled"}


def _pid_state(runtime_json: dict[str, Any]) -> dict[str, Any]:
    pid = runtime_json.get("pid")
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return {"pid": pid, "state": "missing"}
    if pid_int <= 0:
        return {"pid": pid_int, "state": "invalid"}
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return {"pid": pid_int, "state": "absent"}
    except PermissionError:
        return {"pid": pid_int, "state": "present_unowned"}
    except OSError as exc:
        return {"pid": pid_int, "state": "unknown", "error": str(exc)}
    return {"pid": pid_int, "state": "present"}


def _tmux_state(runtime_json: dict[str, Any]) -> dict[str, Any]:
    build_id = str(runtime_json.get("build_id") or "build-local")
    socket = _commissioning._tmux_socket_name(build_id)
    try:
        proc = subprocess.run(
            ["tmux", "-L", socket, "ls"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return {"state": "unknown", "socket": socket, "error": "tmux_not_found", "sessions": []}
    except OSError as exc:
        return {"state": "unknown", "socket": socket, "error": str(exc), "sessions": []}

    sessions = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode == 0:
        return {"state": "present" if sessions else "none", "socket": socket, "sessions": sessions}

    stderr = proc.stderr.strip()
    no_server = "no server running" in stderr.lower() or "failed to connect" in stderr.lower()
    if no_server:
        return {"state": "none", "socket": socket, "sessions": [], "error": stderr or None}
    return {"state": "unknown", "socket": socket, "sessions": sessions, "error": stderr or None}


def _run_lifecycle(runtime_json: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    nonterminal = sorted(
        str(node.get("node_address"))
        for node in nodes
        if node.get("node_address") and str(node.get("state") or "").lower() not in _TERMINAL_STATES
    )
    pid = _pid_state(runtime_json)
    tmux = _tmux_state(runtime_json)
    stopped = bool(nonterminal) and pid.get("state") == "absent"
    reason = None
    if stopped:
        if tmux.get("state") == "present":
            reason = "runtime pid absent, harness tmux sessions still present, and nonterminal bindings remain"
        elif tmux.get("state") == "none":
            reason = "runtime pid absent, no harness tmux sessions, and nonterminal bindings remain"
        else:
            reason = "runtime pid absent, tmux state unknown, and nonterminal bindings remain"
    return {
        "state": "stopped_runtime" if stopped else "active_or_complete",
        "reason": reason,
        "pid": pid,
        "tmux": tmux,
        "nonterminal_node_addresses": nonterminal,
        "nonterminal_node_count": len(nonterminal),
    }


def _is_codex_fact(*, model_used: Any = None, codex_seat_id: Any = None) -> bool:
    if codex_seat_id:
        return True
    return "codex" in str(model_used or "").lower()


def _codex_pressure(
    *,
    events: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    runtime_failures: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize passive Codex load/auth pressure without imposing any admission policy."""
    current_active = [
        node for node in nodes
        if _is_codex_fact(
            model_used=node.get("model_used"),
            codex_seat_id=node.get("codex_seat_id"),
        )
        and str(node.get("state") or "").lower() not in _TERMINAL_STATES
    ]
    seat_ids = sorted({
        str(node.get("codex_seat_id"))
        for node in nodes
        if node.get("codex_seat_id")
    })
    auth_versions = sorted({
        str(node.get("auth_version"))
        for node in nodes
        if node.get("auth_version")
    })

    active: set[str] = set()
    active_models: dict[str, str] = {}
    timeline: list[dict[str, Any]] = []
    max_active = 0
    for event in sorted(events, key=lambda row: (row.get("seq") is None, row.get("seq") or 0)):
        node_address = str(event.get("node_address") or "")
        if not node_address:
            continue
        delta = event.get("binding_delta") if isinstance(event.get("binding_delta"), dict) else {}
        is_codex = _is_codex_fact(
            model_used=delta.get("model_used") or active_models.get(node_address),
            codex_seat_id=delta.get("codex_seat_id"),
        )
        changed = False
        if event.get("event") == "spawn_open" and is_codex:
            active.add(node_address)
            if delta.get("model_used"):
                active_models[node_address] = str(delta.get("model_used"))
            changed = True
        terminal = str(event.get("to_state") or "").lower() in _TERMINAL_STATES
        if node_address in active and (terminal or delta.get("in_flight_release")):
            active.discard(node_address)
            active_models.pop(node_address, None)
            changed = True
        if changed:
            max_active = max(max_active, len(active))
            timeline.append(
                {
                    "seq": event.get("seq"),
                    "ts": event.get("ts"),
                    "node_address": node_address,
                    "event": event.get("event"),
                    "active_count": len(active),
                }
            )

    failure_counts: dict[str, int] = {}
    for failure in runtime_failures:
        if failure.get("runtime") != "codex" and "codex" not in str(failure.get("summary") or "").lower():
            continue
        failure_class = str(failure.get("failure_class") or "unknown")
        failure_counts[failure_class] = failure_counts.get(failure_class, 0) + 1

    return {
        "current_active_count": len(current_active),
        "current_active_nodes": [node.get("node_address") for node in current_active],
        "known_seat_ids": seat_ids,
        "auth_versions": auth_versions,
        "timeline": timeline,
        "max_active_count": max(max_active, len(current_active)),
        "runtime_failure_counts": failure_counts,
    }


def build_index(runtime_root: Path) -> dict[str, Any]:
    runtime_root = Path(runtime_root)
    runtime_json = _read_json(runtime_root / "runtime.json")
    bindings = _read_json(runtime_root / "binding-ledger.json")
    if not isinstance(bindings, dict):
        bindings = {}
    raw_events = _read_json_rows(runtime_root / "run-ledger.jsonl")
    events = [_event_summary(row) for row in raw_events]
    nodes = [
        _binding_summary(runtime_root, address, binding)
        for address, binding in sorted(bindings.items())
        if isinstance(binding, dict)
    ]

    session_history = _session_history(raw_events, bindings)
    transcript_rows = []
    transcript_digest_events: list[dict[str, Any]] = []
    transcript_probe_events: list[dict[str, Any]] = []
    seen_transcripts: set[tuple[str, str]] = set()
    for session in session_history:
        transcript = session.get("transcript_path")
        if transcript:
            key = (str(session.get("node_address")), _transcript_key(transcript) or str(transcript))
            if key in seen_transcripts:
                continue
            seen_transcripts.add(key)
            stats = _transcript_stats(Path(transcript))
            stats.update(
                {
                    "node_address": session["node_address"],
                    "session_uuid": session.get("session_uuid"),
                    "session_source_seq": session.get("seq"),
                    "current_binding": session.get("current_binding"),
                    "level": session.get("level"),
                    "role_variant": session.get("role_variant"),
                    "model_used": session.get("model_used"),
                }
            )
            digest_events, probe_events = _transcript_behavior(
                Path(transcript),
                session,
                line_count=int(stats.get("line_count") or 0),
            )
            stats["transcript_digest_events"] = len(digest_events)
            stats["transcript_probe_events"] = len(probe_events)
            transcript_digest_events.extend(digest_events)
            transcript_probe_events.extend(probe_events)
            transcript_rows.append(stats)

    summaries, summary_stats = _reasoning_summaries(bindings, session_history)
    gate_packets = _gate_packets(runtime_root)
    inbox_rows = _inbox_rows(runtime_root)
    artifacts = _node_artifacts(runtime_root, bindings)
    trace_stanzas, requirement_references = _requirement_evidence(runtime_root, bindings)
    return_contract_defects = _return_contract_defects(raw_events)
    transcript_runtime_failures = _transcript_runtime_failures(session_history)
    runtime_failures = _runtime_failures(events, nodes, transcript_runtime_failures)
    runtime_failure_incidents = _runtime_failure_incidents(runtime_failures)
    blocked_input_incidents = _blocked_input_incidents(raw_events)
    run_lifecycle = _run_lifecycle(runtime_json, nodes)
    codex_pressure = _codex_pressure(
        events=events,
        nodes=nodes,
        runtime_failures=runtime_failures,
    )

    return {
        "schema_version": 9,
        "observer_effect": "read-only runtime artifact parse; no pane, inbox, ledger, or transcript mutation",
        "runtime_root": str(runtime_root),
        "runtime": runtime_json,
        "counts": {
            "nodes": len(nodes),
            "events": len(events),
            "inbox_rows": len(inbox_rows),
            "gate_packets": len(gate_packets),
            "transcripts": len(transcript_rows),
            "transcript_digest_events": len(transcript_digest_events),
            "transcript_probe_events": len(transcript_probe_events),
            "reasoning_summaries": len(summaries),
            "artifacts": len(artifacts),
            "trace_stanzas": len(trace_stanzas),
            "requirement_reference_files": len(requirement_references),
            "return_contract_defects": len(return_contract_defects),
            "runtime_failures": len(runtime_failures),
            "runtime_failure_incidents": len(runtime_failure_incidents),
            "blocked_input_incidents": len(blocked_input_incidents),
            "codex_pressure_timeline": len(codex_pressure["timeline"]),
        },
        "run_lifecycle": run_lifecycle,
        "infrastructure_pressure": {
            "codex": codex_pressure,
        },
        "nodes": nodes,
        "events": events,
        "session_history": session_history,
        "inbox_rows": inbox_rows,
        "artifacts": artifacts,
        "gate_packets": gate_packets,
        "transcripts": transcript_rows,
        "transcript_digest_events": transcript_digest_events,
        "transcript_probe_events": transcript_probe_events,
        "reasoning_summary_stats": summary_stats,
        "reasoning_summaries": summaries,
        "trace_stanzas": trace_stanzas,
        "requirement_references": requirement_references,
        "return_contract_defects": return_contract_defects,
        "runtime_failures": runtime_failures,
        "runtime_failure_incidents": runtime_failure_incidents,
        "blocked_input_incidents": blocked_input_incidents,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_root", help="harness runtime root to index")
    parser.add_argument("--output", "-o", help="write JSON to this path instead of stdout")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    args = parser.parse_args(argv)

    payload = build_index(Path(args.runtime_root))
    text = json.dumps(payload, ensure_ascii=False, indent=None if args.compact else 2)
    text += "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
