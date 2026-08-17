from harnessd import runtime_failures


def _row(message, code=""):
    return {
        "type": "event_msg",
        "timestamp": "2026-06-16T00:00:00Z",
        "payload": {
            "type": "error",
            "message": message,
            "codex_error_info": code,
        },
    }


def _claude_api_error(status=529, formatted="529 Overloaded"):
    return {
        "type": "system",
        "subtype": "api_error",
        "level": "error",
        "timestamp": "2026-06-17T07:16:34.196Z",
        "error": {
            "message": f'{status} {{"type":"error","error":{{"type":"overloaded_error","message":"Overloaded"}}}}',
            "status": status,
            "requestId": "req_test",
            "formatted": formatted,
            "connection": None,
            "isNetworkDown": False,
            "rateLimits": None,
        },
    }


def _claude_tool_call_parse_failure():
    return {
        "type": "assistant",
        "isApiErrorMessage": True,
        "timestamp": "2026-06-18T20:17:20.693Z",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "The model's tool call could not be parsed (retry also failed).",
                }
            ],
        },
    }


def _claude_textual_tool_invocation():
    return {
        "type": "assistant",
        "timestamp": "2026-06-20T00:19:53.121Z",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "court\n<invoke name=\"Bash\">\n"
                        "<parameter name=\"command\">tail -n +3 .inbox.review.jsonl</parameter>\n"
                        "<parameter name=\"description\">Read new inbox rows</parameter>\n"
                        "</invoke>"
                    ),
                }
            ],
            "stop_reason": "end_turn",
        },
    }


def test_codex_unauthorized_error_maps_to_auth_expired():
    failure = runtime_failures.runtime_failure_from_codex_event(
        _row("Access token cannot be refreshed because the refresh token was already used.", "unauthorized")
    )

    assert failure == {
        "failure_class": runtime_failures.AUTH_EXPIRED,
        "runtime": "codex",
        "error_code": "unauthorized",
        "summary": "Codex OAuth refresh failed in transcript",
        "timestamp": "2026-06-16T00:00:00Z",
    }


def test_codex_rate_limit_error_maps_to_auth_rate_limited():
    failure = runtime_failures.runtime_failure_from_codex_event(
        _row("Too many requests. Please wait.", "rate_limit_exceeded")
    )

    assert failure == {
        "failure_class": runtime_failures.AUTH_RATE_LIMITED,
        "runtime": "codex",
        "error_code": "rate_limit_exceeded",
        "summary": "Codex runtime reported rate limiting in transcript",
        "timestamp": "2026-06-16T00:00:00Z",
    }


def test_non_error_event_is_not_a_runtime_failure():
    assert runtime_failures.runtime_failure_from_codex_event(
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "ok"}}
    ) is None


def test_claude_api_error_maps_to_runtime_provider_error():
    failure = runtime_failures.runtime_failure_from_transcript_event(_claude_api_error())

    assert failure == {
        "failure_class": runtime_failures.RUNTIME_PROVIDER_ERROR,
        "runtime": "claude_code",
        "error_code": "529",
        "summary": "Claude Code provider/API error in transcript",
        "timestamp": "2026-06-17T07:16:34.196Z",
    }


def test_claude_tool_call_parse_failure_maps_to_runtime_failure():
    failure = runtime_failures.runtime_failure_from_transcript_event(
        _claude_tool_call_parse_failure()
    )

    assert failure == {
        "failure_class": runtime_failures.TOOL_CALL_PARSE_FAILED,
        "runtime": "claude_code",
        "error_code": "tool_call_parse_failed",
        "summary": "Claude Code could not parse the model tool call after retry",
        "timestamp": "2026-06-18T20:17:20.693Z",
    }


def test_transcript_agent_progress_predicate_requires_assistant_progress():
    assert runtime_failures.transcript_event_is_agent_progress({"type": "assistant"})
    assert runtime_failures.transcript_event_is_agent_progress(
        {"type": "response_item", "payload": {"type": "message", "role": "assistant"}}
    )
    assert not runtime_failures.transcript_event_is_agent_progress(_claude_api_error())
    assert not runtime_failures.transcript_event_is_agent_progress(_claude_tool_call_parse_failure())
    assert not runtime_failures.transcript_event_is_agent_progress(_claude_textual_tool_invocation())
    assert not runtime_failures.transcript_event_is_agent_progress({"type": "user"})


def test_textual_tool_invocation_is_non_progress_not_terminal_runtime_failure():
    row = _claude_textual_tool_invocation()

    assert runtime_failures.claude_textual_tool_invocation(row)
    assert runtime_failures.runtime_failure_from_transcript_event(row) is None
