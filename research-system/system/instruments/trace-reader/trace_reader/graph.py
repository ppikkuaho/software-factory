"""Parent-chain graph, active-path walk, and branch grouping (C6 §2, §3, §8).

The graph is built over EVERY uuid-bearing row regardless of type — a graph over
message rows only *snaps* at rewind points (verified the hard way: the founding
session's active path collapsed 536 -> 10 until non-message uuid rows were
included). Type filtering happens at emit time, never here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BranchGroup:
    branch_id: str            # e.g. "main/b3" or "agent-<id>/b1"
    root_uuid: str            # root-most abandoned ancestor
    root_line: int            # its file line
    fork_parent_uuid: str | None   # on-path parent (fork point), or None for chain-break roots
    fork_line: int | None     # file line of the fork-point row on the active path
    member_uuids: set         # every abandoned uuid in this group


@dataclass
class ActorGraph:
    node: dict                # uuid -> (line, obj)
    active: set               # uuids on the active path
    final_uuid: str | None    # final uuid-bearing row (leaf we walked from)
    groups: list              # list[BranchGroup]
    uuid_to_branch: dict      # abandoned uuid -> BranchGroup


def build_node_index(rows) -> dict:
    node = {}
    for line, obj in rows:
        if not isinstance(obj, dict):
            continue
        u = obj.get("uuid")
        if u is not None:
            # first writer wins on the (pathological) duplicate; determinism only
            node.setdefault(u, (line, obj))
    return node


def final_uuid(rows) -> str | None:
    leaf = None
    for _line, obj in rows:
        if isinstance(obj, dict) and obj.get("uuid") is not None:
            leaf = obj["uuid"]
    return leaf


def active_path(node: dict, leaf: str | None) -> set:
    active = set()
    cur = leaf
    while cur is not None and cur in node and cur not in active:
        active.add(cur)
        cur = node[cur][1].get("parentUuid")
    return active


def _rootmost(node, active, uuid):
    """Walk up until the parent is on-path or absent. Return (root_uuid, fork_parent_uuid)."""
    cur = uuid
    while True:
        parent = node[cur][1].get("parentUuid")
        if parent in active:
            return cur, parent
        if parent is None or parent not in node:
            return cur, None
        cur = parent


def branch_groups(actor: str, node: dict, active: set) -> ActorGraph:
    abandoned = [u for u in node if u not in active]

    # collect the root-most ancestor of every abandoned row
    roots = {}   # root_uuid -> (fork_parent_uuid, members set)
    membership = {}   # abandoned uuid -> root_uuid
    for u in abandoned:
        root, fork_parent = _rootmost(node, active, u)
        membership[u] = root
        entry = roots.get(root)
        if entry is None:
            roots[root] = [fork_parent, {u}]
        else:
            entry[1].add(u)

    # deterministic numbering: by fork position on the active path, then by the
    # group root's own file position (C6 §3 "by fork position ... then file order")
    def sort_key(root_uuid):
        fork_parent, _members = roots[root_uuid]
        root_line = node[root_uuid][0]
        fork_line = node[fork_parent][0] if fork_parent is not None else root_line
        return (fork_line, root_line)

    ordered_roots = sorted(roots, key=sort_key)

    groups = []
    root_to_group = {}
    for i, root_uuid in enumerate(ordered_roots, start=1):
        fork_parent, members = roots[root_uuid]
        g = BranchGroup(
            branch_id=f"{actor}/b{i}",
            root_uuid=root_uuid,
            root_line=node[root_uuid][0],
            fork_parent_uuid=fork_parent,
            fork_line=node[fork_parent][0] if fork_parent is not None else None,
            member_uuids=members,
        )
        groups.append(g)
        root_to_group[root_uuid] = g

    uuid_to_branch = {u: root_to_group[membership[u]] for u in abandoned}
    return ActorGraph(
        node=node,
        active=active,
        final_uuid=None,  # filled by caller
        groups=groups,
        uuid_to_branch=uuid_to_branch,
    )


def analyze(actor: str, rows) -> ActorGraph:
    node = build_node_index(rows)
    leaf = final_uuid(rows)
    active = active_path(node, leaf)
    g = branch_groups(actor, node, active)
    g.final_uuid = leaf
    return g
