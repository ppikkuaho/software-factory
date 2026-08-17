"""Rejection errors.

Every mechanical-rule rejection (B4 §9) must name the violated rule and the note
section, e.g. "granted_tier 3 > proposed_tier 2 (B4 §9)", and exit non-zero. The
CLI catches HtError, prints "REJECTED: <message>" to stderr, and returns code 2.
"""

from __future__ import annotations


class HtError(Exception):
    """A rejected mutation. `message` already carries the rule + note section."""

    exit_code = 2

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class HtUsageError(HtError):
    """Bad invocation (missing args, unknown target). Distinct exit code."""

    exit_code = 3
