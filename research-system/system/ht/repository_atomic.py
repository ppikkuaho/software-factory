"""Descriptor-rooted, failure-atomic repository publication.

The Git commit is the logical publication point.  Candidate worktree bytes are
needed for the existing staged-content hook, so this transaction freezes every
input/output preimage, performs all filesystem operations relative to one held
root descriptor, and restores owned candidates on any pre-publication failure.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Mapping
from uuid import uuid4

from . import gitutil
from .errors import HtError


class _CaptureCurrent:
    pass


CAPTURE_CURRENT: Final = _CaptureCurrent()
ExpectedPreimage = bytes | None | _CaptureCurrent


@dataclass(frozen=True)
class _FileSnapshot:
    data: bytes | None
    mode: int | None
    device: int | None
    inode: int | None
    uid: int | None
    gid: int | None
    size: int | None
    mtime_ns: int | None
    ctime_ns: int | None


@dataclass(frozen=True)
class _CreatedDirectory:
    parts: tuple[str, ...]
    device: int
    inode: int


class _MissingAncestor(Exception):
    pass


def _rename_noreplace(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
) -> None:
    """Atomically rename without ever replacing a destination entry."""

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_fd,
            source_bytes,
            destination_fd,
            destination_bytes,
            0x00000004,  # RENAME_EXCL
        )
    elif sys.platform.startswith("linux"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_fd,
            source_bytes,
            destination_fd,
            destination_bytes,
            1,  # RENAME_NOREPLACE
        )
    else:  # pragma: no cover - fail closed on unsupported kernels
        raise HtError("atomic no-replace directory rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _identity(value: os.stat_result) -> tuple[int, ...]:
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


class RepositoryTransaction:
    """One exact-input, exact-output repository transaction.

    ``expected`` declares every source and output path.  ``read_only`` selects
    declarations used only to build deterministic projections; every remaining
    declaration must be written exactly once and classified as tracked or
    ignored before commit.
    """

    def __init__(
        self,
        root: Path,
        expected: Mapping[Path, ExpectedPreimage],
        *,
        read_only: Iterable[Path] = (),
        base_head: str,
    ) -> None:
        self.root = root.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        self._root_fd = os.open(self.root, flags)
        root_info = os.fstat(self._root_fd)
        if not stat.S_ISDIR(root_info.st_mode):
            os.close(self._root_fd)
            raise HtError(f"repository transaction root is not a directory: {self.root}")
        self.base_head = base_head
        self._root_identity = (root_info.st_dev, root_info.st_ino)
        try:
            self._git_binding = gitutil.capture_repository_binding(
                self.root,
                self._root_fd,
                self._root_identity,
            )
        except Exception:
            os.close(self._root_fd)
            raise
        self._snapshots: dict[str, _FileSnapshot] = {}
        self._absolute: dict[str, Path] = {}
        self._candidate_bytes: dict[str, bytes] = {}
        self._candidate_snapshots: dict[str, _FileSnapshot] = {}
        self._written: list[str] = []
        self._tracked: set[str] = set()
        self._ignored: set[str] = set()
        self._created_dirs: list[_CreatedDirectory] = []
        self._committed = False
        self._rolled_back = False
        self._closed = False

        try:
            normalized: list[tuple[str, Path, ExpectedPreimage]] = []
            for path, preimage in expected.items():
                rel, absolute = self._normalize(path)
                if rel in self._snapshots or any(row[0] == rel for row in normalized):
                    raise HtError(f"repository transaction names path twice: {rel}")
                normalized.append((rel, absolute, preimage))

            read_only_rels = {self._normalize(path)[0] for path in read_only}
            declared_rels = {row[0] for row in normalized}
            unknown_read_only = read_only_rels - declared_rels
            if unknown_read_only:
                raise HtError(
                    "repository read-only declarations are not in the preimage set: "
                    + ", ".join(sorted(unknown_read_only))
                )
            self._read_only = read_only_rels
            self._outputs = declared_rels - read_only_rels

            captured = {rel: self._read_snapshot(rel) for rel, _path, _pre in normalized}
            for rel, absolute, preimage in normalized:
                snapshot = captured[rel]
                if preimage is not CAPTURE_CURRENT and snapshot.data != preimage:
                    expected_label = "absent" if preimage is None else "exact bytes"
                    actual_label = "absent" if snapshot.data is None else "different bytes"
                    raise HtError(
                        f"exact repository preimage changed for {rel} "
                        f"(expected {expected_label}; found {actual_label})"
                    )
                self._snapshots[rel] = snapshot
                self._absolute[rel] = absolute
        except Exception:
            self._close()
            raise

    def __enter__(self) -> RepositoryTransaction:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, traceback
        if not self._committed and not self._rolled_back and (
            self._written or self._created_dirs
        ):
            try:
                self.rollback()
            except HtError as rollback_error:
                if exc is None:
                    raise
                raise HtError(
                    f"repository transaction failed ({exc}); {rollback_error.message}"
                ) from exc
        self._close()
        return False

    def write_bytes(self, path: Path, content: bytes, *, gitignored: bool = False) -> None:
        if self._committed or self._rolled_back:
            raise HtError("repository transaction is already closed")
        if not isinstance(content, bytes):
            raise TypeError("repository transaction content must be bytes")
        rel, _absolute = self._normalize(path)
        if rel not in self._snapshots:
            raise HtError(f"repository transaction path was not predeclared: {rel}")
        if rel in self._read_only:
            raise HtError(f"repository read-only path cannot be written: {rel}")
        if rel in self._candidate_bytes:
            raise HtError(f"repository transaction path written twice: {rel}")
        if self._read_snapshot(rel) != self._snapshots[rel]:
            raise HtError(
                f"exact repository preimage changed before candidate write for {rel}"
            )

        mode = self._snapshots[rel].mode or 0o644
        if mode != 0o644:
            raise HtError(
                f"repository candidate mode must be 0644 before Git staging: {rel}"
            )
        candidate = self._atomic_replace(rel, content, mode)
        if candidate.data != content or candidate.mode != 0o644:
            raise HtError(f"repository candidate install is not exact: {rel}")
        self._candidate_bytes[rel] = content
        self._candidate_snapshots[rel] = candidate
        self._written.append(rel)
        (self._ignored if gitignored else self._tracked).add(rel)

    def read_bytes(self, path: Path) -> bytes:
        """Read one frozen HEAD+candidate source without consulting live paths."""
        rel, _absolute = self._normalize(path)
        if rel not in self._snapshots:
            raise HtError(f"repository source path was not declared: {rel}")
        if rel in self._candidate_bytes:
            return self._candidate_bytes[rel]
        data = self._snapshots[rel].data
        if data is None:
            raise HtError(f"repository source path is absent: {rel}")
        return data

    def commit(self, rel_paths: list[str], role: str, message: str) -> None:
        if self._committed or self._rolled_back:
            raise HtError("repository transaction is already closed")
        written = set(self._written)
        if written != self._outputs:
            missing = sorted(self._outputs - written)
            extra = sorted(written - self._outputs)
            raise HtError(
                "repository transaction output declarations are incomplete "
                f"(missing={missing}, extra={extra})"
            )
        if self._tracked & self._ignored or self._tracked | self._ignored != written:
            raise HtError("repository tracked/ignored output classification is incoherent")
        if not self._tracked:
            raise HtError("repository transaction has no tracked publication path")
        if len(rel_paths) != len(set(rel_paths)) or set(rel_paths) != self._tracked:
            raise HtError(
                "repository commit set does not exactly equal tracked transaction outputs"
            )
        self._verify_candidates()
        try:
            gitutil.commit(
                self.root,
                rel_paths,
                role,
                message,
                expected_parent=self.base_head,
                verify_worktree=self._verify_candidates,
                repository_fd=self._root_fd,
                repository_identity=self._root_identity,
                binding=self._require_git_binding(),
                candidate_bytes={
                    relative: self._candidate_bytes[relative]
                    for relative in self._tracked
                },
            )
        except gitutil.GitStateChanged:
            # Ownership is unknowable after an external Git-state change.
            # Preserve all evidence instead of manufacturing a rollback state.
            self._committed = True
            raise
        except Exception:
            self.rollback()
            raise
        self._committed = True
        self._close()

    def rollback(self) -> None:
        if self._committed:
            raise HtError("cannot roll back a committed repository transaction")
        if self._rolled_back:
            return
        conflicts: list[str] = []
        for rel in reversed(self._written):
            try:
                current = self._read_snapshot(rel)
            except HtError as exc:
                conflicts.append(f"{rel} ({exc.message})")
                continue
            if current != self._candidate_snapshots[rel]:
                conflicts.append(f"{rel} (candidate identity changed)")
                continue
            original = self._snapshots[rel]
            if original.data is None:
                self._unlink(rel)
            else:
                assert original.mode is not None
                self._atomic_replace(rel, original.data, original.mode)

        conflicts.extend(self._retire_created_dirs())
        self._rolled_back = True
        self._close()
        if conflicts:
            raise HtError(
                "repository rollback preserved externally changed paths: "
                + ", ".join(conflicts)
            )

    def _normalize(self, path: Path) -> tuple[str, Path]:
        lexical = path if path.is_absolute() else self.root / path
        absolute = Path(os.path.abspath(lexical))
        if not absolute.is_relative_to(self.root) or absolute == self.root:
            raise HtError(f"repository transaction path escapes root: {path}")
        relative = absolute.relative_to(self.root)
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise HtError(f"repository transaction path is not normalized: {path}")
        return relative.as_posix(), absolute

    def _open_parent(self, rel: str, *, create: bool) -> tuple[int, str]:
        parts = tuple(Path(rel).parts)
        fd = os.dup(self._root_fd)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for index, part in enumerate(parts[:-1]):
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise _MissingAncestor
                    child = self._create_directory(fd, part, rel)
                    created_info = os.fstat(child)
                    self._created_dirs.append(
                        _CreatedDirectory(
                            parts[: index + 1],
                            created_info.st_dev,
                            created_info.st_ino,
                        )
                    )
                except OSError as exc:
                    raise HtError(
                        f"repository path component is not a real directory: {rel}"
                    ) from exc
                os.close(fd)
                fd = child
            return fd, parts[-1]
        except Exception:
            os.close(fd)
            raise

    def _create_directory(self, parent_fd: int, name: str, rel: str) -> int:
        """Install one owned directory with an atomic no-replace rename."""

        temporary = f".ht-repository-dir-{uuid4()}"
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        child: int | None = None
        installed = False
        try:
            os.mkdir(temporary, 0o755, dir_fd=parent_fd)
            child = os.open(temporary, flags, dir_fd=parent_fd)
            created = os.fstat(child)
            if not stat.S_ISDIR(created.st_mode):
                raise HtError(f"repository-created path is not a directory: {rel}")
            _rename_noreplace(parent_fd, temporary, parent_fd, name)
            installed = True
            os.fsync(parent_fd)
            return child
        except Exception:
            if child is not None:
                os.close(child)
            if not installed:
                try:
                    os.rmdir(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            raise

    def _read_snapshot(self, rel: str) -> _FileSnapshot:
        try:
            parent_fd, name = self._open_parent(rel, create=False)
        except _MissingAncestor:
            return _FileSnapshot(None, None, None, None, None, None, None, None, None)
        try:
            return self._read_name_snapshot(parent_fd, name, rel)
        finally:
            os.close(parent_fd)

    def _read_name_snapshot(
        self, parent_fd: int, name: str, rel: str
    ) -> _FileSnapshot:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _FileSnapshot(None, None, None, None, None, None, None, None, None)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise HtError(
                f"repository path is not a single-link regular file: {rel}"
            )
        fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(fd)
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(fd)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(before) != _identity(opened)
            or _identity(opened) != _identity(after)
        ):
            raise HtError(f"repository path changed during stable read: {rel}")
        return _FileSnapshot(
            b"".join(chunks),
            stat.S_IMODE(opened.st_mode),
            opened.st_dev,
            opened.st_ino,
            opened.st_uid,
            opened.st_gid,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )

    def _atomic_replace(self, rel: str, content: bytes, mode: int) -> _FileSnapshot:
        parent_fd, name = self._open_parent(rel, create=True)
        temp = f".ht-repository-txn-{name}-{uuid4()}"
        fd: int | None = None
        try:
            fd = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode,
                dir_fd=parent_fd,
            )
            os.fchmod(fd, mode)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise HtError(f"repository publication temp is not single-link: {rel}")
            view = memoryview(content)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise HtError(f"short repository publication write: {rel}")
                view = view[count:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            candidate = self._read_name_snapshot(parent_fd, name, rel)
            if candidate.data is None:
                raise HtError(f"repository publication disappeared after install: {rel}")
            return candidate
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temp, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def _unlink(self, rel: str) -> None:
        parent_fd, name = self._open_parent(rel, create=False)
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise HtError(f"repository rollback target is not single-link: {rel}")
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def _retire_created_dirs(self) -> list[str]:
        """Retire owned directories without ever deleting a pathname race.

        POSIX has no descriptor-relative rmdir-by-handle.  We therefore move a
        pathname atomically to a transaction-unique root name, inspect the moved
        inode, and restore any unowned replacement.  Exact owned directories are
        retained as gitignored recovery evidence instead of risking deletion of
        a replacement inserted after an identity check.
        """

        conflicts: list[str] = []
        for created in reversed(self._created_dirs):
            parts = created.parts
            parent_rel = Path(*parts[:-1]).as_posix() if parts[:-1] else None
            quarantine = f".ht-repository-retired-{uuid4()}"
            try:
                if parent_rel is None:
                    parent_fd = os.dup(self._root_fd)
                else:
                    parent_fd, _unused = self._open_parent(
                        f"{parent_rel}/.sentinel", create=False
                    )
                try:
                    _rename_noreplace(
                        parent_fd,
                        parts[-1],
                        self._root_fd,
                        quarantine,
                    )
                    moved = os.stat(
                        quarantine,
                        dir_fd=self._root_fd,
                        follow_symlinks=False,
                    )
                    moved_fd: int | None = None
                    moved_entries: list[str] | None = None
                    if stat.S_ISDIR(moved.st_mode):
                        moved_fd = os.open(
                            quarantine,
                            os.O_RDONLY
                            | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=self._root_fd,
                        )
                        try:
                            moved_entries = os.listdir(moved_fd)
                        finally:
                            os.close(moved_fd)
                    if (
                        stat.S_ISDIR(moved.st_mode)
                        and (moved.st_dev, moved.st_ino)
                        == (created.device, created.inode)
                        and moved_entries == []
                    ):
                        os.fsync(self._root_fd)
                        continue
                    try:
                        _rename_noreplace(
                            self._root_fd,
                            quarantine,
                            parent_fd,
                            parts[-1],
                        )
                    except OSError:
                        conflicts.append(
                            f"{'/'.join(parts)} "
                            f"(replacement quarantined as {quarantine})"
                        )
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except (FileNotFoundError, _MissingAncestor):
                continue
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                    conflicts.append(f"{'/'.join(parts)} ({exc})")
        return conflicts

    def _verify_candidates(self) -> None:
        for rel in self._written:
            current = self._read_snapshot(rel)
            if current != self._candidate_snapshots[rel] or current.mode != 0o644:
                raise HtError(
                    f"repository candidate identity changed before commit: {rel}"
                )

    def _close(self) -> None:
        if not self._closed:
            if self._git_binding is not None:
                self._git_binding.close()
            os.close(self._root_fd)
            self._closed = True

    def _require_git_binding(self) -> gitutil.RepositoryBinding:
        if self._git_binding is None:
            raise HtError("repository transaction root is not a Git repository")
        return self._git_binding
