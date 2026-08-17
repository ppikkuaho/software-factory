"""Repository-state transactions fail closed without partial publication."""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ht.errors import HtError
from ht import gitutil
import ht.repository_atomic as repository_atomic
from ht.repository_atomic import RepositoryTransaction

from conftest import Sandbox


_SYNTHETIC_HEAD = "0" * 40


def _git_common_dir(root: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
        text=True,
        capture_output=True,
        check=True,
    )
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = root / common
    return common.resolve()


def _git_without_interpreter(
    sandbox: Sandbox, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(sandbox.root), *args],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(sandbox.root),
        },
        text=True,
        capture_output=True,
    )


def test_fresh_root_installs_interpreter_independent_canonical_hook(
    sandbox: Sandbox,
) -> None:
    hook = _git_common_dir(sandbox.root) / "hooks/pre-commit"

    assert hook.read_bytes() == gitutil.PRECOMMIT_HOOK_BYTES
    assert hook.stat().st_mode & 0o111
    assert sys.executable.encode("utf-8") not in hook.read_bytes()


def test_canonical_hook_allows_ordinary_code_commit_without_interpreter(
    sandbox: Sandbox,
) -> None:
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    code = sandbox.root / "ordinary-code.py"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    assert sandbox.git("add", "--", code.name).returncode == 0

    committed = _git_without_interpreter(sandbox, "commit", "-m", "ordinary code")

    assert committed.returncode == 0, committed.stderr
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() != before


def test_canonical_hook_rejects_mixed_code_and_protected_state_without_interpreter(
    sandbox: Sandbox,
) -> None:
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    code = sandbox.root / "ordinary-code.py"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    composed = sandbox.root / "readout/composed-tree.json"
    composed.write_bytes(composed.read_bytes() + b" \n")
    assert sandbox.git(
        "add", "--", code.name, "readout/composed-tree.json"
    ).returncode == 0

    rejected = _git_without_interpreter(
        sandbox, "commit", "-m", "mixed uncontrolled"
    )

    assert rejected.returncode != 0
    assert "HT_PYTHON is required" in rejected.stderr
    assert "out-of-band" in rejected.stderr
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before


@pytest.mark.parametrize("direction", ["protected-to-outside", "outside-to-protected"])
def test_canonical_hook_rejects_protected_boundary_rename_without_interpreter(
    sandbox: Sandbox,
    direction: str,
) -> None:
    if direction == "protected-to-outside":
        source = "readout/INTERPRETATION.md"
        destination = "INTERPRETATION.md"
    else:
        source = "ordinary-code.py"
        destination = "tier1/ordinary-code.py"
        (sandbox.root / source).write_text("VALUE = 1\n", encoding="utf-8")
        assert sandbox.git("add", "--", source).returncode == 0
        seeded = _git_without_interpreter(
            sandbox, "commit", "-m", "seed ordinary code"
        )
        assert seeded.returncode == 0, seeded.stderr
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    moved = sandbox.git("mv", "--", source, destination)
    assert moved.returncode == 0, moved.stderr

    rejected = _git_without_interpreter(
        sandbox, "commit", "-m", f"rename {direction}"
    )

    assert rejected.returncode != 0
    assert "HT_PYTHON is required" in rejected.stderr
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before


def test_canonical_hook_still_rejects_direct_out_of_band_state_write(
    sandbox: Sandbox,
) -> None:
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    composed = sandbox.root / "readout/composed-tree.json"
    composed.write_bytes(composed.read_bytes() + b" \n")
    assert sandbox.git("add", "--", "readout/composed-tree.json").returncode == 0

    rejected = sandbox.git(
        "commit",
        "-m",
        "out of band",
        env_extra={"HT_PYTHON": sys.executable, "HT_ROLE": "director"},
    )

    assert rejected.returncode != 0
    assert "out-of-band state write" in rejected.stderr
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before


