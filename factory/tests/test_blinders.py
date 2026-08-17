"""Physical horse-blinders read policy (2026-07-23 ruling).

These tests lead the owed pass red-first.  The pure derivation proves the F34 graph, and the
Darwin-only cases drive the rendered profile through the real sandbox-exec binary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from harnessd import addressing, daemon, ipc, ledger, turn_state
from harnessd.spawn import blinders, sandbox


_SANDBOX_EXEC = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
_HAS_SANDBOX = os.uname().sysname == "Darwin" and Path(_SANDBOX_EXEC).is_file()
real_sandbox = pytest.mark.skipif(
    not _HAS_SANDBOX,
    reason="real macOS sandbox-exec is required for physical blinders tests",
)


def _tree(tmp_path: Path) -> tuple[Path, str, dict[str, dict]]:
    root = tmp_path / "runtime"
    address = "L1/project/area/task-a#exec"
    bindings = {
        "L1#exec": {"parent_address": None},
        "L1/project#exec": {"parent_address": "L1#exec"},
        "L1/project/area#exec": {"parent_address": "L1/project#exec"},
        address: {"parent_address": "L1/project/area#exec"},
        "L1/project/area/task-b#exec": {
            "parent_address": "L1/project/area#exec",
        },
        "L1/project/other/task-c#exec": {
            "parent_address": "L1/project/other#exec",
        },
    }
    for node_address in bindings:
        node = root / "nodes" / node_address.partition("#")[0]
        node.mkdir(parents=True, exist_ok=True)
    return root, address, bindings


def _derive(
    tmp_path: Path,
    *,
    mode: str = blinders.ENFORCE,
    address: str | None = None,
    bindings: dict[str, dict] | None = None,
    load_manifest: list[str] | None = None,
    reference_map_json_file: str | None = None,
) -> tuple[dict, Path, str, dict[str, dict]]:
    root, default_address, default_bindings = _tree(tmp_path)
    address = address or default_address
    bindings = bindings or default_bindings
    workspace = root / "nodes" / address.partition("#")[0]
    policy = blinders.derive_policy(
        node_address=address,
        runtime_root=str(root),
        bindings=bindings,
        mode=mode,
        workspace=str(workspace),
        load_manifest=load_manifest or [],
        reference_map_json_file=reference_map_json_file,
        runtime="claude-code",
        harness_root=str(tmp_path / "harness"),
    )
    return policy, root, address, bindings


def _profile(policy: dict, root: Path, address: str, tmp_path: Path) -> str:
    workspace = root / "nodes" / address.partition("#")[0]
    config = tmp_path / "cc-config"
    config.mkdir(exist_ok=True)
    (workspace / ".tmp").mkdir(exist_ok=True)
    block = sandbox.resolve_containment(
        address,
        runtime_root=str(root),
        config_dir=str(config),
        home=str(tmp_path / "home"),
        read_policy=policy,
        runtime="claude-code",
    )
    return sandbox.render_profile(block)


def _run(
    profile: str,
    *argv: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_SANDBOX_EXEC, "-p", profile, *argv],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        input=input_text,
    )


def test_policy_derives_own_subtree_parent_and_sibling_direct_surfaces(tmp_path):
    policy, root, address, _bindings = _derive(tmp_path)
    own = str(root / "nodes" / address.partition("#")[0])
    parent = str(root / "nodes/L1/project/area")
    sibling = str(root / "nodes/L1/project/area/task-b")

    assert own in policy["allow_subpaths"]
    assert parent in policy["direct_surfaces"]
    assert sibling in policy["direct_surfaces"]
    assert str(root / "nodes/L1/project/other/task-c") not in policy["direct_surfaces"]


def test_claude_runtime_state_reopens_only_the_minted_session_env(tmp_path):
    policy, _root, _address, _bindings = _derive(tmp_path)
    config = tmp_path / "cc-config"
    finalized = blinders.with_runtime_state(
        policy,
        runtime="claude-code",
        state_root=str(config),
        session_uuid="own-session",
    )

    assert str(config / "session-env") in finalized["runtime_inner_denies"]
    assert finalized["runtime_inner_allows"] == [
        str(config / "session-env" / "own-session")
    ]
    assert str(config / "session-env" / "sibling-session") not in finalized[
        "runtime_inner_allows"
    ]


def test_runtime_control_socket_rules_preserve_logical_and_canonical_aliases(tmp_path):
    policy, root, address, _bindings = _derive(tmp_path)
    logical = "/tmp/l1-l5-alias/.harnessd/harnessd.sock"
    canonical = "/private/tmp/l1-l5-alias/.harnessd/harnessd.sock"
    policy["runtime_control_sockets"] = [logical, canonical]

    profile = _profile(policy, root, address, tmp_path)

    assert f'(path-literal "{logical}")' in profile
    assert f'(path-literal "{canonical}")' in profile


def test_visible_reference_targets_are_grants_but_hidden_targets_are_not(tmp_path):
    visible = tmp_path / "harness/design/visible.md"
    hidden = tmp_path / "harness/design/maintainer-only.md"
    visible.parent.mkdir(parents=True)
    visible.write_text("visible")
    hidden.write_text("hidden")
    reference_map = tmp_path / "reference-map.json"
    reference_map.write_text(
        json.dumps(
            {
                "references": [
                    {
                        "resolved_paths": [
                            {"path": "design/visible.md", "absolute_path": str(visible)}
                        ]
                    }
                ],
                "hidden": [
                    {
                        "resolved_paths": [
                            {
                                "path": "design/maintainer-only.md",
                                "absolute_path": str(hidden),
                            }
                        ]
                    }
                ],
            }
        )
    )

    policy, _root, _address, _bindings = _derive(
        tmp_path, reference_map_json_file=str(reference_map)
    )

    assert str(visible) in policy["allow_literals"]
    assert str(hidden) not in policy["allow_literals"]


def test_declared_symlink_keeps_logical_and_real_target_literals(tmp_path):
    target = tmp_path / "harness/AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("rules")
    link = tmp_path / "harness/CLAUDE.md"
    link.symlink_to("AGENTS.md")

    policy, _root, _address, _bindings = _derive(
        tmp_path, load_manifest=[str(link)]
    )

    assert str(link) in policy["allow_literals"]
    assert str(target) in policy["allow_literals"]


def test_relative_load_manifest_and_node_pointer_use_their_distinct_roots(tmp_path):
    harness_doc = tmp_path / "harness/operational/L5/role.md"
    harness_doc.parent.mkdir(parents=True)
    harness_doc.write_text("role")
    policy, root, address, _bindings = _derive(
        tmp_path,
        load_manifest=["operational/L5/role.md"],
    )
    workspace = root / "nodes" / address.partition("#")[0]

    with_node_pointer = blinders.derive_policy(
        node_address=address,
        runtime_root=str(root),
        bindings=_bindings,
        mode=blinders.ENFORCE,
        workspace=str(workspace),
        load_manifest=["operational/L5/role.md"],
        spec_pointer="brief.md",
        runtime="claude-code",
        harness_root=str(tmp_path / "harness"),
    )

    assert str(harness_doc) in policy["allow_literals"]
    assert str(harness_doc) in with_node_pointer["allow_literals"]
    assert str(workspace / "brief.md") in with_node_pointer["allow_literals"]
    assert str(workspace / "operational/L5/role.md") not in policy["allow_literals"]


def test_relative_declared_path_cannot_escape_its_manifest_root(tmp_path):
    with pytest.raises(blinders.BlindersError, match="escapes"):
        _derive(tmp_path, load_manifest=["../../outside.md"])


def test_l1_god_view_is_address_derived_and_not_grantable_by_role_text(tmp_path):
    root, _address, bindings = _tree(tmp_path)
    l1 = blinders.derive_policy(
        node_address="L1#exec",
        runtime_root=str(root),
        bindings=bindings,
        mode=blinders.ENFORCE,
        workspace=str(root / "nodes/L1"),
        load_manifest=[],
        runtime="claude-code",
        harness_root=str(tmp_path / "harness"),
    )
    injected = blinders.derive_policy(
        node_address="L1/project/area/task-a#exec",
        runtime_root=str(root),
        bindings=bindings,
        mode=blinders.ENFORCE,
        workspace=str(root / "nodes/L1/project/area/task-a"),
        load_manifest=[],
        runtime="claude-code",
        harness_root=str(tmp_path / "harness"),
        role_variant="L1",
    )

    assert l1["l1_god_view"] is True
    assert str(root / "nodes/L1") in l1["allow_subpaths"]
    assert injected["l1_god_view"] is False


@real_sandbox
def test_l1_reads_the_nested_node_tree_but_not_arbitrary_local_files(tmp_path):
    root, _address, bindings = _tree(tmp_path)
    nested = root / "nodes/L1/project/area/task-a/product.txt"
    outside = tmp_path / "unrelated-user-file.txt"
    nested.write_text("product")
    outside.write_text("private")
    policy = blinders.derive_policy(
        node_address="L1#exec",
        runtime_root=str(root),
        bindings=bindings,
        mode=blinders.ENFORCE,
        workspace=str(root / "nodes/L1"),
        load_manifest=[],
        runtime="claude-code",
        harness_root=str(tmp_path / "harness"),
    )
    profile = _profile(policy, root, "L1#exec", tmp_path)

    assert _run(profile, "/bin/cat", str(nested)).returncode == 0
    assert _run(profile, "/bin/cat", str(outside)).returncode != 0


def test_observe_reports_reads_without_the_broad_read_deny(tmp_path):
    policy, root, address, _bindings = _derive(tmp_path, mode=blinders.OBSERVE)
    profile = _profile(policy, root, address, tmp_path)

    assert "(allow (with report) file-read*)" in profile
    assert "BLINDERS ENFORCE" not in profile
    assert "(deny file-write*)" in profile


def test_enforce_profile_application_failure_is_fail_closed(tmp_path, monkeypatch):
    policy, root, address, _bindings = _derive(tmp_path, mode=blinders.ENFORCE)
    workspace = root / "nodes" / address.partition("#")[0]
    config = tmp_path / "cc-config"
    config.mkdir()
    block = sandbox.resolve_containment(
        address,
        runtime_root=str(root),
        config_dir=str(config),
        home=str(tmp_path / "home"),
        read_policy=policy,
        runtime="claude-code",
    )
    monkeypatch.setattr(
        sandbox,
        "probe_profile",
        lambda _text: sandbox.ProfileProbe(False, 65, "compile refused"),
    )

    with pytest.raises(sandbox.ProfileApplicationError):
        sandbox.prepare_launch(
            ["env", "-i", "/usr/bin/true"],
            block,
            base_dir=str(config),
            session_name="enforce-seat",
        )


def test_observe_profile_application_failure_is_durably_degraded(tmp_path, monkeypatch):
    policy, root, address, _bindings = _derive(tmp_path, mode=blinders.OBSERVE)
    config = tmp_path / "cc-config"
    config.mkdir()
    block = sandbox.resolve_containment(
        address,
        runtime_root=str(root),
        config_dir=str(config),
        home=str(tmp_path / "home"),
        read_policy=policy,
        runtime="claude-code",
    )
    monkeypatch.setattr(
        sandbox,
        "probe_profile",
        lambda _text: sandbox.ProfileProbe(False, 65, "compile refused"),
    )
    inner = ["env", "-i", "/usr/bin/true"]

    prepared = sandbox.prepare_launch(
        inner,
        block,
        base_dir=str(config),
        session_name="observe-seat",
    )

    assert prepared.argv == inner
    assert prepared.applied is False
    assert prepared.posture["degraded"] is True
    assert "compile refused" in prepared.posture["degraded_reason"]


@real_sandbox
def test_enforce_allows_direct_surfaces_but_denies_descendants_and_cousins(tmp_path):
    policy, root, address, _bindings = _derive(tmp_path)
    own = root / "nodes/L1/project/area/task-a"
    parent = root / "nodes/L1/project/area"
    sibling = root / "nodes/L1/project/area/task-b"
    cousin = root / "nodes/L1/project/other/task-c"
    (own / "own.txt").write_text("own")
    (parent / "parent.txt").write_text("parent")
    (sibling / "surface.txt").write_text("surface")
    (sibling / "internals").mkdir()
    (sibling / "internals/private.txt").write_text("private")
    (cousin / "cousin.txt").write_text("cousin")
    profile = _profile(policy, root, address, tmp_path)
    # A known sibling may publish a new direct surface artifact after spawn/profile render.
    (sibling / "published-after-spawn.txt").write_text("new surface")

    assert _run(profile, "/bin/cat", str(own / "own.txt")).returncode == 0
    assert _run(profile, "/bin/cat", str(parent / "parent.txt")).returncode == 0
    assert _run(profile, "/bin/cat", str(sibling / "surface.txt")).returncode == 0
    assert (
        _run(profile, "/bin/cat", str(sibling / "published-after-spawn.txt")).returncode
        == 0
    )
    nested = _run(profile, "/bin/cat", str(sibling / "internals/private.txt"))
    cousin_read = _run(profile, "/bin/cat", str(cousin / "cousin.txt"))
    assert nested.returncode != 0
    assert "Operation not permitted" in nested.stderr
    assert cousin_read.returncode != 0


@real_sandbox
def test_enforce_reads_declared_doc_but_not_an_unrelated_local_doc(tmp_path):
    declared = tmp_path / "harness/design/declared.md"
    unrelated = tmp_path / "private-notes.md"
    declared.parent.mkdir(parents=True)
    declared.write_text("declared")
    unrelated.write_text("not declared")
    policy, root, address, _bindings = _derive(
        tmp_path,
        load_manifest=[str(declared)],
    )
    profile = _profile(policy, root, address, tmp_path)

    assert _run(profile, "/bin/cat", str(declared)).returncode == 0
    denied = _run(profile, "/bin/cat", str(unrelated))
    assert denied.returncode != 0
    assert "Operation not permitted" in denied.stderr


@real_sandbox
def test_measured_runtime_essentials_boot_python_and_direct_git(tmp_path):
    python = Path("/opt/homebrew/bin/python3")
    git = Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git")
    if not python.is_file() or not git.is_file():
        pytest.skip("commissioning host Homebrew Python/full-Xcode git are not installed")
    policy, root, address, _bindings = _derive(tmp_path)
    profile = _profile(policy, root, address, tmp_path)
    workspace = root / "nodes" / address.partition("#")[0]
    child_env = {
        "PATH": (
            "/Applications/Xcode.app/Contents/Developer/usr/bin:"
            "/opt/homebrew/bin:/usr/bin:/bin"
        ),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "XDG_CONFIG_HOME": str(workspace / ".config"),
        "TMPDIR": str(workspace / ".tmp"),
    }

    py = _run(
        profile,
        str(python),
        "-c",
        "import json, pathlib, sqlite3, subprocess, tempfile; print('python-ok')",
        env=child_env,
    )
    git_run = _run(profile, str(git), "--version", env=child_env)
    assert py.returncode == 0, py.stderr
    assert py.stdout.strip() == "python-ok"
    assert git_run.returncode == 0, git_run.stderr
    assert git_run.stdout.startswith("git version")


@real_sandbox
def test_claude_config_inner_carveout_keeps_settings_and_denies_other_seats(tmp_path):
    policy, root, address, _bindings = _derive(tmp_path)
    config = tmp_path / "cc-config"
    (config / "projects/other-seat").mkdir(parents=True)
    (config / "tasks/other-seat").mkdir(parents=True)
    (config / "session-env/own-seat").mkdir(parents=True)
    (config / "session-env/other-seat").mkdir(parents=True)
    (config / "settings.json").write_text("{}")
    (config / ".oauth_token").write_text("secret")
    (config / "projects/other-seat/transcript.jsonl").write_text("private")
    (config / "session-env/own-seat/runtime").write_text("own")
    (config / "session-env/other-seat/runtime").write_text("private")
    workspace = root / "nodes" / address.partition("#")[0]
    (workspace / ".tmp").mkdir(exist_ok=True)
    block = sandbox.resolve_containment(
        address,
        runtime_root=str(root),
        config_dir=str(config),
        home=str(tmp_path / "home"),
        read_policy=policy,
        runtime="claude-code",
    )
    block = sandbox.finalize_runtime_state(
        block,
        runtime="claude-code",
        state_root=str(config),
        session_uuid="own-seat",
    )
    profile = sandbox.render_profile(block)

    assert _run(profile, "/bin/cat", str(config / "settings.json")).returncode == 0
    assert (
        _run(profile, "/bin/cat", str(config / "session-env/own-seat/runtime")).returncode
        == 0
    )
    denied = _run(
        profile,
        "/bin/cat",
        str(config / "projects/other-seat/transcript.jsonl"),
    )
    assert denied.returncode != 0
    assert "Operation not permitted" in denied.stderr
    sibling = _run(
        profile,
        "/bin/cat",
        str(config / "session-env/other-seat/runtime"),
    )
    assert sibling.returncode != 0
    assert "Operation not permitted" in sibling.stderr
    assert _run(profile, "/bin/cat", str(config / ".oauth_token")).returncode != 0


@real_sandbox
def test_enforce_profile_keeps_ledger_blind_but_allows_hook_and_harnessctl_ipc(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "runtime"
    address = "L1#exec"
    node = addressing.node_dir(address, root)
    node.mkdir(parents=True)
    binding = {
        "node_address": address,
        "parent_address": None,
        "state": "running",
        "level": "L1",
        "runtime": "claude-code",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": "enforce-hook-owner",
    }
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", root)
    ledger.write_binding({address: binding}, _lock_held=True)
    surface = turn_state.seed(
        address,
        binding,
        runtime_root=root,
        profile=turn_state.CLAUDE_FULL_EDGES,
    )
    binding.update({key: value for key, value in surface.items() if key != "turn_runtime_root"})
    ledger.write_binding({address: binding}, _lock_held=True)

    listener = daemon.make_ipc_listener(root)
    server_errors = []

    def serve_two():
        try:
            ipc.serve_one(listener)
            ipc.serve_one(listener)
        except Exception as exc:  # pragma: no cover - surfaced below
            server_errors.append(exc)

    server = threading.Thread(target=serve_two)
    server.start()
    policy = blinders.derive_policy(
        node_address=address,
        runtime_root=str(root),
        bindings={address: binding},
        mode=blinders.ENFORCE,
        workspace=str(node),
        load_manifest=[],
        runtime="claude-code",
        harness_root=str(Path(__file__).resolve().parents[1]),
    )
    config_dir = tmp_path / "cc-config"
    config_dir.mkdir()
    block = sandbox.resolve_containment(
        address,
        runtime_root=str(root),
        config_dir=str(config_dir),
        home=str(tmp_path / "home"),
        read_policy=policy,
        runtime="claude-code",
    )
    profile = sandbox.render_profile(block)
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUseFailure",
            "tool_use_id": "failed-bash",
            "tool_name": "Bash",
        }
    )
    hook_argv = turn_state.hook_argv(
        python_executable=sys.executable,
        runtime_root=root,
        node_address=address,
        owner_token=binding["owner_token"],
        runtime="claude-code",
    )

    started = time.monotonic()
    try:
        hook = _run(profile, *hook_argv, input_text=payload)
        hook_latency = time.monotonic() - started
        print(f"JAILED_HOOK_ROUND_TRIP_SECONDS={hook_latency:.6f}")
        show = _run(
            profile,
            sys.executable,
            "-m",
            "harnessd.harnessctl",
            "--socket",
            str(root / ".harnessd" / "harnessd.sock"),
            "show",
            address,
        )
    finally:
        server.join(timeout=3)
        listener.close()

    denied = _run(profile, "/bin/cat", str(root / ledger.BINDING_FILENAME))
    assert server_errors == []
    assert hook.returncode == 0, hook.stderr
    assert hook_latency < 1.0
    assert show.returncode == 0, show.stderr
    assert json.loads(show.stdout)["binding"]["node_address"] == address
    assert denied.returncode != 0
    assert "Operation not permitted" in denied.stderr
