"""Strict read-only loaders for the committed B2 role foundation registries.

This module deliberately does not load a producer adapter.  The production
adapter registry becomes truthful only when its separately reviewed launcher
bytes exist; the foundation lane must not mint a placeholder digest.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Any

from . import gitutil
from .errors import HtError
from .paths import normalize_repository_relpath
from .runtime.schema import strict_loads


PROFILE_ORDER = (
    "principal-coordinator-v1",
    "l4-director-v1",
    "senior-v1",
    "junior-v1",
    "checker-v1",
    "verifier-v1",
)

PROFILE_CONTRACT = {
    "principal-coordinator-v1": (
        "pc",
        "top",
        ("pc-wake",),
        (
            "pc.route-subgoal",
            "pc.rerank-issue-queue",
            "pc.annotate-ratification",
            "pc.no-op",
        ),
    ),
    "l4-director-v1": (
        "director",
        "L4",
        ("director-inbox",),
        (
            "director.create-dispatch",
            "director.raise-interrupt",
            "director.recall-dispatch",
        ),
    ),
    "senior-v1": (
        "unit",
        "L4",
        ("senior-plan", "senior-report"),
        ("senior.submit-plan", "senior.submit-report"),
    ),
    "junior-v1": (
        "unit",
        "L4",
        ("junior-task",),
        ("junior.return-unit-artifact",),
    ),
    "checker-v1": (
        "unit",
        "L4",
        ("checker-qa",),
        ("checker.return-qa",),
    ),
    "verifier-v1": (
        "verifier",
        "L4",
        ("verifier-adjudication",),
        ("verifier.adjudicate-report",),
    ),
}

PROFILE_PACKET_CONTRACT = {
    "principal-coordinator-v1": (
        "principal-coordinator",
        "repository-file#system/roles/principal-coordinator.v1.md",
        "v1",
        "f58a40d6f6c6b88799aa50d3f9cd50b84ad42aa4688acf4bdf438a6f6659f1fa",
    ),
    "l4-director-v1": (
        "l4-director",
        "repository-file#system/roles/l4-director.v1.md",
        "v1",
        "267008c5b9b56a3ea1b5d3993558112248bc79f5159231fc63b4d02feec0df31",
    ),
    "senior-v1": (
        "senior",
        "repository-file#system/roles/senior.v1.md",
        "v1",
        "469807e2ba414c8d97a204c2326c08ae813ef9294b6c4f364c5e82cf5b4b53fe",
    ),
    "junior-v1": (
        "junior",
        "repository-file#system/roles/junior.v1.md",
        "v1",
        "5e48b8d3357fae0ec0b4b67aa561ba0fa92a59b70e122dda2b70bb381a842c72",
    ),
    "checker-v1": (
        "checker",
        "repository-file#system/roles/checker.v1.md",
        "v1",
        "7249e63e3403e215d300bfbefcb35eb00638ccfdf27a1b3c2ccac635dc1b5fbc",
    ),
    "verifier-v1": (
        "verifier",
        "repository-file#system/roles/verifier.v1.md",
        "v1",
        "cc586fbe1999fe72f9e91f0d75403766c08b5cb302b250b2ab0a10d8c07800e9",
    ),
}

VERIFIER_PROCEDURE_CONTRACT = {
    "ref": "repository-file#system/roles/verifier-procedure.v1.md",
    "version": "v1",
    "sha256": "378b69f6a591fa62d846d4e333aea9ba1053c181eeaa7a7b7a0bc0bcdc480f27",
}

PROJECTION_ORDER = (
    "issue",
    "issue-queue",
    "ratification-item",
    "subgoal",
    "task-package",
    "action-receipt",
    "inbox-delivery",
    "inbox-receipt",
    "dispatch",
    "plan|report|unit-artifact|qa",
    "schema-declared-bounded-json",
    "committed-methodology-text",
)

PROJECTION_TOKENS = (
    "hypothesis-tree-semantic-issue/1.0.0",
    "hypothesis-tree-semantic-issue-queue/1.0.0",
    "hypothesis-tree-semantic-ratification-item/1.0.0",
    "hypothesis-tree-semantic-subgoal/1.0.0",
    "hypothesis-tree-semantic-task-package/1.0.0",
    "hypothesis-tree-semantic-action-receipt/1.0.0",
    "hypothesis-tree-semantic-inbox-delivery/1.0.0",
    "hypothesis-tree-semantic-inbox-receipt/1.0.0",
    "hypothesis-tree-semantic-dispatch/1.0.0",
    "hypothesis-tree-semantic-artifact-text/1.0.0",
    "hypothesis-tree-semantic-bounded-json/1.0.0",
    "hypothesis-tree-semantic-methodology-text/1.0.0",
)

MANIFEST_PACKET_ORDER = (
    ("senior", "v0-draft"),
    ("junior", "v0-draft"),
    ("checker", "v0-draft"),
    ("verifier", "v0-draft"),
    ("senior", "v1"),
    ("junior", "v1"),
    ("checker", "v1"),
    ("verifier", "v1"),
    ("principal-coordinator", "v1"),
    ("l4-director", "v1"),
)

EXPECTED_PROJECTION_POLICIES = (
    {"mode": "select", "fields": ["id", "title", "provenance", "scope", "question", "done_definition", "lanes", "subgoals", "observatory_attachments", "status", "closure"], "nested": {}},
    {"mode": "select", "fields": ["entries"], "nested": {}},
    {"mode": "select", "fields": ["id", "kind", "payload_ref", "text", "queued_by", "date", "disposition", "annotations"], "nested": {"annotations": ["date", "note"]}},
    {"mode": "select", "fields": ["id", "issue_ref", "lane", "question", "done_definition"], "nested": {}},
    {"mode": "select", "fields": ["package_kind", "role_profile_id", "target_ref", "issue_ref", "subgoal_ref", "dispatch_ref", "semantic_brief", "reference_map", "workspace", "output_contract", "source_action_ref"], "nested": {"reference_map": ["ref", "use_when"]}},
    {"mode": "select", "fields": ["id", "action_kind", "target_ref", "payload", "profile_id", "role_packet", "task_package_ref", "outputs", "created_at"], "nested": {}},
    {"mode": "select", "fields": ["delivery_id", "subgoal_ref", "issue_ref", "lane", "profile_id", "task_package_ref", "created_by_action_ref", "retry_of", "created_at"], "nested": {}},
    {"mode": "select", "fields": ["delivery_id", "subgoal_ref", "issue_ref", "lane", "profile_id", "task_package_ref", "received_at"], "nested": {}},
    {"mode": "select", "fields": ["id", "node", "issue_ref", "subgoal_ref", "question", "done_definition", "plan_ref", "steers", "interrupt", "outcome", "epoch", "adjudications", "report_ref", "archive_ref", "report_hash", "director_note"], "nested": {}},
    {"mode": "registered-raw-utf8", "fields": [], "nested": {}},
    {"mode": "recursive-deny", "fields": ["action_ref", "created_by_action_ref", "packet_sha256", "request_id", "binding_id", "lease_epoch", "session_id", "fence", "capture_manifest_sha256", "source_action_sha256"], "nested": {}},
    {"mode": "raw-utf8-negative-scan", "fields": [], "nested": {}},
)


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HtError(f"{label} must be one JSON object (B2 §5)")
    actual = set(value)
    if actual != expected:
        raise HtError(
            f"{label} fields differ: missing={sorted(expected - actual)} "
            f"unknown={sorted(actual - expected)} (B2 §5)"
        )
    return value


def _stable_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class _RepositoryRead:
    """One descriptor-rooted custody context for a complete logical load."""

    def __init__(self, repository_root: Path, *, require_git: bool) -> None:
        try:
            self.root = repository_root.resolve(strict=True)
        except OSError as exc:
            raise HtError("B2 role repository root cannot be resolved safely") from exc
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self.root_fd = os.open(self.root, flags)
        except OSError as exc:
            raise HtError("B2 role repository root cannot be held safely") from exc
        self.git_binding: gitutil.RepositoryBinding | None = None
        try:
            root_info = os.fstat(self.root_fd)
            if not stat.S_ISDIR(root_info.st_mode):
                raise HtError("B2 role repository root is not a directory")
            self.root_identity = (root_info.st_dev, root_info.st_ino)
            self._validate_root_binding()
            if require_git:
                self.git_binding = gitutil.capture_repository_binding(
                    self.root,
                    self.root_fd,
                    self.root_identity,
                )
                if self.git_binding is None:
                    raise HtError("B2 role repository has no exact Git identity (B2 §5)")
                self._validate_git_binding()
        except Exception:
            self.close()
            raise

    def __enter__(self) -> _RepositoryRead:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        binding = getattr(self, "git_binding", None)
        if binding is not None:
            binding.close()
            self.git_binding = None
        root_fd = getattr(self, "root_fd", None)
        if root_fd is not None:
            os.close(root_fd)
            self.root_fd = None  # type: ignore[assignment]

    def _validate_root_binding(self) -> None:
        try:
            held = os.fstat(self.root_fd)
            visible = self.root.lstat()
        except OSError as exc:
            raise HtError("B2 role repository root binding disappeared") from exc
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(visible.st_mode)
            or (held.st_dev, held.st_ino) != self.root_identity
            or (visible.st_dev, visible.st_ino) != self.root_identity
        ):
            raise HtError("B2 role repository root binding changed")

    def _validate_git_binding(self) -> None:
        binding = self.git_binding
        if binding is None or binding.closed:
            raise HtError("B2 role repository Git binding is unavailable")
        self._validate_root_binding()
        try:
            held_git = os.fstat(binding.git_dir_fd)
            held_common = os.fstat(binding.common_dir_fd)
            visible_git = binding.git_dir_path.lstat()
            visible_common = binding.common_dir_path.lstat()
        except OSError as exc:
            raise HtError("B2 role repository Git binding disappeared") from exc
        if (
            not stat.S_ISDIR(visible_git.st_mode)
            or not stat.S_ISDIR(visible_common.st_mode)
            or (held_git.st_dev, held_git.st_ino) != binding.git_dir_identity
            or (visible_git.st_dev, visible_git.st_ino) != binding.git_dir_identity
            or (held_common.st_dev, held_common.st_ino) != binding.common_dir_identity
            or (visible_common.st_dev, visible_common.st_ino)
            != binding.common_dir_identity
        ):
            raise HtError("B2 role repository Git binding changed")

    def _git_bytes(self, spec: str, label: str) -> bytes:
        self._validate_git_binding()
        assert self.git_binding is not None
        result = gitutil._controlled_run(
            self.root,
            ["show", spec],
            check=False,
            text=False,
            git_dir_fd=self.git_binding.git_dir_fd,
        )
        self._validate_git_binding()
        if result.returncode != 0:
            raise HtError(f"{label} has no exact Git identity at {spec!r} (B2 §5)")
        return result.stdout

    def stable_file_bytes(
        self,
        relative_path: str,
        label: str,
        *,
        allow_uncommitted: bool,
    ) -> bytes:
        try:
            relative = normalize_repository_relpath(relative_path)
        except Exception as exc:
            raise HtError(
                f"{label} has a non-normalized repository path (B2 §5)"
            ) from exc
        parts = relative.split("/")
        directory_fds = [self.root_fd]
        directory_identities = [self.root_identity]
        leaf_fd: int | None = None
        try:
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            for component in parts[:-1]:
                try:
                    descriptor = os.open(
                        component,
                        directory_flags,
                        dir_fd=directory_fds[-1],
                    )
                except OSError as exc:
                    raise HtError(
                        f"{label} has a symlinked or non-directory ancestor (B2 §5)"
                    ) from exc
                try:
                    metadata = os.fstat(descriptor)
                except OSError as exc:
                    os.close(descriptor)
                    raise HtError(
                        f"{label} ancestor cannot be inspected safely (B2 §5)"
                    ) from exc
                if not stat.S_ISDIR(metadata.st_mode):
                    os.close(descriptor)
                    raise HtError(
                        f"{label} has a symlinked or non-directory ancestor (B2 §5)"
                    )
                directory_fds.append(descriptor)
                directory_identities.append((metadata.st_dev, metadata.st_ino))

            leaf_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                leaf_fd = os.open(parts[-1], leaf_flags, dir_fd=directory_fds[-1])
            except OSError as exc:
                raise HtError(
                    f"{label} cannot be descriptor-opened safely: {relative} (B2 §5)"
                ) from exc
            before = os.fstat(leaf_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o644
                or before.st_nlink != 1
            ):
                raise HtError(f"{label} has wrong type/mode/link count (B2 §5)")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(leaf_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            if len(data) != before.st_size:
                raise HtError(f"{label} length changed during stable read (B2 §5)")

            if not allow_uncommitted:
                head = self._git_bytes(f"HEAD:{relative}", label)
                index = self._git_bytes(f":{relative}", label)
                if data != head or data != index:
                    raise HtError(
                        f"{label} worktree/index/HEAD bytes differ (B2 §5)"
                    )

            after = os.fstat(leaf_fd)
            try:
                named = os.stat(
                    parts[-1],
                    dir_fd=directory_fds[-1],
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise HtError(f"{label} disappeared during stable read (B2 §5)") from exc
            identity = _stable_identity(before)
            if _stable_identity(after) != identity or _stable_identity(named) != identity:
                raise HtError(f"{label} changed during stable read (B2 §5)")

            for index, component in enumerate(parts[:-1], start=1):
                try:
                    visible = os.stat(
                        component,
                        dir_fd=directory_fds[index - 1],
                        follow_symlinks=False,
                    )
                    held = os.fstat(directory_fds[index])
                except OSError as exc:
                    raise HtError(f"{label} ancestor binding disappeared (B2 §5)") from exc
                if (
                    not stat.S_ISDIR(visible.st_mode)
                    or not stat.S_ISDIR(held.st_mode)
                    or (visible.st_dev, visible.st_ino) != directory_identities[index]
                    or (held.st_dev, held.st_ino) != directory_identities[index]
                ):
                    raise HtError(f"{label} ancestor binding changed (B2 §5)")
            self._validate_root_binding()
            return data
        finally:
            if leaf_fd is not None:
                os.close(leaf_fd)
            for descriptor in reversed(directory_fds[1:]):
                os.close(descriptor)


def _strict_file(
    reader: _RepositoryRead,
    relative_path: str,
    label: str,
    *,
    allow_uncommitted: bool,
) -> Any:
    return strict_loads(
        reader.stable_file_bytes(
            relative_path,
            label,
            allow_uncommitted=allow_uncommitted,
        ),
        label=label,
    )


def _validate_manifest(manifest: dict[str, Any]) -> dict[tuple[str, str], dict]:
    packets = manifest.get("packets")
    if not isinstance(packets, list):
        raise HtError("role-packet manifest packets must be an array (B2 §5)")
    observed: list[tuple[str, str]] = []
    rows: dict[tuple[str, str], dict] = {}
    files: set[str] = set()
    for packet in packets:
        if not isinstance(packet, dict):
            raise HtError("role-packet manifest row must be an object (B2 §5)")
        version = packet.get("version")
        expected = {"role", "version", "file", "sha256", "bytes"}
        if version == "v1":
            expected |= {"provenance", "blocks"}
        row = _exact_keys(packet, expected, "role-packet manifest row")
        key = (row["role"], row["version"])
        if key in rows or row["file"] in files:
            raise HtError("role-packet manifest repeats a packet identity (B2 §5)")
        if not isinstance(row["file"], str) or Path(row["file"]).name != row["file"]:
            raise HtError("role-packet manifest file is not one basename (B2 §5)")
        if version == "v1":
            if not isinstance(row["provenance"], list) or not all(
                isinstance(value, str) and value for value in row["provenance"]
            ):
                raise HtError("role-packet manifest provenance is malformed (B2 §5)")
            if not isinstance(row["blocks"], list):
                raise HtError("role-packet manifest blocks must be an array (B2 §5)")
            for block in row["blocks"]:
                if not isinstance(block, dict):
                    raise HtError("role-packet manifest block must be an object (B2 §5)")
                allowed = {"name", "version"} | ({"variant"} if "variant" in block else set())
                _exact_keys(block, allowed, "role-packet manifest block")
        observed.append(key)
        rows[key] = row
        files.add(row["file"])
    if tuple(observed) != MANIFEST_PACKET_ORDER:
        raise HtError("role-packet manifest rows are not exact append order (B2 §5)")
    return rows


def _bound_repository_file(
    reader: _RepositoryRead,
    binding: dict[str, Any],
    label: str,
    *,
    allow_uncommitted: bool,
) -> tuple[Path, bytes]:
    ref = binding.get("ref")
    prefix = "repository-file#"
    if not isinstance(ref, str) or not ref.startswith(prefix):
        raise HtError(f"{label} has an invalid repository-file ref (B2 §5)")
    relative = ref.removeprefix(prefix)
    try:
        normalize_repository_relpath(relative)
    except Exception as exc:
        raise HtError(f"{label} has a non-normalized repository-file ref (B2 §5)") from exc
    if not relative.startswith("system/roles/"):
        raise HtError(f"{label} is outside system/roles (B2 §5)")
    data = reader.stable_file_bytes(
        relative,
        label,
        allow_uncommitted=allow_uncommitted,
    )
    if hashlib.sha256(data).hexdigest() != binding.get("sha256"):
        raise HtError(f"{label} bytes drifted (B2 §5)")
    return reader.root / relative, data


def load_runtime_profiles(
    repository_root: Path,
    *,
    _allow_uncommitted: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Load the exact six-profile registry and rebind packet/procedure bytes."""

    with _RepositoryRead(
        repository_root,
        require_git=not _allow_uncommitted,
    ) as reader:
        return _load_runtime_profiles(
            reader,
            _allow_uncommitted=_allow_uncommitted,
        )


