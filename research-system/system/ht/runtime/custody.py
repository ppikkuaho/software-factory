"""Exact local flock custody primitives for the B1 runtime."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator

from ht.errors import HtError

from .atomic import FILE_MODE, fsync_directory, read_exact_file, require_directory


def _open_lock(path: Path) -> int:
    read_exact_file(path)
    fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != FILE_MODE
            or opened.st_nlink != 1
            or opened.st_size != 0
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise HtError("runtime lock is not the exact canonical empty 0600 file (B1 §13)")
        return fd
    except Exception:
        os.close(fd)
        raise


def create_custody(path: Path) -> int:
    """Create the custody artifact and return its open description LOCK_SH-held."""

    ensure_custody_file(path)
    fd = _open_lock(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        return fd
    except Exception:
        os.close(fd)
        raise


def ensure_custody_file(path: Path) -> None:
    """Create or validate one unlocked, empty, durable 0600 custody file."""

    require_directory(path.parent)
    try:
        fd = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            FILE_MODE,
        )
    except FileExistsError:
        fd = _open_lock(path)
    else:
        os.fchmod(fd, FILE_MODE)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != FILE_MODE
            or opened.st_nlink != 1
            or opened.st_size != 0
        ):
            raise HtError("runtime custody lock is not empty (B1 §13)")
        os.fsync(fd)
    except Exception:
        os.close(fd)
        raise
    os.close(fd)
    fsync_directory(path.parent)
    validation_fd = _open_lock(path)
    os.close(validation_fd)


def validate_inherited(fd: int, path: Path) -> None:
    """Prove an inherited descriptor is the canonical custody inode."""

    inherited = os.fstat(fd)
    current = path.lstat()
    if (
        not stat.S_ISREG(inherited.st_mode)
        or stat.S_IMODE(inherited.st_mode) != FILE_MODE
        or inherited.st_nlink != 1
        or inherited.st_size != 0
        or stat.S_ISLNK(current.st_mode)
        or (inherited.st_dev, inherited.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise HtError("inherited custody descriptor differs from canonical lock (B1 §13)")


def custody_is_free(path: Path) -> bool:
    """Return true only when the exact lock can be taken exclusively now."""

    fd = _open_lock(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def try_instance_lock(path: Path) -> int | None:
    fd = _open_lock(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


@contextmanager
def audit_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    fd = _open_lock(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
