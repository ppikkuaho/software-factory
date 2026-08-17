"""Deterministic, uncitable whole-system projection for the PC (F10)."""

from __future__ import annotations

import re
from typing import Any

from . import jsonio
from .paths import Root


_ISSUE_MINT = re.compile(r"issue#(?P<id>I-[0-9]+)")


def build_from_documents(
    trees: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build from one caller-frozen repository document view."""
    edges: list[dict[str, str]] = []
    for tree_row in trees:
        component = tree_row["component"]
        for node_row in tree_row["nodes"]:
            node = node_row["node"]
            node_id = str(node["id"])
            minted_from = node.get("minted_from")
            match = _ISSUE_MINT.fullmatch(minted_from) if isinstance(minted_from, str) else None
            if match is not None:
                edges.append(
                    {
                        "issue_ref": match.group("id"),
                        "tree": component,
                        "node_ref": f"node#{node_id}",
                        "source": "minted_from",
                        "source_ref": f"node#{node_id}",
                    }
                )
            for dispatch in node_row["dispatches"]:
                issue_ref = dispatch.get("issue_ref")
                if isinstance(issue_ref, str):
                    edges.append(
                        {
                            "issue_ref": issue_ref,
                            "tree": component,
                            "node_ref": f"node#{node_id}",
                            "source": "issue_ref",
                            "source_ref": f"dispatch#{dispatch['id']}",
                        }
                    )
    edges.sort(
        key=lambda edge: (
            edge["issue_ref"],
            edge["tree"],
            edge["node_ref"],
            edge["source"],
            edge["source_ref"],
        )
    )
    return {
        "generated": True,
        "citable": False,
        "trees": trees,
        "issues": issues,
        "issue_edges": edges,
    }


def build(root: Root) -> dict[str, Any]:
    """Return a mechanical union; copy canonical bytes semantically, derive no standing."""
    trees: list[dict[str, Any]] = []
    for tree_path in sorted(root.trees_dir.glob("*/tree.json")):
        component = tree_path.parent.name
        nodes: list[dict[str, Any]] = []
        for node_path in sorted(
            root.nodes_dir(component).glob("*/node.json"),
            key=lambda path: path.parent.name,
        ):
            dispatches = [
                jsonio.load(path)
                for path in sorted(
                    node_path.parent.glob("dispatches/*.json"),
                    key=lambda path: path.name,
                )
            ]
            nodes.append({"node": jsonio.load(node_path), "dispatches": dispatches})
        trees.append(
            {"component": component, "tree": jsonio.load(tree_path), "nodes": nodes}
        )
    issues = [
        jsonio.load(path)
        for path in sorted((root.tier1_dir / "issues").glob("I-*.json"))
    ]
    return build_from_documents(trees, issues)


def regenerate(root: Root):
    jsonio.dump(root.composed_tree_json, build(root))
    return root.composed_tree_json
