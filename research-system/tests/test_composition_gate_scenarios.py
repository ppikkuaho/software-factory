"""W7: real composition-gate scenarios over synthetic committed state.

Every public ``ht``/``ht-cgate`` child imports the co-versioned sources under
test.  Historical merge records are current-schema synthetic fixtures; the
candidate lifecycle, screen transcription, compound decision, standing-class
gate, and merge consumption all exercise their real public mechanisms.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import patch

import pytest

from composition_gate.classification import (
    ALLGREEN,
    INVALID,
    SEMANTIC_FAILURE,
    classify_screen,
)
from composition_gate.decision import prepare_decision
from composition_gate.screen import CHECK_NAMES
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
NULL_SCREEN = {
    "results": [],
    "output_ref": None,
    "log_ref": None,
    "log_sha256": None,
    "output_sha256": None,
    "computed": None,
    "head_commit": None,
    "head_tree": None,
    "config_hash": None,
    "engine_version": None,
}


def _run(
    sb: Sandbox,
    *args: str,
    role: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return sb.run(
        *args,
        role=role,
        env_extra={"PYTHONPATH": SOURCE_PYTHONPATH},
    )


def _ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"{result.stderr}\n{result.stdout}"


@pytest.fixture
def scenario_root(tmp_path: Path) -> Sandbox:
    root = tmp_path / "research-root"
    root.mkdir()
    sb = Sandbox(root)
    _ok(_run(sb, "root", "init", role="harness"))
    return sb


def _seed_candidate(
    sb: Sandbox,
    *,
    standing_class: str = "trunk",
    surfaces: tuple[str, ...] = (),
    globs: tuple[str, ...] = (),
    create_record: bool = True,
) -> str:
    _ok(
        _run(
            sb,
            "tree",
            "init",
            "L4",
            "--root-question",
            "Does the synthetic W7 candidate compose?",
            role="director",
        )
    )
    _ok(
        _run(
            sb,
            "node",
            "mint",
            "--tree",
            "L4",
            "--root",
            "--premise",
            "Synthetic W7 candidate",
            "--rationale",
            "composition-gate scenario corpus",
            role="director",
        )
    )
    _ok(
        _run(
            sb,
            "dispatch",
            "create",
            "--tree",
            "L4",
            "--node",
            "1",
            "--question",
            "Does this synthetic candidate satisfy the scenario?",
            "--done-definition",
            "One anchored synthetic claim is adjudicated.",
            role="director",
        )
    )
    report = sb.write_file(
        "var/w7/candidate-report.md",
        "# Synthetic W7 report\nline two\nline three\nline four\n",
    )
    _ok(
        _run(
            sb,
            "report",
            "submit",
            "--tree",
            "L4",
            "--dispatch",
            "d-1-1",
            "--src",
            str(report),
            role="unit",
        )
    )
    _ok(
        _run(
            sb,
            "claim",
            "grant",
            "--tree",
            "L4",
            "--dispatch",
            "d-1-1",
            "--text",
            "The synthetic W7 candidate is supported.",
            "--proposed-tier",
            "2",
            "--granted-tier",
            "2",
            "--standing-class",
            standing_class,
            "--anchor",
            "trees/L4/nodes/1/reports/d-1-1-report.md:1:3",
            role="verifier",
        )
    )
    _ok(
        _run(
            sb,
            "dispatch",
            "outcome",
            "--tree",
            "L4",
            "--dispatch",
            "d-1-1",
            "--outcome",
            "completed",
            role="harness",
        )
    )
    command = [
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        "tree#L4/adjudication#d-1-1-a1",
        "--scope-lane",
        "L4",
    ]
    for surface in surfaces:
        command.extend(("--scope-surface", surface))
    for glob in globs:
        command.extend(("--scope-glob", glob))
    if create_record:
        _ok(_run(sb, *command, role="harness"))
        assert sb.load("tier1/merge-records/MR-1.json")["screen"] == NULL_SCREEN
    return "MR-1"


def _scope(
    lane: str,
    *,
    surfaces: tuple[str, ...] = (),
    globs: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "lane": lane,
        "seats": [],
        "surfaces": list(surfaces),
        "globs": list(globs),
    }


def _historical_record(
    record_id: str,
    *,
    lane: str,
    epoch: int | None,
    node: str | None = None,
    surfaces: tuple[str, ...] = (),
    globs: tuple[str, ...] = (),
) -> dict[str, Any]:
    gate = None
    if epoch is not None:
        gate = {
            "verdict": "land",
            "date": "2026-07-14",
            "review_ref": f"GR-{record_id.removeprefix('MR-')}",
            "review_sha256": "0" * 64,
            "note": "Synthetic historical land fixture.",
        }
    return {
        "id": record_id,
        "candidate_ref": f"tree#{lane}/node#{node or record_id.lower()}",
        "lane_verdict": "lane-pass",
        "scope": _scope(lane, surfaces=surfaces, globs=globs),
        "screen": dict(NULL_SCREEN),
        "gate_verdict": gate,
        "watch_link": None,
        "created": "2026-07-14",
        "consumed_epoch": epoch,
    }


def _watch(record: dict[str, Any], row_id: str) -> dict[str, Any]:
    return {
        "id": row_id,
        "merged_node": record["candidate_ref"],
        "prediction_claim": None,
        "observed": None,
        "verdict": None,
        "severity": None,
        "status": "queued",
        "kind": "staleness-assessment",
        "epoch": record["consumed_epoch"],
    }


def _commit_documents(
    sb: Sandbox,
    documents: dict[str, dict[str, Any]],
    message: str,
) -> None:
    for relative, document in documents.items():
        sb.write_file(
            relative,
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        )
    _ok(sb.git("add", "--", *documents))
    _ok(sb.git("commit", "--no-verify", "-m", message))


def _add_history(
    sb: Sandbox,
    records: list[dict[str, Any]],
    *,
    watches: tuple[dict[str, Any], ...] = (),
) -> None:
    documents = {
        f"tier1/merge-records/{record['id']}.json": record for record in records
    }
    if watches:
        tree_ref = "trees/L4/tree.json"
        tree = sb.load(tree_ref)
        tree["watch_queue"] = list(watches)
        documents[tree_ref] = tree
    _commit_documents(sb, documents, "seed current-schema W7 history")


def _screen_and_transcribe(sb: Sandbox, record_id: str) -> dict[str, Any]:
    evidence = sb.root / "var/w7/screens" / record_id
    evidence.mkdir(parents=True, exist_ok=False)
    output = evidence / "output.json"
    log = evidence / "screen.log.json"
    environment = {
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
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    _ok(screened)
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
    _ok(transcribed)
    rendered = json.loads(output.read_text(encoding="utf-8"))
    assert sb.load(f"tier1/merge-records/{record_id}.json")["screen"][
        "results"
    ] == rendered["results"]
    return rendered


def _result_vector(screen: dict[str, Any]) -> list[tuple[str, str]]:
    return [(row["check"], row["result"]) for row in screen["results"]]


def _changed_paths(sb: Sandbox) -> set[str]:
    result = sb.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
    _ok(result)
    return set(result.stdout.splitlines())


def _tracked_state(sb: Sandbox) -> tuple[bytes, ...]:
    index = sb.root / ".git/index"
    commands = (
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ("diff", "--binary"),
        ("diff", "--cached", "--binary"),
    )
    rows: list[bytes] = []
    for command in commands:
        result = sb.git(*command)
        _ok(result)
        rows.append(result.stdout.encode("utf-8"))
    # Git's read-only observations can refresh index stat metadata on some
    # versions.  Freeze the raw index only after those observations so two
    # snapshots compare the command-under-test boundary, not this helper's
    # own first observation against its settled state.
    return (index.read_bytes(), *rows)


class _ForbiddenGenerator:
    def generate(self, _request: object) -> GenerationResult:
        raise AssertionError("stage-1 route invoked a generator")


class _ScenarioGenerator:
    def __init__(self, *, verdict: str, note: str, session: str) -> None:
        self.verdict = verdict
        self.note = note
        self.session = session
        self.calls = 0

    def generate(self, _request: object) -> GenerationResult:
        self.calls += 1
        return GenerationResult.synthetic(
            {
                "verdict": self.verdict,
                "note": self.note,
                "observations": [],
            },
            session_id=self.session,
        )


def _scope_history() -> tuple[list[dict[str, Any]], tuple[dict[str, Any], ...]]:
    return [
        _historical_record("MR-2", lane="L4", epoch=1),
    ], ()


def _surface_history() -> tuple[list[dict[str, Any]], tuple[dict[str, Any], ...]]:
    records = [
        _historical_record(
            f"MR-{ordinal + 1}",
            lane=f"surface-history-{ordinal}",
            epoch=ordinal,
            surfaces=("panel",),
        )
        for ordinal in range(1, 12)
    ]
    records.extend(
        _historical_record(
            f"MR-{ordinal + 1}",
            lane=f"recent-disjoint-{ordinal}",
            epoch=ordinal,
            surfaces=(f"other-{ordinal}",),
        )
        for ordinal in range(12, 17)
    )
    return records, ()


def _settlement_history() -> tuple[list[dict[str, Any]], tuple[dict[str, Any], ...]]:
    frontier = _historical_record("MR-2", lane="L4", epoch=5)
    recent = [
        _historical_record(
            f"MR-{ordinal}",
            lane=f"recent-{ordinal}",
            epoch=ordinal + 3,
        )
        for ordinal in range(3, 8)
    ]
    source = _historical_record("MR-8", lane="watch-source", epoch=4)
    return [frontier, *recent, source], (_watch(source, "W-OLD"),)


def _queue_history() -> tuple[list[dict[str, Any]], tuple[dict[str, Any], ...]]:
    return [
        _historical_record("MR-2", lane="pending-two", epoch=None),
        _historical_record("MR-3", lane="pending-three", epoch=None),
    ], ()


def _watch_history() -> tuple[list[dict[str, Any]], tuple[dict[str, Any], ...]]:
    source = _historical_record(
        "MR-2",
        lane="watch-source",
        epoch=1,
        surfaces=("panel",),
    )
    recent = [
        _historical_record(
            f"MR-{ordinal}",
            lane=f"recent-{ordinal}",
            epoch=ordinal - 1,
            surfaces=(f"other-{ordinal}",),
        )
        for ordinal in range(3, 8)
    ]
    return [source, *recent], (_watch(source, "W-WATCH"),)


SCENARIOS = (
    pytest.param(
        "scope-overlap",
        (),
        _scope_history,
        ["fail", "n/a", "pass", "pass", "n/a"],
        [{"rule_id": "R-OVERLAP-SEQ", "outcome": "land-after-MR-2"}],
        "land-after-MR-2",
        "Sequence after MR-2 before this candidate proceeds.",
        id="scope-overlap-binding-sequence",
    ),
    pytest.param(
        "surface-budget",
        ("panel",),
        _surface_history,
        ["pass", "fail", "pass", "pass", "n/a"],
        [],
        "bounce-for-surface-rework",
        "Bounce for surface rework on panel.",
        id="surface-budget-stage2-bounce",
    ),
    pytest.param(
        "settlement-completeness",
        (),
        _settlement_history,
        ["pass", "n/a", "fail", "pass", "n/a"],
        [{"rule_id": "R-SETTLE-HOLD", "outcome": "hold"}],
        "hold",
        "Hold until W-OLD is resolved and settlement can unblock.",
        id="settlement-binding-hold",
    ),
    pytest.param(
        "queue-adjacency",
        (),
        _queue_history,
        ["pass", "n/a", "pass", "fail", "n/a"],
        [{"rule_id": "R-CONSOLIDATE", "outcome": "consolidate-first"}],
        "consolidate-first",
        "Consolidate MR-1, MR-2, and MR-3 before scheduling proceeds.",
        id="queue-binding-consolidate",
    ),
    pytest.param(
        "watch-debt",
        ("panel",),
        _watch_history,
        ["pass", "pass", "pass", "pass", "fail"],
        [],
        "hold",
        "Hold until W-WATCH is resolved to unblock the overlapping watch debt.",
        id="watch-stage2-hold",
    ),
)


@pytest.mark.parametrize(
    (
        "failed_check",
        "surfaces",
        "history_factory",
        "expected_results",
        "expected_rules",
        "verdict",
        "note",
    ),
    SCENARIOS,
)
def test_each_mechanical_check_is_the_sole_failure_and_routes_through_stage2(
    scenario_root: Sandbox,
    failed_check: str,
    surfaces: tuple[str, ...],
    history_factory,
    expected_results: list[str],
    expected_rules: list[dict[str, str]],
    verdict: str,
    note: str,
) -> None:
    sb = scenario_root
    record_id = _seed_candidate(sb, surfaces=surfaces)
    history, watches = history_factory()
    _add_history(sb, history, watches=watches)

    screen = _screen_and_transcribe(sb, record_id)
    assert _result_vector(screen) == list(zip(CHECK_NAMES, expected_results, strict=True))
    assert [row["check"] for row in screen["results"] if row["result"] == "fail"] == [
        failed_check
    ]
    record = sb.load(f"tier1/merge-records/{record_id}.json")
    assert classify_screen(sb.root, record) == SEMANTIC_FAILURE
    decision = prepare_decision(sb.root, record_id)
    assert decision["route"] == "stage2"
    assert decision["stage"] == 2
    assert decision["verdict"] is None
    assert decision["rules_fired"] == expected_rules

    generator = _ScenarioGenerator(
        verdict=verdict,
        note=note,
        session=f"synthetic-w7-{failed_check}",
    )
    before = sb.git("rev-parse", "HEAD").stdout.strip()
    attempt_id = hashlib.sha256(failed_check.encode()).hexdigest()[:32]
    with patch.dict(os.environ, {"HT_ROLE": "cgate"}):
        result = execute_decision(
            sb.root,
            record_id,
            generator=generator,
            attempt_id=attempt_id,
        )
    assert generator.calls == 1
    commits = sb.git("rev-list", "--count", f"{before}..HEAD")
    _ok(commits)
    assert commits.stdout.strip() == "1"
    assert _changed_paths(sb) == {
        f"tier1/merge-records/{record_id}.json",
        "tier1/gate-reviews/GR-1.json",
    }
    decided = sb.load(f"tier1/merge-records/{record_id}.json")
    review_ref = "tier1/gate-reviews/GR-1.json"
    review = sb.load(review_ref)
    review_bytes = (sb.root / review_ref).read_bytes()
    review_digest = hashlib.sha256(review_bytes).hexdigest()
    assert result == {
        "record_id": record_id,
        "gate_review_ref": "GR-1",
        "gate_review_sha256": review_digest,
        "verdict": verdict,
        "ratification_ref": None,
    }
    assert decided["gate_verdict"] == {
        "verdict": verdict,
        "date": decided["gate_verdict"]["date"],
        "review_ref": "GR-1",
        "review_sha256": review_digest,
        "note": note,
    }
    assert review["merge_record_ref"] == record_id
    assert review["stage"] == 2
    assert review["attempt_id"] == attempt_id
    assert review["rules_fired"] == expected_rules
    assert review["verdict"] == verdict
    assert review["note"] == note
    assert review["escalation_ref"] is None
    assert review["generator"] == {
        "mechanism": "injected-synthetic",
        "status": "synthetic",
        "requested_model": None,
        "actual_model": "<synthetic>",
        "session_ref": f"synthetic-w7-{failed_check}",
        "error": None,
    }
    assert review["packet"]["manifest_ref"].startswith(
        f"var/cgate/{record_id}/attempts/{attempt_id}/"
    )
    for ref, digest in review["packet"]["input_hashes"].items():
        artifact = sb.root / f"var/cgate/{record_id}/attempts/{attempt_id}" / ref
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
    raw = sb.root / review["raw_output"]["ref"]
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == review["raw_output"][
        "sha256"
    ]
    assert not list((sb.root / "tier1/ratification-queue").glob("RQ-*.json"))

    state_before_merge = _tracked_state(sb)
    rejected = _run(
        sb,
        "node",
        "merge",
        "--tree",
        "L4",
        "--node",
        "1",
        "--merge-record",
        record_id,
        role="director",
    )
    assert rejected.returncode != 0
    assert "requires exactly 'land'" in rejected.stderr
    assert verdict in rejected.stderr
    assert _tracked_state(sb) == state_before_merge
    assert decided["consumed_epoch"] is None


def test_missing_committed_tree_input_fails_toward_review_atomically(
    scenario_root: Sandbox,
) -> None:
    sb = scenario_root
    record_id = _seed_candidate(sb)
    _ok(sb.git("rm", "trees/L4/tree.json"))
    _ok(sb.git("commit", "--no-verify", "-m", "remove required synthetic tree input"))

    screen = _screen_and_transcribe(sb, record_id)
    assert _result_vector(screen) == list(
        zip(CHECK_NAMES, ["pass", "n/a", "fail", "pass", "fail"], strict=True)
    )
    errors = [row for row in screen["results"] if row["detail"].startswith("screen-error:")]
    assert [row["check"] for row in errors] == [
        "settlement-completeness",
        "watch-debt",
    ]
    for row in errors:
        assert row["inputs"]["error"]["type"] == "ScreenInputError"
        assert "missing exact tree.json" in row["inputs"]["error"]["message"]
        assert "committed tree directory trees/L4/" in row["detail"]

    record = sb.load(f"tier1/merge-records/{record_id}.json")
    assert classify_screen(sb.root, record) == INVALID
    decision = prepare_decision(sb.root, record_id)
    assert decision["route"] == "stuck"
    assert decision["stage"] == 1
    assert decision["verdict"] == "escalate-stuck"
    assert decision["rules_fired"] == [
        {"rule_id": "R-SCREEN-INVALID", "outcome": "escalate-stuck"}
    ]

    before = sb.git("rev-parse", "HEAD").stdout.strip()
    with patch.dict(os.environ, {"HT_ROLE": "cgate"}):
        result = execute_decision(sb.root, record_id, generator=_ForbiddenGenerator())
    commits = sb.git("rev-list", "--count", f"{before}..HEAD")
    _ok(commits)
    assert commits.stdout.strip() == "1"
    assert _changed_paths(sb) == {
        f"tier1/merge-records/{record_id}.json",
        "tier1/gate-reviews/GR-1.json",
        "tier1/ratification-queue/RQ-1.json",
    }
    assert result["verdict"] == "escalate-stuck"
    assert result["gate_review_ref"] == "GR-1"
    assert result["ratification_ref"] == "RQ-1"
    review_ref = "tier1/gate-reviews/GR-1.json"
    review_bytes = (sb.root / review_ref).read_bytes()
    review_digest = hashlib.sha256(review_bytes).hexdigest()
    assert result["gate_review_sha256"] == review_digest
    decided = sb.load(f"tier1/merge-records/{record_id}.json")
    assert decided["gate_verdict"]["review_ref"] == "GR-1"
    assert decided["gate_verdict"]["review_sha256"] == review_digest
    review = sb.load(review_ref)
    assert review["stage"] == 1
    assert review["attempt_id"] is None
    assert review["packet"] is None
    assert review["generator"] is None
    assert review["raw_output"] is None
    assert review["verdict"] == "escalate-stuck"
    assert review["escalation_ref"] == "RQ-1"
    assert sb.load("tier1/ratification-queue/RQ-1.json")["payload_ref"] == (
        "tier1/gate-reviews/GR-1.json"
    )
    assert not (sb.root / f"var/cgate/{record_id}/attempts").exists()


def test_allgreen_sandbox_claim_requires_revalidation_then_consumes_same_mr(
    scenario_root: Sandbox,
) -> None:
    sb = scenario_root
    record_id = _seed_candidate(
        sb, standing_class="sandbox", create_record=False
    )
    create_command = (
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        "tree#L4/adjudication#d-1-1-a1",
        "--scope-lane",
        "L4",
    )
    state_before_rejection = _tracked_state(sb)
    rejected = _run(sb, *create_command, role="harness")
    assert rejected.returncode != 0
    assert "merge-ineligible granted claims: c-1-1" in rejected.stderr
    assert _tracked_state(sb) == state_before_rejection
    assert not (sb.root / "tier1/merge-records/MR-1.json").exists()

    revalidated = _run(
        sb,
        "claim",
        "revalidate",
        "c-1-1",
        "--tree",
        "L4",
        "--ref",
        "tree#L4/adjudication#d-1-1-a1",
        role="verifier",
    )
    _ok(revalidated)
    claim = sb.load("trees/L4/nodes/1/node.json")["claims"][0]
    assert claim["revalidation"]["epoch"] == 0
    assert claim["revalidation"]["ref"] == "tree#L4/adjudication#d-1-1-a1"

    _ok(_run(sb, *create_command, role="harness"))
    assert sb.load("tier1/merge-records/MR-1.json")["screen"] == NULL_SCREEN
    screen = _screen_and_transcribe(sb, record_id)
    assert _result_vector(screen) == list(
        zip(CHECK_NAMES, ["pass", "n/a", "pass", "pass", "n/a"], strict=True)
    )
    record = sb.load(f"tier1/merge-records/{record_id}.json")
    assert classify_screen(sb.root, record) == ALLGREEN
    decision = prepare_decision(sb.root, record_id)
    assert decision["route"] == "auto"
    assert decision["stage"] == 1
    assert decision["verdict"] == "land"

    before = sb.git("rev-parse", "HEAD").stdout.strip()
    with patch.dict(os.environ, {"HT_ROLE": "cgate"}):
        result = execute_decision(sb.root, record_id, generator=_ForbiddenGenerator())
    commits = sb.git("rev-list", "--count", f"{before}..HEAD")
    _ok(commits)
    assert commits.stdout.strip() == "1"
    assert result["verdict"] == "land"
    assert result["ratification_ref"] is None
    review_ref = "tier1/gate-reviews/GR-1.json"
    review_bytes = (sb.root / review_ref).read_bytes()
    review_digest = hashlib.sha256(review_bytes).hexdigest()
    assert result["gate_review_ref"] == "GR-1"
    assert result["gate_review_sha256"] == review_digest
    decided = sb.load(f"tier1/merge-records/{record_id}.json")
    assert decided["gate_verdict"]["review_ref"] == "GR-1"
    assert decided["gate_verdict"]["review_sha256"] == review_digest
    assert sb.load(review_ref)["stage"] == 1

    merged = _run(
        sb,
        "node",
        "merge",
        "--tree",
        "L4",
        "--node",
        "1",
        "--merge-record",
        record_id,
        role="director",
    )
    _ok(merged)
    assert sb.load("trees/L4/nodes/1/node.json")["status"] == "merged"
    assert sb.load("trees/L4/tree.json")["epoch"] == 1
    assert sb.load(f"tier1/merge-records/{record_id}.json")["consumed_epoch"] == 1
