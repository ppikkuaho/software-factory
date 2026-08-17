"""Typed work resolution plus current worktree/index/HEAD identity proof."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any

from ht.errors import HtError
from ht.paths import Root
from ht.references import canonical_json_sha256, resolve_ref

from .schema import strict_loads


SUPPORTED_WORK_TYPES = frozenset({"issue", "node", "dispatch"})


@dataclass(frozen=True)
class WorkSnapshot:
    type: str
    canonical_ref: str
    repository_relpath: str
    canonical_object_sha256: str
    raw_file_sha256: str
    head_blob_oid: str
    submission_repository_commit: str
    git_object_format: str

    def as_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "canonical_ref": self.canonical_ref,
            "repository_relpath": self.repository_relpath,
            "canonical_object_sha256": self.canonical_object_sha256,
            "raw_file_sha256": self.raw_file_sha256,
            "head_blob_oid": self.head_blob_oid,
            "submission_repository_commit": self.submission_repository_commit,
            "git_object_format": self.git_object_format,
        }


def _git(root: Path, args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise HtError(f"runtime Git proof failed: {detail or 'git exited nonzero'} (B1 §7)")
    return result.stdout


def _stable_repository_bytes(path: Path) -> bytes:
    def require_exact_regular(info: os.stat_result, when: str) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise HtError(
                f"runtime work object {path} changed type {when} (B1 §7)"
            )
        if info.st_mode & 0o111:
            raise HtError(
                f"runtime work object {path} became executable {when} (B1 §7)"
            )

    def identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
        return (
            info.st_dev,
            info.st_ino,
            stat.S_IFMT(info.st_mode),
            stat.S_IMODE(info.st_mode),
            info.st_size,
            info.st_mtime_ns,
            info.st_nlink,
        )

    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise HtError(f"runtime work object {path} is not a regular non-symlink (B1 §7)")
    if before.st_mode & 0o111:
        raise HtError(f"runtime work object {path} is executable in the worktree (B1 §7)")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        require_exact_regular(opened, "during open")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        require_exact_regular(after, "during read")
    finally:
        os.close(fd)
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise HtError(f"runtime work object {path} changed during proof (B1 §7)")
    try:
        current = path.lstat()
    except FileNotFoundError as exc:
        raise HtError(f"runtime work object {path} disappeared during proof (B1 §7)") from exc
    if stat.S_ISLNK(current.st_mode):
        raise HtError(f"runtime work object {path} became a symlink during proof (B1 §7)")
    require_exact_regular(current, "at current pathname")
    if identity(current) != identity(after):
        raise HtError(f"runtime work object {path} was replaced during proof (B1 §7)")
    return b"".join(chunks)


def _single_record(output: bytes, label: str) -> bytes:
    records = [record for record in output.split(b"\0") if record]
    if len(records) != 1:
        raise HtError(f"runtime work path has {len(records)} {label} entries, expected one (B1 §7)")
    return records[0]


def _head_entry(root: Path, relpath: str) -> str:
    record = _single_record(
        _git(root, ["ls-tree", "-z", "HEAD", "--", relpath]),
        "HEAD",
    )
    try:
        metadata, actual_path = record.split(b"\t", 1)
        mode, object_type, oid = metadata.decode("ascii").split(" ")
        decoded_path = actual_path.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HtError(f"malformed HEAD entry for runtime work path (B1 §7)") from exc
    if decoded_path != relpath or mode != "100644" or object_type != "blob":
        raise HtError(
            f"runtime work HEAD entry must be exact 100644 blob for {relpath} (B1 §7)"
        )
    return oid


def _index_entry(root: Path, relpath: str) -> str:
    record = _single_record(
        _git(root, ["ls-files", "--stage", "-z", "--", relpath]),
        "index",
    )
    try:
        metadata, actual_path = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split(" ")
        decoded_path = actual_path.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HtError(f"malformed index entry for runtime work path (B1 §7)") from exc
    if decoded_path != relpath or mode != "100644" or stage != "0":
        raise HtError(
            f"runtime work index entry must be exactly one stage-0 100644 entry for {relpath} (B1 §7)"
        )
    return oid


def snapshot_work(root: Root, work_ref: str) -> WorkSnapshot:
    """Resolve and freeze one canonical typed object and its Git triplet."""

    resolved = resolve_ref(root, work_ref, expected=SUPPORTED_WORK_TYPES)
    if resolved.path is None:
        raise HtError(f"runtime work ref {resolved.canonical} has no canonical file (B1 §7)")
    raw = _stable_repository_bytes(resolved.path)
    document = strict_loads(raw, label=f"work object {resolved.canonical}")
    if not isinstance(document, dict) or document != resolved.document:
        raise HtError(f"runtime work object changed during typed resolution (B1 §7)")
    relpath = resolved.path.absolute().relative_to(root.path.resolve()).as_posix()

    object_format = _git(root.path, ["rev-parse", "--show-object-format"]).decode("ascii").strip()
    if object_format not in {"sha1", "sha256"}:
        raise HtError(f"unsupported Git object format {object_format!r} (B1 §7)")
    head_oid = _head_entry(root.path, relpath)
    index_oid = _index_entry(root.path, relpath)
    if index_oid != head_oid:
        raise HtError(f"runtime work index differs from current HEAD for {relpath} (B1 §7)")
    working_oid = _git(root.path, ["hash-object", "--no-filters", "--stdin"], input_bytes=raw).decode("ascii").strip()
    expected_length = 40 if object_format == "sha1" else 64
    if re.fullmatch(rf"[0-9a-f]{{{expected_length}}}", head_oid) is None:
        raise HtError(f"runtime work HEAD has invalid {object_format} object id (B1 §7)")
    if working_oid != head_oid:
        raise HtError(f"runtime worktree bytes differ from index/HEAD for {relpath} (B1 §7)")

    canonical_hash = canonical_json_sha256(document)
    raw_hash = hashlib.sha256(raw).hexdigest()
    commit = _git(root.path, ["rev-parse", "HEAD"]).decode("ascii").strip()

    # The semantic recheck closes the resolve/read/Git race window.
    rechecked = resolve_ref(root, resolved.canonical, expected={resolved.kind})
    if (
        rechecked.path != resolved.path
        or rechecked.canonical != resolved.canonical
        or rechecked.document != document
    ):
        raise HtError(f"runtime typed work identity changed during proof (B1 §7)")
    second_raw = _stable_repository_bytes(rechecked.path)
    if second_raw != raw:
        raise HtError(f"runtime work bytes changed during proof (B1 §7)")
    if _head_entry(root.path, relpath) != head_oid or _index_entry(root.path, relpath) != head_oid:
        raise HtError(f"runtime work Git triplet changed during proof (B1 §7)")

    return WorkSnapshot(
        type=resolved.kind,
        canonical_ref=resolved.canonical,
        repository_relpath=relpath,
        canonical_object_sha256=canonical_hash,
        raw_file_sha256=raw_hash,
        head_blob_oid=head_oid,
        submission_repository_commit=commit,
        git_object_format=object_format,
    )


def revalidate_work(root: Root, expected: WorkSnapshot) -> WorkSnapshot:
    current = snapshot_work(root, expected.canonical_ref)
    comparable = (
        "type",
        "canonical_ref",
        "repository_relpath",
        "canonical_object_sha256",
        "raw_file_sha256",
        "head_blob_oid",
        "git_object_format",
    )
    if any(getattr(current, name) != getattr(expected, name) for name in comparable):
        raise HtError(f"runtime work identity drifted before claim (B1 §8)")
    return current