@pytest.mark.parametrize(
    "legacy",
    [
        (
            "#!/bin/sh\n"
            "# hypothesis-tree pre-commit backstop (A2 §4.2) — thin shim into the ht package.\n"
            "exec /root/.venv/bin/python -m ht _precommit\n"
        ),
        (
            "#!/bin/sh\n"
            "# hypothesis-tree pre-commit backstop (A2 §4.2). Thin shim into the ht package so\n"
            "# the schema + authority logic is never duplicated. Installed by `ht root init`.\n"
            "exec /linked/worktree/.venv/bin/python3 -m ht _precommit\n"
        ),
    ],
)
def test_hook_install_atomically_migrates_known_interpreter_bound_shims(
    sandbox: Sandbox,
    legacy: str,
) -> None:
    hooks = _git_common_dir(sandbox.root) / "hooks"
    hook = hooks / "pre-commit"
    hook.write_text(legacy, encoding="utf-8")
    hook.chmod(0o755)
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()

    result = sandbox.run("root", "hook-install", role="harness")

    assert result.returncode == 0, result.stderr
    assert hook.read_bytes() == gitutil.PRECOMMIT_HOOK_BYTES
    backups = list(hooks.glob(".ht-precommit-migration-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == legacy
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before
    assert sandbox.git("status", "--porcelain=v1").stdout == ""


def test_hook_install_refuses_unknown_existing_hook(sandbox: Sandbox) -> None:
    hook = _git_common_dir(sandbox.root) / "hooks/pre-commit"
    unknown = b"#!/bin/sh\necho operator hook\n"
    hook.write_bytes(unknown)
    hook.chmod(0o755)

    result = sandbox.run("root", "hook-install", role="harness")

    assert result.returncode == 2
    assert "refusing to replace unknown pre-commit hook" in result.stderr
    assert hook.read_bytes() == unknown


def test_hook_install_rejects_configured_inactive_hooks_path(
    sandbox: Sandbox,
) -> None:
    canonical = _git_common_dir(sandbox.root) / "hooks/pre-commit"
    before_hook = canonical.read_bytes()
    configured = sandbox.git("config", "core.hooksPath", ".githooks")
    assert configured.returncode == 0, configured.stderr
    (sandbox.root / ".githooks").mkdir()

    result = sandbox.run("root", "hook-install", role="harness")

    assert result.returncode == 2
    assert "core.hooksPath is configured" in result.stderr
    assert canonical.read_bytes() == before_hook

    # Calibrate the supported Git behavior behind the refusal: an ordinary
    # manual commit now bypasses the common hook completely.
    composed = sandbox.root / "readout/composed-tree.json"
    composed.write_bytes(composed.read_bytes() + b" \n")
    assert sandbox.git("add", "--", "readout/composed-tree.json").returncode == 0
    bypassed = _git_without_interpreter(
        sandbox, "commit", "-m", "custom hooks path bypass calibration"
    )
    assert bypassed.returncode == 0, bypassed.stderr


def test_linked_worktree_migration_installs_into_git_common_dir(
    sandbox: Sandbox,
) -> None:
    linked = sandbox.root.parent / "linked-research-root"
    added = sandbox.git("worktree", "add", "-q", "-b", "hook-probe", str(linked))
    assert added.returncode == 0, added.stderr
    assert (linked / ".git").is_file()
    hooks = _git_common_dir(linked) / "hooks"
    hook = hooks / "pre-commit"
    legacy = (
        "#!/bin/sh\n"
        "# hypothesis-tree pre-commit backstop (A2 §4.2) — thin shim into the ht package.\n"
        "exec /base/worktree/.venv/bin/python -m ht _precommit\n"
    )
    hook.write_text(legacy, encoding="utf-8")
    hook.chmod(0o755)

    result = Sandbox(linked).run("root", "hook-install", role="harness")

    assert result.returncode == 0, result.stderr
    assert hook.read_bytes() == gitutil.PRECOMMIT_HOOK_BYTES
    assert not (linked / ".git/hooks").exists()
    backups = list(hooks.glob(".ht-precommit-migration-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == legacy


def test_linked_worktree_installer_rejects_worktree_hooks_path(
    sandbox: Sandbox,
) -> None:
    enabled = sandbox.git("config", "extensions.worktreeConfig", "true")
    assert enabled.returncode == 0, enabled.stderr
    linked = sandbox.root.parent / "linked-configured-hooks-root"
    added = sandbox.git(
        "worktree", "add", "-q", "-b", "configured-hook-probe", str(linked)
    )
    assert added.returncode == 0, added.stderr
    linked_sandbox = Sandbox(linked)
    configured = linked_sandbox.git(
        "config", "--worktree", "core.hooksPath", ".githooks"
    )
    assert configured.returncode == 0, configured.stderr

    result = linked_sandbox.run("root", "hook-install", role="harness")

    assert result.returncode == 2
    assert "core.hooksPath is configured" in result.stderr
    assert (
        _git_common_dir(linked) / "hooks/pre-commit"
    ).read_bytes() == gitutil.PRECOMMIT_HOOK_BYTES


def test_transaction_rejects_byte_changed_preimage_before_any_write(
    tmp_path: Path,
) -> None:
    first = tmp_path / "tier1/subgoals/SG-1.json"
    second = tmp_path / "tier1/task-packages/TP-1.json"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b'{"id":"SG-1"}\n')

    with pytest.raises(HtError, match="exact repository preimage changed"):
        RepositoryTransaction(
            tmp_path,
            {
                first: b'{\n  "id": "SG-1"\n}\n',
                second: None,
            },
            base_head=_SYNTHETIC_HEAD,
        )

    assert first.read_bytes() == b'{"id":"SG-1"}\n'
    assert not second.exists()


def test_transaction_write_once_precondition_rejects_existing_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tier1/action-receipts/AR-1.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'{"id":"AR-1"}\n')

    with pytest.raises(HtError, match="exact repository preimage changed"):
        RepositoryTransaction(tmp_path, {target: None}, base_head=_SYNTHETIC_HEAD)

    assert target.read_bytes() == b'{"id":"AR-1"}\n'


def test_transaction_rechecks_preimage_after_concurrent_drift(tmp_path: Path) -> None:
    target = tmp_path / "tier1/subgoals/SG-1.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'{"id":"SG-1","version":1}\n')
    transaction = RepositoryTransaction(
        tmp_path,
        {target: b'{"id":"SG-1","version":1}\n'},
        base_head=_SYNTHETIC_HEAD,
    )

    target.write_bytes(b'{"id":"SG-1","version":2}\n')

    with pytest.raises(HtError, match="changed before candidate write"):
        transaction.write_bytes(target, b'{"id":"SG-1","version":3}\n')
    assert target.read_bytes() == b'{"id":"SG-1","version":2}\n'


def test_rollback_preserves_external_write_after_candidate(tmp_path: Path) -> None:
    target = tmp_path / "tier1/subgoals/SG-1.json"
    target.parent.mkdir(parents=True)
    original = b'{"id":"SG-1","version":1}\n'
    candidate = b'{"id":"SG-1","version":2}\n'
    external = b'{"id":"SG-1","version":3}\n'
    target.write_bytes(original)
    transaction = RepositoryTransaction(
        tmp_path, {target: original}, base_head=_SYNTHETIC_HEAD
    )
    transaction.write_bytes(target, candidate)
    target.write_bytes(external)

    with pytest.raises(HtError, match="preserved externally changed paths"):
        transaction.rollback()

    assert target.read_bytes() == external


def test_transaction_rejects_symlinked_ancestor_without_touching_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "tier1").mkdir()
    (root / "tier1/subgoals").symlink_to(outside, target_is_directory=True)
    escaped = outside / "SG-1.json"

    with pytest.raises(HtError, match="not a real directory"):
        RepositoryTransaction(
            root,
            {root / "tier1/subgoals/SG-1.json": None},
            base_head=_SYNTHETIC_HEAD,
        )

    assert not escaped.exists()


def test_descriptor_walk_cannot_be_redirected_by_intermediate_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "a/b").mkdir(parents=True)
    (outside / "b").mkdir(parents=True)
    target = root / "a/b/state.json"
    transaction = RepositoryTransaction(
        root, {target: None}, base_head=_SYNTHETIC_HEAD
    )
    real_open = os.open
    b_opens = 0

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal b_opens
        if path == "b" and dir_fd is not None:
            b_opens += 1
            if b_opens == 2:
                (root / "a").rename(root / "a-held")
                (root / "a").symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)
    transaction.write_bytes(target, b"candidate\n")

    assert not (outside / "b/state.json").exists()
    assert (root / "a-held/b/state.json").read_bytes() == b"candidate\n"
    (root / "a").unlink()
    (root / "a-held").rename(root / "a")
    transaction.rollback()
    assert not target.exists()


