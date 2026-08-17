"""`ht report submit --dispatch DID --src FILE` (unit).

Copies the report to nodes/<id>/reports/<dispatch_id>-report.md and records its
sha256 in dispatch.report_hash. Freeze (U2 / B4 §9): a dispatch whose report_hash
is already set refuses re-submission; the pre-commit hook separately rejects any
later modification of a submitted report file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .. import gitutil
from ..errors import HtError, HtUsageError
from ..pipeline import DocWrite, Plan, RawFile
from . import _common
from ._common import Ctx


def _assert_fresh_report_destination(
    ctx: Ctx, dest: Path, dispatch_id: str
) -> None:
    root = ctx.root.path.resolve()
    lexical = dest.absolute()
    if not lexical.is_relative_to(root):
        raise HtError(f"report destination for {dispatch_id} escapes the research root")
    cursor = root
    for part in lexical.relative_to(root).parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise HtError(
                f"report destination for {dispatch_id} has symlinked ancestor {cursor}"
            )
        if cursor.exists() and not cursor.is_dir():
            raise HtError(f"report destination ancestor for {dispatch_id} is not a directory")
    if dest.exists() or dest.is_symlink():
        raise HtError(
            f"report path for {dispatch_id} already exists; reports are write-once"
        )
    relative = lexical.relative_to(root).as_posix()
    history = gitutil.run(
        ctx.root.path,
        ["log", "--all", "--format=%H", "--", relative],
        check=False,
    )
    if history.returncode != 0 or history.stdout.strip():
        raise HtError(
            f"report path for {dispatch_id} was used historically; reports are write-once"
        )


def submit(ctx: Ctx, dispatch_id: str, src: str, tree_opt: str | None) -> Plan:
    component, node_id = _common.resolve_dispatch(ctx, dispatch_id, tree_opt)
    path = ctx.root.dispatch_json(component, node_id, dispatch_id)
    old = _common.jsonio.load(path)
    if old.get("report_hash"):
        raise HtError(
            f"report already submitted for {dispatch_id} — reports are frozen at submit "
            f"(U2 / B4 §9); no re-submission or edits"
        )

    src_path = Path(src)
    if not src_path.is_file():
        raise HtUsageError(f"--src '{src}' is not a file")
    content = src_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    dest = ctx.root.reports_dir(component, node_id) / f"{dispatch_id}-report.md"
    _assert_fresh_report_destination(ctx, dest, dispatch_id)

    new = dict(old)
    new["report_ref"] = dest.absolute().relative_to(ctx.root.path.resolve()).as_posix()
    new["report_hash"] = digest

    return Plan(
        role=ctx.role,
        message=f"ht report submit: {dispatch_id} (sha256 {digest[:12]})",
        writes=[DocWrite(path, "dispatch", old, new)],
        raw_files=[RawFile(dest=dest, content=content, gitignored=False)],
        semantic=lambda: _assert_fresh_report_destination(ctx, dest, dispatch_id),
    )