def _load_runtime_profiles(
    reader: _RepositoryRead,
    *,
    _allow_uncommitted: bool,
) -> tuple[dict[str, Any], ...]:

    value = _exact_keys(
        _strict_file(
            reader,
            "system/roles/RUNTIME-PROFILES.json",
            "B2 runtime profiles",
            allow_uncommitted=_allow_uncommitted,
        ),
        {"schema_version", "profiles"},
        "B2 runtime profiles",
    )
    if value["schema_version"] != "hypothesis-tree-role-profile-registry/1.0.0":
        raise HtError("B2 runtime profile registry has a foreign schema version (B2 §5)")
    profiles = value["profiles"]
    if not isinstance(profiles, list) or [item.get("profile_id") if isinstance(item, dict) else None for item in profiles] != list(PROFILE_ORDER):
        raise HtError("B2 runtime profiles are not the exact ordered six-profile set (B2 §5)")

    manifest = _exact_keys(
        _strict_file(
            reader,
            "system/roles/MANIFEST.json",
            "role-packet manifest",
            allow_uncommitted=_allow_uncommitted,
        ),
        {
            "$comment", "provenance", "v1_provenance", "v1_ratification",
            "mechanical_scope", "hash_algorithm", "hash_basis",
            "block_registry", "packets",
        },
        "role-packet manifest",
    )
    manifest_rows = _validate_manifest(manifest)
    result: list[dict[str, Any]] = []
    exact_profile_fields = {
        "profile_id", "authority_role", "level", "role_packet", "procedure",
        "package_kinds", "action_kinds", "runner_token",
        "producer_adapter_token", "transcript_format",
    }
    exact_binding_fields = {"ref", "version", "sha256"}

    for profile in profiles:
        row = _exact_keys(profile, exact_profile_fields, "runtime profile")
        authority, level, packages, actions = PROFILE_CONTRACT[row["profile_id"]]
        if (row["authority_role"], row["level"]) != (authority, level):
            raise HtError(f"profile {row['profile_id']} authority/level drift (B2 §5)")
        if tuple(row["package_kinds"]) != packages or tuple(row["action_kinds"]) != actions:
            raise HtError(f"profile {row['profile_id']} package/action drift (B2 §5)")
        if (
            row["runner_token"] != "ht-runtime-profile-runner/1.0.0"
            or row["producer_adapter_token"] != "codex-exec-fixed/1.0.0"
            or row["transcript_format"] != "codex-exec-jsonl/0.144.1"
        ):
            raise HtError(f"profile {row['profile_id']} runner/producer drift (B2 §5)")

        packet = _exact_keys(row["role_packet"], exact_binding_fields, "role-packet binding")
        manifest_role, packet_ref, packet_version, packet_sha256 = (
            PROFILE_PACKET_CONTRACT[row["profile_id"]]
        )
        if packet != {
            "ref": packet_ref,
            "version": packet_version,
            "sha256": packet_sha256,
        }:
            raise HtError(
                f"profile {row['profile_id']} role-packet binding drift (B2 §5)"
            )
        packet_path, packet_bytes = _bound_repository_file(
            reader,
            packet,
            f"profile {row['profile_id']} role packet",
            allow_uncommitted=_allow_uncommitted,
        )
        manifest_row = manifest_rows.get((manifest_role, packet["version"]))
        if manifest_row is None or manifest_row.get("file") != packet_path.name or manifest_row.get("sha256") != packet["sha256"] or manifest_row.get("bytes") != len(packet_bytes):
            raise HtError(f"profile {row['profile_id']} disagrees with MANIFEST.json (B2 §5)")

        procedure = row["procedure"]
        if row["profile_id"] == "verifier-v1":
            bound = _exact_keys(procedure, exact_binding_fields, "verifier procedure binding")
            if bound != VERIFIER_PROCEDURE_CONTRACT:
                raise HtError("verifier procedure binding drift (B2 §5)")
            _bound_repository_file(
                reader,
                bound,
                "verifier procedure",
                allow_uncommitted=_allow_uncommitted,
            )
        elif procedure is not None:
            raise HtError(f"only verifier-v1 may bind a procedure (B2 §5): {row['profile_id']}")
        result.append(row)
    return tuple(result)