def test_incomplete_declared_output_set_rejects_and_rolls_back(
    sandbox: Sandbox,
) -> None:
    first = sandbox.root / "alpha.txt"
    second = sandbox.root / "beta.txt"
    head = sandbox.git("rev-parse", "HEAD").stdout.strip()

    with pytest.raises(HtError, match="output declarations are incomplete"):
        with RepositoryTransaction(
            sandbox.root,
            {first: None, second: None},
            base_head=head,
        ) as transaction:
            transaction.write_bytes(first, b"alpha\n")
            transaction.commit(["alpha.txt"], "harness", "incomplete candidate")

    assert not first.exists()
    assert not second.exists()
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == head


def test_capture_and_candidate_reject_hardlink_aliases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "state.json"
    alias = tmp_path / "alias.json"
    target.write_bytes(b"original\n")
    os.link(target, alias)
    with pytest.raises(HtError, match="single-link regular file"):
        RepositoryTransaction(
            root, {target: b"original\n"}, base_head=_SYNTHETIC_HEAD
        )

    alias.unlink()
    target.unlink()
    transaction = RepositoryTransaction(
        root, {target: None}, base_head=_SYNTHETIC_HEAD
    )
    transaction.write_bytes(target, b"candidate\n")
    os.link(target, alias)
    with pytest.raises(HtError, match="single-link"):
        with transaction:
            transaction.commit(["state.json"], "harness", "aliased candidate")
    assert alias.read_bytes() == b"candidate\n"


def test_first_install_failure_removes_every_created_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "new/deep/state.json"
    transaction = RepositoryTransaction(
        root, {target: None}, base_head=_SYNTHETIC_HEAD
    )
    real_open = os.open

    def failing_open(path, flags, mode=0o777, *, dir_fd=None):
        if isinstance(path, str) and path.startswith(".ht-repository-txn-"):
            raise OSError(errno.EIO, "injected first-install failure")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", failing_open)
    with pytest.raises(OSError, match="injected first-install failure"):
        with transaction:
            transaction.write_bytes(target, b"candidate\n")
    assert not (root / "new").exists()


