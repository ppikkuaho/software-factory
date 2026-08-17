"""Git plumbing: role-stamped commits and staged-content access.

Every mutation ends with a commit of exactly the touched state files, carrying an
`HT-Role: <role>` trailer and made with HT_COMMIT=1 in the environment so the
pre-commit backstop admits it (out-of-band writes lack HT_COMMIT). The hook reads
staged content via `git show :path` and prior content via `git show HEAD:path`.
"""

from __future__ import annotations

import os
import ctypes
import io
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .errors import HtError


class GitStateChanged(HtError):
    """Git publication state changed outside the transaction; preserve evidence."""


_CONTROLLED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
_GIT_BINARY = shutil.which("git", path=_CONTROLLED_PATH)

# A single logical HT read can cross controlled Git plumbing and older call
# sites that start Git directly.  Optional index refreshes from either surface
# would make a read mutate repository state.  This process-wide policy disables
# only Git's optional lock-taking/cache refreshes; commands that explicitly
# update the index, objects, or refs still take their required locks.
os.environ["GIT_OPTIONAL_LOCKS"] = "0"

PRECOMMIT_HOOK = """\
#!/bin/sh
# hypothesis-tree pre-commit backstop (A2 §4.2). Interpreter-independent shim.
if ! command -v git >/dev/null 2>&1; then
    echo "REJECTED (pre-commit): cannot inspect staged protected state (git unavailable)" >&2
    exit 1
fi
git diff --cached --quiet --no-ext-diff -- trees ledger readout tier1
protected_status=$?
case "$protected_status" in
    0) exit 0 ;;
    1) ;;
    *) echo "REJECTED (pre-commit): cannot inspect staged protected state" >&2; exit 1 ;;
esac
if [ -z "${HT_PYTHON:-}" ]; then
    echo "REJECTED (pre-commit): out-of-band commit — use ht (HT_PYTHON is required)" >&2
    exit 1
fi
case "$HT_PYTHON" in
    /*) ;;
    *) echo "REJECTED (pre-commit): HT_PYTHON must be absolute" >&2; exit 1 ;;
esac
if [ ! -x "$HT_PYTHON" ]; then
    echo "REJECTED (pre-commit): HT_PYTHON is not executable" >&2
    exit 1
fi
exec "$HT_PYTHON" -m ht _precommit
"""
PRECOMMIT_HOOK_BYTES = PRECOMMIT_HOOK.encode("utf-8")

_LEGACY_PRECOMMIT_PREFIXES = (
    (
        "#!/bin/sh\n"
        "# hypothesis-tree pre-commit backstop (A2 §4.2) — thin shim into the ht package.\n"
        "exec "
    ),
    (
        "#!/bin/sh\n"
        "# hypothesis-tree pre-commit backstop (A2 §4.2). Thin shim into the ht package so\n"
        "# the schema + authority logic is never duplicated. Installed by `ht root init`.\n"
        "exec "
    ),
)
_LEGACY_INTERPRETER = re.compile(r"/[A-Za-z0-9_+.,/@%:=~-]+")


def run(root: Path, args: list[str], *, env_extra: dict | None = None,
        check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    internal_fd_text = env.get("HT_INTERNAL_GIT_DIR_FD")
    cwd: str | None = str(root)
    pass_fds: tuple[int, ...] = ()
    preexec_fn: Callable[[], None] | None = None
    if internal_fd_text is not None:
        try:
            internal_fd = int(internal_fd_text)
        except ValueError as exc:
            raise HtError("invalid internal held Git-directory descriptor") from exc
        os.fstat(internal_fd)
        env["GIT_DIR"] = "."
        cwd = None

        def enter_held_git_directory() -> None:
            os.fchdir(internal_fd)

        preexec_fn = enter_held_git_directory
        pass_fds = (internal_fd,)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=capture,
        pass_fds=pass_fds,
        preexec_fn=preexec_fn,
    )


def is_repo(root: Path) -> bool:
    try:
        r = run(root, ["rev-parse", "--is-inside-work-tree"], check=False)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except FileNotFoundError:  # pragma: no cover - git missing
        raise HtError("git not found on PATH")


def has_head(root: Path) -> bool:
    r = run(root, ["rev-parse", "--verify", "HEAD"], check=False)
    return r.returncode == 0


def show(root: Path, ref_path: str) -> str | None:
    """`git show <ref>:<path>` content, or None if absent at that ref."""
    r = run(root, ["show", ref_path], check=False)
    if r.returncode != 0:
        return None
    return r.stdout


