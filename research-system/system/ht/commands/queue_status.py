"""Read-only status and macOS reminder for the tier-1 ratification queue.

RQ-1's daily reminder is deliberately a view over queue state: it never writes
the queue and it does not require a role credential. Only rows whose disposition
is null are pending.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

from ..errors import HtError
from ..paths import Root


def _pending_counts(root: Root) -> Counter[str]:
    queue_dir = root.tier1_dir / "ratification-queue"
    counts: Counter[str] = Counter()
    if not queue_dir.is_dir():
        return counts

    for path in sorted(queue_dir.glob("RQ-*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HtError(
                f"cannot read ratification queue item '{_display_path(root, path)}': "
                f"{exc} (RQ-1 notification path)"
            ) from exc
        if not isinstance(item, dict):
            raise HtError(
                f"ratification queue item '{_display_path(root, path)}' is not "
                "an object (RQ-1 notification path)"
            )
        if "disposition" not in item:
            raise HtError(
                f"ratification queue item '{_display_path(root, path)}' has no "
                "disposition field (RQ-1 notification path)"
            )
        if item["disposition"] is None:
            kind = item.get("kind")
            if not isinstance(kind, str) or not kind:
                raise HtError(
                    f"pending ratification queue item '{_display_path(root, path)}' "
                    "has no kind (RQ-1 notification path)"
                )
            counts[kind] += 1
    return counts


def _display_path(root: Root, path: Path) -> str:
    try:
        return root.rel(path)
    except ValueError:
        return str(path)


def _kind_summary(counts: Counter[str]) -> str:
    return ", ".join(f"{kind}={counts[kind]}" for kind in sorted(counts))


def _status_line(counts: Counter[str], pending_merges: int) -> str:
    total = sum(counts.values())
    by_kind = _kind_summary(counts) or "none"
    return (
        f"queue-status: pending={total}; by-kind: {by_kind}; "
        f"pending-merges={pending_merges}"
    )


def _notification_message(counts: Counter[str], pending_merges: int) -> str:
    total = sum(counts.values())
    noun = "item" if total == 1 else "items"
    return (
        f"{total} pending ratification {noun}: {_kind_summary(counts)}; "
        f"pending merges: {pending_merges}"
    )


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _notify(message: str) -> None:
    script = (
        f"display notification {_applescript_string(message)} "
        'with title "Hypothesis Tree"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise HtError(
            f"macOS notification failed: {exc} (RQ-1 notification path)"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or (
            f"osascript exited {result.returncode}"
        )
        raise HtError(
            f"macOS notification failed: {detail} (RQ-1 notification path)"
        )


def run(root: Root, *, notify: bool, dry_run: bool) -> int:
    """Print status or send the non-empty reminder; never mutate state."""
    # Keep the selector lazy so importing unrelated CLI commands does not pull
    # in the composition-gate discovery dependency.  Capture exactly one
    # committed snapshot and one unconsumed view before any observable output
    # or notification attempt.
    from ..mrec_views import load_merge_record_snapshot

    snapshot = load_merge_record_snapshot(root.path)
    pending_merges = len(snapshot.unconsumed())
    counts = _pending_counts(root)
    total = sum(counts.values())

    if not notify:
        print(_status_line(counts, pending_merges))
        return 0

    if total == 0:
        return 0

    message = _notification_message(counts, pending_merges)
    if dry_run:
        print(f"DRY-RUN notification: {message}")
        return 0

    _notify(message)
    return 0