def test_pipeline_rolls_back_files_and_index_when_real_hook_rejects(
    sandbox: Sandbox,
) -> None:
    before_head = sandbox.git("rev-parse", "HEAD").stdout.strip()
    before_status = sandbox.git(
        "status", "--porcelain=v1", "--untracked-files=all"
    ).stdout
    before_index_tree = sandbox.git("write-tree").stdout.strip()
    before_composed = (sandbox.root / "readout/composed-tree.json").read_bytes()

    hook = sandbox.root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 73\n", encoding="utf-8")
    hook.chmod(0o755)

    result = sandbox.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "Does transactional publication hold?",
        role="director",
    )

    assert result.returncode == 2
    assert "exact pinned HT validator shim" in result.stderr
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before_head
    assert (
        sandbox.git("status", "--porcelain=v1", "--untracked-files=all").stdout
        == before_status
    )
    assert sandbox.git("write-tree").stdout.strip() == before_index_tree
    assert (sandbox.root / "readout/composed-tree.json").read_bytes() == before_composed
    assert not (sandbox.root / "trees/L4").exists()
    assert not list(sandbox.root.rglob(".ht-repository-txn-*"))


def test_hook_failure_preserves_preexisting_staged_and_unrelated_worktree_state(
    sandbox: Sandbox,
) -> None:
    staged = sandbox.root / "operator-note.txt"
    staged.write_text("already staged\n", encoding="utf-8")
    assert sandbox.git("add", "--", staged.name).returncode == 0
    unrelated = sandbox.root / "unrelated.txt"
    unrelated.write_text("leave me alone\n", encoding="utf-8")
    before_tree = sandbox.git("write-tree").stdout.strip()
    before_staged_blob = sandbox.git("show", ":operator-note.txt").stdout

    hook = sandbox.root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 73\n", encoding="utf-8")
    hook.chmod(0o755)
    result = sandbox.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "Does transactional publication hold?",
        role="director",
    )

    assert result.returncode == 2
    assert sandbox.git("write-tree").stdout.strip() == before_tree
    assert sandbox.git("show", ":operator-note.txt").stdout == before_staged_blob
    assert unrelated.read_text(encoding="utf-8") == "leave me alone\n"
    assert not (sandbox.root / "trees/L4").exists()


def test_commit_msg_failure_rolls_back_after_real_precommit_passes(
    sandbox: Sandbox,
) -> None:
    before_head = sandbox.git("rev-parse", "HEAD").stdout.strip()
    before_tree = sandbox.git("write-tree").stdout.strip()
    hook = sandbox.root / ".git/hooks/commit-msg"
    hook.write_text("#!/bin/sh\nexit 74\n", encoding="utf-8")
    hook.chmod(0o755)

    result = sandbox.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "Does post-validation rollback hold?",
        role="director",
    )

    assert result.returncode == 2
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before_head
    assert sandbox.git("write-tree").stdout.strip() == before_tree
    assert sandbox.git(
        "status", "--porcelain=v1", "--untracked-files=all"
    ).stdout == ""
    assert not (sandbox.root / "trees/L4").exists()


def test_pipeline_commits_multi_file_candidate_through_real_hook(
    sandbox: Sandbox,
) -> None:
    before_head = sandbox.git("rev-parse", "HEAD").stdout.strip()

    result = sandbox.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "Does transactional publication hold?",
        role="director",
    )

    assert result.returncode == 0, result.stderr
    after_head = sandbox.git("rev-parse", "HEAD").stdout.strip()
    assert after_head != before_head
    committed = set(
        sandbox.git("diff-tree", "--no-commit-id", "--name-only", "-r", after_head)
        .stdout.splitlines()
    )
    assert committed == {
        "readout/composed-tree.json",
        "trees/L4/index.json",
        "trees/L4/index.live.json",
        "trees/L4/tree.json",
    }
    assert sandbox.git(
        "status", "--porcelain=v1", "--untracked-files=all"
    ).stdout == ""


def test_success_retires_exactly_one_index_and_hook_directory_per_transaction(
    sandbox: Sandbox,
) -> None:
    git_dir = sandbox.root / ".git"
    before_indexes = set(git_dir.glob(".ht-repository-retired-file-*"))
    before_hooks = set(git_dir.glob(".ht-repository-retired-hooks-*"))

    result = sandbox.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "Are ordinary forensic residues bounded?",
        role="director",
    )

    assert result.returncode == 0, result.stderr
    new_indexes = set(git_dir.glob(".ht-repository-retired-file-*")) - before_indexes
    new_hooks = set(git_dir.glob(".ht-repository-retired-hooks-*")) - before_hooks
    assert len(new_indexes) == 1
    assert len(new_hooks) == 1
    retired_index = next(iter(new_indexes))
    retired_hooks = next(iter(new_hooks))
    assert retired_index.is_file() and retired_index.stat().st_size > 0
    assert retired_hooks.is_dir() and list(retired_hooks.iterdir()) == []
    assert not list(git_dir.glob(".ht-repository-index-*"))
    assert not list(git_dir.glob(".ht-repository-hooks-*"))
    assert sandbox.git(
        "status", "--porcelain=v1", "--untracked-files=all"
    ).stdout == ""