def staged_name_status(root: Path) -> list[tuple[str, str]]:
    r = run(root, ["diff", "--cached", "--name-status", "-z"])
    out = r.stdout
    items: list[tuple[str, str]] = []
    tokens = out.split("\x00")
    i = 0
    while i < len(tokens):
        code = tokens[i]
        if not code:
            i += 1
            continue
        # Preserve both sides of a rename/copy. The hook must see a protected
        # source path even when the destination moves outside a state lane.
        if code[0] in ("R", "C"):
            source = tokens[i + 1] if i + 1 < len(tokens) else ""
            destination = tokens[i + 2] if i + 2 < len(tokens) else ""
            items.append((code[0], source))
            items.append((code[0], destination))
            i += 3
        else:
            path = tokens[i + 1] if i + 1 < len(tokens) else ""
            items.append((code[0], path))
            i += 2
    return items


@dataclass(frozen=True)
class HeadSnapshot:
    oid: str
    # Values are (kind, bytes); kind is regular|symlink|special.
    files: dict[str, tuple[str, bytes]]


@dataclass
class RepositoryBinding:
    """Held descriptors for the exact Git repository captured by a transaction."""

    git_dir_fd: int
    common_dir_fd: int
    git_dir_identity: tuple[int, int]
    common_dir_identity: tuple[int, int]
    git_dir_path: Path
    common_dir_path: Path
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            os.close(self.common_dir_fd)
            os.close(self.git_dir_fd)
            self.closed = True


