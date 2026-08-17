"""Shared command helpers: context, id allocation, decision-log entries."""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import authority, gitutil, jsonio
from ..errors import HtError, HtUsageError
from ..paths import Root


@dataclass
class Ctx:
    root: Root
    role: str
    lane: Any = authority.UNASSIGNED_LANE


def today() -> str:
    return datetime.date.today().isoformat()


def slugify(text: str, max_words: int = 4) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    slug = "-".join(words[:max_words])
    return slug or "node"


def load_tree(ctx: Ctx, component: str) -> dict:
    path = ctx.root.tree_json(component)
    if not path.exists():
        raise HtUsageError(f"no such tree '{component}' (run `ht tree init`)")
    return jsonio.load(path)


def load_node(ctx: Ctx, component: str, node_id: str) -> dict:
    path = ctx.root.node_json(component, node_id)
    if not path.exists():
        raise HtUsageError(f"no such node '{node_id}' in tree '{component}'")
    return jsonio.load(path)


def all_node_ids(ctx: Ctx, component: str) -> list[str]:
    nodes_dir = ctx.root.nodes_dir(component)
    if not nodes_dir.is_dir():
        return []
    return [p.parent.name for p in nodes_dir.glob("*/node.json")]


def all_components(ctx: Ctx) -> list[str]:
    trees_dir = ctx.root.trees_dir
    if not trees_dir.is_dir():
        return []
    return [p.parent.name for p in trees_dir.glob("*/tree.json")]


def current_global_epoch(ctx: Ctx) -> int:
    """Read the committed union-global trunk epoch.

    V1 keeps each tree's last-merge stamp locally while allocating globally from
    the maximum epoch in every tree's epoch_history. A read during an in-flight
    merge returns the PRE-merge value, which is semantically correct because the
    trunk has not moved until that merge commit is visible. Writers allocate the
    next value under the global mutex (macro §6; director rider R-Q7).
    """
    epochs = [0]
    committed_docs: list[dict] = []
    if gitutil.is_repo(ctx.root.path) and gitutil.has_head(ctx.root.path):
        listed = gitutil.run(
            ctx.root.path,
            ["ls-tree", "-r", "--name-only", "HEAD", "--", "trees"],
        )
        for rel_path in sorted(
            line
            for line in listed.stdout.splitlines()
            if re.fullmatch(r"trees/[^/]+/tree\.json", line)
        ):
            text = gitutil.show(ctx.root.path, f"HEAD:{rel_path}")
            if text is not None:
                committed_docs.append(json.loads(text))
    else:
        # Bootstrap/direct-library callers may not have a repository or HEAD yet.
        committed_docs = [load_tree(ctx, component) for component in all_components(ctx)]

    for tree in committed_docs:
        epochs.extend(
            row["epoch"]
            for row in tree.get("epoch_history", [])
            if isinstance(row, dict)
            and isinstance(row.get("epoch"), int)
            and not isinstance(row.get("epoch"), bool)
        )
    return max(epochs)


def resolve_tree_for_node(ctx: Ctx, node_id: str, tree_opt: str | None) -> str:
    """Locate the component that owns node_id. With --tree, verify; otherwise
    search all trees and require a unique match (v0 is usually single-tree)."""
    if tree_opt is not None:
        if not ctx.root.node_json(tree_opt, node_id).exists():
            raise HtUsageError(f"no such node '{node_id}' in tree '{tree_opt}'")
        return tree_opt
    matches = [c for c in all_components(ctx) if ctx.root.node_json(c, node_id).exists()]
    if not matches:
        raise HtUsageError(f"no such node '{node_id}' in any tree")
    if len(matches) > 1:
        raise HtUsageError(
            f"node id '{node_id}' is ambiguous across trees {matches}; pass --tree"
        )
    return matches[0]


def node_from_dispatch_id(dispatch_id: str) -> str:
    # d-<node>-<N> -> <node>
    if not dispatch_id.startswith("d-") or "-" not in dispatch_id[2:]:
        raise HtUsageError(f"malformed dispatch id '{dispatch_id}' (expected d-<node>-<N>)")
    return dispatch_id[2:].rsplit("-", 1)[0]


