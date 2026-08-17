"""Wholesale index regeneration (A1 §6; J7 — no incremental machinery).

index.json and index.live.json are rebuilt from scratch from every node.json on
every mutation. Both are canonical:

- index.json: full, depth-ordered (a partial top-down read is a valid read).
- index.live.json: the live view the director reads each turn — open/parked/worked
  nodes + their ancestors, and each maximal fully-dead subtree (all closed/merged)
  collapsed to its root line carrying a rollup marker.
"""

from __future__ import annotations

from pathlib import Path

from . import jsonio
from .paths import Root

LIVE_STATUSES = {"unexplored", "worked", "parked"}
DEAD_STATUSES = {"closed", "merged"}


def _id_key(node_id: str) -> list[int]:
    # positional ids like "2.3.1" sort in depth order as tuples of ints
    return [int(seg) for seg in node_id.split(".")]


def _premise_line(premise: str) -> str:
    first = premise.strip().splitlines()[0] if premise.strip() else ""
    return first if len(first) <= 120 else first[:117] + "..."


def _latest_fork_epoch(node: dict) -> int | None:
    gb = node.get("git_binding")
    if not gb or not gb.get("fork"):
        return None
    return gb["fork"][-1]["epoch"]


def _unsettled(node: dict) -> bool:
    return any(c.get("settlement") == "pending" for c in node.get("conflicts", []))


def _load_nodes(root: Root, component: str) -> list[dict]:
    nodes_dir = root.nodes_dir(component)
    nodes: list[dict] = []
    if nodes_dir.is_dir():
        for node_json in nodes_dir.glob("*/node.json"):
            nodes.append(jsonio.load(node_json))
    nodes.sort(key=lambda n: _id_key(n["id"]))
    return nodes


def _entry(node: dict) -> dict:
    return {
        "id": node["id"],
        "premise_line": _premise_line(node["premise"]),
        "status": node["status"],
        "standing": node["standing"],
        "latest_fork_epoch": _latest_fork_epoch(node),
        "unsettled_conflict": _unsettled(node),
    }


def _children_map(nodes: list[dict]) -> dict[str, list[str]]:
    kids: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for n in nodes:
        p = n.get("parent")
        if p is not None and p in kids:
            kids[p].append(n["id"])
    return kids


def build_from_documents(tree: dict, nodes: list[dict]) -> tuple[dict, dict]:
    """Build both indexes from one caller-frozen repository document view."""
    nodes = sorted(nodes, key=lambda n: _id_key(n["id"]))
    by_id = {n["id"]: n for n in nodes}
    kids = _children_map(nodes)

    # subtree status rollup: is a node's whole subtree dead?
    dead: dict[str, bool] = {}
    dead_counts: dict[str, dict[str, int]] = {}

    def compute(nid: str) -> None:
        node = by_id[nid]
        closed = 1 if node["status"] == "closed" else 0
        merged = 1 if node["status"] == "merged" else 0
        subtree_dead = node["status"] in DEAD_STATUSES
        total = 1
        for c in kids[nid]:
            compute(c)
            subtree_dead = subtree_dead and dead[c]
            closed += dead_counts[c]["closed"]
            merged += dead_counts[c]["merged"]
            total += dead_counts[c]["total"]
        dead[nid] = subtree_dead
        dead_counts[nid] = {"closed": closed, "merged": merged, "total": total}

    roots = [n["id"] for n in nodes if n.get("parent") is None or n.get("parent") not in by_id]
    for r in roots:
        compute(r)

    def has_live_descendant(nid: str) -> bool:
        node = by_id[nid]
        if node["status"] in LIVE_STATUSES:
            return True
        return any(has_live_descendant(c) for c in kids[nid])

    full_index = {
        "cursor": tree["cursor"],
        "epoch": tree["epoch"],
        "nodes": [_entry(n) for n in nodes],
    }

    live_nodes: list[dict] = []
    for n in nodes:
        nid = n["id"]
        parent = n.get("parent")
        if dead[nid] and (parent is None or parent not in by_id or not dead[parent]):
            # root of a maximal dead subtree -> collapse to one rollup line
            entry = _entry(n)
            counts = dead_counts[nid]
            entry["rollup"] = {
                "collapsed": counts["total"] - 1,
                "closed": counts["closed"],
                "merged": counts["merged"],
            }
            live_nodes.append(entry)
        elif dead[nid]:
            # inside a larger dead subtree -> omit
            continue
        elif n["status"] in LIVE_STATUSES or has_live_descendant(nid):
            live_nodes.append(_entry(n))
        # else: dead leaf already handled above

    live_index = {
        "cursor": tree["cursor"],
        "epoch": tree["epoch"],
        "nodes": live_nodes,
    }
    return full_index, live_index


def build(root: Root, component: str) -> tuple[dict, dict]:
    return build_from_documents(
        jsonio.load(root.tree_json(component)),
        _load_nodes(root, component),
    )


def regenerate(root: Root, component: str) -> list[Path]:
    """Rebuild + write both index files; return the paths written."""
    full_index, live_index = build(root, component)
    full_path = root.index_json(component)
    live_path = root.index_live_json(component)
    jsonio.dump(full_path, full_index)
    jsonio.dump(live_path, live_index)
    return [full_path, live_path]
