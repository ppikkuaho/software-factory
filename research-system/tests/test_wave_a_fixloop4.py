"""Public regressions for Wave-A issue-close exact cohort closure."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from conftest import Sandbox
from ht import schemas
from ht.commands import issue as issue_command
from ht.commands._common import Ctx
from ht.errors import HtError
from ht.paths import Root


ISSUE = "tier1/issues/I-1.json"
NODE = "trees/L4/nodes/1/node.json"
DISPATCH = "trees/L4/nodes/1/dispatches/d-1-1.json"
SECOND_DISPATCH = "trees/L4/nodes/1/dispatches/d-1-2.json"


def _assert_controlled_rejection(result) -> None:
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def _file_state(path: Path) -> tuple[str, int, bytes] | tuple[str]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ("missing",)
    assert stat.S_ISREG(metadata.st_mode)
    return "regular", stat.S_IMODE(metadata.st_mode), path.read_bytes()


def _snapshot(sb: Sandbox, relatives: tuple[str, ...]) -> tuple:
    return (
        sb.git("rev-parse", "HEAD").stdout,
        sb.git("status", "--porcelain=v2", "-z").stdout,
        _file_state(sb.root / ISSUE),
        tuple((relative, _file_state(sb.root / relative)) for relative in relatives),
        tuple(
            (
                relative,
                sb.git("ls-files", "--stage", "-z", "--", relative).stdout,
                sb.git("ls-tree", "-z", "HEAD", "--", relative).stdout,
            )
            for relative in relatives
        ),
    )


def _run_ok(result) -> None:
    assert result.returncode == 0, result.stderr


def _seed_dispatch(sb: Sandbox, *, terminal: bool) -> None:
    _run_ok(
        sb.run(
            "issue",
            "mint",
            "--title",
            "SYNTHETIC-FIXLOOP4-ISSUE",
            "--question",
            "SYNTHETIC-FIXLOOP4-QUESTION",
            "--done-definition",
            "SYNTHETIC-FIXLOOP4-DONE",
            "--provenance",
            "user-seed#synthetic-fixloop4",
            "--lanes",
            "L4",
            role="pc",
        )
    )
    _run_ok(sb.run("issue", "ratify", "--issue", "I-1", role="user"))
    _run_ok(sb.run("issue", "activate", "--issue", "I-1", role="user"))
    _run_ok(
        sb.run(
            "tree",
            "init",
            "L4",
            "--root-question",
            "SYNTHETIC-FIXLOOP4-ROOT",
            role="director",
        )
    )
    _run_ok(
        sb.run(
            "node",
            "mint",
            "--tree",
            "L4",
            "--root",
            "--premise",
            "SYNTHETIC-FIXLOOP4-PREMISE",
            "--from-issue",
            "I-1",
            "--rationale",
            "SYNTHETIC-FIXLOOP4-RATIONALE",
            role="director",
        )
    )
    _run_ok(
        sb.run(
            "dispatch",
            "create",
            "--tree",
            "L4",
            "--node",
            "1",
            "--question",
            "SYNTHETIC-FIXLOOP4-DISPATCH",
            "--done-definition",
            "SYNTHETIC-FIXLOOP4-DISPATCH-DONE",
            "--issue-ref",
            "I-1",
            role="director",
        )
    )
    if terminal:
        _run_ok(
            sb.run(
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


def _valid_second_dispatch(sb: Sandbox) -> bytes:
    document = sb.load(DISPATCH)
    document["id"] = "d-1-2"
    document["question"] = "SYNTHETIC-FIXLOOP4-WORKING-ONLY"
    schemas.validate(sb.root / "system/schemas", "dispatch", document)
    return (json.dumps(document, indent=2) + "\n").encode()


def _close(sb: Sandbox):
    return sb.run(
        "issue",
        "close",
        "--issue",
        "I-1",
        "--text",
        "SYNTHETIC-FIXLOOP4-CLOSE",
        role="pc",
    )


@pytest.mark.parametrize("corruption", ["tracked-working-missing", "working-only"])
def test_issue_close_requires_working_index_head_dispatch_set_equality(
    sandbox: Sandbox,
    corruption: str,
) -> None:
    _seed_dispatch(sandbox, terminal=True)
    if corruption == "tracked-working-missing":
        (sandbox.root / DISPATCH).unlink()
    else:
        (sandbox.root / SECOND_DISPATCH).write_bytes(_valid_second_dispatch(sandbox))
    relatives = (NODE, DISPATCH, SECOND_DISPATCH)
    before = _snapshot(sandbox, relatives)

    rejected = _close(sandbox)

    _assert_controlled_rejection(rejected)
    assert "exact committed cohort" in rejected.stderr
    assert _snapshot(sandbox, relatives) == before


@pytest.mark.parametrize(
    "corruption",
    ["dirty-content", "staged-content", "staged-add", "staged-delete", "staged-mode"],
)
def test_issue_close_rejects_dirty_or_staged_dispatch_identity(
    sandbox: Sandbox,
    corruption: str,
) -> None:
    _seed_dispatch(sandbox, terminal=True)
    dispatch_path = sandbox.root / DISPATCH
    if corruption in {"dirty-content", "staged-content"}:
        document = sandbox.load(DISPATCH)
        document["outcome"] = "recalled"
        dispatch_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        if corruption == "staged-content":
            _run_ok(sandbox.git("add", "--", DISPATCH))
    elif corruption == "staged-add":
        (sandbox.root / SECOND_DISPATCH).write_bytes(_valid_second_dispatch(sandbox))
        _run_ok(sandbox.git("add", "--", SECOND_DISPATCH))
    elif corruption == "staged-delete":
        _run_ok(sandbox.git("rm", "--", DISPATCH))
    else:
        _run_ok(sandbox.git("update-index", "--chmod=+x", "--", DISPATCH))
    relatives = (NODE, DISPATCH, SECOND_DISPATCH)
    before = _snapshot(sandbox, relatives)

    rejected = _close(sandbox)

    _assert_controlled_rejection(rejected)
    assert _snapshot(sandbox, relatives) == before


@pytest.mark.parametrize("change", ["membership", "identity"])
def test_issue_close_semantic_recheck_repeats_cohort_and_blob_proof(
    sandbox: Sandbox,
    change: str,
) -> None:
    _seed_dispatch(sandbox, terminal=True)
    plan = issue_command.close(
        Ctx(Root(sandbox.root), "pc"),
        "I-1",
        "SYNTHETIC-FIXLOOP4-SEMANTIC",
    )
    assert plan.semantic is not None
    if change == "membership":
        (sandbox.root / SECOND_DISPATCH).write_bytes(_valid_second_dispatch(sandbox))
    else:
        document = sandbox.load(DISPATCH)
        document["outcome"] = "recalled"
        (sandbox.root / DISPATCH).write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
    relatives = (NODE, DISPATCH, SECOND_DISPATCH)
    before = _snapshot(sandbox, relatives)

    with pytest.raises(HtError):
        plan.semantic()

    assert _snapshot(sandbox, relatives) == before


def test_issue_close_rejects_uncommitted_demotion_source_node_status(
    sandbox: Sandbox,
) -> None:
    _seed_dispatch(sandbox, terminal=False)
    source = sandbox.root.parent / "synthetic-fixloop4-report.md"
    source.write_text("# SYNTHETIC FIXLOOP4 REPORT\nline two\n", encoding="utf-8")
    _run_ok(
        sandbox.run(
            "report",
            "submit",
            "--tree",
            "L4",
            "--dispatch",
            "d-1-1",
            "--src",
            str(source),
            role="unit",
        )
    )
    _run_ok(
        sandbox.run(
            "claim",
            "grant",
            "--tree",
            "L4",
            "--dispatch",
            "d-1-1",
            "--text",
            "SYNTHETIC-FIXLOOP4-CLAIM",
            "--proposed-tier",
            "2",
            "--granted-tier",
            "2",
            "--standing-class",
            "trunk",
            "--anchor",
            "trees/L4/nodes/1/reports/d-1-1-report.md:1:1",
            role="verifier",
        )
    )
    _run_ok(
        sandbox.run(
            "node",
            "park",
            "--tree",
            "L4",
            "--node",
            "1",
            "--rationale",
            "SYNTHETIC-FIXLOOP4-PARK",
            role="director",
        )
    )
    _run_ok(
        sandbox.run(
            "settle",
            "--tree",
            "L4",
            "--node",
            "1",
            "--resolution",
            "demoted",
            role="director",
        )
    )
    demoted_bytes = (sandbox.root / NODE).read_bytes()
    committed_nondemoted_oid = sandbox.git(
        "rev-parse", f"HEAD~2:{NODE}"
    ).stdout.strip()
    assert committed_nondemoted_oid
    _run_ok(
        sandbox.git(
            "update-index",
            "--cacheinfo",
            "100644",
            committed_nondemoted_oid,
            NODE,
        )
    )
    _run_ok(sandbox.git("commit", "--no-verify", "-m", "synthetic node baseline"))
    assert (sandbox.root / NODE).read_bytes() == demoted_bytes
    relatives = (NODE, DISPATCH)
    before = _snapshot(sandbox, relatives)

    rejected = _close(sandbox)

    _assert_controlled_rejection(rejected)
    assert "demotion-source node" in rejected.stderr
    assert _snapshot(sandbox, relatives) == before
