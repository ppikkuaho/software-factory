"""Item-1 W6/Q8 black-box tests for the merge-record CLI lifecycle."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import unicodedata

import pytest

from composition_gate.screen import CHECK_NAMES, render_screen, run_screen
from composition_gate.classification import ALLGREEN, classify_screen
from conftest import Sandbox, seed_mrec_candidate, seed_worked_node
from ht.commands import mrec
from ht.commands._common import Ctx
from ht.errors import HtError
from ht.cgate import execute_decision
from ht.mutex import global_mutex
from ht.paths import Root
from ht.pipeline import enforce_and_commit


def _create(
    sb: Sandbox,
    *,
    candidate_ref: str = "tree#L4/node#1",
    role: str = "harness",
    lane_verdict: str = "lane-pass",
    scope_lane: str | None = None,
    with_screen: bool = True,
):
    lane = scope_lane
    if lane is None:
        lane = (
            candidate_ref.removeprefix("tree#").split("/node#", 1)[0]
            if candidate_ref.startswith("tree#") and "/node#" in candidate_ref
            else "L4"
        )
    adjudication_ref = "tree#L4/adjudication#d-1-1-a1"
    if re.fullmatch(r"tree#[^/#\s]+/node#[1-9][0-9]*", candidate_ref):
        adjudication_ref = seed_mrec_candidate(sb, candidate_ref)
    args = [
        "mrec",
        "create",
        "--candidate-ref",
        candidate_ref,
        "--lane-verdict",
        lane_verdict,
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        lane,
    ]
    if with_screen:
        args.extend(
            [
                "--screen-result",
                "required-checks=pass:all required checks passed",
                "--screen-log-ref",
                "logs/screen-1.json",
            ]
        )
    return sb.run(*args, role=role)


def _record_ids(sb: Sandbox) -> list[str]:
    return sorted(
        path.stem
        for path in (sb.root / "tier1/merge-records").glob("MR-*.json")
    )


def _create_record(sb: Sandbox, *, candidate_ref: str = "tree#L4/node#1") -> str:
    result = _create(sb, candidate_ref=candidate_ref, with_screen=False)
    assert result.returncode == 0, result.stderr
    record_id = _record_ids(sb)[-1]
    screened = _screen(sb, record_id)
    assert screened.returncode == 0, screened.stderr
    return record_id


def _verdict(
    sb: Sandbox,
    record_id: str,
    verdict: str,
    *,
    role: str = "cgate",
    note: str | None = None,
):
    args = [
        "mrec",
        "verdict",
        "--record",
        record_id,
        "--verdict",
        verdict,
    ]
    if note is not None:
        args.extend(["--note", note])
    return sb.run(*args, role=role)


def _execute_auto(sb: Sandbox, record_id: str):
    previous = os.environ.get("HT_ROLE")
    os.environ["HT_ROLE"] = "cgate"
    try:
        return execute_decision(sb.root, record_id)
    finally:
        if previous is None:
            os.environ.pop("HT_ROLE", None)
        else:
            os.environ["HT_ROLE"] = previous


def _screen_output(sb: Sandbox, record_id: str) -> Path:
    path = sb.root / f"var/{record_id}-screen.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_screen(run_screen(sb.root, record_id)), encoding="utf-8")
    return path


def _screen(
    sb: Sandbox,
    record_id: str,
    *,
    role: str = "harness",
    log_ref: str = "var/screen-log.json",
):
    output = _screen_output(sb, record_id)
    log_path = sb.root / log_ref
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(output.read_bytes())
    return sb.run(
        "mrec",
        "screen",
        record_id,
        "--results-json",
        str(output),
        "--log-ref",
        log_ref,
        role=role,
    )


def test_harness_creates_canonical_merge_record_with_screen_result(
    sandbox: Sandbox,
):
    result = _create(sandbox)
    assert result.returncode == 0, result.stderr

    record = sandbox.load("tier1/merge-records/MR-1.json")
    claim = sandbox.load("trees/L4/nodes/1/node.json")["claims"][0]
    claim_hash = hashlib.sha256(
        json.dumps(
            claim,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert record == {
        "id": "MR-1",
        "candidate_ref": "tree#L4/node#1",
        "lane_verdict": "lane-pass",
        "lane_adjudication_ref": "tree#L4/adjudication#d-1-1-a1",
        "backing_claims": [
            {
                "ref": "tree#L4/claim#c-1-1",
                "sha256": claim_hash,
                "adjudication_ref": "tree#L4/adjudication#d-1-1-a1",
            }
        ],
        "scope": {"lane": "L4", "seats": [], "surfaces": [], "globs": []},
        "screen": {
            "results": [
                {
                    "check": "required-checks",
                    "result": "pass",
                    "detail": "all required checks passed",
                }
            ],
            "output_ref": None,
            "log_ref": None,
            "log_sha256": None,
            "output_sha256": None,
            "computed": None,
            "head_commit": None,
            "head_tree": None,
            "config_hash": None,
            "engine_version": None,
        },
        "gate_verdict": None,
        "watch_link": None,
        "created": record["created"],
        "consumed_epoch": None,
    }
    assert record["created"]


def test_mrec_create_requires_scope_and_can_birth_an_empty_screen(sandbox: Sandbox):
    missing_scope = sandbox.run(
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        "tree#L4/adjudication#d-1-1-a1",
        role="harness",
    )
    assert missing_scope.returncode != 0
    assert "--scope-lane" in missing_scope.stderr

    created = _create(sandbox, with_screen=False)
    assert created.returncode == 0, created.stderr
    assert sandbox.load("tier1/merge-records/MR-1.json")["screen"] == {
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

    orphan_log = sandbox.run(
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
        "--screen-log-ref",
        "var/orphan.json",
        role="harness",
    )
    assert orphan_log.returncode != 0
    assert "log ref requires supplied screen results" in orphan_log.stderr


def test_mrec_create_rejects_scope_lane_mismatch(sandbox: Sandbox):
    result = _create(sandbox, scope_lane="L5")
    assert result.returncode != 0
    assert "must equal candidate_ref lane 'L4'" in result.stderr
    assert _record_ids(sandbox) == []


def test_scope_lists_are_deduplicated_in_first_seen_order(sandbox: Sandbox):
    adjudication_ref = seed_mrec_candidate(sandbox, "tree#L4/node#1")
    result = sandbox.run(
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        "L4",
        "--scope-seat",
        "senior",
        "--scope-seat",
        "checker",
        "--scope-seat",
        "senior",
        "--scope-seat",
        "SENIOR",
        "--scope-surface",
        "merge queue",
        "--scope-surface",
        "merge queue",
        "--scope-surface",
        "MERGE QUEUE",
        "--scope-glob",
        "system/**/*.py",
        "--scope-glob",
        "tests/**/*.py",
        "--scope-glob",
        "system/**/*.py",
        "--scope-glob",
        "SYSTEM/**/*.PY",
        role="harness",
    )
    assert result.returncode == 0, result.stderr
    assert sandbox.load("tier1/merge-records/MR-1.json")["scope"] == {
        "lane": "L4",
        "seats": ["senior", "checker"],
        "surfaces": ["merge queue"],
        "globs": ["system/**/*.py", "tests/**/*.py"],
    }


def test_scope_lists_deduplicate_nfc_nfd_aliases(sandbox: Sandbox):
    adjudication_ref = seed_mrec_candidate(sandbox, "tree#L4/node#1")
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    result = sandbox.run(
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        "L4",
        "--scope-seat",
        nfc,
        "--scope-seat",
        nfd,
        "--scope-surface",
        nfc,
        "--scope-surface",
        nfd,
        "--scope-glob",
        f"{nfc}/*.py",
        "--scope-glob",
        f"{nfd}/*.py",
        role="harness",
    )
    assert result.returncode == 0, result.stderr
    scope = sandbox.load("tier1/merge-records/MR-1.json")["scope"]
    assert scope == {
        "lane": "L4",
        "seats": [nfc],
        "surfaces": [nfc],
        "globs": [f"{nfc}/*.py"],
    }


def test_scope_alias_dedup_preserves_first_semantic_occurrence(sandbox: Sandbox):
    adjudication_ref = seed_mrec_candidate(sandbox, "tree#L4/node#1")
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfc != nfd

    result = sandbox.run(
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        "L4",
        "--scope-seat",
        nfd,
        "--scope-seat",
        nfc,
        "--scope-seat",
        nfd.upper(),
        role="harness",
    )

    assert result.returncode == 0, result.stderr
    assert sandbox.load("tier1/merge-records/MR-1.json")["scope"]["seats"] == [nfd]


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--scope-glob", "/absolute/path/**"),
        ("--scope-glob", "../outside/**"),
        ("--scope-glob", "inside/../outside/**"),
        ("--scope-glob", "escaped\\path"),
        ("--scope-surface", "/absolute/surface"),
        ("--scope-surface", "group/../surface"),
        ("--scope-surface", "escaped\\surface"),
    ],
)
def test_mrec_create_rejects_path_shaped_scope_values(
    sandbox: Sandbox,
    flag: str,
    value: str,
):
    adjudication_ref = seed_mrec_candidate(sandbox, "tree#L4/node#1")
    result = sandbox.run(
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        "L4",
        flag,
        value,
        role="harness",
    )
    assert result.returncode != 0
    assert "must be relative" in result.stderr
    assert "may not contain '..' path segments or backslashes" in result.stderr
    assert _record_ids(sandbox) == []


def test_wrong_scope_is_bounced_to_a_new_record_not_edited(sandbox: Sandbox):
    adjudication_ref = seed_mrec_candidate(sandbox, "tree#L4/node#1")
    first = sandbox.run(
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        "L4",
        "--scope-seat",
        "wrong-seat",
        role="harness",
    )
    assert first.returncode == 0, first.stderr
    original = sandbox.load("tier1/merge-records/MR-1.json")

    replacement = sandbox.run(
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication_ref,
        "--scope-lane",
        "L4",
        "--scope-seat",
        "senior",
        role="harness",
    )
    assert replacement.returncode == 0, replacement.stderr
    assert sandbox.load("tier1/merge-records/MR-1.json") == original
    assert sandbox.load("tier1/merge-records/MR-2.json")["scope"]["seats"] == [
        "senior"
    ]


@pytest.mark.parametrize("role", ["cgate", "director", "verifier", "unit"])
def test_only_harness_can_create_merge_records(sandbox: Sandbox, role: str):
    result = _create(sandbox, role=role)
    assert result.returncode != 0
    assert "merge_record" in result.stderr
    assert "harness" in result.stderr
    assert _record_ids(sandbox) == []


def test_mrec_create_accepts_unique_bare_node_ref_and_canonicalizes(sandbox: Sandbox):
    seed_mrec_candidate(sandbox, "tree#L4/node#1")
    result = _create(sandbox, candidate_ref="node#1")
    assert result.returncode == 0, result.stderr
    assert _record_ids(sandbox) == ["MR-1"]
    assert sandbox.load("tier1/merge-records/MR-1.json")["candidate_ref"] == (
        "tree#L4/node#1"
    )


def test_mrec_ids_are_global_and_monotonic(sandbox: Sandbox):
    assert _create(sandbox, candidate_ref="tree#L4/node#1").returncode == 0
    assert _create(sandbox, candidate_ref="tree#L5/node#7").returncode == 0
    assert _record_ids(sandbox) == ["MR-1", "MR-2"]
    assert sandbox.load("tier1/merge-records/MR-2.json")["candidate_ref"] == (
        "tree#L5/node#7"
    )


def test_harness_screen_replaces_complete_identity_while_verdict_is_null(sandbox: Sandbox):
    record_id = _create_record(sandbox)
    first = _screen(sandbox, record_id, log_ref="var/screen-1.json")
    assert first.returncode == 0, first.stderr
    stored = sandbox.load(f"tier1/merge-records/{record_id}.json")["screen"]
    assert [row["check"] for row in stored["results"]] == list(CHECK_NAMES)
    assert {row["result"] for row in stored["results"]} <= {"pass", "n/a"}
    assert stored["log_ref"] == "var/screen-1.json"
    first_head = stored["head_commit"]

    second = _screen(sandbox, record_id, log_ref="var/screen-2.json")
    assert second.returncode == 0, second.stderr
    stored = sandbox.load(f"tier1/merge-records/{record_id}.json")["screen"]
    assert [row["check"] for row in stored["results"]] == list(CHECK_NAMES)
    assert {row["result"] for row in stored["results"]} <= {"pass", "n/a"}
    assert stored["head_commit"] != first_head
    assert stored["log_ref"] == "var/screen-2.json"
    assert stored["output_ref"] == "var/MR-1-screen.json"
    assert stored["output_sha256"] == hashlib.sha256(
        (sandbox.root / "var/MR-1-screen.json").read_bytes()
    ).hexdigest()
    assert stored["log_sha256"] == hashlib.sha256(
        (sandbox.root / "var/screen-2.json").read_bytes()
    ).hexdigest()


def test_screen_transcription_persists_identity_and_classifies_allgreen(
    sandbox: Sandbox,
):
    created = _create(sandbox, with_screen=False)
    assert created.returncode == 0, created.stderr
    record_id = "MR-1"
    output = sandbox.root / f"var/{record_id}-screen.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_screen(run_screen(sandbox.root, record_id)), encoding="utf-8")
    log = sandbox.root / "var/cgate.log"
    log.write_bytes(output.read_bytes())
    transcribed = sandbox.run(
        "mrec",
        "screen",
        record_id,
        "--results-json",
        str(output),
        "--log-ref",
        "var/cgate.log",
        role="harness",
    )
    assert transcribed.returncode == 0, transcribed.stderr
    stored = sandbox.load(f"tier1/merge-records/{record_id}.json")["screen"]
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert stored["output_ref"] == f"var/{record_id}-screen.json"
    assert stored["log_ref"] == "var/cgate.log"
    assert stored["computed"] == payload["computed"]
    assert stored["head_commit"] == payload["head_commit"]
    assert stored["head_tree"] == payload["head_tree"]
    assert stored["config_hash"] == payload["config_hash"]
    assert stored["engine_version"] == payload["engine_version"]
    assert stored["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert stored["log_sha256"] == hashlib.sha256(log.read_bytes()).hexdigest()
    assert classify_screen(sandbox.root, sandbox.load(f"tier1/merge-records/{record_id}.json")) == ALLGREEN


def test_screen_can_use_one_exact_file_for_output_and_log(sandbox: Sandbox):
    created = _create(sandbox, with_screen=False)
    assert created.returncode == 0, created.stderr
    record_id = "MR-1"
    output = _screen_output(sandbox, record_id)
    screened = sandbox.run(
        "mrec",
        "screen",
        record_id,
        "--results-json",
        str(output),
        "--log-ref",
        f"var/{record_id}-screen.json",
        role="harness",
    )
    assert screened.returncode == 0, screened.stderr
    stored = sandbox.load(f"tier1/merge-records/{record_id}.json")["screen"]
    assert stored["output_ref"] == stored["log_ref"] == f"var/{record_id}-screen.json"
    assert stored["output_sha256"] == stored["log_sha256"]


def test_screen_commits_w2_error_identity_but_it_is_never_verdictable(
    sandbox: Sandbox,
):
    created = _create(sandbox, with_screen=False)
    assert created.returncode == 0, created.stderr
    non_repo = sandbox.root.parent / "non-repository-input"
    non_repo.mkdir(parents=True)
    output = sandbox.root / "var/MR-1-error-screen.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_screen(run_screen(non_repo, "MR-1")), encoding="utf-8")
    screened = sandbox.run(
        "mrec",
        "screen",
        "MR-1",
        "--results-json",
        str(output),
        "--log-ref",
        "var/MR-1-error-screen.json",
        role="harness",
    )
    assert screened.returncode == 0, screened.stderr
    record = sandbox.load("tier1/merge-records/MR-1.json")
    assert record["screen"]["head_commit"] is None
    assert record["screen"]["head_tree"] is None
    assert classify_screen(sandbox.root, record) == "invalid"
    rejected = _verdict(sandbox, "MR-1", "land")
    assert rejected.returncode != 0
    assert "[R-i7-9" in rejected.stderr


def test_screen_plan_rejects_evidence_mutated_before_atomic_commit(sandbox: Sandbox):
    created = _create(sandbox, with_screen=False)
    assert created.returncode == 0, created.stderr
    output = _screen_output(sandbox, "MR-1")
    log = sandbox.root / "var/stable.log"
    log.write_bytes(output.read_bytes())
    plan = mrec.screen(
        Ctx(Root(sandbox.root), "harness"),
        "MR-1",
        str(output),
        "var/stable.log",
    )
    output.write_bytes(output.read_bytes() + b"\n")
    assert plan.semantic is not None
    with pytest.raises(HtError, match="screen evidence changed before commit"):
        plan.semantic()


def test_screen_plan_rejects_separate_log_mutated_before_atomic_commit(
    sandbox: Sandbox,
):
    created = _create(sandbox, with_screen=False)
    assert created.returncode == 0, created.stderr
    output = _screen_output(sandbox, "MR-1")
    log = sandbox.root / "var/separate.log"
    log.write_bytes(output.read_bytes())
    plan = mrec.screen(
        Ctx(Root(sandbox.root), "harness"),
        "MR-1",
        str(output),
        "var/separate.log",
    )

    log.write_bytes(log.read_bytes() + b"\n")

    head_before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    with pytest.raises(HtError, match="screen evidence changed before commit"):
        enforce_and_commit(Root(sandbox.root), plan)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == head_before
    assert sandbox.load("tier1/merge-records/MR-1.json")["screen"]["results"] == []


def test_screen_plan_rejects_physical_head_move_before_atomic_commit(
    sandbox: Sandbox,
):
    created = _create(sandbox, with_screen=False)
    assert created.returncode == 0, created.stderr
    output = _screen_output(sandbox, "MR-1")
    log = sandbox.root / "var/stable.log"
    log.write_bytes(output.read_bytes())
    plan = mrec.screen(
        Ctx(Root(sandbox.root), "harness"),
        "MR-1",
        str(output),
        "var/stable.log",
    )
    moved = sandbox.run(
        "tree",
        "init",
        "synthetic",
        "--root-question",
        "Synthetic post-plan head move",
        role="director",
    )
    assert moved.returncode == 0, moved.stderr

    moved_head = sandbox.git("rev-parse", "HEAD").stdout.strip()
    with pytest.raises(HtError, match="physical HEAD moved during screen transcription"):
        enforce_and_commit(Root(sandbox.root), plan)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == moved_head
    assert sandbox.load("tier1/merge-records/MR-1.json")["screen"]["results"] == []


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--results-json", "../outside-output.json"),
        ("--log-ref", "../outside-log.json"),
    ],
)
def test_screen_rejects_evidence_path_escape(sandbox: Sandbox, flag: str, value: str):
    record_id = _create_record(sandbox)
    output = _screen_output(sandbox, record_id)
    log = sandbox.root / "var/escape-log.json"
    log.write_bytes(output.read_bytes())
    args = [
        "mrec",
        "screen",
        record_id,
        "--results-json",
        str(output),
        "--log-ref",
        "var/escape-log.json",
    ]
    index = args.index(flag)
    args[index + 1] = value
    rejected = sandbox.run(*args, role="harness")
    assert rejected.returncode != 0
    assert "below the research root's var/" in rejected.stderr


def test_screen_rejects_output_computed_at_a_moved_head(sandbox: Sandbox):
    record_id = _create_record(sandbox)
    output = _screen_output(sandbox, record_id)
    moved = sandbox.run(
        "tree",
        "init",
        "synthetic",
        "--root-question",
        "Synthetic moved head",
        role="director",
    )
    assert moved.returncode == 0, moved.stderr
    log = sandbox.root / "var/moved-head-log.json"
    log.write_bytes(output.read_bytes())
    rejected = sandbox.run(
        "mrec",
        "screen",
        record_id,
        "--results-json",
        str(output),
        "--log-ref",
        "var/moved-head-log.json",
        role="harness",
    )
    assert rejected.returncode != 0
    assert "different physical HEAD" in rejected.stderr


def test_screen_requires_harness_role(sandbox: Sandbox):
    record_id = _create_record(sandbox)
    rejected = _screen(
        sandbox,
        record_id,
        role="cgate",
        log_ref="var/cgate-screen.json",
    )
    assert rejected.returncode != 0
    assert "merge_record.screen" in rejected.stderr
    assert "harness" in rejected.stderr


def test_screen_rejects_empty_results(sandbox: Sandbox):
    record_id = _create_record(sandbox)
    output = sandbox.root / "var/empty-screen.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"record_id": record_id, "results": []}))
    before = sandbox.load(f"tier1/merge-records/{record_id}.json")
    rejected = sandbox.run(
        "mrec",
        "screen",
        record_id,
        "--results-json",
        str(output),
        "--log-ref",
        "var/empty-screen.json",
        role="harness",
    )
    assert rejected.returncode != 0
    assert "requires at least one result" in rejected.stderr
    assert sandbox.load(f"tier1/merge-records/{record_id}.json") == before


def test_screen_rejects_fabricated_one_check_result(sandbox: Sandbox):
    created = _create(sandbox, with_screen=False)
    assert created.returncode == 0, created.stderr
    output = sandbox.root / "var/forged-screen.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "record_id": "MR-1",
                "results": [
                    {
                        "check": "scope-overlap",
                        "result": "pass",
                        "detail": "fabricated",
                        "inputs": {"source": "fabricated"},
                    }
                ],
            }
        )
    )

    rejected = sandbox.run(
        "mrec",
        "screen",
        "MR-1",
        "--results-json",
        str(output),
        "--log-ref",
        "var/forged-screen.json",
        role="harness",
    )
    assert rejected.returncode != 0
    assert "exactly the five canonical checks in engine order" in rejected.stderr
    assert sandbox.load("tier1/merge-records/MR-1.json")["screen"]["results"] == []


@pytest.mark.parametrize("detail", ["", "   \t"])
def test_blank_detail_is_rejected_by_screen_and_verdict_seams(
    sandbox: Sandbox,
    detail: str,
):
    created = _create(sandbox, with_screen=False)
    assert created.returncode == 0, created.stderr
    output = _screen_output(sandbox, "MR-1")
    payload = json.loads(output.read_text())
    payload["results"][-1]["result"] = "n/a"
    payload["results"][-1]["detail"] = detail
    output.write_text(json.dumps(payload), encoding="utf-8")

    rejected_screen = sandbox.run(
        "mrec",
        "screen",
        "MR-1",
        "--results-json",
        str(output),
        "--log-ref",
        "var/blank-detail.json",
        role="harness",
    )
    assert rejected_screen.returncode != 0
    assert "requires non-empty detail" in rejected_screen.stderr

    record = sandbox.load("tier1/merge-records/MR-1.json")
    record["screen"] = {
        "results": payload["results"],
        "log_ref": "var/blank-detail.json",
    }
    sandbox.write_file("tier1/merge-records/MR-1.json", json.dumps(record, indent=2) + "\n")
    added = sandbox.git("add", "tier1/merge-records/MR-1.json")
    assert added.returncode == 0, added.stderr
    committed = sandbox.git("commit", "--no-verify", "-m", "seed blank detail")
    assert committed.returncode == 0, committed.stderr

    rejected_verdict = _verdict(sandbox, "MR-1", "land")
    assert rejected_verdict.returncode != 0
    assert "direct ht mrec verdict is disabled" in rejected_verdict.stderr
    assert "ht-cgate decide" in rejected_verdict.stderr
    assert sandbox.load("tier1/merge-records/MR-1.json")["gate_verdict"] is None


def test_screen_is_frozen_after_gate_verdict(sandbox: Sandbox):
    record_id = _create_record(sandbox)
    _execute_auto(sandbox, record_id)
    before = sandbox.load(f"tier1/merge-records/{record_id}.json")

    rejected = _screen(sandbox, record_id)
    assert rejected.returncode != 0
    assert "frozen after verdict" in rejected.stderr
    assert "D2" in rejected.stderr
    assert sandbox.load(f"tier1/merge-records/{record_id}.json") == before


def test_screen_is_rejected_on_consumed_record(tree: Sandbox):
    seed_worked_node(tree)
    record_id = _create_record(tree)
    _execute_auto(tree, record_id)
    merged = tree.run(
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
    assert merged.returncode == 0, merged.stderr
    before = tree.load(f"tier1/merge-records/{record_id}.json")

    rejected = _screen(tree, record_id)
    assert rejected.returncode != 0
    assert "cannot screen consumed merge record" in rejected.stderr
    assert tree.load(f"tier1/merge-records/{record_id}.json") == before


def test_verdict_rejects_birth_record_with_empty_screen(sandbox: Sandbox):
    created = _create(sandbox, with_screen=False)
    assert created.returncode == 0, created.stderr
    rejected = _verdict(sandbox, "MR-1", "land")
    assert rejected.returncode != 0
    assert "direct ht mrec verdict is disabled" in rejected.stderr
    assert "ht-cgate decide" in rejected.stderr


def test_verdict_rejects_legacy_birth_screen_without_input_citations(
    sandbox: Sandbox,
):
    created = _create(sandbox)
    assert created.returncode == 0, created.stderr
    rejected = _verdict(sandbox, "MR-1", "land")
    assert rejected.returncode != 0
    assert "direct ht mrec verdict is disabled" in rejected.stderr
    assert "ht-cgate decide" in rejected.stderr
    assert sandbox.load("tier1/merge-records/MR-1.json")["gate_verdict"] is None


def test_mrec_screen_reports_global_mutex_contention_cleanly(sandbox: Sandbox):
    record_id = _create_record(sandbox)
    with global_mutex(Root(sandbox.root)):
        rejected = _screen(sandbox, record_id)
    assert rejected.returncode != 0
    assert "global merge/ledger mutex is contended" in rejected.stderr


@pytest.mark.parametrize(
    "value",
    [
        "land",
        "land-after-MR-7",
        "consolidate-first",
        "bounce-for-surface-rework",
        "hold",
        "escalate-to-user",
        "escalate-stuck",
        "future-item-7-value",
    ],
)
def test_direct_mrec_verdict_rejects_every_value(
    sandbox: Sandbox, value: str
):
    record_id = _create_record(sandbox)
    result = _verdict(sandbox, record_id, value, note="composition gate result")
    assert result.returncode != 0
    assert "direct ht mrec verdict is disabled" in result.stderr
    assert "ht-cgate decide" in result.stderr
    assert sandbox.load(f"tier1/merge-records/{record_id}.json")["gate_verdict"] is None


def test_verdict_requires_an_atomic_nonempty_value(sandbox: Sandbox):
    record_id = _create_record(sandbox)

    missing = sandbox.run(
        "mrec", "verdict", "--record", record_id, role="cgate"
    )
    assert missing.returncode != 0
    assert "--verdict" in missing.stderr

    empty = _verdict(sandbox, record_id, "")
    assert empty.returncode != 0
    assert "direct ht mrec verdict is disabled" in empty.stderr
    assert sandbox.load(f"tier1/merge-records/{record_id}.json")["gate_verdict"] is None


def test_second_cgate_verdict_is_frozen(sandbox: Sandbox):
    record_id = _create_record(sandbox)
    _execute_auto(sandbox, record_id)
    before = sandbox.load(f"tier1/merge-records/{record_id}.json")

    second = _verdict(sandbox, record_id, "hold")
    assert second.returncode != 0
    assert "direct ht mrec verdict is disabled" in second.stderr
    assert sandbox.load(f"tier1/merge-records/{record_id}.json") == before


@pytest.mark.parametrize("role", ["harness", "director", "verifier", "unit"])
def test_non_cgate_cannot_fill_verdict(sandbox: Sandbox, role: str):
    record_id = _create_record(sandbox)
    result = _verdict(sandbox, record_id, "land", role=role)
    assert result.returncode != 0
    assert "direct ht mrec verdict is disabled" in result.stderr
    assert "ht-cgate decide" in result.stderr
    assert sandbox.load(f"tier1/merge-records/{record_id}.json")["gate_verdict"] is None


def test_verdict_on_consumed_record_is_rejected(tree: Sandbox):
    seed_worked_node(tree)
    record_id = _create_record(tree)
    _execute_auto(tree, record_id)
    merged = tree.run(
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
    assert merged.returncode == 0, merged.stderr
    consumed = tree.load(f"tier1/merge-records/{record_id}.json")
    assert consumed["consumed_epoch"] is not None

    replay = _verdict(tree, record_id, "hold")
    assert replay.returncode != 0
    assert "direct ht mrec verdict is disabled" in replay.stderr
    assert tree.load(f"tier1/merge-records/{record_id}.json") == consumed


def test_mrec_create_reports_global_mutex_contention_cleanly(sandbox: Sandbox):
    lock_path = Path(sandbox.root, ".ht-global.lock")
    with global_mutex(Root(sandbox.root)):
        result = _create(sandbox)

    assert result.returncode != 0
    assert "global merge/ledger mutex is contended" in result.stderr
    assert lock_path.exists()
    assert _record_ids(sandbox) == []
