"""W6: held and stuck merge records do not block unrelated progress.

This is deliberately a high-level proof.  State is created and advanced through
the public ``ht`` commands, stage-1 evidence comes from the public ``ht-cgate``
CLI, and decisions use the real compound finalizer.  Every subprocess imports
the co-versioned sources under test rather than an installed wheel.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any
from unittest.mock import patch

import pytest

from composition_gate.classification import ALLGREEN, SEMANTIC_FAILURE, classify_screen
from composition_gate.stage2 import GenerationResult
from conftest import Sandbox
from ht.cgate import execute_decision


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PYTHONPATH = os.pathsep.join(
    (
        str(PROJECT_ROOT / "system/instruments/composition-gate"),
        str(PROJECT_ROOT / "system"),
    )
)


def _run(
    sb: Sandbox,
    *args: str,
    role: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one public ht child against exactly the source tree under test."""

    child_env = {"PYTHONPATH": SOURCE_PYTHONPATH}
    if env_extra:
        child_env.update(env_extra)
    return sb.run(*args, role=role, env_extra=child_env)


@pytest.fixture
def lifecycle_root(tmp_path: Path) -> Sandbox:
    root = tmp_path / "research-root"
    root.mkdir()
    sb = Sandbox(root)
    initialized = _run(sb, "root", "init", role="harness")
    assert initialized.returncode == 0, initialized.stderr
    return sb


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"{result.stderr}\n{result.stdout}"


def _seed_worked_node(sb: Sandbox, component: str) -> None:
    """Create one mergeable node in ``component`` using explicit tree routing."""

    _assert_ok(
        _run(
            sb,
            "tree",
            "init",
            component,
            "--root-question",
            f"Does synthetic {component} evidence hold?",
            role="director",
        )
    )
    _assert_ok(
        _run(
            sb,
            "node",
            "mint",
            "--tree",
            component,
            "--root",
            "--premise",
            f"Synthetic {component} merge candidate",
            "--rationale",
            "queue-and-continue lifecycle proof",
            role="director",
        )
    )
    _assert_ok(
        _run(
            sb,
            "dispatch",
            "create",
            "--tree",
            component,
            "--node",
            "1",
            "--question",
            f"Check synthetic {component} candidate",
            "--done-definition",
            "One anchored granted claim exists.",
            role="director",
        )
    )
    source = sb.write_file(
        f"var/seeds/{component}-report.md",
        f"# Synthetic {component} report\nline two\nline three\nline four\n",
    )
    _assert_ok(
        _run(
            sb,
            "report",
            "submit",
            "--tree",
            component,
            "--dispatch",
            "d-1-1",
            "--src",
            str(source),
            role="unit",
        )
    )
    anchor = f"trees/{component}/nodes/1/reports/d-1-1-report.md:1:3"
    _assert_ok(
        _run(
            sb,
            "claim",
            "grant",
            "--tree",
            component,
            "--dispatch",
            "d-1-1",
            "--text",
            f"Synthetic {component} candidate is supported.",
            "--proposed-tier",
            "2",
            "--granted-tier",
            "2",
            "--standing-class",
            "trunk",
            "--anchor",
            anchor,
            role="verifier",
        )
    )
    _assert_ok(
        _run(
            sb,
            "dispatch",
            "outcome",
            "--tree",
            component,
            "--dispatch",
            "d-1-1",
            "--outcome",
            "completed",
            role="harness",
        )
    )
    assert sb.load(f"trees/{component}/nodes/1/node.json")["status"] == "worked"


def _create_record(
    sb: Sandbox,
    *,
    component: str,
    surface: str,
    glob: str,
) -> str:
    _assert_ok(
        _run(
            sb,
            "mrec",
            "create",
            "--candidate-ref",
            f"tree#{component}/node#1",
            "--lane-verdict",
            "lane-pass",
            "--lane-adjudication-ref",
            f"tree#{component}/adjudication#d-1-1-a1",
            "--scope-lane",
            component,
            "--scope-surface",
            surface,
            "--scope-glob",
            glob,
            role="harness",
        )
    )
    paths = sb.git("ls-tree", "-r", "--name-only", "HEAD", "tier1/merge-records")
    _assert_ok(paths)
    ids = [Path(line).stem for line in paths.stdout.splitlines() if line.endswith(".json")]
    return max(ids, key=_ordinal)


