"""Gate-owned git merge control path.

The operational git protocol says review PASS is the merge prerequisite. This module is the
control-plane seam for that rule: it performs a real git merge only after the source node's binding
is finalized as ``gate_state=gate_passed``. Agents may prepare branches and commits, but this is the
sanctioned merge edge that ties code movement to the gate spine.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

from . import addressing, executor, ledger


BRANCH_SENTINEL = "__self__"
AUTO_MERGE_EVENT = "git_auto_merge_outcome"


class MergeResult(NamedTuple):
    ok: bool
    merged: bool
    source_branch: Optional[str]
    target_branch: Optional[str]
    requested_by: Optional[str]
    errors: list[str]


class AutoMergeResult(NamedTuple):
    ok: bool
    outcome: str
    merged: bool
    already_merged: bool
    anomaly: bool
    repo_path: Optional[str]
    source_branch: Optional[str]
    target_branch: Optional[str]
    requested_by: Optional[str]
    repair_pointer: Optional[str]
    errors: list[str]


def _node_path_from_address(address: str | None) -> Optional[str]:
    if not address or not isinstance(address, str):
        return None
    node_path = address.split("#", 1)[0].strip().strip("/")
    return node_path or None


def _branch_for_node_path(node_path: str | None) -> Optional[str]:
    if not node_path:
        return None
    return node_path


def _branch_from_address(address: str | None) -> Optional[str]:
    return _branch_for_node_path(_node_path_from_address(address))


def git_ref_for_branch(branch: str | None) -> Optional[str]:
    """Encode a logical branch path as the concrete Git branch ref.

    Git's file-backed ref namespace cannot contain both ``proj/a`` and ``proj/a/b`` as branch refs.
    The harness keeps the logical branch path equal to the node path, then stores the concrete Git
    branch under a leaf sentinel so parent and child branches can coexist.
    """
    if not branch:
        return None
    return f"{branch.strip().strip('/')}/{BRANCH_SENTINEL}"


def _immediate_parent_branch(source_branch: str | None) -> Optional[str]:
    if not source_branch:
        return None
    parts = [p for p in source_branch.split("/") if p]
    if len(parts) <= 1:
        return None
    return "/".join(parts[:-1])


def _git_env(repo: Path) -> dict:
    return {
        "GIT_AUTHOR_NAME": "harnessd",
        "GIT_AUTHOR_EMAIL": "harnessd@example.invalid",
        "GIT_COMMITTER_NAME": "harnessd",
        "GIT_COMMITTER_EMAIL": "harnessd@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(repo),
        "PATH": os.environ.get("PATH", ""),
    }


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [os.environ.get("HARNESSD_GIT", "git"), "-C", str(repo), *args],
        env=_git_env(repo),
        capture_output=True,
        text=True,
    )


def _git_error(prefix: str, result: subprocess.CompletedProcess) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        return f"{prefix} (exit {result.returncode}): {detail}"
    return f"{prefix} (exit {result.returncode})"


def _normalize_requested_by(requested_by: str | None) -> str:
    if requested_by is None:
        return "operator"
    normalized = str(requested_by).strip()
    return normalized or "operator"


def _prior_auto_merge_result(node_address: str, gate_id: str | None) -> AutoMergeResult | None:
    """Replay the durable outcome for one accepted gate instead of touching Git twice."""
    try:
        rows = reversed(ledger.load_wal())
    except Exception:  # noqa: BLE001 — unreadable history cannot prove idempotence
        return None
    for row in rows:
        if row.get("event") != AUTO_MERGE_EVENT or row.get("node_address") != node_address:
            continue
        delta = row.get("binding_delta") or {}
        if delta.get("gate_id") != gate_id:
            continue
        return AutoMergeResult(
            ok=bool(delta.get("auto_merge_ok")),
            outcome=str(delta.get("auto_merge_outcome") or "failed"),
            merged=bool(delta.get("auto_merge_merged")),
            already_merged=bool(delta.get("auto_merge_already_merged")),
            anomaly=bool(delta.get("auto_merge_anomaly")),
            repo_path=delta.get("repo_path"),
            source_branch=delta.get("source_branch"),
            target_branch=delta.get("target_branch"),
            requested_by=delta.get("merge_requested_by"),
            repair_pointer=delta.get("auto_merge_repair_pointer"),
            errors=list(delta.get("auto_merge_errors") or []),
        )
    return None


def _workspace_repo(binding: dict) -> tuple[Path | None, bool, list[str]]:
    """Resolve the producer's Git top level, constrained to this runtime's node tree."""
    workspace_text = str(binding.get("workspace") or "").strip()
    if not workspace_text:
        return None, False, ["AUTO-MERGE-NOT-APPLICABLE: producer workspace is absent"]
    workspace = Path(workspace_text)
    if not workspace.is_dir():
        return None, False, [
            f"AUTO-MERGE-NOT-APPLICABLE: producer workspace {workspace_text!r} is absent"
        ]
    top = _run_git(workspace, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        return None, False, [
            f"AUTO-MERGE-NOT-APPLICABLE: producer workspace {workspace_text!r} "
            "is not in a Git worktree"
        ]
    raw_repo = top.stdout.strip()
    if not raw_repo:
        return None, True, [
            "AUTO-MERGE-REPO-OUTSIDE-RUNTIME: Git returned an empty top-level path; LOOK CLOSER"
        ]
    repo = Path(raw_repo).resolve()
    if ledger.RUNTIME_ROOT is None:
        return repo, True, [
            "AUTO-MERGE-REPO-OUTSIDE-RUNTIME: runtime root is unavailable, so repository "
            f"{str(repo)!r} cannot be authorized; LOOK CLOSER"
        ]
    nodes_root = (Path(ledger.RUNTIME_ROOT).resolve() / addressing.NODES_DIRNAME).resolve()
    if repo != nodes_root and nodes_root not in repo.parents:
        return repo, True, [
            "AUTO-MERGE-REPO-OUTSIDE-RUNTIME: discovered Git top level "
            f"{str(repo)!r} is outside runtime node tree {str(nodes_root)!r}; LOOK CLOSER"
        ]
    return repo, False, []


def _repair_pointer(
    node_address: str,
    repo: Path,
    requested_by: str | None,
) -> str:
    command = (
        "python3 -m harnessd.harnessctl merge "
        f"{shlex.quote(node_address)} --repo {shlex.quote(str(repo))}"
    )
    if requested_by:
        command += f" --requested-by {shlex.quote(requested_by)}"
    return command


def _journal_auto_merge_result(
    node_address: str,
    binding: dict,
    result: AutoMergeResult,
) -> AutoMergeResult:
    if result.anomaly:
        summary = (
            f"AUTO-MERGE ANOMALY — LOOK CLOSER: {node_address} resolved repository "
            f"{result.repo_path!r} outside the runtime node tree; merge not applicable"
        )
    elif result.outcome == "failed":
        summary = (
            f"AUTO-MERGE FAILED for {node_address}; review verdict remains gate_passed and "
            f"operator/parent repair is required via {result.repair_pointer}"
        )
    elif result.outcome == "not_applicable":
        summary = (
            f"auto-merge not applicable for {node_address}: producer workspace has no "
            "authorized in-runtime Git worktree"
        )
    elif result.already_merged:
        summary = (
            f"auto-merge replay for {node_address}: source is already present in parent; "
            "no Git mutation repeated"
        )
    else:
        summary = f"auto-merge completed for {node_address} after durable gate PASS"
    executor.journal(
        node_address,
        event=AUTO_MERGE_EVENT,
        from_state=binding.get("state"),
        to_state=binding.get("state"),
        lease_epoch=binding.get("lease_epoch"),
        binding_delta={
            "gate_id": binding.get("gate_id"),
            "gate_state": binding.get("gate_state"),
            "auto_merge_ok": result.ok,
            "auto_merge_outcome": result.outcome,
            "auto_merge_merged": result.merged,
            "auto_merge_already_merged": result.already_merged,
            "auto_merge_anomaly": result.anomaly,
            "repo_path": result.repo_path,
            "source_branch": result.source_branch,
            "target_branch": result.target_branch,
            "merge_requested_by": result.requested_by,
            "auto_merge_repair_pointer": result.repair_pointer,
            "auto_merge_errors": result.errors,
        },
        summary=summary,
    )
    return result


def auto_merge_after_gate_pass(node_address: str) -> AutoMergeResult:
    """Run the sanctioned merge consequence of a durable gate PASS.

    Repository identity is derived from the producer workspace and accepted only inside the
    runtime node tree. A non-worktree is a normal not-applicable outcome; an enclosing/outside
    repository is a loud anomaly and is never touched. The gate verdict is review truth and is
    never rolled back by a Git failure.
    """
    binding = ledger.read_binding(node_address)
    source_branch = _branch_from_address(node_address)
    target_branch = _immediate_parent_branch(source_branch)
    requested_by = (binding or {}).get("parent_address")
    if binding is None:
        return AutoMergeResult(
            ok=False,
            outcome="failed",
            merged=False,
            already_merged=False,
            anomaly=False,
            repo_path=None,
            source_branch=source_branch,
            target_branch=target_branch,
            requested_by=requested_by,
            repair_pointer=None,
            errors=[f"MERGE-NO-BINDING: no binding for node {node_address!r}"],
        )
    prior = _prior_auto_merge_result(node_address, binding.get("gate_id"))
    if prior is not None:
        return prior

    repo, anomaly, resolution_errors = _workspace_repo(binding)
    if repo is None or anomaly:
        return _journal_auto_merge_result(
            node_address,
            binding,
            AutoMergeResult(
                ok=True,
                outcome="not_applicable",
                merged=False,
                already_merged=False,
                anomaly=anomaly,
                repo_path=str(repo) if repo is not None else None,
                source_branch=source_branch,
                target_branch=target_branch,
                requested_by=requested_by,
                repair_pointer=None,
                errors=resolution_errors,
            ),
        )

    merge_result = merge_branch(
        node_address,
        repo_path=repo,
        requested_by=requested_by,
    )
    if merge_result.ok:
        outcome = AutoMergeResult(
            ok=True,
            outcome="merged",
            merged=True,
            already_merged=not merge_result.merged,
            anomaly=False,
            repo_path=str(repo),
            source_branch=merge_result.source_branch,
            target_branch=merge_result.target_branch,
            requested_by=merge_result.requested_by,
            repair_pointer=None,
            errors=[],
        )
    else:
        outcome = AutoMergeResult(
            ok=False,
            outcome="failed",
            merged=False,
            already_merged=False,
            anomaly=False,
            repo_path=str(repo),
            source_branch=merge_result.source_branch,
            target_branch=merge_result.target_branch,
            requested_by=merge_result.requested_by,
            repair_pointer=_repair_pointer(node_address, repo, requested_by),
            errors=merge_result.errors,
        )
    return _journal_auto_merge_result(node_address, binding, outcome)


def merge_branch(
    node_address: str,
    *,
    repo_path: str | Path,
    target_branch: str | None = None,
    requested_by: str | None = None,
) -> MergeResult:
    """Merge ``node_address``'s branch into its parent branch after review gate PASS.

    ``node_address`` is the producer binding whose branch is being merged. ``source_branch`` and
    ``target_branch`` are logical branch paths: the node path before ``#``. The concrete Git refs
    append ``/__self__`` so parent and child refs can coexist in Git's file-backed ref namespace. The
    target branch is explicit when supplied, otherwise it derives from the source path's structural
    parent. A recorded ``parent_address`` must name that same structural parent.
    """
    merge_requested_by = _normalize_requested_by(requested_by)
    binding = ledger.read_binding(node_address)
    source_branch = _branch_from_address(node_address)
    if binding is None:
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target_branch,
            requested_by=merge_requested_by,
            errors=[f"MERGE-NO-BINDING: no binding for node {node_address!r}"],
        )
    if binding.get("state") != "done" or binding.get("gate_state") != "gate_passed":
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target_branch,
            requested_by=merge_requested_by,
            errors=[
                "MERGE-GATE-PASS-REQUIRED: source node must be done with "
                f"gate_state='gate_passed' before merge; got state={binding.get('state')!r}, "
                f"gate_state={binding.get('gate_state')!r}"
            ],
        )
    if not binding.get("gate_id"):
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target_branch,
            requested_by=merge_requested_by,
            errors=[
                "MERGE-GATE-ID-REQUIRED: source node must carry the accepted gate_id proof "
                "pointer before merge"
            ],
        )
    if not source_branch:
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=None,
            target_branch=target_branch,
            requested_by=merge_requested_by,
            errors=[f"MERGE-SOURCE-BRANCH-UNRESOLVED: cannot derive branch from {node_address!r}"],
        )
    expected_target = _immediate_parent_branch(source_branch)
    recorded_parent = _branch_from_address((binding or {}).get("parent_address"))
    if recorded_parent and recorded_parent != expected_target:
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target_branch or recorded_parent,
            requested_by=merge_requested_by,
            errors=[
                "MERGE-PARENT-MISMATCH: "
                f"source branch {source_branch!r} has structural parent {expected_target!r}, "
                f"but binding parent_address maps to {recorded_parent!r}"
            ],
        )
    target = target_branch or expected_target
    if not target:
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=None,
            requested_by=merge_requested_by,
            errors=[
                "MERGE-TARGET-BRANCH-UNRESOLVED: pass --target-branch or set parent_address "
                "on the source binding"
            ],
        )
    if target != expected_target:
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target,
            requested_by=merge_requested_by,
            errors=[
                "MERGE-TARGET-NOT-PARENT: "
                f"source branch {source_branch!r} may merge only into its immediate parent "
                f"{expected_target!r}, not {target!r}"
            ],
        )
    if target == source_branch:
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target,
            requested_by=merge_requested_by,
            errors=["MERGE-SAME-BRANCH: source and target branches are identical"],
        )
    requester_branch = _branch_from_address(merge_requested_by)
    if merge_requested_by != "operator" and requester_branch != target:
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target,
            requested_by=merge_requested_by,
            errors=[
                "MERGE-REQUESTER-NOT-PARENT: "
                f"requested_by {merge_requested_by!r} maps to {requester_branch!r}, "
                f"but source branch {source_branch!r} may move only by parent {target!r} "
                "or by operator repair"
            ],
        )

    repo = Path(repo_path)
    if not repo.is_dir():
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target,
            requested_by=merge_requested_by,
            errors=[f"MERGE-REPO-MISSING: repo path {str(repo)!r} is not a directory"],
        )

    inside = _run_git(repo, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target,
            requested_by=merge_requested_by,
            errors=[_git_error("MERGE-REPO-INVALID: repo path is not a git work tree", inside)],
        )

    source_ref = git_ref_for_branch(source_branch)
    target_ref = git_ref_for_branch(target)
    if not source_ref or not target_ref:
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target,
            requested_by=merge_requested_by,
            errors=[
                "MERGE-GIT-REF-UNRESOLVED: could not encode source/target branch as concrete Git ref"
            ],
        )

    # Crash replay and explicit repair retries are harmless: if the accepted source is already
    # reachable from the target, the merge consequence has landed and must not be repeated or
    # journaled as new movement.
    ancestor = _run_git(repo, ["merge-base", "--is-ancestor", source_ref, target_ref])
    if ancestor.returncode == 0:
        return MergeResult(
            ok=True,
            merged=False,
            source_branch=source_branch,
            target_branch=target,
            requested_by=merge_requested_by,
            errors=[],
        )
    if ancestor.returncode != 1:
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target,
            requested_by=merge_requested_by,
            errors=[
                _git_error(
                    "MERGE-ANCESTRY-CHECK-FAILED: source or target ref cannot be verified",
                    ancestor,
                )
            ],
        )

    checkout = _run_git(repo, ["checkout", target_ref])
    if checkout.returncode != 0:
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target,
            requested_by=merge_requested_by,
            errors=[
                _git_error(
                    f"MERGE-CHECKOUT-FAILED: could not check out target {target!r} "
                    f"(git ref {target_ref!r})",
                    checkout,
                )
            ],
        )

    merge = _run_git(
        repo,
        [
            "merge",
            "--no-ff",
            "--no-edit",
            source_ref,
        ],
    )
    if merge.returncode != 0:
        abort = _run_git(repo, ["merge", "--abort"])
        conflict = "CONFLICT" in ((merge.stderr or "") + (merge.stdout or ""))
        prefix = "MERGE-CONFLICT" if conflict else "MERGE-FAILED"
        errors = [
            _git_error(
                f"{prefix}: could not merge {source_branch!r} "
                f"(git ref {source_ref!r}) into {target!r} (git ref {target_ref!r})",
                merge,
            )
        ]
        if abort.returncode != 0:
            errors.append(_git_error("MERGE-ABORT-FAILED: repo may require operator repair", abort))
        return MergeResult(
            ok=False,
            merged=False,
            source_branch=source_branch,
            target_branch=target,
            requested_by=merge_requested_by,
            errors=errors,
        )

    executor.journal(
        node_address,
        event="git_merged",
        from_state=binding.get("state"),
        to_state=binding.get("state"),
        binding_delta={
            "source_branch": source_branch,
            "target_branch": target,
            "source_git_ref": source_ref,
            "target_git_ref": target_ref,
            "gate_state": binding.get("gate_state"),
            "gate_id": binding.get("gate_id"),
            "gate_review_address": binding.get("gate_review_address"),
            "gate_verdict": binding.get("gate_verdict"),
            "merge_requested_by": merge_requested_by,
        },
        summary=(
            f"git merge: {source_branch} -> {target} after review gate PASS "
            f"requested_by={merge_requested_by} "
            f"({binding.get('gate_review_address') or 'review pointer absent'})"
        ),
    )
    return MergeResult(
        ok=True,
        merged=True,
        source_branch=source_branch,
        target_branch=target,
        requested_by=merge_requested_by,
        errors=[],
    )
