"""Anchor resolution (B4 §9 — claims must cite >=1 resolvable anchor).

Anchor shape everywhere: {path (research-root-relative), start_line, end_line}.
Resolvable == file exists AND 1 <= start_line <= end_line <= line-count.
R-i6-3 additionally makes generated readout and index views mechanically
uncitable after path normalization, with paths outside the root rejected fail-closed.
"""

from __future__ import annotations

from . import classify
from .errors import HtError
from .paths import Root


def resolve(root: Root, anchor: dict) -> None:
    """Raise HtError if the anchor does not resolve."""
    path = anchor["path"]
    start = anchor["start_line"]
    end = anchor["end_line"]
    target = root.resolve_rel(path)
    try:
        rel = target.relative_to(root.path.resolve())
    except ValueError:
        raise HtError(
            f"anchor path '{path}' resolves outside the research root "
            f"([R-i6-3] fail-closed; B4 §9 anchor resolution)"
        )
    normalized = rel.as_posix()
    if classify.is_generated_view(normalized):
        raise HtError(
            f"anchor path '{path}' targets a generated view ('{normalized}') — "
            f"generated views are uncitable as claim evidence "
            f"([R-i6-3] mechanical path-class ban)"
        )
    if not target.is_file():
        raise HtError(
            f"anchor path '{path}' does not resolve to a file (B4 §9 anchor resolution)"
        )
    line_count = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
    if not (1 <= start <= end <= line_count):
        raise HtError(
            f"anchor '{path}:{start}:{end}' out of range "
            f"(file has {line_count} lines; need 1<=start<=end<=count) "
            f"(B4 §9 anchor resolution)"
        )


def resolve_all(root: Root, anchor_list: list[dict]) -> None:
    if not anchor_list:
        raise HtError("claim cites zero anchors (B4 §9 — >=1 resolvable anchor required)")
    for a in anchor_list:
        resolve(root, a)
