"""Acceptance contract for the static B2 real-producer adapter foundation."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "system" / "instruments" / "runtime_role_producer_launcher.py"
REGISTRY = ROOT / "system" / "roles" / "PRODUCER-ADAPTERS.json"
RETURN_SCHEMA = ROOT / "system" / "schemas" / "producer-return.schema.json"
SESSION_ID = "00000000-0000-4000-8000-000000000001"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "b2_runtime_role_producer_launcher", LAUNCHER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ADAPTER = _load_module()


def _copy_foundation(tmp_path: Path) -> Path:
    root = tmp_path / "research-root"
    (root / "system" / "instruments").mkdir(parents=True)
    (root / "system" / "roles").mkdir(parents=True)
    (root / "system" / "schemas").mkdir(parents=True)
    (root / "trees").mkdir()
    shutil.copy2(LAUNCHER, root / ADAPTER.LAUNCHER_RELPATH)
    shutil.copy2(REGISTRY, root / ADAPTER.REGISTRY_RELPATH)
    shutil.copy2(RETURN_SCHEMA, root / ADAPTER.RETURN_SCHEMA_RELPATH)
    os.chmod(root / ADAPTER.LAUNCHER_RELPATH, 0o555)
    os.chmod(root / ADAPTER.REGISTRY_RELPATH, 0o644)
    os.chmod(root / ADAPTER.RETURN_SCHEMA_RELPATH, 0o644)
    return root


def _commit_foundation(root: Path) -> None:
    git = "/usr/bin/git"
    subprocess.run([git, "init", "-q"], cwd=root, check=True)
    subprocess.run(
        [git, "add", ADAPTER.REGISTRY_RELPATH, ADAPTER.LAUNCHER_RELPATH,
         ADAPTER.RETURN_SCHEMA_RELPATH],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [
            git,
            "-c",
            "user.name=B2 Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixed producer foundation",
        ],
        cwd=root,
        check=True,
    )


def _mutate_registry(root: Path, case: str) -> None:
    path = root / ADAPTER.REGISTRY_RELPATH
    value = json.loads(path.read_text(encoding="utf-8"))
    row = value["adapters"][0]
    if case == "unknown-top-field":
        value["extra"] = None
    elif case == "second-adapter":
        value["adapters"].append(dict(row))
    elif case == "adapter-token":
        row["adapter_token"] = "caller-selected/1.0.0"
    elif case == "binary-path":
        row["binary"] = "/tmp/caller-codex"
    elif case == "binary-hash":
        row["binary_sha256"] = "0" * 64
    elif case == "version":
        row["version"] = "codex-cli 999.0.0"
    elif case == "launcher-path":
        row["launcher_repository_relpath"] = "system/instruments/other.py"
    elif case == "launcher-hash":
        row["launcher_sha256"] = "0" * 64
    elif case == "schema-path":
        row["producer_return_schema"]["repository_relpath"] = "schema.json"
    elif case == "schema-hash":
        row["producer_return_schema"]["sha256"] = "0" * 64
    elif case == "schema-bytes":
        row["producer_return_schema"]["bytes"] += 1
    elif case == "prompt-probe-extra":
        row["prompt_probe"]["caller_override"] = True
    else:  # pragma: no cover - test author error
        raise AssertionError(case)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def test_registry_rebinds_exact_launcher_schema_and_closed_prompt_probe() -> None:
    adapter = ADAPTER.load_fixed_adapter_manifest(ROOT, _allow_uncommitted=True)
    launcher_bytes = LAUNCHER.read_bytes()
    schema_bytes = RETURN_SCHEMA.read_bytes()

    assert adapter.adapter_token == "codex-exec-fixed/1.0.0"
    assert adapter.transcript_format == "codex-exec-jsonl/0.144.1"
    assert adapter.launcher_token == "ht-runtime-role-blocked-exec/1.0.0"
    assert adapter.launcher.repository_relpath == ADAPTER.LAUNCHER_RELPATH
    assert adapter.launcher.sha256 == hashlib.sha256(launcher_bytes).hexdigest()
    assert adapter.launcher.bytes == len(launcher_bytes)
    assert adapter.producer_return_schema.repository_relpath == (
        "system/schemas/producer-return.schema.json"
    )
    assert adapter.producer_return_schema.sha256 == hashlib.sha256(
        schema_bytes
    ).hexdigest()
    assert adapter.producer_return_schema.bytes == len(schema_bytes) == 11_881
    assert adapter.prompt_probe.row_roles == ("developer", "developer", "user")
    assert adapter.prompt_probe.system_skill_tree_entries == 74
    assert tuple(item.name for item in adapter.prompt_probe.system_skill_entrypoints) == (
        "imagegen",
        "openai-docs",
        "plugin-creator",
        "skill-creator",
        "skill-installer",
    )

    binding = ADAPTER.adapter_binding(adapter)
    assert dict(binding) == {
        "producer_adapter_token": "codex-exec-fixed/1.0.0",
        "producer_launcher_token": "ht-runtime-role-blocked-exec/1.0.0",
        "producer_launcher_sha256": adapter.launcher.sha256,
        "producer_return_schema_source_sha256": adapter.producer_return_schema.sha256,
        "producer_return_schema_copy_sha256": adapter.producer_return_schema.sha256,
        "transcript_format": "codex-exec-jsonl/0.144.1",
        "approval_required": True,
    }
    with pytest.raises(TypeError):
        binding["approval_required"] = False


def test_exact_fixed_argv_environment_paths_and_hash_preimages() -> None:
    adapter = ADAPTER.load_fixed_adapter_manifest(ROOT, _allow_uncommitted=True)
    paths = ADAPTER.derive_trusted_session_paths(ROOT, SESSION_ID)
    invocation = ADAPTER.build_fixed_codex_invocation(adapter, paths)
    home = paths.producer_home
    skills = ",".join(
        f'{{path="{home / item.relative_path}",enabled=false}}'
        for item in adapter.prompt_probe.system_skill_entrypoints
    )
    expected_argv = (
        str(ADAPTER.PINNED_BINARY),
        "--ask-for-approval",
        "never",
        "exec",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--disable",
        "multi_agent",
        "--disable",
        "plugins",
        "--disable",
        "apps",
        "--disable",
        "hooks",
        "--disable",
        "browser_use",
        "--disable",
        "computer_use",
        "--disable",
        "image_generation",
        "--disable",
        "in_app_browser",
        "-C",
        str(paths.reference_snapshot),
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        'default_permissions="snapshot_read"',
        "-c",
        ADAPTER.PERMISSIONS_CONFIG,
        "-c",
        'web_search="disabled"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "include_apps_instructions=false",
        "-c",
        "include_collaboration_mode_instructions=false",
        "-c",
        "include_permissions_instructions=false",
        "-c",
        "include_environment_context=false",
        "-c",
        ADAPTER.SHELL_ENVIRONMENT_CONFIG,
        "-c",
        f"skills.config=[{skills}]",
        "--output-schema",
        str(paths.producer_return_schema),
        "--output-last-message",
        str(paths.output_last_message),
        "-",
    )
    expected_environment = {
        "HOME": str(paths.producer_home),
        "CODEX_HOME": str(paths.producer_home),
        "CODEX_SQLITE_HOME": str(paths.producer_sqlite),
        "TMPDIR": str(paths.producer_tmp),
        "PATH": ADAPTER.HOST_PATH,
        "SHELL": "/bin/zsh",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "dumb",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    assert invocation.argv == expected_argv
    assert invocation.environment_dict() == expected_environment
    assert invocation.cwd == paths.reference_snapshot
    assert invocation.output_schema == paths.producer_return_schema
    assert invocation.output_last_message == paths.output_last_message
    assert invocation.argv_canonical_json == (
        json.dumps(
            list(expected_argv),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode()
    assert invocation.environment_canonical_json == (
        json.dumps(
            expected_environment,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode()
    assert invocation.argv_sha256 == hashlib.sha256(
        invocation.argv_canonical_json
    ).hexdigest()
    assert invocation.environment_sha256 == hashlib.sha256(
        invocation.environment_canonical_json
    ).hexdigest()


def test_canonical_hash_encoding_matches_frozen_calibration_vectors() -> None:
    invocation = ADAPTER.FixedProducerInvocation(
        argv=("a", "b c", "é"),
        environment=(("Z", "2"), ("A", "1")),
        cwd=Path("/"),
        output_schema=Path("/schema"),
        output_last_message=Path("/last"),
    )
    assert invocation.argv_canonical_json == '["a","b c","é"]\n'.encode()
    assert invocation.argv_sha256 == (
        "c3744ffe2b1b230d6bc158ed6edea6e22163ab8e528c407bfadeda548fd16460"
    )
    assert invocation.environment_canonical_json == b'{"A":"1","Z":"2"}\n'
    assert invocation.environment_sha256 == (
        "c805a41267dfc3b5cc5cb7b64b22160793d1438e53621f92021f85b14a53f5a8"
    )


@pytest.mark.parametrize(
    "case",
    (
        "unknown-top-field",
        "second-adapter",
        "adapter-token",
        "binary-path",
        "binary-hash",
        "version",
        "launcher-path",
        "launcher-hash",
        "schema-path",
        "schema-hash",
        "schema-bytes",
        "prompt-probe-extra",
    ),
)
def test_known_bad_registry_rows_are_rejected(tmp_path: Path, case: str) -> None:
    root = _copy_foundation(tmp_path)
    _mutate_registry(root, case)
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.load_fixed_adapter_manifest(root, _allow_uncommitted=True)


@pytest.mark.parametrize("target", ("launcher", "schema"))
def test_registry_hash_binding_rejects_changed_repository_bytes(
    tmp_path: Path, target: str
) -> None:
    root = _copy_foundation(tmp_path)
    path = root / (
        ADAPTER.LAUNCHER_RELPATH
        if target == "launcher"
        else ADAPTER.RETURN_SCHEMA_RELPATH
    )
    os.chmod(path, 0o755 if target == "launcher" else 0o644)
    path.write_bytes(path.read_bytes() + b"\n")
    os.chmod(path, 0o555 if target == "launcher" else 0o644)
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.load_fixed_adapter_manifest(root, _allow_uncommitted=True)


def test_duplicate_registry_key_and_symlinked_fixed_path_are_rejected(
    tmp_path: Path,
) -> None:
    root = _copy_foundation(tmp_path)
    registry = root / ADAPTER.REGISTRY_RELPATH
    registry.write_bytes(
        b'{"schema_version":"x","schema_version":"y","adapters":[]}\n'
    )
    with pytest.raises(ADAPTER.ProducerAdapterError, match="duplicate key"):
        ADAPTER.load_fixed_adapter_manifest(root, _allow_uncommitted=True)

    root = _copy_foundation(tmp_path / "alias")
    launcher = root / ADAPTER.LAUNCHER_RELPATH
    actual = launcher.with_name("actual-launcher.py")
    launcher.rename(actual)
    launcher.symlink_to(actual.name)
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.load_fixed_adapter_manifest(root, _allow_uncommitted=True)


@pytest.mark.parametrize(
    ("relative_path", "ancestor_relative_path"),
    (
        (ADAPTER.REGISTRY_RELPATH, "system/roles"),
        (ADAPTER.LAUNCHER_RELPATH, "system/instruments"),
        (ADAPTER.RETURN_SCHEMA_RELPATH, "system/schemas"),
    ),
)
def test_fixed_repository_file_ancestor_swap_cannot_escape_held_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    ancestor_relative_path: str,
) -> None:
    root = _copy_foundation(tmp_path)
    target = root.joinpath(*relative_path.split("/"))
    ancestor = root.joinpath(*ancestor_relative_path.split("/"))
    held_ancestor = ancestor.with_name(ancestor.name + "-held")
    outside_ancestor = tmp_path / (ancestor.name + "-outside")
    outside_ancestor.mkdir()
    outside_target = outside_ancestor / target.name
    shutil.copy2(target, outside_target)

    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        spelling = os.fspath(path)
        target_open = spelling == str(target) or (
            spelling == target.name and dir_fd is not None
        )
        if target_open and not swapped:
            ancestor.rename(held_ancestor)
            ancestor.symlink_to(outside_ancestor, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ADAPTER.os, "open", swapping_open)
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.load_fixed_adapter_manifest(root, _allow_uncommitted=True)

    assert swapped
    assert target.resolve(strict=True) == outside_target


def test_whole_root_swap_cannot_redirect_fixed_repository_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy_foundation(tmp_path / "original")
    replacement = _copy_foundation(tmp_path / "replacement")
    held_root = root.with_name(root.name + "-held")
    target = root.joinpath(*ADAPTER.REGISTRY_RELPATH.split("/"))

    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        spelling = os.fspath(path)
        target_open = spelling == str(target) or (
            spelling == target.name and dir_fd is not None
        )
        if target_open and not swapped:
            root.rename(held_root)
            replacement.rename(root)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ADAPTER.os, "open", swapping_open)
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.load_fixed_adapter_manifest(root, _allow_uncommitted=True)

    assert swapped
    assert root.stat().st_ino != held_root.stat().st_ino


def test_committed_manifest_git_queries_use_held_root_and_controlled_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy_foundation(tmp_path)
    _commit_foundation(root)
    real_run = subprocess.run
    git_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def recording_run(argv, **kwargs):
        if argv and argv[0] == "/usr/bin/git":
            git_calls.append((tuple(argv), dict(kwargs)))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(ADAPTER.subprocess, "run", recording_run)
    loaded = ADAPTER.load_fixed_adapter_manifest(root)

    assert loaded.adapter_token == ADAPTER.ADAPTER_TOKEN
    assert len(git_calls) == 9
    for argv, kwargs in git_calls:
        assert "-C" not in argv
        assert kwargs["env"] == ADAPTER._CONTROLLED_GIT_ENV
        assert kwargs["cwd"] is None
        assert len(kwargs["pass_fds"]) == 1
        assert callable(kwargs["preexec_fn"])


def test_whole_root_replacement_during_git_query_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy_foundation(tmp_path / "original")
    replacement = _copy_foundation(tmp_path / "replacement")
    _commit_foundation(root)
    _commit_foundation(replacement)
    held_root = root.with_name(root.name + "-held")
    real_run = subprocess.run
    swapped = False

    def swapping_run(argv, **kwargs):
        nonlocal swapped
        if argv and argv[0] == "/usr/bin/git" and not swapped:
            root.rename(held_root)
            replacement.rename(root)
            swapped = True
        return real_run(argv, **kwargs)

    monkeypatch.setattr(ADAPTER.subprocess, "run", swapping_run)
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.load_fixed_adapter_manifest(root)

    assert swapped
    assert root.stat().st_ino != held_root.stat().st_ino


@pytest.mark.parametrize(
    "surface",
    ("argv-append", "argv-reorder", "env-extra", "env-value", "cwd", "schema", "last"),
)
def test_caller_controlled_invocation_surfaces_are_rejected(surface: str) -> None:
    adapter = ADAPTER.load_fixed_adapter_manifest(ROOT, _allow_uncommitted=True)
    paths = ADAPTER.derive_trusted_session_paths(ROOT, SESSION_ID)
    expected = ADAPTER.build_fixed_codex_invocation(adapter, paths)
    if surface == "argv-append":
        candidate = replace(expected, argv=expected.argv + ("--dangerous",))
    elif surface == "argv-reorder":
        argv = list(expected.argv)
        argv[1], argv[3] = argv[3], argv[1]
        candidate = replace(expected, argv=tuple(argv))
    elif surface == "env-extra":
        candidate = replace(expected, environment=expected.environment + (("TOKEN", "x"),))
    elif surface == "env-value":
        candidate = replace(
            expected,
            environment=tuple(
                (key, "/tmp") if key == "HOME" else (key, value)
                for key, value in expected.environment
            ),
        )
    elif surface == "cwd":
        candidate = replace(expected, cwd=Path("/tmp"))
    elif surface == "schema":
        candidate = replace(expected, output_schema=Path("/tmp/schema.json"))
    elif surface == "last":
        candidate = replace(expected, output_last_message=Path("/tmp/last.json"))
    else:  # pragma: no cover - test author error
        raise AssertionError(surface)
    with pytest.raises(ADAPTER.ProducerAdapterError, match="argv/env/path"):
        ADAPTER.require_exact_codex_invocation(candidate, adapter, paths)


def test_session_and_adapter_paths_cannot_be_substituted() -> None:
    adapter = ADAPTER.load_fixed_adapter_manifest(ROOT, _allow_uncommitted=True)
    for bad_session in (
        "../session",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        "not-a-uuid",
    ):
        with pytest.raises(ADAPTER.ProducerAdapterError):
            ADAPTER.derive_trusted_session_paths(ROOT, bad_session)
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.derive_trusted_session_paths(str(ROOT), SESSION_ID)

    paths = ADAPTER.derive_trusted_session_paths(ROOT, SESSION_ID)
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.build_fixed_codex_invocation(
            adapter, replace(paths, producer_home=Path("/tmp/caller-home"))
        )
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.build_fixed_codex_invocation(
            adapter,
            replace(
                paths,
                producer_return_schema=Path("/tmp/caller-schema.json"),
            ),
        )
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.build_fixed_codex_invocation(
            replace(adapter, binary=Path("/tmp/caller-codex")), paths
        )
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.build_fixed_codex_invocation(
            replace(adapter, model="caller-model"), paths
        )
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.adapter_binding(
            replace(
                adapter,
                launcher=replace(adapter.launcher, sha256="0" * 64),
            )
        )
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.adapter_binding(
            replace(
                adapter,
                producer_return_schema=replace(
                    adapter.producer_return_schema,
                    sha256="0" * 64,
                ),
            )
        )


def test_installed_binary_version_hash_and_native_format_are_proved_read_only() -> None:
    adapter = ADAPTER.load_fixed_adapter_manifest(ROOT, _allow_uncommitted=True)
    if not ADAPTER.PINNED_BINARY.exists():
        pytest.skip("the registry-pinned Codex binary is not installed on this host")
    with ADAPTER.PINNED_BINARY.open("rb") as handle:
        installed_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
    if installed_sha256 != adapter.binary_sha256:
        pytest.skip(
            "the installed Codex binary does not match the registry-pinned SHA-256"
        )
    version_probe = subprocess.run(
        [str(ADAPTER.PINNED_BINARY), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if version_probe.returncode != 0:
        pytest.skip("the registry-pinned Codex binary version probe did not succeed")
    if version_probe.stdout.strip() != adapter.version:
        pytest.skip(
            "the installed Codex binary does not match the registry-pinned version"
        )
    proof = ADAPTER.prove_pinned_codex_binary(adapter)
    assert proof.path == ADAPTER.PINNED_BINARY
    assert proof.sha256 == (
        "29915529b97697def1a957b0505e770aa6a45744435d62fc263e98d7619e167a"
    )
    assert proof.version == "codex-cli 0.144.1"
    assert proof.format == "Mach-O 64-bit executable arm64"
    assert proof.bytes == 260_405_808

    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.prove_pinned_codex_binary(
            replace(adapter, binary_sha256="0" * 64)
        )
    with pytest.raises(ADAPTER.ProducerAdapterError):
        ADAPTER.prove_pinned_codex_binary(
            replace(adapter, version="codex-cli 999.0.0")
        )


def test_non_native_executable_is_rejected_before_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "codex"
    fake.write_bytes(b"#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    fake_sha = hashlib.sha256(fake.read_bytes()).hexdigest()
    adapter = ADAPTER.load_fixed_adapter_manifest(ROOT, _allow_uncommitted=True)
    monkeypatch.setattr(ADAPTER, "PINNED_BINARY", fake)
    monkeypatch.setattr(ADAPTER, "PINNED_BINARY_SHA256", fake_sha)
    monkeypatch.setattr(ADAPTER, "_require_loaded_adapter", lambda _adapter: None)
    candidate = replace(adapter, binary=fake, binary_sha256=fake_sha)
    with pytest.raises(ADAPTER.ProducerAdapterError, match="native arm64 Mach-O"):
        ADAPTER.prove_pinned_codex_binary(candidate)


def test_foundation_has_no_execution_or_runtime_integration_surface() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "def main(" not in source
    assert "subprocess.Popen" not in source
    assert "os.execve" not in source
    assert "codex-exec.jsonl" not in source
    assert "auth.json" not in source
    assert "producer-home-manifest.json" not in source
    mode = stat.S_IMODE(LAUNCHER.stat().st_mode)
    assert mode in {0o555, 0o755}
