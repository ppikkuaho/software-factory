from tests import test_real_boot


def test_adopted_failed_tool_selector_ignores_raw_rows_and_other_tools():
    rows = [
        {
            "row_kind": "raw_hook_event",
            "hook_event": "PostToolUseFailure",
            "detail": {},
        },
        {
            "row_kind": "adopted_hook_event",
            "hook_event": "PostToolUseFailure",
            "detail": {"tool_name": "TaskCreate", "tool_use_id": "task-failed"},
        },
        {
            "row_kind": "adopted_hook_event",
            "hook_event": "PostToolUseFailure",
            "detail": {
                "tool_name": "Bash",
                "tool_use_id": "bash-failed",
                "in_flight_tools": [],
            },
        },
        {
            "row_kind": "hook_response",
            "hook_event": "hook_response",
            "detail": {"ingress_hook_event": "PostToolUseFailure"},
        },
    ]

    assert test_real_boot._adopted_failed_tool_rows(rows, tool_name="Bash") == [
        rows[2]
    ]