def test_success_commits_only_transaction_paths_and_leaves_prior_stage_intact(
    sandbox: Sandbox,
) -> None:
    staged = sandbox.root / "operator-note.txt"
    staged.write_text("already staged\n", encoding="utf-8")
    assert sandbox.git("add", "--", staged.name).returncode == 0
    staged_tree = sandbox.git("write-tree").stdout.strip()

    result = sandbox.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "Does exact commit scoping hold?",
        role="director",
    )

    assert result.returncode == 0, result.stderr
    committed = set(
        sandbox.git("show", "--format=", "--name-only", "HEAD").stdout.splitlines()
    )
    assert "operator-note.txt" not in committed
    assert sandbox.git("show", ":operator-note.txt").stdout == "already staged\n"
    assert sandbox.git("diff", "--cached", "--name-only").stdout == "operator-note.txt\n"
    assert sandbox.git("write-tree").stdout.strip() != staged_tree


def test_hostile_git_config_environment_cannot_bypass_pinned_hook(
    sandbox: Sandbox,
) -> None:
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    hook = sandbox.root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 73\n", encoding="utf-8")
    hook.chmod(0o755)

    result = sandbox.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "Can ambient Git config bypass the hook?",
        role="director",
        env_extra={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/dev/null",
            "GIT_WORK_TREE": str(sandbox.root.parent),
        },
    )

    assert result.returncode == 2
    assert "exact pinned HT validator shim" in result.stderr
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before
    assert not (sandbox.root / "trees/L4").exists()


def test_real_git_index_hardlink_is_rejected_before_candidate_write(
    sandbox: Sandbox,
) -> None:
    index_result = sandbox.git("rev-parse", "--git-path", "index")
    assert index_result.returncode == 0, index_result.stderr
    index = Path(index_result.stdout.strip())
    if not index.is_absolute():
        index = sandbox.root / index
    alias = sandbox.root.parent / "git-index-alias"
    os.link(index, alias)
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()

    result = sandbox.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "Are Git index aliases rejected?",
        role="director",
    )

    assert result.returncode == 2
    assert "Git index" in result.stderr
    assert "single-link" in result.stderr
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before
    assert not (sandbox.root / "trees/L4").exists()
    alias.unlink()


def test_same_tree_external_commit_without_role_is_never_accepted(
    sandbox: Sandbox,
) -> None:
    hook = sandbox.root / ".git/hooks/commit-msg"
    hook.write_text(
        "#!/bin/sh\n"
        "tree=$(/usr/bin/git write-tree) || exit 91\n"
        "external=$(printf 'external commit without role\\n' | "
        "/usr/bin/git commit-tree \"$tree\" -p HEAD -F -) || exit 92\n"
        "/usr/bin/git update-ref HEAD \"$external\" HEAD || exit 93\n"
        "exit 73\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    result = sandbox.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "Can an external same-tree commit impersonate HT?",
        role="director",
    )

    assert result.returncode == 2
    assert "does not equal the exact expected role-stamped commit" in result.stderr
    message = sandbox.git("show", "-s", "--format=%B", "HEAD").stdout
    assert message == "external commit without role\n\n"
    assert "HT-Role" not in message


def test_replaced_lexical_repository_cannot_receive_transaction_commit(
    sandbox: Sandbox,
) -> None:
    target = sandbox.root / "identity-probe.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    transaction = RepositoryTransaction(
        sandbox.root,
        {target: None},
        base_head=before,
    )
    transaction.write_bytes(target, b"held-repository candidate\n")

    held_root = sandbox.root.with_name("research-root-held")
    sandbox.root.rename(held_root)
    shutil.copytree(held_root, sandbox.root)
    (sandbox.root / target.name).unlink()

    with pytest.raises(
        gitutil.GitStateChanged,
        match="repository root identity changed",
    ):
        with transaction:
            transaction.commit([target.name], "harness", "identity probe")

    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before
    assert not (sandbox.root / target.name).exists()
    assert (held_root / target.name).read_bytes() == b"held-repository candidate\n"


def test_same_bytes_with_executable_mode_cannot_be_committed(
    sandbox: Sandbox,
) -> None:
    target = sandbox.root / "mode-probe.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()

    with pytest.raises(HtError, match="candidate identity changed"):
        with RepositoryTransaction(
            sandbox.root,
            {target: None},
            base_head=before,
        ) as transaction:
            transaction.write_bytes(target, b"exact candidate bytes\n")
            target.chmod(0o755)
            transaction.commit([target.name], "harness", "mode probe")

    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before
    assert target.read_bytes() == b"exact candidate bytes\n"
    assert target.stat().st_mode & 0o777 == 0o755
    assert sandbox.git("ls-tree", "--name-only", "HEAD", target.name).stdout == ""


def test_rollback_never_removes_replacement_for_created_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "new/deep/state.json"
    transaction = RepositoryTransaction(
        root,
        {target: None},
        base_head=_SYNTHETIC_HEAD,
    )
    transaction.write_bytes(target, b"candidate\n")

    owned = root / "new-owned"
    (root / "new").rename(owned)
    replacement = root / "new"
    replacement.mkdir()
    replacement_identity = (replacement.stat().st_dev, replacement.stat().st_ino)

    with pytest.raises(HtError, match="candidate identity changed"):
        transaction.rollback()

    assert replacement.is_dir()
    assert (replacement.stat().st_dev, replacement.stat().st_ino) == replacement_identity
    assert (owned / "deep/state.json").read_bytes() == b"candidate\n"