def load_reference_projections(
    repository_root: Path,
    *,
    _allow_uncommitted: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Load the closed semantic projection dispatch registry."""

    with _RepositoryRead(
        repository_root,
        require_git=not _allow_uncommitted,
    ) as reader:
        return _load_reference_projections(
            reader,
            _allow_uncommitted=_allow_uncommitted,
        )


def _load_reference_projections(
    reader: _RepositoryRead,
    *,
    _allow_uncommitted: bool,
) -> tuple[dict[str, Any], ...]:

    value = _exact_keys(
        _strict_file(
            reader,
            "system/roles/REFERENCE-PROJECTIONS.json",
            "B2 reference projections",
            allow_uncommitted=_allow_uncommitted,
        ),
        {"schema_version", "renderer_token", "entries"},
        "B2 reference projections",
    )
    if value["schema_version"] != "hypothesis-tree-reference-projection-registry/1.0.0" or value["renderer_token"] != "ht-reference-projection/1.0.0":
        raise HtError("B2 reference projection registry token drift (B2 §8.1)")
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) != len(PROJECTION_ORDER):
        raise HtError(
            "B2 reference projection entries must be the exact closed row count (B2 §8.1)"
        )
    observed = []
    for index, entry in enumerate(entries):
        row = _exact_keys(
            entry,
            {"source_kind", "projection_schema_version", "field_policy"},
            "reference projection row",
        )
        policy = _exact_keys(row["field_policy"], {"mode", "fields", "nested"}, "reference projection field policy")
        if policy != EXPECTED_PROJECTION_POLICIES[index]:
            raise HtError(
                f"B2 reference projection policy drift at {row['source_kind']} (B2 §8.1)"
            )
        observed.append((row["source_kind"], row["projection_schema_version"]))
    if observed != list(zip(PROJECTION_ORDER, PROJECTION_TOKENS, strict=True)):
        raise HtError("B2 reference projections are not the exact closed dispatch (B2 §8.1)")
    return tuple(entries)