def resolve_dispatch(ctx: Ctx, dispatch_id: str, tree_opt: str | None) -> tuple[str, str]:
    """Return (component, node_id) for a dispatch id, verifying the record exists."""
    node_id = node_from_dispatch_id(dispatch_id)
    component = resolve_tree_for_node(ctx, node_id, tree_opt)
    if not ctx.root.dispatch_json(component, node_id, dispatch_id).exists():
        raise HtUsageError(f"no such dispatch '{dispatch_id}'")
    return component, node_id


def iter_ledger_entries(ctx: Ctx) -> list[tuple[str, str, Path, dict]]:
    """Return every entry in the federated ledger union, deterministically.

    Books auto-mint as directories, so discovery deliberately walks every book
    instead of consulting a registry.  W8 separately rejects orphan book names;
    the union must still see them so a typo can never create a hidden silo.
    """
    entries: list[tuple[str, str, Path, dict]] = []
    if not ctx.root.ledger_dir.is_dir():
        return entries
    for book_dir in sorted(p for p in ctx.root.ledger_dir.iterdir() if p.is_dir()):
        for section in ("user", "research", "observatory"):
            section_dir = book_dir / section
            if not section_dir.is_dir():
                continue
            for path in sorted(section_dir.glob("L-*.json")):
                entries.append((book_dir.name, section, path, jsonio.load(path)))
    return entries


def find_ledger_entry(ctx: Ctx, entry_id: str) -> tuple[str, str, dict]:
    matches = [
        (book, section, doc)
        for book, section, _path, doc in iter_ledger_entries(ctx)
        if doc.get("id") == entry_id
    ]
    if not matches:
        raise HtUsageError(f"no such ledger entry '{entry_id}'")
    if len(matches) > 1:
        locations = [f"{book}/{section}" for book, section, _doc in matches]
        raise HtError(
            f"ledger id '{entry_id}' is duplicated across the union at {locations} "
            "(macro §5 global namespace)"
        )
    return matches[0]


def _id_key(node_id: str) -> list[int]:
    return [int(seg) for seg in node_id.split(".")]


def next_node_id(ctx: Ctx, component: str, parent: str | None) -> str:
    """DP-1 positional id allocation: sibling ordinals append monotonically."""
    ids = all_node_ids(ctx, component)
    if parent is None:
        tops = [_id_key(i)[0] for i in ids if "." not in i]
        return str(max(tops) + 1 if tops else 1)
    prefix = parent + "."
    child_ordinals = [
        _id_key(i)[-1] for i in ids if i.startswith(prefix) and i.count(".") == parent.count(".") + 1
    ]
    nxt = max(child_ordinals) + 1 if child_ordinals else 1
    return f"{parent}.{nxt}"


def next_dispatch_id(ctx: Ctx, component: str, node_id: str) -> tuple[str, int]:
    d_dir = ctx.root.node_dir(component, node_id) / "dispatches"
    n = 0
    if d_dir.is_dir():
        n = len(list(d_dir.glob("*.json")))
    return f"d-{node_id}-{n + 1}", n + 1


def next_ledger_id(ctx: Ctx) -> str:
    # The caller MUST hold the global mutex across this scan and the eventual
    # commit.  Without that critical section, two book-local creates can both
    # observe the same maximum and mint a duplicate global ID (item 1 W2 race).
    nums = []
    for _book, _section, path, _doc in iter_ledger_entries(ctx):
        m = re.fullmatch(r"L-(\d+)", path.stem)
        if m:
            nums.append(int(m.group(1)))
    nxt = max(nums) + 1 if nums else 1
    return f"L-{nxt}"


def load_merge_record(ctx: Ctx, record_id: str) -> dict:
    if not isinstance(record_id, str) or re.fullmatch(r"MR-\d+", record_id) is None:
        raise HtUsageError(
            "merge-record id must have the form MR-<n>; path-like ids are forbidden "
            "(item 1 W6/W7)"
        )
    path = ctx.root.merge_record_json(record_id)
    if not path.exists():
        raise HtUsageError(f"no such merge record '{record_id}'")
    return jsonio.load(path)


def decision_entry(
    move: str,
    *,
    frm: str | None = None,
    to: str | None = None,
    target: str | None = None,
    allocation: str | None = None,
    rationale: str,
    refs: list[str] | None = None,
    epoch: int,
) -> dict:
    return {
        "move": move,
        "from": frm,
        "to": to,
        "target": target,
        "allocation": allocation,
        "rationale": rationale,
        "refs": refs or [],
        "epoch": epoch,
        "date": today(),
    }
