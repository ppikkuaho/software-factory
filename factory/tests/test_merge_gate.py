"""Increment 5 merge permission seam.

The sanctioned git merge path is harness-owned and requires the source node to have cleared its
review gate. These tests use real Git branches and the Git-ref-safe branch codec
``node/path/__self__`` so parent and child node branches can coexist.
"""

from __future__ import annotations

import copy
import os
import subprocess

import pytest

from harnessd import addressing, fencing, harnessctl, ipc, ledger, merge_gate


NODE = "proj/area/workstream/task#exec"
PARENT = "proj/area/workstream#exec"
SOURCE_BRANCH = "proj/area/workstream/task"
TARGET_BRANCH = "proj/area/workstream"
SOURCE_REF = merge_gate.git_ref_for_branch(SOURCE_BRANCH)
TARGET_REF = merge_gate.git_ref_for_branch(TARGET_BRANCH)


@pytest.fixture
def runtime(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    previous = ledger.RUNTIME_ROOT
    ledger.RUNTIME_ROOT = runtime_root
    try:
        yield runtime_root
    finally:
        ledger.RUNTIME_ROOT = previous


def _git_env(repo):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(repo),
    })
    return env


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=_git_env(repo),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _git_status(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        env=_git_env(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", TARGET_REF)
    _git(repo, "checkout", "-b", SOURCE_REF)
    (repo / "feature.txt").write_text("feature from task\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "task feature")
    _git(repo, "checkout", TARGET_REF)
    return repo


def _conflict_repo(tmp_path, name="conflict-repo"):
    repo = tmp_path / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True)
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", TARGET_REF)
    (repo / "shared.txt").write_text("target change\n", encoding="utf-8")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-m", "target change")
    _git(repo, "checkout", "-b", SOURCE_REF, "HEAD~1")
    (repo / "shared.txt").write_text("source change\n", encoding="utf-8")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-m", "source change")
    _git(repo, "checkout", TARGET_REF)
    return repo


def _seed(
    *,
    state="done",
    gate_state="gate_passed",
    gate_id="gate-merge-001",
    parent_address=PARENT,
    workspace=None,
):
    token = fencing.mint_owner_token(NODE, "sa-merge", "sess-merge", 1)
    rec = {
        "node_address": NODE,
        "parent_address": parent_address,
        "level": "L5",
        "subagent_id": "sa-merge",
        "session_uuid": "sess-merge",
        "state": state,
        "generation": 4,
        "lease_epoch": 1,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "terminal",
        "gate_state": gate_state,
        "gate_id": gate_id,
        "gate_review_address": NODE.replace("#exec", "#review"),
        "gate_verdict": "ACCEPT" if gate_state == "gate_passed" else None,
        "workspace": str(workspace) if workspace is not None else None,
    }
    ledger.write_binding({NODE: copy.deepcopy(rec)}, _lock_held=True)
    return rec


def _wal(event=None):
    rows = [r for r in ledger.load_wal() if r.get("node_address") == NODE]
    if event:
        rows = [r for r in rows if r.get("event") == event]
    return rows


def test_merge_branch_requires_gate_pass(runtime, tmp_path):
    repo = _repo(tmp_path)
    _seed(gate_state="candidate_submitted")

    result = merge_gate.merge_branch(NODE, repo_path=repo)

    assert result.ok is False and result.merged is False
    assert any("MERGE-GATE-PASS-REQUIRED" in e for e in result.errors)
    _git(repo, "checkout", TARGET_REF)
    assert not (repo / "feature.txt").exists(), "merge ran before the review gate passed"
    assert _wal("git_merged") == []


def test_merge_branch_requires_gate_id_proof_pointer(runtime, tmp_path):
    repo = _repo(tmp_path)
    _seed(gate_id=None)

    result = merge_gate.merge_branch(NODE, repo_path=repo)

    assert result.ok is False and result.merged is False
    assert any("MERGE-GATE-ID-REQUIRED" in e for e in result.errors)
    _git(repo, "checkout", TARGET_REF)
    assert not (repo / "feature.txt").exists()


def test_merge_branch_refuses_binding_parent_that_disagrees_with_source_path(runtime, tmp_path):
    repo = _repo(tmp_path)
    _seed(parent_address="proj/other/workstream#exec")

    result = merge_gate.merge_branch(NODE, repo_path=repo)

    assert result.ok is False and result.merged is False
    assert any("MERGE-PARENT-MISMATCH" in e for e in result.errors)
    assert _git_status(repo) == ""


def test_merge_branch_refuses_non_parent_requester(runtime, tmp_path):
    repo = _repo(tmp_path)
    _seed()

    result = merge_gate.merge_branch(NODE, repo_path=repo, requested_by="proj/other#exec")

    assert result.ok is False and result.merged is False
    assert result.requested_by == "proj/other#exec"
    assert any("MERGE-REQUESTER-NOT-PARENT" in e for e in result.errors)
    _git(repo, "checkout", TARGET_REF)
    assert not (repo / "feature.txt").exists()
    assert _wal("git_merged") == []


def test_merge_branch_merges_after_gate_pass_and_journals(runtime, tmp_path):
    repo = _repo(tmp_path)
    _seed()

    result = merge_gate.merge_branch(NODE, repo_path=repo, requested_by=PARENT)

    assert result.ok is True and result.merged is True
    assert result.source_branch == SOURCE_BRANCH
    assert result.target_branch == TARGET_BRANCH
    assert result.requested_by == PARENT
    _git(repo, "checkout", TARGET_REF)
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "feature from task\n"
    rows = _wal("git_merged")
    assert len(rows) == 1
    delta = rows[0].get("binding_delta") or {}
    assert delta["source_branch"] == SOURCE_BRANCH
    assert delta["target_branch"] == TARGET_BRANCH
    assert delta["source_git_ref"] == SOURCE_REF
    assert delta["target_git_ref"] == TARGET_REF
    assert delta["gate_state"] == "gate_passed"
    assert delta["gate_id"] == "gate-merge-001"
    assert delta["merge_requested_by"] == PARENT


def test_merge_branch_replay_is_noop_success_without_duplicate_merge_journal(runtime, tmp_path):
    repo = _repo(tmp_path)
    _seed()

    first = merge_gate.merge_branch(NODE, repo_path=repo, requested_by=PARENT)
    second = merge_gate.merge_branch(NODE, repo_path=repo, requested_by=PARENT)

    assert first.ok is True and first.merged is True
    assert second.ok is True and second.merged is False
    assert second.errors == []
    assert len(_wal("git_merged")) == 1


def test_auto_merge_derives_in_runtime_repo_and_replay_is_idempotent(runtime):
    repo = _repo(runtime / "nodes" / "proj" / "area", name="workstream")
    workspace = addressing.node_dir(NODE, runtime)
    workspace.mkdir(parents=True, exist_ok=True)
    _seed(workspace=workspace)

    first = merge_gate.auto_merge_after_gate_pass(NODE)
    second = merge_gate.auto_merge_after_gate_pass(NODE)

    assert first.ok is True
    assert first.outcome == "merged"
    assert first.repo_path == str(repo.resolve())
    assert first.source_branch == SOURCE_BRANCH
    assert first.target_branch == TARGET_BRANCH
    assert first.requested_by == PARENT
    assert second == first
    _git(repo, "checkout", TARGET_REF)
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "feature from task\n"
    assert len(_wal("git_merged")) == 1
    assert len(_wal("git_auto_merge_outcome")) == 1


def test_auto_merge_outside_runtime_is_not_applicable_and_journals_loud_anomaly(
    runtime,
    tmp_path,
):
    repo = _repo(tmp_path)
    workspace = repo / "task"
    workspace.mkdir()
    _seed(workspace=workspace)

    result = merge_gate.auto_merge_after_gate_pass(NODE)

    assert result.ok is True
    assert result.outcome == "not_applicable"
    assert result.anomaly is True
    assert result.repo_path == str(repo.resolve())
    assert any("OUTSIDE-RUNTIME" in error for error in result.errors)
    assert _wal("git_merged") == []
    rows = _wal("git_auto_merge_outcome")
    assert len(rows) == 1
    assert rows[0]["binding_delta"]["auto_merge_anomaly"] is True
    assert "LOOK CLOSER" in rows[0]["summary"]


def test_auto_merge_conflict_is_failed_with_explicit_repair_pointer(runtime):
    repo = _conflict_repo(runtime / "nodes" / "proj" / "area", name="workstream")
    workspace = addressing.node_dir(NODE, runtime)
    workspace.mkdir(parents=True, exist_ok=True)
    _seed(workspace=workspace)

    result = merge_gate.auto_merge_after_gate_pass(NODE)

    assert result.ok is False
    assert result.outcome == "failed"
    assert result.merged is False
    assert "harnessctl merge" in result.repair_pointer
    assert NODE in result.repair_pointer
    assert str(repo.resolve()) in result.repair_pointer
    assert any("MERGE-CONFLICT" in error for error in result.errors)
    assert _git_status(repo) == ""
    assert len(_wal("git_auto_merge_outcome")) == 1


def test_merge_branch_defaults_requester_to_operator(runtime, tmp_path):
    repo = _repo(tmp_path)
    _seed()

    result = merge_gate.merge_branch(NODE, repo_path=repo)

    assert result.ok is True and result.merged is True
    assert result.requested_by == "operator"
    rows = _wal("git_merged")
    assert rows[0].get("binding_delta", {})["merge_requested_by"] == "operator"


def test_merge_branch_blank_requester_defaults_to_operator(runtime, tmp_path):
    repo = _repo(tmp_path)
    _seed()

    result = merge_gate.merge_branch(NODE, repo_path=repo, requested_by=" \t")

    assert result.ok is True and result.merged is True
    assert result.requested_by == "operator"
    rows = _wal("git_merged")
    assert rows[0].get("binding_delta", {})["merge_requested_by"] == "operator"


def test_merge_branch_aborts_conflict_and_leaves_repo_clean(runtime, tmp_path):
    repo = _conflict_repo(tmp_path)
    _seed()

    result = merge_gate.merge_branch(NODE, repo_path=repo)

    assert result.ok is False and result.merged is False
    assert any("MERGE-CONFLICT" in e for e in result.errors)
    assert _git_status(repo) == ""
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "target change\n"
    assert _wal("git_merged") == []


def test_ipc_merge_routes_full_result(runtime, tmp_path):
    repo = _repo(tmp_path)
    _seed()

    resp = ipc.handle_request({
        "command": "merge",
        "addr": NODE,
        "repo_path": str(repo),
        "requested_by": PARENT,
    })

    assert resp["ok"] is True
    assert resp["command"] == "merge"
    assert resp["merged"] is True
    assert resp["source_branch"] == SOURCE_BRANCH
    assert resp["target_branch"] == TARGET_BRANCH
    assert resp["requested_by"] == PARENT


def test_harnessctl_merge_serializes_repo_and_target_branch():
    parser = harnessctl.build_parser()
    args = parser.parse_args([
        "merge",
        NODE,
        "--repo",
        "/tmp/repo",
        "--target-branch",
        TARGET_BRANCH,
        "--requested-by",
        PARENT,
    ])
    request = harnessctl._build_request(args)

    assert request == {
        "command": "merge",
        "addr": NODE,
        "repo_path": "/tmp/repo",
        "target_branch": TARGET_BRANCH,
        "requested_by": PARENT,
    }


def test_harnessctl_merge_omitted_requester_serializes_none():
    parser = harnessctl.build_parser()
    args = parser.parse_args([
        "merge",
        NODE,
        "--repo",
        "/tmp/repo",
    ])
    request = harnessctl._build_request(args)

    assert request == {
        "command": "merge",
        "addr": NODE,
        "repo_path": "/tmp/repo",
        "target_branch": None,
        "requested_by": None,
    }


def test_harnessctl_face_names_merge_as_repair_only():
    parser = harnessctl.build_parser()

    assert "repair" in parser.format_help().lower()
