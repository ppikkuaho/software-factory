#!/usr/bin/env python3
"""Frozen B2 Codex producer manifest and invocation primitives.

This module deliberately has no command-line entry point and never starts a
producer.  The custody wrapper/runner increment will consume these closed,
read-only values after it has independently proved the runtime packet and
session filesystem.  No caller can supply an argv element, environment key,
registry path, schema path, or output path through this API.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import struct
import subprocess
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID


REGISTRY_RELPATH = "system/roles/PRODUCER-ADAPTERS.json"
LAUNCHER_RELPATH = "system/instruments/runtime_role_producer_launcher.py"
RETURN_SCHEMA_RELPATH = "system/schemas/producer-return.schema.json"

REGISTRY_SCHEMA = "hypothesis-tree-producer-adapter-registry/1.0.0"
ADAPTER_TOKEN = "codex-exec-fixed/1.0.0"
TRANSCRIPT_FORMAT = "codex-exec-jsonl/0.144.1"
LAUNCHER_TOKEN = "ht-runtime-role-blocked-exec/1.0.0"
RUNNER_TOKEN = "ht-runtime-profile-runner/1.0.0"

PINNED_BINARY = Path(
    "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/"
    "@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"
)
PINNED_BINARY_SHA256 = (
    "29915529b97697def1a957b0505e770aa6a45744435d62fc263e98d7619e167a"
)
PINNED_VERSION = "codex-cli 0.144.1"
PINNED_MODEL = "gpt-5.6-sol"
PINNED_REASONING_EFFORT = "high"
RETURN_SCHEMA_SHA256 = (
    "0c9258e593c13708dd6c1646cb6a2fc63ad036125a6ccdeff2bf74d354f2e673"
)
RETURN_SCHEMA_BYTES = 11_881

PROMPT_PROBE_TOKEN = "codex-debug-prompt-input/0.144.1"
PROMPT_PROBE_SENTINEL = "SYNTHETIC-B2-PROMPT-INPUT-SENTINEL/1.0.0"
PROMPT_PROBE_VECTOR_SCHEMA = (
    "hypothesis-tree-codex-prompt-envelope-vector/1.0.0"
)
PROMPT_PROBE_PREFIX_SHA256 = (
    "657e0ac04dcda3ca4d9a740720e9cab99f1ee296a33a7db0f0a2b4d269787ced"
)
PROMPT_PROBE_VECTOR_SHA256 = (
    "140ef81422cc382b19ad9f58f865709ae5a28ead3620d265dbb6a90437629ba6"
)
SYSTEM_SKILL_TREE_ENTRIES = 74
SYSTEM_SKILL_TREE_MANIFEST_SHA256 = (
    "e564689a50fec30528aba9c443838249b277765b7263669a14f697f4b63f6a78"
)

HOST_PATH = (
    "/Applications/Xcode.app/Contents/Developer/usr/bin:/opt/homebrew/bin:"
    "/usr/bin:/bin:/usr/sbin:/sbin"
)
_GIT_BINARY = Path("/usr/bin/git")
_CONTROLLED_GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}
PERMISSIONS_CONFIG = (
    'permissions={snapshot_read={filesystem={":root"="read","/Users"="deny",'
    '"/Volumes"="deny","/Network"="deny","/Library"="deny",'
    '"/private"="deny","/tmp"="deny","/var"="deny","/etc"="deny",'
    '"/cores"="deny","/home"="deny","/net"="deny","/dev"="deny",'
    '"/dev/null"="write","/dev/random"="read","/dev/urandom"="read",'
    '":workspace_roots"={"."="read"}}}}'
)
SHELL_ENVIRONMENT_CONFIG = (
    'shell_environment_policy={inherit="none",set={PATH="'
    + HOST_PATH
    + '",SHELL="/bin/zsh",LANG="en_US.UTF-8",LC_ALL="en_US.UTF-8",'
    'TERM="dumb",GIT_CONFIG_GLOBAL="/dev/null",GIT_CONFIG_NOSYSTEM="1"}}'
)


class ProducerAdapterError(ValueError):
    """The frozen B2 producer adapter contract was not met."""


@dataclass(frozen=True)
class RepositoryFileBinding:
    repository_relpath: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class SkillEntrypoint:
    name: str
    relative_path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class PromptProbeManifest:
    token: str
    sentinel: str
    vector_schema: str
    envelope_prefix_sha256: str
    vector_sha256: str
    row_roles: tuple[str, ...]
    system_skill_tree_entries: int
    system_skill_tree_manifest_sha256: str
    system_skill_entrypoints: tuple[SkillEntrypoint, ...]


@dataclass(frozen=True)
class FixedProducerAdapter:
    repository_root: Path
    adapter_token: str
    transcript_format: str
    binary: Path
    binary_sha256: str
    version: str
    model: str
    reasoning_effort: str
    launcher_token: str
    launcher: RepositoryFileBinding
    producer_return_schema: RepositoryFileBinding
    prompt_probe: PromptProbeManifest


@dataclass(frozen=True)
class InstalledBinaryProof:
    path: Path
    sha256: str
    bytes: int
    version: str
    format: str


@dataclass(frozen=True)
class TrustedSessionPaths:
    repository_root: Path
    session_id: str
    session_root: Path
    reference_snapshot: Path
    producer_home: Path
    producer_sqlite: Path
    producer_tmp: Path
    producer_return_schema: Path
    output_last_message: Path


@dataclass(frozen=True)
class FixedProducerInvocation:
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    cwd: Path
    output_schema: Path
    output_last_message: Path

    def environment_dict(self) -> dict[str, str]:
        """Return a fresh exact mapping suitable for a future ``execve``."""

        return dict(self.environment)

    @property
    def argv_canonical_json(self) -> bytes:
        return _canonical_json_bytes(list(self.argv))

    @property
    def environment_canonical_json(self) -> bytes:
        return _canonical_json_bytes(self.environment_dict())

    @property
    def argv_sha256(self) -> str:
        return hashlib.sha256(self.argv_canonical_json).hexdigest()

    @property
    def environment_sha256(self) -> str:
        return hashlib.sha256(self.environment_canonical_json).hexdigest()


@dataclass
class _HeldRepositoryRoot:
    path: Path
    descriptor: int
    identity: tuple[int, ...]
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True


_SKILL_ENTRYPOINTS = (
    SkillEntrypoint(
        "imagegen",
        "skills/.system/imagegen/SKILL.md",
        "59981d23519222bcecf1be48bb37730bbc50539ceb0e35ad09fcef98a3df19d3",
        24_003,
    ),
    SkillEntrypoint(
        "openai-docs",
        "skills/.system/openai-docs/SKILL.md",
        "669a42ccf3323fe0ceda6e466730bcb05dddf1e0c220d6523ea504909fc49165",
        18_747,
    ),
    SkillEntrypoint(
        "plugin-creator",
        "skills/.system/plugin-creator/SKILL.md",
        "8fd56316b2c49cbdc657a5d197967a233018e1fada65b00a5dd030dce6499a6e",
        11_040,
    ),
    SkillEntrypoint(
        "skill-creator",
        "skills/.system/skill-creator/SKILL.md",
        "da44c88f6b3845a8fa8c60792ec9a722110a55a9793c279757b48fefb11f819c",
        22_047,
    ),
    SkillEntrypoint(
        "skill-installer",
        "skills/.system/skill-installer/SKILL.md",
        "d68b77e5bbb34dedab89d134da52855f140fc4b4299b80104f534e3b9e98f8ee",
        3_367,
    ),
)

_PROMPT_PROBE = PromptProbeManifest(
    token=PROMPT_PROBE_TOKEN,
    sentinel=PROMPT_PROBE_SENTINEL,
    vector_schema=PROMPT_PROBE_VECTOR_SCHEMA,
    envelope_prefix_sha256=PROMPT_PROBE_PREFIX_SHA256,
    vector_sha256=PROMPT_PROBE_VECTOR_SHA256,
    row_roles=("developer", "developer", "user"),
    system_skill_tree_entries=SYSTEM_SKILL_TREE_ENTRIES,
    system_skill_tree_manifest_sha256=SYSTEM_SKILL_TREE_MANIFEST_SHA256,
    system_skill_entrypoints=_SKILL_ENTRYPOINTS,
)


def _fail(message: str) -> ProducerAdapterError:
    return ProducerAdapterError(f"{message} (B2 §5/§6/§18)")


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(f"{label} must be one JSON object")
    actual = set(value)
    if actual != expected:
        raise _fail(
            f"{label} fields differ: missing={sorted(expected - actual)} "
            f"unknown={sorted(actual - expected)}"
        )
    return value


def _strict_json(data: bytes, label: str) -> object:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite number {value!r}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _fail(f"{label} is not strict UTF-8 JSON: {exc}") from exc


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _fail("producer invocation is not canonical-JSON encodable") from exc


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _inspect_canonical_root(
    repository_root: Path,
) -> tuple[Path, os.stat_result]:
    if not isinstance(repository_root, Path) or not repository_root.is_absolute():
        raise _fail("research root must be a trusted absolute Path")
    try:
        resolved = repository_root.resolve(strict=True)
        metadata = repository_root.lstat()
    except OSError as exc:
        raise _fail("research root cannot be inspected") from exc
    if resolved != repository_root or not stat.S_ISDIR(metadata.st_mode):
        raise _fail("research root must be canonical and non-symlinked")
    if not (repository_root / "system" / "schemas").is_dir() or not (
        repository_root / "trees"
    ).is_dir():
        raise _fail("research root lacks the proved repository markers")
    return repository_root, metadata


def _canonical_root(repository_root: Path) -> Path:
    root, _metadata = _inspect_canonical_root(repository_root)
    return root


def _open_directory_at(parent_fd: int, name: str, label: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _fail(f"{label} is not a stable real directory") from exc
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = _directory_identity(opened)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or identity != _directory_identity(before)
            or identity != _directory_identity(named)
        ):
            raise _fail(f"{label} changed while its directory was opened")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _require_held_root(binding: _HeldRepositoryRoot) -> None:
    if binding.closed:
        raise _fail("held research root is already closed")
    try:
        opened = os.fstat(binding.descriptor)
        named = binding.path.lstat()
    except OSError as exc:
        raise _fail("research root changed after descriptor capture") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or _directory_identity(opened) != binding.identity
        or _directory_identity(named) != binding.identity
    ):
        raise _fail("research root changed after descriptor capture")


def _hold_canonical_root(repository_root: Path) -> _HeldRepositoryRoot:
    root, inspected = _inspect_canonical_root(repository_root)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise _fail("research root cannot be descriptor-opened safely") from exc
    binding = _HeldRepositoryRoot(
        path=root,
        descriptor=descriptor,
        identity=_directory_identity(inspected),
    )
    try:
        _require_held_root(binding)
        system_fd = _open_directory_at(descriptor, "system", "research system marker")
        try:
            schemas_fd = _open_directory_at(
                system_fd, "schemas", "research schemas marker"
            )
            os.close(schemas_fd)
        finally:
            os.close(system_fd)
        trees_fd = _open_directory_at(descriptor, "trees", "research trees marker")
        os.close(trees_fd)
        _require_held_root(binding)
        return binding
    except Exception:
        binding.close()
        raise


def _require_literal_path(path: Path) -> None:
    spelling = str(path)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in spelling) or any(
        char in spelling for char in ('"', "\\")
    ):
        raise _fail("trusted producer path cannot be represented literally in config")


def _bound_git(
    binding: _HeldRepositoryRoot,
    arguments: tuple[str, ...],
    label: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        binary = _GIT_BINARY.lstat()
    except OSError as exc:
        raise _fail("controlled Git binary is unavailable") from exc
    if (
        not stat.S_ISREG(binary.st_mode)
        or binary.st_nlink < 1
        or not binary.st_mode & stat.S_IXUSR
        or binary.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise _fail("controlled Git binary has unsafe filesystem identity")
    _require_held_root(binding)

    def enter_held_repository() -> None:
        os.fchdir(binding.descriptor)

    result = subprocess.run(
        [str(_GIT_BINARY), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=None,
        env=dict(_CONTROLLED_GIT_ENV),
        pass_fds=(binding.descriptor,),
        preexec_fn=enter_held_repository,
        check=False,
    )
    _require_held_root(binding)
    if result.returncode != 0 or result.stderr:
        raise _fail(f"{label} controlled Git query failed")
    return result


def _git_bytes(binding: _HeldRepositoryRoot, spec: str, label: str) -> bytes:
    result = _bound_git(binding, ("show", spec), label)
    if result.returncode != 0:
        raise _fail(f"{label} has no exact Git identity at {spec!r}")
    return result.stdout


def _git_mode(
    binding: _HeldRepositoryRoot, relative_path: str, label: str
) -> str:
    result = _bound_git(
        binding,
        ("ls-files", "--stage", "--", relative_path),
        label,
    )
    try:
        rows = result.stdout.decode("ascii", "strict").splitlines()
    except UnicodeDecodeError as exc:
        raise _fail(f"{label} Git index mode is not ASCII") from exc
    if result.returncode != 0 or len(rows) != 1:
        raise _fail(f"{label} has no unique Git index mode")
    return rows[0].split(" ", 1)[0]


def _open_directory_chain(
    binding: _HeldRepositoryRoot,
    parts: tuple[str, ...],
    label: str,
) -> tuple[list[int], list[tuple[int, ...]]]:
    descriptors = [os.dup(binding.descriptor)]
    identities = [binding.identity]
    try:
        for part in parts:
            child = _open_directory_at(descriptors[-1], part, label)
            descriptors.append(child)
            identities.append(_directory_identity(os.fstat(child)))
        return descriptors, identities
    except Exception:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _require_directory_chain(
    binding: _HeldRepositoryRoot,
    parts: tuple[str, ...],
    descriptors: list[int],
    identities: list[tuple[int, ...]],
    label: str,
) -> None:
    _require_held_root(binding)
    if len(descriptors) != len(parts) + 1 or len(identities) != len(descriptors):
        raise _fail(f"{label} directory binding is malformed")
    for index, part in enumerate(parts):
        try:
            parent = os.fstat(descriptors[index])
            child = os.fstat(descriptors[index + 1])
            named = os.stat(
                part,
                dir_fd=descriptors[index],
                follow_symlinks=False,
            )
        except OSError as exc:
            raise _fail(f"{label} directory binding disappeared") from exc
        if (
            _directory_identity(parent) != identities[index]
            or _directory_identity(child) != identities[index + 1]
            or _directory_identity(named) != identities[index + 1]
            or not stat.S_ISDIR(child.st_mode)
        ):
            raise _fail(f"{label} directory binding changed during stable read")


def _stable_repository_file(
    binding: _HeldRepositoryRoot,
    relative_path: str,
    label: str,
    *,
    executable: bool,
    allow_uncommitted: bool,
) -> bytes:
    if relative_path not in {REGISTRY_RELPATH, LAUNCHER_RELPATH, RETURN_SCHEMA_RELPATH}:
        raise _fail(f"{label} path is not a fixed adapter path")
    parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise _fail(f"{label} path is not normalized")
    directories, directory_identities = _open_directory_chain(
        binding, parts[:-1], label
    )
    parent_fd = directories[-1]
    name = parts[-1]
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise _fail(f"{label} cannot be opened without following links") from exc
        try:
            opened = os.fstat(descriptor)
            mode = stat.S_IMODE(opened.st_mode)
            valid_mode = (
                stat.S_ISREG(opened.st_mode)
                and opened.st_nlink == 1
                and opened.st_uid == os.getuid()
                and (
                    (executable and mode in {0o555, 0o755})
                    or (not executable and mode == 0o644)
                )
            )
            identity = _file_identity(opened)
            if not valid_mode or identity != _file_identity(before):
                raise _fail(f"{label} has wrong type/owner/mode/link count")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise _fail(f"{label} pathname disappeared during stable read") from exc
        if identity != _file_identity(after) or identity != _file_identity(named):
            raise _fail(f"{label} changed during stable read")
        data = b"".join(chunks)
        if len(data) != opened.st_size:
            raise _fail(f"{label} changed length during stable read")
        _require_directory_chain(
            binding,
            parts[:-1],
            directories,
            directory_identities,
            label,
        )
        if not allow_uncommitted:
            if data != _git_bytes(binding, f"HEAD:{relative_path}", label):
                raise _fail(f"{label} differs from HEAD")
            if data != _git_bytes(binding, f":{relative_path}", label):
                raise _fail(f"{label} differs from the Git index")
            expected_mode = "100755" if executable else "100644"
            if _git_mode(binding, relative_path, label) != expected_mode:
                raise _fail(f"{label} has wrong Git executable mode")
        _require_directory_chain(
            binding,
            parts[:-1],
            directories,
            directory_identities,
            label,
        )
        return data
    finally:
        for directory in reversed(directories):
            os.close(directory)


def _entrypoint_dict(value: SkillEntrypoint) -> dict[str, object]:
    return {
        "name": value.name,
        "relative_path": value.relative_path,
        "sha256": value.sha256,
        "bytes": value.bytes,
    }


def _validate_prompt_probe(value: object) -> PromptProbeManifest:
    row = _exact_keys(
        value,
        {
            "token",
            "sentinel",
            "vector_schema",
            "envelope_prefix_sha256",
            "vector_sha256",
            "row_roles",
            "system_skill_tree_entries",
            "system_skill_tree_manifest_sha256",
            "system_skill_entrypoints",
        },
        "producer prompt-probe row",
    )
    expected = {
        "token": _PROMPT_PROBE.token,
        "sentinel": _PROMPT_PROBE.sentinel,
        "vector_schema": _PROMPT_PROBE.vector_schema,
        "envelope_prefix_sha256": _PROMPT_PROBE.envelope_prefix_sha256,
        "vector_sha256": _PROMPT_PROBE.vector_sha256,
        "row_roles": list(_PROMPT_PROBE.row_roles),
        "system_skill_tree_entries": _PROMPT_PROBE.system_skill_tree_entries,
        "system_skill_tree_manifest_sha256": (
            _PROMPT_PROBE.system_skill_tree_manifest_sha256
        ),
        "system_skill_entrypoints": [
            _entrypoint_dict(item) for item in _PROMPT_PROBE.system_skill_entrypoints
        ],
    }
    if row != expected:
        raise _fail("producer prompt-probe row drifted from the closed contract")
    return _PROMPT_PROBE


def load_fixed_adapter_manifest(
    repository_root: Path,
    *,
    _allow_uncommitted: bool = False,
) -> FixedProducerAdapter:
    """Load and rebind the sole committed B2 producer adapter.

    ``_allow_uncommitted`` exists only for pre-commit acceptance tests.  Normal
    callers also require worktree/index/HEAD equality and exact Git modes.
    """

    binding = _hold_canonical_root(repository_root)
    try:
        adapter = _load_fixed_adapter_manifest(
            binding,
            allow_uncommitted=_allow_uncommitted,
        )
        _require_held_root(binding)
        return adapter
    finally:
        binding.close()


def _load_fixed_adapter_manifest(
    binding: _HeldRepositoryRoot,
    *,
    allow_uncommitted: bool,
) -> FixedProducerAdapter:
    root = binding.path
    registry_bytes = _stable_repository_file(
        binding,
        REGISTRY_RELPATH,
        "producer adapter registry",
        executable=False,
        allow_uncommitted=allow_uncommitted,
    )
    registry = _exact_keys(
        _strict_json(registry_bytes, "producer adapter registry"),
        {"schema_version", "adapters"},
        "producer adapter registry",
    )
    if registry["schema_version"] != REGISTRY_SCHEMA:
        raise _fail("producer adapter registry has a foreign schema version")
    adapters = registry["adapters"]
    if not isinstance(adapters, list) or len(adapters) != 1:
        raise _fail("producer adapter registry must contain exactly one row")
    row = _exact_keys(
        adapters[0],
        {
            "adapter_token",
            "transcript_format",
            "binary",
            "binary_sha256",
            "version",
            "model",
            "reasoning_effort",
            "launcher_token",
            "launcher_repository_relpath",
            "launcher_sha256",
            "producer_return_schema",
            "prompt_probe",
        },
        "producer adapter row",
    )
    fixed_scalars = {
        "adapter_token": ADAPTER_TOKEN,
        "transcript_format": TRANSCRIPT_FORMAT,
        "binary": str(PINNED_BINARY),
        "binary_sha256": PINNED_BINARY_SHA256,
        "version": PINNED_VERSION,
        "model": PINNED_MODEL,
        "reasoning_effort": PINNED_REASONING_EFFORT,
        "launcher_token": LAUNCHER_TOKEN,
        "launcher_repository_relpath": LAUNCHER_RELPATH,
    }
    for field, expected in fixed_scalars.items():
        if row[field] != expected:
            raise _fail(f"producer adapter {field} drifted from the closed contract")

    launcher_sha256 = row["launcher_sha256"]
    if not isinstance(launcher_sha256, str) or len(launcher_sha256) != 64:
        raise _fail("producer launcher SHA is not lowercase SHA-256")
    launcher_bytes = _stable_repository_file(
        binding,
        LAUNCHER_RELPATH,
        "producer launcher",
        executable=True,
        allow_uncommitted=allow_uncommitted,
    )
    if hashlib.sha256(launcher_bytes).hexdigest() != launcher_sha256:
        raise _fail("producer launcher bytes do not match the registry SHA")

    schema = _exact_keys(
        row["producer_return_schema"],
        {"repository_relpath", "sha256", "bytes"},
        "producer-return schema binding",
    )
    expected_schema = {
        "repository_relpath": RETURN_SCHEMA_RELPATH,
        "sha256": RETURN_SCHEMA_SHA256,
        "bytes": RETURN_SCHEMA_BYTES,
    }
    if schema != expected_schema:
        raise _fail("producer-return schema binding drifted from the closed contract")
    schema_bytes = _stable_repository_file(
        binding,
        RETURN_SCHEMA_RELPATH,
        "producer-return schema",
        executable=False,
        allow_uncommitted=allow_uncommitted,
    )
    if len(schema_bytes) != RETURN_SCHEMA_BYTES or (
        hashlib.sha256(schema_bytes).hexdigest() != RETURN_SCHEMA_SHA256
    ):
        raise _fail("producer-return schema bytes do not match the registry binding")

    prompt_probe = _validate_prompt_probe(row["prompt_probe"])
    return FixedProducerAdapter(
        repository_root=root,
        adapter_token=ADAPTER_TOKEN,
        transcript_format=TRANSCRIPT_FORMAT,
        binary=PINNED_BINARY,
        binary_sha256=PINNED_BINARY_SHA256,
        version=PINNED_VERSION,
        model=PINNED_MODEL,
        reasoning_effort=PINNED_REASONING_EFFORT,
        launcher_token=LAUNCHER_TOKEN,
        launcher=RepositoryFileBinding(
            LAUNCHER_RELPATH, launcher_sha256, len(launcher_bytes)
        ),
        producer_return_schema=RepositoryFileBinding(
            RETURN_SCHEMA_RELPATH, RETURN_SCHEMA_SHA256, RETURN_SCHEMA_BYTES
        ),
        prompt_probe=prompt_probe,
    )


def _require_loaded_adapter(adapter: FixedProducerAdapter) -> None:
    if not isinstance(adapter, FixedProducerAdapter):
        raise _fail("producer adapter must be the closed loaded manifest value")
    current = load_fixed_adapter_manifest(
        adapter.repository_root,
        _allow_uncommitted=True,
    )
    if adapter != current:
        raise _fail("producer adapter object differs from the fixed registry bytes")


def _stable_binary_identity(
    path: Path,
) -> tuple[str, bytes, os.stat_result]:
    if path != PINNED_BINARY or not path.is_absolute():
        raise _fail("producer binary path is not the pinned absolute path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _fail("pinned producer binary is unavailable or aliased") from exc
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or not mode & stat.S_IXUSR
            or mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise _fail("pinned producer binary has wrong type/owner/mode/link count")
        digest = hashlib.sha256()
        header = b""
        while True:
            chunk = os.read(descriptor, 4 * 1024 * 1024)
            if not chunk:
                break
            if len(header) < 8:
                header += chunk[: 8 - len(header)]
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    named = path.lstat()
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise _fail("pinned producer binary changed during stable read")
    if identity != (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns):
        raise _fail("pinned producer binary pathname changed during stable read")
    return digest.hexdigest(), header, before


def prove_pinned_codex_binary(
    adapter: FixedProducerAdapter,
) -> InstalledBinaryProof:
    """Read-only prove the exact local arm64 Codex binary and ``--version``."""

    _require_loaded_adapter(adapter)
    digest, header, metadata = _stable_binary_identity(adapter.binary)
    if digest != adapter.binary_sha256:
        raise _fail("pinned producer binary SHA drifted")
    if len(header) < 8:
        raise _fail("pinned producer binary is not a native arm64 Mach-O")
    magic, cpu_type = struct.unpack("<II", header)
    if magic != 0xFEEDFACF or cpu_type != 0x0100000C:
        raise _fail("pinned producer binary is not a native arm64 Mach-O")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise _fail("pinned producer binary is not native to this host")
    result = subprocess.run(
        [str(adapter.binary), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/",
        env={},
        timeout=10,
        check=False,
    )
    expected_stdout = (adapter.version + "\n").encode("ascii")
    if result.returncode != 0 or result.stdout != expected_stdout or result.stderr:
        raise _fail("pinned producer binary --version output drifted")
    return InstalledBinaryProof(
        path=adapter.binary,
        sha256=adapter.binary_sha256,
        bytes=metadata.st_size,
        version=adapter.version,
        format="Mach-O 64-bit executable arm64",
    )


def derive_trusted_session_paths(
    repository_root: Path, session_id: str
) -> TrustedSessionPaths:
    """Derive every producer path from one proved root and canonical UUID."""

    root = _canonical_root(repository_root)
    if not isinstance(session_id, str):
        raise _fail("session ID must be a canonical lowercase UUID string")
    try:
        parsed = UUID(session_id)
    except (ValueError, AttributeError) as exc:
        raise _fail("session ID must be a canonical lowercase UUID string") from exc
    if str(parsed) != session_id or parsed.version not in {1, 2, 3, 4, 5}:
        raise _fail("session ID must be a canonical lowercase UUID string")
    session_root = root / "var" / "runtime" / "sessions" / session_id
    paths = TrustedSessionPaths(
        repository_root=root,
        session_id=session_id,
        session_root=session_root,
        reference_snapshot=session_root / "reference-snapshot",
        producer_home=session_root / "producer-home",
        producer_sqlite=session_root / "producer-sqlite",
        producer_tmp=session_root / "producer-tmp",
        producer_return_schema=session_root / "producer-return-schema.json",
        output_last_message=session_root
        / "producer-tmp"
        / "output-last-message.json",
    )
    for path in paths.__dict__.values():
        if isinstance(path, Path):
            _require_literal_path(path)
    return paths


def _require_trusted_session_paths(paths: TrustedSessionPaths) -> None:
    if not isinstance(paths, TrustedSessionPaths):
        raise _fail("producer paths must be the closed derived session value")
    expected = derive_trusted_session_paths(paths.repository_root, paths.session_id)
    if paths != expected:
        raise _fail("producer paths differ from the canonical session derivation")


def _skills_config(home: Path) -> str:
    rows = ",".join(
        f'{{path="{home / item.relative_path}",enabled=false}}'
        for item in _SKILL_ENTRYPOINTS
    )
    return f"skills.config=[{rows}]"


def build_fixed_codex_invocation(
    adapter: FixedProducerAdapter,
    paths: TrustedSessionPaths,
) -> FixedProducerInvocation:
    """Construct the sole exact Codex argv/environment/cwd tuple."""

    _require_loaded_adapter(adapter)
    _require_trusted_session_paths(paths)
    if adapter.repository_root != paths.repository_root:
        raise _fail("adapter and session paths belong to different research roots")
    argv = (
        str(adapter.binary),
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
        adapter.model,
        "-c",
        f'model_reasoning_effort="{adapter.reasoning_effort}"',
        "-c",
        'default_permissions="snapshot_read"',
        "-c",
        PERMISSIONS_CONFIG,
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
        SHELL_ENVIRONMENT_CONFIG,
        "-c",
        _skills_config(paths.producer_home),
        "--output-schema",
        str(paths.producer_return_schema),
        "--output-last-message",
        str(paths.output_last_message),
        "-",
    )
    environment = (
        ("HOME", str(paths.producer_home)),
        ("CODEX_HOME", str(paths.producer_home)),
        ("CODEX_SQLITE_HOME", str(paths.producer_sqlite)),
        ("TMPDIR", str(paths.producer_tmp)),
        ("PATH", HOST_PATH),
        ("SHELL", "/bin/zsh"),
        ("LANG", "en_US.UTF-8"),
        ("LC_ALL", "en_US.UTF-8"),
        ("TERM", "dumb"),
        ("GIT_CONFIG_GLOBAL", "/dev/null"),
        ("GIT_CONFIG_NOSYSTEM", "1"),
    )
    return FixedProducerInvocation(
        argv=argv,
        environment=environment,
        cwd=paths.reference_snapshot,
        output_schema=paths.producer_return_schema,
        output_last_message=paths.output_last_message,
    )


def require_exact_codex_invocation(
    candidate: FixedProducerInvocation,
    adapter: FixedProducerAdapter,
    paths: TrustedSessionPaths,
) -> FixedProducerInvocation:
    """Reject any caller-selected argument, environment, cwd, or output path."""

    if not isinstance(candidate, FixedProducerInvocation):
        raise _fail("producer invocation must be the closed invocation value")
    expected = build_fixed_codex_invocation(adapter, paths)
    if candidate != expected:
        raise _fail("producer invocation differs from the closed argv/env/path contract")
    return expected


def adapter_binding(adapter: FixedProducerAdapter) -> Mapping[str, object]:
    """Return immutable request/packet identity fields for later runtime code."""

    _require_loaded_adapter(adapter)
    expected = {
        "producer_adapter_token": ADAPTER_TOKEN,
        "producer_launcher_token": LAUNCHER_TOKEN,
        "producer_launcher_sha256": adapter.launcher.sha256,
        "producer_return_schema_source_sha256": (
            adapter.producer_return_schema.sha256
        ),
        "producer_return_schema_copy_sha256": (
            adapter.producer_return_schema.sha256
        ),
        "transcript_format": TRANSCRIPT_FORMAT,
        "approval_required": True,
    }
    return MappingProxyType(expected)


__all__ = (
    "ADAPTER_TOKEN",
    "FixedProducerAdapter",
    "FixedProducerInvocation",
    "InstalledBinaryProof",
    "LAUNCHER_TOKEN",
    "PINNED_BINARY_SHA256",
    "PINNED_VERSION",
    "ProducerAdapterError",
    "RETURN_SCHEMA_SHA256",
    "TRANSCRIPT_FORMAT",
    "TrustedSessionPaths",
    "adapter_binding",
    "build_fixed_codex_invocation",
    "derive_trusted_session_paths",
    "load_fixed_adapter_manifest",
    "prove_pinned_codex_binary",
    "require_exact_codex_invocation",
)
