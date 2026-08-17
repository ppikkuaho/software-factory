"""Shared runtime-failure taxonomy and transcript classifiers."""

from __future__ import annotations

from typing import Any


AUTH_EXPIRED = "auth_expired"
AUTH_RATE_LIMITED = "auth_rate_limited"
AUTH_LEASE_UNAVAILABLE = "auth_lease_unavailable"
AUTH_REFRESH_CONTENDED = "auth_refresh_contended"
AUTH_REFRESH_FAILED = "auth_refresh_failed"
RUNTIME_PROVIDER_ERROR = "runtime_provider_error"
TOOL_CALL_PARSE_FAILED = "tool_call_parse_failed"

RUNTIME_FAILURE_CLASSES = frozenset(
    {
        AUTH_EXPIRED,
        AUTH_RATE_LIMITED,
        AUTH_LEASE_UNAVAILABLE,
        AUTH_REFRESH_CONTENDED,
        AUTH_REFRESH_FAILED,
        RUNTIME_PROVIDER_ERROR,
        TOOL_CALL_PARSE_FAILED,
    }
)

CODEX_RATE_LIMIT_ERROR_CODES = frozenset({"rate_limited", "rate_limit_exceeded"})


def runtime_failure_class_from_codex_error(message: str, error_code: str) -> str | None:
    lowered = str(message or "").lower()
    code = str(error_code or "")
    if code == "unauthorized" or ("access token" in lowered and "refresh token" in lowered):
        return AUTH_EXPIRED
    if code in CODEX_RATE_LIMIT_ERROR_CODES or (
        "rate limit" in lowered or "too many requests" in lowered or "429" in lowered
    ):
        return AUTH_RATE_LIMITED
    return None


def codex_error_payload(row: dict[str, Any]) -> tuple[str, str, Any] | None:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    if row.get("type") != "event_msg" or payload.get("type") != "error":
        return None
    return (
        str(payload.get("message") or ""),
        str(payload.get("codex_error_info") or ""),
        row.get("timestamp"),
    )


def runtime_failure_from_codex_event(row: dict[str, Any]) -> dict[str, Any] | None:
    payload = codex_error_payload(row)
    if payload is None:
        return None
    message, code, timestamp = payload
    failure_class = runtime_failure_class_from_codex_error(message, code)
    if failure_class == AUTH_EXPIRED:
        return {
            "failure_class": AUTH_EXPIRED,
            "runtime": "codex",
            "error_code": code or "unauthorized",
            "summary": "Codex OAuth refresh failed in transcript",
            "timestamp": timestamp,
        }
    if failure_class == AUTH_RATE_LIMITED:
        return {
            "failure_class": AUTH_RATE_LIMITED,
            "runtime": "codex",
            "error_code": code or "rate_limited",
            "summary": "Codex runtime reported rate limiting in transcript",
            "timestamp": timestamp,
        }
    return None


def claude_api_error_payload(row: dict[str, Any]) -> tuple[str, int | None, str, Any] | None:
    """Return the Claude Code API-error payload shape, if ``row`` is one.

    Claude Code persists provider/API failures as system rows, for example:
    ``{"type":"system","subtype":"api_error","level":"error","error":{"status":529,...}}``.
    Those rows grow the transcript but are not evidence that a wake was consumed.
    """
    if row.get("type") != "system" or row.get("subtype") != "api_error":
        return None
    error = row.get("error") if isinstance(row.get("error"), dict) else {}
    status_raw = error.get("status")
    try:
        status = int(status_raw) if status_raw is not None else None
    except (TypeError, ValueError):
        status = None
    return (
        str(error.get("message") or ""),
        status,
        str(error.get("formatted") or ""),
        row.get("timestamp"),
    )


def runtime_failure_from_claude_event(row: dict[str, Any]) -> dict[str, Any] | None:
    payload = claude_api_error_payload(row)
    if payload is not None:
        _message, status, _formatted, timestamp = payload
        return {
            "failure_class": RUNTIME_PROVIDER_ERROR,
            "runtime": "claude_code",
            "error_code": str(status) if status is not None else "api_error",
            "summary": "Claude Code provider/API error in transcript",
            "timestamp": timestamp,
        }

    if claude_tool_call_parse_failed(row):
        return {
            "failure_class": TOOL_CALL_PARSE_FAILED,
            "runtime": "claude_code",
            "error_code": TOOL_CALL_PARSE_FAILED,
            "summary": "Claude Code could not parse the model tool call after retry",
            "timestamp": row.get("timestamp"),
        }
    return None


def _claude_message_text(row: dict[str, Any]) -> str:
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks)


def claude_tool_call_parse_failed(row: dict[str, Any]) -> bool:
    """Return True for Claude Code's terminal tool-call parse failure row.

    This is passive observability only. The row means Claude Code could not
    parse the model-authored tool invocation even after its automatic retry, so
    a transcript can grow while the agent has not successfully acted on the
    previous wake or task.
    """
    if row.get("type") != "assistant":
        return False
    text = _claude_message_text(row).lower()
    return (
        "tool call could not be parsed" in text
        and "retry also failed" in text
    )


def claude_textual_tool_invocation(row: dict[str, Any]) -> bool:
    """Return True when Claude emitted a tool call as plain text.

    Claude Code normally persists tool requests as structured ``tool_use``
    blocks. In live runs we have also observed assistant text containing the
    XML-ish ``<invoke name=...>`` shape, sometimes after a malformed retry and
    sometimes as an ``end_turn``. That row grows the transcript but does not
    prove the agent actually executed the intended tool or consumed an inbox
    wake, so wake acknowledgement must treat it as non-progress.
    """
    if row.get("type") != "assistant":
        return False
    text = _claude_message_text(row).lower()
    return (
        "<invoke name=" in text
        and "<parameter name=" in text
        and "</invoke>" in text
    )


def runtime_failure_from_transcript_event(row: dict[str, Any]) -> dict[str, Any] | None:
    """Classify runtime/provider failures across transcript formats."""
    return runtime_failure_from_codex_event(row) or runtime_failure_from_claude_event(row)


def transcript_event_is_agent_progress(row: dict[str, Any]) -> bool:
    """Return True for transcript rows that prove the model/agent actually advanced a turn.

    This intentionally excludes user rows, metadata rows, and provider error rows. A wake typed into
    a pane can create transcript bytes without the agent ever processing the durable inbox pointer;
    the ack path needs positive evidence of assistant/agent progress.
    """
    if not isinstance(row, dict):
        return False
    if runtime_failure_from_transcript_event(row):
        return False
    if claude_textual_tool_invocation(row):
        return False
    if row.get("type") == "assistant":
        message = row.get("message")
        if isinstance(message, dict):
            return message.get("role") in {None, "assistant"}
        return True
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    if row.get("type") == "response_item" and payload.get("type") == "message":
        return payload.get("role") == "assistant"
    if row.get("type") == "event_msg":
        if payload.get("type") in {"agent_message", "assistant_message"}:
            return True
        if payload.get("type") == "task_complete" and payload.get("last_agent_message"):
            return True
    return False