def test_installed_precommit_swap_during_git_opening_cannot_replace_validator(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = sandbox.root / ".git/hooks/pre-commit"
    original_hook = hook.read_bytes()
    original_mode = hook.stat().st_mode & 0o777
    marker = sandbox.root / "mutable-hook-ran"
    malicious_hook = (
        "#!/bin/sh\n"
        f"printf ran > {marker}\n"
        "exit 93\n"
    ).encode("utf-8")
    real_controlled_run = gitutil._controlled_run
    swapped = False

    def swap_while_git_opens(root, args, **kwargs):
        nonlocal swapped
        is_commit = "commit" in args and any(
            str(arg).startswith("core.hooksPath=") for arg in args
        )
        if not is_commit:
            return real_controlled_run(root, args, **kwargs)
        swapped = True
        hook.write_bytes(malicious_hook)
        hook.chmod(0o755)
        try:
            return real_controlled_run(root, args, **kwargs)
        finally:
            hook.write_bytes(original_hook)
            hook.chmod(original_mode)

    monkeypatch.setattr(gitutil, "_controlled_run", swap_while_git_opens)
    target = sandbox.root / "hook-stability.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    with RepositoryTransaction(
        sandbox.root,
        {target: None},
        base_head=before,
    ) as transaction:
        transaction.write_bytes(target, b"stable validator\n")
        transaction.commit([target.name], "harness", "hook stability probe")

    assert swapped
    assert not marker.exists()
    assert hook.read_bytes() == original_hook
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() != before