def _controlled_env(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env.update(
        {
            "PATH": _CONTROLLED_PATH,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if env_extra:
        env.update(env_extra)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _controlled_run(
    root: Path,
    args: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    check: bool = True,
    text: bool = True,
    input_data: str | bytes | None = None,
    repository_fd: int | None = None,
    git_dir_fd: int | None = None,
) -> subprocess.CompletedProcess:
    if _GIT_BINARY is None:
        raise HtError("trusted git binary not found on controlled PATH")
    binary = Path(_GIT_BINARY)
    info = binary.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink < 1:
        raise HtError(f"trusted git binary is not a regular executable: {binary}")
    cwd = str(root)
    pass_fds: tuple[int, ...] = ()
    preexec_fn: Callable[[], None] | None = None
    if repository_fd is not None and git_dir_fd is not None:
        raise HtError("child process cannot enter two held repository directories")
    held_fd = git_dir_fd if git_dir_fd is not None else repository_fd
    if held_fd is not None:
        # Darwin's /dev/fd directory entries cannot be used as Popen(cwd=...),
        # even though they can be statted.  Enter the already-held directory in
        # the forked child instead; this is both portable and immune to lexical
        # path replacement between the parent's identity check and exec.
        cwd = None

        def enter_held_repository() -> None:
            os.fchdir(held_fd)

        preexec_fn = enter_held_repository
        pass_fds = (held_fd,)
    controlled_env = _controlled_env(env_extra)
    if git_dir_fd is not None:
        controlled_env["GIT_DIR"] = "."
    return subprocess.run(
        [str(binary), *args],
        cwd=cwd,
        env=controlled_env,
        check=check,
        text=text,
        capture_output=True,
        input=input_data,
        pass_fds=pass_fds,
        preexec_fn=preexec_fn,
    )


def _require_real_repository(
    root: Path,
    *,
    repository_fd: int | None = None,
    repository_identity: tuple[int, int] | None = None,
) -> tuple[Path, Path, tuple[int, int]]:
    if repository_identity is not None:
        try:
            lexical = root.stat()
        except FileNotFoundError as exc:
            raise GitStateChanged("lexical repository root disappeared") from exc
        if (lexical.st_dev, lexical.st_ino) != repository_identity:
            raise GitStateChanged(
                "lexical repository root was replaced after transaction capture"
            )
        if repository_fd is None or (
            os.fstat(repository_fd).st_dev,
            os.fstat(repository_fd).st_ino,
        ) != repository_identity:
            raise GitStateChanged("held repository descriptor changed identity")
    top = _controlled_run(
        root,
        ["rev-parse", "--show-toplevel"],
        check=False,
        repository_fd=repository_fd,
    )
    if top.returncode != 0:
        raise HtError("controlled Git command did not resolve a repository root")
    top_path = Path(top.stdout.strip())
    try:
        top_info = top_path.stat()
    except FileNotFoundError as exc:
        raise GitStateChanged("Git top-level disappeared during identity check") from exc
    expected_identity = repository_identity or (
        root.stat().st_dev,
        root.stat().st_ino,
    )
    if (top_info.st_dev, top_info.st_ino) != expected_identity:
        raise GitStateChanged("Git top-level does not match the held repository")
    common = _controlled_run(
        root,
        ["rev-parse", "--git-common-dir"],
        repository_fd=repository_fd,
    )
    common_path = Path(common.stdout.strip())
    if not common_path.is_absolute():
        common_path = root / common_path
    common_path = common_path.resolve(strict=True)
    if not common_path.is_dir():
        raise HtError("controlled Git common directory is not a directory")
    common_info = common_path.stat()
    common_identity = (common_info.st_dev, common_info.st_ino)
    index = _controlled_run(
        root,
        ["rev-parse", "--git-path", "index"],
        repository_fd=repository_fd,
    )
    index_path = Path(index.stdout.strip())
    if not index_path.is_absolute():
        index_path = root / index_path
    _stable_regular_bytes(index_path, label="Git index")
    return common_path, index_path, common_identity


def capture_repository_binding(
    root: Path,
    repository_fd: int,
    repository_identity: tuple[int, int],
) -> RepositoryBinding | None:
    """Hold the exact Git/control directories before candidate publication.

    ``None`` is returned only for descriptor-rooted synthetic roots that are not
    Git repositories; a real repository whose identity is incoherent fails
    closed.  Once captured, later Git children enter ``git_dir_fd`` directly and
    never rediscover a mutable lexical ``.git`` path.
    """

    probe = _controlled_run(
        root,
        ["rev-parse", "--show-toplevel"],
        check=False,
        repository_fd=repository_fd,
    )
    if probe.returncode != 0:
        return None
    top_path = Path(probe.stdout.strip())
    try:
        top_info = top_path.stat()
        lexical = root.stat()
    except FileNotFoundError as exc:
        raise GitStateChanged("repository identity disappeared during capture") from exc
    if (
        (top_info.st_dev, top_info.st_ino) != repository_identity
        or (lexical.st_dev, lexical.st_ino) != repository_identity
        or (
            os.fstat(repository_fd).st_dev,
            os.fstat(repository_fd).st_ino,
        )
        != repository_identity
    ):
        raise GitStateChanged("repository root changed during Git binding capture")

    git_dir_result = _controlled_run(
        root,
        ["rev-parse", "--absolute-git-dir"],
        repository_fd=repository_fd,
    )
    common_result = _controlled_run(
        root,
        ["rev-parse", "--git-common-dir"],
        repository_fd=repository_fd,
    )
    git_dir_path = Path(git_dir_result.stdout.strip())
    if not git_dir_path.is_absolute():
        git_dir_path = top_path / git_dir_path
    git_dir_path = git_dir_path.resolve(strict=True)
    common_path = Path(common_result.stdout.strip())
    if not common_path.is_absolute():
        common_path = top_path / common_path
    common_path = common_path.resolve(strict=True)

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    git_dir_fd = os.open(git_dir_path, flags)
    try:
        common_dir_fd = os.open(common_path, flags)
    except Exception:
        os.close(git_dir_fd)
        raise
    try:
        git_info = os.fstat(git_dir_fd)
        common_info = os.fstat(common_dir_fd)
        if not stat.S_ISDIR(git_info.st_mode) or not stat.S_ISDIR(common_info.st_mode):
            raise GitStateChanged("captured Git binding is not directory-backed")
        # Recheck every lexical identity after both descriptors are held.  A
        # replacement racing any path resolution is rejected before candidates
        # are installed or Git plumbing is allowed to write.
        if (
            (git_dir_path.stat().st_dev, git_dir_path.stat().st_ino)
            != (git_info.st_dev, git_info.st_ino)
            or (common_path.stat().st_dev, common_path.stat().st_ino)
            != (common_info.st_dev, common_info.st_ino)
            or (root.stat().st_dev, root.stat().st_ino) != repository_identity
        ):
            raise GitStateChanged("Git directory changed during binding capture")
        return RepositoryBinding(
            git_dir_fd=git_dir_fd,
            common_dir_fd=common_dir_fd,
            git_dir_identity=(git_info.st_dev, git_info.st_ino),
            common_dir_identity=(common_info.st_dev, common_info.st_ino),
            git_dir_path=git_dir_path,
            common_dir_path=common_path,
        )
    except Exception:
        os.close(common_dir_fd)
        os.close(git_dir_fd)
        raise


def _stable_regular_bytes(path: Path, *, label: str, executable: bool = False) -> bytes:
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise HtError(f"expected {label} is absent: {path}") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (executable and before.st_mode & 0o111 == 0)
    ):
        raise HtError(f"expected {label} is not a single-link regular file: {path}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
    finally:
        os.close(fd)
    after = path.lstat()
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
    )
    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise HtError(f"{label} changed during stable read: {path}")
    return b"".join(chunks)


def head_snapshot(root: Path, prefixes: list[str]) -> HeadSnapshot:
    """Return one immutable HEAD plus regular/symlink bytes below prefixes."""
    _require_real_repository(root)
    head = _controlled_run(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    oid = head.stdout.strip()
    if not prefixes:
        return HeadSnapshot(oid, {})
    unique = sorted(set(prefixes))
    selected = [
        prefix
        for prefix in unique
        if not any(prefix.startswith(f"{other}/") for other in unique if other != prefix)
    ]
    archive = _controlled_run(
        root,
        ["archive", "--format=tar", oid, "--", *selected],
        text=False,
        check=False,
    )
    if archive.returncode != 0:
        detail = archive.stderr.decode("utf-8", errors="replace")
        raise HtError(f"cannot read frozen Git source view: {detail.strip()}")
    files: dict[str, tuple[str, bytes]] = {}
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as payload:
        for member in payload.getmembers():
            if member.isfile():
                extracted = payload.extractfile(member)
                if extracted is None:
                    raise HtError(f"cannot read archived Git blob {member.name}")
                files[member.name] = ("regular", extracted.read())
            elif member.issym() or member.islnk():
                files[member.name] = ("symlink", member.linkname.encode("utf-8"))
            elif not member.isdir():
                files[member.name] = ("special", b"")
    return HeadSnapshot(oid, files)


@dataclass(frozen=True)
class _OwnedFile:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    mtime_ns: int
    ctime_ns: int
    data: bytes


def _stable_regular_at(
    directory_fd: int,
    name: str,
    *,
    label: str,
    executable: bool = False,
    optional: bool = False,
) -> _OwnedFile | None:
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        if optional:
            return None
        raise HtError(f"expected {label} is absent")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (executable and before.st_mode & 0o111 == 0)
    ):
        raise HtError(f"expected {label} is not a single-link regular file")
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        opened = os.fstat(fd)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
    finally:
        os.close(fd)
    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)

    def identity(value: os.stat_result) -> tuple[int, ...]:
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

    if identity(before) != identity(opened) or identity(opened) != identity(after):
        raise HtError(f"{label} changed during stable descriptor read")
    return _OwnedFile(
        device=opened.st_dev,
        inode=opened.st_ino,
        mode=opened.st_mode,
        links=opened.st_nlink,
        size=opened.st_size,
        mtime_ns=opened.st_mtime_ns,
        ctime_ns=opened.st_ctime_ns,
        data=b"".join(chunks),
    )


def _same_owned_file(left: _OwnedFile, right: _OwnedFile) -> bool:
    return left == right


def _same_file_after_rename(left: _OwnedFile, right: _OwnedFile) -> bool:
    """Compare ownership/content while allowing rename's expected ctime update."""

    return (
        left.device,
        left.inode,
        left.mode,
        left.links,
        left.size,
        left.mtime_ns,
        left.data,
    ) == (
        right.device,
        right.inode,
        right.mode,
        right.links,
        right.size,
        right.mtime_ns,
        right.data,
    )


def _rename_noreplace_at(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renameatx_np
        flag = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = libc.renameat2
        flag = 1  # RENAME_NOREPLACE
    else:  # pragma: no cover - unsupported kernels fail closed
        raise HtError("atomic no-replace Git-artifact rename is unavailable")
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
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        flag,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _rename_exchange_at(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
) -> None:
    """Atomically exchange two directory entries, or fail closed."""

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renameatx_np
        flag = 0x00000002  # RENAME_SWAP
    elif sys.platform.startswith("linux"):
        rename = libc.renameat2
        flag = 2  # RENAME_EXCHANGE
    else:  # pragma: no cover - unsupported kernels fail closed
        raise HtError("atomic Git-hook exchange is unavailable")
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
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        flag,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _remove_private_file(
    directory_fd: int,
    name: str,
    expected: _OwnedFile | None,
) -> str | None:
    current = _stable_regular_at(
        directory_fd,
        name,
        label=f"transaction-private file {name}",
        optional=True,
    )
    if current is None:
        return None
    quarantine = f".ht-repository-retired-file-{uuid4()}"
    _rename_noreplace_at(directory_fd, name, directory_fd, quarantine)
    moved = _stable_regular_at(
        directory_fd,
        quarantine,
        label=f"retired transaction-private file {name}",
    )
    assert moved is not None
    if expected is None or not _same_file_after_rename(moved, expected):
        try:
            _rename_noreplace_at(directory_fd, quarantine, directory_fd, name)
        except OSError as restore_error:
            raise GitStateChanged(
                f"transaction-private file replacement was quarantined; preserved: {name}"
            ) from restore_error
        raise GitStateChanged(
            f"transaction-private file changed ownership; preserved: {name}"
        )
    # No POSIX primitive deletes by proven file handle.  Retain the exact owned
    # inode under its unique Git-internal retirement name as forensic evidence;
    # a later explicit recovery/GC step may remove it under stronger custody.
    os.fsync(directory_fd)
    return quarantine


def _retire_private_directory(
    git_dir_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> str:
    """Atomically quarantine and classify a private directory pathname."""

    quarantine = f".ht-repository-retired-hooks-{uuid4()}"
    _rename_noreplace_at(git_dir_fd, name, git_dir_fd, quarantine)
    moved = os.stat(quarantine, dir_fd=git_dir_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(moved.st_mode)
        or (moved.st_dev, moved.st_ino) != expected_identity
    ):
        try:
            _rename_noreplace_at(git_dir_fd, quarantine, git_dir_fd, name)
        except OSError as restore_error:
            raise GitStateChanged(
                "transaction-private hook replacement was quarantined; preserved"
            ) from restore_error
        raise GitStateChanged(
            "transaction-private hook directory changed ownership; preserved"
        )
    return quarantine


def _controlled_program(
    argv: list[str],
    *,
    env_extra: dict[str, str],
    repository_fd: int,
    git_dir_fd: int,
) -> subprocess.CompletedProcess[str]:
    def enter_held_repository() -> None:
        os.fchdir(repository_fd)

    env = _controlled_env(env_extra)
    env["HT_INTERNAL_GIT_DIR_FD"] = str(git_dir_fd)
    return subprocess.run(
        argv,
        env=env,
        text=True,
        capture_output=True,
        pass_fds=(repository_fd, git_dir_fd),
        preexec_fn=enter_held_repository,
    )


def _write_private_file(
    directory_fd: int, name: str, content: bytes, *, mode: int
) -> _OwnedFile:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        mode,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(fd, mode)
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise HtError(f"short write for transaction-private file {name}")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory_fd)
    created = _stable_regular_at(
        directory_fd, name, label=f"transaction-private file {name}"
    )
    assert created is not None
    return created


def _is_known_legacy_precommit_hook(content: bytes) -> bool:
    """Recognize only the two historical HT shims, with an inert absolute path."""

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    suffix = " -m ht _precommit\n"
    for prefix in _LEGACY_PRECOMMIT_PREFIXES:
        if text.startswith(prefix) and text.endswith(suffix):
            interpreter = text[len(prefix) : -len(suffix)]
            if _LEGACY_INTERPRETER.fullmatch(interpreter) is not None:
                return True
    return False


def install_precommit_hook(root: Path) -> str:
    """Install or safely migrate the exact hook in Git's held common directory.

    The canonical bytes contain no worktree or interpreter identity.  Existing
    content is replaced only when it is absent, already canonical, or one of the
    exact historical HT shim forms.  A migrated inode is retained under its
    unique exchange name as evidence; unknown hooks are never overwritten.
    """

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    repository_fd = os.open(root, flags)
    binding: RepositoryBinding | None = None
    hooks_fd: int | None = None
    try:
        root_info = os.fstat(repository_fd)
        repository_identity = (root_info.st_dev, root_info.st_ino)
        binding = capture_repository_binding(root, repository_fd, repository_identity)
        if binding is None:
            raise HtError("cannot install pre-commit hook outside a Git repository")
        configured_hooks = _controlled_run(
            root,
            ["config", "--get", "core.hooksPath"],
            check=False,
            repository_fd=repository_fd,
        )
        if configured_hooks.returncode == 0:
            raise HtError(
                "refusing to install an inactive pre-commit hook while "
                "core.hooksPath is configured; unset core.hooksPath first"
            )
        if configured_hooks.returncode != 1:
            raise HtError("cannot determine the repository's effective core.hooksPath")
        try:
            hooks_fd = os.open("hooks", flags, dir_fd=binding.common_dir_fd)
        except FileNotFoundError:
            os.mkdir("hooks", 0o700, dir_fd=binding.common_dir_fd)
            os.fsync(binding.common_dir_fd)
            hooks_fd = os.open("hooks", flags, dir_fd=binding.common_dir_fd)

        existing = _stable_regular_at(
            hooks_fd,
            "pre-commit",
            label="pre-commit hook",
            optional=True,
        )
        if existing is not None and existing.data != PRECOMMIT_HOOK_BYTES:
            if not _is_known_legacy_precommit_hook(existing.data):
                raise HtError(
                    "refusing to replace unknown pre-commit hook; preserve or "
                    "remove it explicitly before HT hook installation"
                )
        if (
            existing is not None
            and existing.data == PRECOMMIT_HOOK_BYTES
            and existing.mode & 0o111 != 0
        ):
            return "current"

        exchange_name = f".ht-precommit-migration-{uuid4()}"
        installed = _write_private_file(
            hooks_fd,
            exchange_name,
            PRECOMMIT_HOOK_BYTES,
            mode=0o700,
        )
        if existing is None:
            try:
                _rename_noreplace_at(
                    hooks_fd, exchange_name, hooks_fd, "pre-commit"
                )
            except OSError as exc:
                raise GitStateChanged(
                    "pre-commit hook appeared during atomic installation; preserved state"
                ) from exc
            status = "installed"
        else:
            try:
                _rename_exchange_at(
                    hooks_fd, exchange_name, hooks_fd, "pre-commit"
                )
            except OSError as exc:
                raise GitStateChanged(
                    "pre-commit hook changed during atomic migration; preserved state"
                ) from exc
            retired = _stable_regular_at(
                hooks_fd,
                exchange_name,
                label="retired pre-commit hook",
            )
            assert retired is not None
            if not _same_file_after_rename(retired, existing):
                try:
                    _rename_exchange_at(
                        hooks_fd, exchange_name, hooks_fd, "pre-commit"
                    )
                except OSError as restore_error:
                    raise GitStateChanged(
                        "pre-commit migration raced replacement; preserved exchanged state"
                    ) from restore_error
                raise GitStateChanged(
                    "pre-commit hook changed ownership during migration; restored state"
                )
            status = "migrated"

        os.fsync(hooks_fd)
        os.fsync(binding.common_dir_fd)
        current = _stable_regular_at(
            hooks_fd,
            "pre-commit",
            label="installed pre-commit hook",
            executable=True,
        )
        if current is None or current.data != PRECOMMIT_HOOK_BYTES:
            raise GitStateChanged(
                "installed pre-commit hook does not match the canonical bytes"
            )
        if not _same_file_after_rename(current, installed):
            raise GitStateChanged(
                "installed pre-commit hook changed ownership after atomic installation"
            )
        return status
    finally:
        if hooks_fd is not None:
            os.close(hooks_fd)
        if binding is not None:
            binding.close()
        os.close(repository_fd)


def commit(
    root: Path,
    rel_paths: list[str],
    role: str,
    message: str,
    *,
    expected_parent: str,
    repository_fd: int,
    repository_identity: tuple[int, int],
    binding: RepositoryBinding,
    candidate_bytes: dict[str, bytes],
    verify_worktree: Callable[[], None] | None = None,
) -> None:
    """Commit exactly HEAD+``rel_paths`` through a private, hook-visible index."""
    if not rel_paths or len(rel_paths) != len(set(rel_paths)):
        raise HtError("repository commit path set must be nonempty and duplicate-free")
    if set(candidate_bytes) != set(rel_paths):
        raise HtError("Git candidate bytes do not exactly match commit paths")
    if binding.closed:
        raise HtError("held Git repository binding is closed")

    def controlled(
        args: list[str],
        *,
        env_extra: dict[str, str] | None = None,
        check: bool = True,
        text: bool = True,
        input_data: str | bytes | None = None,
    ) -> subprocess.CompletedProcess:
        return _controlled_run(
            root,
            args,
            env_extra=env_extra,
            check=check,
            text=text,
            input_data=input_data,
            git_dir_fd=binding.git_dir_fd,
        )

    def verify_repository_identity() -> None:
        try:
            lexical = root.stat()
        except FileNotFoundError as exc:
            raise GitStateChanged("repository identity path disappeared") from exc
        if (lexical.st_dev, lexical.st_ino) != repository_identity or (
            os.fstat(repository_fd).st_dev,
            os.fstat(repository_fd).st_ino,
        ) != repository_identity:
            raise GitStateChanged("repository root identity changed during commit")
        git_info = os.fstat(binding.git_dir_fd)
        common_info = os.fstat(binding.common_dir_fd)
        if (git_info.st_dev, git_info.st_ino) != binding.git_dir_identity or (
            common_info.st_dev,
            common_info.st_ino,
        ) != binding.common_dir_identity:
            raise GitStateChanged("held Git repository binding changed identity")
        try:
            visible_git = binding.git_dir_path.stat()
            visible_common = binding.common_dir_path.stat()
        except FileNotFoundError as exc:
            raise GitStateChanged("visible Git repository binding disappeared") from exc
        if (visible_git.st_dev, visible_git.st_ino) != binding.git_dir_identity or (
            visible_common.st_dev,
            visible_common.st_ino,
        ) != binding.common_dir_identity:
            raise GitStateChanged("visible Git repository binding was replaced")

    before = controlled(["rev-parse", "--verify", "HEAD^{commit}"])
    if before.stdout.strip() != expected_parent:
        raise GitStateChanged("repository HEAD changed before candidate commit")

    hooks_fd = os.open(
        "hooks",
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=binding.common_dir_fd,
    )
    hook_row = _stable_regular_at(
        hooks_fd, "pre-commit", label="pre-commit hook", executable=True
    )
    assert hook_row is not None
    if hook_row.data != PRECOMMIT_HOOK_BYTES:
        os.close(hooks_fd)
        raise HtError(
            "installed pre-commit hook is not the exact pinned HT validator shim"
        )
    commit_msg_row = _stable_regular_at(
        hooks_fd,
        "commit-msg",
        label="commit-msg hook",
        executable=True,
        optional=True,
    )
    os.close(hooks_fd)

    real_index_before = _stable_regular_at(
        binding.git_dir_fd, "index", label="Git index"
    )
    assert real_index_before is not None
    private_index = f".ht-repository-index-{uuid4()}"
    private_lock = f"{private_index}.lock"
    private_hooks = f".ht-repository-hooks-{uuid4()}"
    private_hooks_created = False
    private_hooks_fd: int | None = None
    private_hooks_identity: tuple[int, int] | None = None
    private_index_owned: _OwnedFile | None = None
    index_env = {"GIT_INDEX_FILE": private_index}
    trailers = [f"HT-Role: {role}"]
    lane = os.environ.get("HT_LANE", "").strip()
    if lane:
        trailers.append(f"HT-Lane: {lane}")
    full_msg = f"{message}\n\n" + "\n".join(trailers) + "\n"
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    commit_env = {
        **index_env,
        "HT_COMMIT": "1",
        "HT_ROLE": role,
        "HT_PYTHON": sys.executable,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
    }
    publication_landed = False

    def verify_private_index() -> None:
        nonlocal private_index_owned
        row = _stable_regular_at(
            binding.git_dir_fd, private_index, label="private Git index"
        )
        assert row is not None
        for relative in rel_paths:
            staged = controlled(
                ["ls-files", "--stage", "-z", "--", relative],
                env_extra=index_env,
            )
            rows = [item for item in staged.stdout.split("\x00") if item]
            header, separator, staged_path = (
                rows[0].partition("\t") if len(rows) == 1 else ("", "", "")
            )
            fields = header.split()
            if (
                separator != "\t"
                or staged_path != relative
                or len(fields) != 3
                or fields[0] != "100644"
                or fields[2] != "0"
            ):
                raise HtError(
                    f"private Git index does not contain exact 100644 candidate: {relative}"
                )
            expected_blob = controlled(
                ["hash-object", "--stdin"],
                text=False,
                input_data=candidate_bytes[relative],
            ).stdout.decode("ascii").strip()
            if fields[1] != expected_blob:
                raise HtError(f"private Git index blob differs from candidate: {relative}")
        private_index_owned = _stable_regular_at(
            binding.git_dir_fd, private_index, label="private Git index"
        )
        assert private_index_owned is not None

    try:
        verify_repository_identity()
        os.mkdir(private_hooks, 0o700, dir_fd=binding.git_dir_fd)
        private_hooks_created = True
        private_hooks_fd = os.open(
            private_hooks,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=binding.git_dir_fd,
        )
        private_hooks_info = os.fstat(private_hooks_fd)
        if not stat.S_ISDIR(private_hooks_info.st_mode):
            raise GitStateChanged(
                "transaction-owned hook path is not a directory"
            )
        private_hooks_identity = (
            private_hooks_info.st_dev,
            private_hooks_info.st_ino,
        )
        if commit_msg_row is not None:
            _write_private_file(
                private_hooks_fd,
                "commit-msg",
                commit_msg_row.data,
                mode=0o700,
            )
        if verify_worktree is not None:
            verify_worktree()
        controlled(["read-tree", expected_parent], env_extra=index_env)
        private_index_owned = _stable_regular_at(
            binding.git_dir_fd, private_index, label="private Git index"
        )
        assert private_index_owned is not None
        for relative in rel_paths:
            content = candidate_bytes[relative]
            hashed = controlled(
                ["hash-object", "-w", "--stdin"],
                text=False,
                input_data=content,
            )
            if hashed.returncode != 0:
                detail = hashed.stderr.decode("utf-8", errors="replace")
                raise HtError(f"cannot write exact candidate blob: {detail.strip()}")
            oid = hashed.stdout.decode("ascii").strip()
            indexed = controlled(
                ["update-index", "--add", "--cacheinfo", "100644", oid, relative],
                env_extra=index_env,
                check=False,
            )
            if indexed.returncode != 0:
                raise HtError(
                    f"cannot install exact private-index candidate:\n{indexed.stderr}".strip()
                )
            private_index_owned = _stable_regular_at(
                binding.git_dir_fd, private_index, label="private Git index"
            )
            assert private_index_owned is not None
        if verify_worktree is not None:
            verify_worktree()
        verify_private_index()
        tree = controlled(["write-tree"], env_extra=index_env)
        candidate_tree = tree.stdout.strip()
        author = controlled(
            ["var", "GIT_AUTHOR_IDENT"],
            env_extra=commit_env,
        ).stdout.strip()
        committer = controlled(
            ["var", "GIT_COMMITTER_IDENT"],
            env_extra=commit_env,
        ).stdout.strip()
        commit_preimage = (
            f"tree {candidate_tree}\n"
            f"parent {expected_parent}\n"
            f"author {author}\n"
            f"committer {committer}\n\n"
            f"{full_msg}"
        )
        # Hash without ``-w``: a rejected hook leaves no transaction-owned
        # unreachable commit object behind.
        expected = controlled(
            ["hash-object", "-t", "commit", "--stdin"],
            env_extra=commit_env,
            input_data=commit_preimage,
        ).stdout.strip()
        verify_private_index()
        if verify_worktree is not None:
            verify_worktree()
        # Run the exact authority/schema validator directly against the private
        # candidate index.  The subsequent Git commit sees only the immutable
        # transaction-owned hook directory, so a mutable installed-hook swap
        # cannot replace the validator between check and exec.
        validated = _controlled_program(
            [sys.executable, "-m", "ht", "_precommit"],
            env_extra=commit_env,
            repository_fd=repository_fd,
            git_dir_fd=binding.git_dir_fd,
        )
        if validated.returncode != 0:
            raise HtError(
                "pinned pre-commit validator rejected the repository candidate:\n"
                f"{validated.stdout}\n{validated.stderr}".strip()
            )
        verify_private_index()
        verify_repository_identity()
        committed = controlled(
            [
                "-c",
                f"core.hooksPath={private_hooks}",
                "commit",
                "--no-gpg-sign",
                "--cleanup=verbatim",
                "-F",
                "-",
            ],
            env_extra=commit_env,
            input_data=full_msg,
            check=False,
        )
        verify_private_index()
        verify_repository_identity()
        after = controlled(["rev-parse", "--verify", "HEAD^{commit}"], check=False)
        after_oid = after.stdout.strip() if after.returncode == 0 else None
        if after_oid != expected:
            if after_oid == expected_parent:
                raise HtError(
                    "git commit failed (transaction-owned commit-msg hook rejected "
                    f"the write):\n{committed.stdout}\n{committed.stderr}".strip()
                )
            raise GitStateChanged(
                "Git HEAD does not equal the exact expected role-stamped commit; "
                "preserved state for explicit recovery"
            )
        publication_landed = True
        if verify_worktree is not None:
            try:
                verify_worktree()
            except HtError as exc:
                raise GitStateChanged(
                    "exact commit landed but repository candidate metadata changed; "
                    "preserved state for explicit recovery"
                ) from exc

        # The private index keeps unrelated operator staging out of the commit.
        # Reconcile only our committed paths in the real index to the exact new
        # HEAD; every unrelated staged entry remains byte-for-byte semantic state.
        reset = controlled(
            ["reset", "-q", expected, "--", *rel_paths],
            check=False,
        )
        if reset.returncode != 0:
            raise GitStateChanged(
                f"exact commit {expected} landed but real-index reconciliation failed"
            )
        real_index_after = _stable_regular_at(
            binding.git_dir_fd, "index", label="Git index"
        )
        assert real_index_after is not None
        verify_repository_identity()
    except Exception as exc:
        if publication_landed and not isinstance(exc, GitStateChanged):
            raise GitStateChanged(
                "exact commit landed but post-publication reconciliation failed; "
                "preserved worktree state"
            ) from exc
        raise
    finally:
        try:
            _remove_private_file(binding.git_dir_fd, private_lock, None)
            _remove_private_file(
                binding.git_dir_fd,
                private_index,
                private_index_owned,
            )
            if private_hooks_created:
                if private_hooks_fd is None or private_hooks_identity is None:
                    raise GitStateChanged(
                        "transaction-owned hook directory disappeared; preserved state"
                    )
                quarantine = _retire_private_directory(
                    binding.git_dir_fd,
                    private_hooks,
                    private_hooks_identity,
                )
                os.close(private_hooks_fd)
                private_hooks_fd = None
                # As with retired private files, retain the exact directory and
                # its stable hook copies.  Deleting a mutable pathname after an
                # identity check would reopen the replacement-deletion race.
                del quarantine
                os.fsync(binding.git_dir_fd)
        except Exception as cleanup_error:
            if private_hooks_fd is not None:
                os.close(private_hooks_fd)
            if publication_landed:
                raise GitStateChanged(
                    "exact commit landed but transaction cleanup failed; "
                    "preserved published state"
                ) from cleanup_error
            raise
