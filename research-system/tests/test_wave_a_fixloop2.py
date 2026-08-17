"""Adversarial black-box regressions for Wave-A fix-loop 2."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from conftest import (
    Sandbox,
    finalize_gate_decision,
    seed_mrec_candidate,
    transcribe_engine_screen,
)


def _assert_controlled_rejection(result) -> None:
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def _git_state(sb: Sandbox) -> tuple[str, str]:
    return (
        sb.git("rev-parse", "HEAD").stdout,
        sb.git("status", "--porcelain=v2", "-z").stdout,
    )


def _mint_active_issue(sb: Sandbox) -> None:
    minted = sb.run(
        "issue",
        "mint",
        "--title",
        "SYNTHETIC-FIXLOOP-ISSUE",
        "--question",
        "SYNTHETIC-FIXLOOP-QUESTION",
        "--done-definition",
        "SYNTHETIC-FIXLOOP-DONE",
        "--provenance",
        "user-seed#synthetic-fixloop",
        "--lanes",
        "L4",
        role="pc",
    )
    assert minted.returncode == 0, minted.stderr
    assert sb.run("issue", "ratify", "--issue", "I-1", role="user").returncode == 0
    assert sb.run("issue", "activate", "--issue", "I-1", role="user").returncode == 0


def _seed_affiliated_dispatch(sb: Sandbox) -> str:
    _mint_active_issue(sb)
    assert sb.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "SYNTHETIC-FIXLOOP-ROOT",
        role="director",
    ).returncode == 0
    assert sb.run(
        "node",
        "mint",
        "--tree",
        "L4",
        "--root",
        "--premise",
        "SYNTHETIC-FIXLOOP-PREMISE",
        "--from-issue",
        "I-1",
        "--rationale",
        "SYNTHETIC-FIXLOOP-RATIONALE",
        role="director",
    ).returncode == 0
    assert sb.run(
        "dispatch",
        "create",
        "--tree",
        "L4",
        "--node",
        "1",
        "--question",
        "SYNTHETIC-FIXLOOP-DISPATCH",
        "--done-definition",
        "SYNTHETIC-FIXLOOP-DISPATCH-DONE",
        "--issue-ref",
        "I-1",
        role="director",
    ).returncode == 0
    source = sb.root.parent / "synthetic-fixloop-report.md"
    source.write_text("# SYNTHETIC FIXLOOP REPORT\nline two\n", encoding="utf-8")
    submitted = sb.run(
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
    assert submitted.returncode == 0, submitted.stderr
    return "trees/L4/nodes/1/reports/d-1-1-report.md:1:1"


def _seed_ready_dispatch(sb: Sandbox) -> str:
    assert sb.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "SYNTHETIC-CLAIM-ROOT",
        role="director",
    ).returncode == 0
    assert sb.run(
        "node",
        "mint",
        "--tree",
        "L4",
        "--root",
        "--premise",
        "SYNTHETIC-CLAIM-PREMISE",
        "--rationale",
        "SYNTHETIC-CLAIM-RATIONALE",
        role="director",
    ).returncode == 0
    assert sb.run(
        "dispatch",
        "create",
        "--tree",
        "L4",
        "--node",
        "1",
        "--question",
        "SYNTHETIC-CLAIM-DISPATCH",
        "--done-definition",
        "SYNTHETIC-CLAIM-DONE",
        role="director",
    ).returncode == 0
    source = sb.root.parent / "synthetic-claim-report.md"
    source.write_text("# SYNTHETIC CLAIM REPORT\nline two\n", encoding="utf-8")
    submitted = sb.run(
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
    assert submitted.returncode == 0, submitted.stderr
    return "trees/L4/nodes/1/reports/d-1-1-report.md:1:1"


def _create_land_record(sb: Sandbox) -> str:
    adjudication = seed_mrec_candidate(sb, "tree#L4/node#1")
    created = sb.run(
        "mrec",
        "create",
        "--candidate-ref",
        "tree#L4/node#1",
        "--lane-verdict",
        "lane-pass",
        "--lane-adjudication-ref",
        adjudication,
        "--scope-lane",
        "L4",
        "--screen-result",
        "required-checks=pass",
        role="harness",
    )
    assert created.returncode == 0, created.stderr
    screened = transcribe_engine_screen(sb, "MR-1")
    assert screened.returncode == 0, screened.stderr
    finalize_gate_decision(sb, "MR-1")
    return "MR-1"


@pytest.mark.parametrize("corruption", ["symlink", "document-id"])
def test_node_merge_strictly_resolves_canonical_merge_record_before_plan(
    sandbox: Sandbox,
    corruption: str,
) -> None:
    record_id = _create_land_record(sandbox)
    record_path = sandbox.root / f"tier1/merge-records/{record_id}.json"
    external = sandbox.root.parent / f"{corruption}-merge-record.json"
    external.write_bytes(record_path.read_bytes())
    if corruption == "symlink":
        record_path.unlink()
        record_path.symlink_to(external)
    else:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["id"] = "MR-999"
        record_path.write_text(json.dumps(record), encoding="utf-8")
    before = (_git_state(sandbox), external.read_bytes(), record_path.read_bytes())

    rejected = sandbox.run(
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

    _assert_controlled_rejection(rejected)
    assert (_git_state(sandbox), external.read_bytes(), record_path.read_bytes()) == before


@pytest.mark.parametrize("operation", ["subgoal-add", "observatory-attach", "close"])
def test_issue_mutations_strictly_resolve_symlinked_target_before_plan(
    sandbox: Sandbox,
    operation: str,
) -> None:
    _mint_active_issue(sandbox)
    if operation == "subgoal-add":
        sb_result = sandbox.run(
            "tree",
            "init",
            "L4",
            "--root-question",
            "SYNTHETIC-ISSUE-TARGET",
            role="director",
        )
        assert sb_result.returncode == 0, sb_result.stderr
        minted = sandbox.run(
            "node",
            "mint",
            "--tree",
            "L4",
            "--root",
            "--premise",
            "SYNTHETIC-ISSUE-NODE",
            "--from-issue",
            "I-1",
            "--rationale",
            "SYNTHETIC-ISSUE-RATIONALE",
            role="director",
        )
        assert minted.returncode == 0, minted.stderr
        extra = ("--ref", "tree#L4/node#1")
    elif operation == "observatory-attach":
        source = sandbox.root.parent / "synthetic-issue-observatory.md"
        source.write_text("# SYNTHETIC ISSUE OBSERVATORY\n", encoding="utf-8")
        registered = sandbox.run(
            "observatory",
            "register",
            "--run-id",
            "synthetic-issue-run",
            "--report-card",
            str(source),
            role="harness",
        )
        assert registered.returncode == 0, registered.stderr
        extra = ("--ref", "observatory-report#synthetic-issue-run")
    else:
        extra = ("--text", "SYNTHETIC ISSUE CLOSE")

    issue_path = sandbox.root / "tier1/issues/I-1.json"
    external = sandbox.root.parent / f"{operation}-issue-target.json"
    external.write_bytes(issue_path.read_bytes())
    issue_path.unlink()
    issue_path.symlink_to(external)
    before = (_git_state(sandbox), external.read_bytes())

    rejected = sandbox.run(
        "issue",
        operation,
        "--issue",
        "I-1",
        *extra,
        role="pc",
    )

    _assert_controlled_rejection(rejected)
    assert (_git_state(sandbox), external.read_bytes()) == before


@pytest.mark.parametrize("granted_tier", [1, 2])
def test_claim_grant_and_demotion_strictly_resolve_containing_node(
    sandbox: Sandbox,
    granted_tier: int,
) -> None:
    anchor = _seed_ready_dispatch(sandbox)
    node_path = sandbox.root / "trees/L4/nodes/1/node.json"
    external = sandbox.root.parent / f"grant-{granted_tier}-node.json"
    external.write_bytes(node_path.read_bytes())
    node_path.unlink()
    node_path.symlink_to(external)
    before = (_git_state(sandbox), external.read_bytes())
    args = [
        "claim",
        "grant",
        "--tree",
        "L4",
        "--dispatch",
        "d-1-1",
        "--text",
        "SYNTHETIC-CLAIM-TEXT",
        "--proposed-tier",
        "2",
        "--granted-tier",
        str(granted_tier),
        "--standing-class",
        "trunk",
        "--anchor",
        anchor,
    ]
    if granted_tier == 1:
        args.extend(("--reason", "SYNTHETIC-DEMOTION-REASON"))

    rejected = sandbox.run(*args, role="verifier")

    _assert_controlled_rejection(rejected)
    assert (_git_state(sandbox), external.read_bytes()) == before
    assert not (
        sandbox.root / "trees/L4/nodes/1/adjudications/d-1-1-a1.md"
    ).exists()


def _register_observatory_fixture(sb: Sandbox) -> tuple[str, Path, bytes]:
    _mint_active_issue(sb)
    source = sb.root.parent / "synthetic-pin-report-card.md"
    content = b"# SYNTHETIC PIN REPORT CARD\n"
    source.write_bytes(content)
    registered = sb.run(
        "observatory",
        "register",
        "--run-id",
        "synthetic-pin-run",
        "--report-card",
        str(source),
        role="harness",
    )
    assert registered.returncode == 0, registered.stderr
    relative = "readout/observatory/synthetic-pin-run/report-card.md"
    return relative, sb.root / relative, content


@pytest.mark.parametrize(
    "corruption",
    [
        "working-executable",
        "working-symlink",
        "index-executable",
        "index-symlink",
        "index-gitlink",
        "head-executable",
        "head-symlink",
        "head-gitlink",
    ],
)
def test_observatory_attachment_requires_exact_regular_blob_identity(
    sandbox: Sandbox,
    corruption: str,
) -> None:
    relative, canonical, content = _register_observatory_fixture(sandbox)
    external: Path | None = None
    if corruption == "working-executable":
        canonical.chmod(0o755)
    elif corruption == "working-symlink":
        external = sandbox.root.parent / "synthetic-pin-external.md"
        external.write_bytes(content)
        canonical.unlink()
        canonical.symlink_to(external)
    elif corruption == "index-executable":
        changed = sandbox.git("update-index", "--chmod=+x", "--", relative)
        assert changed.returncode == 0, changed.stderr
    elif corruption == "index-symlink":
        oid = sandbox.git(
            "hash-object", "-w", "--stdin", input_text="synthetic-link-target"
        ).stdout.strip()
        changed = sandbox.git("update-index", "--cacheinfo", "120000", oid, relative)
        assert changed.returncode == 0, changed.stderr
    elif corruption == "index-gitlink":
        head = sandbox.git("rev-parse", "HEAD").stdout.strip()
        changed = sandbox.git("update-index", "--cacheinfo", "160000", head, relative)
        assert changed.returncode == 0, changed.stderr
    elif corruption == "head-executable":
        canonical.chmod(0o755)
        assert sandbox.git("add", "--", relative).returncode == 0
        committed = sandbox.git(
            "commit", "--no-verify", "-m", "synthetic executable report head"
        )
        assert committed.returncode == 0, committed.stderr
        canonical.chmod(0o644)
        assert sandbox.git("update-index", "--chmod=-x", "--", relative).returncode == 0
    elif corruption == "head-symlink":
        canonical.unlink()
        canonical.symlink_to("synthetic-report-target")
        assert sandbox.git("add", "--", relative).returncode == 0
        committed = sandbox.git(
            "commit", "--no-verify", "-m", "synthetic symlink report head"
        )
        assert committed.returncode == 0, committed.stderr
        canonical.unlink()
        # A symlink's Git blob is its target text.  Make the working regular
        # file and 100644 index entry use that exact blob OID, isolating the
        # rejection to HEAD's 120000 mode/type.
        canonical.write_bytes(b"synthetic-report-target")
        oid = sandbox.git("hash-object", "-w", "--", relative).stdout.strip()
        assert oid
        changed = sandbox.git("update-index", "--cacheinfo", "100644", oid, relative)
        assert changed.returncode == 0, changed.stderr
        head_oid = sandbox.git("rev-parse", f"HEAD:{relative}").stdout.strip()
        assert head_oid == oid
    else:
        # A gitlink must point to a commit object, so it cannot share an OID
        # with a 100644 blob.  Preserve identical regular working/index bytes
        # and isolate the unavoidable difference to HEAD's 160000 commit entry.
        commit_oid = sandbox.git("rev-parse", "HEAD").stdout.strip()
        changed = sandbox.git(
            "update-index", "--cacheinfo", "160000", commit_oid, relative
        )
        assert changed.returncode == 0, changed.stderr
        committed = sandbox.git(
            "commit", "--no-verify", "-m", "synthetic gitlink report head"
        )
        assert committed.returncode == 0, committed.stderr
        blob_oid = sandbox.git("hash-object", "-w", "--", relative).stdout.strip()
        assert blob_oid
        changed = sandbox.git(
            "update-index", "--cacheinfo", "100644", blob_oid, relative
        )
        assert changed.returncode == 0, changed.stderr
        assert sandbox.git("rev-parse", f":{relative}").stdout.strip() == blob_oid

    issue_path = sandbox.root / "tier1/issues/I-1.json"
    index_before = sandbox.git("ls-files", "--stage", "--", relative).stdout
    head_before = sandbox.git("ls-tree", "HEAD", "--", relative).stdout
    external_before = None if external is None else external.read_bytes()
    before = (
        _git_state(sandbox),
        issue_path.read_bytes(),
        index_before,
        head_before,
        external_before,
    )

    rejected = sandbox.run(
        "issue",
        "observatory-attach",
        "--issue",
        "I-1",
        "--ref",
        "observatory-report#synthetic-pin-run",
        role="pc",
    )

    _assert_controlled_rejection(rejected)
    assert (
        _git_state(sandbox),
        issue_path.read_bytes(),
        sandbox.git("ls-files", "--stage", "--", relative).stdout,
        sandbox.git("ls-tree", "HEAD", "--", relative).stdout,
        None if external is None else external.read_bytes(),
    ) == before


@pytest.mark.parametrize(
    "corruption",
    ["dispatch-directory-symlink", "unexpected-filename", "document-id", "affiliation"],
)
def test_issue_close_fails_closed_on_noncanonical_dispatch_enumeration(
    sandbox: Sandbox,
    corruption: str,
) -> None:
    _seed_affiliated_dispatch(sandbox)
    terminal = sandbox.run(
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
    assert terminal.returncode == 0, terminal.stderr
    dispatch_dir = sandbox.root / "trees/L4/nodes/1/dispatches"
    dispatch_path = dispatch_dir / "d-1-1.json"
    external: Path | None = None
    if corruption == "dispatch-directory-symlink":
        external = sandbox.root.parent / "synthetic-dispatch-lane"
        shutil.move(str(dispatch_dir), str(external))
        dispatch_dir.symlink_to(external, target_is_directory=True)
    elif corruption == "unexpected-filename":
        shutil.copyfile(dispatch_path, dispatch_dir / "not-a-dispatch.json")
    else:
        document = json.loads(dispatch_path.read_text(encoding="utf-8"))
        if corruption == "document-id":
            document["id"] = "d-1-2"
        else:
            document["issue_ref"] = "INVALID-ISSUE-AFFILIATION"
        dispatch_path.write_text(json.dumps(document), encoding="utf-8")
    external_bytes = None
    if external is not None:
        external_bytes = {
            path.name: path.read_bytes() for path in sorted(external.iterdir())
        }
    before = (
        _git_state(sandbox),
        (sandbox.root / "tier1/issues/I-1.json").read_bytes(),
        external_bytes,
    )

    rejected = sandbox.run(
        "issue",
        "close",
        "--issue",
        "I-1",
        "--text",
        "SYNTHETIC-CLOSE-TEXT",
        role="pc",
    )

    _assert_controlled_rejection(rejected)
    after_external = None
    if external is not None:
        after_external = {
            path.name: path.read_bytes() for path in sorted(external.iterdir())
        }
    assert (
        _git_state(sandbox),
        (sandbox.root / "tier1/issues/I-1.json").read_bytes(),
        after_external,
    ) == before


@pytest.mark.parametrize("verdict", ["grant", "reject"])
def test_adjudication_paths_are_write_once_across_git_history(
    sandbox: Sandbox,
    verdict: str,
) -> None:
    anchor = _seed_ready_dispatch(sandbox)
    if verdict == "grant":
        args = (
            "claim",
            "grant",
            "--tree",
            "L4",
            "--dispatch",
            "d-1-1",
            "--text",
            "SYNTHETIC-HISTORICAL-GRANT",
            "--proposed-tier",
            "2",
            "--granted-tier",
            "2",
            "--standing-class",
            "trunk",
            "--anchor",
            anchor,
        )
    else:
        args = (
            "claim",
            "reject",
            "--tree",
            "L4",
            "--dispatch",
            "d-1-1",
            "--text",
            "SYNTHETIC-HISTORICAL-REJECT",
            "--reason",
            "SYNTHETIC-HISTORICAL-REASON",
        )
    first = sandbox.run(*args, role="verifier")
    assert first.returncode == 0, first.stderr
    relative = "trees/L4/nodes/1/adjudications/d-1-1-a1.md"
    reverted = sandbox.git("revert", "--no-commit", "HEAD")
    assert reverted.returncode == 0, reverted.stderr
    committed = sandbox.git(
        "commit", "--no-verify", "-m", f"synthetic revert {verdict} adjudication"
    )
    assert committed.returncode == 0, committed.stderr
    assert not (sandbox.root / relative).exists()
    assert sandbox.git("log", "--all", "--format=%H", "--", relative).stdout.strip()
    before = _git_state(sandbox)

    rejected = sandbox.run(*args, role="verifier")

    _assert_controlled_rejection(rejected)
    assert "historical path" in rejected.stderr
    assert _git_state(sandbox) == before
    assert not (sandbox.root / relative).exists()