def test_transient_executable_mode_during_index_build_is_rejected(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox.root / "transient-mode.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    real_controlled_run = gitutil._controlled_run
    changed = False

    def chmod_around_index_update(root, args, **kwargs):
        nonlocal changed
        if args[:2] != ["update-index", "--add"] or changed:
            return real_controlled_run(root, args, **kwargs)
        changed = True
        target.chmod(0o755)
        try:
            return real_controlled_run(root, args, **kwargs)
        finally:
            target.chmod(0o644)

    monkeypatch.setattr(gitutil, "_controlled_run", chmod_around_index_update)
    with pytest.raises(HtError, match="preserved externally changed paths"):
        with RepositoryTransaction(
            sandbox.root,
            {target: None},
            base_head=before,
        ) as transaction:
            transaction.write_bytes(target, b"same bytes throughout\n")
            transaction.commit([target.name], "harness", "transient mode probe")

    assert changed
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before
    assert target.read_bytes() == b"same bytes throughout\n"
    assert target.stat().st_mode & 0o777 == 0o644


def test_git_directory_swap_after_identity_check_never_mutates_replacement(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox.root / "git-binding-probe.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    installed_git = sandbox.root / ".git"
    held_git = sandbox.root / ".git-held"
    real_mkdir = os.mkdir
    swapped = False

    def swap_before_private_setup(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if isinstance(path, str) and path.startswith(".ht-repository-hooks-") and not swapped:
            swapped = True
            installed_git.rename(held_git)
            shutil.copytree(held_git, installed_git)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swap_before_private_setup)
    with pytest.raises(gitutil.GitStateChanged, match="visible Git repository binding"):
        with RepositoryTransaction(
            sandbox.root,
            {target: None},
            base_head=before,
        ) as transaction:
            transaction.write_bytes(target, b"candidate stays visible\n")
            transaction.commit([target.name], "harness", "Git binding probe")

    assert swapped
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == before
    held_head = subprocess.run(
        ["git", f"--git-dir={held_git}", "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert held_head == before
    assert target.read_bytes() == b"candidate stays visible\n"


def test_created_directory_swap_after_atomic_install_uses_held_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "new/state.json"
    held = root / "new-held"
    real_rename = repository_atomic._rename_noreplace
    swapped = False

    def swap_after_install(source_fd, source, destination_fd, destination):
        nonlocal swapped
        real_rename(source_fd, source, destination_fd, destination)
        if destination == "new" and not swapped:
            swapped = True
            (root / "new").rename(held)
            (root / "new").mkdir()

    monkeypatch.setattr(repository_atomic, "_rename_noreplace", swap_after_install)
    transaction = RepositoryTransaction(
        root,
        {target: None},
        base_head=_SYNTHETIC_HEAD,
    )
    transaction.write_bytes(target, b"held directory candidate\n")
    replacement_identity = ((root / "new").stat().st_dev, (root / "new").stat().st_ino)

    with pytest.raises(HtError, match="candidate identity changed"):
        transaction.rollback()

    assert swapped
    assert ((root / "new").stat().st_dev, (root / "new").stat().st_ino) == replacement_identity
    assert (held / "state.json").read_bytes() == b"held directory candidate\n"


def test_cleanup_failure_after_exact_commit_never_rolls_back_worktree(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox.root / "post-commit-cleanup.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    real_remove = gitutil._remove_private_file
    injected = False

    def fail_private_index_cleanup(directory_fd, name, expected):
        nonlocal injected
        if name.startswith(".ht-repository-index-") and not name.endswith(".lock"):
            injected = True
            raise HtError("injected post-publication cleanup failure")
        return real_remove(directory_fd, name, expected)

    monkeypatch.setattr(gitutil, "_remove_private_file", fail_private_index_cleanup)
    with pytest.raises(gitutil.GitStateChanged, match="transaction cleanup failed"):
        with RepositoryTransaction(
            sandbox.root,
            {target: None},
            base_head=before,
        ) as transaction:
            transaction.write_bytes(target, b"published candidate\n")
            transaction.commit([target.name], "harness", "cleanup classification probe")

    assert injected
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() != before
    assert target.read_bytes() == b"published candidate\n"
    assert sandbox.git("show", f"HEAD:{target.name}").stdout == "published candidate\n"


def test_real_index_failure_after_exact_commit_never_rolls_back_worktree(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox.root / "post-commit-index.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    real_controlled_run = gitutil._controlled_run
    injected = False

    def fail_real_index_reset(root, args, **kwargs):
        nonlocal injected
        if args and args[0] == "reset":
            injected = True
            return subprocess.CompletedProcess(
                ["git", *args],
                71,
                stdout="",
                stderr="injected real-index failure",
            )
        return real_controlled_run(root, args, **kwargs)

    monkeypatch.setattr(gitutil, "_controlled_run", fail_real_index_reset)
    with pytest.raises(gitutil.GitStateChanged, match="real-index reconciliation failed"):
        with RepositoryTransaction(
            sandbox.root,
            {target: None},
            base_head=before,
        ) as transaction:
            transaction.write_bytes(target, b"published despite index leak\n")
            transaction.commit([target.name], "harness", "index classification probe")

    assert injected
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() != before
    assert target.read_bytes() == b"published despite index leak\n"
    assert sandbox.git("show", f"HEAD:{target.name}").stdout == "published despite index leak\n"


def test_post_commit_private_index_replacement_is_never_deleted(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox.root / "private-index-ownership.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    real_remove = gitutil._remove_private_file
    replacement: Path | None = None
    owned_aside: Path | None = None

    def replace_private_index(directory_fd, name, expected):
        nonlocal replacement, owned_aside
        if (
            name.startswith(".ht-repository-index-")
            and not name.endswith(".lock")
            and replacement is None
        ):
            replacement = sandbox.root / ".git" / name
            owned_aside = replacement.with_name(f"{name}.owned-aside")
            replacement.rename(owned_aside)
            replacement.write_bytes(b"unowned replacement\n")
        return real_remove(directory_fd, name, expected)

    monkeypatch.setattr(gitutil, "_remove_private_file", replace_private_index)
    with pytest.raises(gitutil.GitStateChanged, match="transaction cleanup failed"):
        with RepositoryTransaction(
            sandbox.root,
            {target: None},
            base_head=before,
        ) as transaction:
            transaction.write_bytes(target, b"published before replacement\n")
            transaction.commit([target.name], "harness", "private index ownership probe")

    assert replacement is not None and owned_aside is not None
    assert replacement.read_bytes() == b"unowned replacement\n"
    assert owned_aside.is_file()
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() != before
    assert target.read_bytes() == b"published before replacement\n"


def test_post_commit_private_hook_directory_replacement_is_restored_not_deleted(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox.root / "private-hook-ownership.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    real_retire = gitutil._retire_private_directory
    replacement: Path | None = None
    owned_aside: Path | None = None

    def replace_private_hooks(git_dir_fd, name, expected_identity):
        nonlocal replacement, owned_aside
        replacement = sandbox.root / ".git" / name
        owned_aside = replacement.with_name(f"{name}.owned-aside")
        replacement.rename(owned_aside)
        replacement.mkdir()
        return real_retire(git_dir_fd, name, expected_identity)

    monkeypatch.setattr(gitutil, "_retire_private_directory", replace_private_hooks)
    with pytest.raises(gitutil.GitStateChanged, match="transaction cleanup failed"):
        with RepositoryTransaction(
            sandbox.root,
            {target: None},
            base_head=before,
        ) as transaction:
            transaction.write_bytes(target, b"published before hook swap\n")
            transaction.commit([target.name], "harness", "private hook ownership probe")

    assert replacement is not None and owned_aside is not None
    assert replacement.is_dir()
    assert owned_aside.is_dir()
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() != before
    assert target.read_bytes() == b"published before hook swap\n"


def test_private_file_replacement_after_retirement_check_is_not_deleted(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox.root / "late-private-file-swap.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    real_stable = gitutil._stable_regular_at
    replacement: Path | None = None
    owned_aside: Path | None = None

    def swap_after_retired_file_read(directory_fd, name, **kwargs):
        nonlocal replacement, owned_aside
        row = real_stable(directory_fd, name, **kwargs)
        if name.startswith(".ht-repository-retired-file-") and replacement is None:
            replacement = sandbox.root / ".git" / name
            owned_aside = replacement.with_name(f"{name}.owned-aside")
            replacement.rename(owned_aside)
            replacement.write_bytes(b"late unowned replacement\n")
        return row

    monkeypatch.setattr(gitutil, "_stable_regular_at", swap_after_retired_file_read)
    with RepositoryTransaction(
        sandbox.root,
        {target: None},
        base_head=before,
    ) as transaction:
        transaction.write_bytes(target, b"published before late file swap\n")
        transaction.commit([target.name], "harness", "late private file swap probe")

    assert replacement is not None and owned_aside is not None
    assert replacement.read_bytes() == b"late unowned replacement\n"
    assert owned_aside.is_file()
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() != before
    assert target.read_bytes() == b"published before late file swap\n"


def test_hook_directory_replacement_after_retirement_check_is_not_deleted(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox.root / "late-private-hook-swap.txt"
    before = sandbox.git("rev-parse", "HEAD").stdout.strip()
    real_stat = gitutil.os.stat
    replacement: Path | None = None
    owned_aside: Path | None = None

    def swap_after_retired_hook_stat(path, *args, **kwargs):
        nonlocal replacement, owned_aside
        info = real_stat(path, *args, **kwargs)
        if (
            isinstance(path, str)
            and path.startswith(".ht-repository-retired-hooks-")
            and replacement is None
        ):
            replacement = sandbox.root / ".git" / path
            owned_aside = replacement.with_name(f"{path}.owned-aside")
            replacement.rename(owned_aside)
            replacement.mkdir()
        return info

    monkeypatch.setattr(gitutil.os, "stat", swap_after_retired_hook_stat)
    with RepositoryTransaction(
        sandbox.root,
        {target: None},
        base_head=before,
    ) as transaction:
        transaction.write_bytes(target, b"published before late hook swap\n")
        transaction.commit([target.name], "harness", "late private hook swap probe")

    assert replacement is not None and owned_aside is not None
    assert replacement.is_dir()
    assert owned_aside.is_dir()
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() != before
    assert target.read_bytes() == b"published before late hook swap\n"


def test_exact_commit_is_recognized_after_commit_msg_reports_failure(
    sandbox: Sandbox,
) -> None:
    staged = sandbox.root / "operator-note.txt"
    staged.write_text("already staged\n", encoding="utf-8")
    assert sandbox.git("add", "--", staged.name).returncode == 0
    hook = sandbox.root / ".git/hooks/commit-msg"
    hook.write_text(
        "#!/bin/sh\n"
        "tree=$(/usr/bin/git write-tree) || exit 91\n"
        "exact=$(/usr/bin/git commit-tree \"$tree\" -p HEAD -F \"$1\") || exit 92\n"
        "/usr/bin/git update-ref HEAD \"$exact\" HEAD || exit 93\n"
        "exit 74\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    result = sandbox.run(
        "tree",
        "init",
        "L4",
        "--root-question",
        "Can exact post-publication identity be recognized?",
        role="director",
    )

    assert result.returncode == 0, result.stderr
    assert "HT-Role: director" in sandbox.git(
        "show", "-s", "--format=%B", "HEAD"
    ).stdout
    assert sandbox.git("diff", "--cached", "--name-only").stdout == "operator-note.txt\n"
    assert sandbox.git("show", ":operator-note.txt").stdout == "already staged\n"


def test_generated_views_ignore_untracked_glob_matching_sources(
    sandbox: Sandbox,
) -> None:
    initialized = sandbox.run(
        "tree", "init", "L4", "--root-question", "Does L4 hold?", role="director"
    )
    assert initialized.returncode == 0, initialized.stderr
    sentinel_tree = sandbox.load("trees/L4/tree.json")
    sentinel_tree["component"] = "SENTINEL"
    sentinel_path = sandbox.root / "trees/SENTINEL/tree.json"
    sentinel_path.parent.mkdir(parents=True)
    sentinel_path.write_text(json.dumps(sentinel_tree) + "\n", encoding="utf-8")

    minted = sandbox.run(
        "node",
        "mint",
        "--tree",
        "L4",
        "--root",
        "--premise",
        "Bounded premise",
        "--rationale",
        "projection contamination probe",
        role="director",
    )
    assert minted.returncode == 0, minted.stderr
    composed = sandbox.load("readout/composed-tree.json")
    assert [row["component"] for row in composed["trees"]] == ["L4"]
    assert sentinel_path.exists()
    assert sandbox.git("ls-files", "--error-unmatch", "trees/SENTINEL/tree.json").returncode != 0

    created = sandbox.run(
        "ledger",
        "create",
        "--section",
        "user",
        "--text",
        "Canonical entry",
        role="user",
    )
    assert created.returncode == 0, created.stderr
    rogue = sandbox.root / "ledger/rogue/user/L-999.json"
    rogue.parent.mkdir(parents=True)
    rogue_doc = sandbox.load("ledger/top/user/L-1.json")
    rogue_doc["id"] = "L-999"
    rogue.write_text(json.dumps(rogue_doc) + "\n", encoding="utf-8")
    status = sandbox.run(
        "ledger",
        "status",
        "--entry",
        "L-1",
        "--to",
        "retired",
        "--reason",
        "probe",
        role="director",
    )
    assert status.returncode == 0, status.stderr
    assert [row["id"] for row in sandbox.load("ledger/union.index.json")["entries"]] == [
        "L-1"
    ]
    assert rogue.exists()
