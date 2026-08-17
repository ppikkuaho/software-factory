"""Principal-coordinator launch packet composition and seat boot."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .. import jsonio
from ..errors import HtError, HtUsageError
from ..paths import Root


_ROLE_PACKET = Path(__file__).resolve().parents[2] / "roles" / "principal-coordinator.v1.md"
SEAT = "ht-pc"


def _rel(root: Root, path: Path) -> str:
    return path.relative_to(root.path).as_posix()


def _require_pointer(root: Root, path: Path) -> str:
    if not path.exists():
        raise HtError(f"PC launch surface is missing: {_rel(root, path)}")
    return _rel(root, path)


def _decision_tail(root: Root, count: int) -> list[dict]:
    paths = sorted(
        (root.tier1_dir / "decision-log").glob("PCD-*.json"),
        key=lambda path: int(path.stem.split("-")[1]),
    )
    return [jsonio.load(path) for path in paths[-count:]] if count else []


def _pending_rq(root: Root) -> list[dict]:
    items = [
        jsonio.load(path)
        for path in sorted((root.tier1_dir / "ratification-queue").glob("RQ-*.json"))
    ]
    return [item for item in items if item.get("disposition") is None]


def _pending_merges(root: Root) -> dict:
    """Pull the D4 consolidate-first state into the bounded PC packet.

    The principal coordinator observes composition-gate state through this
    read-only pull surface; no cgate-to-PC write or notification channel is
    introduced.
    """

    # Keep the committed-state selector off unrelated PC import paths.  It has
    # a deliberately bounded composition-gate dependency and is needed only
    # while composing the launch packet.
    from ..mrec_views import load_merge_record_snapshot

    snapshot = load_merge_record_snapshot(root.path)
    entries = snapshot.unconsumed()
    records: list[dict] = []
    partition_counts = {
        "awaiting-verdict": 0,
        "land-ready": 0,
        "verdict-issued/unconsumed": 0,
    }
    verdict_counts: dict[str, int] = {}
    pending_count = 0
    landed_count = 0

    for entry in entries:
        record = entry.as_dict()
        gate_verdict = record["gate_verdict"]
        records.append(
            {
                "id": record["id"],
                "candidate_ref": record["candidate_ref"],
                "scope": record["scope"],
                "gate_verdict": gate_verdict,
                "memberships": {
                    "pending": entry.pending,
                    "landed": entry.landed,
                },
            }
        )
        pending_count += int(entry.pending)
        landed_count += int(entry.landed)
        partition_counts[entry.partition] += 1
        if gate_verdict is not None:
            verdict = gate_verdict["verdict"]
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    return {
        "records": records,
        "counts": {
            "total_unconsumed": len(entries),
            "pending_view": pending_count,
            "landed_view": landed_count,
            "by_partition": partition_counts,
            "by_verdict": {
                verdict: verdict_counts[verdict]
                for verdict in sorted(verdict_counts)
            },
        },
    }


def compose(root: Root, *, decision_tail: int) -> str:
    if decision_tail < 0:
        raise HtUsageError("--decision-tail must be non-negative")
    if not _ROLE_PACKET.exists():
        raise HtError(f"PC role packet is missing: {_ROLE_PACKET}")

    pending_merges = _pending_merges(root)
    pointer_paths = {
        "union_ledger_index": _require_pointer(root, root.ledger_union_index),
        "composed_tree": _require_pointer(root, root.composed_tree_json),
        "statistics": _require_pointer(root, root.readout_dir / "statistics.json"),
        "interpretation_rules": _require_pointer(
            root, root.readout_dir / "INTERPRETATION.md"
        ),
        "issue_queue": _require_pointer(root, root.issue_queue_json),
    }
    tree_indexes = [
        _require_pointer(root, path)
        for path in sorted(root.trees_dir.glob("*/index.json"))
    ]
    tree_standings = [
        _require_pointer(root, path)
        for path in sorted(root.trees_dir.glob("*/index.live.json"))
    ]
    report_cards = [
        _require_pointer(root, path)
        for path in sorted(root.readout_dir.glob("observatory/*/report-card.md"))
    ]
    pending_rq = _pending_rq(root)
    counts: dict[str, int] = {}
    for item in pending_rq:
        kind = str(item.get("kind", "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
    surfaces = {
        "seat": SEAT,
        "pointers": pointer_paths,
        "tree_indexes": tree_indexes,
        "tree_standings": tree_standings,
        "observatory_report_cards": report_cards,
        "queue_status_command": "ht queue-status",
        "queue_status_summary": {
            "pending": len(pending_rq),
            "by_kind": {kind: counts[kind] for kind in sorted(counts)},
        },
        "issue_queue_state": jsonio.load(root.issue_queue_json),
        "decision_log_tail": _decision_tail(root, decision_tail),
        "pending_ratification_queue": pending_rq,
        "pending_merges": pending_merges,
        "launch_command": (
            f"tmux new-session -d -s {SEAT} codex -C {root.path} <launch-document>; "
            f"first act: bus enable --name {SEAT} --description "
            '"Principal coordinator for the hypothesis-tree research root"'
        ),
    }
    return (
        "# Principal coordinator launch document\n\n"
        "## Role packet\n\n"
        + _ROLE_PACKET.read_text(encoding="utf-8")
        + "\n## Surface pointers and bounded state\n\n```json\n"
        + json.dumps(surfaces, indent=2, ensure_ascii=False)
        + "\n```\n"
    )


def spawn(
    root: Root,
    *,
    dry_run: bool,
    launch: bool,
    out: str | None,
    decision_tail: int,
) -> int:
    if dry_run == launch:
        raise HtUsageError("pc spawn requires exactly one of --dry-run or --launch")
    document = compose(root, decision_tail=decision_tail)
    if out is not None:
        output_path = Path(out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(document, encoding="utf-8")
    if dry_run:
        print(document, end="")
        return 0

    # Commissioning only: this path is intentionally not exercised by item 6.
    # The prompt makes bus identity the launched seat's first act, so delivery is
    # still broker-observed rather than inferred by the parent command.
    if shutil.which("tmux") is None or shutil.which("codex") is None:
        raise HtError("pc spawn --launch requires tmux and codex on PATH")
    launched = subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", SEAT,
            "codex", "-C", str(root.path), document,
        ],
        cwd=root.path,
        text=True,
        capture_output=True,
        check=False,
    )
    if launched.returncode != 0:
        detail = (launched.stderr or launched.stdout).strip() or (
            f"tmux exited {launched.returncode}"
        )
        raise HtError(f"pc spawn --launch failed: {detail}")
    return 0
