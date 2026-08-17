"""Fail-closed filesystem primitives for runtime state."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
from dataclasses import dataclass
from uuid import RFC_4122, UUID, uuid4

from ht.errors import HtError


FILE_MODE = 0o600
DIRECTORY_MODE = 0o700


@dataclass(frozen=True)
class OperationInspection:
    """Read-only physical facts for one exact atomic-operation target."""

    path: Path
    operation: str
    final_exists: bool
    final_bytes: bytes | None
    temporary_names: tuple[str, ...]
    linked_temporary_name: str | None


def _fail(message: str) -> HtError:
    return HtError(f"runtime filesystem corruption: {message} (B1 §3/§6)")


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def require_directory(path: Path, *, mode: int = DIRECTORY_MODE) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise _fail(f"missing directory {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _fail(f"{path} is not an exact non-symlink directory")
    if stat.S_IMODE(info.st_mode) != mode:
        raise _fail(f"{path} mode is {stat.S_IMODE(info.st_mode):04o}, expected {mode:04o}")


def make_directory(path: Path) -> None:
    try:
        os.mkdir(path, DIRECTORY_MODE)
    except FileExistsError:
        require_directory(path)
        return
    os.chmod(path, DIRECTORY_MODE, follow_symlinks=False)
    require_directory(path)
    fsync_directory(path.parent)


def _open_nofollow(path: Path, flags: int, mode: int = FILE_MODE) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)


def read_exact_file(
    path: Path,
    *,
    expected_mode: int = FILE_MODE,
    require_single_link: bool = True,
) -> bytes:
    """Stable-read one exact regular file without following a final symlink."""

    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise _fail(f"missing file {path}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _fail(f"{path} is not an exact regular non-symlink file")
    if stat.S_IMODE(before.st_mode) != expected_mode:
        raise _fail(
            f"{path} mode is {stat.S_IMODE(before.st_mode):04o}, "
            f"expected {expected_mode:04o}"
        )
    if require_single_link and before.st_nlink != 1:
        raise _fail(f"{path} has {before.st_nlink} hard links, expected exactly one")
    fd = _open_nofollow(path, os.O_RDONLY)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise _fail(f"{path} changed type during open")
        if stat.S_IMODE(opened.st_mode) != expected_mode:
            raise _fail(f"{path} changed mode during open")
        if require_single_link and opened.st_nlink != 1:
            raise _fail(f"{path} changed link count during open")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        current = path.lstat()
    except FileNotFoundError as exc:
        raise _fail(f"{path} disappeared while being read") from exc
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        raise _fail(f"{path} changed type while being read")
    if stat.S_IMODE(current.st_mode) != expected_mode:
        raise _fail(f"{path} changed mode while being read")
    if require_single_link and current.st_nlink != 1:
        raise _fail(f"{path} changed link count while being read")
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        stat.S_IMODE(value.st_mode),
        value.st_nlink,
    )
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise _fail(f"{path} changed while being read")
    if identity(current) != identity(after):
        raise _fail(f"{path} was replaced while being read")
    return b"".join(chunks)


def _write_fully(fd: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:  # pragma: no cover - OS contract
            raise OSError("short write")
        written += count


def _owned_temporaries(path: Path, operation: str) -> list[Path]:
    return sorted(
        (
            child
            for child in path.parent.iterdir()
            if is_operation_temporary_name(
                child.name,
                operation=operation,
                target_name=path.name,
            )
        ),
        key=lambda child: child.name,
    )


def _is_uuid4(value: str) -> bool:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return (
        str(parsed) == value
        and parsed.variant == RFC_4122
        and parsed.version == 4
    )


def is_operation_temporary_name(
    name: str,
    *,
    operation: str,
    target_name: str,
) -> bool:
    """Match the exact canonical name emitted by one atomic operation."""

    if operation not in {"publish", "replace"}:
        return False
    prefix = f".ht-{operation}-{target_name}-"
    return name.startswith(prefix) and _is_uuid4(name[len(prefix) :])


def inspect_operation_state(
    path: Path,
    *,
    operation: str,
    expected_final: bytes | None = None,
) -> OperationInspection:
    """Inspect one publication/replacement without cleaning or publishing it.

    Exact operation names are not sufficient authority on their own: every
    temporary must also have the physical shape that this operation creates,
    and publication aliases must point only at the named final.
    """

    if operation not in {"publish", "replace"}:
        raise _fail(f"unknown atomic operation {operation!r} for {path}")
    require_directory(path.parent)
    temporaries = _owned_temporaries(path, operation)
    final_exists = path.exists() or path.is_symlink()
    final_bytes: bytes | None = None
    final_info: os.stat_result | None = None
    if final_exists:
        final_bytes = read_exact_file(path, require_single_link=False)
        final_info = path.lstat()
        if expected_final is not None and final_bytes != expected_final:
            raise _fail(f"conflicting atomic final {path}")

    linked: list[str] = []
    names: list[str] = []
    for temporary in temporaries:
        info = temporary.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != FILE_MODE
        ):
            raise _fail(f"atomic temporary {temporary} is not an exact 0600 regular file")
        names.append(temporary.name)
        same_as_final = bool(
            final_info is not None
            and (info.st_dev, info.st_ino) == (final_info.st_dev, final_info.st_ino)
        )
        if operation == "replace":
            if info.st_nlink != 1:
                raise _fail(f"replacement temporary {temporary} has external aliases")
        elif same_as_final:
            if info.st_nlink != 2:
                raise _fail(f"publication commit link {temporary} has unexpected aliases")
            linked.append(temporary.name)
        elif info.st_nlink != 1:
            raise _fail(f"uncommitted publication temporary {temporary} has external aliases")

    if operation == "replace":
        if final_info is not None and final_info.st_nlink != 1:
            raise _fail(f"replacement target {path} has external aliases")
    else:
        if len(linked) > 1:
            raise _fail(f"immutable file {path} has multiple publication commit aliases")
        if final_info is not None and final_info.st_nlink != 1 + len(linked):
            raise _fail(f"immutable file {path} has an unowned hard-link alias")

    return OperationInspection(
        path=path,
        operation=operation,
        final_exists=final_exists,
        final_bytes=final_bytes,
        temporary_names=tuple(names),
        linked_temporary_name=linked[0] if linked else None,
    )


def _unlink_uncommitted(temporary: Path) -> None:
    """Remove one exact unlinked operation temp; its bytes never committed."""

    try:
        info = temporary.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _fail(f"interrupted temporary {temporary} is not an exact regular file")
    if info.st_nlink != 1:
        raise _fail(f"uncommitted temporary {temporary} has external hard links")
    temporary.unlink()


def _validate_published_final(path: Path, data: bytes, *, clean_commit_link: bool) -> None:
    """Validate a final, allowing only its own crash-left hard-link temp."""

    # Another exact publisher may be between link(final) and unlink(temp).
    # Retrying handles its unlink racing the stable metadata checks below.
    for _attempt in range(32):
        try:
            actual = read_exact_file(path, require_single_link=False)
            final = path.lstat()
        except HtError as exc:
            if "changed" in exc.message or "replaced" in exc.message:
                continue
            raise
        if actual != data:
            raise _fail(f"conflicting immutable file {path}")
        linked: list[Path] = []
        for temporary in _owned_temporaries(path, "publish"):
            try:
                info = temporary.lstat()
            except FileNotFoundError:
                continue
            if (info.st_dev, info.st_ino) == (final.st_dev, final.st_ino):
                linked.append(temporary)
        if len(linked) > 1:
            raise _fail(f"immutable file {path} has multiple publication aliases")
        # The publisher that created ``final`` may unlink its commit temp after
        # our lstat and before our directory scan.  Re-read the final inode so
        # a stale st_nlink value cannot turn an exact concurrent publisher into
        # a false unowned-alias report.  A stable unexplained link still fails.
        try:
            refreshed = path.lstat()
        except FileNotFoundError:
            continue
        if (refreshed.st_dev, refreshed.st_ino) != (final.st_dev, final.st_ino):
            continue
        if refreshed.st_nlink != final.st_nlink:
            continue
        final = refreshed
        if final.st_nlink != 1 + len(linked):
            raise _fail(f"immutable file {path} has an unowned hard-link alias")
        if linked and clean_commit_link:
            for temporary in linked:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            fsync_directory(path.parent)
            continue
        if final.st_nlink == 1:
            if read_exact_file(path) != data:
                raise _fail(f"published bytes differ at {path}")
            return
    raise _fail(f"immutable file {path} did not reach stable publication metadata")


def recover_immutable_publication(path: Path, data: bytes) -> None:
    """Clean uncommitted temps or a crash-left final/temp commit link."""

    require_directory(path.parent)
    temporaries = _owned_temporaries(path, "publish")
    final_exists = path.exists() or path.is_symlink()
    if final_exists:
        _validate_published_final(path, data, clean_commit_link=True)
    for temporary in temporaries:
        # A linked commit temp was removed above. Any survivor has never been
        # linked to the final and is safe to discard regardless of partial bytes.
        _unlink_uncommitted(temporary)
    if temporaries:
        fsync_directory(path.parent)
    if final_exists:
        _validate_published_final(path, data, clean_commit_link=False)


def publish_immutable(path: Path, data: bytes) -> None:
    """Publish canonical bytes by hidden-temp plus hard-link, never overwrite."""

    require_directory(path.parent)
    if path.exists() or path.is_symlink():
        _validate_published_final(path, data, clean_commit_link=True)
        # The exact final may be the commit point left by a process that
        # crashed before fsyncing the directory.  An idempotent restart must
        # finish that durability step before accepting the publication.
        fsync_directory(path.parent)
        _validate_published_final(path, data, clean_commit_link=False)
        return
    temporary = path.parent / f".ht-publish-{path.name}-{uuid4()}"
    fd = _open_nofollow(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        os.fchmod(fd, FILE_MODE)
        _write_fully(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            _validate_published_final(path, data, clean_commit_link=True)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK, errno.EXDEV}:
                raise _fail(f"cannot safely publish {path}: {exc}") from exc
            raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        fsync_directory(path.parent)
    _validate_published_final(path, data, clean_commit_link=True)


def replace_file(
    path: Path,
    data: bytes,
    *,
    expected_old: bytes | tuple[bytes, ...],
) -> None:
    """Crash-safe replacement for projections (never for immutable truth)."""

    require_directory(path.parent)
    allowed = (expected_old,) if isinstance(expected_old, bytes) else expected_old
    current = read_exact_file(path)
    if current != data and current not in allowed:
        raise _fail(f"existing replacement target {path} is not an authorized old state")
    interrupted = _owned_temporaries(path, "replace")
    for temporary in interrupted:
        # os.replace is the commit point. A surviving temp is necessarily an
        # uncommitted attempt, including a partial write or older timestamp.
        _unlink_uncommitted(temporary)
    if interrupted:
        fsync_directory(path.parent)
    if current == data:
        # ``current`` may be an already-replaced target whose publisher
        # crashed before its directory fsync.  Complete the replacement and
        # then prove the exact final again before reporting success.
        fsync_directory(path.parent)
        if read_exact_file(path) != data:
            raise _fail(f"replacement bytes differ at {path}")
        return
    temporary = path.parent / f".ht-replace-{path.name}-{uuid4()}"
    fd = _open_nofollow(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        os.fchmod(fd, FILE_MODE)
        _write_fully(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if read_exact_file(path) != data:
        raise _fail(f"replacement bytes differ at {path}")