def _screen_record(sb: Sandbox, record_id: str) -> dict[str, Any]:
    """Run public ht-cgate screen, then transcribe its immutable evidence."""

    evidence_dir = sb.root / "var/cgate-screens" / record_id
    output = evidence_dir / "output.json"
    log = evidence_dir / "screen.log.json"
    evidence_dir.mkdir(parents=True, exist_ok=False)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(sb.root),
        "PYTHONPATH": SOURCE_PYTHONPATH,
    }
    screened = subprocess.run(
        [
            sys.executable,
            "-m",
            "composition_gate.cli",
            "screen",
            record_id,
            "--root",
            str(sb.root),
            "--out",
            str(output),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    _assert_ok(screened)
    assert screened.stdout == output.read_text(encoding="utf-8")
    log.write_bytes(output.read_bytes())
    transcribed = _run(
        sb,
        "mrec",
        "screen",
        record_id,
        "--results-json",
        str(output),
        "--log-ref",
        log.relative_to(sb.root).as_posix(),
        role="harness",
    )
    _assert_ok(transcribed)
    return json.loads(output.read_text(encoding="utf-8"))


def _execute(
    sb: Sandbox,
    record_id: str,
    generator: object,
    *,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    head_before = sb.git("rev-parse", "HEAD").stdout.strip()
    with patch.dict(os.environ, {"HT_ROLE": "cgate"}):
        result = execute_decision(
            sb.root,
            record_id,
            generator=generator,
            attempt_id=attempt_id,
        )
    commits = sb.git("rev-list", "--count", f"{head_before}..HEAD")
    _assert_ok(commits)
    assert commits.stdout.strip() == "1"
    return result


class _ForbiddenGenerator:
    def generate(self, _request: object) -> GenerationResult:
        raise AssertionError("allgreen/invalid route invoked a generator")


class _SyntheticHoldGenerator:
    calls = 0

    def generate(self, _request: object) -> GenerationResult:
        self.calls += 1
        return GenerationResult.synthetic(
            {
                "verdict": "hold",
                "note": (
                    "Hold candidate A while its scope overlap and queue adjacency "
                    "are resolved."
                ),
                "observations": [],
            },
            session_id="synthetic-w6-hold",
        )


def _ordinal(record_id: str) -> int:
    return int(record_id.removeprefix("MR-"))


def _worktree_bytes(sb: Sandbox, relative: str) -> bytes:
    return (sb.root / relative).read_bytes()


def _committed_bytes(sb: Sandbox, relative: str) -> bytes:
    result = sb.git("show", f"HEAD:{relative}")
    _assert_ok(result)
    return result.stdout.encode("utf-8")


def _var_snapshot(root: Path) -> tuple[tuple[Any, ...], ...]:
    var = root / "var"
    if not var.exists():
        return ()
    rows: list[tuple[Any, ...]] = []
    for path in sorted((var, *var.rglob("*")), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            rows.append((relative, "symlink", mode, os.readlink(path)))
        elif path.is_dir():
            rows.append((relative, "dir", mode))
        elif path.is_file():
            rows.append((relative, "file", mode, path.read_bytes()))
        else:
            rows.append((relative, "other", mode))
    return tuple(rows)


def _physical_state(sb: Sandbox) -> tuple[Any, ...]:
    def git_bytes(*args: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(sb.root), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return result.stdout

    return (
        git_bytes("rev-parse", "HEAD"),
        (sb.root / ".git/HEAD").read_bytes(),
        (sb.root / ".git/index").read_bytes(),
        git_bytes("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        _var_snapshot(sb.root),
    )


def _parse_pc_packet(document: str) -> dict[str, Any]:
    marker = "## Surface pointers and bounded state\n\n```json\n"
    return json.loads(document.split(marker, 1)[1].split("\n```", 1)[0])


def _expected_views(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    unconsumed = [record for record in records if record["consumed_epoch"] is None]
    pending = [
        record
        for record in unconsumed
        if record["gate_verdict"] is None
        or record["gate_verdict"]["verdict"] == "land"
    ]
    landed = [record for record in unconsumed if record["gate_verdict"] is not None]
    consumed = [record for record in records if record["consumed_epoch"] is not None]
    numeric = lambda record: _ordinal(record["id"])
    consumed_order = lambda record: (-record["consumed_epoch"], -numeric(record))
    awaiting = [record for record in unconsumed if record["gate_verdict"] is None]
    land_ready = [
        record
        for record in unconsumed
        if record["gate_verdict"] is not None
        and record["gate_verdict"]["verdict"] == "land"
    ]
    other = [
        record
        for record in unconsumed
        if record["gate_verdict"] is not None
        and record["gate_verdict"]["verdict"] != "land"
    ]
    return {
        "pending": sorted(pending, key=numeric),
        "landed": sorted(landed, key=numeric),
        "consumed": sorted(consumed, key=consumed_order),
        "all": (
            sorted(awaiting, key=numeric)
            + sorted(land_ready, key=numeric)
            + sorted(other, key=numeric)
            + sorted(consumed, key=consumed_order)
        ),
    }


def _expected_pending_merges(records: list[dict[str, Any]]) -> dict[str, Any]:
    unconsumed = sorted(
        (record for record in records if record["consumed_epoch"] is None),
        key=lambda record: _ordinal(record["id"]),
    )
    rows: list[dict[str, Any]] = []
    partitions = {
        "awaiting-verdict": 0,
        "land-ready": 0,
        "verdict-issued/unconsumed": 0,
    }
    verdicts: dict[str, int] = {}
    pending_count = 0
    landed_count = 0
    for record in unconsumed:
        gate = record["gate_verdict"]
        pending = gate is None or gate["verdict"] == "land"
        landed = gate is not None
        if gate is None:
            partition = "awaiting-verdict"
        elif gate["verdict"] == "land":
            partition = "land-ready"
        else:
            partition = "verdict-issued/unconsumed"
        partitions[partition] += 1
        pending_count += int(pending)
        landed_count += int(landed)
        if gate is not None:
            token = gate["verdict"]
            verdicts[token] = verdicts.get(token, 0) + 1
        rows.append(
            {
                "id": record["id"],
                "candidate_ref": record["candidate_ref"],
                "scope": record["scope"],
                "gate_verdict": gate,
                "memberships": {"pending": pending, "landed": landed},
            }
        )
    return {
        "records": rows,
        "counts": {
            "total_unconsumed": len(unconsumed),
            "pending_view": pending_count,
            "landed_view": landed_count,
            "by_partition": partitions,
            "by_verdict": {key: verdicts[key] for key in sorted(verdicts)},
        },
    }


def _assert_readonly_views(
    sb: Sandbox,
    *,
    record_ids: list[str],
    pending_rqs: int,
) -> None:
    records = [sb.load(f"tier1/merge-records/{record_id}.json") for record_id in record_ids]
    expected_views = _expected_views(records)
    expected_pc = _expected_pending_merges(records)
    before = _physical_state(sb)

    for status in ("pending", "landed", "consumed", "all"):
        listed = _run(sb, "mrec", "list", "--status", status, "--json")
        _assert_ok(listed)
        assert json.loads(listed.stdout) == expected_views[status]

    pc = _run(sb, "pc", "spawn", "--dry-run", "--decision-tail", "0")
    _assert_ok(pc)
    assert _parse_pc_packet(pc.stdout)["pending_merges"] == expected_pc

    status = _run(sb, "queue-status")
    _assert_ok(status)
    pending_merges = expected_pc["counts"]["total_unconsumed"]
    kinds = "none" if pending_rqs == 0 else f"cgate-escalation={pending_rqs}"
    assert status.stdout == (
        f"queue-status: pending={pending_rqs}; by-kind: {kinds}; "
        f"pending-merges={pending_merges}\n"
    )
    if pending_rqs:
        notification = _run(sb, "queue-status", "--notify", "--dry-run")
        _assert_ok(notification)
        noun = "item" if pending_rqs == 1 else "items"
        assert notification.stdout == (
            f"DRY-RUN notification: {pending_rqs} pending ratification {noun}: "
            f"cgate-escalation={pending_rqs}; pending merges: {pending_merges}\n"
        )

    assert _physical_state(sb) == before


def _watch(merged_node: str, epoch: int) -> dict[str, Any]:
    return {
        "id": "W-1",
        "merged_node": merged_node,
        "prediction_claim": None,
        "observed": None,
        "verdict": None,
        "severity": None,
        "status": "queued",
        "kind": "staleness-assessment",
        "epoch": epoch,
    }


def test_held_and_stuck_records_do_not_block_queue_and_continue_lifecycle(
    lifecycle_root: Sandbox,
    tmp_path: Path,
) -> None:
    sb = lifecycle_root
    _seed_worked_node(sb, "L4")
    _seed_worked_node(sb, "L5")

    # MR-1 is the overlapping, deliberately unscreened decoy.  MR-2 is the
    # genuine candidate A and therefore sees both overlap and queue adjacency.
    mr1 = _create_record(
        sb,
        component="L4",
        surface="candidate-A",
        glob="trees/L4/nodes/1/**",
    )
    mr2 = _create_record(
        sb,
        component="L4",
        surface="candidate-A",
        glob="trees/L4/nodes/1/**",
    )
    assert (mr1, mr2) == ("MR-1", "MR-2")
    screen2 = _screen_record(sb, mr2)
    outcomes2 = {row["check"]: row["result"] for row in screen2["results"]}
    assert outcomes2 == {
        "scope-overlap": "fail",
        "surface-budget": "pass",
        "settlement-completeness": "pass",
        "queue-adjacency": "fail",
        "watch-debt": "n/a",
    }
    assert classify_screen(sb.root, sb.load(f"tier1/merge-records/{mr2}.json")) == SEMANTIC_FAILURE

    hold_generator = _SyntheticHoldGenerator()
    held = _execute(sb, mr2, hold_generator, attempt_id="2" * 32)
    assert hold_generator.calls == 1
    assert held["verdict"] == "hold"
    assert held["gate_review_ref"] == "GR-1"
    assert held["ratification_ref"] is None
    held_mr_ref = f"tier1/merge-records/{mr2}.json"
    held_gr_ref = "tier1/gate-reviews/GR-1.json"
    held_mr_bytes = _worktree_bytes(sb, held_mr_ref)
    held_gr_bytes = _worktree_bytes(sb, held_gr_ref)
    assert held_mr_bytes == _committed_bytes(sb, held_mr_ref)
    assert held_gr_bytes == _committed_bytes(sb, held_gr_ref)
    assert sb.load(held_gr_ref)["stage"] == 2
    assert sb.load(held_gr_ref)["generator"]["mechanism"] == "injected-synthetic"
    assert sb.load(held_mr_ref)["watch_link"] is None

    before_rejected_merge = _physical_state(sb)
    rejected = _run(
        sb,
        "node",
        "merge",
        "--tree",
        "L4",
        "--node",
        "1",
        "--merge-record",
        mr2,
        role="director",
    )
    assert rejected.returncode != 0
    assert "requires exactly 'land'" in rejected.stderr
    assert "hold" in rejected.stderr
    assert _physical_state(sb) == before_rejected_merge

    # Verdict freezes the screen before the transcription path reads either
    # user-supplied evidence path.
    frozen = _run(
        sb,
        "mrec",
        "screen",
        mr2,
        "--results-json",
        str(sb.root / "var/does-not-exist-output.json"),
        "--log-ref",
        "var/does-not-exist-log.json",
        role="harness",
    )
    assert frozen.returncode != 0
    assert "screen is frozen" in frozen.stderr
    assert "cannot read" not in frozen.stderr
    assert _worktree_bytes(sb, held_mr_ref) == held_mr_bytes
    assert _worktree_bytes(sb, held_gr_ref) == held_gr_bytes

    _assert_readonly_views(sb, record_ids=[mr1, mr2], pending_rqs=0)

    # With no RQ yet, --notify must be silent and must not invoke osascript.
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    osascript_log = tmp_path / "osascript.log"
    fake_osascript = fake_bin / "osascript"
    fake_osascript.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$OSASCRIPT_LOG\"\n",
        encoding="utf-8",
    )
    fake_osascript.chmod(0o755)
    silent = _run(
        sb,
        "queue-status",
        "--notify",
        env_extra={
            "PATH": os.pathsep.join((str(fake_bin), os.environ.get("PATH", "/usr/bin:/bin"))),
            "OSASCRIPT_LOG": str(osascript_log),
        },
    )
    _assert_ok(silent)
    assert silent.stdout == ""
    assert not osascript_log.exists()

    # The invalid birth path is stage 1, generator-free, and atomically creates
    # GR-2 + RQ-1 + the MR-1 escalate-stuck verdict.
    stuck = _execute(sb, mr1, _ForbiddenGenerator())
    assert stuck["verdict"] == "escalate-stuck"
    assert stuck["gate_review_ref"] == "GR-2"
    assert stuck["ratification_ref"] == "RQ-1"
    assert sb.load("tier1/gate-reviews/GR-2.json")["stage"] == 1
    assert sb.load("tier1/ratification-queue/RQ-1.json")["payload_ref"] == (
        "tier1/gate-reviews/GR-2.json"
    )
    stuck_refs = (
        f"tier1/merge-records/{mr1}.json",
        "tier1/gate-reviews/GR-2.json",
        "tier1/ratification-queue/RQ-1.json",
    )
    stuck_bytes = {ref: _worktree_bytes(sb, ref) for ref in stuck_refs}
    assert all(stuck_bytes[ref] == _committed_bytes(sb, ref) for ref in stuck_refs)
    _assert_readonly_views(sb, record_ids=[mr1, mr2], pending_rqs=1)

    # The existing RQ now triggers the reminder and carries the current merge
    # count, but the fake external notification cannot mutate the research root.
    before_notify = _physical_state(sb)
    notified = _run(
        sb,
        "queue-status",
        "--notify",
        env_extra={
            "PATH": os.pathsep.join((str(fake_bin), os.environ.get("PATH", "/usr/bin:/bin"))),
            "OSASCRIPT_LOG": str(osascript_log),
        },
    )
    _assert_ok(notified)
    assert notified.stdout == ""
    assert "1 pending ratification item: cgate-escalation=1; pending merges: 2" in (
        osascript_log.read_text(encoding="utf-8")
    )
    assert _physical_state(sb) == before_notify

    # Candidate B is disjoint from A.  The old held/stuck records are outside
    # the pending frontier, so its real screen is allgreen and auto-lands.
    mr3 = _create_record(
        sb,
        component="L5",
        surface="candidate-B",
        glob="trees/L5/nodes/1/**",
    )
    assert mr3 == "MR-3"
    screen3 = _screen_record(sb, mr3)
    assert [row["result"] for row in screen3["results"]] == [
        "pass",
        "pass",
        "pass",
        "pass",
        "n/a",
    ]
    assert classify_screen(sb.root, sb.load(f"tier1/merge-records/{mr3}.json")) == ALLGREEN
    landed3 = _execute(sb, mr3, _ForbiddenGenerator())
    assert landed3["verdict"] == "land"
    assert landed3["gate_review_ref"] == "GR-3"
    assert landed3["ratification_ref"] is None
    mr3_before_merge = sb.load(f"tier1/merge-records/{mr3}.json")
    gr3_bytes = _worktree_bytes(sb, "tier1/gate-reviews/GR-3.json")
    _assert_readonly_views(sb, record_ids=[mr1, mr2, mr3], pending_rqs=1)

    merged3 = _run(
        sb,
        "node",
        "merge",
        "--tree",
        "L5",
        "--node",
        "1",
        "--merge-record",
        mr3,
        role="director",
    )
    _assert_ok(merged3)
    mr3_after_merge = sb.load(f"tier1/merge-records/{mr3}.json")
    assert mr3_after_merge == {**mr3_before_merge, "consumed_epoch": 1}
    assert sb.load("trees/L5/tree.json")["epoch"] == 1
    assert sb.load("trees/L4/tree.json")["epoch"] == 0
    assert sb.load("trees/L4/tree.json")["watch_queue"] == [
        _watch("tree#L5/node#1", 1)
    ]
    assert sb.load("trees/L5/tree.json")["watch_queue"] == []
    assert _worktree_bytes(sb, "tier1/gate-reviews/GR-3.json") == gr3_bytes
    assert _worktree_bytes(sb, held_mr_ref) == held_mr_bytes
    assert _worktree_bytes(sb, held_gr_ref) == held_gr_bytes
    assert all(_worktree_bytes(sb, ref) == stuck_bytes[ref] for ref in stuck_refs)
    _assert_readonly_views(sb, record_ids=[mr1, mr2, mr3], pending_rqs=1)

    # A fresh merge record is the only legal way to retry candidate A.  Its
    # evidence paths are new, while MR-2's evidence and verdict remain frozen.
    mr4 = _create_record(
        sb,
        component="L4",
        surface="candidate-A",
        glob="trees/L4/nodes/1/**",
    )
    assert mr4 == "MR-4"
    screen4 = _screen_record(sb, mr4)
    assert [row["result"] for row in screen4["results"]] == [
        "pass",
        "pass",
        "pass",
        "pass",
        "n/a",
    ]
    assert classify_screen(sb.root, sb.load(f"tier1/merge-records/{mr4}.json")) == ALLGREEN
    landed4 = _execute(sb, mr4, _ForbiddenGenerator())
    assert landed4["verdict"] == "land"
    assert landed4["gate_review_ref"] == "GR-4"
    assert landed4["ratification_ref"] is None
    mr4_before_merge = sb.load(f"tier1/merge-records/{mr4}.json")
    gr4_bytes = _worktree_bytes(sb, "tier1/gate-reviews/GR-4.json")
    _assert_readonly_views(sb, record_ids=[mr1, mr2, mr3, mr4], pending_rqs=1)

    merged4 = _run(
        sb,
        "node",
        "merge",
        "--tree",
        "L4",
        "--node",
        "1",
        "--merge-record",
        mr4,
        role="director",
    )
    _assert_ok(merged4)
    mr4_after_merge = sb.load(f"tier1/merge-records/{mr4}.json")
    assert mr4_after_merge == {**mr4_before_merge, "consumed_epoch": 2}
    assert sb.load("trees/L4/tree.json")["epoch"] == 2
    assert sb.load("trees/L5/tree.json")["epoch"] == 1
    assert sb.load("trees/L4/tree.json")["watch_queue"] == [
        _watch("tree#L5/node#1", 1)
    ]
    assert sb.load("trees/L5/tree.json")["watch_queue"] == [
        _watch("tree#L4/node#1", 2)
    ]
    assert sb.load("trees/L4/nodes/1/node.json")["status"] == "merged"
    assert sb.load("trees/L5/nodes/1/node.json")["status"] == "merged"
    assert _worktree_bytes(sb, "tier1/gate-reviews/GR-4.json") == gr4_bytes

    # All immutable earlier evidence survives both unrelated and related trunk
    # progress byte-for-byte.  Only the two landed records gained their epochs.
    assert _worktree_bytes(sb, held_mr_ref) == held_mr_bytes
    assert _worktree_bytes(sb, held_gr_ref) == held_gr_bytes
    assert all(_worktree_bytes(sb, ref) == stuck_bytes[ref] for ref in stuck_refs)
    assert _worktree_bytes(sb, "tier1/gate-reviews/GR-3.json") == gr3_bytes
    for record_id in (mr1, mr2, mr3, mr4):
        assert sb.load(f"tier1/merge-records/{record_id}.json")["watch_link"] is None
    assert sb.load(f"tier1/merge-records/{mr1}.json")["consumed_epoch"] is None
    assert sb.load(f"tier1/merge-records/{mr2}.json")["consumed_epoch"] is None
    assert sb.load(f"tier1/merge-records/{mr3}.json")["consumed_epoch"] == 1
    assert sb.load(f"tier1/merge-records/{mr4}.json")["consumed_epoch"] == 2
    _assert_readonly_views(sb, record_ids=[mr1, mr2, mr3, mr4], pending_rqs=1)
