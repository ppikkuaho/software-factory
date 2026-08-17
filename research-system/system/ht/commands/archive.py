"""`ht archive write --dispatch DID --src FILE [--name NAME]` (unit).

Copies a file into nodes/<id>/archive/ (git-ignored) and records archive_ref on
the dispatch. Write-once: an existing destination is refused (U2). The archive
file itself never enters git; only the dispatch archive_ref is committed.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import HtError, HtUsageError
from ..pipeline import DocWrite, Plan, RawFile
from . import _common
from ._common import Ctx


def write(ctx: Ctx, dispatch_id: str, src: str, name: str | None, tree_opt: str | None) -> Plan:
    component, node_id = _common.resolve_dispatch(ctx, dispatch_id, tree_opt)
    src_path = Path(src)
    if not src_path.is_file():
        raise HtUsageError(f"--src '{src}' is not a file")
    dest_name = name or src_path.name
    dest = ctx.root.archive_dir(component, node_id) / dest_name
    if dest.exists():
        raise HtError(
            f"archive '{dest_name}' already exists for node {node_id} — archives are "
            f"write-once (U2 / B4 §9)"
        )

    path = ctx.root.dispatch_json(component, node_id, dispatch_id)
    old = _common.jsonio.load(path)
    new = dict(old)
    new["archive_ref"] = ctx.root.rel(dest)

    return Plan(
        role=ctx.role,
        message=f"ht archive write: {dest_name} for {dispatch_id}",
        writes=[DocWrite(path, "dispatch", old, new)],
        raw_files=[RawFile(dest=dest, content=src_path.read_bytes(), gitignored=True)],
    )
