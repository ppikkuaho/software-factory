"""Harness registration of immutable observatory report-card artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .. import gitutil
from ..errors import HtError, HtUsageError
from ..pipeline import Plan, RawFile
from ..references import parse_ref
from ._common import Ctx


def _stable_bytes(path: Path) -> tuple[bytes, tuple[int, int, int, int, int]]:
    try:
        before = path.stat()
        if not path.is_file() or path.is_symlink():
            raise HtUsageError(f"report card '{path}' must be a regular non-symlink file")
        content = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise HtUsageError(f"cannot read report card '{path}': {exc}") from exc
    before_fp = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_fp = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_fp != after_fp or len(content) != after.st_size:
        raise HtError(f"report card '{path}' changed during stable read; retry")
    return content, after_fp


def _assert_fresh_confined_destination(ctx: Ctx, dest: Path, canonical: str) -> None:
    root = ctx.root.path.resolve()
    base = (ctx.root.readout_dir / "observatory").absolute()
    lexical = dest.absolute()
    if not lexical.is_relative_to(base) or not base.is_relative_to(root):
        raise HtError(f"observatory report {canonical} destination escapes its lane")
    cursor = root
    for part in lexical.relative_to(root).parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise HtError(
                f"observatory report {canonical} destination has symlinked ancestor {cursor}"
            )
        if cursor.exists() and not cursor.is_dir():
            raise HtError(
                f"observatory report {canonical} destination ancestor is not a directory"
            )
    if dest.exists() or dest.is_symlink():
        raise HtError(
            f"observatory report {canonical} already exists; report cards are write-once"
        )
    relative = lexical.relative_to(root).as_posix()
    history = gitutil.run(
        ctx.root.path,
        ["log", "--all", "--format=%H", "--", relative],
        check=False,
    )
    if history.returncode != 0 or history.stdout.strip():
        raise HtError(
            f"observatory report {canonical} reuses a historical path; report cards "
            "are write-once"
        )


def register(ctx: Ctx, run_id: str, report_card: str) -> Plan:
    if ctx.role != "harness":
        raise HtError(
            f"role '{ctx.role}' may not register observatory reports "
            "(owner: harness)"
        )
    parsed = parse_ref(f"observatory-report#{run_id}")
    if parsed.kind != "observatory-report":  # pragma: no cover - grammar invariant
        raise HtUsageError("invalid observatory run id")
    source = Path(report_card).expanduser()
    content, fingerprint = _stable_bytes(source)
    digest = hashlib.sha256(content).hexdigest()
    dest = ctx.root.observatory_report_card(parsed.object_id)
    _assert_fresh_confined_destination(ctx, dest, parsed.canonical)

    def unchanged() -> None:
        current, current_fingerprint = _stable_bytes(source)
        if (
            current_fingerprint != fingerprint
            or hashlib.sha256(current).hexdigest() != digest
        ):
            raise HtError(f"report card '{source}' changed before registration commit")
        _assert_fresh_confined_destination(ctx, dest, parsed.canonical)

    return Plan(
        role=ctx.role,
        message=f"ht observatory register: {parsed.canonical} (sha256 {digest[:12]})",
        raw_files=[RawFile(dest=dest, content=content, gitignored=False)],
        semantic=unchanged,
    )
